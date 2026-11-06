import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from app.integrations.providers import Conflict, EspoCRM, NoRedirect, Permanent, Retryable, Telegram, Uncertain, request_json

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
    def test_espo_requires_valid_assigned_user(self):
        for owner in ("", "bad-id", None):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                EspoCRM("https://example.invalid", "unit-test", Journal(), owner)

    def test_espo_creates_then_updates_without_duplicate(self):
        records = {}
        def fake(method, url, payload, headers, side_effect):
            self.assertEqual(headers["X-Api-Key"], "unit-test")
            if method in ("POST", "PUT"):
                self.assertEqual(payload["assignedUserId"], "owner123")
            entity = "Contact" if "/Contact" in url else "Opportunity"
            if method in ("POST", "PUT") and entity == "Opportunity":
                self.assertEqual(payload["amountCurrency"], "RUB")
            if method == "GET":
                return {"list": [{"id": records[entity]}] if entity in records else []}
            if method == "POST":
                records[entity] = entity + "123"
                return {"id": records[entity]}
            self.assertEqual(method, "PUT")
            return {"id": records[entity]}
        adapter = EspoCRM("https://example.invalid", "unit-test", Journal(), "owner123", fake)
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
        adapter = EspoCRM("https://example.invalid", "unit-test", Journal(), "owner123", fake)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        self.assertEqual(len(posts), 1)

    def test_new_case_reuses_single_contact_with_same_phone(self):
        actions = []
        def fake(method, url, payload, headers, side_effect):
            actions.append((method, url))
            if method == "GET":
                return {"list": [{"id": "contact123"}]} if "phoneNumber" in url else {"list": []}
            if method == "POST" and "/Opportunity" in url:
                self.assertEqual(payload["contactsIds"], ["contact123"])
                return {"id": "deal123"}
            self.fail("Existing contact must not be modified or duplicated")
        adapter = EspoCRM("https://example.invalid", "unit-test", Journal(), "owner123", fake)
        self.assertEqual(adapter.sync(8, LEAD).contact_id, "contact123")
        self.assertFalse(any(method == "PUT" for method, _ in actions))

    def test_multiple_contact_matches_do_not_create_or_choose(self):
        def fake(method, url, payload, headers, side_effect):
            if method == "GET" and "phoneNumber" in url:
                return {"list": [{"id": "one1"}, {"id": "two2"}]}
            self.assertEqual(method, "GET")
            return {"list": []}
        with self.assertRaises(Uncertain):
            EspoCRM("https://example.invalid", "unit-test", Journal(), "owner123", fake).sync(10, LEAD)

    def test_contact_conflict_is_reconciled_only_when_single_match_is_visible(self):
        seen = []
        def fake(method, url, payload, headers, side_effect):
            if method == "GET" and "phoneNumber" in url:
                seen.append(1)
                return {"list": [] if len(seen) == 1 else [{"id": "contact123"}]}
            if method == "GET":
                return {"list": []}
            if "/Contact" in url:
                raise Conflict("remote_conflict")
            return {"id": "deal123"}
        self.assertEqual(EspoCRM("https://example.invalid", "unit-test", Journal(), "owner123", fake).sync(11, LEAD).contact_id, "contact123")

    def test_contact_conflict_with_no_visible_match_is_uncertain(self):
        def fake(method, url, payload, headers, side_effect):
            if method == "GET":
                return {"list": []}
            raise Conflict("remote_conflict")
        adapter = EspoCRM("https://example.invalid", "unit-test", Journal(), "owner123", fake)
        with self.assertRaises(Uncertain):
            adapter.sync(12, LEAD)
        with self.assertRaises(Uncertain):
            adapter.sync(12, LEAD)

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
        with patch("app.integrations.providers._open", side_effect=error):
            with self.assertRaises(Retryable) as caught:
                request_json("POST", "https://example.invalid", {}, side_effect=True)
        self.assertEqual(caught.exception.delay, 17)
        with patch("app.integrations.providers._open", side_effect=URLError("down")):
            with self.assertRaises(Uncertain):
                request_json("POST", "https://example.invalid", {}, side_effect=True)
            with self.assertRaises(Retryable):
                request_json("GET", "https://example.invalid")
        error = HTTPError("https://example.invalid", 403, "forbidden", {}, io.BytesIO(b''))
        with patch("app.integrations.providers._open", side_effect=error), self.assertRaises(Permanent):
            request_json("GET", "https://example.invalid")
        error = HTTPError("https://example.invalid", 409, "conflict", {}, io.BytesIO(b''))
        with patch("app.integrations.providers._open", side_effect=error), self.assertRaises(Conflict):
            request_json("POST", "https://example.invalid", {}, side_effect=True)


if __name__ == "__main__":
    unittest.main()
