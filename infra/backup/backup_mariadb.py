"""Create a private SQL dump of one local EspoCRM MariaDB database."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import uuid

from backup_postgres import container_name, identifier, private_directory


def has_footer(path):
    with path.open("rb") as source:
        source.seek(max(0, path.stat().st_size - 4096))
        tail = source.read()
    return b"-- Dump completed on " in tail


def create_backup(container, database, directory):
    directory = private_directory(directory)
    name = f"mariadb-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}.sql"
    target = directory / name
    partial = directory / (name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        # Credentials are already present inside the database container; never put them in host arguments.
        command = ('test "$MARIADB_DATABASE" = "$1" && test -n "$MARIADB_USER" && '
                   'test -n "$MARIADB_PASSWORD" && MYSQL_PWD="$MARIADB_PASSWORD" '
                   'exec mariadb-dump --user="$MARIADB_USER" --single-transaction --quick '
                   '--skip-lock-tables --no-tablespaces -- "$1"')
        with os.fdopen(fd, "wb") as output:
            result = subprocess.run(["docker", "exec", container, "sh", "-c", command, "sh", database],
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=300)
            output.flush()
            os.fsync(output.fileno())
        if result.returncode or not partial.stat().st_size or not has_footer(partial):
            raise RuntimeError("MariaDB dump failed or was incomplete; no backup was published.")
        with partial.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        os.link(partial, target)
        return target, digest
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, type=container_name)
    parser.add_argument("--database", required=True, type=identifier)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        path, digest = create_backup(args.container, args.database, args.output_dir)
    except subprocess.TimeoutExpired:
        raise SystemExit("MariaDB backup timed out; no archive was published.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"MariaDB backup created: {path}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
