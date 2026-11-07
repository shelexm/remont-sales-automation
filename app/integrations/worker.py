"""Delivery worker. Operates only on an explicitly configured PostgreSQL database."""
import json
import hashlib
import os
import time
import uuid

import psycopg

from app.integrations.providers import EspoCRM, Permanent, Retryable, Telegram, Uncertain
from app.integrations.bitrix import BitrixCRM
from app.db.integrations import DemoCRM, Journal, claim, ensure_provider_binding, finish


def deliver_once(dsn, crm=None, telegram=None, unprocessed_stage="Prospecting"):
    destinations = (["crm"] if crm else []) + (["telegram", "telegram_reminder"] if telegram else [])
    if not destinations:
        return False
    token = uuid.uuid4()
    job = claim(dsn, destinations, token)
    if not job:
        return False
    job_id, case_id, destination, attempts, lead = job
    try:
        if destination == "crm":
            result = crm.sync(case_id, lead)
            if isinstance(crm, (EspoCRM, BitrixCRM)):
                with psycopg.connect(dsn, connect_timeout=3) as conn:
                    conn.execute("""INSERT INTO crm_tracking(case_id, remote_id) VALUES (%s, %s)
                        ON CONFLICT (case_id) DO UPDATE SET remote_id = EXCLUDED.remote_id,
                        next_check_at = now()""", (case_id, result.deal_id))
        elif destination == "telegram_reminder":
            if isinstance(crm, (EspoCRM, BitrixCRM)):
                stage = refresh_stage(dsn, crm, case_id)
                should_remind = stage == unprocessed_stage
            else:
                with psycopg.connect(dsn, connect_timeout=3) as conn:
                    row = conn.execute("SELECT status FROM demo_crm WHERE case_id = %s", (case_id,)).fetchone()
                should_remind = bool(row and row[0] == "new")
            if should_remind:
                telegram.remind(case_id)
        else:
            telegram.notify(case_id, lead)
    except Retryable as exc:
        finish(dsn, job_id, token, "failed" if attempts >= 5 else "pending", exc.code, max(exc.delay, min(3600, 2 ** attempts * 10)))
    except Uncertain:
        finish(dsn, job_id, token, "uncertain", "remote_result_unknown")
    except Permanent:
        finish(dsn, job_id, token, "failed", "remote_rejected")
    except Exception:
        # May have failed after a remote side effect: never resend blindly.
        finish(dsn, job_id, token, "uncertain", "internal_error")
    else:
        finish(dsn, job_id, token, "done")
    return True


def refresh_stage(dsn, crm, case_id):
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        row = conn.execute("SELECT remote_id FROM crm_tracking WHERE case_id = %s", (case_id,)).fetchone()
    if not row:
        raise Retryable("crm_status_unavailable")
    stage = crm.get_stage(row[0])
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        conn.execute("UPDATE crm_tracking SET stage = %s, checked_at = now(), next_check_at = now() + interval '5 minutes' WHERE case_id = %s", (stage, case_id))
    return stage


def refresh_one_due(dsn, crm):
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        row = conn.execute("""UPDATE crm_tracking SET next_check_at = now() + interval '5 minutes'
            WHERE case_id = (SELECT case_id FROM crm_tracking WHERE next_check_at <= now()
            ORDER BY next_check_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING case_id""").fetchone()
    if row:
        try:
            refresh_stage(dsn, crm, row[0])
        except (Retryable, Permanent):
            # No stale status is treated as proof that a reminder is due.
            pass


def enqueue_reminders(dsn, minutes, provider="demo", unprocessed_stage="Prospecting"):
    if not 1 <= minutes <= 10080:
        raise ValueError("FOLLOWUP_MINUTES must be between 1 and 10080")
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        if provider == "demo":
            conn.execute("""INSERT INTO integration_jobs(event_id, case_id, destination)
                SELECT c.first_event_id, c.id, 'telegram_reminder' FROM lead_cases c
                JOIN demo_crm d ON d.case_id = c.id
                WHERE d.status = 'new' AND c.first_seen_at <= now() - make_interval(mins => %s)
                ON CONFLICT (event_id, destination) DO NOTHING""", (minutes,))
        else:
            conn.execute("""INSERT INTO integration_jobs(event_id, case_id, destination)
                SELECT c.first_event_id, c.id, 'telegram_reminder' FROM lead_cases c
                JOIN crm_tracking t ON t.case_id = c.id
                WHERE t.stage = %s AND t.checked_at >= now() - interval '10 minutes'
                  AND c.first_seen_at <= now() - make_interval(mins => %s)
                ON CONFLICT (event_id, destination) DO NOTHING""", (unprocessed_stage, minutes))


def main():
    dsn = os.environ["DATABASE_URL"]
    provider = os.environ.get("CRM_PROVIDER", "demo")
    if provider == "demo":
        crm = DemoCRM(dsn)
        identity = "local"
    elif provider == "espocrm":
        crm = EspoCRM(os.environ["ESPO_URL"], os.environ["ESPO_API_KEY"], Journal(dsn), os.environ["ESPO_ASSIGNED_USER_ID"])
        identity = hashlib.sha256(crm.root.encode()).hexdigest()
    elif provider == "bitrix24":
        crm = BitrixCRM(os.environ["BITRIX_WEBHOOK_URL"], os.environ["BITRIX_CONTACT_FIELD"],
                        os.environ["BITRIX_DEAL_FIELD"], os.environ["BITRIX_INSTALLATION_ID"], Journal(dsn))
        identity = crm.entities["deal"]
    else:
        raise ValueError("Unsupported CRM_PROVIDER")
    telegram = None
    if os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true":
        telegram = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    if provider == "bitrix24" and telegram and not os.environ.get("BITRIX_UNPROCESSED_STAGE"):
        raise ValueError("BITRIX_UNPROCESSED_STAGE is required when Telegram reminders are enabled")
    ensure_provider_binding(dsn, provider, identity,
                            adopt_legacy=os.environ.get("CRM_ADOPT_EXISTING", "false").lower() == "true",
                            adopt_mixed=os.environ.get("CRM_ADOPT_MIXED_HISTORY", "false").lower() == "true")
    while True:
        try:
            unprocessed_stage = (os.environ.get("BITRIX_UNPROCESSED_STAGE", "") if provider == "bitrix24"
                                 else os.environ.get("ESPO_UNPROCESSED_STAGE", "Prospecting"))
            if telegram:
                if provider in ("espocrm", "bitrix24"):
                    refresh_one_due(dsn, crm)
                enqueue_reminders(dsn, int(os.environ.get("FOLLOWUP_MINUTES", "60")), provider, unprocessed_stage)
            if not deliver_once(dsn, crm, telegram, unprocessed_stage):
                time.sleep(3)
        except psycopg.Error:
            # Technical code only; never print exception details or personal data.
            print(json.dumps({"event": "database_unavailable"}), flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
