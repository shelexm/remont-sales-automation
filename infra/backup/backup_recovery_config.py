"""Save private local demo connection settings needed for an isolated recovery drill."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import uuid

from backup_postgres import container_name, private_directory
from recovery_config import secret_env, validate_config


def create_backup(source_db, source_integrations, directory):
    directory = private_directory(directory)
    data = {"version": 1, **secret_env(source_db, ["POSTGRES_PASSWORD"]),
            **secret_env(source_integrations, ["ESPO_URL", "ESPO_API_KEY", "ESPO_ASSIGNED_USER_ID"])}
    validate_config(data)
    name = f"recovery-config-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}.json"
    target, partial = directory / name, directory / (name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.link(partial, target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=container_name)
    parser.add_argument("--source-integrations", required=True, type=container_name)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        path = create_backup(args.source_db, args.source_integrations, args.output_dir)
    except subprocess.TimeoutExpired:
        raise SystemExit("Reading source configuration timed out; no backup was published.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Private recovery configuration created: {path}")


if __name__ == "__main__":
    main()
