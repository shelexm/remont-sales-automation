"""Save a separate private copy of the configured n8n encryption key."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import uuid

from backup_postgres import container_name, private_directory
from n8n_keys import read_container_key


def create_backup(container, directory):
    directory = private_directory(directory)
    key = read_container_key(container)
    name = f"n8n-key-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}.key"
    target = directory / name
    partial = directory / (name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(key)
            output.flush()
            os.fsync(output.fileno())
        os.link(partial, target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", type=container_name, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        path = create_backup(args.container, args.output_dir)
    except subprocess.TimeoutExpired:
        raise SystemExit("Reading the n8n key timed out; no backup was published.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Private n8n key backup created: {path}")


if __name__ == "__main__":
    main()
