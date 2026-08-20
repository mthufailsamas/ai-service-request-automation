"""Bounded AI-analysis domain service with deterministic fixture support."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


PROMPT_CONTRACT_VERSION = "analysis-v1"
MAX_INPUT_CHARACTERS = 8_000
MAX_PROPOSAL_BYTES = 32 * 1024
MAX_PROVIDER_ATTEMPTS = 2
DEFAULT_LEASE_SECONDS = 240
OLLAMA_MODEL_NAME = "qwen3:4b-instruct"
OLLAMA_TIMEOUT_SECONDS = 180
MAX_OLLAMA_RESPONSE_BYTES = 1024 * 1024

FIELD_NAMES = (
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

REQUEST_TYPE_MAP = {
    "policy_question": "POLICY_QUESTION",
    "incident_report": "INCIDENT_REPORT",
    "access_request": "ACCESS_REQUEST",
    "data_change_request": "DATA_CHANGE_REQUEST",
    "status_request": "STATUS_REQUEST",
}


def _nullable_string_schema() -> dict[str, Any]:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


OLLAMA_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "request_type": {
            "type": "string",
            "enum": list(REQUEST_TYPE_MAP),
        },
        "summary": {"type": "string"},
        "fields": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                field_name: _nullable_string_schema()
                for field_name in FIELD_NAMES
            },
            "required": list(FIELD_NAMES),
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string", "enum": list(FIELD_NAMES)},
                    "quote": {"type": "string"},
                },
                "required": ["field", "quote"],
            },
        },
    },
    "required": ["request_type", "summary", "fields", "evidence"],
}

OLLAMA_SYSTEM_PROMPT = """You analyze internal service requests.

Return only the JSON object required by the supplied schema.

Allowed request types and required fields:
- policy_question: policy_topic, question
- incident_report: affected_service, incident_description, impact, urgency
- access_request: target_system, requested_access_level, business_reason, approver_id
- data_change_request: target_system, record_reference, requested_changes, business_reason, approver_id
- status_request: case_reference

Rules:
1. Extract only information explicitly present in the subject or message.
2. Use null when a field is absent. Never invent an identifier or default value.
3. Keep fields unrelated to the selected request type null.
4. affected_service is the named business application or service experiencing
   the failure. Prefer it over a device or tool when both are mentioned.
5. business_reason is an explicit justification or supporting basis introduced
   by wording such as because, so that, based on, karena, agar, or berdasarkan.
6. Evidence quotes must be exact text spans copied from the subject or message.
7. Keep the summary short and operational.
"""

_OLLAMA_MODEL_CALL_LOCK = threading.Lock()

REQUIRED_FIELDS = {
    "policy_question": ("policy_topic", "question"),
    "incident_report": (
        "affected_service",
        "incident_description",
        "impact",
        "urgency",
    ),
    "access_request": (
        "target_system",
        "requested_access_level",
        "business_reason",
        "approver_id",
    ),
    "data_change_request": (
        "target_system",
        "record_reference",
        "requested_changes",
        "business_reason",
        "approver_id",
    ),
    "status_request": ("case_reference",),
}

IMPACT_VALUES = {"low", "medium", "high", "critical"}
WORKFLOW_REFERENCE_PATTERN = re.compile(r"^WFSTART-([1-9][0-9]*)$")
HUMAN_RESUME_REFERENCE_PATTERN = re.compile(r"^HDRESUME-([1-9][0-9]*)$")
HUMAN_DECISION_REFERENCE_PATTERN = re.compile(r"^HD-([1-9][0-9]*)$")


class AnalysisFields(BaseModel):
    """All 13 nullable fields required by the accepted proposal contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    policy_topic: str | None = Field(max_length=200)
    question: str | None = Field(max_length=2_000)
    affected_service: str | None = Field(max_length=200)
    incident_description: str | None = Field(max_length=2_000)
    impact: str | None = Field(max_length=20)
    urgency: str | None = Field(max_length=20)
    target_system: str | None = Field(max_length=200)
    requested_access_level: str | None = Field(max_length=80)
    business_reason: str | None = Field(max_length=2_000)
    approver_id: str | None = Field(max_length=50)
    record_reference: str | None = Field(max_length=100)
    requested_changes: str | None = Field(max_length=4_000)
    case_reference: str | None = Field(max_length=32)


class EvidenceItem(BaseModel):
    """One exact source quote supporting one proposed field."""

    model_config = ConfigDict(extra="forbid", strict=True)

    field_name: Literal[
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
    ] = Field(alias="field")
    quote: str = Field(min_length=1, max_length=500)

    @field_validator("quote")
    @classmethod
    def require_nonblank_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence quote must not be blank")
        return value


class AnalysisProposal(BaseModel):
    """Exact untrusted provider proposal before deterministic checks."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_type: Literal[
        "policy_question",
        "incident_report",
        "access_request",
        "data_change_request",
        "status_request",
    ]
    summary: str = Field(min_length=1, max_length=500)
    extracted_fields: AnalysisFields = Field(alias="fields")
    evidence: list[EvidenceItem] = Field(max_length=13)

    @field_validator("summary")
    @classmethod
    def require_nonblank_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be blank")
        return value


@dataclass(frozen=True)
class ProviderResult:
    """Bounded result returned by an analysis provider."""

    model_name: str
    model_identifier: str
    proposal: Any
    wall_time_ms: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class FixtureResponse:
    """One explicit result or retryable failure in a fixture sequence."""

    kind: Literal["result", "retryable_failure"]
    proposal: Any = None
    wall_time_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    delay_ms: int = 0
    error_code: str = "FIXTURE_TRANSIENT_FAILURE"
    message: str = "The controlled fixture reported a transient failure."


class AnalysisProvider(Protocol):
    """Small provider interface fixed by the v1 contract."""

    model_name: str
    model_identifier: str

    def analyze(self, subject: str, original_message: str) -> ProviderResult:
        """Return one proposal or raise one classified provider failure."""


class RetryableProviderError(Exception):
    """A transport-like provider failure eligible for one bounded retry."""

    def __init__(self, error_code: str, message: str, wall_time_ms: int = 0):
        super().__init__(message)
        self.error_code = error_code
        self.wall_time_ms = wall_time_ms


class AnalysisNotFound(Exception):
    """The stable case reference does not identify an existing case."""


class AnalysisConflict(Exception):
    """The case, version, event, or provider identity does not permit analysis."""


class AnalysisInProgress(Exception):
    """Another invocation already owns the unexpired processing attempt."""


class ProviderConfigurationError(Exception):
    """The selected analysis provider is not safely configured."""


class FixtureConfigurationError(ProviderConfigurationError):
    """A controlled fixture file is incomplete or internally inconsistent."""


class OllamaConfigurationError(ProviderConfigurationError):
    """The accepted local Ollama provider configuration is invalid."""


@dataclass(frozen=True)
class AnalysisExecution:
    """Small internal acknowledgement for one analysis invocation."""

    analysis_run_id: UUID
    case_reference: str
    attempt_number: int
    outcome: str
    analysis_status: str
    validation_decision: str | None
    current_state: str
    case_version: int
    idempotent_replay: bool
    provider_called: bool


@dataclass(frozen=True)
class _ClaimedAttempt:
    analysis_run_id: UUID
    case_id: UUID
    case_reference: str
    expected_case_version: int
    input_sha256: str
    attempt_number: int
    subject: str
    original_message: str
    trigger_reference: str | None


@dataclass(frozen=True)
class _ValidationPlan:
    analysis_status: str
    decision: str
    missing_fields: tuple[str, ...]
    rule_results: tuple[dict[str, Any], ...]
    reason: str
    proposal: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    request_type: str | None = None
    summary: str | None = None
    details: dict[str, Any] | None = None


def canonical_input_sha256(subject: str, original_message: str) -> str:
    """Hash the exact accepted input with canonical JSON serialization."""

    canonical = json.dumps(
        {"original_message": original_message, "subject": subject},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _nonnegative_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FixtureConfigurationError(
            f"{field_name} must be a nonnegative integer"
        )
    return value


class FixtureAnalysisProvider:
    """Thread-safe provider using only explicit fictional response sequences."""

    def __init__(
        self,
        fixtures: dict[str, list[FixtureResponse]],
        *,
        model_name: str = "fixture-provider",
        model_identifier: str = "fixture-ai-analysis-v1",
    ) -> None:
        if not model_name.strip() or len(model_name) > 100:
            raise FixtureConfigurationError("fixture model_name is invalid")
        if not model_identifier.strip() or len(model_identifier) > 100:
            raise FixtureConfigurationError("fixture model_identifier is invalid")
        if any(not responses for responses in fixtures.values()):
            raise FixtureConfigurationError(
                "every configured fixture must contain at least 1 response"
            )
        self.model_name = model_name
        self.model_identifier = model_identifier
        self._fixtures = {key: list(value) for key, value in fixtures.items()}
        self._call_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_json_file(cls, path: str | Path) -> FixtureAnalysisProvider:
        """Load a strict, read-only fictional fixture configuration."""

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON constant {value} is forbidden")

        try:
            document = json.loads(
                Path(path).read_text(encoding="utf-8"),
                parse_constant=reject_constant,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise FixtureConfigurationError(
                "the fixture provider file could not be loaded"
            ) from error

        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "model_name",
            "model_identifier",
            "fixtures",
        }:
            raise FixtureConfigurationError("the fixture file shape is invalid")
        if document["schema_version"] != "1":
            raise FixtureConfigurationError("the fixture schema version is invalid")
        if not isinstance(document["fixtures"], list):
            raise FixtureConfigurationError("fixtures must be an array")

        configured: dict[str, list[FixtureResponse]] = {}
        for item in document["fixtures"]:
            if not isinstance(item, dict) or set(item) != {
                "subject",
                "original_message",
                "responses",
            }:
                raise FixtureConfigurationError("a fixture input shape is invalid")
            if not isinstance(item["subject"], str) or not isinstance(
                item["original_message"], str
            ):
                raise FixtureConfigurationError("fixture input must be text")
            if not isinstance(item["responses"], list) or not item["responses"]:
                raise FixtureConfigurationError("fixture responses must be nonempty")

            responses: list[FixtureResponse] = []
            for response in item["responses"]:
                if not isinstance(response, dict) or "kind" not in response:
                    raise FixtureConfigurationError("a fixture response is invalid")
                kind = response["kind"]
                common_keys = {"kind", "wall_time_ms", "delay_ms"}
                if kind == "result":
                    allowed = common_keys | {
                        "proposal",
                        "input_tokens",
                        "output_tokens",
                    }
                    if set(response) - allowed or "proposal" not in response:
                        raise FixtureConfigurationError(
                            "a fixture result shape is invalid"
                        )
                    responses.append(
                        FixtureResponse(
                            kind="result",
                            proposal=response["proposal"],
                            wall_time_ms=_nonnegative_integer(
                                response.get("wall_time_ms", 0), "wall_time_ms"
                            ),
                            input_tokens=_nonnegative_integer(
                                response.get("input_tokens", 0), "input_tokens"
                            ),
                            output_tokens=_nonnegative_integer(
                                response.get("output_tokens", 0), "output_tokens"
                            ),
                            delay_ms=_nonnegative_integer(
                                response.get("delay_ms", 0), "delay_ms"
                            ),
                        )
                    )
                elif kind == "retryable_failure":
                    allowed = common_keys | {"error_code", "message"}
                    if set(response) - allowed:
                        raise FixtureConfigurationError(
                            "a fixture failure shape is invalid"
                        )
                    error_code = response.get(
                        "error_code", "FIXTURE_TRANSIENT_FAILURE"
                    )
                    message = response.get(
                        "message",
                        "The controlled fixture reported a transient failure.",
                    )
                    if not isinstance(error_code, str) or not error_code.strip():
                        raise FixtureConfigurationError("fixture error_code is invalid")
                    if not isinstance(message, str) or not message.strip():
                        raise FixtureConfigurationError("fixture message is invalid")
                    responses.append(
                        FixtureResponse(
                            kind="retryable_failure",
                            wall_time_ms=_nonnegative_integer(
                                response.get("wall_time_ms", 0), "wall_time_ms"
                            ),
                            delay_ms=_nonnegative_integer(
                                response.get("delay_ms", 0), "delay_ms"
                            ),
                            error_code=error_code,
                            message=message,
                        )
                    )
                else:
                    raise FixtureConfigurationError("fixture response kind is invalid")

            input_hash = canonical_input_sha256(
                item["subject"], item["original_message"]
            )
            if input_hash in configured:
                raise FixtureConfigurationError("duplicate fixture input was configured")
            configured[input_hash] = responses

        return cls(
            configured,
            model_name=document["model_name"],
            model_identifier=document["model_identifier"],
        )

    def call_count(self, subject: str, original_message: str) -> int:
        input_hash = canonical_input_sha256(subject, original_message)
        with self._lock:
            return self._call_counts.get(input_hash, 0)

    def analyze(self, subject: str, original_message: str) -> ProviderResult:
        input_hash = canonical_input_sha256(subject, original_message)
        with self._lock:
            call_index = self._call_counts.get(input_hash, 0)
            responses = self._fixtures.get(input_hash)
            if responses is None:
                raise FixtureConfigurationError(
                    "no fictional response is configured for this exact input"
                )
            if call_index >= len(responses):
                raise FixtureConfigurationError(
                    "the fictional response sequence was exhausted"
                )
            self._call_counts[input_hash] = call_index + 1
            response = responses[call_index]

        if response.delay_ms > 2_000:
            raise FixtureConfigurationError("fixture delay exceeds 2,000 ms")
        if response.delay_ms:
            time.sleep(response.delay_ms / 1_000)
        if response.kind == "retryable_failure":
            raise RetryableProviderError(
                response.error_code,
                response.message,
                response.wall_time_ms,
            )
        return ProviderResult(
            model_name=self.model_name,
            model_identifier=self.model_identifier,
            proposal=response.proposal,
            wall_time_ms=response.wall_time_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )


class OllamaAnalysisProvider:
    """Call only the accepted local Ollama model through the v1 JSON contract."""

    def __init__(
        self,
        *,
        base_url: str,
        model_identifier: str,
        model_name: str = OLLAMA_MODEL_NAME,
        timeout_seconds: int = OLLAMA_TIMEOUT_SECONDS,
    ) -> None:
        parsed_url = urllib.parse.urlsplit(base_url)
        local_hosts = {"127.0.0.1", "::1", "localhost", "host.docker.internal"}
        try:
            port = parsed_url.port
        except ValueError as error:
            raise OllamaConfigurationError("the Ollama port is invalid") from error
        if (
            parsed_url.scheme != "http"
            or parsed_url.hostname not in local_hosts
            or parsed_url.username is not None
            or parsed_url.password is not None
            or port != 11434
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise OllamaConfigurationError(
                "Ollama must use the local HTTP endpoint on port 11434"
            )
        if model_name != OLLAMA_MODEL_NAME:
            raise OllamaConfigurationError(
                "the configured model is not the accepted qwen3:4b-instruct model"
            )
        if re.fullmatch(r"[0-9a-f]{12,64}", model_identifier) is None:
            raise OllamaConfigurationError("the Ollama model identifier is invalid")
        if timeout_seconds != OLLAMA_TIMEOUT_SECONDS:
            raise OllamaConfigurationError(
                "the Ollama timeout must remain fixed at 180 seconds"
            )

        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.model_identifier = model_identifier
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _invalid_result(self, error_code: str, wall_time_ms: int) -> ProviderResult:
        return ProviderResult(
            model_name=self.model_name,
            model_identifier=self.model_identifier,
            proposal={"provider_error": {"code": error_code}},
            wall_time_ms=max(0, wall_time_ms),
            input_tokens=0,
            output_tokens=0,
        )

    def analyze(self, subject: str, original_message: str) -> ProviderResult:
        user_message = f"Subject: {subject}\nMessage: {original_message}"
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "format": OLLAMA_RESPONSE_SCHEMA,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
                "num_ctx": 4096,
                "num_predict": 512,
            },
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started_at = time.perf_counter()
        try:
            with _OLLAMA_MODEL_CALL_LOCK:
                with self._opener.open(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    raw_response = response.read(MAX_OLLAMA_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            wall_time_ms = round((time.perf_counter() - started_at) * 1_000)
            if error.code == 429 or 500 <= error.code <= 599:
                raise RetryableProviderError(
                    f"OLLAMA_HTTP_{error.code}",
                    "The local Ollama service returned a retryable HTTP status.",
                    wall_time_ms,
                ) from error
            return self._invalid_result(
                f"OLLAMA_HTTP_{error.code}",
                wall_time_ms,
            )
        except (TimeoutError, socket.timeout) as error:
            wall_time_ms = round((time.perf_counter() - started_at) * 1_000)
            raise RetryableProviderError(
                "OLLAMA_TIMEOUT",
                "The local Ollama request timed out.",
                wall_time_ms,
            ) from error
        except urllib.error.URLError as error:
            wall_time_ms = round((time.perf_counter() - started_at) * 1_000)
            error_code = (
                "OLLAMA_TIMEOUT"
                if isinstance(error.reason, (TimeoutError, socket.timeout))
                else "OLLAMA_UNAVAILABLE"
            )
            raise RetryableProviderError(
                error_code,
                "The local Ollama service could not be reached.",
                wall_time_ms,
            ) from error
        except OSError as error:
            wall_time_ms = round((time.perf_counter() - started_at) * 1_000)
            raise RetryableProviderError(
                "OLLAMA_UNAVAILABLE",
                "The local Ollama transport failed.",
                wall_time_ms,
            ) from error

        wall_time_ms = round((time.perf_counter() - started_at) * 1_000)
        if len(raw_response) > MAX_OLLAMA_RESPONSE_BYTES:
            return self._invalid_result("OLLAMA_RESPONSE_TOO_LARGE", wall_time_ms)

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON constant {value} is forbidden")

        try:
            response_document = json.loads(
                raw_response.decode("utf-8"),
                parse_constant=reject_constant,
            )
            if not isinstance(response_document, dict):
                raise ValueError("Ollama response was not an object")
            message = response_document.get("message")
            if not isinstance(message, dict) or not isinstance(
                message.get("content"), str
            ):
                raise ValueError("Ollama response did not contain message content")
            if response_document.get("model") != self.model_name:
                raise ValueError("Ollama response model did not match the request")
            if response_document.get("done") is not True:
                raise ValueError("Ollama response was not complete")
            input_tokens = response_document.get("prompt_eval_count")
            output_tokens = response_document.get("eval_count")
            if (
                isinstance(input_tokens, bool)
                or not isinstance(input_tokens, int)
                or input_tokens < 0
                or isinstance(output_tokens, bool)
                or not isinstance(output_tokens, int)
                or output_tokens < 0
            ):
                raise ValueError("Ollama token counters were invalid")
            proposal = json.loads(
                message["content"],
                parse_constant=reject_constant,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return self._invalid_result("OLLAMA_INVALID_RESPONSE", wall_time_ms)

        return ProviderResult(
            model_name=self.model_name,
            model_identifier=self.model_identifier,
            proposal=proposal,
            wall_time_ms=max(0, wall_time_ms),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _rule(
    rule_code: str,
    outcome: Literal["PASS", "REVIEW", "REJECT"],
    field_name: str,
    proposed_value: Any,
    resolved_value: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "rule_code": rule_code,
        "outcome": outcome,
        "field_name": field_name,
        "proposed_value": proposed_value,
        "resolved_value": resolved_value,
        "reason": reason,
    }


def _raw_output_fingerprint(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(type(value)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _invalid_output_plan(value: Any, reason: str) -> _ValidationPlan:
    return _ValidationPlan(
        analysis_status="INVALID_OUTPUT",
        decision="NEEDS_REVIEW",
        missing_fields=(),
        rule_results=(
            _rule(
                "PROPOSAL_CONTRACT",
                "REVIEW",
                "proposal",
                None,
                None,
                reason,
            ),
        ),
        reason=reason,
        proposal={
            "error": {
                "code": "INVALID_PROVIDER_OUTPUT",
                "raw_output_sha256": _raw_output_fingerprint(value),
            }
        },
        evidence=(),
    )


def _serialized_proposal(value: Any) -> bytes | None:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None


def _source_contains(subject: str, original_message: str, value: str) -> bool:
    return value in subject or value in original_message


def _identifier_appears_exactly(
    subject: str,
    original_message: str,
    value: str,
) -> bool:
    boundary = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(value)}(?![A-Za-z0-9_-])"
    )
    return bool(boundary.search(subject) or boundary.search(original_message))


def _resolve_system(
    connection: psycopg.Connection[Any], value: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT system_id, system_code, system_name
        FROM managed_systems
        WHERE is_active
          AND (
              upper(system_code) = upper(btrim(%s))
              OR lower(system_name) = lower(btrim(%s))
              OR EXISTS (
                  SELECT 1
                  FROM unnest(aliases) AS alias_name
                  WHERE lower(btrim(alias_name)) = lower(btrim(%s))
              )
          )
        ORDER BY system_id
        """,
        (value, value, value),
    ).fetchall()
    return [dict(row) for row in rows]


def _has_permission(
    connection: psycopg.Connection[Any],
    user_id: UUID,
    system_id: UUID,
    permission_code: str,
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM system_permissions
            WHERE user_id = %s
              AND system_id = %s
              AND permission_code = %s
              AND is_active
            """,
            (user_id, system_id, permission_code),
        ).fetchone()
        is not None
    )


def _validate_proposal(
    connection: psycopg.Connection[Any],
    case: dict[str, Any],
    raw_proposal: Any,
) -> _ValidationPlan:
    encoded = _serialized_proposal(raw_proposal)
    if encoded is None or len(encoded) > MAX_PROPOSAL_BYTES:
        return _invalid_output_plan(
            raw_proposal,
            "The provider proposal was not bounded JSON.",
        )

    try:
        proposal = AnalysisProposal.model_validate(raw_proposal)
    except ValidationError:
        return _invalid_output_plan(
            raw_proposal,
            "The provider proposal did not match the exact v1 schema.",
        )

    proposal_dump = proposal.model_dump(mode="json", by_alias=True)
    evidence_dump = tuple(proposal_dump.pop("evidence"))
    values = proposal.extracted_fields.model_dump(mode="json")
    allowed_fields = set(REQUIRED_FIELDS[proposal.request_type])
    rules: list[dict[str, Any]] = [
        _rule(
            "PROPOSAL_SCHEMA",
            "PASS",
            "proposal",
            None,
            "analysis-v1",
            "The proposal matched the exact bounded v1 schema.",
        )
    ]

    unrelated = [
        field_name
        for field_name in FIELD_NAMES
        if field_name not in allowed_fields and values[field_name] is not None
    ]
    if unrelated:
        field_name = unrelated[0]
        rules.append(
            _rule(
                "UNRELATED_FIELD_NULL",
                "REVIEW",
                field_name,
                values[field_name],
                None,
                "A field unrelated to the proposed request type was populated.",
            )
        )
        return _ValidationPlan(
            "INVALID_OUTPUT",
            "NEEDS_REVIEW",
            (),
            tuple(rules),
            "The proposal populated an unrelated field.",
            proposal_dump,
            evidence_dump,
        )
    rules.append(
        _rule(
            "UNRELATED_FIELDS_NULL",
            "PASS",
            "fields",
            None,
            None,
            "Every unrelated field remained null.",
        )
    )

    seen_evidence: set[str] = set()
    for item in proposal.evidence:
        field_name = item.field_name
        proposed_value = values[field_name]
        if field_name in seen_evidence:
            rules.append(
                _rule(
                    "EVIDENCE_UNIQUE",
                    "REVIEW",
                    field_name,
                    proposed_value,
                    None,
                    "Duplicate evidence for one field is not allowed.",
                )
            )
            return _ValidationPlan(
                "INVALID_OUTPUT",
                "NEEDS_REVIEW",
                (),
                tuple(rules),
                "The proposal contained duplicate evidence.",
                proposal_dump,
                evidence_dump,
            )
        seen_evidence.add(field_name)
        if field_name not in allowed_fields or proposed_value is None:
            rules.append(
                _rule(
                    "EVIDENCE_FIELD_ALLOWED",
                    "REVIEW",
                    field_name,
                    proposed_value,
                    None,
                    "Evidence referred to an unrelated or null field.",
                )
            )
            return _ValidationPlan(
                "INVALID_OUTPUT",
                "NEEDS_REVIEW",
                (),
                tuple(rules),
                "The evidence field was not usable.",
                proposal_dump,
                evidence_dump,
            )
        if not _source_contains(
            case["subject"], case["original_message"], item.quote
        ):
            rules.append(
                _rule(
                    "EVIDENCE_EXACT_QUOTE",
                    "REVIEW",
                    field_name,
                    proposed_value,
                    None,
                    "The evidence quote was not an exact source span.",
                )
            )
            return _ValidationPlan(
                "INVALID_OUTPUT",
                "NEEDS_REVIEW",
                (),
                tuple(rules),
                "At least 1 evidence quote was not present in the source.",
                proposal_dump,
                evidence_dump,
            )

    missing_evidence = [
        field_name
        for field_name in allowed_fields
        if isinstance(values[field_name], str)
        and values[field_name].strip()
        and field_name not in seen_evidence
    ]
    if missing_evidence:
        field_name = sorted(missing_evidence)[0]
        rules.append(
            _rule(
                "EVIDENCE_REQUIRED",
                "REVIEW",
                field_name,
                values[field_name],
                None,
                "A non-null routing field had no source evidence.",
            )
        )
        return _ValidationPlan(
            "INVALID_OUTPUT",
            "NEEDS_REVIEW",
            (),
            tuple(rules),
            "The proposal lacked required field evidence.",
            proposal_dump,
            evidence_dump,
        )
    rules.append(
        _rule(
            "EVIDENCE_CONTRACT",
            "PASS",
            "evidence",
            None,
            len(evidence_dump),
            "Every used field had unique exact source evidence.",
        )
    )

    missing_fields = tuple(
        field_name
        for field_name in REQUIRED_FIELDS[proposal.request_type]
        if values[field_name] is None
        or not str(values[field_name]).strip()
    )
    if missing_fields:
        rules.append(
            _rule(
                "REQUIRED_FIELDS",
                "REVIEW",
                missing_fields[0],
                None,
                None,
                "Required information was missing from the proposal.",
            )
        )
        return _ValidationPlan(
            "COMPLETED",
            "NEEDS_INFORMATION",
            missing_fields,
            tuple(rules),
            "The requester must provide required information.",
            proposal_dump,
            evidence_dump,
        )
    rules.append(
        _rule(
            "REQUIRED_FIELDS",
            "PASS",
            "fields",
            None,
            None,
            "Every required field contained a nonblank value.",
        )
    )

    if proposal.request_type == "incident_report":
        for field_name in ("impact", "urgency"):
            normalized = str(values[field_name]).casefold()
            if normalized not in IMPACT_VALUES:
                rules.append(
                    _rule(
                        "ENUM_ALLOWED",
                        "REVIEW",
                        field_name,
                        values[field_name],
                        None,
                        "The proposed value was outside the accepted enum.",
                    )
                )
                return _ValidationPlan(
                    "INVALID_OUTPUT",
                    "NEEDS_REVIEW",
                    (),
                    tuple(rules),
                    "An impact or urgency value was invalid.",
                    proposal_dump,
                    evidence_dump,
                )
        rules.append(
            _rule(
                "INCIDENT_ENUMS",
                "PASS",
                "impact_and_urgency",
                None,
                None,
                "Impact and urgency matched accepted enum values.",
            )
        )

    details: dict[str, Any] = {
        "policy_topic": None,
        "policy_question": None,
        "affected_system_id": None,
        "incident_description": None,
        "impact": None,
        "urgency": None,
        "target_system_id": None,
        "requested_access_level": None,
        "business_reason": None,
        "approver_user_id": None,
        "record_reference": None,
        "requested_changes": None,
        "referenced_case_id": None,
    }

    resolved_system: dict[str, Any] | None = None
    system_field: str | None = None
    if proposal.request_type == "incident_report":
        system_field = "affected_service"
    elif proposal.request_type in {"access_request", "data_change_request"}:
        system_field = "target_system"
    if system_field is not None:
        system_matches = _resolve_system(connection, str(values[system_field]))
        if len(system_matches) != 1:
            rules.append(
                _rule(
                    "SYSTEM_EXACT",
                    "REVIEW",
                    system_field,
                    values[system_field],
                    None,
                    "The system did not resolve to exactly 1 active reference.",
                )
            )
            return _ValidationPlan(
                "COMPLETED",
                "NEEDS_REVIEW",
                (),
                tuple(rules),
                "The proposed system reference was ambiguous or unknown.",
                proposal_dump,
                evidence_dump,
            )
        resolved_system = system_matches[0]
        rules.append(
            _rule(
                "SYSTEM_EXACT",
                "PASS",
                system_field,
                values[system_field],
                resolved_system["system_code"],
                "The system resolved through an exact active reference.",
            )
        )

    for identifier_field in ("approver_id", "record_reference", "case_reference"):
        identifier_value = values[identifier_field]
        if identifier_value is not None and not _identifier_appears_exactly(
            case["subject"], case["original_message"], str(identifier_value)
        ):
            rules.append(
                _rule(
                    "IDENTIFIER_EXACT_SOURCE",
                    "REVIEW",
                    identifier_field,
                    identifier_value,
                    None,
                    "The complete identifier did not appear exactly in the source.",
                )
            )
            return _ValidationPlan(
                "COMPLETED",
                "NEEDS_REVIEW",
                (),
                tuple(rules),
                "A consequential identifier failed exact source validation.",
                proposal_dump,
                evidence_dump,
            )
    rules.append(
        _rule(
            "IDENTIFIERS_EXACT_SOURCE",
            "PASS",
            "identifiers",
            None,
            None,
            "Every consequential identifier appeared exactly in the source.",
        )
    )

    if proposal.request_type in {"access_request", "data_change_request"}:
        assert resolved_system is not None
        requester_permission = (
            "REQUEST_ACCESS"
            if proposal.request_type == "access_request"
            else "REQUEST_DATA_CHANGE"
        )
        if not _has_permission(
            connection,
            case["requester_id"],
            resolved_system["system_id"],
            requester_permission,
        ):
            rules.append(
                _rule(
                    "REQUESTER_PERMISSION",
                    "REJECT",
                    "target_system",
                    values["target_system"],
                    None,
                    "The requester lacks the required active system permission.",
                )
            )
            return _ValidationPlan(
                "COMPLETED",
                "REJECTED",
                (),
                tuple(rules),
                "The requester is not authorized for the proposed action.",
                proposal_dump,
                evidence_dump,
            )
        rules.append(
            _rule(
                "REQUESTER_PERMISSION",
                "PASS",
                "target_system",
                values["target_system"],
                requester_permission,
                "The requester has the required active system permission.",
            )
        )

        approval_permission = (
            "APPROVE_ACCESS"
            if proposal.request_type == "access_request"
            else "APPROVE_DATA_CHANGE"
        )
        approvers = connection.execute(
            """
            SELECT users.user_id, users.employee_reference
            FROM users
            WHERE users.employee_reference = upper(btrim(%s))
              AND users.is_active
              AND EXISTS (
                  SELECT 1
                  FROM user_roles
                  WHERE user_roles.user_id = users.user_id
                    AND role_code = 'APPROVER'
              )
              AND EXISTS (
                  SELECT 1
                  FROM system_permissions
                  WHERE system_permissions.user_id = users.user_id
                    AND system_permissions.system_id = %s
                    AND system_permissions.permission_code = %s
                    AND system_permissions.is_active
              )
            """,
            (
                values["approver_id"],
                resolved_system["system_id"],
                approval_permission,
            ),
        ).fetchall()
        if len(approvers) != 1:
            rules.append(
                _rule(
                    "APPROVER_EXACT_AND_AUTHORIZED",
                    "REVIEW",
                    "approver_id",
                    values["approver_id"],
                    None,
                    "The approver did not resolve to exactly 1 authorized user.",
                )
            )
            return _ValidationPlan(
                "COMPLETED",
                "NEEDS_REVIEW",
                (),
                tuple(rules),
                "The proposed approver could not be authorized exactly.",
                proposal_dump,
                evidence_dump,
            )
        details["approver_user_id"] = approvers[0]["user_id"]
        rules.append(
            _rule(
                "APPROVER_EXACT_AND_AUTHORIZED",
                "PASS",
                "approver_id",
                values["approver_id"],
                approvers[0]["employee_reference"],
                "The exact active approver has the required permission.",
            )
        )

    if proposal.request_type == "status_request":
        referenced = connection.execute(
            """
            SELECT
                cases.case_id,
                cases.requester_id,
                COALESCE(
                    case_details.target_system_id,
                    case_details.affected_system_id
                ) AS system_id
            FROM cases
            LEFT JOIN case_details USING (case_id)
            WHERE cases.case_reference = %s
            """,
            (values["case_reference"],),
        ).fetchall()
        if len(referenced) != 1:
            rules.append(
                _rule(
                    "REFERENCED_CASE_EXACT",
                    "REVIEW",
                    "case_reference",
                    values["case_reference"],
                    None,
                    "The referenced case did not resolve exactly.",
                )
            )
            return _ValidationPlan(
                "COMPLETED",
                "NEEDS_REVIEW",
                (),
                tuple(rules),
                "The referenced case was unknown or ambiguous.",
                proposal_dump,
                evidence_dump,
            )
        referenced_case = referenced[0]
        owns_case = referenced_case["requester_id"] == case["requester_id"]
        may_view = owns_case or (
            referenced_case["system_id"] is not None
            and _has_permission(
                connection,
                case["requester_id"],
                referenced_case["system_id"],
                "VIEW_STATUS",
            )
        )
        if not may_view:
            rules.append(
                _rule(
                    "STATUS_OWNERSHIP",
                    "REJECT",
                    "case_reference",
                    values["case_reference"],
                    None,
                    "The requester cannot view the referenced case.",
                )
            )
            return _ValidationPlan(
                "COMPLETED",
                "REJECTED",
                (),
                tuple(rules),
                "The requester is not authorized to view the referenced case.",
                proposal_dump,
                evidence_dump,
            )
        details["referenced_case_id"] = referenced_case["case_id"]
        rules.append(
            _rule(
                "STATUS_OWNERSHIP",
                "PASS",
                "case_reference",
                values["case_reference"],
                str(referenced_case["case_id"]),
                "The requester owns or may explicitly view the referenced case.",
            )
        )

    possible_duplicate = connection.execute(
        """
        SELECT case_reference
        FROM cases
        WHERE content_fingerprint = %s
          AND case_id <> %s
        ORDER BY case_reference
        LIMIT 1
        """,
        (case["content_fingerprint"], case["case_id"]),
    ).fetchone()
    if possible_duplicate is not None:
        rules.append(
            _rule(
                "POSSIBLE_DUPLICATE",
                "REVIEW",
                "content_fingerprint",
                None,
                possible_duplicate["case_reference"],
                "Another distinct request has the same content fingerprint.",
            )
        )
        return _ValidationPlan(
            "COMPLETED",
            "NEEDS_REVIEW",
            (),
            tuple(rules),
            "A possible duplicate requires service-agent review.",
            proposal_dump,
            evidence_dump,
        )
    rules.append(
        _rule(
            "POSSIBLE_DUPLICATE",
            "PASS",
            "content_fingerprint",
            None,
            None,
            "No distinct request shared this content fingerprint.",
        )
    )

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
        (case["requester_id"],),
    ).fetchone()
    if requester is None:
        rules.append(
            _rule(
                "REQUESTER_ACTIVE",
                "REJECT",
                "requester_id",
                str(case["requester_id"]),
                None,
                "The requester is no longer active and authorized.",
            )
        )
        return _ValidationPlan(
            "COMPLETED",
            "REJECTED",
            (),
            tuple(rules),
            "The requester failed the final authorization check.",
            proposal_dump,
            evidence_dump,
        )
    rules.append(
        _rule(
            "REQUESTER_ACTIVE",
            "PASS",
            "requester_id",
            str(case["requester_id"]),
            str(case["requester_id"]),
            "The requester remains active and authorized.",
        )
    )

    if proposal.request_type == "policy_question":
        details["policy_topic"] = values["policy_topic"]
        details["policy_question"] = values["question"]
    elif proposal.request_type == "incident_report":
        assert resolved_system is not None
        details["affected_system_id"] = resolved_system["system_id"]
        details["incident_description"] = values["incident_description"]
        details["impact"] = str(values["impact"]).upper()
        details["urgency"] = str(values["urgency"]).upper()
    elif proposal.request_type == "access_request":
        assert resolved_system is not None
        details["target_system_id"] = resolved_system["system_id"]
        details["requested_access_level"] = values["requested_access_level"]
        details["business_reason"] = values["business_reason"]
    elif proposal.request_type == "data_change_request":
        assert resolved_system is not None
        details["target_system_id"] = resolved_system["system_id"]
        details["record_reference"] = values["record_reference"]
        details["requested_changes"] = values["requested_changes"]
        details["business_reason"] = values["business_reason"]

    return _ValidationPlan(
        "COMPLETED",
        "READY",
        (),
        tuple(rules),
        "The proposal passed all deterministic analysis rules.",
        proposal_dump,
        evidence_dump,
        request_type=REQUEST_TYPE_MAP[proposal.request_type],
        summary=proposal.summary,
        details=details,
    )


def _load_case_and_event(
    connection: psycopg.Connection[Any],
    *,
    case_id: UUID,
    case_reference: str,
    expected_case_version: int,
    workflow_start_reference: str,
) -> dict[str, Any]:
    case = connection.execute(
        """
        SELECT case_id, case_reference, requester_id, subject, original_message,
               content_fingerprint, current_state, version
        FROM cases
        WHERE case_id = %s
        FOR UPDATE
        """,
        (case_id,),
    ).fetchone()
    if case is None:
        raise AnalysisNotFound("The analysis case was not found.")
    if case["case_reference"] != case_reference:
        raise AnalysisConflict("The analysis case reference does not match.")

    reference_match = WORKFLOW_REFERENCE_PATTERN.fullmatch(
        workflow_start_reference
    )
    if reference_match is None:
        raise AnalysisConflict("The workflow-start reference is invalid.")
    event = connection.execute(
        """
        SELECT event_id, sequence_number, from_state, to_state, event_payload
        FROM case_events
        WHERE event_id = %s
          AND case_id = %s
          AND event_type = 'ANALYSIS_STARTED'
        """,
        (int(reference_match.group(1)), case_id),
    ).fetchone()
    if event is None:
        raise AnalysisConflict("The workflow-start reference does not match.")
    workflow_key = event["event_payload"].get(
        "workflow_start_idempotency_key"
    )
    if (
        event["from_state"] != "RECEIVED"
        or event["to_state"] != "ANALYZING"
        or event["sequence_number"] != expected_case_version
        or not isinstance(workflow_key, str)
        or re.fullmatch(r"[0-9a-f]{64}", workflow_key) is None
    ):
        raise AnalysisConflict("The workflow-start evidence is incompatible.")
    return dict(case)


def _load_resumed_case_and_event(
    connection: psycopg.Connection[Any],
    *,
    case_id: UUID,
    case_reference: str,
    expected_case_version: int,
    human_resume_reference: str,
) -> dict[str, Any]:
    reference_match = HUMAN_RESUME_REFERENCE_PATTERN.fullmatch(
        human_resume_reference
    )
    if reference_match is None:
        raise AnalysisConflict("The human-resume reference is invalid.")

    case = connection.execute(
        """
        SELECT case_id, case_reference, requester_id, subject, original_message,
               content_fingerprint, current_state, version
        FROM cases
        WHERE case_id = %s
        FOR UPDATE
        """,
        (case_id,),
    ).fetchone()
    if case is None:
        raise AnalysisNotFound("The resumed analysis case was not found.")
    if case["case_reference"] != case_reference:
        raise AnalysisConflict("The resumed analysis case reference conflicts.")

    acknowledgement = connection.execute(
        """
        SELECT to_state, event_payload
        FROM case_events
        WHERE event_id = %s
          AND case_id = %s
          AND event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
        """,
        (int(reference_match.group(1)), case_id),
    ).fetchone()
    if acknowledgement is None:
        raise AnalysisConflict("The human-resume acknowledgement was not found.")
    acknowledgement_payload = acknowledgement["event_payload"]
    decision_match = HUMAN_DECISION_REFERENCE_PATTERN.fullmatch(
        str(acknowledgement_payload.get("human_decision_reference", ""))
    )
    if decision_match is None:
        raise AnalysisConflict("The human-resume decision reference is invalid.")

    decision = connection.execute(
        """
        SELECT to_state, event_type, actor_type, actor_user_id, event_payload
        FROM case_events
        WHERE event_id = %s AND case_id = %s
        """,
        (int(decision_match.group(1)), case_id),
    ).fetchone()
    information = (
        decision["event_payload"].get("information")
        if decision is not None
        else None
    )
    if (
        acknowledgement["to_state"] != "ANALYZING"
        or acknowledgement_payload.get("resume_route")
        != "ANALYSIS_CONTINUATION"
        or acknowledgement_payload.get("action") != "SUBMIT_INFORMATION"
        or acknowledgement_payload.get("case_version")
        != expected_case_version
        or decision is None
        or decision["event_type"] != "REQUESTER_INFORMATION_SUBMITTED"
        or decision["actor_type"] != "USER"
        or decision["actor_user_id"] != case["requester_id"]
        or decision["to_state"] != "ANALYZING"
        or decision["event_payload"].get("action") != "SUBMIT_INFORMATION"
        or decision["event_payload"].get("result_case_version")
        != expected_case_version
        or re.fullmatch(
            r"[0-9a-f]{64}",
            str(acknowledgement_payload.get("outbox_idempotency_key", "")),
        )
        is None
        or not isinstance(information, str)
        or not information.strip()
    ):
        raise AnalysisConflict(
            "The requester-information acknowledgement is incompatible."
        )

    resumed = dict(case)
    resumed["original_message"] = (
        f"{case['original_message']}\n\n"
        f"Requester additional information:\n{information.strip()}"
    )
    return resumed


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


def _append_case_event(
    connection: psycopg.Connection[Any],
    *,
    case_id: UUID,
    from_state: str,
    to_state: str,
    event_type: str,
    reason: str,
    payload: dict[str, Any],
) -> None:
    connection.execute(
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
        VALUES (%s, %s, %s, %s, %s, 'SYSTEM', %s, %s)
        """,
        (
            case_id,
            _next_event_sequence(connection, case_id),
            from_state,
            to_state,
            event_type,
            reason,
            Jsonb(payload),
        ),
    )


def _replay_execution(
    case: dict[str, Any], row: dict[str, Any]
) -> AnalysisExecution:
    return AnalysisExecution(
        analysis_run_id=row["analysis_run_id"],
        case_reference=case["case_reference"],
        attempt_number=row["attempt_number"],
        outcome="REPLAY",
        analysis_status=row["status"],
        validation_decision=row["validation_decision"],
        current_state=case["current_state"],
        case_version=case["version"],
        idempotent_replay=True,
        provider_called=False,
    )


def _target_state(plan: _ValidationPlan) -> str:
    if plan.decision == "NEEDS_INFORMATION":
        return "NEEDS_INFORMATION"
    if plan.decision == "NEEDS_REVIEW":
        return "NEEDS_REVIEW"
    if plan.decision == "REJECTED":
        return "REJECTED"
    if plan.request_type in {"ACCESS_REQUEST", "DATA_CHANGE_REQUEST"}:
        return "PENDING_APPROVAL"
    if plan.request_type == "POLICY_QUESTION":
        return "ANALYZING"
    return "READY_FOR_ACTION"


def _event_type(plan: _ValidationPlan) -> str:
    if plan.decision == "NEEDS_INFORMATION":
        return "ANALYSIS_NEEDS_INFORMATION"
    if plan.decision == "NEEDS_REVIEW":
        return "ANALYSIS_NEEDS_REVIEW"
    if plan.decision == "REJECTED":
        return "ANALYSIS_REJECTED"
    if plan.request_type in {"ACCESS_REQUEST", "DATA_CHANGE_REQUEST"}:
        return "APPROVAL_REQUESTED"
    if plan.request_type == "POLICY_QUESTION":
        return "ANALYSIS_READY_FOR_RETRIEVAL"
    return "ANALYSIS_READY"


def _persist_validation_plan(
    connection: psycopg.Connection[Any],
    *,
    case: dict[str, Any],
    analysis_run_id: UUID,
    attempt_number: int,
    plan: _ValidationPlan,
    provider_result: ProviderResult | None,
    existing_terminal_row: bool = False,
    trigger_reference: str | None = None,
) -> AnalysisExecution:
    if not existing_terminal_row:
        assert provider_result is not None
        updated = connection.execute(
            """
            UPDATE ai_analysis_runs
            SET proposal = %s,
                evidence = %s,
                status = %s,
                wall_time_ms = %s,
                input_tokens = %s,
                output_tokens = %s,
                completed_at = now()
            WHERE analysis_run_id = %s
              AND status = 'PROCESSING'
            """,
            (
                Jsonb(plan.proposal),
                Jsonb(list(plan.evidence)),
                plan.analysis_status,
                provider_result.wall_time_ms,
                provider_result.input_tokens,
                provider_result.output_tokens,
                analysis_run_id,
            ),
        )
        if updated.rowcount != 1:
            raise AnalysisConflict("The analysis attempt is no longer processing.")

    validation = connection.execute(
        """
        INSERT INTO validation_runs (
            case_id,
            analysis_run_id,
            overall_decision,
            missing_fields,
            rule_results,
            reason
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING validation_run_id
        """,
        (
            case["case_id"],
            analysis_run_id,
            plan.decision,
            list(plan.missing_fields),
            Jsonb(list(plan.rule_results)),
            plan.reason,
        ),
    ).fetchone()

    target_state = _target_state(plan)
    new_version = case["version"] + (target_state != case["current_state"])

    if plan.decision == "READY":
        assert plan.request_type is not None
        assert plan.summary is not None
        assert plan.details is not None
        details = plan.details
        connection.execute(
            """
            UPDATE cases
            SET request_type = %s,
                ai_summary = %s,
                current_state = %s,
                version = %s,
                updated_at = now()
            WHERE case_id = %s
            """,
            (
                plan.request_type,
                plan.summary,
                target_state,
                new_version,
                case["case_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO case_details (
                case_id,
                policy_topic,
                policy_question,
                affected_system_id,
                incident_description,
                impact,
                urgency,
                target_system_id,
                requested_access_level,
                business_reason,
                approver_user_id,
                record_reference,
                requested_changes,
                referenced_case_id,
                accepted_by_type,
                accepted_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, 'SYSTEM_RULE', now()
            )
            """,
            (
                case["case_id"],
                details["policy_topic"],
                details["policy_question"],
                details["affected_system_id"],
                details["incident_description"],
                details["impact"],
                details["urgency"],
                details["target_system_id"],
                details["requested_access_level"],
                details["business_reason"],
                details["approver_user_id"],
                details["record_reference"],
                details["requested_changes"],
                details["referenced_case_id"],
            ),
        )
        if plan.request_type in {"ACCESS_REQUEST", "DATA_CHANGE_REQUEST"}:
            connection.execute(
                """
                INSERT INTO approvals (
                    case_id,
                    approver_user_id,
                    request_type,
                    decision,
                    requested_at
                )
                VALUES (%s, %s, %s, 'PENDING', now())
                """,
                (
                    case["case_id"],
                    details["approver_user_id"],
                    plan.request_type,
                ),
            )
    else:
        connection.execute(
            """
            UPDATE cases
            SET current_state = %s,
                version = %s,
                updated_at = now()
            WHERE case_id = %s
            """,
            (target_state, new_version, case["case_id"]),
        )

    event_payload = {
        "analysis_run_id": str(analysis_run_id),
        "attempt_number": attempt_number,
        "schema_version": "1",
        "validation_decision": plan.decision,
        "validation_run_id": str(validation["validation_run_id"]),
    }
    if trigger_reference is not None:
        event_payload["analysis_trigger_reference"] = trigger_reference
    _append_case_event(
        connection,
        case_id=case["case_id"],
        from_state=case["current_state"],
        to_state=target_state,
        event_type=_event_type(plan),
        reason=plan.reason,
        payload=event_payload,
    )

    return AnalysisExecution(
        analysis_run_id=analysis_run_id,
        case_reference=case["case_reference"],
        attempt_number=attempt_number,
        outcome=plan.decision,
        analysis_status=plan.analysis_status,
        validation_decision=plan.decision,
        current_state=target_state,
        case_version=new_version,
        idempotent_replay=False,
        provider_called=provider_result is not None,
    )


def _claim_attempt(
    database_url: str,
    provider: AnalysisProvider,
    *,
    case_id: UUID,
    case_reference: str,
    expected_case_version: int,
    workflow_start_reference: str | None,
    lease_seconds: int,
    human_resume_reference: str | None = None,
) -> _ClaimedAttempt | AnalysisExecution:
    if lease_seconds < 0:
        raise ValueError("lease_seconds must be nonnegative")
    if (
        not provider.model_name.strip()
        or len(provider.model_name) > 100
        or not provider.model_identifier.strip()
        or len(provider.model_identifier) > 100
    ):
        raise AnalysisConflict("The configured provider identity is invalid.")

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        if human_resume_reference is None:
            if workflow_start_reference is None:
                raise AnalysisConflict("The workflow-start reference is required.")
            case = _load_case_and_event(
                connection,
                case_id=case_id,
                case_reference=case_reference,
                expected_case_version=expected_case_version,
                workflow_start_reference=workflow_start_reference,
            )
            trigger_reference = workflow_start_reference
        else:
            if workflow_start_reference is not None:
                raise AnalysisConflict("Only 1 analysis trigger may be supplied.")
            case = _load_resumed_case_and_event(
                connection,
                case_id=case_id,
                case_reference=case_reference,
                expected_case_version=expected_case_version,
                human_resume_reference=human_resume_reference,
            )
            trigger_reference = human_resume_reference
        input_sha256 = canonical_input_sha256(
            case["subject"], case["original_message"]
        )
        attempts = connection.execute(
            """
            SELECT
                analysis.analysis_run_id,
                analysis.attempt_number,
                analysis.status,
                analysis.created_at,
                validation.overall_decision AS validation_decision
            FROM ai_analysis_runs AS analysis
            LEFT JOIN LATERAL (
                SELECT overall_decision
                FROM validation_runs
                WHERE validation_runs.analysis_run_id = analysis.analysis_run_id
                ORDER BY created_at
                LIMIT 1
            ) AS validation ON true
            WHERE analysis.case_id = %s
              AND analysis.input_sha256 = %s
              AND analysis.prompt_contract_version = %s
              AND analysis.model_identifier = %s
            ORDER BY analysis.attempt_number DESC
            """,
            (
                case_id,
                input_sha256,
                PROMPT_CONTRACT_VERSION,
                provider.model_identifier,
            ),
        ).fetchall()
        latest = dict(attempts[0]) if attempts else None

        if latest is not None and latest["validation_decision"] is not None:
            return _replay_execution(case, latest)
        if (
            latest is not None
            and latest["status"] == "FAILED"
            and latest["attempt_number"] == MAX_PROVIDER_ATTEMPTS
            and case["current_state"] == "FAILED"
        ):
            return _replay_execution(case, latest)

        if (
            case["current_state"] != "ANALYZING"
            or case["version"] != expected_case_version
        ):
            raise AnalysisConflict(
                "The case state or version does not permit a new analysis attempt."
            )

        if latest is not None and latest["status"] == "PROCESSING":
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
            if latest["created_at"] > cutoff:
                raise AnalysisInProgress(
                    "An unexpired analysis attempt is already processing."
                )
            error = {
                "error": {
                    "code": "ANALYSIS_LEASE_EXPIRED",
                    "message": (
                        "The attempt lease expired; the provider outcome is unknown."
                    ),
                }
            }
            connection.execute(
                """
                UPDATE ai_analysis_runs
                SET proposal = %s,
                    evidence = '[]'::jsonb,
                    status = 'FAILED',
                    wall_time_ms = 0,
                    input_tokens = 0,
                    output_tokens = 0,
                    completed_at = now()
                WHERE analysis_run_id = %s
                  AND status = 'PROCESSING'
                """,
                (Jsonb(error), latest["analysis_run_id"]),
            )
            if latest["attempt_number"] == MAX_PROVIDER_ATTEMPTS:
                new_version = case["version"] + 1
                connection.execute(
                    """
                    UPDATE cases
                    SET current_state = 'FAILED',
                        version = %s,
                        updated_at = now()
                    WHERE case_id = %s
                    """,
                    (new_version, case_id),
                )
                _append_case_event(
                    connection,
                    case_id=case_id,
                    from_state="ANALYZING",
                    to_state="FAILED",
                    event_type="ANALYSIS_FAILED",
                    reason="The final processing lease expired.",
                    payload={
                        "analysis_run_id": str(latest["analysis_run_id"]),
                        "attempt_number": latest["attempt_number"],
                        "error_code": "ANALYSIS_LEASE_EXPIRED",
                        "schema_version": "1",
                    },
                )
                return AnalysisExecution(
                    analysis_run_id=latest["analysis_run_id"],
                    case_reference=case_reference,
                    attempt_number=latest["attempt_number"],
                    outcome="FAILED",
                    analysis_status="FAILED",
                    validation_decision=None,
                    current_state="FAILED",
                    case_version=new_version,
                    idempotent_replay=False,
                    provider_called=False,
                )
            _append_case_event(
                connection,
                case_id=case_id,
                from_state="ANALYZING",
                to_state="ANALYZING",
                event_type="ANALYSIS_ATTEMPT_RECOVERED",
                reason="An expired attempt was recorded before the bounded retry.",
                payload={
                    "analysis_run_id": str(latest["analysis_run_id"]),
                    "attempt_number": latest["attempt_number"],
                    "error_code": "ANALYSIS_LEASE_EXPIRED",
                    "schema_version": "1",
                },
            )

        attempt_number = (latest["attempt_number"] + 1) if latest else 1
        if attempt_number > MAX_PROVIDER_ATTEMPTS:
            raise AnalysisConflict("The bounded analysis attempt limit was reached.")

        if len(case["subject"]) + len(case["original_message"]) > MAX_INPUT_CHARACTERS:
            analysis = connection.execute(
                """
                INSERT INTO ai_analysis_runs (
                    case_id,
                    model_name,
                    model_identifier,
                    prompt_contract_version,
                    input_sha256,
                    proposal,
                    evidence,
                    status,
                    wall_time_ms,
                    input_tokens,
                    output_tokens,
                    attempt_number,
                    completed_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, '[]'::jsonb,
                    'SKIPPED', 0, 0, 0, %s, now()
                )
                RETURNING analysis_run_id
                """,
                (
                    case_id,
                    provider.model_name,
                    provider.model_identifier,
                    PROMPT_CONTRACT_VERSION,
                    input_sha256,
                    Jsonb({"error": {"code": "INPUT_TOO_LARGE"}}),
                    attempt_number,
                ),
            ).fetchone()
            plan = _ValidationPlan(
                analysis_status="SKIPPED",
                decision="NEEDS_REVIEW",
                missing_fields=(),
                rule_results=(
                    _rule(
                        "INPUT_BUDGET",
                        "REVIEW",
                        "original_message",
                        len(case["subject"]) + len(case["original_message"]),
                        MAX_INPUT_CHARACTERS,
                        "The combined input exceeded the 8,000-character limit.",
                    ),
                ),
                reason="The untruncated request requires service-agent review.",
                proposal={"error": {"code": "INPUT_TOO_LARGE"}},
                evidence=(),
            )
            return _persist_validation_plan(
                connection,
                case=case,
                analysis_run_id=analysis["analysis_run_id"],
                attempt_number=attempt_number,
                plan=plan,
                provider_result=None,
                existing_terminal_row=True,
                trigger_reference=trigger_reference,
            )

        analysis = connection.execute(
            """
            INSERT INTO ai_analysis_runs (
                case_id,
                model_name,
                model_identifier,
                prompt_contract_version,
                input_sha256,
                proposal,
                evidence,
                status,
                wall_time_ms,
                input_tokens,
                output_tokens,
                attempt_number
            )
            VALUES (
                %s, %s, %s, %s, %s, '{}'::jsonb, '[]'::jsonb,
                'PROCESSING', 0, 0, 0, %s
            )
            RETURNING analysis_run_id
            """,
            (
                case_id,
                provider.model_name,
                provider.model_identifier,
                PROMPT_CONTRACT_VERSION,
                input_sha256,
                attempt_number,
            ),
        ).fetchone()

    return _ClaimedAttempt(
        analysis_run_id=analysis["analysis_run_id"],
        case_id=case_id,
        case_reference=case_reference,
        expected_case_version=expected_case_version,
        input_sha256=input_sha256,
        attempt_number=attempt_number,
        subject=case["subject"],
        original_message=case["original_message"],
        trigger_reference=trigger_reference,
    )


def _finalize_retryable_failure(
    database_url: str,
    claim: _ClaimedAttempt,
    error: RetryableProviderError,
) -> AnalysisExecution:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        case = connection.execute(
            """
            SELECT case_id, case_reference, subject, original_message,
                   current_state, version
            FROM cases
            WHERE case_id = %s
            FOR UPDATE
            """,
            (claim.case_id,),
        ).fetchone()
        if case is None:
            raise AnalysisNotFound("The analysis case was not found.")
        if (
            case["case_reference"] != claim.case_reference
            or case["current_state"] != "ANALYZING"
            or case["version"] != claim.expected_case_version
            or canonical_input_sha256(
                claim.subject, claim.original_message
            ) != claim.input_sha256
        ):
            raise AnalysisConflict("The case changed before failure finalization.")

        updated = connection.execute(
            """
            UPDATE ai_analysis_runs
            SET proposal = %s,
                evidence = '[]'::jsonb,
                status = 'FAILED',
                wall_time_ms = %s,
                input_tokens = 0,
                output_tokens = 0,
                completed_at = now()
            WHERE analysis_run_id = %s
              AND status = 'PROCESSING'
            """,
            (
                Jsonb(
                    {
                        "error": {
                            "code": error.error_code[:80],
                            "message": str(error)[:300],
                        }
                    }
                ),
                max(0, error.wall_time_ms),
                claim.analysis_run_id,
            ),
        )
        if updated.rowcount != 1:
            raise AnalysisConflict("The analysis attempt is no longer processing.")

        final_attempt = claim.attempt_number == MAX_PROVIDER_ATTEMPTS
        target_state = "FAILED" if final_attempt else "ANALYZING"
        new_version = case["version"] + final_attempt
        if final_attempt:
            connection.execute(
                """
                UPDATE cases
                SET current_state = 'FAILED',
                    version = %s,
                    updated_at = now()
                WHERE case_id = %s
                """,
                (new_version, claim.case_id),
            )
        event_payload = {
            "analysis_run_id": str(claim.analysis_run_id),
            "attempt_number": claim.attempt_number,
            "error_code": error.error_code[:80],
            "schema_version": "1",
        }
        if claim.trigger_reference is not None:
            event_payload["analysis_trigger_reference"] = claim.trigger_reference
        _append_case_event(
            connection,
            case_id=claim.case_id,
            from_state="ANALYZING",
            to_state=target_state,
            event_type=(
                "ANALYSIS_FAILED" if final_attempt else "ANALYSIS_RETRY_SCHEDULED"
            ),
            reason=(
                "Both bounded provider attempts failed."
                if final_attempt
                else "The first retryable provider failure was recorded."
            ),
            payload=event_payload,
        )

    return AnalysisExecution(
        analysis_run_id=claim.analysis_run_id,
        case_reference=claim.case_reference,
        attempt_number=claim.attempt_number,
        outcome="FAILED" if final_attempt else "RETRYABLE_FAILURE",
        analysis_status="FAILED",
        validation_decision=None,
        current_state=target_state,
        case_version=new_version,
        idempotent_replay=False,
        provider_called=True,
    )


def _finalize_provider_result(
    database_url: str,
    claim: _ClaimedAttempt,
    result: ProviderResult,
    provider: AnalysisProvider,
) -> AnalysisExecution:
    provider_contract_valid = (
        result.model_name == provider.model_name
        and result.model_identifier == provider.model_identifier
        and isinstance(result.wall_time_ms, int)
        and not isinstance(result.wall_time_ms, bool)
        and result.wall_time_ms >= 0
        and isinstance(result.input_tokens, int)
        and not isinstance(result.input_tokens, bool)
        and result.input_tokens >= 0
        and isinstance(result.output_tokens, int)
        and not isinstance(result.output_tokens, bool)
        and result.output_tokens >= 0
    )

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        case = connection.execute(
            """
            SELECT case_id, case_reference, requester_id, subject,
                   original_message, content_fingerprint, current_state, version
            FROM cases
            WHERE case_id = %s
            FOR UPDATE
            """,
            (claim.case_id,),
        ).fetchone()
        if case is None:
            raise AnalysisNotFound("The analysis case was not found.")
        if (
            case["case_reference"] != claim.case_reference
            or case["current_state"] != "ANALYZING"
            or case["version"] != claim.expected_case_version
            or canonical_input_sha256(
                claim.subject, claim.original_message
            ) != claim.input_sha256
        ):
            raise AnalysisConflict("The case changed before result finalization.")

        validation_case = dict(case)
        validation_case["subject"] = claim.subject
        validation_case["original_message"] = claim.original_message
        if provider_contract_valid:
            plan = _validate_proposal(connection, validation_case, result.proposal)
            safe_result = result
        else:
            plan = _invalid_output_plan(
                result.proposal,
                "The provider metadata or counters violated the v1 contract.",
            )
            safe_result = ProviderResult(
                model_name=provider.model_name,
                model_identifier=provider.model_identifier,
                proposal=result.proposal,
                wall_time_ms=max(
                    0,
                    result.wall_time_ms
                    if isinstance(result.wall_time_ms, int)
                    and not isinstance(result.wall_time_ms, bool)
                    else 0,
                ),
                input_tokens=0,
                output_tokens=0,
            )

        return _persist_validation_plan(
            connection,
            case=validation_case,
            analysis_run_id=claim.analysis_run_id,
            attempt_number=claim.attempt_number,
            plan=plan,
            provider_result=safe_result,
            trigger_reference=claim.trigger_reference,
        )


def _execute_claimed_analysis(
    database_url: str,
    provider: AnalysisProvider,
    claimed: _ClaimedAttempt | AnalysisExecution,
) -> AnalysisExecution:
    if isinstance(claimed, AnalysisExecution):
        return claimed
    try:
        result = provider.analyze(claimed.subject, claimed.original_message)
    except RetryableProviderError as error:
        return _finalize_retryable_failure(database_url, claimed, error)
    return _finalize_provider_result(database_url, claimed, result, provider)


def analyze_case(
    database_url: str,
    provider: AnalysisProvider,
    *,
    case_id: UUID,
    case_reference: str,
    expected_case_version: int,
    workflow_start_reference: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> AnalysisExecution:
    """Claim, call at most once, and atomically finalize one analysis attempt."""

    claimed = _claim_attempt(
        database_url,
        provider,
        case_id=case_id,
        case_reference=case_reference,
        expected_case_version=expected_case_version,
        workflow_start_reference=workflow_start_reference,
        lease_seconds=lease_seconds,
    )
    return _execute_claimed_analysis(database_url, provider, claimed)


def analyze_resumed_case(
    database_url: str,
    provider: AnalysisProvider,
    *,
    case_id: UUID,
    case_reference: str,
    expected_case_version: int,
    human_resume_reference: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> AnalysisExecution:
    """Analyze immutable requester information after a verified resume handoff."""

    claimed = _claim_attempt(
        database_url,
        provider,
        case_id=case_id,
        case_reference=case_reference,
        expected_case_version=expected_case_version,
        workflow_start_reference=None,
        lease_seconds=lease_seconds,
        human_resume_reference=human_resume_reference,
    )
    return _execute_claimed_analysis(database_url, provider, claimed)
