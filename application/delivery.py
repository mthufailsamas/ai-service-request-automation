"""Deliver 1 ready primary outbox message to the Service Desk Sandbox."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


DeliveryOutcome = Literal["SUCCESS", "TRANSIENT_FAILURE", "PERMANENT_FAILURE"]


@dataclass(frozen=True)
class OutboxMessage:
    outbox_message_id: Any
    idempotency_key: str
    payload: dict[str, Any]
    attempt_number: int
    max_attempts: int


@dataclass(frozen=True)
class DeliveryResponse:
    outcome: DeliveryOutcome
    http_status: int | None
    downstream_reference: str | None
    response_payload: dict[str, Any]
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class DeliveryExecution:
    outbox_message_id: Any
    attempt_number: int
    outcome: DeliveryOutcome
    final_status: str
    http_status: int | None
    downstream_reference: str | None


def _decode_json_object(raw_body: bytes) -> dict[str, Any]:
    if not raw_body:
        return {}

    decoded = raw_body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        return {"raw_response": decoded[:2000]}

    if isinstance(parsed, dict):
        return parsed
    return {"raw_response": decoded[:2000]}


def _error_text(
    response_payload: dict[str, Any],
    field_name: str,
    fallback: str,
) -> str:
    value = response_payload.get(field_name)
    if isinstance(value, str) and value.strip():
        return value
    return fallback


class ServiceDeskClient:
    """Translate the sandbox HTTP contract into 3 delivery outcomes."""

    def __init__(self, base_url: str, token: str, timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def create_service_record(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        test_outcome: str | None = None,
    ) -> DeliveryResponse:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        if test_outcome is not None:
            headers["X-Sandbox-Test-Outcome"] = test_outcome

        request = urllib.request.Request(
            f"{self.base_url}/api/v1/service-records",
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                http_status = response.status
                response_payload = _decode_json_object(response.read())
        except urllib.error.HTTPError as error:
            http_status = error.code
            response_payload = _decode_json_object(error.read())
        except (urllib.error.URLError, TimeoutError) as error:
            return DeliveryResponse(
                outcome="TRANSIENT_FAILURE",
                http_status=None,
                downstream_reference=None,
                response_payload={"transport_error": type(error).__name__},
                error_code="DOWNSTREAM_TRANSPORT_ERROR",
                error_message=str(error) or "The downstream service was unreachable.",
            )

        if http_status in {200, 201}:
            downstream_reference = response_payload.get("service_record_reference")
            replay_flag = response_payload.get("idempotent_replay")
            expected_replay_flag = http_status == 200
            if (
                isinstance(downstream_reference, str)
                and downstream_reference.strip()
                and response_payload.get("status") == "ACCEPTED"
                and replay_flag is expected_replay_flag
            ):
                return DeliveryResponse(
                    outcome="SUCCESS",
                    http_status=http_status,
                    downstream_reference=downstream_reference,
                    response_payload=response_payload,
                    error_code=None,
                    error_message=None,
                )

            return DeliveryResponse(
                outcome="PERMANENT_FAILURE",
                http_status=http_status,
                downstream_reference=None,
                response_payload=response_payload,
                error_code="INVALID_DOWNSTREAM_RESPONSE",
                error_message="The sandbox success response violated its contract.",
            )

        if http_status == 503 and response_payload.get("retryable") is True:
            return DeliveryResponse(
                outcome="TRANSIENT_FAILURE",
                http_status=http_status,
                downstream_reference=None,
                response_payload=response_payload,
                error_code=_error_text(
                    response_payload,
                    "error_code",
                    "DOWNSTREAM_TEMPORARILY_UNAVAILABLE",
                ),
                error_message=_error_text(
                    response_payload,
                    "message",
                    "The sandbox is temporarily unavailable.",
                ),
            )

        return DeliveryResponse(
            outcome="PERMANENT_FAILURE",
            http_status=http_status,
            downstream_reference=None,
            response_payload=response_payload,
            error_code=_error_text(
                response_payload,
                "error_code",
                f"DOWNSTREAM_HTTP_{http_status}",
            ),
            error_message=_error_text(
                response_payload,
                "message",
                "The sandbox permanently rejected the delivery.",
            ),
        )


def claim_next_message(
    database_url: str,
    *,
    message_type: str = "DOWNSTREAM_ACTION",
    destination: str = "service-desk-sandbox",
) -> OutboxMessage | None:
    """Atomically reserve 1 ready delivery without holding a network transaction."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        claimed = connection.execute(
            """
            WITH next_message AS (
                SELECT outbox_message_id
                FROM outbox_messages
                WHERE status = 'PENDING'
                  AND message_type = %s
                  AND destination = %s
                  AND available_at <= now()
                  AND attempt_count < max_attempts
                ORDER BY available_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE outbox_messages AS message
            SET status = 'PROCESSING',
                attempt_count = message.attempt_count + 1,
                locked_at = now()
            FROM next_message
            WHERE message.outbox_message_id = next_message.outbox_message_id
            RETURNING
                message.outbox_message_id,
                message.idempotency_key,
                message.payload,
                message.attempt_count,
                message.max_attempts
            """,
            (message_type, destination),
        ).fetchone()

    if claimed is None:
        return None

    return OutboxMessage(
        outbox_message_id=claimed["outbox_message_id"],
        idempotency_key=claimed["idempotency_key"],
        payload=claimed["payload"],
        attempt_number=claimed["attempt_count"],
        max_attempts=claimed["max_attempts"],
    )


def finalize_delivery(
    database_url: str,
    message: OutboxMessage,
    result: DeliveryResponse,
    *,
    started_at: datetime,
    finished_at: datetime,
    retry_delay_seconds: int,
) -> DeliveryExecution:
    """Append the attempt and move the outbox state in 1 transaction."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        current = connection.execute(
            """
            SELECT status, attempt_count, max_attempts
            FROM outbox_messages
            WHERE outbox_message_id = %s
            FOR UPDATE
            """,
            (message.outbox_message_id,),
        ).fetchone()

        if current is None:
            raise RuntimeError("The claimed outbox message no longer exists.")
        if (
            current["status"] != "PROCESSING"
            or current["attempt_count"] != message.attempt_number
        ):
            raise RuntimeError("The claimed outbox state changed before finalization.")

        connection.execute(
            """
            INSERT INTO delivery_attempts (
                outbox_message_id,
                attempt_number,
                outcome,
                http_status,
                downstream_reference,
                response_payload,
                error_code,
                error_message,
                started_at,
                finished_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                message.outbox_message_id,
                message.attempt_number,
                result.outcome,
                result.http_status,
                result.downstream_reference,
                Jsonb(result.response_payload),
                result.error_code,
                result.error_message,
                started_at,
                finished_at,
            ),
        )

        if result.outcome == "SUCCESS":
            final_status = "SENT"
            connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'SENT',
                    locked_at = NULL,
                    last_error = NULL,
                    completed_at = %s
                WHERE outbox_message_id = %s
                """,
                (finished_at, message.outbox_message_id),
            )
        elif (
            result.outcome == "TRANSIENT_FAILURE"
            and message.attempt_number < current["max_attempts"]
        ):
            final_status = "PENDING"
            retry_at = finished_at + timedelta(seconds=retry_delay_seconds)
            connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'PENDING',
                    locked_at = NULL,
                    last_error = %s,
                    available_at = %s
                WHERE outbox_message_id = %s
                """,
                (result.error_message, retry_at, message.outbox_message_id),
            )
        else:
            final_status = "FAILED"
            final_error = result.error_message or "Delivery failed permanently."
            if result.outcome == "TRANSIENT_FAILURE":
                final_error = f"{final_error} The attempt limit was reached."
            connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'FAILED',
                    locked_at = NULL,
                    last_error = %s,
                    completed_at = %s
                WHERE outbox_message_id = %s
                """,
                (final_error, finished_at, message.outbox_message_id),
            )

    return DeliveryExecution(
        outbox_message_id=message.outbox_message_id,
        attempt_number=message.attempt_number,
        outcome=result.outcome,
        final_status=final_status,
        http_status=result.http_status,
        downstream_reference=result.downstream_reference,
    )


def process_one_message(
    database_url: str,
    client: ServiceDeskClient,
    *,
    test_outcome: str | None = None,
    retry_delay_seconds: int = 30,
) -> DeliveryExecution | None:
    message = claim_next_message(database_url)
    if message is None:
        return None

    started_at = datetime.now(timezone.utc)
    result = client.create_service_record(
        message.payload,
        message.idempotency_key,
        test_outcome=test_outcome,
    )
    finished_at = datetime.now(timezone.utc)

    return finalize_delivery(
        database_url,
        message,
        result,
        started_at=started_at,
        finished_at=finished_at,
        retry_delay_seconds=retry_delay_seconds,
    )


def main() -> int:
    database_url = os.environ.get("PRIMARY_DATABASE_URL")
    sandbox_url = os.environ.get("SERVICE_DESK_SANDBOX_URL")
    sandbox_token = os.environ.get("SERVICE_DESK_SANDBOX_TOKEN")
    if not database_url or not sandbox_url or not sandbox_token:
        raise RuntimeError(
            "PRIMARY_DATABASE_URL, SERVICE_DESK_SANDBOX_URL, and "
            "SERVICE_DESK_SANDBOX_TOKEN are required."
        )

    client = ServiceDeskClient(sandbox_url, sandbox_token)
    execution = process_one_message(database_url, client)
    if execution is None:
        print("No ready downstream outbox message was found.")
        return 0

    print(
        json.dumps(
            {
                "outbox_message_id": str(execution.outbox_message_id),
                "attempt_number": execution.attempt_number,
                "outcome": execution.outcome,
                "final_status": execution.final_status,
                "http_status": execution.http_status,
                "downstream_reference": execution.downstream_reference,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
