"""Bounded recovery of expired durable outbox claims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


ACCEPTED_DESTINATIONS = {
    "WORKFLOW_START": "N8N_REQUEST_INTAKE",
    "HUMAN_DECISION_RESUME": "N8N_HUMAN_DECISION_RESUME",
    "DOWNSTREAM_ACTION": "service-desk-sandbox",
    "REQUESTER_NOTIFICATION": "local-requester-inbox",
}


@dataclass(frozen=True)
class RecoveredClaim:
    outbox_message_id: UUID
    message_type: str
    attempt_number: int
    final_status: str


@dataclass(frozen=True)
class RecoverySweep:
    recovered_claims: tuple[RecoveredClaim, ...]
    pending_retries: int
    terminal_failures: int


def recover_expired_claims(
    database_url: str,
    *,
    lease_seconds: int,
    retry_delay_seconds: int,
    limit: int,
) -> RecoverySweep:
    """Recover only expired accepted claims, bounded by one database transaction."""

    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    if not 0 <= retry_delay_seconds <= 3600:
        raise ValueError("retry_delay_seconds must be between 0 and 3600")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    finished_at = datetime.now(timezone.utc)
    cutoff = finished_at - timedelta(seconds=lease_seconds)
    recovered: list[RecoveredClaim] = []

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            """
            SELECT message.outbox_message_id, message.message_type,
                   message.attempt_count, message.max_attempts,
                   message.locked_at
            FROM outbox_messages AS message
            WHERE message.status = 'PROCESSING'
              AND message.locked_at <= %s
              AND (message.message_type, message.destination) IN (
                  ('WORKFLOW_START', 'N8N_REQUEST_INTAKE'),
                  ('HUMAN_DECISION_RESUME', 'N8N_HUMAN_DECISION_RESUME'),
                  ('DOWNSTREAM_ACTION', 'service-desk-sandbox'),
                  ('REQUESTER_NOTIFICATION', 'local-requester-inbox')
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM delivery_attempts AS attempt
                  WHERE attempt.outbox_message_id = message.outbox_message_id
                    AND attempt.attempt_number = message.attempt_count
              )
            ORDER BY message.locked_at, message.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (cutoff, limit),
        ).fetchall()

        for row in rows:
            error_message = (
                "The worker lease expired; the transport outcome is unknown."
            )
            connection.execute(
                """
                INSERT INTO delivery_attempts (
                    outbox_message_id, attempt_number, outcome, http_status,
                    downstream_reference, response_payload, error_code,
                    error_message, started_at, finished_at
                )
                VALUES (
                    %s, %s, 'TRANSIENT_FAILURE', NULL, NULL, %s,
                    'RECOVERY_LEASE_EXPIRED', %s, %s, %s
                )
                """,
                (
                    row["outbox_message_id"],
                    row["attempt_count"],
                    Jsonb({"transport_outcome": "UNKNOWN"}),
                    error_message,
                    row["locked_at"],
                    finished_at,
                ),
            )
            if row["attempt_count"] < row["max_attempts"]:
                final_status = "PENDING"
                connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'PENDING', locked_at = NULL,
                        last_error = %s, available_at = %s
                    WHERE outbox_message_id = %s
                    """,
                    (
                        error_message,
                        finished_at + timedelta(seconds=retry_delay_seconds),
                        row["outbox_message_id"],
                    ),
                )
            else:
                final_status = "FAILED"
                connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'FAILED', locked_at = NULL,
                        last_error = %s, completed_at = %s
                    WHERE outbox_message_id = %s
                    """,
                    (
                        error_message + " The attempt limit was reached.",
                        finished_at,
                        row["outbox_message_id"],
                    ),
                )
            recovered.append(
                RecoveredClaim(
                    outbox_message_id=row["outbox_message_id"],
                    message_type=row["message_type"],
                    attempt_number=row["attempt_count"],
                    final_status=final_status,
                )
            )

    return RecoverySweep(
        recovered_claims=tuple(recovered),
        pending_retries=sum(item.final_status == "PENDING" for item in recovered),
        terminal_failures=sum(item.final_status == "FAILED" for item in recovered),
    )
