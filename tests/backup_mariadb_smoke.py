"""Exercise MariaDB backup and restore with disposable isolated containers."""
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
name = "remont-mariadb-test-" + uuid.uuid4().hex
started = False
try:
    subprocess.run(["docker", "run", "--rm", "-d", "--network", "none", "--name", name,
                    "-e", "MARIADB_USER=test", "-e", "MARIADB_DATABASE=espocrm", "-e",
                    "MARIADB_PASSWORD=" + secrets.token_hex(24), "-e",
                    "MARIADB_ROOT_PASSWORD=" + secrets.token_hex(24), "mariadb:11.4"],
                   check=True, stdout=subprocess.DEVNULL)
    started = True
    probe = ('MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-admin '
             '--protocol=tcp --host=127.0.0.1 --user=root ping')
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "exec", name, "sh", "-c", probe],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Test MariaDB did not become ready")
    fixture = ("CREATE TABLE contact (id varchar(40) PRIMARY KEY, name varchar(100)); "
               "CREATE TABLE opportunity (id varchar(40) PRIMARY KEY); "
               "INSERT INTO contact VALUES ('one', 'synthetic-private-marker'); "
               "INSERT INTO opportunity VALUES ('one');")
    insert = 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb --user="$MARIADB_USER" "$MARIADB_DATABASE"'
    subprocess.run(["docker", "exec", "-i", name, "sh", "-c", insert], input=fixture, text=True,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="remont-mariadb-backup-") as directory:
        backup = subprocess.run([sys.executable, str(ROOT / "infra/backup/backup_mariadb.py"),
                                 "--container", name, "--database", "espocrm", "--output-dir", directory],
                                capture_output=True, text=True, check=True)
        archives = list(Path(directory).glob("*.sql"))
        if len(archives) != 1 or "synthetic-private-marker" in backup.stdout:
            raise AssertionError("Backup was not safely created")
        if archives[0].stat().st_mode & 0o777 != 0o600:
            raise AssertionError("Backup is not private")
        wrong_database = subprocess.run([sys.executable, str(ROOT / "infra/backup/backup_mariadb.py"),
                                         "--container", name, "--database", "wrong", "--output-dir", directory],
                                        capture_output=True, text=True)
        if wrong_database.returncode == 0 or list(Path(directory).glob("*.partial")):
            raise AssertionError("Wrong database was accepted or partial dump remained")
        restored = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_mariadb.py"), str(archives[0])],
                                  capture_output=True, text=True, check=True)
        if "contacts=1, opportunities=1" not in restored.stdout or "synthetic-private-marker" in restored.stdout:
            raise AssertionError("Restored data did not match the fixture or output exposed private data")
        broken = Path(directory) / "truncated.sql"
        broken.write_bytes(archives[0].read_bytes()[:256])
        broken.chmod(0o600)
        invalid = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_mariadb.py"), str(broken)],
                                 capture_output=True, text=True)
        if invalid.returncode == 0:
            raise AssertionError("Truncated archive passed validation")
        broken.chmod(0o644)
        unsafe = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_mariadb.py"), str(broken)],
                                capture_output=True, text=True)
        if unsafe.returncode == 0 or "private regular file" not in unsafe.stderr:
            raise AssertionError("Nonprivate archive was accepted")
    print("Disposable MariaDB backup and restore passed")
finally:
    if started:
        removed = subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if removed.returncode:
            raise RuntimeError(f"Could not remove disposable test container {name}")
