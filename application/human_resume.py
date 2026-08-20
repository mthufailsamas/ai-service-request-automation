"""Durably hand committed human decisions to the local n8n resume workflow."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator


HUMAN_RESUME_MESSAGE_TYPE = "HUMAN_DECISION_RESUME"
HUMAN_RESUME_DESTINATION = "N8N_HUMAN_DECISION_RESUME"
HUMAN_RESUME_SCHEMA_VERSION = "1"
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CASE_REFERENCE_PATTERN = re.compile(r"^CASE-[0-9]{4}-[0-9]{4,}$")
HUMAN_DECISION_REFERENCE_PATTERN = re.compile(r"^HD-([1-9][0-9]*)$")

ACTION_EVENT_ROUTE = {
    "SUBMIT_INFORMATION": (
        "REQUESTER_INFORMATION_SUBMITTED",
        "ANALYSIS_CONTINUATION",
    ),
    "CONFIRM_REVIEW": (
        "SERVICE_AGENT_REVIEW_CONFIRMED",
        "ANALYSIS_CONTINUATION",
    ),
    "CORRECT_REVIEW": (
        "SERVICE_AGENT_CORRECTION_ACCEPTED",
        "ANALYSIS_CONTINUATION",
    ),
    "REJECT_REVIEW": (
        "SERVICE_AGENT_REJECTED",
        "TERMINAL_NOTIFICATION",
    ),
    "APPROVE_REQUEST": (
        "APPROVAL_APPROVED",
        "DOWNSTREAM_ACTION",
    ),
    "REJECT_REQUEST": (
        "APPROVAL_REJECTED",
        "TERMINAL_NOTIFICATION",
    ),
}
HUMAN_EVENT_TYPES = tuple(value[0] for value in ACTION_EVENT_ROUTE.values())
RESUME_ROUTES = {value[1] for value in ACTION_EVENT_ROUTE.values()}
ACTION_STATE = {
    "SUBMIT_INFORMATION": "ANALYZING",
    "CONFIRM_REVIEW": "ANALYZING",
    "CORRECT_REVIEW": "ANALYZING",
    "REJECT_REVIEW": "REJECTED",
    "APPROVE_REQUEST": "READY_FOR_ACTION",
    "REJECT_REQUEST": "REJECTED",
}

HumanResumeOutcome = Literal[
    "SUCCESS",
    "TRANSIENT_FAILURE",
    "PERMANENT_FAILURE",
]


class HumanResumeCommand(BaseModel):
    """Exact primary acknowledgement command sent only by local n8n."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]
    case_reference: str = Field(pattern=r"^CASE-[0-9]{4}-[0-9]{4,}$")
    expected_case_version: int = Field(ge=2)
    human_decision_reference: str = Field(pattern=r"^HD-[1-9][0-9]*$")
    action: Literal[
        "SUBMIT_INFORMATION",
        "CONFIRM_REVIEW",
        "CORRECT_REVIEW",
        "REJECT_REVIEW",
        "APPROVE_REQUEST",
        "REJECT_REQUEST",
    ]
    trigger_event: Literal[
        "REQUESTER_INFORMATION_SUBMITTED",
        "SERVICE_AGENT_REVIEW_CONFIRMED",
        "SERVICE_AGENT_CORRECTION_ACCEPTED",
        "SERVICE_AGENT_REJECTED",
        "APPROVAL_APPROVED",
        "APPROVAL_REJECTED",
    ]
    resume_route: Literal[
        "ANALYSIS_CONTINUATION",
        "DOWNSTREAM_ACTION",
        "TERMINAL_NOTIFICATION",
    ]

    @model_validator(mode="after")
    def require_action_event_route(self) -> HumanResumeCommand:
        expected_event, expected_route = ACTION_EVENT_ROUTE[self.action]
        if self.trigger_event != expected_event or self.resume_route != expected_route:
            raise ValueError("The human-resume action, event, and route do not match.")
        return self


class HumanResumeNotFound(Exception):
    """The referenced case or human decision is absent."""


class HumanResumeConflict(Exception):
    """The outbox, event, case, or replay evidence conflicts."""


@dataclass(frozen=True)
class HumanResumeAcknowledgement:
    human_resume_reference: str
    case_reference: str
    resume_route: str
    current_state: str
    case_version: int
    idempotent_replay: bool


@dataclass(frozen=True)
class HumanResumeMessage:
    outbox_message_id: UUID
    idempotency_key: str
    payload: dict[str, Any]
    attempt_number: int
    max_attempts: int


@dataclass(frozen=True)
class HumanResumeResponse:
    outcome: HumanResumeOutcome
    http_status: int | None
    downstream_reference: str | None
    response_payload: dict[str, Any]
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class HumanResumeExecution:
    outbox_message_id: UUID
    attempt_number: int
    outcome: HumanResumeOutcome
    final_status: str
    http_status: int | None
    downstream_reference: str | None


class HumanResumeClientProtocol(Protocol):
    def resume(
        self, payload: dict[str, Any], idempotency_key: str
    ) -> HumanResumeResponse:
        """Deliver 1 bounded resume intent."""


def _human_resume_idempotency_key(case_id: UUID, event_id: int) -> str:
    source = f"HUMAN_DECISION_RESUME|{case_id}|{event_id}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def validate_human_resume_payload(payload: dict[str, Any]) -> None:
    """Reject malformed outbox intent before a network call."""

    required = {
        "schema_version",
        "case_id",
        "case_reference",
        "case_version",
        "human_decision_reference",
        "action",
        "trigger_event",
        "resume_route",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("The human-resume payload fields are invalid.")
    if payload["schema_version"] != HUMAN_RESUME_SCHEMA_VERSION:
        raise ValueError("The human-resume schema version is unsupported.")
    try:
        case_id = UUID(payload["case_id"])
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("The human-resume case ID is invalid.") from error
    if str(case_id) != payload["case_id"]:
        raise ValueError("The human-resume case ID must be canonical.")
    if not isinstance(payload["case_reference"], str) or (
        CASE_REFERENCE_PATTERN.fullmatch(payload["case_reference"]) is None
    ):
        raise ValueError("The human-resume case reference is invalid.")
    if type(payload["case_version"]) is not int or payload["case_version"] < 2:
        raise ValueError("The human-resume case version is invalid.")
    if not isinstance(payload["human_decision_reference"], str) or (
        HUMAN_DECISION_REFERENCE_PATTERN.fullmatch(
            payload["human_decision_reference"]
        )
        is None
    ):
        raise ValueError("The human-decision reference is invalid.")
    action = payload["action"]
    if action not in ACTION_EVENT_ROUTE:
        raise ValueError("The human-resume action is invalid.")
    expected_event, expected_route = ACTION_EVENT_ROUTE[action]
    if (
        payload["trigger_event"] != expected_event
        or payload["resume_route"] != expected_route
    ):
        raise ValueError("The human-resume action mapping is invalid.")


def enqueue_next_human_resume(database_url: str) -> UUID | None:
    """Create at most 1 immutable resume intent from a committed human event."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        event = connection.execute(
            """
            SELECT event.event_id,
                   event.case_id,
                   event.event_type,
                   event.to_state,
                   event.event_payload,
                   cases.case_reference
            FROM case_events AS event
            JOIN cases ON cases.case_id = event.case_id
            WHERE event.event_type = ANY(%s)
              AND event.actor_type = 'USER'
              AND event.event_payload ? 'human_command_id'
              AND event.event_payload ? 'input_sha256'
              AND event.event_payload ? 'result_case_version'
              AND cases.current_state = event.to_state
              AND cases.version::text
                  = event.event_payload->>'result_case_version'
              AND NOT EXISTS (
                  SELECT 1
                  FROM outbox_messages AS message
                  WHERE message.message_type = %s
                    AND message.destination = %s
                    AND message.payload->>'human_decision_reference'
                        = 'HD-' || event.event_id::text
              )
            ORDER BY event.event_id
            FOR UPDATE OF event SKIP LOCKED
            LIMIT 1
            """,
            (
                list(HUMAN_EVENT_TYPES),
                HUMAN_RESUME_MESSAGE_TYPE,
                HUMAN_RESUME_DESTINATION,
            ),
        ).fetchone()
        if event is None:
            return None

        event_payload = event["event_payload"]
        action = event_payload.get("action")
        command_id = event_payload.get("human_command_id")
        input_sha256 = event_payload.get("input_sha256")
        result_version = event_payload.get("result_case_version")
        if event_payload.get("schema_version") != HUMAN_RESUME_SCHEMA_VERSION:
            raise HumanResumeConflict("The committed human schema is invalid.")
        if action not in ACTION_EVENT_ROUTE:
            raise HumanResumeConflict("The committed human action is invalid.")
        expected_event, resume_route = ACTION_EVENT_ROUTE[action]
        if (
            event["event_type"] != expected_event
            or event["to_state"] != ACTION_STATE[action]
        ):
            raise HumanResumeConflict("The committed human event conflicts with its action.")
        try:
            parsed_command_id = UUID(command_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise HumanResumeConflict(
                "The committed human command ID is invalid."
            ) from error
        if str(parsed_command_id) != command_id:
            raise HumanResumeConflict("The committed human command ID is invalid.")
        if not isinstance(input_sha256, str) or (
            IDEMPOTENCY_KEY_PATTERN.fullmatch(input_sha256) is None
        ):
            raise HumanResumeConflict("The committed human input hash is invalid.")
        if type(result_version) is not int or result_version < 2:
            raise HumanResumeConflict("The committed human result version is invalid.")

        human_reference = f"HD-{event['event_id']}"
        idempotency_key = _human_resume_idempotency_key(
            event["case_id"], event["event_id"]
        )
        payload = {
            "schema_version": HUMAN_RESUME_SCHEMA_VERSION,
            "case_id": str(event["case_id"]),
            "case_reference": event["case_reference"],
            "case_version": result_version,
            "human_decision_reference": human_reference,
            "action": action,
            "trigger_event": event["event_type"],
            "resume_route": resume_route,
        }
        validate_human_resume_payload(payload)
        inserted = connection.execute(
            """
            INSERT INTO outbox_messages (
                case_id,
                message_type,
                destination,
                idempotency_key,
                payload,
                status,
                max_attempts,
                available_at
            )
            VALUES (%s, %s, %s, %s, %s, 'PENDING', 3, now())
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING outbox_message_id
            """,
            (
                event["case_id"],
                HUMAN_RESUME_MESSAGE_TYPE,
                HUMAN_RESUME_DESTINATION,
                idempotency_key,
                Jsonb(payload),
            ),
        ).fetchone()
        if inserted is None:
            existing = connection.execute(
                """
                SELECT outbox_message_id, payload
                FROM outbox_messages
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is None or existing["payload"] != payload:
                raise HumanResumeConflict(
                    "The human-resume idempotency identity conflicts."
                )
            return existing["outbox_message_id"]
        return inserted["outbox_message_id"]


def _next_event_sequence(
    connection: psycopg.Connection[Any], case_id: UUID
) -> int:
    return connection.execute(
        """
        SELECT COALESCE(MAX(sequence_number), 0) + 1 AS sequence_number
        FROM case_events
        WHERE case_id = %s
        """,
        (case_id,),
    ).fetchone()["sequence_number"]


def acknowledge_human_resume(
    database_url: str,
    *,
    case_id: UUID,
    command: HumanResumeCommand,
    idempotency_key: str,
) -> HumanResumeAcknowledgement:
    """Acknowledge 1 exact committed resume route without changing case state."""

    if IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
        raise HumanResumeConflict("The human-resume idempotency key is invalid.")
    decision_match = HUMAN_DECISION_REFERENCE_PATTERN.fullmatch(
        command.human_decision_reference
    )
    assert decision_match is not None

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
            raise HumanResumeNotFound("The human-resume case was not found.")
        if case["case_reference"] != command.case_reference:
            raise HumanResumeConflict("The human-resume case reference conflicts.")

        expected_payload = {
            "schema_version": HUMAN_RESUME_SCHEMA_VERSION,
            "case_id": str(case_id),
            "case_reference": command.case_reference,
            "case_version": command.expected_case_version,
            "human_decision_reference": command.human_decision_reference,
            "action": command.action,
            "trigger_event": command.trigger_event,
            "resume_route": command.resume_route,
        }
        outbox = connection.execute(
            """
            SELECT status, payload
            FROM outbox_messages
            WHERE case_id = %s
              AND message_type = %s
              AND destination = %s
              AND idempotency_key = %s
            """,
            (
                case_id,
                HUMAN_RESUME_MESSAGE_TYPE,
                HUMAN_RESUME_DESTINATION,
                idempotency_key,
            ),
        ).fetchone()
        if (
            outbox is None
            or outbox["payload"] != expected_payload
            or outbox["status"] not in {"PROCESSING", "SENT"}
        ):
            raise HumanResumeConflict(
                "The request does not match a dispatched human-resume intent."
            )

        existing = connection.execute(
            """
            SELECT event_id, to_state, event_payload
            FROM case_events
            WHERE case_id = %s
              AND event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
              AND event_payload->>'human_decision_reference' = %s
            ORDER BY event_id
            LIMIT 1
            """,
            (case_id, command.human_decision_reference),
        ).fetchone()
        if existing is not None:
            payload = existing["event_payload"]
            if (
                payload.get("action") != command.action
                or payload.get("resume_route") != command.resume_route
                or payload.get("outbox_idempotency_key") != idempotency_key
                or payload.get("case_version") != command.expected_case_version
            ):
                raise HumanResumeConflict(
                    "The existing human-resume acknowledgement conflicts."
                )
            return HumanResumeAcknowledgement(
                human_resume_reference=f"HDRESUME-{existing['event_id']}",
                case_reference=command.case_reference,
                resume_route=command.resume_route,
                current_state=existing["to_state"],
                case_version=command.expected_case_version,
                idempotent_replay=True,
            )

        decision_event = connection.execute(
            """
            SELECT event_id, event_type, to_state, event_payload
            FROM case_events
            WHERE event_id = %s
              AND case_id = %s
              AND actor_type = 'USER'
            """,
            (int(decision_match.group(1)), case_id),
        ).fetchone()
        if decision_event is None:
            raise HumanResumeNotFound("The committed human decision was not found.")
        decision_payload = decision_event["event_payload"]
        if (
            decision_event["event_type"] != command.trigger_event
            or decision_payload.get("action") != command.action
            or decision_payload.get("result_case_version")
            != command.expected_case_version
            or case["version"] != command.expected_case_version
            or case["current_state"] != decision_event["to_state"]
        ):
            raise HumanResumeConflict(
                "The committed human decision does not match current case state."
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
                reason,
                event_payload
            )
            VALUES (
                %s, %s, %s, %s,
                'HUMAN_DECISION_RESUME_ACKNOWLEDGED',
                'INTEGRATION',
                'n8n accepted the committed human-decision resume route.',
                %s
            )
            RETURNING event_id
            """,
            (
                case_id,
                _next_event_sequence(connection, case_id),
                case["current_state"],
                case["current_state"],
                Jsonb(
                    {
                        "action": command.action,
                        "case_version": command.expected_case_version,
                        "human_decision_reference": command.human_decision_reference,
                        "outbox_idempotency_key": idempotency_key,
                        "resume_route": command.resume_route,
                        "schema_version": HUMAN_RESUME_SCHEMA_VERSION,
                    }
                ),
            ),
        ).fetchone()

    return HumanResumeAcknowledgement(
        human_resume_reference=f"HDRESUME-{event['event_id']}",
        case_reference=command.case_reference,
        resume_route=command.resume_route,
        current_state=case["current_state"],
        case_version=command.expected_case_version,
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
    return parsed if isinstance(parsed, dict) else {"raw_response": decoded[:2_000]}


def _error_text(
    response_payload: dict[str, Any], field_name: str, fallback: str
) -> str:
    value = response_payload.get(field_name)
    return value[:2_000] if isinstance(value, str) and value.strip() else fallback


def _valid_success_response(
    response_payload: dict[str, Any], request_payload: dict[str, Any]
) -> str | None:
    reference = response_payload.get("human_resume_reference")
    if (
        response_payload.get("schema_version") == HUMAN_RESUME_SCHEMA_VERSION
        and response_payload.get("status") == "ACCEPTED"
        and isinstance(reference, str)
        and re.fullmatch(r"HDRESUME-[1-9][0-9]*", reference) is not None
        and response_payload.get("case_reference")
        == request_payload["case_reference"]
        and response_payload.get("resume_route")
        == request_payload["resume_route"]
        and response_payload.get("current_state")
        == ACTION_STATE[request_payload["action"]]
        and response_payload.get("case_version") == request_payload["case_version"]
        and type(response_payload.get("idempotent_replay")) is bool
    ):
        return reference
    return None


class HumanResumeClient:
    """Translate the n8n webhook result into 3 durable outcomes."""

    def __init__(self, webhook_url: str, token: str, timeout_seconds: float = 5.0):
        self.webhook_url = webhook_url
        self.token = token
        self.timeout_seconds = timeout_seconds

    def resume(
        self, payload: dict[str, Any], idempotency_key: str
    ) -> HumanResumeResponse:
        try:
            validate_human_resume_payload(payload)
            if IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
                raise ValueError("The human-resume idempotency key is invalid.")
        except ValueError as error:
            return HumanResumeResponse(
                "PERMANENT_FAILURE",
                None,
                None,
                {"validation_error": str(error)},
                "INVALID_HUMAN_RESUME_INTENT",
                str(error),
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
                request, timeout=self.timeout_seconds
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
            return HumanResumeResponse(
                "TRANSIENT_FAILURE",
                None,
                None,
                {"transport_error": type(error).__name__},
                "N8N_TRANSPORT_ERROR",
                str(error) or "The n8n human-resume webhook was unreachable.",
            )

        if http_status == 200:
            reference = _valid_success_response(response_payload, payload)
            if reference is not None:
                return HumanResumeResponse(
                    "SUCCESS", http_status, reference, response_payload, None, None
                )
            return HumanResumeResponse(
                "TRANSIENT_FAILURE",
                http_status,
                None,
                response_payload,
                "INVALID_N8N_ACKNOWLEDGEMENT",
                "The n8n response did not prove durable human-resume acceptance.",
            )
        if 200 <= http_status <= 299 or http_status in {408, 429} or (
            500 <= http_status <= 599
        ):
            return HumanResumeResponse(
                "TRANSIENT_FAILURE",
                http_status,
                None,
                response_payload,
                _error_text(response_payload, "error_code", f"N8N_HTTP_{http_status}"),
                _error_text(
                    response_payload,
                    "message",
                    "The n8n human-resume acknowledgement is retryable or ambiguous.",
                ),
            )
        return HumanResumeResponse(
            "PERMANENT_FAILURE",
            http_status,
            None,
            response_payload,
            _error_text(response_payload, "error_code", f"N8N_HTTP_{http_status}"),
            _error_text(
                response_payload,
                "message",
                "The n8n human-resume webhook permanently rejected the intent.",
            ),
        )


def claim_next_human_resume(database_url: str) -> HumanResumeMessage | None:
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
            RETURNING message.outbox_message_id,
                      message.idempotency_key,
                      message.payload,
                      message.attempt_count,
                      message.max_attempts
            """,
            (HUMAN_RESUME_MESSAGE_TYPE, HUMAN_RESUME_DESTINATION),
        ).fetchone()
    if claimed is None:
        return None
    return HumanResumeMessage(
        claimed["outbox_message_id"],
        claimed["idempotency_key"],
        claimed["payload"],
        claimed["attempt_count"],
        claimed["max_attempts"],
    )


def finalize_human_resume(
    database_url: str,
    message: HumanResumeMessage,
    result: HumanResumeResponse,
    *,
    started_at: datetime,
    finished_at: datetime,
    retry_delay_seconds: int,
) -> HumanResumeExecution:
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
        if (
            current is None
            or current["status"] != "PROCESSING"
            or current["attempt_count"] != message.attempt_number
        ):
            raise RuntimeError("The claimed human-resume state changed.")
        connection.execute(
            """
            INSERT INTO delivery_attempts (
                outbox_message_id, attempt_number, outcome, http_status,
                downstream_reference, response_payload, error_code,
                error_message, started_at, finished_at
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
                SET status = 'SENT', locked_at = NULL, last_error = NULL,
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
            connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'PENDING', locked_at = NULL, last_error = %s,
                    available_at = %s
                WHERE outbox_message_id = %s
                """,
                (
                    result.error_message,
                    finished_at + timedelta(seconds=retry_delay_seconds),
                    message.outbox_message_id,
                ),
            )
        else:
            final_status = "FAILED"
            error_message = result.error_message or "Human resume failed."
            if result.outcome == "TRANSIENT_FAILURE":
                error_message += " The attempt limit was reached."
            connection.execute(
                """
                UPDATE outbox_messages
                SET status = 'FAILED', locked_at = NULL, last_error = %s,
                    completed_at = %s
                WHERE outbox_message_id = %s
                """,
                (error_message, finished_at, message.outbox_message_id),
            )
    return HumanResumeExecution(
        message.outbox_message_id,
        message.attempt_number,
        result.outcome,
        final_status,
        result.http_status,
        result.downstream_reference,
    )


def recover_abandoned_human_resume(
    database_url: str,
    *,
    lease_seconds: int,
    retry_delay_seconds: int,
) -> HumanResumeExecution | None:
    finished_at = datetime.now(timezone.utc)
    cutoff = finished_at - timedelta(seconds=lease_seconds)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        abandoned = connection.execute(
            """
            SELECT message.outbox_message_id, message.attempt_count,
                   message.max_attempts, message.locked_at
            FROM outbox_messages AS message
            WHERE message.status = 'PROCESSING'
              AND message.message_type = %s
              AND message.destination = %s
              AND message.locked_at <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM delivery_attempts AS attempt
                  WHERE attempt.outbox_message_id = message.outbox_message_id
                    AND attempt.attempt_number = message.attempt_count
              )
            ORDER BY message.locked_at, message.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (HUMAN_RESUME_MESSAGE_TYPE, HUMAN_RESUME_DESTINATION, cutoff),
        ).fetchone()
        if abandoned is None:
            return None
        error_message = "The human-resume dispatcher lease expired; outcome is unknown."
        connection.execute(
            """
            INSERT INTO delivery_attempts (
                outbox_message_id, attempt_number, outcome, http_status,
                downstream_reference, response_payload, error_code,
                error_message, started_at, finished_at
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
                SET status = 'PENDING', locked_at = NULL, last_error = %s,
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
                SET status = 'FAILED', locked_at = NULL, last_error = %s,
                    completed_at = %s
                WHERE outbox_message_id = %s
                """,
                (
                    error_message + " The attempt limit was reached.",
                    finished_at,
                    abandoned["outbox_message_id"],
                ),
            )
    return HumanResumeExecution(
        abandoned["outbox_message_id"],
        abandoned["attempt_count"],
        "TRANSIENT_FAILURE",
        final_status,
        None,
        None,
    )


def process_one_human_resume(
    database_url: str,
    client: HumanResumeClientProtocol,
    *,
    retry_delay_seconds: int = 30,
    lease_seconds: int = 60,
) -> HumanResumeExecution | None:
    recovered = recover_abandoned_human_resume(
        database_url,
        lease_seconds=lease_seconds,
        retry_delay_seconds=retry_delay_seconds,
    )
    if recovered is not None:
        return recovered
    message = claim_next_human_resume(database_url)
    if message is None:
        return None
    started_at = datetime.now(timezone.utc)
    result = client.resume(message.payload, message.idempotency_key)
    finished_at = datetime.now(timezone.utc)
    return finalize_human_resume(
        database_url,
        message,
        result,
        started_at=started_at,
        finished_at=finished_at,
        retry_delay_seconds=retry_delay_seconds,
    )


def main() -> int:
    database_url = os.environ.get("PRIMARY_DATABASE_URL")
    webhook_url = os.environ.get("N8N_HUMAN_RESUME_URL")
    webhook_token = os.environ.get("N8N_HUMAN_RESUME_TOKEN")
    if not database_url or not webhook_url or not webhook_token:
        raise RuntimeError(
            "PRIMARY_DATABASE_URL, N8N_HUMAN_RESUME_URL, and "
            "N8N_HUMAN_RESUME_TOKEN are required."
        )
    while enqueue_next_human_resume(database_url) is not None:
        pass
    execution = process_one_human_resume(
        database_url,
        HumanResumeClient(webhook_url, webhook_token),
    )
    return 0 if execution is None or execution.final_status != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
