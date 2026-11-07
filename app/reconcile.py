"""Inspect ambiguous deliveries and record independently confirmed Telegram messages."""
import argparse
import json
import os
import re
import sys

import psycopg


def list_uncertain(dsn):
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '3s'")
        rows = conn.execute("""SELECT id, case_id, destination, attempts, created_at
            FROM integration_jobs WHERE status = 'uncertain' ORDER BY id""").fetchall()
    return [{"job_id": row[0], "case_id": row[1], "destination": row[2],
             "attempts": row[3], "created_at": row[4].isoformat()} for row in rows]


def confirm_telegram(dsn, job_id, operator):
    """Record a human's direct chat verification and mark this one job delivered atomically."""
    if not isinstance(job_id, int) or job_id <= 0 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", operator):
        raise ValueError("Invalid job ID or operator identifier.")
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        conn.execute("SET LOCAL statement_timeout = '3s'")
        conn.execute("SET LOCAL lock_timeout = '2s'")
        # Prevent a same-case job being inserted between the ambiguity check and the audit update.
        conn.execute("LOCK TABLE integration_jobs IN SHARE ROW EXCLUSIVE MODE")
        row = conn.execute("SELECT destination, status, case_id FROM integration_jobs WHERE id = %s FOR UPDATE",
                           (job_id,)).fetchone()
        if not row or row[1] != "uncertain":
            raise ValueError("Job does not exist or is no longer uncertain.")
        if row[0] not in ("telegram", "telegram_reminder"):
            raise ValueError("Only Telegram deliveries can be confirmed by this command.")
        count = conn.execute("SELECT count(*) FROM integration_jobs WHERE case_id = %s AND destination = %s",
                             (row[2], row[0])).fetchone()[0]
        if count != 1:
            raise ValueError("Multiple matching jobs exist; chat text alone cannot uniquely identify this delivery.")
        conn.execute("""INSERT INTO integration_resolutions(job_id, decision, evidence, operator)
            VALUES (%s, 'confirmed_delivered', 'telegram_chat', %s)""", (job_id, operator))
        conn.execute("""UPDATE integration_jobs SET status = 'done', lease_until = NULL, lease_token = NULL,
            updated_at = now() WHERE id = %s AND status = 'uncertain'""", (job_id,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List uncertain job metadata without lead data")
    confirm = commands.add_parser("confirm-telegram", help="Record delivery directly verified in the Telegram chat")
    confirm.add_argument("--job", required=True, type=int)
    confirm.add_argument("--operator", required=True, help="Short operator identifier, 1–32 ASCII letters/digits/_.-")
    confirm.add_argument("--confirmed-in-chat", action="store_true", required=True,
                         help="Assert that this exact message was independently verified in Telegram")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not configured", file=sys.stderr)
        return 2
    try:
        if args.command == "list":
            print(json.dumps(list_uncertain(dsn), ensure_ascii=False))
        else:
            confirm_telegram(dsn, args.job, args.operator)
            print("Confirmed delivery recorded; no message was sent.")
    except psycopg.Error:
        print("Reconciliation failed; check database connectivity and migrations", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
