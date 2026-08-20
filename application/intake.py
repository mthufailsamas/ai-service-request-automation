"""Deterministic case creation shared by the web form and REST webhook."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator


SourceChannel = Literal["WEB", "WEBHOOK"]

WEB_SUBMISSION_PATTERN = re.compile(
    r"^WEB-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class AttachmentMetadata(BaseModel):
    """Safe metadata only; intake never stores attachment bytes or paths."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=100)
    size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("name", "media_type")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace text")
        return value

    @field_validator("name")
    @classmethod
    def reject_paths(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("must be a file name, not a path")
        return value


class IntakeRequest(BaseModel):
    """The shared command produced after either transport validates its body."""

    model_config = ConfigDict(extra="forbid")

    source_channel: SourceChannel
    external_request_id: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=20_000)
    attachment_metadata: list[AttachmentMetadata] = Field(
        default_factory=list,
        max_length=10,
    )
    received_at: datetime

    @field_validator("external_request_id")
    @classmethod
    def normalize_external_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must contain non-whitespace text")
        return normalized

    @field_validator("subject", "message")
    @classmethod
    def preserve_nonblank_original_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace text")
        return value

    @field_validator("received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("external_request_id")
    @classmethod
    def validate_web_submission_id(cls, value: str, info: Any) -> str:
        source_channel = info.data.get("source_channel")
        if source_channel == "WEB" and not WEB_SUBMISSION_PATTERN.fullmatch(value):
            raise ValueError("must be a canonical WEB UUID v4 submission ID")
        return value


@dataclass(frozen=True)
class RequesterSelector:
    """Exactly 1 authenticated channel identity used to resolve a requester."""

    user_id: UUID | None = None
    employee_reference: str | None = None


@dataclass(frozen=True)
class IntakeResult:
    case_id: UUID
    case_reference: str
    current_state: str
    idempotent_replay: bool


class RequesterNotAuthorized(Exception):
    """The channel identity did not resolve to an active requester."""


class IdempotencyConflict(Exception):
    """An existing request identifier was reused with different input."""


def _normalize_duplicate_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def idempotency_key_for(request: IntakeRequest) -> str:
    source_value = f"{request.source_channel}|{request.external_request_id}"
    return hashlib.sha256(source_value.encode("utf-8")).hexdigest()


def content_fingerprint_for(
    requester_id: UUID,
    request: IntakeRequest,
) -> str:
    duplicate_signal = {
        "message": _normalize_duplicate_text(request.message),
        "requester_id": str(requester_id),
        "subject": _normalize_duplicate_text(request.subject),
    }
    return hashlib.sha256(
        _canonical_json(duplicate_signal).encode("utf-8")
    ).hexdigest()


def _attachment_values(request: IntakeRequest) -> list[dict[str, Any]]:
    return [
        item.model_dump(mode="json", exclude_none=True)
        for item in request.attachment_metadata
    ]


def _resolve_requester(
    connection: psycopg.Connection[Any],
    selector: RequesterSelector,
) -> UUID:
    if (selector.user_id is None) == (selector.employee_reference is None):
        raise ValueError("RequesterSelector must contain exactly 1 identity")

    if selector.user_id is not None:
        requester = connection.execute(
            """
            SELECT user_id
            FROM users
            WHERE user_id = %s
              AND is_active
              AND EXISTS (
                  SELECT 1
                  FROM user_roles
                  WHERE user_roles.user_id = users.user_id
                    AND role_code = 'REQUESTER'
              )
            """,
            (selector.user_id,),
        ).fetchone()
    else:
        employee_reference = selector.employee_reference.strip().upper()
        requester = connection.execute(
            """
            SELECT user_id
            FROM users
            WHERE employee_reference = %s
              AND is_active
              AND EXISTS (
                  SELECT 1
                  FROM user_roles
                  WHERE user_roles.user_id = users.user_id
                    AND role_code = 'REQUESTER'
              )
            """,
            (employee_reference,),
        ).fetchone()

    if requester is None:
        raise RequesterNotAuthorized
    return requester["user_id"]


def requester_is_authorized(database_url: str, user_id: UUID) -> bool:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        try:
            _resolve_requester(connection, RequesterSelector(user_id=user_id))
        except RequesterNotAuthorized:
            return False
    return True


def _same_business_input(
    existing_case: dict[str, Any],
    requester_id: UUID,
    request: IntakeRequest,
) -> bool:
    return (
        existing_case["requester_id"] == requester_id
        and existing_case["subject"] == request.subject
        and existing_case["original_message"] == request.message
        and existing_case["attachment_metadata"] == _attachment_values(request)
    )


def create_or_replay_case(
    database_url: str,
    request: IntakeRequest,
    requester_selector: RequesterSelector,
) -> IntakeResult:
    """Commit a new case/event/workflow intent or return its exact replay."""

    idempotency_key = idempotency_key_for(request)

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        # Calls for the same source ID serialize before identity and replay checks.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (idempotency_key,),
        )
        requester_id = _resolve_requester(connection, requester_selector)

        existing_case = connection.execute(
            """
            SELECT
                case_id,
                case_reference,
                requester_id,
                subject,
                original_message,
                attachment_metadata,
                current_state
            FROM cases
            WHERE source_channel = %s
              AND external_request_id = %s
            """,
            (request.source_channel, request.external_request_id),
        ).fetchone()

        if existing_case is not None:
            if not _same_business_input(existing_case, requester_id, request):
                raise IdempotencyConflict
            return IntakeResult(
                case_id=existing_case["case_id"],
                case_reference=existing_case["case_reference"],
                current_state=existing_case["current_state"],
                idempotent_replay=True,
            )

        reference_number = connection.execute(
            "SELECT nextval('case_reference_sequence') AS number"
        ).fetchone()["number"]
        case_reference = (
            f"CASE-{datetime.now(timezone.utc).year}-{reference_number:04d}"
        )
        attachments = _attachment_values(request)
        content_fingerprint = content_fingerprint_for(requester_id, request)

        created_case = connection.execute(
            """
            INSERT INTO cases (
                case_reference,
                source_channel,
                external_request_id,
                idempotency_key,
                content_fingerprint,
                requester_id,
                subject,
                original_message,
                attachment_metadata,
                received_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING case_id
            """,
            (
                case_reference,
                request.source_channel,
                request.external_request_id,
                idempotency_key,
                content_fingerprint,
                requester_id,
                request.subject,
                request.message,
                Jsonb(attachments),
                request.received_at,
            ),
        ).fetchone()
        case_id = created_case["case_id"]

        is_web = request.source_channel == "WEB"
        actor_type = "USER" if is_web else "INTEGRATION"
        actor_user_id = requester_id if is_web else None
        reason = (
            "Request submitted through the web form."
            if is_web
            else "Request accepted through the REST webhook."
        )

        connection.execute(
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
            VALUES (%s, 1, NULL, 'RECEIVED', 'CASE_RECEIVED',
                    %s, %s, %s, %s)
            """,
            (
                case_id,
                actor_type,
                actor_user_id,
                reason,
                Jsonb(
                    {
                        "external_request_id": request.external_request_id,
                        "source": request.source_channel,
                    }
                ),
            ),
        )

        workflow_idempotency_key = hashlib.sha256(
            f"WORKFLOW_START|{case_id}".encode("utf-8")
        ).hexdigest()
        connection.execute(
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
            VALUES (%s, 'WORKFLOW_START', 'N8N_REQUEST_INTAKE',
                    %s, %s, 'PENDING', 3, now())
            """,
            (
                case_id,
                workflow_idempotency_key,
                Jsonb(
                    {
                        "case_id": str(case_id),
                        "case_reference": case_reference,
                        "case_version": 1,
                        "schema_version": "1",
                        "trigger_event": "CASE_RECEIVED",
                    }
                ),
            ),
        )

        return IntakeResult(
            case_id=case_id,
            case_reference=case_reference,
            current_state="RECEIVED",
            idempotent_replay=False,
        )


def find_requester_case(
    database_url: str,
    case_reference: str,
    requester_id: UUID,
) -> dict[str, Any] | None:
    """Return the small browser receipt view only to its active requester."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        requester = connection.execute(
            """
            SELECT 1
            FROM users
            WHERE user_id = %s
              AND is_active
              AND EXISTS (
                  SELECT 1
                  FROM user_roles
                  WHERE user_roles.user_id = users.user_id
                    AND role_code = 'REQUESTER'
              )
            """,
            (requester_id,),
        ).fetchone()
        if requester is None:
            return None

        case = connection.execute(
            """
            SELECT case_reference, subject, current_state, received_at
            FROM cases
            WHERE case_reference = %s
              AND requester_id = %s
            """,
            (case_reference, requester_id),
        ).fetchone()

    return dict(case) if case is not None else None
