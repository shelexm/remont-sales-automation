"""Provider boundary. This module never logs credentials or remote responses."""
import json
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(NoRedirect())


def _open(request, timeout):
    return _opener.open(request, timeout=timeout)


class Retryable(Exception):
    def __init__(self, code="unavailable", delay=60):
        super().__init__(code)
        self.code, self.delay = code, max(1, min(int(delay), 86400))


class Uncertain(Exception):
    pass


class Permanent(Exception):
    pass


@dataclass(frozen=True)
class CRMResult:
    contact_id: str
    deal_id: str


class CRMProvider(Protocol):
    def sync(self, case_id: int, lead: dict) -> CRMResult: ...


def request_json(method, url, payload=None, headers=None, side_effect=False):
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode()
    try:
        with _open(Request(url, data=data, headers=request_headers, method=method), timeout=8) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("response_too_large")
            return json.loads(body)
    except HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After", "60")
            try:
                parsed = json.loads(exc.read(4096))
                retry_after = parsed.get("parameters", {}).get("retry_after", retry_after)
            except (ValueError, TypeError, AttributeError):
                pass
            try:
                raise Retryable("rate_limited", int(retry_after)) from None
            except (ValueError, TypeError):
                raise Retryable("rate_limited") from None
        if exc.code >= 500:
            if side_effect:
                raise Uncertain("remote_result_unknown") from None
            raise Retryable("remote_unavailable") from None
        raise Permanent("remote_rejected") from None
    except (URLError, TimeoutError, socket.timeout, ConnectionError, ValueError, json.JSONDecodeError):
        if side_effect:
            raise Uncertain("remote_result_unknown") from None
        raise Retryable("remote_unavailable") from None


class EspoCRM:
    """Uses externally stored creation journal; cAutomationKey is required in EspoCRM."""
    def __init__(self, base_url, api_key, journal, transport=request_json):
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("ESPO_URL must be an HTTPS URL without credentials")
        if not api_key:
            raise ValueError("ESPO_API_KEY is required")
        self.root, self.api_key, self.journal, self.transport = base_url.rstrip("/") + "/api/v1/", api_key, journal, transport

    def _call(self, method, path, payload=None, side_effect=False):
        return self.transport(method, self.root + path, payload, {"X-Api-Key": self.api_key}, side_effect)

    def _sync_entity(self, entity, key, fields):
        params = urlencode({"where[0][type]": "equals", "where[0][attribute]": "cAutomationKey", "where[0][value]": key, "maxSize": "2"})
        result = self._call("GET", entity + "?" + params)
        matches = result.get("list") if isinstance(result, dict) else None
        if not isinstance(matches, list):
            raise Permanent("invalid_crm_response")
        if len(matches) > 1:
            raise Uncertain("multiple_crm_matches")
        if matches:
            remote_id = matches[0].get("id")
            if not isinstance(remote_id, str) or not remote_id.isalnum():
                raise Permanent("invalid_crm_id")
            self.journal.found(entity, key, remote_id)
            self._call("PUT", f"{entity}/{remote_id}", fields, side_effect=False)
            return remote_id
        if not self.journal.begin_create(entity, key):
            raise Uncertain("creation_previously_attempted")
        try:
            created = self._call("POST", entity, {**fields, "cAutomationKey": key}, side_effect=True)
        except Retryable:
            self.journal.reset_create(entity, key)
            raise
        except Permanent:
            self.journal.reset_create(entity, key)
            raise
        remote_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(remote_id, str) or not remote_id.isalnum():
            raise Uncertain("invalid_crm_create_response")
        self.journal.found(entity, key, remote_id)
        return remote_id

    def get_stage(self, remote_id):
        if not isinstance(remote_id, str) or not remote_id.isalnum():
            raise Permanent("invalid_crm_id")
        result = self._call("GET", f"Opportunity/{remote_id}")
        stage = result.get("stage") if isinstance(result, dict) else None
        if not isinstance(stage, str) or not stage:
            raise Permanent("invalid_crm_stage")
        return stage

    def sync(self, case_id, lead):
        # A case-scoped contact avoids putting phone numbers in correlation keys.
        key = f"remont-case-{case_id}"
        contact = self._sync_entity("Contact", key, {"lastName": lead["name"], "phoneNumber": lead["phone"]})
        deal = self._sync_entity("Opportunity", key, {"name": f"Ремонт #{case_id}", "amount": lead["budget"], "contactsIds": [contact]})
        return CRMResult(contact, deal)


class Telegram:
    def __init__(self, bot_token, chat_id, transport=request_json):
        if not bot_token or not str(chat_id):
            raise ValueError("Telegram token and chat id are required")
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id, self.transport = str(chat_id), transport

    def notify(self, case_id, lead):
        # Do not send personal contact details to chat by default.
        text = f"Новая заявка #{case_id}\nУслуга: {lead['service']}\nБюджет: {lead['budget_class']}"
        result = self.transport("POST", self.url, {"chat_id": self.chat_id, "text": text}, {}, True)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise Uncertain("telegram_invalid_response")
        return result.get("result", {}).get("message_id")

    def remind(self, case_id):
        result = self.transport("POST", self.url, {"chat_id": self.chat_id,
                                "text": f"Напоминание: заявка #{case_id} ожидает обработки"}, {}, True)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise Uncertain("telegram_invalid_response")
        return result.get("result", {}).get("message_id")
