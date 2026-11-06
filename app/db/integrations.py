"""Persistence for integration jobs and demo CRM records."""
import psycopg
from psycopg.types.json import Jsonb

from app.integrations.providers import CRMResult

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
