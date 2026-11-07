"""Initialize an isolated n8n volume. Never import into an existing volume."""
import argparse
import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid environment line in {path.name}")
        key, value = line.split("=", 1)
        values[key] = value
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--demo-env", type=Path, default=HERE / ".local" / "demo.env")
    parser.add_argument("--project-name", default="remont-local-automation")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project_name):
        raise SystemExit("Invalid Compose project name.")
    db = read_env(args.db_env)
    demo = read_env(args.demo_env)
    password = db.get("POSTGRES_PASSWORD", "")
    encryption = demo.get("N8N_ENCRYPTION_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9]{32,}", password) or password.startswith("replace_"):
        raise SystemExit("Set a random alphanumeric POSTGRES_PASSWORD (at least 32 characters).")
    if not re.fullmatch(r"[A-Za-z0-9]{32,}", encryption) or encryption.startswith("replace_"):
        raise SystemExit("Set a random alphanumeric N8N_ENCRYPTION_KEY (at least 32 characters).")
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    volume = args.project_name + "_n8n-data"
    exists = subprocess.run(["docker", "volume", "inspect", volume], capture_output=True).returncode == 0
    if exists:
        raise SystemExit("The n8n volume already exists. Refusing to overwrite workflows or credentials.")

    workflow = json.loads((ROOT / "workflows" / "process-leads.json").read_text())
    workflow["id"] = "remontAutoLocal"
    for node in workflow["nodes"][1:]:
        node["credentials"] = {"postgres": {"id": "remontAutoPG", "name": "remontAutoPG"}}
    credentials = [{"id": "remontAutoPG", "name": "remontAutoPG", "type": "postgres", "data": {
        "host": "remont-trial-db-1", "port": 5432, "database": "remont", "user": "remont",
        "password": password, "ssl": "disable"}}]
    base = ["docker", "compose", "-p", args.project_name, "--env-file", str(args.demo_env.resolve()),
            "-f", str(HERE / "compose.yaml"), "run", "--rm", "--no-deps", "-T",
            "--entrypoint", "n8n", "n8n"]
    for command, data in ((["import:credentials", "--input=/dev/stdin"], credentials),
                          (["import:workflow", "--input=/dev/stdin"], workflow),
                          (["publish:workflow", "--id=remontAutoLocal"], None)):
        result = subprocess.run(base + command, input=json.dumps(data) if data is not None else "",
                                text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise SystemExit("n8n initialization failed. Its new volume was retained for inspection; no secrets were logged.")
    print("n8n workflow published to a new isolated volume. Start the n8n service to enable the schedule.")


if __name__ == "__main__":
    main()
