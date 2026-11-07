"""Restore a trusted EspoCRM SQL dump into a disposable network-isolated MariaDB."""
import argparse
import os
from pathlib import Path
import secrets
import stat
import subprocess
import time
import uuid

from backup_mariadb import has_footer

IMAGE = "mariadb:11.4"


def verify(archive):
    archive = Path(archive).expanduser()
    if archive.is_symlink():
        raise ValueError("Archive must not be a symlink.")
    info = archive.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Archive must be a private regular file owned by the current user (0600).")
    if not has_footer(archive):
        raise ValueError("SQL dump is incomplete or lacks its completion marker.")
    name = "remont-mariadb-restore-" + uuid.uuid4().hex
    started = False
    try:
        result = subprocess.run(["docker", "run", "--rm", "-d", "--network", "none", "--name", name,
                                 "-e", "MARIADB_DATABASE=restorecheck", "-e",
                                 "MARIADB_ROOT_PASSWORD=" + secrets.token_hex(24), IMAGE],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        if result.returncode:
            raise RuntimeError("Could not start the disposable MariaDB container.")
        started = True
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            # TCP avoids racing the image's initial socket-only setup server.
            probe = ('MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-admin '
                     '--protocol=tcp --host=127.0.0.1 --user=root ping')
            ready = subprocess.run(["docker", "exec", name, "sh", "-c", probe], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Disposable MariaDB did not become ready.")
        restore = 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb --user=root --database=restorecheck'
        with archive.open("rb") as source:
            result = subprocess.run(["docker", "exec", "-i", name, "sh", "-c", restore], stdin=source,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        if result.returncode:
            raise RuntimeError("Restore failed in the disposable container.")
        query = "SELECT (SELECT COUNT(*) FROM contact), (SELECT COUNT(*) FROM opportunity)"
        check_cmd = ('MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb --user=root --database=restorecheck '
                     '--batch --skip-column-names --execute="$1"')
        checked = subprocess.run(["docker", "exec", name, "sh", "-c", check_cmd, "sh", query],
                                 capture_output=True, text=True, timeout=30)
        if checked.returncode:
            raise RuntimeError("Restored database is missing required CRM tables.")
        values = checked.stdout.strip().split("\t")
        if len(values) != 2 or any(not x.isdecimal() for x in values):
            raise RuntimeError("Unexpected result while checking the restored database.")
        print(f"MariaDB restore verified: contacts={int(values[0])}, opportunities={int(values[1])}.")
    finally:
        if started:
            removed = subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=30)
            if removed.returncode:
                raise RuntimeError(f"Could not remove disposable container {name}; restored data may remain.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        verify(args.archive)
    except subprocess.TimeoutExpired:
        raise SystemExit("MariaDB restore timed out; inspect Docker for the disposable container.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
