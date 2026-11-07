"""Archive local EspoCRM data and customization directories without stopping it."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import uuid

from backup_postgres import container_name, private_directory


def inspect_archive(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Archive must not be a symlink.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Archive must be a private regular file owned by the current user (0600).")
    if info.st_size < 1024:
        raise ValueError("Archive is too short.")
    with path.open("rb") as source:
        source.seek(-1024, os.SEEK_END)
        if source.read() != b"\0" * 1024:
            raise ValueError("Archive is incomplete.")
    entries = 0
    roots = set()
    with tarfile.open(path, mode="r:") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if not parts or member.name.startswith("/") or ".." in parts:
                raise ValueError("Archive contains an unsafe path.")
            if parts[0] == "data":
                roots.add("data")
            elif parts[0] == "custom":
                roots.add("custom")
            elif parts[:2] == ("client", "custom"):
                roots.add("client/custom")
            else:
                raise ValueError("Archive contains an unexpected root.")
            if member.isdev() or member.isfifo():
                raise ValueError("Archive contains a special file.")
            if member.isfile():
                with archive.extractfile(member) as source:
                    while source.read(1024 * 1024):
                        pass
            entries += 1
    if roots != {"data", "custom", "client/custom"}:
        raise ValueError("Archive is missing one of the CRM directories.")
    return entries


def create_backup(container, directory):
    directory = private_directory(directory)
    name = f"espo-files-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}.tar"
    target = directory / name
    partial = directory / (name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            result = subprocess.run(["docker", "exec", container, "tar", "-C", "/var/www/html", "-cf", "-",
                                     "data", "custom", "client/custom"], stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=300)
            output.flush()
            os.fsync(output.fileno())
        if result.returncode:
            raise RuntimeError("CRM file archive failed; no backup was published.")
        inspect_archive(partial)
        with partial.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        os.link(partial, target)
        return target, digest
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, type=container_name)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        path, digest = create_backup(args.container, args.output_dir)
    except subprocess.TimeoutExpired:
        raise SystemExit("CRM file backup timed out; no archive was published.") from None
    except (OSError, ValueError, RuntimeError, tarfile.TarError):
        raise SystemExit("CRM file backup failed validation; no archive was published.") from None
    print(f"CRM file backup created: {path}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
