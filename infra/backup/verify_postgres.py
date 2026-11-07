"""Restore a trusted backup into a fresh, network-isolated disposable PostgreSQL container."""
import argparse
import os
from pathlib import Path
import secrets
import stat
import subprocess
import time
import uuid

IMAGE = "postgres:17-alpine"


def verify(archive):
    archive = Path(archive).expanduser()
    if archive.is_symlink():
        raise ValueError("Archive must not be a symlink.")
    info = archive.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Archive must be a private regular file owned by the current user (0600).")
    name = "remont-restore-check-" + uuid.uuid4().hex
    password = secrets.token_hex(24)
    started = False
    try:
        result = subprocess.run(["docker", "run", "--rm", "-d", "--network", "none", "--name", name,
                                 "-e", "POSTGRES_USER=verify", "-e", "POSTGRES_DB=restorecheck",
                                 "-e", "POSTGRES_PASSWORD=" + password, IMAGE],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise RuntimeError("Could not start the disposable PostgreSQL container.")
        started = True
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "verify", "-d", "restorecheck"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready.")
        with archive.open("rb") as source:
            restored = subprocess.run(["docker", "exec", "-i", name, "pg_restore", "--exit-on-error",
                                       "--no-owner", "--no-privileges", "-U", "verify", "-d", "restorecheck"],
                                      stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        if restored.returncode:
            raise RuntimeError("Restore failed in the disposable container.")
        sql = ("SELECT (SELECT count(*) FROM alembic_version), "
               "(SELECT count(*) FROM lead_events), (SELECT count(*) FROM lead_cases), "
               "(SELECT count(*) FROM integration_jobs), "
               "to_regprocedure('claim_lead(uuid,integer,integer)') IS NOT NULL, "
               "to_regprocedure('process_lead(bigint,uuid)') IS NOT NULL, "
               "EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='enqueue_integrations_trigger' AND NOT tgisinternal);")
        check = subprocess.run(["docker", "exec", name, "psql", "-U", "verify", "-d", "restorecheck", "-At",
                                "-v", "ON_ERROR_STOP=1", "-c", sql], capture_output=True, text=True, timeout=30)
        if check.returncode:
            raise RuntimeError("Restored database is missing required project tables or version metadata.")
        values = check.stdout.strip().split("|")
        if len(values) != 7 or values[0] != "1" or values[4:] != ["t", "t", "t"]:
            raise RuntimeError("Restored database failed required version, function or trigger checks.")
        print(f"Restore verified: events={int(values[1])}, cases={int(values[2])}, integration_jobs={int(values[3])}.")
        print("Alembic version, queue functions and integration trigger are present.")
    finally:
        if started:
            removed = subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=30)
            if removed.returncode:
                raise RuntimeError(f"Could not remove the disposable container {name}; its restored data may remain.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Trusted custom-format project backup")
    args = parser.parse_args()
    try:
        verify(args.archive)
    except subprocess.TimeoutExpired:
        raise SystemExit("Restore verification timed out; inspect Docker for the disposable test container.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
