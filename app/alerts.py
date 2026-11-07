"""Transition-based, at-most-once Telegram alerts with no lead data."""
import json
import os
from pathlib import Path
import uuid

from app.integrations.providers import request_json

LABELS = {
    "receiver": "доступность приёмника",
    "tunnel": "доступность публичного туннеля",
    "queue.database": "проверка базы очередей",
    "queue.events": "очередь заявок",
    "queue.crm": "очередь CRM",
    "queue.telegram": "очередь уведомлений Telegram",
    "queue.telegram_reminder": "очередь напоминаний Telegram",
}
QUEUE_KEYS = set(LABELS) - {"receiver", "tunnel", "queue.database"}


def issues_from_report(report, active):
    issues = set()
    if report.get("receiver") != "ok":
        issues.add("receiver")
    if report.get("public_tunnel") != "ok":
        issues.add("tunnel")
    queues = report.get("queues", {})
    if queues.get("status") == "unavailable":
        # Unknown queue state cannot prove that previous queue incidents were resolved.
        return issues | {"queue.database"} | (set(active) & QUEUE_KEYS)
    bad = ("overdue", "expired_lease", "failed", "uncertain")
    if any(queues.get("events", {}).get(key, 0) for key in bad):
        issues.add("queue.events")
    for destination in ("crm", "telegram", "telegram_reminder"):
        values = queues.get("integrations", {}).get(destination, {})
        if any(values.get(key, 0) for key in bad):
            issues.add("queue." + destination)
    return issues


def read_state(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or any(k not in LABELS or v not in ("attempted", "sent") for k, v in data.items()):
        raise ValueError("Invalid alert state")
    return data


def save_state(path, state):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def telegram_send(token, chat_id, text):
    result = request_json("POST", f"https://api.telegram.org/bot{token}/sendMessage",
                          {"chat_id": str(chat_id), "text": text}, {}, True)
    return isinstance(result, dict) and result.get("ok") is True


def dispatch_changes(path, state, issues, send):
    new = issues - set(state)
    resolved = set(state) - issues
    resolved_notified = {key for key in resolved if state[key] == "sent"}
    if not new and not resolved:
        return state, "unchanged"
    next_state = {key: value for key, value in state.items() if key not in resolved}
    next_state.update({key: "attempted" for key in new})
    # Record attempts before sending: a crash or unknown network result must not cause blind retries.
    save_state(path, next_state)
    state.clear()
    state.update(next_state)
    lines = ["Мониторинг «Линия дома»:"]
    lines += ["Проблема: " + LABELS[key] for key in sorted(new)]
    lines += ["Восстановлено: " + LABELS[key] for key in sorted(resolved_notified)]
    if not new and not resolved_notified:
        return state, "unchanged"
    try:
        confirmed = send("\n".join(lines))
    except Exception:
        confirmed = False
    if not confirmed:
        return state, "unconfirmed"
    next_state.update({key: "sent" for key in new})
    save_state(path, next_state)
    state.clear()
    state.update(next_state)
    return state, "sent"


class StableAlerts:
    def __init__(self, path, send):
        self.path, self.send = Path(path), send
        self.active = read_state(self.path)
        self.observations = {}

    def observe(self, report):
        issues = issues_from_report(report, self.active)
        stable = set(self.active)
        waiting = False
        for key in LABELS:
            current = key in issues
            previous, count = self.observations.get(key, (None, 0))
            count = min(count + 1, 2) if previous == current else 1
            self.observations[key] = (current, count)
            if count >= 2:
                if current:
                    stable.add(key)
                else:
                    stable.discard(key)
            elif current != (key in self.active):
                waiting = True
        if stable != set(self.active):
            self.active, outcome = dispatch_changes(self.path, self.active, stable, self.send)
            return outcome
        return "waiting" if waiting else "unchanged"
