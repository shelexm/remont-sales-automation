"""Exercise the receiver healthcheck using only disposable Compose resources."""
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
project = "remont-ready-test-" + uuid.uuid4().hex[:12]
with tempfile.TemporaryDirectory(prefix="remont-ready-") as directory:
    temp = Path(directory)
    env = temp / "test.env"
    env.write_text("POSTGRES_PASSWORD=" + secrets.token_hex(24) + "\nWEBHOOK_KEY=" + secrets.token_hex(32) + "\n")
    env.chmod(0o600)
    override = temp / "override.yaml"
    override.write_text("""services:
  receiver:
    ports: !reset []
    healthcheck:
      interval: 2s
      timeout: 6s
      retries: 1
      start_period: 2s
""")
    compose = ["docker", "compose", "-p", project, "--env-file", str(env), "-f", str(ROOT / "compose.yaml"),
               "-f", str(override)]
    created = False
    try:
        # A unique project name makes all resources disposable and separate from a running demo.
        created = True
        subprocess.run(compose + ["up", "-d", "--build", "receiver"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        receiver = subprocess.check_output(compose + ["ps", "-q", "receiver"], cwd=ROOT, text=True).strip()
        database = subprocess.check_output(compose + ["ps", "-q", "db"], cwd=ROOT, text=True).strip()
        if not receiver or not database:
            raise RuntimeError("Disposable receiver or database is missing")

        def wait_health(expected, limit=45):
            deadline = time.monotonic() + limit
            while time.monotonic() < deadline:
                current = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Health.Status}}", receiver], text=True).strip()
                if current == expected:
                    return
                time.sleep(1)
            raise RuntimeError(f"Receiver did not become {expected}")

        wait_health("healthy")
        # A peer container must not see the internal database-readiness endpoint.
        remote_probe = ("import urllib.request,urllib.error\n"
                        "try: urllib.request.urlopen('http://receiver:8080/ready',timeout=5)\n"
                        "except urllib.error.HTTPError as e: assert e.code==404\n"
                        "else: raise AssertionError('readiness leaked to network')\n")
        subprocess.run(["docker", "run", "--rm", "--network", project + "_default", "python:3.12-slim",
                        "python", "-c", remote_probe], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "stop", database], check=True, stdout=subprocess.DEVNULL)
        wait_health("unhealthy")
        probe = ("import urllib.request,urllib.error\n"
                 "assert urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=5).status==200\n"
                 "try: urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5)\n"
                 "except urllib.error.HTTPError as e: assert e.code==503\n"
                 "else: raise AssertionError('readiness accepted unavailable DB')\n")
        subprocess.run(["docker", "exec", receiver, "python", "-c", probe], check=True)
        subprocess.run(["docker", "start", database], check=True, stdout=subprocess.DEVNULL)
        wait_health("healthy")
        print("Receiver health tracks isolated PostgreSQL failure and recovery; remote readiness is hidden")
    finally:
        if created:
            result = subprocess.run(compose + ["down", "--volumes", "--remove-orphans", "--rmi", "local"], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise RuntimeError(f"Could not remove disposable Compose project {project}")
