CREATE TABLE IF NOT EXISTS lead_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id text NOT NULL UNIQUE,
    payload_hash char(64) NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    lease_token uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_events_queue_idx ON lead_events (available_at, id) WHERE status IN ('pending', 'processing');

-- Caller generates a new token for every claim. Fencing rejects stale completions.
CREATE OR REPLACE FUNCTION claim_lead(p_token uuid, p_lease_seconds integer DEFAULT 60, p_max_attempts integer DEFAULT 5)
RETURNS SETOF lead_events LANGUAGE plpgsql AS $$
BEGIN
    IF p_token IS NULL OR p_lease_seconds NOT BETWEEN 1 AND 3600 OR p_max_attempts NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'invalid lease';
    END IF;
    UPDATE lead_events SET status = 'failed', lease_until = NULL, lease_token = NULL, updated_at = now()
    WHERE status = 'processing' AND lease_until <= now() AND attempts >= p_max_attempts;
    RETURN QUERY
    WITH candidate AS (
        SELECT id FROM lead_events
        WHERE attempts < p_max_attempts AND ((status = 'pending' AND available_at <= now())
           OR (status = 'processing' AND lease_until <= now()))
        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
    )
    UPDATE lead_events e SET status = 'processing', attempts = e.attempts + 1,
        lease_token = p_token, lease_until = now() + make_interval(secs => p_lease_seconds), updated_at = now()
    FROM candidate c WHERE e.id = c.id RETURNING e.*;
END $$;

CREATE OR REPLACE FUNCTION finish_lead(p_id bigint, p_token uuid, p_ok boolean,
                                      p_max_attempts integer DEFAULT 5, p_delay_seconds integer DEFAULT 60)
RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE changed integer;
BEGIN
    IF p_token IS NULL OR p_ok IS NULL OR p_max_attempts NOT BETWEEN 1 AND 100 OR p_delay_seconds NOT BETWEEN 0 AND 86400 THEN
        RAISE EXCEPTION 'invalid completion arguments';
    END IF;
    UPDATE lead_events SET
        status = CASE WHEN p_ok THEN 'completed' WHEN attempts >= p_max_attempts THEN 'failed' ELSE 'pending' END,
        available_at = CASE WHEN p_ok THEN available_at ELSE now() + make_interval(secs => p_delay_seconds) END,
        lease_until = NULL, lease_token = NULL, updated_at = now()
    WHERE id = p_id AND status = 'processing' AND lease_token = p_token AND lease_until > now();
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END $$;

CREATE TABLE IF NOT EXISTS lead_cases (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    phone text NOT NULL,
    service text NOT NULL,
    first_event_id bigint NOT NULL REFERENCES lead_events(id),
    last_event_id bigint NOT NULL REFERENCES lead_events(id),
    occurrences integer NOT NULL DEFAULT 1,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_cases_dedup_idx ON lead_cases (phone, service, last_seen_at DESC);
ALTER TABLE lead_events ADD COLUMN IF NOT EXISTS case_id bigint REFERENCES lead_cases(id);

-- Serialise matching phone/service while determining whether this is a recent repeat.
CREATE OR REPLACE FUNCTION process_lead(p_id bigint, p_token uuid)
RETURNS TABLE(event_id bigint, result_case_id bigint, duplicate boolean)
LANGUAGE plpgsql AS $$
DECLARE e lead_events%ROWTYPE; existing_id bigint;
BEGIN
    SELECT * INTO e FROM lead_events WHERE id = p_id AND status = 'processing'
        AND lease_token = p_token AND lease_until > now() FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended((e.payload->>'phone') || ':' || (e.payload->>'service'), 0));
    SELECT id INTO existing_id FROM lead_cases
    WHERE phone = e.payload->>'phone' AND service = e.payload->>'service'
      AND last_seen_at >= now() - interval '24 hours'
    ORDER BY last_seen_at DESC, id DESC LIMIT 1 FOR UPDATE;
    IF existing_id IS NULL THEN
        INSERT INTO lead_cases(phone, service, first_event_id, last_event_id)
        VALUES (e.payload->>'phone', e.payload->>'service', e.id, e.id) RETURNING id INTO existing_id;
        duplicate := false;
    ELSE
        UPDATE lead_cases SET last_event_id = e.id, occurrences = occurrences + 1, last_seen_at = now()
        WHERE id = existing_id;
        duplicate := true;
    END IF;
    UPDATE lead_events SET status = 'completed', case_id = existing_id,
        lease_until = NULL, lease_token = NULL, updated_at = now() WHERE id = e.id;
    event_id := e.id;
    result_case_id := existing_id;
    RETURN NEXT;
END $$;
