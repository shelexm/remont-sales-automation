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
