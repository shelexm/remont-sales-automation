"""Aggregate accepted lead events without exposing customer data."""
import argparse
from datetime import timezone
import json
import os
import sys

import psycopg


def report(dsn, days=30):
    if not 1 <= days <= 365:
        raise ValueError("Days must be between 1 and 365.")
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '5s'")
        start, end = conn.execute("SELECT now() - (%s * interval '1 day'), now()", (days,)).fetchone()
        scope = (start, end)
        counts = conn.execute("""
            SELECT count(*), count(*) FILTER (WHERE e.status = 'completed'),
                   count(*) FILTER (WHERE e.status = 'pending'),
                   count(*) FILTER (WHERE e.status = 'processing'),
                   count(*) FILTER (WHERE e.status = 'failed'),
                   count(*) FILTER (WHERE e.case_id IS NOT NULL AND c.first_event_id = e.id),
                   count(*) FILTER (WHERE e.case_id IS NOT NULL AND c.first_event_id <> e.id),
                   count(*) FILTER (WHERE e.case_id IS NULL)
            FROM lead_events e LEFT JOIN lead_cases c ON c.id = e.case_id
            WHERE e.created_at >= %s AND e.created_at < %s
        """, scope).fetchone()
        services = conn.execute("""
            SELECT CASE WHEN payload->>'service' IN ('cosmetic', 'capital', 'design', 'other')
                        THEN payload->>'service' ELSE 'unknown' END AS category, count(*)
            FROM lead_events WHERE created_at >= %s AND created_at < %s GROUP BY category ORDER BY category
        """, scope).fetchall()
        budgets = conn.execute("""
            SELECT CASE WHEN payload->>'budget_class' IN ('high', 'low')
                        THEN payload->>'budget_class' ELSE 'unknown' END AS category, count(*)
            FROM lead_events WHERE created_at >= %s AND created_at < %s GROUP BY category ORDER BY category
        """, scope).fetchall()
        jobs = conn.execute("""
            SELECT j.destination, j.status, count(*) FROM integration_jobs j
            JOIN lead_events e ON e.id = j.event_id
            WHERE e.created_at >= %s AND e.created_at < %s
            GROUP BY j.destination, j.status ORDER BY j.destination, j.status
        """, scope).fetchall()
        no_crm = conn.execute("""
            SELECT count(*) FROM lead_events e
            WHERE e.created_at >= %s AND e.created_at < %s AND e.status = 'completed'
              AND NOT EXISTS (SELECT 1 FROM integration_jobs j WHERE j.event_id=e.id AND j.destination='crm')
        """, scope).fetchone()[0]
    keys = ("accepted", "completed", "pending", "processing", "failed",
            "first_case_events", "repeat_case_events", "without_case")
    deliveries = {}
    for destination, status, count in jobs:
        deliveries.setdefault(destination, {})[status] = count
    return {"window_days": days, "from_utc": start.astimezone(timezone.utc).isoformat(),
            "until_utc": end.astimezone(timezone.utc).isoformat(), "events": dict(zip(keys, counts, strict=True)),
            "services": dict(services), "budget_classes": dict(budgets), "integration_jobs": deliveries,
            "completed_without_crm_job": no_crm}


def print_report(data):
    events = data["events"]
    service_labels = {"cosmetic": "косметический", "capital": "капитальный", "design": "дизайн-проект",
                      "other": "другое", "unknown": "неизвестно"}
    budget_labels = {"high": "высокий", "low": "низкий", "unknown": "неизвестно"}
    destination_labels = {"crm": "CRM", "telegram": "Telegram", "telegram_reminder": "напоминания Telegram"}
    status_labels = {"done": "выполнено", "pending": "ожидает", "processing": "обрабатывается",
                     "failed": "ошибка", "uncertain": "результат неизвестен"}
    print(f"Отчёт за последние {data['window_days']} дн. (UTC): {data['from_utc']} — {data['until_utc']}")
    print(f"Принято уникальных событий: {events['accepted']}; обработано: {events['completed']}; "
          f"ожидают: {events['pending']}; в обработке: {events['processing']}; ошибок: {events['failed']}")
    print(f"Первых событий карточки: {events['first_case_events']}; повторных событий карточки: "
          f"{events['repeat_case_events']}; без карточки: {events['without_case']}")
    print("Услуги: " + (", ".join(f"{service_labels[key]}: {value}" for key, value in data["services"].items()) or "нет данных"))
    print("Класс бюджета: " + (", ".join(f"{budget_labels[key]}: {value}" for key, value in data["budget_classes"].items()) or "нет данных"))
    for destination, statuses in data["integration_jobs"].items():
        label = destination_labels.get(destination, "неизвестное назначение")
        print(f"{label}: " + ", ".join(f"{status_labels.get(k, 'неизвестный статус')}: {v}" for k, v in statuses.items()))
    print(f"Обработано без задания CRM: {data['completed_without_crm_job']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not configured", file=sys.stderr)
        return 2
    try:
        data = report(dsn, args.days)
    except psycopg.Error:
        print("Analytics failed; check database connectivity and schema", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    else:
        print_report(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
