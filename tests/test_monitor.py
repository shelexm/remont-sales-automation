import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.monitor import http_health, snapshot, status_ok, write_status


class MonitorTests(unittest.TestCase):
    def test_changes_to_unavailable_or_unconfigured_are_attention(self):
        with patch("app.monitor.queue_report", return_value={"status": "ok"}), \
             patch("app.monitor.http_health", side_effect=[True, False]):
            self.assertEqual(snapshot("db", "https://example.test/health")["public_tunnel"], "unavailable")
        with patch("app.monitor.queue_report", return_value={"status": "attention"}), \
             patch("app.monitor.http_health", return_value=True):
            self.assertEqual(snapshot("db", "https://example.test/health")["status"], "attention")
            self.assertEqual(snapshot("db", "")["public_tunnel"], "not_configured")

    def test_health_file_expires_and_rejects_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            self.assertFalse(status_ok(path, 1000))
            write_status(path, {"status": "ok"}, 1000)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(status_ok(path, 1150))
            self.assertFalse(status_ok(path, 1151))
            write_status(path, {"status": "attention"}, 1000)
            self.assertFalse(status_ok(path, 1001))
            self.assertEqual(json.loads(path.read_text())["status"], "attention")

    def test_external_url_must_use_https_and_not_include_credentials(self):
        self.assertFalse(http_health("http://example.test/health", external=True))
        self.assertFalse(http_health("https://name:secret@example.test/health", external=True))
        self.assertFalse(http_health("https://[broken", external=True))

    def test_real_http_probe_rejects_redirects_and_bad_responses(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/healthy")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}' if self.path == "/healthy" else b"not json")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            self.assertTrue(http_health(base + "/healthy", external=False))
            self.assertFalse(http_health(base + "/redirect", external=False))
            self.assertFalse(http_health(base + "/bad", external=False))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
