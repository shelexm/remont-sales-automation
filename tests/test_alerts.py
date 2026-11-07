import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.alerts import StableAlerts, issues_from_report


def report(*, tunnel="ok", receiver="ok", queues=None):
    return {"receiver": receiver, "public_tunnel": tunnel,
            "queues": queues if queues is not None else {"status": "ok", "events": {}, "integrations": {}}}


class AlertTests(unittest.TestCase):
    def test_debounce_recovery_and_restart_do_not_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            messages = []
            monitor = StableAlerts(path, lambda text: messages.append(text) or True)
            self.assertEqual(monitor.observe(report(tunnel="unavailable")), "waiting")
            self.assertEqual(monitor.observe(report()), "unchanged")
            self.assertEqual(monitor.observe(report(tunnel="unavailable")), "waiting")
            self.assertEqual(monitor.observe(report(tunnel="unavailable")), "sent")
            self.assertEqual(len(messages), 1)
            self.assertNotIn("phone", messages[0])
            monitor = StableAlerts(path, lambda text: messages.append(text) or True)
            monitor.observe(report(tunnel="unavailable"))
            monitor.observe(report(tunnel="unavailable"))
            self.assertEqual(len(messages), 1)
            monitor.observe(report())
            self.assertEqual(monitor.observe(report()), "sent")
            self.assertIn("Восстановлено", messages[1])
            self.assertEqual(json.loads(path.read_text()), {})

    def test_unknown_delivery_is_not_retried_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            messages = []
            monitor = StableAlerts(path, lambda text: messages.append(text) or False)
            monitor.observe(report(receiver="unavailable"))
            self.assertEqual(monitor.observe(report(receiver="unavailable")), "unconfirmed")
            monitor = StableAlerts(path, lambda text: messages.append(text) or True)
            monitor.observe(report(receiver="unavailable"))
            monitor.observe(report(receiver="unavailable"))
            self.assertEqual(len(messages), 1)
            monitor.observe(report())
            monitor.observe(report())
            self.assertEqual(len(messages), 1)

    def test_database_unavailable_does_not_clear_existing_queue_incident(self):
        issues = issues_from_report(report(queues={"status": "unavailable"}), {"queue.crm": "sent"})
        self.assertEqual(issues, {"queue.crm", "queue.database"})

    def test_flapping_unrelated_issue_does_not_hide_persistent_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = []
            monitor = StableAlerts(Path(directory) / "state", lambda text: messages.append(text) or True)
            monitor.observe(report(tunnel="unavailable", receiver="ok"))
            self.assertEqual(monitor.observe(report(tunnel="unavailable", receiver="unavailable")), "sent")
            self.assertEqual(len(messages), 1)
            self.assertIn("туннеля", messages[0])

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not json")
            with self.assertRaises(ValueError):
                StableAlerts(path, lambda text: True)

    def test_failure_after_send_does_not_retry_in_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            messages = []
            monitor = StableAlerts(path, lambda text: messages.append(text) or True)
            monitor.observe(report(receiver="unavailable"))
            from app import alerts
            original = alerts.save_state
            calls = []

            def fail_second(*args):
                calls.append(1)
                if len(calls) == 2:
                    raise OSError("simulated failure")
                return original(*args)

            with patch("app.alerts.save_state", side_effect=fail_second):
                with self.assertRaises(OSError):
                    monitor.observe(report(receiver="unavailable"))
            monitor.observe(report(receiver="unavailable"))
            self.assertEqual(len(messages), 1)
            self.assertEqual(monitor.active["receiver"], "attempted")
