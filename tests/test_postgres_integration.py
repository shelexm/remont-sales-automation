import os
import http.client
import subprocess
import sys
import threading
import time
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

    def test_creation_journal_allows_retries_only_after_confirmed_rejection(self):
        from app.db.integrations import Journal
        journal = Journal(DATABASE_URL)
        self.assertTrue(journal.begin_create('Contact', 'test-key'))
        self.assertFalse(journal.begin_create('Contact', 'test-key'))
        journal.reset_create('Contact', 'test-key')
        self.assertTrue(journal.begin_create('Contact', 'test-key'))
        journal.found('Contact', 'test-key', 'remote123')
        journal.reset_create('Contact', 'test-key')
        self.assertFalse(journal.begin_create('Contact', 'test-key'))
        with self.psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute("SELECT remote_id, create_started FROM crm_entities WHERE entity_type='Contact' AND external_key='test-key'").fetchone()
        self.assertEqual(row, ('remote123', True))

    def test_provider_binding_prevents_switch_and_cross_account_reuse(self):
        from app.db.integrations import ensure_provider_binding
        ensure_provider_binding(DATABASE_URL, "bitrix24", "account-a")
        ensure_provider_binding(DATABASE_URL, "bitrix24", "account-a")
        for provider, identity in (("bitrix24", "account-b"), ("espocrm", "account-a"), ("demo", "local")):
            with self.subTest(provider=provider, identity=identity), self.assertRaises(ValueError):
                ensure_provider_binding(DATABASE_URL, provider, identity, adopt_legacy=True)

    def test_bitrix_cannot_adopt_existing_unbound_crm_history(self):
        from app.db.integrations import ensure_provider_binding
        with self.psycopg.connect(DATABASE_URL) as conn:
            conn.execute("INSERT INTO crm_entities(entity_type, external_key, create_started) VALUES ('Contact','old',true)")
        with self.assertRaises(ValueError):
            ensure_provider_binding(DATABASE_URL, "bitrix24", "account-a", adopt_legacy=True)
        with self.assertRaises(ValueError):
            ensure_provider_binding(DATABASE_URL, "espocrm", "account-a")
        ensure_provider_binding(DATABASE_URL, "espocrm", "account-a", adopt_legacy=True)
        with self.assertRaises(ValueError):
            ensure_provider_binding(DATABASE_URL, "bitrix24", "account-a")

    def test_concurrent_provider_binding_has_only_one_winner(self):
        from app.db.integrations import ensure_provider_binding

        def bind(provider):
            try:
                ensure_provider_binding(DATABASE_URL, provider, "account")
                return "ok"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(bind, ["demo", "bitrix24"]))
        self.assertCountEqual(results, ["ok", "rejected"])

    def test_unrecognized_journal_is_not_adopted(self):
        from app.db.integrations import ensure_provider_binding
        with self.psycopg.connect(DATABASE_URL) as conn:
            conn.execute("INSERT INTO crm_entities(entity_type, external_key, create_started) VALUES ('other-provider','old',true)")
        for provider in ("demo", "espocrm", "bitrix24"):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                ensure_provider_binding(DATABASE_URL, provider, "account", adopt_legacy=True, adopt_mixed=True)

    def test_mixed_legacy_history_needs_separate_acknowledgement(self):
        from app.db.integrations import DemoCRM, ensure_provider_binding
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        lead = validate(LEAD, 1000000)
        save_lead(DATABASE_URL, lead)
        with self.psycopg.connect(DATABASE_URL) as conn:
            token = uuid.uuid4()
            event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
            case = conn.execute("SELECT result_case_id FROM process_lead(%s, %s)", (event[0], token)).fetchone()[0]
        DemoCRM(DATABASE_URL).sync(case, lead)
        with self.assertRaises(ValueError):
            ensure_provider_binding(DATABASE_URL, "espocrm", "account-a", adopt_legacy=True)
        with self.assertRaises(ValueError):
            ensure_provider_binding(DATABASE_URL, "bitrix24", "account-a", adopt_legacy=True, adopt_mixed=True)
        ensure_provider_binding(DATABASE_URL, "espocrm", "account-a", adopt_legacy=True, adopt_mixed=True)
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM demo_crm").fetchone()[0], 1)

    def test_bitrix_jobs_refresh_stage_and_create_reminder(self):
        from app.db.leads import save_lead
        from app.integrations.bitrix import BitrixCRM
        from app.integrations.providers import CRMResult
        from app.integrations.worker import deliver_once, enqueue_reminders, refresh_stage
        from test_lead_ingest import LEAD, validate

        class FakeBitrix(BitrixCRM):
            def __init__(self):
                pass

            def sync(self, case_id, lead):
                return CRMResult("101", "202")

            def get_stage(self, deal_id):
                self.last_deal = deal_id
                return "NEW"

        class FakeTelegram:
            def __init__(self):
                self.reminders = []

            def remind(self, case_id):
                self.reminders.append(case_id)

        save_lead(DATABASE_URL, validate(LEAD, 1000000))
        with self.psycopg.connect(DATABASE_URL) as conn:
            token = uuid.uuid4()
            event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
            case = conn.execute("SELECT result_case_id FROM process_lead(%s, %s)", (event[0], token)).fetchone()[0]
        crm, telegram = FakeBitrix(), FakeTelegram()
        self.assertTrue(deliver_once(DATABASE_URL, crm, None))
        self.assertEqual(refresh_stage(DATABASE_URL, crm, case), "NEW")
        self.assertEqual(crm.last_deal, "202")
        with self.psycopg.connect(DATABASE_URL) as conn:
            conn.execute("UPDATE lead_cases SET first_seen_at=now()-interval '2 hours'")
            conn.execute("UPDATE integration_jobs SET status='done' WHERE destination='telegram'")
        enqueue_reminders(DATABASE_URL, 60, "bitrix24", "NEW")
        self.assertTrue(deliver_once(DATABASE_URL, crm, telegram, "NEW"))
        self.assertEqual(telegram.reminders, [case])

    def test_bitrix_worker_starts_only_after_binding_and_requires_stage_for_reminders(self):
        env = {**os.environ, "DATABASE_URL": DATABASE_URL, "CRM_PROVIDER": "bitrix24",
               "BITRIX_WEBHOOK_URL": "https://example.invalid/rest/1/exampletoken/",
               "BITRIX_CONTACT_FIELD": "UF_CRM_CONTACT_KEY", "BITRIX_DEAL_FIELD": "UF_CRM_DEAL_KEY",
               "BITRIX_INSTALLATION_ID": "example_installation", "TELEGRAM_ENABLED": "false",
               "CRM_ADOPT_EXISTING": "false"}
        env.pop("BITRIX_UNPROCESSED_STAGE", None)
        blocked = subprocess.run([sys.executable, "-m", "app.integrations.worker"],
                                 env={**env, "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "fixture",
                                      "TELEGRAM_CHAT_ID": "123"}, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("BITRIX_UNPROCESSED_STAGE", blocked.stderr)
        self.assertNotIn("exampletoken", blocked.stderr)
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM crm_entities").fetchone()[0], 0)
        process = subprocess.Popen([sys.executable, "-m", "app.integrations.worker"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with self.psycopg.connect(DATABASE_URL) as conn:
                    bound = conn.execute("SELECT remote_id FROM crm_entities WHERE entity_type='__provider_binding__'").fetchone()
                if bound:
                    break
                if process.poll() is not None:
                    self.fail("Bitrix worker stopped before binding")
                time.sleep(0.1)
            else:
                self.fail("Bitrix worker did not bind")
            self.assertTrue(bound[0].startswith("bitrix24:"))
            self.assertNotIn("exampletoken", bound[0])
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_queue_diagnostics_detects_due_failures_without_mutation(self):
        from app.diagnostics import queue_report
        self.assertEqual(queue_report(DATABASE_URL)["status"], "ok")
        with self.psycopg.connect(DATABASE_URL) as conn:
            conn.execute("INSERT INTO lead_events (source_id, payload_hash, payload, available_at) "
                         "VALUES ('diagnostic', %s, '{}', now() - interval '6 minutes')", ("a" * 64,))
            conn.execute("INSERT INTO lead_events (source_id, payload_hash, payload, available_at) "
                         "VALUES ('future-retry', %s, '{}', now() + interval '1 hour')", ("b" * 64,))
        report = queue_report(DATABASE_URL)
        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["events"]["pending"], 2)
        self.assertEqual(report["events"]["overdue"], 1)
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM lead_events WHERE status = 'pending'").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            queue_report(DATABASE_URL, 0)

    def test_queue_diagnostics_detects_uncertain_delivery(self):
        from app.diagnostics import queue_report
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        save_lead(DATABASE_URL, validate(LEAD, 1000000))
        with self.psycopg.connect(DATABASE_URL) as conn:
            token = uuid.uuid4()
            event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
            conn.execute("SELECT * FROM process_lead(%s, %s)", (event[0], token))
            conn.execute("UPDATE integration_jobs SET status = 'uncertain' WHERE destination = 'telegram'")
        report = queue_report(DATABASE_URL)
        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["integrations"]["telegram"]["uncertain"], 1)
        self.assertNotIn("phone", str(report))

    def test_analytics_counts_cohort_repeat_cases_and_current_jobs_without_pii(self):
        from app.analytics import report
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        lead = validate(LEAD, 1000000)
        save_lead(DATABASE_URL, lead)
        save_lead(DATABASE_URL, {**lead, "source_id": "repeat"})
        low = validate({**LEAD, "tranid": "low", "Phone": "+79990000111", "Budget": "500", "Service": "cosmetic"}, 1000000)
        save_lead(DATABASE_URL, low)
        with self.psycopg.connect(DATABASE_URL) as conn:
            for _ in range(2):
                token = uuid.uuid4()
                event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
                conn.execute("SELECT * FROM process_lead(%s, %s)", (event[0], token))
            conn.execute("UPDATE integration_jobs SET status='done' WHERE destination='crm'")
            conn.execute("DELETE FROM integration_jobs WHERE destination='crm' AND event_id=2")
            conn.execute("INSERT INTO lead_events(source_id, payload_hash, payload) VALUES "
                         "('unknown', %s, %s::jsonb)", ("d" * 64, '{"service":"private-string","budget_class":"private-string"}'))
            conn.execute("INSERT INTO lead_events(source_id, payload_hash, payload, created_at) "
                         "VALUES ('old', %s, '{}'::jsonb, now() - interval '40 days')", ("e" * 64,))
        stats = report(DATABASE_URL, 30)
        self.assertEqual(stats["events"], {"accepted": 4, "completed": 2, "pending": 2, "processing": 0,
                                         "failed": 0, "first_case_events": 1, "repeat_case_events": 1,
                                         "without_case": 2})
        self.assertEqual(stats["services"], {"capital": 2, "cosmetic": 1, "unknown": 1})
        self.assertEqual(stats["budget_classes"], {"high": 2, "low": 1, "unknown": 1})
        self.assertEqual(stats["integration_jobs"]["crm"], {"done": 1})
        self.assertEqual(stats["completed_without_crm_job"], 1)
        self.assertNotIn("private-string", str(stats))
        self.assertNotIn("7999", str(stats))
        with self.assertRaises(ValueError):
            report(DATABASE_URL, 0)

    def test_reconciliation_is_atomic_audited_and_single_use(self):
        from app.reconcile import confirm_telegram, list_uncertain
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        save_lead(DATABASE_URL, validate(LEAD, 1000000))
        with self.psycopg.connect(DATABASE_URL) as conn:
            token = uuid.uuid4()
            event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
            conn.execute("SELECT * FROM process_lead(%s, %s)", (event[0], token))
            conn.execute("UPDATE integration_jobs SET status = 'uncertain'")
            jobs = dict(conn.execute("SELECT destination, id FROM integration_jobs").fetchall())
        self.assertEqual(len(list_uncertain(DATABASE_URL)), 2)
        with self.assertRaises(ValueError):
            confirm_telegram(DATABASE_URL, jobs["crm"], "operator")
        with self.assertRaises(ValueError):
            confirm_telegram(DATABASE_URL, jobs["telegram"], "unsafe operator")

        def attempt():
            try:
                confirm_telegram(DATABASE_URL, jobs["telegram"], "operator")
                return "recorded"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["recorded", "rejected"])
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT status FROM integration_jobs WHERE id=%s", (jobs["telegram"],)).fetchone()[0], "done")
            audit = conn.execute("SELECT decision, evidence, operator FROM integration_resolutions WHERE job_id=%s",
                                 (jobs["telegram"],)).fetchall()
            self.assertEqual(audit, [("confirmed_delivered", "telegram_chat", "operator")])
            self.assertEqual(conn.execute("SELECT status FROM integration_jobs WHERE id=%s", (jobs["crm"],)).fetchone()[0], "uncertain")

    def test_reconciliation_refuses_duplicate_case_message(self):
        from app.reconcile import confirm_telegram
        from app.db.leads import save_lead
        from test_lead_ingest import LEAD, validate
        lead = validate(LEAD, 1000000)
        save_lead(DATABASE_URL, lead)
        save_lead(DATABASE_URL, {**lead, "source_id": "second-event"})
        with self.psycopg.connect(DATABASE_URL) as conn:
            for _ in range(2):
                token = uuid.uuid4()
                event = conn.execute("SELECT id FROM claim_lead(%s)", (token,)).fetchone()
                conn.execute("SELECT * FROM process_lead(%s, %s)", (event[0], token))
            conn.execute("UPDATE integration_jobs SET status='uncertain' WHERE destination='telegram'")
            job = conn.execute("SELECT min(id) FROM integration_jobs WHERE destination='telegram'").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "Multiple matching jobs"):
            confirm_telegram(DATABASE_URL, job, "operator")
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM integration_resolutions").fetchone()[0], 0)

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
