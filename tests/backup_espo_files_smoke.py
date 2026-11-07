"""Exercise the EspoCRM file archive using an isolated synthetic container."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
name = "remont-espo-files-test-" + uuid.uuid4().hex
started = False
try:
    fixture = ("mkdir -p /var/www/html/data /var/www/html/custom /var/www/html/client/custom; "
               "printf synthetic-private-value > /var/www/html/data/config.php; "
               "printf demo > /var/www/html/custom/example; "
               "printf demo > /var/www/html/client/custom/example; sleep 300")
    subprocess.run(["docker", "run", "--rm", "-d", "--network", "none", "--name", name,
                    "--entrypoint", "sh", "python:3.12-slim", "-c", fixture], check=True,
                   stdout=subprocess.DEVNULL)
    started = True
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ready = subprocess.run(["docker", "exec", name, "test", "-f", "/var/www/html/client/custom/example"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ready.returncode == 0:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("CRM file fixture did not start")
    with tempfile.TemporaryDirectory(prefix="remont-espo-files-backup-") as directory:
        result = subprocess.run([sys.executable, str(ROOT / "infra/backup/backup_espo_files.py"),
                                 "--container", name, "--output-dir", directory],
                                capture_output=True, text=True, check=True)
        archives = list(Path(directory).glob("*.tar"))
        if len(archives) != 1 or "synthetic-private-value" in result.stdout:
            raise AssertionError("Archive failed or exposed private data")
        if archives[0].stat().st_mode & 0o777 != 0o600:
            raise AssertionError("Archive is not private")
        check = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_espo_files.py"), str(archives[0])],
                               capture_output=True, text=True, check=True)
        if "entries=" not in check.stdout or "synthetic-private-value" in check.stdout:
            raise AssertionError("Archive validation failed or exposed private data")
        broken = Path(directory) / "broken.tar"
        broken.write_bytes(archives[0].read_bytes()[:1024])
        broken.chmod(0o600)
        invalid = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_espo_files.py"), str(broken)],
                                 capture_output=True, text=True)
        if invalid.returncode == 0:
            raise AssertionError("Truncated archive was accepted")
        archives[0].chmod(0o644)
        unsafe = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_espo_files.py"), str(archives[0])],
                                capture_output=True, text=True)
        if unsafe.returncode == 0:
            raise AssertionError("Nonprivate archive was accepted")
    print("Disposable EspoCRM file archive and validation passed")
finally:
    if started:
        removed = subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if removed.returncode:
            raise RuntimeError(f"Could not remove disposable test container {name}")
