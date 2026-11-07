"""Experimental Bitrix24 CRM adapter; not yet wired into the delivery worker."""
import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from app.integrations.providers import CRMResult, NoRedirect, Permanent, Retryable, Uncertain

_opener = build_opener(NoRedirect())
_rate_codes = {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"}


def _api_error(code, side_effect):
    if isinstance(code, str) and code in _rate_codes:
        raise Retryable("bitrix_rate_limited", 60)
    if side_effect:
        raise Uncertain("bitrix_result_unknown")
    if code in ("INTERNAL_SERVER_ERROR", "ERROR_UNEXPECTED_ANSWER", "OVERLOAD_LIMIT"):
        raise Retryable("bitrix_unavailable", 60)
    raise Permanent("bitrix_rejected")


def bitrix_transport(method, url, payload, headers, side_effect):
    """Suppress URLs and error bodies: an inbound webhook URL contains a secret."""
    data = json.dumps(payload, ensure_ascii=False).encode()
    request = Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"},
                      method=method)
    try:
        with _opener.open(request, timeout=8) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("response_too_large")
            result = json.loads(body)
    except HTTPError as error:
        try:
            code = json.loads(error.read(4096)).get("error")
        except (ValueError, AttributeError):
            code = None
        finally:
            error.close()
        if isinstance(code, str) and code in _rate_codes:
            raise Retryable("bitrix_rate_limited", 60) from None
        if error.code >= 500:
            if side_effect:
                raise Uncertain("bitrix_result_unknown") from None
            raise Retryable("bitrix_unavailable", 60) from None
        if side_effect:
            raise Uncertain("bitrix_rejected_after_write") from None
        raise Permanent("bitrix_rejected") from None
    except (URLError, TimeoutError, OSError, ValueError):
        if side_effect:
            raise Uncertain("bitrix_result_unknown") from None
        raise Retryable("bitrix_unavailable", 60) from None
    if not isinstance(result, dict):
        if side_effect:
            raise Uncertain("bitrix_invalid_response")
        raise Permanent("bitrix_invalid_response")
    if "error" in result:
        _api_error(result.get("error"), side_effect)
    return result


def _id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise Uncertain("bitrix_invalid_id")
    return str(value)


def _phone(value):
    if not isinstance(value, str) or re.search(r"[^0-9+()\-\s]", value):
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits if len(digits) == 11 and digits.startswith("7") else None


class BitrixCRM:
    """Use installation-scoped custom keys and journal; callers must isolate provider state."""
    def __init__(self, webhook_url, contact_field, deal_field, installation_id, journal, transport=bitrix_transport):
        try:
            parts = urlsplit(webhook_url)
            port = parts.port
        except (TypeError, ValueError):
            raise ValueError("Invalid Bitrix24 webhook URL") from None
        path = parts.path.strip("/").split("/")
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query
                or parts.fragment or len(path) != 3 or path[0] != "rest" or not path[1].isdigit()
                or not re.fullmatch(r"[A-Za-z0-9]{8,128}", path[2])):
            raise ValueError("Invalid Bitrix24 webhook URL")
        if not all(isinstance(field, str) and re.fullmatch(r"UF_CRM_[A-Z0-9_]{1,64}", field)
                   for field in (contact_field, deal_field)):
            raise ValueError("Bitrix24 custom field codes must start with UF_CRM_")
        if not isinstance(installation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", installation_id):
            raise ValueError("A unique installation ID is required")
        self.root = webhook_url.rstrip("/") + "/"
        namespace = hashlib.sha256(f"{parts.hostname}:{port or 443}:{installation_id}".encode()).hexdigest()[:20]
        self.prefix = "remont-" + installation_id + "-case-"
        self.entities = {"contact": "bitrix-contact-" + namespace, "deal": "bitrix-deal-" + namespace}
        self.fields = {"contact": contact_field, "deal": deal_field}
        self.journal, self.transport, self.verified_fields = journal, transport, False

    def _call(self, method, params=None, side_effect=False):
        result = self.transport("POST", self.root + method, params or {}, {}, side_effect)
        if not isinstance(result, dict):
            raise Uncertain("bitrix_invalid_response") if side_effect else Permanent("bitrix_invalid_response")
        if "error" in result:
            _api_error(result.get("error"), side_effect)
        if "result" not in result:
            raise Uncertain("bitrix_invalid_response") if side_effect else Permanent("bitrix_invalid_response")
        return result

    def _check_fields(self):
        if self.verified_fields:
            return
        for entity in ("contact", "deal"):
            fields = self._call(f"crm.{entity}.fields")["result"]
            info = fields.get(self.fields[entity]) if isinstance(fields, dict) else None
            if (not isinstance(info, dict) or info.get("type") != "string"
                    or info.get("isReadOnly") is not False or info.get("isMultiple") is not False
                    or info.get("isImmutable") is not False):
                raise Permanent("bitrix_custom_field_invalid")
        self.verified_fields = True

    def _find(self, entity, filters, selected):
        response = self._call(f"crm.{entity}.list", {"filter": filters, "select": selected})
        rows, total = response["result"], response.get("total")
        if not isinstance(rows, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise Permanent("bitrix_invalid_list")
        if total > 1 or len(rows) > 1 or "next" in response:
            raise Uncertain("bitrix_multiple_matches")
        if total != len(rows) or (rows and not isinstance(rows[0], dict)):
            raise Uncertain("bitrix_incomplete_list")
        return rows[0] if rows else None

    def _find_key(self, entity, key):
        field = self.fields[entity]
        found = self._find(entity, {"=" + field: key}, ["ID", field])
        if found and found.get(field) != key:
            raise Uncertain("bitrix_key_mismatch")
        return _id(found.get("ID")) if found else None

    def _find_phone(self, phone):
        found = self._find("contact", {"=PHONE": phone}, ["ID", "PHONE"])
        if not found:
            return None
        phones = found.get("PHONE")
        if not isinstance(phones, list) or not any(isinstance(p, dict) and _phone(p.get("VALUE")) == phone for p in phones):
            raise Uncertain("bitrix_phone_mismatch")
        return _id(found.get("ID"))

    def _create(self, entity, key, fields):
        journal_type = self.entities[entity]
        if not self.journal.begin_create(journal_type, key):
            raise Uncertain("bitrix_creation_previously_attempted")
        try:
            response = self._call(f"crm.{entity}.add", {"fields": fields}, side_effect=True)
        except Retryable:
            self.journal.reset_create(journal_type, key)
            raise
        except Permanent:
            self.journal.reset_create(journal_type, key)
            raise
        remote_id = _id(response["result"])
        self.journal.found(journal_type, key, remote_id)
        # Do not trust a successful create if the unique recovery field was silently ignored.
        fetched = self._call(f"crm.{entity}.get", {"id": remote_id})["result"]
        if not isinstance(fetched, dict) or _id(fetched.get("ID")) != remote_id or fetched.get(self.fields[entity]) != key:
            raise Uncertain("bitrix_created_key_unverified")
        return remote_id

    def sync(self, case_id, lead):
        if isinstance(case_id, bool) or not isinstance(case_id, int) or case_id <= 0:
            raise ValueError("Invalid case ID")
        self._check_fields()
        key = self.prefix + str(case_id)
        contact = self._find_key("contact", key)
        if not contact:
            contact = self._find_phone(lead["phone"])
        if contact:
            self.journal.found(self.entities["contact"], key, contact)
        else:
            contact = self._create("contact", key, {"NAME": lead["name"], "PHONE": [{"VALUE": lead["phone"],
                                                                                         "VALUE_TYPE": "WORK"}],
                                                       self.fields["contact"]: key})
        deal = self._find_key("deal", key)
        fields = {"TITLE": f"Ремонт #{case_id}", "OPPORTUNITY": lead["budget"], "CURRENCY_ID": "RUB",
                  "CONTACT_IDS": [int(contact)], self.fields["deal"]: key}
        if deal:
            self.journal.found(self.entities["deal"], key, deal)
            updated = self._call("crm.deal.update", {"id": deal, "fields": fields}, side_effect=True)["result"]
            if updated is not True:
                raise Uncertain("bitrix_update_unverified")
        else:
            deal = self._create("deal", key, fields)
        return CRMResult(contact, deal)

    def get_stage(self, deal_id):
        try:
            remote_id = _id(deal_id)
        except Uncertain:
            raise Permanent("bitrix_invalid_id") from None
        data = self._call("crm.deal.get", {"id": remote_id})["result"]
        try:
            matches_id = isinstance(data, dict) and _id(data.get("ID")) == remote_id
        except Uncertain:
            matches_id = False
        if not matches_id or not isinstance(data.get("STAGE_ID"), str) or not data["STAGE_ID"]:
            raise Permanent("bitrix_invalid_stage")
        return data["STAGE_ID"]
