import os
import http.client
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor
import unittest
import uuid

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "TEST_DATABASE_URL is not configured")
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        env = {**os.environ, "DATABASE_URL": DATABASE_URL}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True)

    def setUp(self):
        with self.psycopg.connect(DATABASE_URL) as conn:
            conn.execute("TRUNCATE integration_jobs, demo_crm, crm_entities, crm_tracking, lead_events, lead_cases RESTART IDENTITY CASCADE")

    def test_readiness_checks_database_and_inbox_table(self):
        from app.db.leads import database_ready
        self.assertTrue(database_ready(DATABASE_URL))
        with self.assertRaises(Exception):
            database_ready("postgresql://bad:bad@127.0.0.1:1/missing")

    def test_duplicate_and_changed_payload(self):
        from test_lead_ingest import LEAD, validate
        from app.db.leads import save_lead
        lead = validate(LEAD, 1000000)
        self.assertEqual(save_lead(DATABASE_URL, lead), "created")
        self.assertEqual(save_lead(DATABASE_URL, lead), "duplicate")
        self.assertEqual(save_lead(DATABASE_URL, {**lead, "name": "Changed"}), "conflict")
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM lead_events").fetchone()[0], 1)

    def test_concurrent_delivery_is_idempotent(self):
        from test_lead_ingest import LEAD, validate
        from app.db.leads import save_lead
        lead = validate(LEAD, 1000000)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: save_lead(DATABASE_URL, lead), range(8)))
        self.assertEqual(results.count("created"), 1)
        self.assertEqual(results.count("duplicate"), 7)

    def test_expired_lease_fencing_and_retry_limit(self):
        with self.psycopg.connect(DATABASE_URL) as conn:
            conn.execute("INSERT INTO lead_events (source_id, payload_hash, payload) VALUES ('id', %s, '{}')", ("a" * 64,))
        first, second = uuid.uuid4(), uuid.uuid4()
        with self.psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute("SELECT id FROM claim_lead(%s, 1, 2)", (first,)).fetchone()
            self.assertIsNotNone(row)
            conn.execute("UPDATE lead_events SET lease_until = now() - interval '1 second'")
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertIsNotNone(conn.execute("SELECT id FROM claim_lead(%s, 60, 2)", (second,)).fetchone())
            self.assertFalse(conn.execute("SELECT finish_lead(%s, %s, true)", (row[0], first)).fetchone()[0])
            self.assertTrue(conn.execute("SELECT finish_lead(%s, %s, false, 2, 0)", (row[0], second)).fetchone()[0])
            self.assertEqual(conn.execute("SELECT status FROM lead_events").fetchone()[0], "failed")

    def test_http_persists_across_receiver_restart(self):
        from app.api.webhook import make_handler
        from test_lead_ingest import LEAD, TOKEN

        def send():
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DATABASE_URL, TOKEN, 1000000))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                client.request("POST", "/webhook/tilda", urlencode(LEAD), {
                    "Content-Type": "application/x-www-form-urlencoded", "X-Webhook-Key": TOKEN})
                response = client.getresponse()
                result = (response.status, response.read())
                client.close()
                return result
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

        self.assertIn(b'accepted', send()[1])
        self.assertIn(b'duplicate', send()[1])
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM lead_events").fetchone()[0], 1)

    def test_same_customer_and_service_within_day_creates_one_case(self):
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        lead = validate(LEAD, 1000000)
        self.assertEqual(save_lead(DATABASE_URL, lead), "created")
        self.assertEqual(save_lead(DATABASE_URL, {**lead, "source_id": "different"}), "created")
        with self.psycopg.connect(DATABASE_URL) as conn:
            for _ in range(2):
                token = uuid.uuid4()
                event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
                self.assertIsNotNone(event)
                result = conn.execute("SELECT duplicate FROM process_lead(%s, %s)", (event[0], token)).fetchone()
                self.assertIsNotNone(result)
            self.assertEqual(conn.execute("SELECT occurrences FROM lead_cases").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM lead_cases").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM lead_events WHERE status = 'completed'").fetchone()[0], 2)

    def test_demo_crm_delivery_telegram_and_followup(self):
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        from app.integrations.worker import DemoCRM, deliver_once, enqueue_reminders
        lead = validate(LEAD, 1000000)
        save_lead(DATABASE_URL, lead)
        with self.psycopg.connect(DATABASE_URL) as conn:
            token = uuid.uuid4()
            event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
            case = conn.execute("SELECT result_case_id FROM process_lead(%s, %s)", (event[0], token)).fetchone()[0]
        class FakeTelegram:
            def __init__(self):
                self.sent = []
            def notify(self, case_id, payload):
                self.sent.append(("new", case_id))
            def remind(self, case_id):
                self.sent.append(("reminder", case_id))
        telegram = FakeTelegram()
        crm = DemoCRM(DATABASE_URL)
        self.assertTrue(deliver_once(DATABASE_URL, crm, telegram))
        self.assertTrue(deliver_once(DATABASE_URL, crm, telegram))
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM demo_crm").fetchone()[0], 1)
            conn.execute("UPDATE lead_cases SET first_seen_at = now() - interval '2 hours'")
        enqueue_reminders(DATABASE_URL, 60)
        enqueue_reminders(DATABASE_URL, 60)
        self.assertTrue(deliver_once(DATABASE_URL, crm, telegram))
        self.assertEqual(telegram.sent, [("new", case), ("reminder", case)])
        self.assertFalse(deliver_once(DATABASE_URL, crm, telegram))
        with self.psycopg.connect(DATABASE_URL) as conn:
            stats = conn.execute("SELECT received_events, unique_cases, new_cases FROM demo_analytics").fetchone()
            self.assertEqual(stats, (1, 1, 1))

    def test_espo_followup_checks_latest_stage_before_sending(self):
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        from app.integrations.providers import CRMResult, EspoCRM
        from app.integrations.worker import deliver_once, enqueue_reminders, refresh_stage
        class FakeEspo(EspoCRM):
            def __init__(self):
                self.stage = "Prospecting"
            def sync(self, case_id, lead):
                return CRMResult("c1", "o1")
            def get_stage(self, remote_id):
                return self.stage
        class FakeTelegram:
            def __init__(self):
                self.reminders = 0
            def notify(self, case_id, lead):
                pass
            def remind(self, case_id):
                self.reminders += 1
        save_lead(DATABASE_URL, validate(LEAD, 1000000))
        with self.psycopg.connect(DATABASE_URL) as conn:
            token = uuid.uuid4()
            event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
            case_id = conn.execute("SELECT result_case_id FROM process_lead(%s, %s)", (event[0], token)).fetchone()[0]
            conn.execute("UPDATE lead_cases SET first_seen_at = now() - interval '2 hours'")
        crm, telegram = FakeEspo(), FakeTelegram()
        self.assertTrue(deliver_once(DATABASE_URL, crm, telegram))
        self.assertTrue(deliver_once(DATABASE_URL, crm, telegram))
        enqueue_reminders(DATABASE_URL, 60, "espocrm", "Prospecting")
        self.assertFalse(deliver_once(DATABASE_URL, crm, telegram))
        self.assertEqual(refresh_stage(DATABASE_URL, crm, case_id), "Prospecting")
        enqueue_reminders(DATABASE_URL, 60, "espocrm", "Prospecting")
        crm.stage = "Qualification"
        self.assertTrue(deliver_once(DATABASE_URL, crm, telegram))
        self.assertEqual(telegram.reminders, 0)

    def test_retry_and_ambiguous_telegram_are_recorded(self):
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        from app.integrations.worker import DemoCRM, deliver_once
        from app.integrations.providers import Retryable, Uncertain
        save_lead(DATABASE_URL, validate(LEAD, 1000000))
        with self.psycopg.connect(DATABASE_URL) as conn:
            token = uuid.uuid4()
            event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
            conn.execute("SELECT * FROM process_lead(%s, %s)", (event[0], token))
        class FailingCRM:
            def sync(self, case_id, lead):
                raise Retryable("rate_limited", 120)
        class UncertainTelegram:
            def notify(self, case_id, lead):
                raise Uncertain("timeout")
        self.assertTrue(deliver_once(DATABASE_URL, FailingCRM(), UncertainTelegram()))
        self.assertTrue(deliver_once(DATABASE_URL, DemoCRM(DATABASE_URL), UncertainTelegram()))
        with self.psycopg.connect(DATABASE_URL) as conn:
            rows = conn.execute("SELECT destination, status FROM integration_jobs ORDER BY destination").fetchall()
            self.assertEqual(rows, [("crm", "pending"), ("telegram", "uncertain")])
            delay = conn.execute("SELECT extract(epoch FROM (available_at - now())) FROM integration_jobs WHERE destination = 'crm'").fetchone()[0]
            self.assertGreater(delay, 100)

    def test_storage_failure_does_not_report_success(self):
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        with self.assertRaises(Exception):
            save_lead("postgresql://bad:bad@127.0.0.1:1/missing", validate(LEAD, 1))
