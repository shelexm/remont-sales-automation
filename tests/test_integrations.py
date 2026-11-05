import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "integrations"))
from providers import EspoCRM, NoRedirect, Permanent, Retryable, Telegram, Uncertain, request_json  # noqa: E402

LEAD = {"name": "Example", "phone": "+79991234567", "budget": 200000, "service": "capital", "budget_class": "low"}


class Journal:
    def __init__(self):
        self.items = {}
    def begin_create(self, entity, key):
        if self.items.get((entity, key)):
            return False
        self.items[(entity, key)] = True
        return True
    def found(self, entity, key, remote_id):
        self.items[(entity, key)] = remote_id
    def reset_create(self, entity, key):
        self.items.pop((entity, key), None)


class ProviderTests(unittest.TestCase):
    def test_espo_creates_then_updates_without_duplicate(self):
        records = {}
        def fake(method, url, payload, headers, side_effect):
            self.assertEqual(headers["X-Api-Key"], "unit-test")
            entity = "Contact" if "/Contact" in url else "Opportunity"
            if method == "GET":
                return {"list": [{"id": records[entity]}] if entity in records else []}
            if method == "POST":
                records[entity] = entity + "123"
                return {"id": records[entity]}
            self.assertEqual(method, "PUT")
            return {"id": records[entity]}
        adapter = EspoCRM("https://example.invalid", "unit-test", Journal(), fake)
        first = adapter.sync(7, LEAD)
        second = adapter.sync(7, LEAD)
        self.assertEqual(first, second)
        self.assertEqual(len(records), 2)

    def test_ambiguous_create_is_never_blindly_retried(self):
        posts = []
        def fake(method, url, payload, headers, side_effect):
            if method == "GET":
                return {"list": []}
            posts.append(url)
            raise Uncertain("timeout")
        adapter = EspoCRM("https://example.invalid", "unit-test", Journal(), fake)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        self.assertEqual(len(posts), 1)

    def test_telegram_notification_excludes_personal_data(self):
        calls = []
        def fake(method, url, payload, headers, side_effect):
            calls.append(payload)
            return {"ok": True, "result": {"message_id": 3}}
        self.assertEqual(Telegram("test-token", "123", fake).notify(7, LEAD), 3)
        self.assertNotIn(LEAD["phone"], json.dumps(calls))
        self.assertNotIn(LEAD["name"], json.dumps(calls))

    def test_redirect_does_not_forward_secrets(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_http_errors_respect_rate_limit_and_uncertainty(self):
        error = HTTPError("https://example.invalid", 429, "rate", {}, io.BytesIO(b'{"parameters":{"retry_after":17}}'))
        with patch("providers._open", side_effect=error):
            with self.assertRaises(Retryable) as caught:
                request_json("POST", "https://example.invalid", {}, side_effect=True)
        self.assertEqual(caught.exception.delay, 17)
        with patch("providers._open", side_effect=URLError("down")):
            with self.assertRaises(Uncertain):
                request_json("POST", "https://example.invalid", {}, side_effect=True)
            with self.assertRaises(Retryable):
                request_json("GET", "https://example.invalid")
        error = HTTPError("https://example.invalid", 403, "forbidden", {}, io.BytesIO(b''))
        with patch("providers._open", side_effect=error), self.assertRaises(Permanent):
            request_json("GET", "https://example.invalid")


if __name__ == "__main__":
    unittest.main()
