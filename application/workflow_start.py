"""Durably hand 1 committed workflow-start intent to local n8n."""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


WorkflowStartOutcome = Literal[
    "SUCCESS",
    "TRANSIENT_FAILURE",
    "PERMANENT_FAILURE",
]

WORKFLOW_MESSAGE_TYPE = "WORKFLOW_START"
WORKFLOW_DESTINATION = "N8N_REQUEST_INTAKE"
WORKFLOW_SCHEMA_VERSION = "1"
WORKFLOW_TRIGGER_EVENT = "CASE_RECEIVED"
WORKFLOW_ACCEPTED_TRANSITION = "RECEIVED->ANALYZING"
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CASE_REFERENCE_PATTERN = re.compile(r"^CASE-[0-9]{4}-[0-9]{4,}$")
WORKFLOW_PAYLOAD_FIELDS = {
    "schema_version",
    "case_id",
    "case_reference",
    "case_version",
    "trigger_event",
}
POST_ANALYSIS_STATES = {
    "ANALYZING",
    "NEEDS_INFORMATION",
    "NEEDS_REVIEW",
    "PENDING_APPROVAL",
    "READY_FOR_ACTION",
    "COMPLETED",
    "REJECTED",
    "FAILED",
}


class AnalysisStartNotFound(Exception):
    """Raised when the requested primary case does not exist."""


class AnalysisStartConflict(Exception):
    """Raised when the requested transition conflicts with durable state."""


@dataclass(frozen=True)
class AnalysisStartResult:
    workflow_start_reference: str
    case_reference: str
    current_state: str
    case_version: int
    idempotent_replay: bool


@dataclass(frozen=True)
class WorkflowStartMessage:
    outbox_message_id: Any
    idempotency_key: str
    payload: dict[str, Any]
    attempt_number: int
    max_attempts: int


@dataclass(frozen=True)
class WorkflowStartResponse:
    outcome: WorkflowStartOutcome
    http_status: int | None
    downstream_reference: str | None
    response_payload: dict[str, Any]
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class WorkflowStartExecution:
    outbox_message_id: Any
    attempt_number: int
    outcome: WorkflowStartOutcome
    final_status: str
    http_status: int | None
    downstream_reference: str | None


def _workflow_reference(event_id: int) -> str:
    return f"WFSTART-{event_id}"


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise ValueError("The workflow-start idempotency key is invalid.")


def validate_workflow_payload(payload: dict[str, Any]) -> None:
    """Reject a malformed local outbox intent before any network request."""

    if not isinstance(payload, dict) or set(payload) != WORKFLOW_PAYLOAD_FIELDS:
        raise ValueError("The workflow-start payload fields are invalid.")
    if payload["schema_version"] != WORKFLOW_SCHEMA_VERSION:
        raise ValueError("The workflow-start schema version is unsupported.")
    if payload["trigger_event"] != WORKFLOW_TRIGGER_EVENT:
        raise ValueError("The workflow-start trigger event is invalid.")
    if type(payload["case_version"]) is not int or payload["case_version"] != 1:
        raise ValueError("The workflow-start case version must be 1.")
    if not isinstance(payload["case_reference"], str) or not (
        CASE_REFERENCE_PATTERN.fullmatch(payload["case_reference"])
    ):
        raise ValueError("The workflow-start case reference is invalid.")
    try:
        case_id = UUID(payload["case_id"])
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("The workflow-start case ID is invalid.") from error
    if str(case_id) != payload["case_id"]:
        raise ValueError("The workflow-start case ID must be canonical.")


def start_or_replay_analysis(
    database_url: str,
    *,
    case_id: UUID,
    case_reference: str,
    expected_case_version: int,
    trigger_event: str,
    idempotency_key: str,
) -> AnalysisStartResult:
    """Commit the guarded RECEIVED-to-ANALYZING transition exactly once."""

    _validate_idempotency_key(idempotency_key)
    if expected_case_version != 1 or trigger_event != WORKFLOW_TRIGGER_EVENT:
        raise AnalysisStartConflict("The workflow-start input is not v1.")

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        case = connection.execute(
            """
            SELECT case_reference, current_state, version
            FROM cases
            WHERE case_id = %s
            FOR UPDATE
            """,
            (case_id,),
        ).fetchone()
        if case is None:
            raise AnalysisStartNotFound("The workflow-start case was not found.")
        if case["case_reference"] != case_reference:
            raise AnalysisStartConflict("The case reference does not match.")

        outbox = connection.execute(
            """
            SELECT status, attempt_count, payload
            FROM outbox_messages
            WHERE case_id = %s
              AND message_type = 'WORKFLOW_START'
              AND destination = 'N8N_REQUEST_INTAKE'
              AND idempotency_key = %s
            """,
            (case_id, idempotency_key),
        ).fetchone()
        expected_payload = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "case_id": str(case_id),
            "case_reference": case_reference,
            "case_version": expected_case_version,
            "trigger_event": trigger_event,
        }
        if outbox is None or outbox["payload"] != expected_payload:
            raise AnalysisStartConflict(
                "The request does not match a committed workflow-start intent."
            )

        existing_event = connection.execute(
            """
            SELECT event_id, event_payload
            FROM case_events
            WHERE case_id = %s
              AND event_type = 'ANALYSIS_STARTED'
              AND event_payload ->> 'workflow_start_idempotency_key' = %s
            ORDER BY sequence_number
            LIMIT 1
            """,
            (case_id, idempotency_key),
        ).fetchone()
        if existing_event is not None:
            expected_event_payload = {
                "schema_version": WORKFLOW_SCHEMA_VERSION,
                "trigger_event": trigger_event,
                "workflow_start_idempotency_key": idempotency_key,
            }
            if existing_event["event_payload"] != expected_event_payload:
                raise AnalysisStartConflict(
                    "The existing workflow-start event conflicts with the request."
                )
            if case["current_state"] not in POST_ANALYSIS_STATES:
                raise AnalysisStartConflict(
                    "The replay found an incompatible current case state."
                )
            if outbox["status"] not in {"PROCESSING", "SENT"}:
                raise AnalysisStartConflict(
                    "The replay is not backed by an active or completed claim."
                )
            return AnalysisStartResult(
                workflow_start_reference=_workflow_reference(
                    existing_event["event_id"]
                ),
                case_reference=case["case_reference"],
                current_state=case["current_state"],
                case_version=case["version"],
                idempotent_replay=True,
            )

        if (
            case["current_state"] != "RECEIVED"
            or case["version"] != expected_case_version
            or outbox["status"] != "PROCESSING"
            or outbox["attempt_count"] < 1
        ):
            raise AnalysisStartConflict(
                "The case or outbox state does not permit analysis start."
            )

        new_version = case["version"] + 1
        connection.execute(
            """
            UPDATE cases
            SET current_state = 'ANALYZING',
                version = %s,
                updated_at = now()
            WHERE case_id = %s
            """,
            (new_version, case_id),
        )
        event = connection.execute(
            """
            INSERT INTO case_events (
                case_id,
                sequence_number,
                from_state,
                to_state,
                event_type,
                actor_type,
                actor_user_id,
                reason,
                event_payload
            )
            VALUES (
                %s,
                %s,
                'RECEIVED',
                'ANALYZING',
                'ANALYSIS_STARTED',
                'INTEGRATION',
                NULL,
                'Workflow start accepted by the orchestration boundary.',
                %s
            )
            RETURNING event_id
            """,
            (
                case_id,
                new_version,
                Jsonb(
                    {
                        "schema_version": WORKFLOW_SCHEMA_VERSION,
                        "trigger_event": trigger_event,
                        "workflow_start_idempotency_key": idempotency_key,
                    }
                ),
            ),
        ).fetchone()

    return AnalysisStartResult(
        workflow_start_reference=_workflow_reference(event["event_id"]),
        case_reference=case_reference,
        current_state="ANALYZING",
        case_version=new_version,
        idempotent_replay=False,
    )


def _decode_json_object(raw_body: bytes) -> dict[str, Any]:
    if not raw_body:
        return {}
    decoded = raw_body[:65_536].decode("utf-8", errors="replace")
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        return {"raw_response": decoded[:2_000]}
    if isinstance(parsed, dict):
        return parsed
    return {"raw_response": decoded[:2_000]}


def _error_text(
    response_payload: dict[str, Any],
    field_name: str,
    fallback: str,
) -> str:
    value = response_payload.get(field_name)
    if isinstance(value, str) and value.strip():
        return value[:2_000]
    return fallback


def _valid_success_response(
    response_payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> str | None:
    reference = response_payload.get("workflow_start_reference")
    replay = response_payload.get("idempotent_replay")
    current_state = response_payload.get("current_state")
    case_version = response_payload.get("case_version")
    if (
        response_payload.get("schema_version") == WORKFLOW_SCHEMA_VERSION
        and response_payload.get("status") == "ACCEPTED"
        and isinstance(reference, str)
        and 0 < len(reference.strip()) <= 100
        and response_payload.get("case_reference")
        == request_payload["case_reference"]
        and response_payload.get("accepted_transition")
        == WORKFLOW_ACCEPTED_TRANSITION
        and current_state in POST_ANALYSIS_STATES
        and type(case_version) is int
        and case_version >= 2
        and type(replay) is bool
    ):
        return reference
    return None


class WorkflowStartClient:
    """Translate the n8n webhook contract into 3 durable outcomes."""

    def __init__(self, webhook_url: str, token: str, timeout_seconds: float = 5.0):
        self.webhook_url = webhook_url
        self.token = token
        self.timeout_seconds = timeout_seconds

    def start_workflow(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> WorkflowStartResponse:
        try:
            validate_workflow_payload(payload)
            _validate_idempotency_key(idempotency_key)
        except ValueError as error:
            return WorkflowStartResponse(
                outcome="PERMANENT_FAILURE",
                http_status=None,
                downstream_reference=None,
                response_payload={"validation_error": str(error)},
                error_code="INVALID_WORKFLOW_START_INTENT",
                error_message=str(error),
            )

        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                http_status = response.status
                response_payload = _decode_json_object(response.read(65_537))
        except urllib.error.HTTPError as error:
            http_status = error.code
            response_payload = _decode_json_object(error.read(65_537))
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
            OSError,
        ) as error:
            return WorkflowStartResponse(
                outcome="TRANSIENT_FAILURE",
                http_status=None,
                downstream_reference=None,
                response_payload={"transport_error": type(error).__name__},
                error_code="N8N_TRANSPORT_ERROR",
                error_message=str(error) or "The n8n webhook was unreachable.",
            )

        if http_status == 200:
            reference = _valid_success_response(response_payload, payload)
            if reference is not None:
                return WorkflowStartResponse(
                    outcome="SUCCESS",
                    http_status=http_status,
                    downstream_reference=reference,
                    response_payload=response_payload,
                    error_code=None,
                    error_message=None,
                )
            return WorkflowStartResponse(
                outcome="TRANSIENT_FAILURE",
                http_status=http_status,
                downstream_reference=None,
                response_payload=response_payload,
                error_code="INVALID_N8N_ACKNOWLEDGEMENT",
                error_message=(
                    "The n8n success response did not prove durable workflow start."
                ),
            )

        if 200 <= http_status <= 299 or http_status in {408, 429} or (
            500 <= http_status <= 599
        ):
            return WorkflowStartResponse(
                outcome="TRANSIENT_FAILURE",
                http_status=http_status,
                downstream_reference=None,
                response_payload=response_payload,
                error_code=_error_text(
                    response_payload,
                    "error_code",
                    f"N8N_HTTP_{http_status}",
                ),
                error_message=_error_text(
                    response_payload,
                    "message",
                    "The n8n acknowledgement is retryable or ambiguous.",
                ),
            )

        return WorkflowStartResponse(
            outcome="PERMANENT_FAILURE",
            http_status=http_status,
            downstream_reference=None,
            response_payload=response_payload,
            error_code=_error_text(
                response_payload,
                "error_code",
                f"N8N_HTTP_{http_status}",
            ),
            error_message=_error_text(
                response_payload,
                "message",
                "The n8n webhook permanently rejected workflow start.",
            ),
        )


def claim_next_workflow_start(database_url: str) -> WorkflowStartMessage | None:
    """Atomically reserve 1 ready workflow-start intent."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        claimed = connection.execute(
            """
            WITH next_message AS (
                SELECT outbox_message_id
                FROM outbox_messages
                WHERE status = 'PENDING'
                  AND message_type = 'WORKFLOW_START'
                  AND destination = 'N8N_REQUEST_INTAKE'
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
            """
        ).fetchone()

    if claimed is None:
        return None
    return WorkflowStartMessage(
        outbox_message_id=claimed["outbox_message_id"],
        idempotency_key=claimed["idempotency_key"],
        payload=claimed["payload"],
        attempt_number=claimed["attempt_count"],
        max_attempts=claimed["max_attempts"],
    )


def finalize_workflow_start(
    database_url: str,
    message: WorkflowStartMessage,
    result: WorkflowStartResponse,
    *,
    started_at: datetime,
    finished_at: datetime,
    retry_delay_seconds: int,
) -> WorkflowStartExecution:
    """Append the attempt and update the claimed outbox row atomically."""

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
            raise RuntimeError("The claimed workflow-start message no longer exists.")
        if (
            current["status"] != "PROCESSING"
            or current["attempt_count"] != message.attempt_number
        ):
            raise RuntimeError(
                "The claimed workflow-start state changed before finalization."
            )

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
            final_error = result.error_message or "Workflow start failed."
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

    return WorkflowStartExecution(
        outbox_message_id=message.outbox_message_id,
        attempt_number=message.attempt_number,
        outcome=result.outcome,
        final_status=final_status,
        http_status=result.http_status,
        downstream_reference=result.downstream_reference,
    )


def recover_abandoned_workflow_start(
    database_url: str,
    *,
    lease_seconds: int,
    retry_delay_seconds: int,
) -> WorkflowStartExecution | None:
    """Finalize at most 1 expired claim with an honest unknown outcome."""

    finished_at = datetime.now(timezone.utc)
    cutoff = finished_at - timedelta(seconds=lease_seconds)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        abandoned = connection.execute(
            """
            SELECT
                message.outbox_message_id,
                message.attempt_count,
                message.max_attempts,
                message.locked_at
            FROM outbox_messages AS message
            WHERE message.status = 'PROCESSING'
              AND message.message_type = 'WORKFLOW_START'
              AND message.destination = 'N8N_REQUEST_INTAKE'
              AND message.locked_at <= %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM delivery_attempts AS attempt
                  WHERE attempt.outbox_message_id = message.outbox_message_id
                    AND attempt.attempt_number = message.attempt_count
              )
            ORDER BY message.locked_at, message.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
        if abandoned is None:
            return None

        error_message = (
            "The dispatcher lease expired; the transport outcome is unknown."
        )
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
            VALUES (%s, %s, 'TRANSIENT_FAILURE', NULL, NULL, %s,
                    'DISPATCH_LEASE_EXPIRED', %s, %s, %s)
            """,
            (
                abandoned["outbox_message_id"],
                abandoned["attempt_count"],
                Jsonb({"transport_outcome": "UNKNOWN"}),
                error_message,
                abandoned["locked_at"],
                finished_at,
            ),
        )

        if abandoned["attempt_count"] < abandoned["max_attempts"]:
            final_status = "PENDING"
            connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'PENDING',
                    locked_at = NULL,
                    last_error = %s,
                    available_at = %s
                WHERE outbox_message_id = %s
                """,
                (
                    error_message,
                    finished_at + timedelta(seconds=retry_delay_seconds),
                    abandoned["outbox_message_id"],
                ),
            )
        else:
            final_status = "FAILED"
            connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'FAILED',
                    locked_at = NULL,
                    last_error = %s,
                    completed_at = %s
                WHERE outbox_message_id = %s
                """,
                (
                    f"{error_message} The attempt limit was reached.",
                    finished_at,
                    abandoned["outbox_message_id"],
                ),
            )

    return WorkflowStartExecution(
        outbox_message_id=abandoned["outbox_message_id"],
        attempt_number=abandoned["attempt_count"],
        outcome="TRANSIENT_FAILURE",
        final_status=final_status,
        http_status=None,
        downstream_reference=None,
    )


def process_one_workflow_start(
    database_url: str,
    client: WorkflowStartClient,
    *,
    retry_delay_seconds: int = 30,
    lease_seconds: int = 60,
) -> WorkflowStartExecution | None:
    recovered = recover_abandoned_workflow_start(
        database_url,
        lease_seconds=lease_seconds,
        retry_delay_seconds=retry_delay_seconds,
    )
    if recovered is not None:
        return recovered

    message = claim_next_workflow_start(database_url)
    if message is None:
        return None

    started_at = datetime.now(timezone.utc)
    result = client.start_workflow(message.payload, message.idempotency_key)
    finished_at = datetime.now(timezone.utc)
    return finalize_workflow_start(
        database_url,
        message,
        result,
        started_at=started_at,
        finished_at=finished_at,
        retry_delay_seconds=retry_delay_seconds,
    )


def main() -> int:
    database_url = os.environ.get("PRIMARY_DATABASE_URL")
    webhook_url = os.environ.get("N8N_WORKFLOW_START_URL")
    webhook_token = os.environ.get("N8N_WORKFLOW_START_TOKEN")
    if not database_url or not webhook_url or not webhook_token:
        raise RuntimeError(
            "PRIMARY_DATABASE_URL, N8N_WORKFLOW_START_URL, and "
            "N8N_WORKFLOW_START_TOKEN are required."
        )

    execution = process_one_workflow_start(
        database_url,
        WorkflowStartClient(webhook_url, webhook_token),
    )
    if execution is None:
        print("No ready workflow-start outbox message was found.")
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
