import http.client
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "lead_ingest"))
from app import InvalidLead, fingerprint, make_handler, validate  # noqa: E402

TOKEN = "a" * 32
LEAD = {"tranid": "123:456", "Name": "Тест", "Phone": "8 (999) 123-45-67", "Service": "capital", "Area": "65", "Budget": "1200000", "Consent": "yes"}


class LeadTests(unittest.TestCase):
    def test_normalization_and_classification(self):
        result = validate(LEAD, 1000000)
        self.assertEqual(result["phone"], "+79991234567")
        self.assertEqual(result["budget_class"], "high")
        self.assertEqual(validate({**LEAD, "Budget": "500"}, 1000000)["budget_class"], "low")

    def test_missing_invalid_phone_and_consent(self):
        for override in ({"Name": ""}, {"Phone": "123"}, {"Consent": "no"}):
            with self.subTest(override=override), self.assertRaises(InvalidLead):
                validate({**LEAD, **override}, 1000000)

    def test_threshold_does_not_change_idempotency_fingerprint(self):
        self.assertEqual(fingerprint(validate(LEAD, 100)), fingerprint(validate(LEAD, 2000000)))


class WebhookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = []
        def store(dsn, lead):
            if dsn == "fail":
                raise RuntimeError("connection")
            cls.saved.append(lead)
            return "created"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler("ok", TOKEN, 1000000, store))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def request(self, payload, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request("POST", "/webhook/tilda", urlencode(payload), {"Content-Type": "application/x-www-form-urlencoded", "X-Webhook-Key": token})
        response = conn.getresponse()
        status = response.status
        response.read()
        conn.close()
        return status

    def test_success_and_tilda_handshake(self):
        before = len(self.saved)
        self.assertEqual(self.request({"test": "test"}), 200)
        self.assertEqual(len(self.saved), before)
        self.assertEqual(self.request(LEAD), 200)
        self.assertEqual(len(self.saved), before + 1)

    def test_storage_failure_returns_503(self):
        def fail_store(dsn, lead):
            raise RuntimeError("storage failure")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler("ok", TOKEN, 1000000, fail_store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("POST", "/webhook/tilda", urlencode(LEAD), {
                "Content-Type": "application/x-www-form-urlencoded", "X-Webhook-Key": TOKEN})
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            response.read()
            conn.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_unauthorized_and_invalid_are_not_stored(self):
        before = len(self.saved)
        self.assertEqual(self.request(LEAD, "wrong"), 401)
        self.assertEqual(self.request({**LEAD, "Phone": "wrong"}), 422)
        self.assertEqual(len(self.saved), before)


if __name__ == "__main__":
    unittest.main()
