import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = str(ROOT / "infra/backup")
if BACKUP_DIR not in sys.path:
    sys.path.insert(0, BACKUP_DIR)
spec = importlib.util.spec_from_file_location("recovery_drill", ROOT / "infra/backup/recovery_drill.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RecoveryDrillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recovery-drill-unit-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture"
        self.path.write_bytes(b"synthetic")
        self.path.chmod(0o600)

    def test_workflow_allowlist_rejects_other_or_inactive_workflows(self):
        db = Path(self.temp.name) / "n8n.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE workflow_entity(id TEXT, active INTEGER)")
            conn.execute("CREATE TABLE credentials_entity(id TEXT)")
            conn.execute("INSERT INTO workflow_entity VALUES ('remontAutoLocal', 1)")
        db.chmod(0o600)
        module.validate_n8n_snapshot(db)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO workflow_entity VALUES ('other', 1)")
        with self.assertRaisesRegex(ValueError, "expected active demo workflow"):
            module.validate_n8n_snapshot(db)

    def run_simulated(self, pending="0"):
        calls = []
        values = {"POSTGRES_PASSWORD": "synthetic-db-secret", "ESPO_URL": "https://crm.remont.internal:8443",
                  "ESPO_API_KEY": "synthetic-crm-secret", "ESPO_ASSIGNED_USER_ID": "demo123"}

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "openssl":
                Path(args[args.index("-out") + 1]).write_bytes(b"synthetic-cert")
                Path(args[args.index("-keyout") + 1]).write_bytes(b"synthetic-key")
            if args[:2] == ["docker", "inspect"]:
                return False
            return True

        def fake_wait(check, seconds, message):
            if not check():
                raise RuntimeError(message)

        def fake_secrets(container, names):
            return {name: values[name] for name in names}

        result = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with patch.object(module, "private_file", side_effect=lambda path: Path(path)), \
             patch.object(module, "validate_inputs", side_effect=lambda db, files: (Path(db), Path(files))), \
             patch.object(module, "validate_n8n_snapshot"), patch.object(module, "read_key_file", return_value=b"synthetic-n8n-secret"), \
             patch.object(module, "secret_env", side_effect=fake_secrets), patch.object(module, "run", side_effect=fake_run), \
             patch.object(module, "postgres_query", side_effect=[pending, "completed|done|abc123"]), \
             patch.object(module, "wait_until", side_effect=fake_wait), \
             patch.object(module.subprocess, "run", return_value=result), patch("builtins.print"):
            module.drill(self.path, self.path, self.path, self.path, self.path, "source-db", "source-worker")
        return calls

    def test_drill_has_no_external_network_ports_or_live_mounts(self):
        calls = self.run_simulated()
        starts = [args for args, _ in calls if args[:2] == ["docker", "run"]]
        self.assertEqual(len(starts), 7)
        self.assertIn("none", starts[0])
        self.assertTrue(all(any(str(v).startswith("container:remont-drill-pg-") for v in args) for args in starts[1:]))
        for args in starts:
            self.assertNotIn("--publish", args)
            self.assertNotIn("--mount", args)
            self.assertNotIn("synthetic-db-secret", str(args))
            self.assertNotIn("synthetic-crm-secret", str(args))
            self.assertNotIn("synthetic-n8n-secret", str(args))
        worker = next(kwargs for args, kwargs in calls if args[:2] == ["docker", "run"] and module.WORKER_IMAGE in args)
        self.assertEqual(worker["env"]["TELEGRAM_ENABLED"], "false")
        self.assertEqual(len([args for args, _ in calls if args[:3] == ["docker", "rm", "--force"]]), 7)

    def test_preexisting_pending_work_aborts_before_crm_or_n8n_start(self):
        with self.assertRaisesRegex(RuntimeError, "pending"):
            self.run_simulated(pending="1")


if __name__ == "__main__":
    unittest.main()
