"""Small local downstream API for controlled delivery testing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DATABASE_URL = os.environ.get("SERVICE_DESK_SANDBOX_DATABASE_URL")
SANDBOX_TOKEN = os.environ.get("SERVICE_DESK_SANDBOX_TOKEN")
TEST_MODE = os.environ.get("SERVICE_DESK_SANDBOX_TEST_MODE", "false").lower() == "true"

if not DATABASE_URL:
    raise RuntimeError("SERVICE_DESK_SANDBOX_DATABASE_URL is required")
if not SANDBOX_TOKEN:
    raise RuntimeError("SERVICE_DESK_SANDBOX_TOKEN is required")


CASE_REFERENCE_PATTERN = re.compile(r"^CASE-[0-9]{4}-[0-9]{4,}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TEST_OUTCOMES = {"TRANSIENT_ONCE", "PERMANENT_FAILURE"}
IMPACT_LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

ActionType = Literal[
    "POLICY_RESPONSE",
    "INCIDENT_TICKET",
    "ACCESS_ACTION",
    "DATA_CHANGE_ACTION",
    "STATUS_RESPONSE",
]

ACTION_DETAIL_FIELDS: dict[str, frozenset[str]] = {
    "POLICY_RESPONSE": frozenset({"answer", "citation_ids"}),
    "INCIDENT_TICKET": frozenset(
        {"affected_service", "impact", "urgency", "description"}
    ),
    "ACCESS_ACTION": frozenset(
        {
            "target_system",
            "access_level",
            "approver_reference",
            "approval_reference",
        }
    ),
    "DATA_CHANGE_ACTION": frozenset(
        {
            "target_system",
            "record_reference",
            "requested_changes",
            "approver_reference",
            "approval_reference",
        }
    ),
    "STATUS_RESPONSE": frozenset(
        {"referenced_case", "visible_state", "public_update"}
    ),
}


class ServiceRecordRequest(BaseModel):
    """Fields the primary application is allowed to deliver downstream."""

    model_config = ConfigDict(extra="forbid")

    case_reference: str = Field(pattern=r"^CASE-[0-9]{4}-[0-9]{4,}$")
    case_version: int = Field(gt=0)
    action_type: ActionType
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    details: dict[str, Any]

    @field_validator("title", "summary")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace text")
        return value

    @model_validator(mode="after")
    def validate_action_details(self) -> "ServiceRecordRequest":
        required_fields = ACTION_DETAIL_FIELDS[self.action_type]
        provided_fields = frozenset(self.details)

        missing = sorted(required_fields - provided_fields)
        unexpected = sorted(provided_fields - required_fields)
        if missing or unexpected:
            problems = []
            if missing:
                problems.append(f"missing details fields: {', '.join(missing)}")
            if unexpected:
                problems.append(f"unexpected details fields: {', '.join(unexpected)}")
            raise ValueError("; ".join(problems))

        for field_name, value in self.details.items():
            if field_name == "citation_ids":
                if not isinstance(value, list) or not value:
                    raise ValueError("citation_ids must be a non-empty list")
                if any(not isinstance(item, str) or not item.strip() for item in value):
                    raise ValueError("citation_ids must contain non-blank strings")
                continue

            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-blank string")

        if self.action_type == "INCIDENT_TICKET":
            if self.details["impact"] not in IMPACT_LEVELS:
                raise ValueError("impact must be an accepted impact level")
            if self.details["urgency"] not in IMPACT_LEVELS:
                raise ValueError("urgency must be an accepted urgency level")

        if self.action_type == "STATUS_RESPONSE":
            if not CASE_REFERENCE_PATTERN.fullmatch(self.details["referenced_case"]):
                raise ValueError("referenced_case must follow CASE-YYYY-NNNN")

        return self


@dataclass(frozen=True)
class ServiceResult:
    http_status: int
    body: dict[str, Any]


app = FastAPI(
    title="Service Desk Sandbox",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def error_body(error_code: str, message: str, retryable: bool) -> dict[str, Any]:
    return {
        "error_code": error_code,
        "message": message,
        "retryable": retryable,
    }


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, _exception: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=error_body(
            "INVALID_REQUEST",
            "The request does not match the Service Desk Sandbox contract.",
            False,
        ),
    )


def require_authentication(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    expected = f"Bearer {SANDBOX_TOKEN}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        return_response = JSONResponse(
            status_code=401,
            content=error_body(
                "INVALID_SERVICE_CREDENTIAL",
                "A valid local service credential is required.",
                False,
            ),
        )
        raise AuthenticationResponse(return_response)


class AuthenticationResponse(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


@app.exception_handler(AuthenticationResponse)
async def authentication_exception_handler(
    _request: Request, exception: AuthenticationResponse
) -> JSONResponse:
    return exception.response


def canonical_request_sha256(record_request: ServiceRecordRequest) -> str:
    canonical_json = json.dumps(
        record_request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def append_event(
    connection: psycopg.Connection[Any],
    *,
    service_record_id: Any,
    idempotency_key: str,
    request_sha256: str,
    event_type: str,
    event_payload: dict[str, Any],
) -> None:
    sequence_row = connection.execute(
        """
        SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_sequence
        FROM service_record_events
        WHERE delivery_idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()

    connection.execute(
        """
        INSERT INTO service_record_events (
            service_record_id,
            delivery_idempotency_key,
            request_sha256,
            sequence_number,
            event_type,
            event_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            service_record_id,
            idempotency_key,
            request_sha256,
            sequence_row["next_sequence"],
            event_type,
            Jsonb(event_payload),
        ),
    )


def conflict_result() -> ServiceResult:
    return ServiceResult(
        409,
        error_body(
            "IDEMPOTENCY_CONFLICT",
            "The idempotency key was already used with different content.",
            False,
        ),
    )


def permanent_failure_result() -> ServiceResult:
    return ServiceResult(
        422,
        error_body(
            "SANDBOX_PERMANENT_REJECTION",
            "The controlled permanent failure is active.",
            False,
        ),
    )


def create_or_replay_record(
    record_request: ServiceRecordRequest,
    idempotency_key: str,
    test_outcome: str | None,
) -> ServiceResult:
    request_sha256 = canonical_request_sha256(record_request)

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        # Calls for the same key are serialized before checking or writing state.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (idempotency_key,),
        )

        existing_record = connection.execute(
            """
            SELECT service_record_id, service_record_reference, request_sha256
            FROM service_records
            WHERE delivery_idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()

        if existing_record is not None:
            if existing_record["request_sha256"] != request_sha256:
                append_event(
                    connection,
                    service_record_id=existing_record["service_record_id"],
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    event_type="IDEMPOTENCY_CONFLICT",
                    event_payload={"reason": "request_hash_mismatch"},
                )
                return conflict_result()

            append_event(
                connection,
                service_record_id=existing_record["service_record_id"],
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                event_type="IDEMPOTENT_REPLAY",
                event_payload={
                    "service_record_reference": existing_record[
                        "service_record_reference"
                    ]
                },
            )
            return ServiceResult(
                200,
                {
                    "service_record_reference": existing_record[
                        "service_record_reference"
                    ],
                    "status": "ACCEPTED",
                    "idempotent_replay": True,
                },
            )

        first_event = connection.execute(
            """
            SELECT request_sha256
            FROM service_record_events
            WHERE delivery_idempotency_key = %s
            ORDER BY sequence_number
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()

        if first_event is not None and first_event["request_sha256"] != request_sha256:
            append_event(
                connection,
                service_record_id=None,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                event_type="IDEMPOTENCY_CONFLICT",
                event_payload={"reason": "request_hash_mismatch_after_failure"},
            )
            return conflict_result()

        permanent_failure = connection.execute(
            """
            SELECT 1
            FROM service_record_events
            WHERE delivery_idempotency_key = %s
              AND request_sha256 = %s
              AND event_type = 'PERMANENT_FAILURE'
            LIMIT 1
            """,
            (idempotency_key, request_sha256),
        ).fetchone()

        if permanent_failure is not None:
            append_event(
                connection,
                service_record_id=None,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                event_type="PERMANENT_FAILURE",
                event_payload={"controlled": True, "terminal_replay": True},
            )
            return permanent_failure_result()

        if test_outcome == "PERMANENT_FAILURE":
            append_event(
                connection,
                service_record_id=None,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                event_type="PERMANENT_FAILURE",
                event_payload={"controlled": True, "terminal_replay": False},
            )
            return permanent_failure_result()

        transient_failure = connection.execute(
            """
            SELECT 1
            FROM service_record_events
            WHERE delivery_idempotency_key = %s
              AND request_sha256 = %s
              AND event_type = 'TRANSIENT_FAILURE'
            LIMIT 1
            """,
            (idempotency_key, request_sha256),
        ).fetchone()

        if test_outcome == "TRANSIENT_ONCE" and transient_failure is None:
            append_event(
                connection,
                service_record_id=None,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                event_type="TRANSIENT_FAILURE",
                event_payload={"controlled": True},
            )
            return ServiceResult(
                503,
                error_body(
                    "SANDBOX_TEMPORARILY_UNAVAILABLE",
                    "The controlled transient failure is active.",
                    True,
                ),
            )

        reference_number = connection.execute(
            "SELECT nextval('service_record_reference_sequence') AS number"
        ).fetchone()["number"]
        service_record_reference = (
            f"SR-{datetime.now(timezone.utc).year}-{reference_number:04d}"
        )

        created_record = connection.execute(
            """
            INSERT INTO service_records (
                service_record_reference,
                delivery_idempotency_key,
                request_sha256,
                source_case_reference,
                source_case_version,
                action_type,
                title,
                summary,
                details
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING service_record_id
            """,
            (
                service_record_reference,
                idempotency_key,
                request_sha256,
                record_request.case_reference,
                record_request.case_version,
                record_request.action_type,
                record_request.title,
                record_request.summary,
                Jsonb(record_request.details),
            ),
        ).fetchone()

        append_event(
            connection,
            service_record_id=created_record["service_record_id"],
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            event_type="RECORD_CREATED",
            event_payload={"service_record_reference": service_record_reference},
        )

        return ServiceResult(
            201,
            {
                "service_record_reference": service_record_reference,
                "status": "ACCEPTED",
                "idempotent_replay": False,
            },
        )


@app.get("/health")
def health() -> dict[str, str]:
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute("SELECT 1")
    return {"status": "ok"}


@app.post(
    "/api/v1/service-records",
    dependencies=[Depends(require_authentication)],
)
def create_service_record(
    record_request: ServiceRecordRequest,
    response: Response,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
    test_outcome: Annotated[
        str | None, Header(alias="X-Sandbox-Test-Outcome")
    ] = None,
) -> dict[str, Any]:
    if idempotency_key is None or not IDEMPOTENCY_KEY_PATTERN.fullmatch(
        idempotency_key
    ):
        response.status_code = 400
        return error_body(
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key must contain 64 lowercase SHA-256 characters.",
            False,
        )

    if test_outcome is not None:
        if not TEST_MODE:
            response.status_code = 400
            return error_body(
                "SANDBOX_TEST_MODE_DISABLED",
                "Controlled outcomes require sandbox test mode.",
                False,
            )
        if test_outcome not in TEST_OUTCOMES:
            response.status_code = 400
            return error_body(
                "INVALID_SANDBOX_TEST_OUTCOME",
                "The controlled outcome is unsupported.",
                False,
            )

    result = create_or_replay_record(
        record_request,
        idempotency_key,
        test_outcome,
    )
    response.status_code = result.http_status
    return result.body


@app.get(
    "/api/v1/service-records/{service_record_reference}",
    dependencies=[Depends(require_authentication)],
)
def read_service_record(
    service_record_reference: str,
    response: Response,
) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        record = connection.execute(
            """
            SELECT
                service_record_reference,
                source_case_reference AS case_reference,
                source_case_version AS case_version,
                action_type,
                title,
                summary,
                details,
                status,
                created_at
            FROM service_records
            WHERE service_record_reference = %s
            """,
            (service_record_reference,),
        ).fetchone()

    if record is None:
        response.status_code = 404
        return error_body(
            "SERVICE_RECORD_NOT_FOUND",
            "The requested service record does not exist.",
            False,
        )

    return dict(record)
