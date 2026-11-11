"""Bounded, synthetic-only bridge for the static public demonstration."""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICES = {"cosmetic", "capital", "design", "other"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def make_handler(receiver, key, origins, limit=12, window=3600):
    recent = deque()
    lock = threading.Lock()
    opener = urllib.request.build_opener(NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, body, origin=None):
            data = json.dumps(body).encode()
            self.send_response(status)
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"status": "ok"})
            return self.reply(404, {"status": "not_found"})

        def do_POST(self):
            origin = self.headers.get("Origin", "")
            if self.path != "/demo/lead":
                return self.reply(404, {"status": "not_found"}, origin)
            if origin not in origins:
                return self.reply(403, {"status": "forbidden"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 512 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    return self.reply(400, {"status": "invalid"}, origin)
                pairs = urllib.parse.parse_qsl(self.rfile.read(size).decode(), strict_parsing=True, max_num_fields=3)
                fields = dict(pairs)
                if len(pairs) != 3 or len(fields) != 3 or set(fields) != {"service", "area", "budget"}:
                    raise ValueError("invalid fields")
                service = fields["service"]
                if service not in SERVICES or not fields["area"].isascii() or not fields["area"].isdigit() or not fields["budget"].isascii() or not fields["budget"].isdigit():
                    raise ValueError("invalid values")
                area, budget = int(fields["area"]), int(fields["budget"])
                if not 1 <= area <= 10000 or not 1 <= budget <= 1000000000:
                    raise ValueError("out of range")
            except (ValueError, UnicodeError):
                return self.reply(400, {"status": "invalid"}, origin)
            with lock:
                now = time.monotonic()
                while recent and recent[0] <= now - window:
                    recent.popleft()
                if len(recent) >= limit:
                    return self.reply(429, {"status": "limit"}, origin)
                recent.append(now)
            # No visitor-supplied name, phone, or arbitrary text crosses this bridge.
            payload = urllib.parse.urlencode({"tranid": "github-demo-" + uuid.uuid4().hex,
                "Name": "Демо заявка", "Phone": "+79990000000", "Service": service,
                "Area": area, "Budget": budget, "Consent": "yes"}).encode()
            request = urllib.request.Request(receiver, payload,
                {"Content-Type": "application/x-www-form-urlencoded", "X-Webhook-Key": key}, method="POST")
            try:
                with opener.open(request, timeout=5) as response:
                    result = json.load(response)
                    if response.status != 200 or result.get("status") != "accepted":
                        raise ValueError("unexpected receiver response")
            except (OSError, ValueError):
                return self.reply(503, {"status": "unavailable"}, origin)
            return self.reply(200, {"status": "accepted"}, origin)

    return Handler


def main():
    origins = set(os.environ["DEMO_ALLOWED_ORIGINS"].split(","))
    receiver = os.environ.get("DEMO_RECEIVER_URL", "http://receiver:8080/webhook/tilda")
    ThreadingHTTPServer(("0.0.0.0", 8081), make_handler(receiver, os.environ["WEBHOOK_KEY"], origins)).serve_forever()


if __name__ == "__main__":
    main()
