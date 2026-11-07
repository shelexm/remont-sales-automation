"""Check that backup failures never publish incomplete archives."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup_postgres", ROOT / "infra/backup/backup_postgres.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
VERIFY_SPEC = importlib.util.spec_from_file_location("verify_postgres", ROOT / "infra/backup/verify_postgres.py")
verify = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify)


class BackupSafetyTests(unittest.TestCase):
    def test_failed_dump_removes_only_its_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "existing.dump"
            sentinel.write_bytes(b"existing")
            with patch.object(backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(RuntimeError):
                    backup.create_backup("example", "remont", "remont", directory)
            self.assertEqual(sentinel.read_bytes(), b"existing")
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["existing.dump"])

    def test_insecure_directory_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            insecure = root / "insecure"
            insecure.mkdir(mode=0o755)
            insecure.chmod(0o755)
            with self.assertRaises(ValueError):
                backup.private_directory(insecure)
            link = root / "link"
            link.symlink_to(insecure, target_is_directory=True)
            with self.assertRaises(ValueError):
                backup.private_directory(link)

    def test_restore_rejects_nonprivate_or_symlink_archive_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "example.dump"
            archive.write_bytes(b"not a database")
            archive.chmod(0o644)
            link = Path(directory) / "link.dump"
            link.symlink_to(archive)
            with patch.object(verify.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    verify.verify(archive)
                with self.assertRaises(ValueError):
                    verify.verify(link)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
