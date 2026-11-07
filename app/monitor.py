"""Periodically report local demo health without handling lead data."""
import json
import os
import sys
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import psycopg

from app.diagnostics import queue_report

STATUS_FILE = Path("/tmp/remont-monitor-status.json")
INTERVAL = 60
MAX_AGE = 150


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_health(url, *, external):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != ("https" if external else "http") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            return False
        with build_opener(NoRedirect).open(Request(url, method="GET"), timeout=4) as response:
            data = response.read(1025)
            return response.status == 200 and len(data) <= 1024 and json.loads(data).get("status") == "ok"
    except HTTPError as error:
        error.close()
        return False
    except (URLError, OSError, ValueError, AttributeError, HTTPException):
        return False


def snapshot(dsn, public_url):
    try:
        queues = queue_report(dsn)
    except (psycopg.Error, ValueError, OSError):
        queues = {"status": "unavailable"}
    receiver = "ok" if http_health("http://receiver:8080/health", external=False) else "unavailable"
    public = ("not_configured" if not public_url else
              "ok" if http_health(public_url, external=True) else "unavailable")
    status = "ok" if receiver == public == queues["status"] == "ok" else "attention"
    return {"status": status, "receiver": receiver, "public_tunnel": public, "queues": queues}


def write_status(path, report, now):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump({"checked_at": now, **report}, output, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)


def status_ok(path, now):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age = now - float(data["checked_at"])
        return data["status"] == "ok" and 0 <= age <= MAX_AGE
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def main():
    if sys.argv[1:] == ["--check"]:
        return 0 if status_ok(STATUS_FILE, time.time()) else 1
    if sys.argv[1:]:
        print("Usage: python -m app.monitor [--check]", file=sys.stderr)
        return 2
    dsn = os.environ.get("DATABASE_URL", "")
    public_url = os.environ.get("MONITOR_PUBLIC_HEALTH_URL", "")
    previous = None
    while True:
        current = snapshot(dsn, public_url)
        write_status(STATUS_FILE, current, time.time())
        if current != previous:
            print(json.dumps(current, ensure_ascii=False, sort_keys=True), flush=True)
            previous = current
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
