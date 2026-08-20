"""Primary FastAPI entry point for the accepted service-request intake."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai_analysis import (
    AnalysisConflict,
    AnalysisInProgress,
    AnalysisNotFound,
    AnalysisProvider,
    FixtureAnalysisProvider,
    OllamaAnalysisProvider,
    ProviderConfigurationError,
    analyze_case,
)
from human_decision import (
    HumanDecisionCommand,
    HumanDecisionConflict,
    HumanDecisionInvalid,
    HumanDecisionNotAuthorized,
    HumanDecisionNotFound,
    execute_human_decision,
)
from human_resume import (
    HumanResumeCommand,
    HumanResumeConflict,
    HumanResumeNotFound,
    acknowledge_human_resume,
)
from intake import (
    AttachmentMetadata,
    IdempotencyConflict,
    IntakeRequest,
    RequesterNotAuthorized,
    RequesterSelector,
    WEB_SUBMISSION_PATTERN,
    create_or_replay_case,
    requester_is_authorized,
)
from policy_retrieval import (
    FixturePolicyProvider,
    OllamaPolicyProvider,
    PolicyProvider,
    RetrievalConflict,
    RetrievalInProgress,
    RetrievalNotFound,
    RetrievalProviderError,
    retrieve_policy,
)
from portal import (
    authenticate_portal_user,
    list_visible_cases,
    load_portal_user,
    load_visible_case,
)
from operations import load_operations_summary
from recovery import recover_expired_claims
from workflow_start import (
    AnalysisStartConflict,
    AnalysisStartNotFound,
    start_or_replay_analysis,
)


DATABASE_URL = os.environ.get("PRIMARY_DATABASE_URL")
WEBHOOK_TOKEN = os.environ.get("INTAKE_WEBHOOK_TOKEN")
SESSION_SECRET = os.environ.get("APP_SESSION_SECRET")
PRIMARY_WORKFLOW_TOKEN = os.environ.get("PRIMARY_WORKFLOW_TOKEN")

if not DATABASE_URL:
    raise RuntimeError("PRIMARY_DATABASE_URL is required")
if not WEBHOOK_TOKEN or len(WEBHOOK_TOKEN) < 32:
    raise RuntimeError("INTAKE_WEBHOOK_TOKEN must contain at least 32 characters")
if not SESSION_SECRET or len(SESSION_SECRET) < 32:
    raise RuntimeError("APP_SESSION_SECRET must contain at least 32 characters")
if not PRIMARY_WORKFLOW_TOKEN or len(PRIMARY_WORKFLOW_TOKEN) < 32:
    raise RuntimeError("PRIMARY_WORKFLOW_TOKEN must contain at least 32 characters")


MAX_BODY_BYTES = 256 * 1024
SESSION_COOKIE_NAME = "service_request_session"
LOGIN_NONCE_COOKIE_NAME = "service_request_login_nonce"
CASE_REFERENCE_PATTERN = re.compile(r"^CASE-[0-9]{4}-[0-9]{4,}$")
EMPLOYEE_REFERENCE_PATTERN = re.compile(r"^[A-Z][A-Z0-9-]{1,49}$")
LOGIN_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
LOGOUT_SUBMISSION_PATTERN = re.compile(r"^LOGOUT-[0-9a-f]{32}$")
PORTAL_NEXT_PATHS = {"/cases", "/requests/new"}
CORRECTION_FIELD_NAMES = (
    "policy_topic",
    "question",
    "affected_service",
    "incident_description",
    "impact",
    "urgency",
    "target_system",
    "requested_access_level",
    "business_reason",
    "approver_id",
    "record_reference",
    "requested_changes",
    "case_reference",
)


class WebhookRequest(BaseModel):
    """Transport fields accepted from an authenticated source system."""

    model_config = ConfigDict(extra="forbid")

    external_request_id: str = Field(min_length=1, max_length=100)
    requester_reference: str = Field(min_length=2, max_length=50)
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=20_000)
    attachments: list[AttachmentMetadata] = Field(default_factory=list, max_length=10)
    received_at: datetime

    @field_validator("external_request_id")
    @classmethod
    def normalize_external_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must contain non-whitespace text")
        return normalized

    @field_validator("requester_reference")
    @classmethod
    def normalize_requester_reference(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not EMPLOYEE_REFERENCE_PATTERN.fullmatch(normalized):
            raise ValueError("must be a valid employee reference")
        return normalized

    @field_validator("subject", "message")
    @classmethod
    def reject_blank_original_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace text")
        return value


class AnalysisStartRequest(BaseModel):
    """Strict internal command accepted only from the local n8n workflow."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    case_reference: str = Field(pattern=r"^CASE-[0-9]{4}-[0-9]{4,}$")
    expected_case_version: Literal[1]
    trigger_event: Literal["CASE_RECEIVED"]


class AnalysisRequest(BaseModel):
    """Strict stable references accepted by the AI-analysis boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]
    case_reference: str = Field(pattern=r"^CASE-[0-9]{4}-[0-9]{4,}$")
    expected_case_version: int = Field(ge=1)
    workflow_start_reference: str = Field(pattern=r"^WFSTART-[1-9][0-9]*$")


class PolicyRetrievalRequest(BaseModel):
    """Strict stable references for 1 accepted policy analysis."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]
    case_reference: str = Field(pattern=r"^CASE-[0-9]{4}-[0-9]{4,}$")
    expected_case_version: int = Field(ge=1)
    analysis_run_id: UUID


class RecoverySweepRequest(BaseModel):
    """Strict bounded command used by the local scheduled recovery workflow."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]
    lease_seconds: int = Field(ge=1, le=3600)
    retry_delay_seconds: int = Field(ge=0, le=3600)
    limit: int = Field(ge=1, le=100)


class BodyTooLarge(Exception):
    """Raised before an oversized request reaches domain code."""


app = FastAPI(
    title="AI Service Request Automation",
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


def _encode_session_payload(payload: dict[str, Any]) -> str:
    raw_payload = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw_payload).rstrip(b"=").decode("ascii")


def create_session_cookie(
    user_id: UUID,
    secret: str,
    *,
    expires_at: datetime | None = None,
) -> str:
    """Create the signed value that a later login checkpoint will set."""

    expiry = expires_at or datetime.now(timezone.utc) + timedelta(hours=8)
    payload_segment = _encode_session_payload(
        {
            "expires_at": int(expiry.timestamp()),
            "user_id": str(user_id),
        }
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_segment}.{signature}"


def _decode_session_cookie(cookie_value: str, secret: str) -> UUID | None:
    try:
        payload_segment, supplied_signature = cookie_value.split(".", 1)
    except ValueError:
        return None

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not secrets.compare_digest(supplied_signature, expected_signature):
        return None

    try:
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment + padding).decode("utf-8")
        )
        if set(payload) != {"expires_at", "user_id"}:
            return None
        if not isinstance(payload["expires_at"], int):
            return None
        if payload["expires_at"] < int(time.time()):
            return None
        return UUID(payload["user_id"])
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def create_csrf_token(session_cookie: str, submission_id: str, secret: str) -> str:
    csrf_input = f"CSRF|{session_cookie}|{submission_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), csrf_input, hashlib.sha256).hexdigest()


def create_login_csrf_token(nonce: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"LOGIN|{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _web_session(request: Request) -> tuple[str, UUID] | None:
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return None
    user_id = _decode_session_cookie(cookie_value, SESSION_SECRET)
    if user_id is None:
        return None
    return cookie_value, user_id


def _safe_next_path(value: str | None) -> str:
    return value if value in PORTAL_NEXT_PATHS else "/cases"


def _login_redirect(next_path: str = "/requests/new") -> RedirectResponse:
    return RedirectResponse(
        url=f"/login?next={urllib.parse.quote(_safe_next_path(next_path), safe='/')}",
        status_code=303,
    )


def _browser_error(status_code: int, message: str) -> HTMLResponse:
    return HTMLResponse(
        status_code=status_code,
        content=(
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>Service request</title></head><body>"
            f"<h1>Request unavailable</h1><p>{html.escape(message)}</p>"
            "</body></html>"
        ),
    )


def _portal_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #f4f6f8; color: #17212b; }}
    main {{ max-width: 980px; margin: 2rem auto; padding: 0 1rem 3rem; }}
    section, form, table {{ background: white; border: 1px solid #d9e0e7; border-radius: .6rem; }}
    section, form {{ padding: 1rem; margin: 1rem 0; }}
    table {{ width: 100%; border-collapse: collapse; overflow: hidden; }}
    th, td {{ text-align: left; padding: .7rem; border-bottom: 1px solid #e6ebef; }}
    label {{ display: block; margin: .65rem 0; }}
    input, textarea, select {{ box-sizing: border-box; width: 100%; padding: .55rem; }}
    button {{ padding: .55rem .9rem; cursor: pointer; }}
    nav {{ display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }}
    .inline {{ display: inline; padding: 0; margin: 0; border: 0; background: transparent; }}
    .muted {{ color: #52606d; }}
    code {{ word-break: break-word; }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""


def _hidden(name: str, value: Any) -> str:
    return (
        f'<input type="hidden" name="{html.escape(name, quote=True)}" '
        f'value="{html.escape(str(value), quote=True)}">'
    )


def _logout_form(cookie_value: str) -> str:
    submission_id = f"LOGOUT-{uuid4().hex}"
    token = create_csrf_token(cookie_value, submission_id, SESSION_SECRET)
    return (
        '<form class="inline" method="post" action="/logout">'
        f'{_hidden("submission_id", submission_id)}'
        f'{_hidden("csrf_token", token)}'
        '<button type="submit">Log out</button></form>'
    )


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                raise BodyTooLarge
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise BodyTooLarge
    return bytes(body)


def _json_error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_body(error_code, message, retryable),
    )


def _intake_response(result: Any) -> JSONResponse:
    return JSONResponse(
        status_code=200 if result.idempotent_replay else 201,
        content={
            "case_reference": result.case_reference,
            "current_state": result.current_state,
            "idempotent_replay": result.idempotent_replay,
        },
    )


@lru_cache(maxsize=1)
def _configured_analysis_provider() -> AnalysisProvider:
    """Load exactly 1 accepted provider when the endpoint is invoked."""

    provider_name = os.environ.get("AI_ANALYSIS_PROVIDER", "")
    if provider_name == "fixture":
        fixture_file = os.environ.get("AI_ANALYSIS_FIXTURE_FILE", "")
        if not fixture_file:
            raise ProviderConfigurationError(
                "The fixture analysis provider is not configured."
            )
        return FixtureAnalysisProvider.from_json_file(fixture_file)
    if provider_name == "ollama":
        base_url = os.environ.get("AI_ANALYSIS_OLLAMA_BASE_URL", "")
        model_identifier = os.environ.get("AI_ANALYSIS_MODEL_IDENTIFIER", "")
        if not base_url or not model_identifier:
            raise ProviderConfigurationError(
                "The Ollama analysis provider is not configured."
            )
        return OllamaAnalysisProvider(
            base_url=base_url,
            model_identifier=model_identifier,
            model_name=os.environ.get(
                "AI_ANALYSIS_MODEL_NAME",
                "qwen3:4b-instruct",
            ),
        )
    raise ProviderConfigurationError("The analysis provider is not configured.")


@lru_cache(maxsize=1)
def _configured_policy_provider() -> PolicyProvider:
    """Load 1 bounded policy provider only when retrieval is invoked."""

    provider_name = os.environ.get("POLICY_RETRIEVAL_PROVIDER", "")
    if provider_name == "fixture":
        return FixturePolicyProvider(
            vector=[0.001] * 1024,
            answer="Jakarta employees may work remotely for up to 2 days per week with prior line-manager agreement.",
            citation_ids=("POL-REMOTE-01#0",),
        )
    if provider_name == "ollama":
        return OllamaPolicyProvider(
            base_url=os.environ.get("POLICY_OLLAMA_BASE_URL", ""),
            embedding_identifier=os.environ.get("POLICY_EMBEDDING_IDENTIFIER", ""),
            answer_identifier=os.environ.get("POLICY_ANSWER_IDENTIFIER", ""),
        )
    raise RetrievalProviderError("The policy retrieval provider is not configured.")


@app.get("/health")
def health() -> dict[str, str]:
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/api/v1/requests")
async def create_webhook_request(request: Request) -> Response:
    supplied_authorization = request.headers.get("authorization", "")
    expected_authorization = f"Bearer {WEBHOOK_TOKEN}"
    if not secrets.compare_digest(supplied_authorization, expected_authorization):
        return _json_error(
            401,
            "AUTHENTICATION_REQUIRED",
            "A valid local integration credential is required.",
        )

    try:
        raw_body = await _read_limited_body(request)
    except BodyTooLarge:
        return _json_error(
            413,
            "REQUEST_TOO_LARGE",
            "The request body exceeds the 256 KiB intake limit.",
        )

    try:
        parsed_body = json.loads(raw_body.decode("utf-8"))
        webhook_request = WebhookRequest.model_validate(parsed_body)
        intake_request = IntakeRequest(
            source_channel="WEBHOOK",
            external_request_id=webhook_request.external_request_id,
            subject=webhook_request.subject,
            message=webhook_request.message,
            attachment_metadata=webhook_request.attachments,
            received_at=webhook_request.received_at,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return _json_error(
            422,
            "INVALID_REQUEST",
            "The request does not match the primary intake contract.",
        )

    if intake_request.received_at > datetime.now(timezone.utc):
        return _json_error(
            422,
            "INVALID_REQUEST",
            "received_at cannot be later than the server acceptance time.",
        )

    try:
        result = create_or_replay_case(
            DATABASE_URL,
            intake_request,
            RequesterSelector(
                employee_reference=webhook_request.requester_reference
            ),
        )
    except RequesterNotAuthorized:
        return _json_error(
            403,
            "REQUESTER_NOT_AUTHORIZED",
            "The requester is not authorized for service-request intake.",
        )
    except IdempotencyConflict:
        return _json_error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "The request identifier was already used for different input.",
        )
    except psycopg.Error:
        return _json_error(
            503,
            "INTAKE_UNAVAILABLE",
            "The request could not be stored atomically. Retry later.",
            retryable=True,
        )

    return _intake_response(result)


@app.post("/internal/v1/cases/{case_id}/analysis-start")
async def start_case_analysis(case_id: UUID, request: Request) -> Response:
    supplied_authorization = request.headers.get("authorization", "")
    expected_authorization = f"Bearer {PRIMARY_WORKFLOW_TOKEN}"
    if not secrets.compare_digest(supplied_authorization, expected_authorization):
        return _json_error(
            401,
            "AUTHENTICATION_REQUIRED",
            "A valid local workflow credential is required.",
        )

    idempotency_key = request.headers.get("idempotency-key", "")
    if not re.fullmatch(r"[0-9a-f]{64}", idempotency_key):
        return _json_error(
            422,
            "INVALID_WORKFLOW_START",
            "A valid workflow-start idempotency key is required.",
        )

    try:
        raw_body = await _read_limited_body(request)
    except BodyTooLarge:
        return _json_error(
            413,
            "REQUEST_TOO_LARGE",
            "The workflow-start request exceeds the 256 KiB limit.",
        )

    try:
        parsed_body = json.loads(raw_body.decode("utf-8"))
        start_request = AnalysisStartRequest.model_validate(parsed_body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return _json_error(
            422,
            "INVALID_WORKFLOW_START",
            "The request does not match the workflow-start contract.",
        )

    try:
        result = start_or_replay_analysis(
            DATABASE_URL,
            case_id=case_id,
            case_reference=start_request.case_reference,
            expected_case_version=start_request.expected_case_version,
            trigger_event=start_request.trigger_event,
            idempotency_key=idempotency_key,
        )
    except AnalysisStartNotFound:
        return _json_error(
            404,
            "CASE_NOT_FOUND",
            "The workflow-start case was not found.",
        )
    except AnalysisStartConflict as error:
        return _json_error(
            409,
            "WORKFLOW_START_CONFLICT",
            str(error),
        )
    except psycopg.Error:
        return _json_error(
            503,
            "WORKFLOW_START_UNAVAILABLE",
            "The analysis start could not be committed. Retry later.",
            retryable=True,
        )

    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "1",
            "status": "ACCEPTED",
            "workflow_start_reference": result.workflow_start_reference,
            "case_reference": result.case_reference,
            "accepted_transition": "RECEIVED->ANALYZING",
            "current_state": result.current_state,
            "case_version": result.case_version,
            "idempotent_replay": result.idempotent_replay,
        },
    )


@app.post("/internal/v1/cases/{case_id}/analysis")
async def analyze_service_request(case_id: UUID, request: Request) -> Response:
    supplied_authorization = request.headers.get("authorization", "")
    expected_authorization = f"Bearer {PRIMARY_WORKFLOW_TOKEN}"
    if not secrets.compare_digest(supplied_authorization, expected_authorization):
        return _json_error(
            401,
            "AUTHENTICATION_REQUIRED",
            "A valid local workflow credential is required.",
        )

    try:
        raw_body = await _read_limited_body(request)
    except BodyTooLarge:
        return _json_error(
            413,
            "REQUEST_TOO_LARGE",
            "The analysis request exceeds the 256 KiB limit.",
        )

    try:
        parsed_body = json.loads(raw_body.decode("utf-8"))
        analysis_request = AnalysisRequest.model_validate(parsed_body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return _json_error(
            422,
            "INVALID_ANALYSIS_REQUEST",
            "The request does not match the AI-analysis entry contract.",
        )

    try:
        provider = _configured_analysis_provider()
        result = analyze_case(
            DATABASE_URL,
            provider,
            case_id=case_id,
            case_reference=analysis_request.case_reference,
            expected_case_version=analysis_request.expected_case_version,
            workflow_start_reference=(
                analysis_request.workflow_start_reference
            ),
        )
    except AnalysisNotFound:
        return _json_error(
            404,
            "CASE_NOT_FOUND",
            "The analysis case was not found.",
        )
    except AnalysisInProgress:
        return _json_error(
            409,
            "ANALYSIS_IN_PROGRESS",
            "An unexpired analysis attempt is already processing.",
            retryable=True,
        )
    except AnalysisConflict as error:
        return _json_error(
            409,
            "ANALYSIS_CONFLICT",
            str(error),
        )
    except ProviderConfigurationError:
        return _json_error(
            503,
            "ANALYSIS_PROVIDER_UNAVAILABLE",
            "The configured local analysis provider is unavailable.",
            retryable=True,
        )
    except psycopg.Error:
        return _json_error(
            503,
            "ANALYSIS_UNAVAILABLE",
            "The analysis outcome could not be committed. Retry later.",
            retryable=True,
        )

    response_body = {
        "schema_version": "1",
        "analysis_run_id": str(result.analysis_run_id),
        "case_reference": result.case_reference,
        "attempt_number": result.attempt_number,
        "outcome": result.outcome,
        "analysis_status": result.analysis_status,
        "validation_decision": result.validation_decision,
        "current_state": result.current_state,
        "case_version": result.case_version,
        "idempotent_replay": result.idempotent_replay,
        "provider_called": result.provider_called,
    }
    if result.outcome == "RETRYABLE_FAILURE":
        return JSONResponse(status_code=503, content=response_body)
    return JSONResponse(status_code=200, content=response_body)


@app.post("/internal/v1/cases/{case_id}/policy-retrieval")
async def retrieve_case_policy(case_id: UUID, request: Request) -> Response:
    supplied_authorization = request.headers.get("authorization", "")
    if not secrets.compare_digest(
        supplied_authorization,
        f"Bearer {PRIMARY_WORKFLOW_TOKEN}",
    ):
        return _json_error(401, "AUTHENTICATION_REQUIRED", "A valid local workflow credential is required.")
    try:
        raw_body = await _read_limited_body(request)
        command = PolicyRetrievalRequest.model_validate_json(raw_body)
    except BodyTooLarge:
        return _json_error(413, "REQUEST_TOO_LARGE", "The policy retrieval request exceeds 256 KiB.")
    except ValidationError:
        return _json_error(422, "INVALID_POLICY_RETRIEVAL_REQUEST", "The request does not match the policy-retrieval contract.")
    try:
        result = retrieve_policy(
            DATABASE_URL,
            _configured_policy_provider(),
            case_id=case_id,
            case_reference=command.case_reference,
            expected_case_version=command.expected_case_version,
            analysis_run_id=command.analysis_run_id,
        )
    except RetrievalNotFound:
        return _json_error(404, "CASE_NOT_FOUND", "The policy retrieval case was not found.")
    except RetrievalInProgress:
        return _json_error(409, "POLICY_RETRIEVAL_IN_PROGRESS", "A matching retrieval is already processing.", retryable=True)
    except RetrievalConflict as error:
        return _json_error(409, "POLICY_RETRIEVAL_CONFLICT", str(error))
    except RetrievalProviderError:
        return _json_error(503, "POLICY_RETRIEVAL_UNAVAILABLE", "The configured local policy provider is unavailable.", retryable=True)
    except psycopg.Error:
        return _json_error(503, "POLICY_RETRIEVAL_UNAVAILABLE", "The retrieval result could not be committed.", retryable=True)
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "1",
            "case_reference": result.case_reference,
            "outcome": result.outcome,
            "current_state": result.current_state,
            "case_version": result.case_version,
            "answer": result.answer,
            "citation_ids": list(result.citation_ids),
            "retrieved_chunk_ids": list(result.retrieved_chunk_ids),
            "idempotent_replay": result.idempotent_replay,
            "provider_called": result.provider_called,
        },
    )


@app.post("/api/v1/cases/{case_reference}/human-decisions")
async def decide_case(case_reference: str, request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _json_error(
            401,
            "AUTHENTICATION_REQUIRED",
            "A valid signed user session is required.",
        )
    cookie_value, actor_user_id = session

    try:
        raw_body = await _read_limited_body(request)
        command = HumanDecisionCommand.model_validate_json(raw_body)
    except BodyTooLarge:
        return _json_error(
            413,
            "REQUEST_TOO_LARGE",
            "The human-decision request exceeds the 256 KiB limit.",
        )
    except ValidationError:
        return _json_error(
            422,
            "INVALID_HUMAN_DECISION",
            "The request does not match the human-decision command contract.",
        )

    supplied_csrf = request.headers.get("x-csrf-token", "")
    expected_csrf = create_csrf_token(
        cookie_value,
        str(command.command_id),
        SESSION_SECRET,
    )
    if not secrets.compare_digest(supplied_csrf, expected_csrf):
        return _json_error(
            403,
            "INVALID_CSRF_TOKEN",
            "The human-decision security token is invalid.",
        )

    try:
        result = execute_human_decision(
            DATABASE_URL,
            case_reference=case_reference,
            actor_user_id=actor_user_id,
            command=command,
        )
    except HumanDecisionNotFound:
        return _json_error(
            404,
            "CASE_NOT_FOUND",
            "The human-decision case was not found.",
        )
    except HumanDecisionNotAuthorized as error:
        return _json_error(
            403,
            "HUMAN_DECISION_NOT_AUTHORIZED",
            str(error),
        )
    except HumanDecisionInvalid as error:
        return _json_error(
            422,
            "INVALID_HUMAN_DECISION",
            str(error),
        )
    except HumanDecisionConflict as error:
        return _json_error(
            409,
            "HUMAN_DECISION_CONFLICT",
            str(error),
        )
    except psycopg.Error:
        return _json_error(
            503,
            "HUMAN_DECISION_UNAVAILABLE",
            "The human decision could not be committed atomically.",
            retryable=True,
        )

    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "1",
            "human_decision_reference": result.human_decision_reference,
            "case_reference": result.case_reference,
            "action": result.action,
            "current_state": result.current_state,
            "case_version": result.case_version,
            "idempotent_replay": result.idempotent_replay,
        },
    )


@app.post("/internal/v1/cases/{case_id}/human-resume")
async def accept_human_resume(case_id: UUID, request: Request) -> Response:
    supplied_authorization = request.headers.get("authorization", "")
    if not secrets.compare_digest(
        supplied_authorization,
        f"Bearer {PRIMARY_WORKFLOW_TOKEN}",
    ):
        return _json_error(
            401,
            "AUTHENTICATION_REQUIRED",
            "A valid local workflow credential is required.",
        )
    idempotency_key = request.headers.get("idempotency-key", "")
    if re.fullmatch(r"[0-9a-f]{64}", idempotency_key) is None:
        return _json_error(
            422,
            "INVALID_HUMAN_RESUME",
            "A valid human-resume idempotency key is required.",
        )
    try:
        raw_body = await _read_limited_body(request)
        command = HumanResumeCommand.model_validate_json(raw_body)
    except BodyTooLarge:
        return _json_error(
            413,
            "REQUEST_TOO_LARGE",
            "The human-resume request exceeds the 256 KiB limit.",
        )
    except ValidationError:
        return _json_error(
            422,
            "INVALID_HUMAN_RESUME",
            "The request does not match the human-resume contract.",
        )
    try:
        result = acknowledge_human_resume(
            DATABASE_URL,
            case_id=case_id,
            command=command,
            idempotency_key=idempotency_key,
        )
    except HumanResumeNotFound:
        return _json_error(
            404,
            "HUMAN_DECISION_NOT_FOUND",
            "The committed human decision was not found.",
        )
    except HumanResumeConflict as error:
        return _json_error(
            409,
            "HUMAN_RESUME_CONFLICT",
            str(error),
        )
    except psycopg.Error:
        return _json_error(
            503,
            "HUMAN_RESUME_UNAVAILABLE",
            "The human-resume acknowledgement could not be committed.",
            retryable=True,
        )
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "1",
            "status": "ACCEPTED",
            "human_resume_reference": result.human_resume_reference,
            "case_reference": result.case_reference,
            "resume_route": result.resume_route,
            "current_state": result.current_state,
            "case_version": result.case_version,
            "idempotent_replay": result.idempotent_replay,
        },
    )


@app.post("/internal/v1/recovery/sweep")
async def run_recovery_sweep(request: Request) -> Response:
    supplied_authorization = request.headers.get("authorization", "")
    expected_authorization = f"Bearer {PRIMARY_WORKFLOW_TOKEN}"
    if not secrets.compare_digest(supplied_authorization, expected_authorization):
        return _json_error(
            401,
            "AUTHENTICATION_REQUIRED",
            "A valid local workflow credential is required.",
        )
    try:
        raw_body = await _read_limited_body(request)
    except BodyTooLarge:
        return _json_error(
            413,
            "REQUEST_TOO_LARGE",
            "The recovery command exceeds the 256 KiB limit.",
        )
    try:
        command = RecoverySweepRequest.model_validate_json(raw_body)
    except ValidationError:
        return _json_error(
            422,
            "INVALID_RECOVERY_COMMAND",
            "The request does not match the recovery v1 contract.",
        )
    try:
        result = recover_expired_claims(
            DATABASE_URL,
            lease_seconds=command.lease_seconds,
            retry_delay_seconds=command.retry_delay_seconds,
            limit=command.limit,
        )
    except psycopg.Error:
        return _json_error(
            503,
            "RECOVERY_UNAVAILABLE",
            "The recovery sweep could not be committed.",
            retryable=True,
        )
    recovered_by_type: dict[str, int] = {}
    for recovered in result.recovered_claims:
        recovered_by_type[recovered.message_type] = (
            recovered_by_type.get(recovered.message_type, 0) + 1
        )
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": "1",
            "status": "COMPLETED",
            "recovered_claims": len(result.recovered_claims),
            "pending_retries": result.pending_retries,
            "terminal_failures": result.terminal_failures,
            "recovered_by_type": recovered_by_type,
        },
    )


@app.get("/api/v1/operations/summary")
def operations_summary(request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _json_error(
            401,
            "AUTHENTICATION_REQUIRED",
            "An administrator portal session is required.",
        )
    _cookie_value, user_id = session
    try:
        user = load_portal_user(DATABASE_URL, user_id)
        if user is None or "ADMIN" not in set(user["roles"]):
            return _json_error(
                403,
                "ADMIN_REQUIRED",
                "Administrator access is required.",
            )
        summary = load_operations_summary(DATABASE_URL)
    except psycopg.Error:
        return _json_error(
            503,
            "OPERATIONS_UNAVAILABLE",
            "Operational evidence is temporarily unavailable.",
            retryable=True,
        )
    return JSONResponse(status_code=200, content=summary)


@app.get("/login")
def login_form(request: Request) -> Response:
    next_path = _safe_next_path(request.query_params.get("next"))
    if _web_session(request) is not None:
        return RedirectResponse(url=next_path, status_code=303)
    nonce = uuid4().hex
    csrf_token = create_login_csrf_token(nonce, SESSION_SECRET)
    response = HTMLResponse(
        content=_portal_page(
            "Local portal login",
            f"""
<h1>AI Service Request Automation</h1>
<p class="muted">Local fictional-user portal</p>
<form method="post" action="/login">
  {_hidden("csrf_token", csrf_token)}
  {_hidden("next", next_path)}
  <label>Employee reference
    <input name="employee_reference" maxlength="50" autocomplete="username" required>
  </label>
  <label>Password
    <input type="password" name="password" maxlength="256" autocomplete="current-password" required>
  </label>
  <button type="submit">Log in</button>
</form>
""",
        )
    )
    response.set_cookie(
        LOGIN_NONCE_COOKIE_NAME,
        nonce,
        max_age=600,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/login")
async def login(request: Request) -> Response:
    if not request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        return _browser_error(422, "The login form format is invalid.")
    try:
        raw_body = await _read_limited_body(request)
        form_values = urllib.parse.parse_qs(
            raw_body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=4,
        )
        if set(form_values) != {
            "csrf_token",
            "employee_reference",
            "next",
            "password",
        }:
            raise ValueError("unexpected login fields")
        supplied_csrf = _single_form_value(form_values, "csrf_token")
        employee_reference = _single_form_value(
            form_values, "employee_reference"
        )
        next_path = _safe_next_path(_single_form_value(form_values, "next"))
        password = _single_form_value(form_values, "password")
    except BodyTooLarge:
        return _browser_error(413, "The login form is too large.")
    except (UnicodeDecodeError, ValueError):
        return _browser_error(422, "The login form is invalid.")

    nonce = request.cookies.get(LOGIN_NONCE_COOKIE_NAME, "")
    if LOGIN_NONCE_PATTERN.fullmatch(nonce) is None or not secrets.compare_digest(
        supplied_csrf,
        create_login_csrf_token(nonce, SESSION_SECRET),
    ):
        return _browser_error(403, "The login security token is invalid.")
    try:
        user = authenticate_portal_user(
            DATABASE_URL, employee_reference, password
        )
    except psycopg.Error:
        return _browser_error(503, "The login service is temporarily unavailable.")
    if user is None:
        return _browser_error(401, "The employee reference or password is invalid.")

    cookie_value = create_session_cookie(user["user_id"], SESSION_SECRET)
    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        max_age=8 * 60 * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(LOGIN_NONCE_COOKIE_NAME, path="/")
    return response


@app.post("/logout")
async def logout(request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _login_redirect("/cases")
    cookie_value, _user_id = session
    if not request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        return _browser_error(422, "The logout form format is invalid.")
    try:
        raw_body = await _read_limited_body(request)
        form_values = urllib.parse.parse_qs(
            raw_body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=2,
        )
        if set(form_values) != {"csrf_token", "submission_id"}:
            raise ValueError("unexpected logout fields")
        submission_id = _single_form_value(form_values, "submission_id")
        supplied_csrf = _single_form_value(form_values, "csrf_token")
    except BodyTooLarge:
        return _browser_error(413, "The logout form is too large.")
    except (UnicodeDecodeError, ValueError):
        return _browser_error(422, "The logout form is invalid.")
    expected_csrf = create_csrf_token(cookie_value, submission_id, SESSION_SECRET)
    if (
        LOGOUT_SUBMISSION_PATTERN.fullmatch(submission_id) is None
        or not secrets.compare_digest(supplied_csrf, expected_csrf)
    ):
        return _browser_error(403, "The logout security token is invalid.")
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/requests/new")
def new_request_form(request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _login_redirect()

    cookie_value, requester_id = session
    try:
        authorized = requester_is_authorized(DATABASE_URL, requester_id)
    except psycopg.Error:
        return _browser_error(503, "The request service is temporarily unavailable.")
    if not authorized:
        return _browser_error(403, "This account cannot submit service requests.")

    submission_id = f"WEB-{uuid4()}"
    csrf_token = create_csrf_token(cookie_value, submission_id, SESSION_SECRET)
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>New service request</title></head>
<body>
  <main>
    <h1>New service request</h1>
    <form method="post" action="/requests">
      <input type="hidden" name="submission_id" value="{submission_id}">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <label>Subject <input name="subject" maxlength="200" required></label>
      <label>Message <textarea name="message" maxlength="20000" required></textarea></label>
      <button type="submit">Submit request</button>
    </form>
  </main>
</body>
</html>"""
    )


def _single_form_value(
    form_values: dict[str, list[str]],
    field_name: str,
) -> str:
    values = form_values.get(field_name)
    if values is None or len(values) != 1:
        raise ValueError(f"{field_name} must occur exactly once")
    return values[0]


def _optional_form_value(
    form_values: dict[str, list[str]], field_name: str
) -> str | None:
    value = _single_form_value(form_values, field_name).strip()
    return value or None


def _decision_form_start(
    cookie_value: str,
    case_reference: str,
    case_version: int,
    action: str,
) -> tuple[str, str]:
    command_id = str(uuid4())
    csrf_token = create_csrf_token(cookie_value, command_id, SESSION_SECRET)
    start = (
        f'<form method="post" action="/cases/{html.escape(case_reference, quote=True)}/actions">'
        f'{_hidden("command_id", command_id)}'
        f'{_hidden("expected_case_version", case_version)}'
        f'{_hidden("action", action)}'
        f'{_hidden("csrf_token", csrf_token)}'
    )
    return start, "</form>"


def _case_action_forms(
    user: dict[str, Any], case: dict[str, Any], cookie_value: str
) -> str:
    forms: list[str] = []
    role_set = set(user["roles"])
    case_reference = case["case_reference"]
    case_version = case["version"]
    if (
        "REQUESTER" in role_set
        and case["requester_id"] == user["user_id"]
        and case["current_state"] == "NEEDS_INFORMATION"
    ):
        start, end = _decision_form_start(
            cookie_value,
            case_reference,
            case_version,
            "SUBMIT_INFORMATION",
        )
        forms.append(
            start
            + "<h2>Supply missing information</h2>"
            + '<label>Additional information<textarea name="information" maxlength="4000" required></textarea></label>'
            + '<button type="submit">Continue analysis</button>'
            + end
        )
    if "SERVICE_AGENT" in role_set and case["current_state"] == "NEEDS_REVIEW":
        start, end = _decision_form_start(
            cookie_value, case_reference, case_version, "CONFIRM_REVIEW"
        )
        forms.append(
            start
            + "<h2>Confirm AI proposal</h2>"
            + '<label>Review note<textarea name="note" maxlength="1000"></textarea></label>'
            + '<button type="submit">Confirm proposal</button>'
            + end
        )
        start, end = _decision_form_start(
            cookie_value, case_reference, case_version, "CORRECT_REVIEW"
        )
        correction_inputs = "".join(
            f'<label>{html.escape(field_name.replace("_", " ").title())}'
            f'<input name="{html.escape(field_name, quote=True)}" maxlength="4000"></label>'
            for field_name in CORRECTION_FIELD_NAMES
        )
        forms.append(
            start
            + "<h2>Correct proposal</h2>"
            + '<label>Request type<select name="request_type" required>'
            + '<option value="policy_question">Policy question</option>'
            + '<option value="incident_report">Incident report</option>'
            + '<option value="access_request">Access request</option>'
            + '<option value="data_change_request">Data change request</option>'
            + '<option value="status_request">Status request</option>'
            + "</select></label>"
            + '<label>Corrected summary<input name="summary" maxlength="500" required></label>'
            + '<label>Review note<textarea name="note" maxlength="1000"></textarea></label>'
            + correction_inputs
            + '<button type="submit">Save correction</button>'
            + end
        )
        start, end = _decision_form_start(
            cookie_value, case_reference, case_version, "REJECT_REVIEW"
        )
        forms.append(
            start
            + "<h2>Reject after review</h2>"
            + '<label>Reason<textarea name="note" maxlength="1000" required></textarea></label>'
            + '<button type="submit">Reject request</button>'
            + end
        )
    if (
        "APPROVER" in role_set
        and case["assigned_approver_id"] == user["user_id"]
        and case["current_state"] == "PENDING_APPROVAL"
    ):
        start, end = _decision_form_start(
            cookie_value, case_reference, case_version, "APPROVE_REQUEST"
        )
        forms.append(
            start
            + "<h2>Approve request</h2>"
            + '<label>Decision note<textarea name="note" maxlength="1000"></textarea></label>'
            + '<button type="submit">Approve</button>'
            + end
        )
        start, end = _decision_form_start(
            cookie_value, case_reference, case_version, "REJECT_REQUEST"
        )
        forms.append(
            start
            + "<h2>Reject approval</h2>"
            + '<label>Reason<textarea name="note" maxlength="1000" required></textarea></label>'
            + '<button type="submit">Reject</button>'
            + end
        )
    return "".join(forms)


@app.post("/requests")
async def create_form_request(request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _login_redirect()
    cookie_value, requester_id = session

    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return _browser_error(422, "The submitted form format is invalid.")

    try:
        raw_body = await _read_limited_body(request)
        form_values = urllib.parse.parse_qs(
            raw_body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=4,
        )
        if set(form_values) != {
            "submission_id",
            "csrf_token",
            "subject",
            "message",
        }:
            raise ValueError("unexpected form fields")
        submission_id = _single_form_value(form_values, "submission_id")
        supplied_csrf = _single_form_value(form_values, "csrf_token")
        subject = _single_form_value(form_values, "subject")
        message = _single_form_value(form_values, "message")
    except BodyTooLarge:
        return _browser_error(413, "The submitted request is too large.")
    except (UnicodeDecodeError, ValueError):
        return _browser_error(422, "The submitted form is invalid.")

    if not WEB_SUBMISSION_PATTERN.fullmatch(submission_id):
        return _browser_error(422, "The submission identifier is invalid.")
    expected_csrf = create_csrf_token(cookie_value, submission_id, SESSION_SECRET)
    if not secrets.compare_digest(supplied_csrf, expected_csrf):
        return _browser_error(403, "The form security token is invalid.")

    try:
        intake_request = IntakeRequest(
            source_channel="WEB",
            external_request_id=submission_id,
            subject=subject,
            message=message,
            attachment_metadata=[],
            received_at=datetime.now(timezone.utc),
        )
        result = create_or_replay_case(
            DATABASE_URL,
            intake_request,
            RequesterSelector(user_id=requester_id),
        )
    except ValidationError:
        return _browser_error(422, "The submitted form is invalid.")
    except RequesterNotAuthorized:
        return _browser_error(403, "This account cannot submit service requests.")
    except IdempotencyConflict:
        return _browser_error(
            409,
            "The submission identifier was already used for different input.",
        )
    except psycopg.Error:
        return _browser_error(503, "The request could not be stored. Retry later.")

    return RedirectResponse(
        url=f"/cases/{result.case_reference}",
        status_code=303,
    )


@app.get("/cases")
def case_list(request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _login_redirect("/cases")
    cookie_value, user_id = session
    try:
        user = load_portal_user(DATABASE_URL, user_id)
        cases = list_visible_cases(DATABASE_URL, user) if user is not None else []
    except psycopg.Error:
        return _browser_error(503, "The case portal is temporarily unavailable.")
    if user is None:
        return _browser_error(403, "This account is not active.")

    rows = "".join(
        "<tr>"
        f'<td><a href="/cases/{html.escape(case["case_reference"], quote=True)}">'
        f'{html.escape(case["case_reference"])}</a></td>'
        f'<td>{html.escape(case["subject"])}</td>'
        f'<td>{html.escape(case["request_type"] or "Pending")}</td>'
        f'<td>{html.escape(case["current_state"])}</td>'
        "</tr>"
        for case in cases
    )
    if not rows:
        rows = '<tr><td colspan="4">No visible cases.</td></tr>'
    new_request_link = (
        '<a href="/requests/new">New request</a>'
        if "REQUESTER" in set(user["roles"])
        else ""
    )
    operations_link = (
        '<a href="/operations">Operations</a>'
        if "ADMIN" in set(user["roles"])
        else ""
    )
    return HTMLResponse(
        content=_portal_page(
            "Visible service requests",
            f"""
<nav>
  <strong>{html.escape(user['display_name'])}</strong>
  <span class="muted">{html.escape(', '.join(user['roles']))}</span>
  {new_request_link}
  {operations_link}
  {_logout_form(cookie_value)}
</nav>
<h1>Visible service requests</h1>
<table>
  <thead><tr><th>Case</th><th>Subject</th><th>Type</th><th>State</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
""",
        )
    )


@app.get("/operations")
def operations_dashboard(request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _login_redirect("/cases")
    cookie_value, user_id = session
    try:
        user = load_portal_user(DATABASE_URL, user_id)
        if user is None or "ADMIN" not in set(user["roles"]):
            return _browser_error(403, "Administrator access is required.")
        summary = load_operations_summary(DATABASE_URL)
    except psycopg.Error:
        return _browser_error(503, "Operational evidence is temporarily unavailable.")

    totals = "".join(
        f"<tr><th>{html.escape(key.replace('_', ' ').title())}</th>"
        f"<td>{int(value)}</td></tr>"
        for key, value in summary["totals"].items()
    )
    cases = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{int(value)}</td></tr>"
        for key, value in summary["cases_by_state"].items()
    ) or '<tr><td colspan="2">No cases.</td></tr>'
    outbox = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{int(value)}</td></tr>"
        for key, value in summary["outbox_by_type_and_status"].items()
    ) or '<tr><td colspan="2">No delivery intents.</td></tr>'
    return HTMLResponse(
        content=_portal_page(
            "Local operations",
            f"""
<nav>
  <a href="/cases">Visible service requests</a>
  {_logout_form(cookie_value)}
</nav>
<h1>Local operational evidence</h1>
<p class="muted">Derived from durable primary records; no AI call.</p>
<h2>Totals</h2><table>{totals}</table>
<h2>Cases by state</h2><table>{cases}</table>
<h2>Outbox by type and status</h2><table>{outbox}</table>
""",
        )
    )


@app.post("/cases/{case_reference}/actions")
async def submit_case_action(case_reference: str, request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _login_redirect("/cases")
    cookie_value, actor_user_id = session
    if CASE_REFERENCE_PATTERN.fullmatch(case_reference) is None:
        return _browser_error(404, "The requested case was not found.")
    if not request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        return _browser_error(422, "The action form format is invalid.")

    try:
        raw_body = await _read_limited_body(request)
        form_values = urllib.parse.parse_qs(
            raw_body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=24,
        )
        action = _single_form_value(form_values, "action")
        base_fields = {
            "action",
            "command_id",
            "csrf_token",
            "expected_case_version",
        }
        action_fields = {
            "SUBMIT_INFORMATION": {"information"},
            "CONFIRM_REVIEW": {"note"},
            "REJECT_REVIEW": {"note"},
            "APPROVE_REQUEST": {"note"},
            "REJECT_REQUEST": {"note"},
            "CORRECT_REVIEW": {
                "note",
                "request_type",
                "summary",
                *CORRECTION_FIELD_NAMES,
            },
        }
        if action not in action_fields or set(form_values) != (
            base_fields | action_fields[action]
        ):
            raise ValueError("unexpected action fields")
        command_id = _single_form_value(form_values, "command_id")
        supplied_csrf = _single_form_value(form_values, "csrf_token")
        expected_case_version = int(
            _single_form_value(form_values, "expected_case_version")
        )
        command_values: dict[str, Any] = {}
        if action == "SUBMIT_INFORMATION":
            command_values["information"] = _single_form_value(
                form_values, "information"
            )
        elif action == "CORRECT_REVIEW":
            command_values.update(
                {
                    "note": _optional_form_value(form_values, "note"),
                    "request_type": _single_form_value(
                        form_values, "request_type"
                    ),
                    "summary": _single_form_value(form_values, "summary"),
                    "fields": {
                        field_name: _optional_form_value(
                            form_values, field_name
                        )
                        for field_name in CORRECTION_FIELD_NAMES
                    },
                }
            )
        else:
            command_values["note"] = _optional_form_value(form_values, "note")
        command = HumanDecisionCommand(
            schema_version="1",
            command_id=UUID(command_id),
            expected_case_version=expected_case_version,
            action=action,
            **command_values,
        )
    except BodyTooLarge:
        return _browser_error(413, "The action form is too large.")
    except (UnicodeDecodeError, ValueError, ValidationError):
        return _browser_error(422, "The action form is invalid.")

    expected_csrf = create_csrf_token(cookie_value, command_id, SESSION_SECRET)
    if not secrets.compare_digest(supplied_csrf, expected_csrf):
        return _browser_error(403, "The action security token is invalid.")
    try:
        execute_human_decision(
            DATABASE_URL,
            case_reference=case_reference,
            actor_user_id=actor_user_id,
            command=command,
        )
    except HumanDecisionNotFound:
        return _browser_error(404, "The requested case was not found.")
    except HumanDecisionNotAuthorized as error:
        return _browser_error(403, str(error))
    except HumanDecisionInvalid as error:
        return _browser_error(422, str(error))
    except HumanDecisionConflict as error:
        return _browser_error(409, str(error))
    except psycopg.Error:
        return _browser_error(503, "The case action could not be committed.")
    redirect_target = (
        "/cases"
        if action in {"CONFIRM_REVIEW", "CORRECT_REVIEW", "REJECT_REVIEW"}
        else f"/cases/{case_reference}"
    )
    return RedirectResponse(url=redirect_target, status_code=303)


@app.get("/cases/{case_reference}")
def case_receipt(case_reference: str, request: Request) -> Response:
    session = _web_session(request)
    if session is None:
        return _login_redirect("/cases")
    cookie_value, user_id = session

    if not CASE_REFERENCE_PATTERN.fullmatch(case_reference):
        return _browser_error(404, "The requested case was not found.")

    try:
        user = load_portal_user(DATABASE_URL, user_id)
        case = (
            load_visible_case(DATABASE_URL, user, case_reference)
            if user is not None
            else None
        )
    except psycopg.Error:
        return _browser_error(503, "The request service is temporarily unavailable.")
    if user is None or case is None:
        return _browser_error(404, "The requested case was not found.")

    events = "".join(
        "<li>"
        f'<code>{html.escape(event["event_type"])}</code> '
        f'{html.escape(event["reason"])}'
        "</li>"
        for event in case["events"]
    )
    action_forms = _case_action_forms(user, case, cookie_value)
    return HTMLResponse(
        content=_portal_page(
            f"Service request {case_reference}",
            f"""
<nav>
  <a href="/cases">All visible cases</a>
  {_logout_form(cookie_value)}
</nav>
<h1>{html.escape(case['case_reference'])}</h1>
<section>
  <p><strong>{html.escape(case['subject'])}</strong></p>
  <p>{html.escape(case['original_message'])}</p>
  <p>Requester: {html.escape(case['requester_name'])}
     ({html.escape(case['requester_reference'])})</p>
  <p>Type: {html.escape(case['request_type'] or 'Pending')}</p>
  <p>State: <code>{html.escape(case['current_state'])}</code></p>
</section>
{action_forms}
<section><h2>Audit history</h2><ol>{events}</ol></section>
""",
        )
    )
