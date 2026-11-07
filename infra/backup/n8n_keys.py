"""Read an n8n key without writing it to command arguments or logs."""
import os
from pathlib import Path
import stat
import subprocess


def validate_key(data):
    if not data or len(data) > 4096 or b"\0" in data:
        raise ValueError("Invalid n8n encryption key.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Invalid n8n encryption key encoding.") from None


def read_container_key(container):
    result = subprocess.run(["docker", "exec", container, "sh", "-c",
                             'test -n "$N8N_ENCRYPTION_KEY" && printf %s "$N8N_ENCRYPTION_KEY"'],
                            capture_output=True, timeout=10)
    if result.returncode:
        raise RuntimeError("Could not read the key from the specified n8n container.")
    validate_key(result.stdout)
    return result.stdout


def read_key_file(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Key file must not be a symlink.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Key file must be private and owned by the current user (0600).")
        with os.fdopen(fd, "rb", closefd=False) as source:
            key = source.read(4097)
        validate_key(key)
        return key
    finally:
        os.close(fd)
