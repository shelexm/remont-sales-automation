"""Tilda webhook receiver. No personal data is written to application logs."""
import hashlib
import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

MAX_BODY = 16384
SERVICES = {"cosmetic", "capital", "design", "other"}
SERVICE_LABELS = {"Косметический ремонт": "cosmetic", "Капитальный ремонт": "capital",
                  "Ремонт по дизайн-проекту": "design", "Другое": "other"}


class InvalidLead(ValueError):
    pass


def validate(data, threshold):
    def field(name, limit):
        value = data.get(name, "")
        if not isinstance(value, str):
            raise InvalidLead(name)
        value = value.strip()
        if not value or len(value) > limit:
            raise InvalidLead(name)
        return value

    source_id = field("tranid", 128)
    name = field("Name", 120)
    raw_phone = field("Phone", 40)
    if re.search(r"[^0-9+()\-\s]", raw_phone) or raw_phone.count("+") > 1 or ("+" in raw_phone and not raw_phone.startswith("+")):
        raise InvalidLead("Phone")
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or digits[0] != "7" or digits[1] not in "3489":
        raise InvalidLead("Phone")
    service = field("Service", 64)
    service = SERVICE_LABELS.get(service, service)
    if service not in SERVICES:
        raise InvalidLead("Service")
    try:
        area = int(field("Area", 5))
        budget = int(field("Budget", 12))
    except ValueError as exc:
        raise InvalidLead("Area or Budget") from exc
    if not 1 <= area <= 10000 or not 1 <= budget <= 1000000000:
        raise InvalidLead("Area or Budget")
    if field("Consent", 8).lower() not in {"yes", "1", "true", "on"}:
        raise InvalidLead("Consent")
    return {"source_id": source_id, "name": name, "phone": "+" + digits,
            "service": service, "area": area, "budget": budget,
            "budget_class": "high" if budget >= threshold else "low"}


def fingerprint(lead):
    source_fields = {key: value for key, value in lead.items() if key != "budget_class"}
    body = json.dumps(source_fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def save_lead(dsn, lead):
    import psycopg
    from psycopg.types.json import Jsonb

    digest = fingerprint(lead)
    with psycopg.connect(dsn, connect_timeout=2) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '2s'")
            cur.execute("""INSERT INTO lead_events (source_id, payload_hash, payload)
                VALUES (%s, %s, %s) ON CONFLICT (source_id) DO NOTHING RETURNING id""",
                        (lead["source_id"], digest, Jsonb(lead)))
            row = cur.fetchone()
            if row:
                return "created"
            cur.execute("SELECT payload_hash FROM lead_events WHERE source_id = %s", (lead["source_id"],))
            existing = cur.fetchone()
            return "duplicate" if existing and hmac.compare_digest(existing[0], digest) else "conflict"


def make_handler(dsn, token, threshold, store=save_lead):
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
            self.reply(200 if self.path == "/health" else 404, {"status": "ok"} if self.path == "/health" else {"error": "not_found"})

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
