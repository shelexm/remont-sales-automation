import os
import unittest
import uuid
from pathlib import Path

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "TEST_DATABASE_URL is not configured")
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        schema = (Path(__file__).resolve().parents[1] / "infra/postgres/001_schema.sql").read_text()
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(schema)

    def setUp(self):
        with self.psycopg.connect(DATABASE_URL) as conn:
            conn.execute("TRUNCATE lead_events RESTART IDENTITY")

    def test_duplicate_and_changed_payload(self):
        from test_lead_ingest import LEAD, validate
        from app import save_lead
        lead = validate(LEAD, 1000000)
        self.assertEqual(save_lead(DATABASE_URL, lead), "created")
        self.assertEqual(save_lead(DATABASE_URL, lead), "duplicate")
        self.assertEqual(save_lead(DATABASE_URL, {**lead, "name": "Changed"}), "conflict")
        with self.psycopg.connect(DATABASE_URL) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM lead_events").fetchone()[0], 1)

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

    def test_storage_failure_does_not_report_success(self):
        from app import save_lead
        from test_lead_ingest import LEAD, validate
        with self.assertRaises(Exception):
            save_lead("postgresql://bad:bad@127.0.0.1:1/missing", validate(LEAD, 1))
