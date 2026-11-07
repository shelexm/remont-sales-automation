"""Read-only, aggregate queue diagnostics for the local demonstration."""
import argparse
import json
import os
import sys

import psycopg


def queue_report(dsn, overdue_minutes=5):
    """Inspect queues in a read-only transaction, without returning lead data."""
    if not 1 <= overdue_minutes <= 1440:
        raise ValueError("overdue_minutes must be between 1 and 1440")
    query = """
        SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
               count(*) FILTER (WHERE status = 'pending' AND available_at < now() - (%s * interval '1 minute')) AS overdue,
               count(*) FILTER (WHERE status = 'processing') AS processing,
               count(*) FILTER (WHERE status = 'processing' AND lease_until < now()) AS expired_lease,
               count(*) FILTER (WHERE status = 'failed') AS failed
        FROM lead_events
    """
    jobs_query = """
        SELECT destination,
               count(*) FILTER (WHERE status = 'pending') AS pending,
               count(*) FILTER (WHERE status = 'pending' AND available_at < now() - (%s * interval '1 minute')) AS overdue,
               count(*) FILTER (WHERE status = 'processing') AS processing,
               count(*) FILTER (WHERE status = 'processing' AND lease_until < now()) AS expired_lease,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'uncertain') AS uncertain
        FROM integration_jobs GROUP BY destination ORDER BY destination
    """
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '3s'")
            event_row = conn.execute(query, (overdue_minutes,)).fetchone()
            job_rows = conn.execute(jobs_query, (overdue_minutes,)).fetchall()
    event_keys = ("pending", "overdue", "processing", "expired_lease", "failed")
    job_keys = (*event_keys, "uncertain")
    events = dict(zip(event_keys, event_row, strict=True))
    jobs = {row[0]: dict(zip(job_keys, row[1:], strict=True)) for row in job_rows}
    issue_keys = ("overdue", "expired_lease", "failed", "uncertain")
    needs_attention = any(events.get(key, 0) for key in issue_keys) or any(
        values.get(key, 0) for values in jobs.values() for key in issue_keys
    )
    return {"status": "attention" if needs_attention else "ok", "overdue_minutes": overdue_minutes,
            "events": events, "integrations": jobs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overdue-minutes", type=int, default=5, help="Minutes past scheduled availability (1–1440)")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not configured", file=sys.stderr)
        return 2
    try:
        report = queue_report(dsn, args.overdue_minutes)
    except (psycopg.Error, ValueError):
        print("Queue diagnostics failed; check database connectivity, schema and threshold", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["status"] == "attention" else 0


if __name__ == "__main__":
    sys.exit(main())
