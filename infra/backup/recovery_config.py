"""Read the minimal local demo recovery configuration without displaying secrets."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess

FIELDS = ("POSTGRES_PASSWORD", "ESPO_URL", "ESPO_API_KEY", "ESPO_ASSIGNED_USER_ID")


def secret_env(container, names):
    values = {}
    for name in names:
        if not re.fullmatch(r"[A-Z_]+", name):
            raise ValueError("Invalid configuration name.")
        result = subprocess.run(["docker", "exec", container, "sh", "-c", 'printenv "$1"', "sh", name],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        raw = result.stdout
        if result.returncode or not raw.endswith(b"\n") or len(raw) > 4097:
            raise RuntimeError("Required source configuration is unavailable.")
        try:
            values[name] = raw[:-1].decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError("Required source configuration is unavailable.") from None
        if not values[name] or "\0" in values[name]:
            raise RuntimeError("Required source configuration is unavailable.")
    return values


def validate_config(data):
    if not isinstance(data, dict) or set(data) != {"version", *FIELDS} or type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Invalid recovery configuration format.")
    if any(not isinstance(data[name], str) or not data[name] or len(data[name]) > 1024 or "\0" in data[name] for name in FIELDS):
        raise ValueError("Invalid recovery configuration values.")
    if data["ESPO_URL"].rstrip("/") != "https://crm.remont.internal:8443":
        raise ValueError("Recovery CRM URL is not the expected local demo endpoint.")
    if not re.fullmatch(r"[A-Za-z0-9]{1,128}", data["ESPO_ASSIGNED_USER_ID"]):
        raise ValueError("Invalid recovery CRM user ID.")
    return data


def read_config(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Recovery configuration must not be a symlink.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Recovery configuration must be a private regular file owned by the current user (0600).")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ValueError("Recovery configuration is too large.")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Invalid recovery configuration format.") from None
        return validate_config(data)
    finally:
        os.close(fd)
