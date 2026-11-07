"""Exercise backup and restore against disposable isolated PostgreSQL containers."""
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
name = "remont-backup-test-" + uuid.uuid4().hex
started = False
try:
    subprocess.run(["docker", "run", "--rm", "-d", "--network", "none", "--name", name,
                    "-e", "POSTGRES_USER=test", "-e", "POSTGRES_DB=test", "-e",
                    "POSTGRES_PASSWORD=" + secrets.token_hex(24), "postgres:17-alpine"],
                   check=True, stdout=subprocess.DEVNULL)
    started = True
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "test", "-d", "test"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Test database did not become ready")
    schema = "\n".join((ROOT / "migrations" / "sql" / p).read_text() for p in ("001_schema.sql", "002_integrations.sql"))
    fixture = """
        CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20261105_0002');
        INSERT INTO lead_events(source_id,payload_hash,payload)
        VALUES ('backup-synthetic-example',repeat('a',64),'{"phone":"+79990000000","service":"capital"}');
        INSERT INTO lead_cases(phone,service,first_event_id,last_event_id) VALUES ('+79990000000','capital',1,1);
        UPDATE lead_events SET case_id=1,status='completed' WHERE id=1;
    """
    subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "test", "-d", "test", "-v", "ON_ERROR_STOP=1"],
                   input=schema + "\n" + fixture, text=True, check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="remont-backup-smoke-") as directory:
        made = subprocess.run([sys.executable, str(ROOT / "infra/backup/backup_postgres.py"),
                               "--container", name, "--database", "test", "--user", "test", "--output-dir", directory],
                              capture_output=True, text=True, check=True)
        archives = list(Path(directory).glob("*.dump"))
        if len(archives) != 1 or "backup-synthetic-example" in made.stdout:
            raise AssertionError("Backup was not safely created")
        result = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_postgres.py"), str(archives[0])],
                                capture_output=True, text=True, check=True)
        if "events=1, cases=1, integration_jobs=2" not in result.stdout:
            raise AssertionError("Restored data did not match the fixture")
        if "backup-synthetic-example" in result.stdout:
            raise AssertionError("Restore output exposed a source identifier")
        broken = Path(directory) / "truncated.dump"
        broken.write_bytes(archives[0].read_bytes()[:256])
        broken.chmod(0o600)
        invalid = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_postgres.py"), str(broken)],
                                 capture_output=True, text=True)
        if invalid.returncode == 0:
            raise AssertionError("Truncated archive passed restore validation")
    print("Disposable PostgreSQL backup and restore passed")
finally:
    if started:
        removed = subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if removed.returncode:
            raise RuntimeError(f"Could not remove disposable test container {name}")
