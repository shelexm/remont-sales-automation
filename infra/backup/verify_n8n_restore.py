"""Test a trusted n8n SQLite snapshot and its source key in an isolated container."""
import argparse
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid

from backup_n8n import inspect_snapshot
from backup_postgres import container_name
from n8n_keys import read_container_key, read_key_file, validate_key

IMAGE = "docker.n8n.io/n8nio/n8n:2.46.6"


def verify(snapshot, source_container=None, key_file=None):
    workflows, credentials = inspect_snapshot(snapshot)
    if credentials == 0:
        raise ValueError("Snapshot has no credentials; key decryption cannot be checked.")
    if (source_container is None) == (key_file is None):
        raise ValueError("Specify exactly one key source.")
    key = validate_key(read_container_key(source_container) if source_container else read_key_file(key_file))
    name = "remont-n8n-restore-" + uuid.uuid4().hex
    volume_created = False
    try:
        created = subprocess.run(["docker", "volume", "create", name], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=30)
        if created.returncode:
            raise RuntimeError("Could not create a disposable restore volume.")
        volume_created = True
        mount = f"type=volume,source={name},target=/home/node/.n8n"
        setup = ('umask 077; cat > /home/node/.n8n/database.sqlite && '
                 'chown -R 1000:1000 /home/node/.n8n')
        with Path(snapshot).expanduser().open("rb") as source:
            copied = subprocess.run(["docker", "run", "--rm", "-i", "--name", name + "-setup",
                                     "--network", "none", "--user", "0:0",
                                     "--mount", mount, "--entrypoint", "sh", IMAGE, "-c", setup],
                                    stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if copied.returncode:
            raise RuntimeError("Could not populate the disposable restore volume.")
        # Export exists only on container tmpfs and is never returned to the host or printed.
        check_js = ("const a=JSON.parse(require('fs').readFileSync('/tmp/check.json','utf8'));"
                    "if(!Array.isArray(a)||a.length!==Number(process.argv[1])||"
                    "!a.every(c=>c.data && typeof c.data==='object' && !Array.isArray(c.data)))process.exit(1)")
        check_cmd = ('n8n list:workflow >/dev/null 2>&1 && '
                     'n8n export:credentials --all --decrypted --output=/tmp/check.json >/dev/null 2>&1 && '
                     'node -e "$1" "$2"')
        env = {**os.environ, "N8N_ENCRYPTION_KEY": key}
        check = subprocess.run(["docker", "run", "--rm", "--name", name + "-check", "--network", "none", "--mount", mount,
                                "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m", "-e", "N8N_ENCRYPTION_KEY",
                                "-e", "N8N_DIAGNOSTICS_ENABLED=false", "-e", "N8N_PERSONALIZATION_ENABLED=false",
                                "-e", "N8N_VERSION_NOTIFICATIONS_ENABLED=false", "--entrypoint", "sh", IMAGE,
                                "-c", check_cmd, "sh", check_js, str(credentials)], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if check.returncode:
            raise RuntimeError("Isolated n8n could not open the snapshot and decrypt its credentials.")
        print(f"Isolated n8n restore verified: workflows={workflows}, decrypted_credentials={credentials}.")
    finally:
        if volume_created:
            # Normally --rm already removed these. If docker run timed out, stop only our own named containers.
            for suffix in ("-setup", "-check"):
                subprocess.run(["docker", "rm", "-f", name + suffix], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
            removed = subprocess.run(["docker", "volume", "rm", name], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=30)
            if removed.returncode:
                raise RuntimeError(f"Could not remove disposable volume {name}; restored data may remain.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-container", type=container_name)
    group.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    try:
        verify(args.snapshot, args.source_container, args.key_file)
    except subprocess.TimeoutExpired:
        raise SystemExit("n8n restore check timed out; inspect Docker for disposable resources.") from None
    except sqlite3.Error:
        raise SystemExit("n8n snapshot failed SQLite validation.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
