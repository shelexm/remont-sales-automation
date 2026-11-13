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
    def test_espo_connection_settings_and_status_are_validated(self):
        for url, key in [('http://example.invalid', 'key'), ('https://user:pass@example.invalid', 'key'),
                         ('https://example.invalid?bad=1', 'key'), ('https://example.invalid', '')]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                EspoCRM(url, key, Journal(), 'owner123')
        for response in ({}, {'stage': ''}, {'stage': None}, []):
            with self.subTest(response=response):
                provider = EspoCRM('https://example.invalid', 'key', Journal(), 'owner123',
                                   lambda *_args: response)
                with self.assertRaises(Permanent):
                    provider.get_stage('valid123')
        provider = EspoCRM('https://example.invalid', 'key', Journal(), 'owner123',
                           lambda *_args: {'stage': 'New'})
        self.assertEqual(provider.get_stage('valid123'), 'New')
        with self.assertRaises(Permanent):
            provider.get_stage('../invalid')

    def test_espo_does_not_trust_invalid_or_ambiguous_search(self):
        for payload, expected in [({'list': [{}, {}]}, Uncertain), ({'list': [{}]}, Permanent),
                                  ({'list': 'not-a-list'}, Permanent), (None, Permanent)]:
            with self.subTest(payload=payload):
                adapter = EspoCRM('https://example.invalid', 'key', Journal(), 'owner123',
                                  lambda *_args: payload)
                with self.assertRaises(expected):
                    adapter.sync(1, LEAD)

    def test_espo_invalid_create_response_is_never_assumed_successful(self):
        calls = []
        def fake(method, url, payload, headers, side_effect):
            calls.append(method)
            return {'list': []} if method == 'GET' else {'id': ''}
        adapter = EspoCRM('https://example.invalid', 'key', Journal(), 'owner123', fake)
        with self.assertRaises(Uncertain):
            adapter.sync(1, LEAD)
        with self.assertRaises(Uncertain):
            adapter.sync(1, LEAD)
        self.assertEqual(calls.count('POST'), 1)

    def test_telegram_reminders_and_failed_responses(self):
        payloads = []
        def send(_method, _url, payload, _headers, _side_effect):
            payloads.append(payload)
            return {'ok': True, 'result': {'message_id': 11}}
        self.assertEqual(Telegram('fixture', '1', send).remind(4), 11)
        self.assertIn('#4', payloads[0]['text'])
        for response in ({'ok': False}, None, []):
            with self.subTest(response=response):
                sender = Telegram('fixture', '1', lambda *_args: response)
                with self.assertRaises(Uncertain):
                    sender.notify(4, LEAD)
                with self.assertRaises(Uncertain):
                    sender.remind(4)
        with self.assertRaises(ValueError):
            Telegram('', '1')

    def test_http_failure_classification_and_safe_error_messages(self):
        for code, side_effect, expected in [(500, False, Retryable), (500, True, Uncertain),
                                             (403, True, Permanent), (409, False, Conflict)]:
            err = HTTPError('https://example.invalid/secret', code, 'error', {}, io.BytesIO(b'{}'))
            with self.subTest(code=code, side_effect=side_effect), \
                 patch('app.integrations.providers._open', side_effect=err), self.assertRaises(expected) as caught:
                request_json('POST', 'https://example.invalid/secret', {}, side_effect=side_effect)
            self.assertNotIn('secret', str(caught.exception))
        err = HTTPError('https://example.invalid', 429, 'rate', {}, io.BytesIO(b'invalid json'))
        with patch('app.integrations.providers._open', side_effect=err), self.assertRaises(Retryable) as caught:
            request_json('GET', 'https://example.invalid')
        self.assertEqual(caught.exception.delay, 60)
        for body, effect, expected in [(b'bad json', False, Retryable), (b'bad json', True, Uncertain),
                                       (b'x' * (1024 * 1024 + 1), False, Retryable)]:
            with self.subTest(body=body[:20], effect=effect), \
                 patch('app.integrations.providers._open', return_value=io.BytesIO(body)), \
                 self.assertRaises(expected):
                request_json('GET', 'https://example.invalid', side_effect=effect)

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
