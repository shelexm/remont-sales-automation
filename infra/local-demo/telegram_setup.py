"""Bind a newly created bot to a private chat using a one-time challenge."""
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.request import Request, urlopen

DIRECTORY = Path(__file__).resolve().parent / ".local"
TOKEN_FILE = DIRECTORY / "telegram-token"
CHALLENGE_FILE = DIRECTORY / "telegram-challenge"
ENV_FILE = DIRECTORY / "telegram.env"


def request(token, method):
    # Never print URLs, exceptions or returned messages: they may contain secrets or personal data.
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=12) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("response too large")
        result = json.loads(raw)
        if result.get("ok") is not True:
            raise ValueError("Telegram error")
        return result["result"]
    except Exception:
        raise SystemExit("Telegram API request failed; verify the token and network. No secret was printed.") from None


def read_token():
    token = TOKEN_FILE.read_text().strip()
    if not re.fullmatch(r"\d{7,12}:[A-Za-z0-9_-]{30,}", token):
        raise SystemExit("The local token file does not contain a valid token format.")
    return token


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(data)


def main():
    token = read_token()
    bot = request(token, "getMe")
    username = bot.get("username", "")
    if bot.get("is_bot") is not True or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise SystemExit("Telegram did not return a valid bot identity.")
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "challenge":
        if not CHALLENGE_FILE.exists():
            write_private(CHALLENGE_FILE, secrets.token_hex(6) + "\n")
        challenge = CHALLENGE_FILE.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{12}", challenge):
            raise SystemExit("Invalid existing challenge; no changes made.")
        print("Bot:", username)
        print("Challenge:", challenge)
    elif mode == "bind":
        if ENV_FILE.exists():
            raise SystemExit("Telegram configuration already exists; refusing to overwrite it.")
        challenge = CHALLENGE_FILE.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{12}", challenge):
            raise SystemExit("Invalid challenge; no changes made.")
        matches = set()
        for update in request(token, "getUpdates?limit=100&timeout=0"):
            message = update.get("message", {})
            chat = message.get("chat", {})
            if chat.get("type") != "private":
                continue
            if message.get("text", "").strip() not in (challenge, "/start " + challenge):
                continue
            chat_id = chat.get("id")
            if isinstance(chat_id, int) and chat_id > 0:
                matches.add(chat_id)
        if len(matches) != 1:
            raise SystemExit("No unique matching private chat found; send the challenge to the bot and retry.")
        write_private(ENV_FILE, f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={next(iter(matches))}\n")
        CHALLENGE_FILE.unlink()
        print("Telegram private chat bound to local configuration.")
    else:
        raise SystemExit("Usage: telegram_setup.py challenge | bind")


if __name__ == "__main__":
    main()
