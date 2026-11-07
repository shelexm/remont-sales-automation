"""Verify isolated n8n restore and reject a wrong key using synthetic credentials."""
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
image = "docker.n8n.io/n8nio/n8n:2.46.6"
suffix = uuid.uuid4().hex
source = "remont-n8n-fixture-" + suffix
wrong = "remont-n8n-wrong-key-" + suffix
started = []
try:
    for name, key in ((source, "test-key-correct-0123456789012345"),
                      (wrong, "test-key-incorrect-01234567890123")):
        subprocess.run(["docker", "run", "--rm", "-d", "--network", "none", "--name", name,
                        "-e", "N8N_ENCRYPTION_KEY=" + key, "-e", "N8N_DIAGNOSTICS_ENABLED=false",
                        "--entrypoint", "sh", image, "-c", "sleep 600"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        started.append(name)
    fixture = ("require('fs').writeFileSync('/tmp/fixture.json',JSON.stringify([{id:'test001',"
               "name:'example',type:'httpHeaderAuth',data:{name:'X-Demo',value:'test-value-only'}}]))")
    subprocess.run(["docker", "exec", source, "node", "-e", fixture], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    subprocess.run(["docker", "exec", source, "n8n", "import:credentials", "--input=/tmp/fixture.json"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    with tempfile.TemporaryDirectory(prefix="remont-n8n-restore-test-") as directory:
        subprocess.run([sys.executable, str(ROOT / "infra/backup/backup_n8n.py"), "--container", source,
                        "--output-dir", directory], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=180)
        archives = list(Path(directory).glob("*.sqlite"))
        if len(archives) != 1:
            raise AssertionError("Synthetic snapshot was not created")
        command = [sys.executable, str(ROOT / "infra/backup/verify_n8n_restore.py"), str(archives[0]),
                   "--source-container"]
        correct = subprocess.run([*command, source], capture_output=True, text=True, timeout=300)
        if correct.returncode or "decrypted_credentials=1" not in correct.stdout:
            raise AssertionError("Restore with correct key failed")
        if "test-value-only" in correct.stdout or "test-key-correct" in correct.stdout:
            raise AssertionError("Restore exposed test credentials or key")
        incorrect = subprocess.run([*command, wrong], capture_output=True, text=True, timeout=300)
        if incorrect.returncode == 0:
            raise AssertionError("Restore with incorrect key was accepted")
        if "test-value-only" in incorrect.stderr or "test-key-incorrect" in incorrect.stderr:
            raise AssertionError("Restore failure exposed test credentials or key")
    print("Isolated n8n restore accepted the correct key and rejected an incorrect key")
finally:
    for container in started:
        removed = subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=30)
        if removed.returncode:
            raise RuntimeError(f"Could not remove disposable test container {container}")
