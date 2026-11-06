"""Validation and canonical identity for incoming leads."""
import hashlib
import json
import re

SERVICES = {"cosmetic", "capital", "design", "other"}
SERVICE_LABELS = {"Косметический ремонт": "cosmetic", "Капитальный ремонт": "capital",
                  "Ремонт по дизайн-проекту": "design", "Другое": "other"}


class InvalidLead(ValueError):
    pass


def validate(data, threshold):
    def field(name, limit):
        value = data.get(name, "")
        if not isinstance(value, str):
            raise InvalidLead(name)
        value = value.strip()
        if not value or len(value) > limit:
            raise InvalidLead(name)
        return value

    source_id = field("tranid", 128)
    name = field("Name", 120)
    raw_phone = field("Phone", 40)
    if re.search(r"[^0-9+()\-\s]", raw_phone) or raw_phone.count("+") > 1 or ("+" in raw_phone and not raw_phone.startswith("+")):
        raise InvalidLead("Phone")
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or digits[0] != "7" or digits[1] not in "3489":
        raise InvalidLead("Phone")
    service = field("Service", 64)
    service = SERVICE_LABELS.get(service, service)
    if service not in SERVICES:
        raise InvalidLead("Service")
    try:
        area = int(field("Area", 5))
        budget = int(field("Budget", 12))
    except ValueError as exc:
        raise InvalidLead("Area or Budget") from exc
    if not 1 <= area <= 10000 or not 1 <= budget <= 1000000000:
        raise InvalidLead("Area or Budget")
    if field("Consent", 8).lower() not in {"yes", "1", "true", "on"}:
        raise InvalidLead("Consent")
    return {"source_id": source_id, "name": name, "phone": "+" + digits,
            "service": service, "area": area, "budget": budget,
            "budget_class": "high" if budget >= threshold else "low"}


def fingerprint(lead):
    source_fields = {key: value for key, value in lead.items() if key != "budget_class"}
    body = json.dumps(source_fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()
