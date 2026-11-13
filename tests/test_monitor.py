import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import monitor
from app.monitor import http_health, snapshot, status_ok, write_status


class StopLoop(BaseException):
    pass


class MonitorTests(unittest.TestCase):
    def test_monitor_check_and_invalid_arguments(self):
        for healthy, code in ((True, 0), (False, 1)):
            with self.subTest(healthy=healthy), patch.object(monitor.sys, 'argv', ['monitor', '--check']), \
                 patch.object(monitor, 'status_ok', return_value=healthy):
                self.assertEqual(monitor.main(), code)
        with patch.object(monitor.sys, 'argv', ['monitor', '--unknown']), \
             patch.object(monitor.sys, 'stderr', new_callable=io.StringIO):
            self.assertEqual(monitor.main(), 2)

    def test_monitor_alert_failure_marks_unhealthy_without_exposing_exception(self):
        report = {'status': 'ok', 'receiver': 'ok', 'public_tunnel': 'ok', 'queues': {'status': 'ok'}}
        env = {'DATABASE_URL': 'db', 'MONITOR_PUBLIC_HEALTH_URL': 'https://example.test/health',
               'MONITOR_TELEGRAM_ENABLED': 'true', 'MONITOR_TELEGRAM_BOT_TOKEN': 'test',
               'MONITOR_TELEGRAM_CHAT_ID': '1'}
        for outcome, expected in [('ok', 'ok'), ('unconfirmed', 'unconfirmed')]:
            alerts = unittest.mock.Mock(active={})
            alerts.observe.return_value = outcome
            with self.subTest(outcome=outcome), patch.object(monitor.sys, 'argv', ['monitor']), \
                 patch.dict(monitor.os.environ, env, clear=True), \
                 patch.object(monitor, 'StableAlerts', return_value=alerts), \
                 patch.object(monitor, 'snapshot', return_value=report.copy()), \
                 patch.object(monitor, 'write_status') as write, \
                 patch.object(monitor.time, 'sleep', side_effect=StopLoop), \
                 patch.object(monitor.sys, 'stdout', new_callable=io.StringIO):
                with self.assertRaises(StopLoop):
                    monitor.main()
                current = write.call_args.args[1]
                self.assertEqual(current['alert_delivery'], expected)
                self.assertEqual(current['status'], 'ok' if outcome == 'ok' else 'attention')

        with patch.object(monitor.sys, 'argv', ['monitor']), patch.dict(monitor.os.environ, env, clear=True), \
             patch.object(monitor, 'StableAlerts', side_effect=ValueError('secret')), \
             patch.object(monitor, 'snapshot', return_value=report.copy()), \
             patch.object(monitor, 'write_status') as write, \
             patch.object(monitor.time, 'sleep', side_effect=StopLoop), \
             patch.object(monitor.sys, 'stdout', new_callable=io.StringIO) as output:
            with self.assertRaises(StopLoop):
                monitor.main()
            self.assertEqual(write.call_args.args[1]['alert_delivery'], 'unavailable')
            self.assertNotIn('secret', output.getvalue())

    def test_queue_unavailable_does_not_report_healthy(self):
        with patch.object(monitor, 'queue_report', side_effect=OSError('offline')), \
             patch.object(monitor, 'http_health', return_value=True):
            result = monitor.snapshot('db', 'https://example.test/health')
        self.assertEqual(result['queues']['status'], 'unavailable')
        self.assertEqual(result['status'], 'attention')

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
