"""Exercise online SQLite snapshot and validation with a disposable n8n image."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
name = "remont-n8n-backup-test-" + uuid.uuid4().hex
started = False
try:
    subprocess.run(["docker", "run", "--rm", "-d", "--network", "none", "--name", name,
                    "--entrypoint", "sh", "docker.n8n.io/n8nio/n8n:2.46.6", "-c", "sleep 300"],
                   check=True, stdout=subprocess.DEVNULL)
    started = True
    fixture = """
      const fs = require('node:fs');
      fs.mkdirSync('/home/node/.n8n', {recursive:true});
      const db = new (require('node:sqlite').DatabaseSync)('/home/node/.n8n/database.sqlite');
      db.exec(`PRAGMA journal_mode=WAL;
        CREATE TABLE workflow_entity (id TEXT, name TEXT);
        CREATE TABLE credentials_entity (id TEXT, data TEXT);
        INSERT INTO workflow_entity VALUES ('one','synthetic-private-name');
        INSERT INTO credentials_entity VALUES ('one','synthetic-private-secret');`);
      setInterval(() => {}, 1000);
    """
    subprocess.run(["docker", "exec", "-d", name, "node", "--no-warnings", "-e", fixture],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ready = subprocess.run(["docker", "exec", name, "test", "-s", "/home/node/.n8n/database.sqlite-wal"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ready.returncode == 0:
            probe = ("const {DatabaseSync}=require('node:sqlite'); "
                     "const d=new DatabaseSync('/home/node/.n8n/database.sqlite',{readOnly:true}); "
                     "if(d.prepare('select count(*) n from credentials_entity').get().n!==1)process.exit(1); d.close()")
            populated = subprocess.run(["docker", "exec", name, "node", "--no-warnings", "-e", probe],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if populated.returncode == 0:
                break
        time.sleep(0.25)
    else:
        raise RuntimeError("SQLite fixture did not create a live WAL")
    with tempfile.TemporaryDirectory(prefix="remont-n8n-backup-") as directory:
        result = subprocess.run([sys.executable, str(ROOT / "infra/backup/backup_n8n.py"),
                                 "--container", name, "--output-dir", directory],
                                check=True, capture_output=True, text=True)
        archives = list(Path(directory).glob("*.sqlite"))
        if len(archives) != 1 or "synthetic-private" in result.stdout:
            raise AssertionError("Snapshot failed or exposed private data")
        if archives[0].stat().st_mode & 0o777 != 0o600:
            raise AssertionError("Snapshot is not private")
        checked = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_n8n.py"), str(archives[0])],
                                 capture_output=True, text=True, check=True)
        if "workflows=1, credentials=1" not in checked.stdout or "synthetic-private" in checked.stdout:
            raise AssertionError("Snapshot did not contain expected data or exposed private data")
        broken = Path(directory) / "broken.sqlite"
        broken.write_bytes(archives[0].read_bytes()[:128])
        broken.chmod(0o600)
        invalid = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_n8n.py"), str(broken)],
                                 capture_output=True, text=True)
        if invalid.returncode == 0:
            raise AssertionError("Truncated snapshot was accepted")
        archives[0].chmod(0o644)
        unsafe = subprocess.run([sys.executable, str(ROOT / "infra/backup/verify_n8n.py"), str(archives[0])],
                                capture_output=True, text=True)
        if unsafe.returncode == 0 or "private regular file" not in unsafe.stderr:
            raise AssertionError("Nonprivate snapshot was accepted")
    print("Disposable n8n SQLite snapshot and validation passed")
finally:
    if started:
        removed = subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if removed.returncode:
            raise RuntimeError(f"Could not remove disposable test container {name}")
