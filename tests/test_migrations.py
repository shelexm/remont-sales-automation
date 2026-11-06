"""Migrations use disposable schemas within an explicitly configured test database."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

import psycopg
from psycopg import sql

TEST_DSN = os.getenv("TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL is not configured")
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.schema = "migration_test_" + uuid.uuid4().hex
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.env = {**os.environ, "DATABASE_URL": TEST_DSN, "PGOPTIONS": "-c search_path=" + self.schema}

    def tearDown(self):
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def migrate(self, revision, check=True):
        return subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision],
                              cwd=ROOT, env=self.env, check=check, capture_output=True, text=True)

    def connection(self):
        return psycopg.connect(TEST_DSN, options="-c search_path=" + self.schema)

    def test_upgrade_preserves_existing_event_and_is_repeatable(self):
        self.migrate("20261105_0001")
        with self.connection() as conn:
            conn.execute("INSERT INTO lead_events(source_id, payload_hash, payload) VALUES (%s, %s, %s)",
                         ("migration-test", "a" * 64, "{}"))
        self.migrate("head")
        self.migrate("head")
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM lead_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT version_num FROM alembic_version").fetchone()[0], "20261105_0002")
            self.assertIsNotNone(conn.execute("SELECT to_regclass('integration_jobs')").fetchone()[0])

    def test_legacy_database_fails_closed_without_losing_data(self):
        with self.connection() as conn:
            for name in ("001_schema.sql", "002_integrations.sql"):
                conn.execute((ROOT / "migrations" / "sql" / name).read_text())
            conn.execute("INSERT INTO lead_events(source_id, payload_hash, payload) VALUES (%s, %s, %s)",
                         ("legacy-test", "b" * 64, "{}"))
        result = self.migrate("head", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing unversioned project tables", result.stderr)
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM lead_events").fetchone()[0], 1)
            self.assertIsNotNone(conn.execute("SELECT to_regclass('integration_jobs')").fetchone()[0])
