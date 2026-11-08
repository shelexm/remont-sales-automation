import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = str(ROOT / "infra/backup")
if BACKUP_DIR not in sys.path:
    sys.path.insert(0, BACKUP_DIR)
spec = importlib.util.spec_from_file_location("backup_recovery_config", ROOT / "infra/backup/backup_recovery_config.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
from recovery_config import read_config  # noqa: E402


class RecoveryConfigTests(unittest.TestCase):
    def test_backup_private_readable_and_never_prints_values(self):
        values = {"POSTGRES_PASSWORD": "synthetic-db-secret", "ESPO_URL": "https://crm.remont.internal:8443",
                  "ESPO_API_KEY": "synthetic-crm-secret", "ESPO_ASSIGNED_USER_ID": "demo123"}
        with tempfile.TemporaryDirectory(prefix="recovery-config-test-") as directory:
            dest = Path(directory) / "private"
            with patch.object(backup, "secret_env", side_effect=lambda source, names: {k: values[k] for k in names}), \
                 patch("builtins.print") as output:
                result = backup.create_backup("db", "worker", dest)
            output.assert_not_called()
            self.assertEqual(dest.stat().st_mode & 0o777, 0o700)
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_config(result), {"version": 1, **values})
            link = dest / "link"
            link.symlink_to(result)
            with self.assertRaises(ValueError):
                read_config(link)
            result.chmod(0o644)
            with self.assertRaises(ValueError):
                read_config(result)

    def test_unknown_endpoint_and_additional_secret_are_rejected(self):
        data = {"version": 1, "POSTGRES_PASSWORD": "synthetic-db-secret", "ESPO_URL": "https://external.invalid",
                "ESPO_API_KEY": "synthetic-crm-secret", "ESPO_ASSIGNED_USER_ID": "demo123"}
        with tempfile.TemporaryDirectory(prefix="recovery-config-test-") as directory:
            path = Path(directory) / "config"
            path.write_text(json.dumps(data))
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                read_config(path)
            data["ESPO_URL"] = "https://crm.remont.internal:8443"
            data["TELEGRAM_BOT_TOKEN"] = "synthetic-extra-secret"
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                read_config(path)


if __name__ == "__main__":
    unittest.main()
