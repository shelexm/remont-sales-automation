CREATE TABLE IF NOT EXISTS integration_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id bigint NOT NULL REFERENCES lead_events(id),
    case_id bigint NOT NULL REFERENCES lead_cases(id),
    destination text NOT NULL CHECK (destination IN ('crm', 'telegram', 'telegram_reminder')),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','failed','uncertain')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    lease_token uuid,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(event_id, destination)
);
CREATE INDEX IF NOT EXISTS integration_jobs_pending_idx ON integration_jobs (available_at, id) WHERE status IN ('pending','processing');
CREATE UNIQUE INDEX IF NOT EXISTS integration_one_active_case ON integration_jobs(case_id, destination) WHERE status = 'processing';
CREATE TABLE IF NOT EXISTS crm_entities (
    entity_type text NOT NULL,
    external_key text NOT NULL,
    create_started boolean NOT NULL DEFAULT false,
    remote_id text,
    PRIMARY KEY(entity_type, external_key)
);
CREATE TABLE IF NOT EXISTS demo_crm (
    case_id bigint PRIMARY KEY REFERENCES lead_cases(id),
    contact_id text NOT NULL,
    deal_id text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'new',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION enqueue_integrations() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'completed' AND OLD.status IS DISTINCT FROM 'completed' AND NEW.case_id IS NOT NULL THEN
        INSERT INTO integration_jobs(event_id, case_id, destination)
        VALUES (NEW.id, NEW.case_id, 'crm'), (NEW.id, NEW.case_id, 'telegram')
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS enqueue_integrations_trigger ON lead_events;
CREATE TRIGGER enqueue_integrations_trigger AFTER UPDATE OF status ON lead_events
FOR EACH ROW EXECUTE FUNCTION enqueue_integrations();
-- Existing completed events from the previous version also need delivery.
INSERT INTO integration_jobs(event_id, case_id, destination)
SELECT id, case_id, destination FROM lead_events CROSS JOIN (VALUES ('crm'), ('telegram')) AS d(destination)
WHERE status = 'completed' AND case_id IS NOT NULL ON CONFLICT DO NOTHING;

CREATE OR REPLACE VIEW demo_analytics AS
SELECT (SELECT count(*) FROM lead_events) AS received_events,
       (SELECT count(*) FROM lead_cases) AS unique_cases,
       (SELECT count(*) FROM demo_crm WHERE status = 'new') AS new_cases,
       (SELECT count(*) FROM integration_jobs WHERE status = 'failed') AS failed_jobs,
       (SELECT count(*) FROM integration_jobs WHERE status = 'uncertain') AS uncertain_jobs;
