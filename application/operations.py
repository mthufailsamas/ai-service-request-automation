"""Role-protected operational evidence derived from durable primary records."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row


def load_operations_summary(
    database_url: str, *, lease_seconds: int = 60
) -> dict[str, Any]:
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        totals = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM cases) AS cases_total,
              (SELECT count(*) FROM outbox_messages) AS outbox_total,
              (SELECT count(*) FROM outbox_messages
               WHERE status = 'PENDING' AND available_at <= now()
                 AND attempt_count < max_attempts) AS retries_ready,
              (SELECT count(*) FROM outbox_messages
               WHERE status = 'PROCESSING') AS claims_processing,
              (SELECT count(*) FROM outbox_messages
               WHERE status = 'PROCESSING'
                 AND locked_at <= now() - make_interval(secs => %s))
                 AS claims_expired,
              (SELECT count(*) FROM outbox_messages
               WHERE status = 'FAILED') AS deliveries_failed,
              (SELECT count(*) FROM delivery_attempts) AS delivery_attempts,
              (SELECT count(*) FROM delivery_attempts
               WHERE error_code = 'RECOVERY_LEASE_EXPIRED')
                 AS recovered_expired_claims,
              (SELECT COALESCE(
                   round(percentile_cont(0.50) WITHIN GROUP (
                       ORDER BY extract(epoch FROM (finished_at - started_at)) * 1000
                   ))::bigint, 0
               ) FROM delivery_attempts) AS delivery_latency_p50_ms,
              (SELECT COALESCE(
                   round(percentile_cont(0.95) WITHIN GROUP (
                       ORDER BY extract(epoch FROM (finished_at - started_at)) * 1000
                   ))::bigint, 0
               ) FROM delivery_attempts) AS delivery_latency_p95_ms,
              (SELECT count(*) FROM ai_analysis_runs
               WHERE status = 'PROCESSING') AS analyses_processing,
              (SELECT count(*) FROM ai_analysis_runs
               WHERE status = 'PROCESSING'
                 AND created_at <= now() - make_interval(secs => %s))
                 AS analyses_expired
            """,
            (lease_seconds, lease_seconds),
        ).fetchone()
        case_rows = connection.execute(
            """
            SELECT current_state AS key, count(*) AS value
            FROM cases
            GROUP BY current_state
            ORDER BY current_state
            """
        ).fetchall()
        outbox_rows = connection.execute(
            """
            SELECT message_type || ':' || status AS key, count(*) AS value
            FROM outbox_messages
            GROUP BY message_type, status
            ORDER BY message_type, status
            """
        ).fetchall()

    return {
        "schema_version": "1",
        "totals": dict(totals),
        "cases_by_state": {row["key"]: row["value"] for row in case_rows},
        "outbox_by_type_and_status": {
            row["key"]: row["value"] for row in outbox_rows
        },
    }
