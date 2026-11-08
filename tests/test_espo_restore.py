import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = str(ROOT / "infra/backup")
if BACKUP_DIR not in sys.path:
    sys.path.insert(0, BACKUP_DIR)
spec = importlib.util.spec_from_file_location("verify_espo_restore", ROOT / "infra/backup/verify_espo_restore.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class EspoRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="espo-restore-unit-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "db.sql"
        self.database.write_bytes(b"-- test data\n-- Dump completed on test\n")
        self.database.chmod(0o600)
        self.files = Path(self.temp.name) / "files.tar"
        with tarfile.open(self.files, "w") as archive:
            for name in ("data", "custom", "client/custom"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                archive.addfile(info)
            info = tarfile.TarInfo("data/config-internal.php")
            payload = b"synthetic-private-config"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        self.files.chmod(0o600)

    def test_unsafe_or_incomplete_inputs_never_start_docker(self):
        self.database.chmod(0o644)
        with patch.object(module, "run") as run, self.assertRaises(ValueError):
            module.verify(self.database, self.files)
        run.assert_not_called()
        self.database.chmod(0o600)
        self.database.write_bytes(b"truncated")
        with patch.object(module, "run") as run, self.assertRaises(ValueError):
            module.verify(self.database, self.files)
        run.assert_not_called()

    def test_links_in_archive_are_rejected_before_docker(self):
        with tarfile.open(self.files, "a") as archive:
            info = tarfile.TarInfo("data/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with patch.object(module, "run") as run, self.assertRaisesRegex(ValueError, "links"):
            module.verify(self.database, self.files)
        run.assert_not_called()

    def test_only_disposable_network_and_memory_resources_are_used(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return not ("inspect" in command and "container" in command)

        result = subprocess.CompletedProcess([], 0, stdout="12\t10\n", stderr="")
        with patch.object(module, "run", side_effect=fake_run), \
             patch.object(module.subprocess, "run", return_value=result), \
             patch("builtins.print"):
            module.verify(self.database, self.files)
        starts = [x for x in commands if x[:2] == ["docker", "run"]]
        self.assertEqual(len(starts), 2)
        self.assertIn("none", starts[0])
        self.assertTrue(any(str(x).startswith("container:remont-espo-check-db-") for x in starts[1]))
        self.assertNotIn("--publish", starts[0] + starts[1])
        self.assertNotIn("--mount", starts[0] + starts[1])
        self.assertGreaterEqual(starts[0].count("--tmpfs") + starts[1].count("--tmpfs"), 4)
        self.assertNotIn("synthetic-private-config", str(commands))
        self.assertEqual(len([x for x in commands if x[:3] == ["docker", "rm", "--force"]]), 2)

    def test_cleanup_runs_after_application_failure(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return not ("app-check" in command or ("inspect" in command and "container" in command))

        with patch.object(module, "run", side_effect=fake_run), self.assertRaisesRegex(RuntimeError, "application"):
            module.verify(self.database, self.files)
        self.assertEqual(len([x for x in commands if x[:3] == ["docker", "rm", "--force"]]), 2)


if __name__ == "__main__":
    unittest.main()
