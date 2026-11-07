"""Create a private PostgreSQL custom-format backup from a named Docker container."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid
from datetime import datetime, timezone


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise argparse.ArgumentTypeError("Use a simple PostgreSQL identifier.")
    return value


def container_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise argparse.ArgumentTypeError("Use a Docker container name or ID.")
    return value


def private_directory(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Backup directory must not be a symlink.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Backup directory must be owned by the current user and accessible only by that user (0700).")
    return path.resolve()


def create_backup(container, database, user, directory):
    directory = private_directory(directory)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"postgres-{timestamp}-{uuid.uuid4().hex[:12]}.dump"
    target = directory / name
    partial = directory / (name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            result = subprocess.run(["docker", "exec", container, "pg_dump", "--format=custom", "--no-owner",
                                     "--no-acl", "--username", user, "--dbname", database],
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=300)
            output.flush()
            os.fsync(output.fileno())
        if result.returncode != 0 or partial.stat().st_size == 0:
            raise RuntimeError("pg_dump failed; no backup was published.")
        with partial.open("rb") as archive:
            check = subprocess.run(["docker", "exec", "-i", container, "pg_restore", "--list"],
                                   stdin=archive, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if check.returncode != 0:
            raise RuntimeError("The backup archive failed the pg_restore list check.")
        with partial.open("rb") as archive:
            digest = hashlib.file_digest(archive, "sha256").hexdigest()
        os.link(partial, target)
        return target, digest
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, type=container_name, help="PostgreSQL container name or ID")
    parser.add_argument("--database", required=True, type=identifier)
    parser.add_argument("--user", required=True, type=identifier)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        path, digest = create_backup(args.container, args.database, args.user, args.output_dir)
    except subprocess.TimeoutExpired:
        raise SystemExit("Backup operation timed out; no archive was published.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Backup created: {path}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
