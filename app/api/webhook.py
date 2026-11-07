"""Tilda webhook transport; technical logs never include form content."""
import hmac
import ipaddress
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

from app.core.leads import InvalidLead, validate
from app.db.leads import database_ready, save_lead

MAX_BODY = 16384


def is_loopback_address(address):
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def make_handler(dsn, token, threshold, store=save_lead, ready=database_ready):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LeadReceiver"
        sys_version = ""

        def log_message(self, *args):
            pass

        def reply(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"status": "ok"})
            if self.path == "/ready":
                if not is_loopback_address(self.client_address[0]):
                    return self.reply(404, {"error": "not_found"})
                try:
                    ok = bool(ready(dsn))
                except Exception:
                    ok = False
                return self.reply(200 if ok else 503, {"status": "ready" if ok else "not_ready"})
            return self.reply(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/webhook/tilda":
                return self.reply(404, {"error": "not_found"})
            supplied = self.headers.get("X-Webhook-Key", "")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return self.reply(401, {"error": "unauthorized"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > MAX_BODY:
                    return self.reply(413, {"error": "invalid_size"})
                if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    return self.reply(415, {"error": "unsupported_media_type"})
                pairs = parse_qsl(self.rfile.read(size).decode("utf-8"), keep_blank_values=True, max_num_fields=50, strict_parsing=True)
                data = dict(pairs)
                if len(data) != len(pairs):
                    raise InvalidLead("duplicate_fields")
                if data == {"test": "test"}:
                    return self.reply(200, {"status": "ok"})
                lead = validate(data, threshold)
            except (ValueError, UnicodeError):
                return self.reply(422, {"error": "invalid_lead"})
            try:
                result = store(dsn, lead)
            except Exception:
                return self.reply(503, {"error": "storage_unavailable"})
            if result == "conflict":
                return self.reply(409, {"error": "source_id_conflict"})
            return self.reply(200, {"status": "accepted" if result == "created" else "duplicate"})

    return Handler


def main():
    dsn = os.environ["DATABASE_URL"]
    token = os.environ["WEBHOOK_KEY"]
    if len(token) < 32:
        raise ValueError("WEBHOOK_KEY must contain at least 32 characters")
    threshold = int(os.environ.get("HIGH_BUDGET_THRESHOLD", "1000000"))
    if threshold <= 0:
        raise ValueError("HIGH_BUDGET_THRESHOLD must be positive")
    ThreadingHTTPServer(("0.0.0.0", 8080), make_handler(dsn, token, threshold)).serve_forever()


if __name__ == "__main__":
    main()
