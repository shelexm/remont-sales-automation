import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from app.integrations.bitrix import BitrixCRM, bitrix_transport
from app.integrations.providers import Permanent, Retryable, Uncertain

LEAD = {"name": "Example", "phone": "+79991234567", "budget": 250000, "service": "capital"}
CONTACT_FIELD, DEAL_FIELD = "UF_CRM_CONTACT_KEY", "UF_CRM_DEAL_KEY"
WEBHOOK = "https://example.invalid/rest/1/synthetic1234/"


class Journal:
    def __init__(self):
        self.items = {}

    def begin_create(self, entity, key):
        if (entity, key) in self.items:
            return False
        self.items[entity, key] = "started"
        return True

    def found(self, entity, key, remote_id):
        self.items[entity, key] = remote_id

    def reset_create(self, entity, key):
        self.items.pop((entity, key), None)


class FakeBitrix:
    def __init__(self):
        self.contacts, self.deals = {}, {}
        self.calls = []
        self.fail_next_add = None
        self.fail_after_add = None
        self.fail_after_entity = None
        self.drop_key = False

    def __call__(self, method, url, params, headers, side_effect):
        self.assert_transport(method, headers)
        api = url.rsplit("/", 1)[-1]
        self.calls.append(api)
        entity = "contact" if ".contact." in api else "deal"
        store = self.contacts if entity == "contact" else self.deals
        field = CONTACT_FIELD if entity == "contact" else DEAL_FIELD
        if api.endswith(".fields"):
            return {"result": {field: {"type": "string", "isReadOnly": False, "isMultiple": False, "isImmutable": False}}}
        if api.endswith(".list"):
            name, value = next(iter(params["filter"].items()))
            # Bitrix ignores the equality operator for PHONE and returns all contacts.
            if name == "=PHONE":
                rows = list(store.values())
                return {"result": rows, "total": len(rows)}
            name = name.lstrip("=")
            if name == "PHONE":
                rows = [x for x in store.values() if any(p["VALUE"] == value for p in x.get("PHONE", []))]
            else:
                rows = [x for x in store.values() if x.get(name) == value]
            return {"result": rows, "total": len(rows)}
        if api.endswith(".add"):
            if self.fail_next_add:
                error, self.fail_next_add = self.fail_next_add, None
                raise error
            ident = str(len(store) + 1)
            record = {**params["fields"], "ID": ident}
            if self.drop_key:
                record.pop(field, None)
            store[ident] = record
            if self.fail_after_add and (self.fail_after_entity is None or self.fail_after_entity == entity):
                error, self.fail_after_add = self.fail_after_add, None
                raise error
            return {"result": int(ident)}
        if api.endswith(".get"):
            return {"result": store[str(params["id"])]}
        if api.endswith(".update"):
            store[str(params["id"])].update(params["fields"])
            return {"result": True}
        raise AssertionError(api)

    @staticmethod
    def assert_transport(method, headers):
        if method != "POST" or headers:
            raise AssertionError("Unexpected transport parameters")


def make_adapter(fake, journal=None, installation="install123"):
    return BitrixCRM(WEBHOOK, CONTACT_FIELD, DEAL_FIELD, installation, journal or Journal(), fake)


class BitrixTests(unittest.TestCase):
    def test_phone_search_ignores_unrelated_contact(self):
        fake = FakeBitrix()
        fake.contacts["1"] = {"ID": "1", "PHONE": [{"VALUE": "+79990000000"}]}
        result = make_adapter(fake).sync(7, LEAD)
        self.assertEqual(result.contact_id, "2")
        self.assertEqual(len(fake.contacts), 2)

    def test_create_update_and_reuse_contact_without_duplicate(self):
        fake = FakeBitrix()
        adapter = make_adapter(fake)
        first = adapter.sync(7, LEAD)
        self.assertEqual(first.contact_id, "1")
        self.assertEqual(first.deal_id, "1")
        self.assertEqual(adapter.sync(7, {**LEAD, "budget": 400000}), first)
        self.assertEqual(fake.deals["1"]["OPPORTUNITY"], 400000)
        self.assertEqual(fake.deals["1"]["CURRENCY_ID"], "RUB")
        self.assertEqual(fake.deals["1"]["CONTACT_IDS"], [1])
        second = adapter.sync(8, LEAD)
        self.assertEqual(second.contact_id, "1")
        self.assertEqual(second.deal_id, "2")
        self.assertEqual((len(fake.contacts), len(fake.deals)), (1, 2))
        fake.deals["1"]["STAGE_ID"] = "NEW"
        self.assertEqual(adapter.get_stage("1"), "NEW")

    def test_ambiguous_create_is_not_retried_without_remote_match(self):
        fake = FakeBitrix()
        fake.fail_next_add = Uncertain("timeout")
        adapter = make_adapter(fake)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        self.assertEqual(fake.calls.count("crm.contact.add"), 1)

    def test_rate_limit_can_retry_after_confirmed_rejection(self):
        fake = FakeBitrix()
        fake.fail_next_add = Retryable("rate", 60)
        adapter = make_adapter(fake)
        with self.assertRaises(Retryable):
            adapter.sync(9, LEAD)
        self.assertEqual(adapter.sync(9, LEAD).contact_id, "1")
        self.assertEqual(fake.calls.count("crm.contact.add"), 2)

    def test_lost_create_response_recovers_via_custom_key_without_second_create(self):
        fake = FakeBitrix()
        fake.fail_after_add = Uncertain("response_lost")
        adapter = make_adapter(fake)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        self.assertEqual(adapter.sync(9, LEAD).contact_id, "1")
        self.assertEqual(fake.calls.count("crm.contact.add"), 1)
        self.assertEqual(len(fake.contacts), 1)

    def test_lost_deal_response_recovers_without_second_deal(self):
        fake = FakeBitrix()
        fake.fail_after_add = Uncertain("response_lost")
        fake.fail_after_entity = "deal"
        adapter = make_adapter(fake)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        self.assertEqual(adapter.sync(9, LEAD).deal_id, "1")
        self.assertEqual(fake.calls.count("crm.deal.add"), 1)
        self.assertEqual(len(fake.deals), 1)

    def test_unpersisted_custom_key_stops_processing(self):
        fake = FakeBitrix()
        fake.drop_key = True
        adapter = make_adapter(fake)
        with self.assertRaises(Uncertain):
            adapter.sync(9, LEAD)
        self.assertEqual(len(fake.contacts), 1)
        self.assertEqual(len(fake.deals), 0)

    def test_duplicate_phone_and_unexpected_filters_stop(self):
        fake = FakeBitrix()
        fake.contacts = {"1": {"ID": "1", "PHONE": [{"VALUE": LEAD["phone"]}]},
                         "2": {"ID": "2", "PHONE": [{"VALUE": LEAD["phone"]}]}}
        with self.assertRaises(Uncertain):
            make_adapter(fake).sync(3, LEAD)
        self.assertNotIn("crm.contact.add", fake.calls)

        def ignored_filter(method, url, params, headers, side_effect):
            if url.endswith(".fields"):
                return fake(method, url, params, headers, side_effect)
            return {"result": [{"ID": "3", CONTACT_FIELD: "wrong"}], "total": 1}
        with self.assertRaises(Uncertain):
            make_adapter(ignored_filter).sync(3, LEAD)

    def test_field_configuration_and_url_are_validated_without_disclosing_secret(self):
        for url in ("http://example.invalid/rest/1/synthetic1234/", WEBHOOK + "?token=bad",
                    "https://example.invalid/rest/1/secret/extra", "https://name:pass@example.invalid/rest/1/synthetic1234/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                BitrixCRM(url, CONTACT_FIELD, DEAL_FIELD, "install123", Journal(), FakeBitrix())
        def bad_field(*args):
            return {"result": {CONTACT_FIELD: {"type": "string", "isReadOnly": True}}}
        with self.assertRaises(Permanent):
            make_adapter(bad_field).sync(2, LEAD)

    def test_portal_and_installation_have_separate_journal_namespace(self):
        first = make_adapter(FakeBitrix(), installation="install123")
        second = make_adapter(FakeBitrix(), installation="install456")
        self.assertNotEqual(first.entities, second.entities)
        self.assertNotEqual(first.prefix, second.prefix)
        self.assertNotIn("synthetic1234", str(first.entities))

    def test_bitrix_http_rate_limit_and_unknown_write_result(self):
        body = json.dumps({"error": "QUERY_LIMIT_EXCEEDED"}).encode()
        error = HTTPError(WEBHOOK, 503, "limit", {}, io.BytesIO(body))
        with patch("app.integrations.bitrix._opener.open", side_effect=error):
            with self.assertRaises(Retryable) as caught:
                bitrix_transport("POST", WEBHOOK, {}, {}, True)
        self.assertEqual(caught.exception.delay, 60)
        self.assertNotIn("synthetic1234", str(caught.exception))
        with patch("app.integrations.bitrix._opener.open", side_effect=URLError("network")):
            with self.assertRaises(Uncertain):
                bitrix_transport("POST", WEBHOOK, {}, {}, True)
            with self.assertRaises(Retryable):
                bitrix_transport("POST", WEBHOOK, {}, {}, False)
        error = HTTPError(WEBHOOK, 400, "rejected", {}, io.BytesIO(b'{"error":"VALIDATION_ERROR"}'))
        with patch("app.integrations.bitrix._opener.open", side_effect=error), self.assertRaises(Uncertain):
            bitrix_transport("POST", WEBHOOK, {}, {}, True)


if __name__ == "__main__":
    unittest.main()
