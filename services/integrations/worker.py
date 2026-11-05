"""Delivery worker. Operates only on an explicitly configured PostgreSQL database."""
import json
import os
import time
import uuid

import psycopg
from psycopg.types.json import Jsonb

from providers import CRMResult, EspoCRM, Permanent, Retryable, Telegram, Uncertain


class Journal:
    def __init__(self, dsn):
        self.dsn = dsn

    def begin_create(self, entity, key):
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            row = conn.execute("""INSERT INTO crm_entities(entity_type, external_key, create_started)
                VALUES (%s, %s, true) ON CONFLICT (entity_type, external_key) DO UPDATE
                SET create_started = true WHERE NOT crm_entities.create_started AND crm_entities.remote_id IS NULL
                RETURNING external_key""", (entity, key)).fetchone()
            return row is not None

    def found(self, entity, key, remote_id):
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            conn.execute("""INSERT INTO crm_entities(entity_type, external_key, create_started, remote_id)
                VALUES (%s, %s, true, %s) ON CONFLICT (entity_type, external_key)
                DO UPDATE SET remote_id = EXCLUDED.remote_id, create_started = true""", (entity, key, remote_id))

    def reset_create(self, entity, key):
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            conn.execute("UPDATE crm_entities SET create_started = false WHERE entity_type = %s AND external_key = %s AND remote_id IS NULL", (entity, key))


class DemoCRM:
    def __init__(self, dsn):
        self.dsn = dsn

    def sync(self, case_id, lead):
        contact_id, deal_id = f"demo-contact-{case_id}", f"demo-deal-{case_id}"
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            conn.execute("""INSERT INTO demo_crm(case_id, contact_id, deal_id, payload)
                VALUES (%s, %s, %s, %s) ON CONFLICT (case_id)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()""", (case_id, contact_id, deal_id, Jsonb(lead)))
        return CRMResult(contact_id, deal_id)


def claim(dsn, destinations, token):
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        # A lost Telegram response cannot be distinguished from a delivered message.
        conn.execute("""UPDATE integration_jobs SET status = 'uncertain', last_error = 'expired_delivery',
            lease_until = NULL, lease_token = NULL, updated_at = now()
            WHERE destination IN ('telegram','telegram_reminder') AND status = 'processing' AND lease_until <= now()""")
        conn.execute("""UPDATE integration_jobs SET status = 'failed', last_error = 'attempt_limit',
            lease_until = NULL, lease_token = NULL, updated_at = now()
            WHERE destination = 'crm' AND status = 'processing' AND lease_until <= now() AND attempts >= 5""")
        row = conn.execute("""WITH candidate AS (
                SELECT j.id FROM integration_jobs j
                WHERE j.destination = ANY(%s) AND j.attempts < 5
                  AND ((j.status = 'pending' AND j.available_at <= now())
                       OR (j.destination = 'crm' AND j.status = 'processing' AND j.lease_until <= now()))
                  AND NOT EXISTS (SELECT 1 FROM integration_jobs other
                    WHERE other.case_id = j.case_id AND other.destination = j.destination
                      AND other.id <> j.id AND other.status = 'processing')
                ORDER BY j.id FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE integration_jobs j SET status = 'processing', attempts = j.attempts + 1,
                lease_token = %s, lease_until = now() + interval '120 seconds', updated_at = now()
              FROM candidate c WHERE j.id = c.id RETURNING j.id, j.case_id, j.destination, j.attempts""",
                           (destinations, token)).fetchone()
        if not row:
            return None
        lead = conn.execute("""SELECT e.payload FROM lead_cases c JOIN lead_events e ON e.id = c.last_event_id
                             WHERE c.id = %s""", (row[1],)).fetchone()
        if not lead:
            raise RuntimeError("missing_case")
        return (*row, lead[0])


def finish(dsn, job_id, token, status, error=None, delay=60):
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        result = conn.execute("""UPDATE integration_jobs SET status = %s, last_error = %s,
            available_at = now() + make_interval(secs => %s), lease_token = NULL, lease_until = NULL, updated_at = now()
            WHERE id = %s AND status = 'processing' AND lease_token = %s AND lease_until > now()""",
                              (status, error, delay, job_id, token))
        return result.rowcount == 1


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
            if isinstance(crm, EspoCRM):
                with psycopg.connect(dsn, connect_timeout=3) as conn:
                    conn.execute("""INSERT INTO crm_tracking(case_id, remote_id) VALUES (%s, %s)
                        ON CONFLICT (case_id) DO UPDATE SET remote_id = EXCLUDED.remote_id,
                        next_check_at = now()""", (case_id, result.deal_id))
        elif destination == "telegram_reminder":
            if isinstance(crm, EspoCRM):
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
    elif provider == "espocrm":
        crm = EspoCRM(os.environ["ESPO_URL"], os.environ["ESPO_API_KEY"], Journal(dsn))
    else:
        raise ValueError("Unsupported CRM_PROVIDER")
    telegram = None
    if os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true":
        telegram = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    while True:
        try:
            unprocessed_stage = os.environ.get("ESPO_UNPROCESSED_STAGE", "Prospecting")
            if telegram:
                if provider == "espocrm":
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
