"""Exercise private chat binding without contacting Telegram or storing real secrets."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "infra" / "local-demo" / "telegram_setup.py"
SPEC = importlib.util.spec_from_file_location("telegram_setup", PATH)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
FAKE_TOKEN = "123456789:" + "x" * 35
BOT = {"is_bot": True, "username": "sample_test_bot"}


class TelegramBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        for attr, name in (("TOKEN_FILE", "telegram-token"), ("CHALLENGE_FILE", "challenge"),
                           ("ENV_FILE", "telegram.env")):
            patcher = patch.object(setup, attr, directory / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        setup.TOKEN_FILE.write_text(FAKE_TOKEN)

    def run_setup(self, mode, replies):
        output = io.StringIO()
        with patch.object(setup, "request", side_effect=replies), patch.object(setup.sys, "argv", ["setup", mode]), \
                contextlib.redirect_stdout(output):
            setup.main()
        self.assertNotIn(FAKE_TOKEN, output.getvalue())
        return output.getvalue()

    def test_private_challenge_binds_once_and_never_prints_token(self):
        self.run_setup("challenge", [BOT])
        challenge = setup.CHALLENGE_FILE.read_text().strip()
        updates = [{"message": {"chat": {"type": "group", "id": -99}, "text": challenge}},
                   {"message": {"chat": {"type": "private", "id": 1234}, "text": "/start " + challenge}}]
        self.run_setup("bind", [BOT, updates])
        self.assertEqual(os.stat(setup.ENV_FILE).st_mode & 0o777, 0o600)
        self.assertIn("TELEGRAM_CHAT_ID=1234", setup.ENV_FILE.read_text())
        self.assertFalse(setup.CHALLENGE_FILE.exists())
        with self.assertRaises(SystemExit):
            self.run_setup("bind", [BOT])

    def test_multiple_matching_private_chats_are_rejected(self):
        self.run_setup("challenge", [BOT])
        challenge = setup.CHALLENGE_FILE.read_text().strip()
        updates = [{"message": {"chat": {"type": "private", "id": chat_id}, "text": challenge}}
                   for chat_id in (1234, 5678)]
        with self.assertRaises(SystemExit):
            self.run_setup("bind", [BOT, updates])
        self.assertFalse(setup.ENV_FILE.exists())


if __name__ == "__main__":
    unittest.main()
