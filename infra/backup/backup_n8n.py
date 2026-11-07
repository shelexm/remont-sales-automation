"""Back up the local n8n SQLite database consistently while n8n is running."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import uuid

from backup_postgres import container_name, private_directory


NODE_BACKUP = r"""
const fs = require('node:fs');
const path = require('node:path');
const { pipeline } = require('node:stream/promises');
const { DatabaseSync, backup } = require('node:sqlite');
process.umask(0o077);
(async () => {
  const directory = fs.mkdtempSync('/tmp/remont-n8n-backup-');
  let db;
  try {
    db = new DatabaseSync('/home/node/.n8n/database.sqlite', { readOnly: true });
    const snapshot = path.join(directory, 'snapshot.sqlite');
    await backup(db, snapshot);
    db.close(); db = undefined;
    await pipeline(fs.createReadStream(snapshot), process.stdout);
  } finally {
    if (db) db.close();
    fs.rmSync(directory, { recursive: true, force: true });
  }
})().catch(() => { process.exitCode = 1; });
"""


def inspect_snapshot(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Snapshot must not be a symlink.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Snapshot must be a private regular file owned by the current user (0600).")
    # immutable avoids creating or consulting journals beside the snapshot being inspected.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed.")
        counts = [connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                  for table in ("workflow_entity", "credentials_entity")]
        return counts
    finally:
        connection.close()


def create_backup(container, directory):
    directory = private_directory(directory)
    name = f"n8n-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}.sqlite"
    target = directory / name
    partial = directory / (name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            result = subprocess.run(["docker", "exec", container, "node", "--no-warnings", "-e", NODE_BACKUP],
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=300)
            output.flush()
            os.fsync(output.fileno())
        if result.returncode or not partial.stat().st_size:
            raise RuntimeError("n8n database snapshot failed; no backup was published.")
        inspect_snapshot(partial)
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
        raise SystemExit("n8n backup timed out; inspect the container for a temporary snapshot.") from None
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        # Database errors may contain data from a broken file: avoid printing details.
        if isinstance(exc, sqlite3.Error):
            raise SystemExit("n8n snapshot failed SQLite validation.") from None
        raise SystemExit(str(exc)) from None
    print(f"n8n SQLite backup created: {path}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
