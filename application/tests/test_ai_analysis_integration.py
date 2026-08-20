"""Controlled fixture-provider integration check for the AI-analysis boundary."""

from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_analysis import (
    AnalysisInProgress,
    FixtureAnalysisProvider,
    FixtureResponse,
    analyze_case,
    canonical_input_sha256,
)


def concise_exception_hook(
    exception_type: type[BaseException],
    exception: BaseException,
    exception_traceback: Any,
) -> None:
    frames = traceback.extract_tb(exception_traceback)
    location = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "unknown"
    print(
        f"FAIL: {exception_type.__name__}: {exception} [{location}]",
        file=sys.stderr,
    )


sys.excepthook = concise_exception_hook


DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
API_URL = os.environ["PRIMARY_API_URL"].rstrip("/")
PRIMARY_WORKFLOW_TOKEN = os.environ["PRIMARY_WORKFLOW_TOKEN"]
ENDPOINT_FIXTURE_FILE = os.environ["AI_ANALYSIS_FIXTURE_FILE"]

EMPLOYEE_REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000002")
ADMIN_ID = UUID("10000000-0000-4000-8000-000000000004")

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


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def http_json(
    url: str,
    *,
    body: dict[str, Any],
    token: str | None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            raw_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw_body = error.read()
    try:
        parsed = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"HTTP {status} response was not JSON: {raw_body[:300]!r}"
        ) from error
    require(isinstance(parsed, dict), "HTTP response was not a JSON object")
    return status, parsed


def create_case(
    label: str,
    subject: str,
    original_message: str,
    *,
    requester_id: UUID = EMPLOYEE_REQUESTER_ID,
    content_fingerprint: str | None = None,
    analyzing: bool = True,
) -> dict[str, Any]:
    """Create a fictional case at the accepted analysis entry boundary."""

    external_request_id = f"ANALYSIS-{label}"
    idempotency_key = hashlib.sha256(external_request_id.encode()).hexdigest()
    fingerprint = content_fingerprint or hashlib.sha256(
        f"{label}|{subject}|{original_message}".encode("utf-8")
    ).hexdigest()
    workflow_key = hashlib.sha256(
        f"WORKFLOW|{external_request_id}".encode()
    ).hexdigest()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        reference_number = connection.execute(
            "SELECT nextval('case_reference_sequence') AS number"
        ).fetchone()["number"]
        case_reference = f"CASE-2026-{reference_number:04d}"
        case = connection.execute(
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
                current_state,
                version,
                received_at
            )
            VALUES (%s, 'WEBHOOK', %s, %s, %s, %s, %s, %s,
                    '[]'::jsonb, %s, %s, %s)
            RETURNING case_id
            """,
            (
                case_reference,
                external_request_id,
                idempotency_key,
                fingerprint,
                requester_id,
                subject,
                original_message,
                "ANALYZING" if analyzing else "RECEIVED",
                2 if analyzing else 1,
                datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        ).fetchone()
        case_id = case["case_id"]
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state,
                event_type, actor_type, reason, event_payload
            )
            VALUES (%s, 1, NULL, 'RECEIVED', 'CASE_RECEIVED',
                    'INTEGRATION', 'Controlled fictional analysis fixture.', %s)
            """,
            (case_id, Jsonb({"source": "CONTROLLED_FIXTURE"})),
        )
        workflow_start_reference = None
        if analyzing:
            event = connection.execute(
                """
                INSERT INTO case_events (
                    case_id, sequence_number, from_state, to_state,
                    event_type, actor_type, reason, event_payload
                )
                VALUES (%s, 2, 'RECEIVED', 'ANALYZING', 'ANALYSIS_STARTED',
                        'INTEGRATION', 'Controlled workflow-start fixture.', %s)
                RETURNING event_id
                """,
                (
                    case_id,
                    Jsonb(
                        {
                            "schema_version": "1",
                            "trigger_event": "CASE_RECEIVED",
                            "workflow_start_idempotency_key": workflow_key,
                        }
                    ),
                ),
            ).fetchone()
            workflow_start_reference = f"WFSTART-{event['event_id']}"

    return {
        "case_id": case_id,
        "case_reference": case_reference,
        "expected_case_version": 2,
        "workflow_start_reference": workflow_start_reference,
        "subject": subject,
        "original_message": original_message,
    }


def proposal(
    request_type: str,
    summary: str,
    values: dict[str, Any],
    *,
    evidence_quotes: dict[str, str] | None = None,
) -> dict[str, Any]:
    fields = {field_name: None for field_name in FIELD_NAMES}
    fields.update(values)
    quotes = evidence_quotes or {}
    evidence = []
    for field_name in FIELD_NAMES:
        value = fields[field_name]
        if value is not None and str(value).strip():
            evidence.append(
                {
                    "field": field_name,
                    "quote": quotes.get(field_name, str(value)),
                }
            )
    return {
        "request_type": request_type,
        "summary": summary,
        "fields": fields,
        "evidence": evidence,
    }


def result_response(
    configured_proposal: Any,
    *,
    delay_ms: int = 0,
) -> FixtureResponse:
    return FixtureResponse(
        kind="result",
        proposal=configured_proposal,
        wall_time_ms=1,
        input_tokens=0,
        output_tokens=0,
        delay_ms=delay_ms,
    )


def fixture_provider(
    subject: str,
    original_message: str,
    *responses: FixtureResponse,
) -> FixtureAnalysisProvider:
    return FixtureAnalysisProvider(
        {
            canonical_input_sha256(subject, original_message): list(responses)
        }
    )


def execute(
    case: dict[str, Any],
    provider: FixtureAnalysisProvider,
    *,
    lease_seconds: int = 240,
) -> Any:
    return analyze_case(
        DATABASE_URL,
        provider,
        case_id=case["case_id"],
        case_reference=case["case_reference"],
        expected_case_version=case["expected_case_version"],
        workflow_start_reference=case["workflow_start_reference"],
        lease_seconds=lease_seconds,
    )


def case_snapshot(case_id: UUID) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        case = connection.execute(
            """
            SELECT request_type, ai_summary, current_state, version
            FROM cases
            WHERE case_id = %s
            """,
            (case_id,),
        ).fetchone()
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM ai_analysis_runs WHERE case_id = %s)
                    AS attempts,
                (SELECT count(*) FROM validation_runs WHERE case_id = %s)
                    AS validations,
                (SELECT count(*) FROM case_details WHERE case_id = %s)
                    AS details,
                (SELECT count(*) FROM approvals WHERE case_id = %s)
                    AS approvals,
                (SELECT count(*) FROM case_events WHERE case_id = %s)
                    AS events
            """,
            (case_id, case_id, case_id, case_id, case_id),
        ).fetchone()
    require(case is not None, "case snapshot was not found")
    return {**dict(case), **dict(counts)}


def attempt_rows(case_id: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        rows = connection.execute(
            """
            SELECT analysis_run_id, attempt_number, status, proposal,
                   completed_at
            FROM ai_analysis_runs
            WHERE case_id = %s
            ORDER BY attempt_number
            """,
            (case_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_validation(case_id: UUID) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT overall_decision, missing_fields, rule_results
            FROM validation_runs
            WHERE case_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
    require(row is not None, "validation evidence was not found")
    return dict(row)


print("AI Service Request Automation - AI-analysis fixture integration check")
print("Scope: 10 contract groups; fictional data; fixture provider; no AI call.")
print("")

with psycopg.connect(DATABASE_URL) as connection:
    baseline = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM ai_analysis_runs),
            (SELECT count(*) FROM validation_runs),
            (SELECT count(*) FROM approvals),
            (SELECT count(*) FROM outbox_messages),
            (SELECT count(*) FROM delivery_attempts),
            (SELECT count(*) FROM policy_documents),
            (SELECT count(*) FROM policy_chunks)
        """
    ).fetchone()
require(baseline == (0, 0, 0, 0, 0, 0, 0), f"Unexpected baseline: {baseline}")

# Group 1: authenticate and guard the stable-reference entry before provider use.
endpoint_document = json.loads(Path(ENDPOINT_FIXTURE_FILE).read_text(encoding="utf-8"))
endpoint_input = endpoint_document["fixtures"][0]
endpoint_case = create_case(
    "ENTRY-0001",
    endpoint_input["subject"],
    endpoint_input["original_message"],
)
endpoint_url = f"{API_URL}/internal/v1/cases/{endpoint_case['case_id']}/analysis"
endpoint_body = {
    "schema_version": "1",
    "case_reference": endpoint_case["case_reference"],
    "expected_case_version": 2,
    "workflow_start_reference": endpoint_case["workflow_start_reference"],
}
status, body = http_json(endpoint_url, body=endpoint_body, token=None)
require(status == 401 and body["error_code"] == "AUTHENTICATION_REQUIRED", "Missing analysis authentication was accepted")
status, body = http_json(
    endpoint_url,
    body={**endpoint_body, "unexpected": "rejected"},
    token=PRIMARY_WORKFLOW_TOKEN,
)
require(status == 422 and body["error_code"] == "INVALID_ANALYSIS_REQUEST", "Extra analysis input was accepted")
status, body = http_json(
    endpoint_url,
    body={**endpoint_body, "expected_case_version": 3},
    token=PRIMARY_WORKFLOW_TOKEN,
)
require(status == 409 and body["error_code"] == "ANALYSIS_CONFLICT", "Wrong analysis version was accepted")
status, body = http_json(
    endpoint_url,
    body={**endpoint_body, "workflow_start_reference": "WFSTART-999999999"},
    token=PRIMARY_WORKFLOW_TOKEN,
)
require(status == 409 and body["error_code"] == "ANALYSIS_CONFLICT", "Wrong workflow reference was accepted")
require(case_snapshot(endpoint_case["case_id"])["attempts"] == 0, "Rejected entry mutated analysis evidence")
status, body = http_json(
    endpoint_url,
    body=endpoint_body,
    token=PRIMARY_WORKFLOW_TOKEN,
)
require(
    status == 200
    and body["validation_decision"] == "READY"
    and body["current_state"] == "READY_FOR_ACTION"
    and body["provider_called"] is True,
    f"Authenticated analysis entry failed: {body}",
)
print("[1/10] Authentication, state, version, and workflow-reference guards: PASS")

# Group 2: all 5 request types and unrelated-null enforcement.
valid_specs = [
    (
        "TYPE-POLICY",
        "Remote work policy question",
        "Please explain remote work policy and how many remote days are allowed.",
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "policy_question",
            "Question about allowed remote work days.",
            {
                "policy_topic": "remote work policy",
                "question": "how many remote days are allowed",
            },
        ),
        "POLICY_QUESTION",
        "ANALYZING",
    ),
    (
        "TYPE-INCIDENT",
        "WMS incident",
        "WMS login is failing. Impact high and urgency medium.",
        AGENT_REQUESTER_ID,
        proposal(
            "incident_report",
            "WMS login failure with high impact.",
            {
                "affected_service": "WMS",
                "incident_description": "WMS login is failing",
                "impact": "high",
                "urgency": "medium",
            },
        ),
        "INCIDENT_REPORT",
        "READY_FOR_ACTION",
    ),
    (
        "TYPE-ACCESS",
        "WMS viewer access",
        "Request WMS VIEWER access for weekly inventory reporting. Approver MGR-104.",
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "access_request",
            "WMS viewer access for weekly reporting.",
            {
                "target_system": "WMS",
                "requested_access_level": "VIEWER",
                "business_reason": "weekly inventory reporting",
                "approver_id": "MGR-104",
            },
        ),
        "ACCESS_REQUEST",
        "PENDING_APPROVAL",
    ),
    (
        "TYPE-DATA",
        "Supplier bank update",
        "In WMS change record SUP-448: change the supplier bank account for an approved supplier correction. Approver MGR-104.",
        AGENT_REQUESTER_ID,
        proposal(
            "data_change_request",
            "Approved supplier bank-account correction.",
            {
                "target_system": "WMS",
                "record_reference": "SUP-448",
                "requested_changes": "change the supplier bank account",
                "business_reason": "approved supplier correction",
                "approver_id": "MGR-104",
            },
        ),
        "DATA_CHANGE_REQUEST",
        "PENDING_APPROVAL",
    ),
    (
        "TYPE-STATUS",
        "Case status request",
        "Please show the current status of CASE-2026-0001.",
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "status_request",
            "Status request for an owned case.",
            {"case_reference": "CASE-2026-0001"},
        ),
        "STATUS_REQUEST",
        "READY_FOR_ACTION",
    ),
]
route_executions: list[Any] = []
for label, subject, message, requester_id, configured, db_type, state in valid_specs:
    configured_case = create_case(
        label,
        subject,
        message,
        requester_id=requester_id,
    )
    provider = fixture_provider(subject, message, result_response(configured))
    execution = execute(configured_case, provider)
    snapshot = case_snapshot(configured_case["case_id"])
    require(
        execution.validation_decision == "READY"
        and snapshot["request_type"] == db_type
        and snapshot["current_state"] == state
        and snapshot["details"] == 1,
        f"{label} mapping was inconsistent: {execution}; {snapshot}",
    )
    route_executions.append(execution)

unrelated_subject = "Incident with unrelated field"
unrelated_message = "WMS is slow. Impact low, urgency low, and quarterly review is mentioned."
unrelated_case = create_case("UNRELATED", unrelated_subject, unrelated_message)
unrelated_proposal = proposal(
    "incident_report",
    "WMS slowness report.",
    {
        "affected_service": "WMS",
        "incident_description": "WMS is slow",
        "impact": "low",
        "urgency": "low",
        "business_reason": "quarterly review",
    },
)
unrelated_execution = execute(
    unrelated_case,
    fixture_provider(
        unrelated_subject,
        unrelated_message,
        result_response(unrelated_proposal),
    ),
)
require(
    unrelated_execution.analysis_status == "INVALID_OUTPUT"
    and unrelated_execution.current_state == "NEEDS_REVIEW",
    "Unrelated populated field did not route to review",
)
route_executions.append(unrelated_execution)
print("[2/10] All 5 request types and unrelated-null enforcement: PASS")

# Group 3: derive missing fields and skip oversized input without provider use.
missing_subject = "Incomplete WMS access"
missing_message = "Please request WMS VIEWER access."
missing_case = create_case("MISSING", missing_subject, missing_message)
missing_proposal = proposal(
    "access_request",
    "Incomplete WMS access request.",
    {"target_system": "WMS", "requested_access_level": "VIEWER"},
)
missing_execution = execute(
    missing_case,
    fixture_provider(
        missing_subject,
        missing_message,
        result_response(missing_proposal),
    ),
)
missing_validation = latest_validation(missing_case["case_id"])
require(
    missing_execution.current_state == "NEEDS_INFORMATION"
    and missing_validation["missing_fields"] == ["business_reason", "approver_id"],
    f"Missing fields were not derived deterministically: {missing_validation}",
)
route_executions.append(missing_execution)

oversized_subject = "Oversized analysis input"
oversized_message = "X" * 8_100
oversized_case = create_case("OVERSIZED", oversized_subject, oversized_message)
oversized_provider = FixtureAnalysisProvider({})
oversized_execution = execute(oversized_case, oversized_provider)
require(
    oversized_execution.analysis_status == "SKIPPED"
    and oversized_execution.current_state == "NEEDS_REVIEW"
    and oversized_execution.provider_called is False
    and oversized_provider.call_count(oversized_subject, oversized_message) == 0,
    "Oversized input reached the provider or missed review",
)
route_executions.append(oversized_execution)
print("[3/10] Missing-field derivation and 8,000-character input guard: PASS")

# Group 4: evidence, enum, identifiers, reference data, permissions, ownership,
# duplicate handling, and final requester authorization.
with psycopg.connect(DATABASE_URL) as connection:
    connection.execute(
        """
        INSERT INTO user_roles (user_id, role_code)
        VALUES (%s, 'APPROVER')
        ON CONFLICT DO NOTHING
        """,
        (AGENT_REQUESTER_ID,),
    )

guard_specs: list[tuple[str, str, str, UUID, dict[str, Any], str, str]] = []
guard_specs.append(
    (
        "GUARD-EVIDENCE",
        "Evidence mismatch",
        "WMS is unavailable. Impact high and urgency high.",
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "incident_report",
            "WMS outage.",
            {
                "affected_service": "WMS",
                "incident_description": "WMS is unavailable",
                "impact": "high",
                "urgency": "high",
            },
            evidence_quotes={"incident_description": "not present in source"},
        ),
        "NEEDS_REVIEW",
        "INVALID_OUTPUT",
    )
)
guard_specs.append(
    (
        "GUARD-ENUM",
        "Invalid impact enum",
        "WMS is degraded. Impact urgent and urgency high.",
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "incident_report",
            "WMS degradation.",
            {
                "affected_service": "WMS",
                "incident_description": "WMS is degraded",
                "impact": "urgent",
                "urgency": "high",
            },
        ),
        "NEEDS_REVIEW",
        "INVALID_OUTPUT",
    )
)
guard_specs.append(
    (
        "GUARD-IDENTIFIER",
        "Truncated approver",
        "Request WMS VIEWER access for reporting. Approver MGR-104.",
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "access_request",
            "Access request with a truncated approver.",
            {
                "target_system": "WMS",
                "requested_access_level": "VIEWER",
                "business_reason": "reporting",
                "approver_id": "MGR-10",
            },
            evidence_quotes={"approver_id": "MGR-104"},
        ),
        "NEEDS_REVIEW",
        "COMPLETED",
    )
)
guard_specs.append(
    (
        "GUARD-SYSTEM",
        "Unknown target system",
        "Request LEGACY VIEWER access for reporting. Approver MGR-104.",
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "access_request",
            "Unknown-system access request.",
            {
                "target_system": "LEGACY",
                "requested_access_level": "VIEWER",
                "business_reason": "reporting",
                "approver_id": "MGR-104",
            },
        ),
        "NEEDS_REVIEW",
        "COMPLETED",
    )
)
guard_specs.append(
    (
        "GUARD-PERMISSION",
        "Requester permission",
        "Request WMS VIEWER access for reporting. Approver MGR-104.",
        AGENT_REQUESTER_ID,
        proposal(
            "access_request",
            "Unauthorized access request.",
            {
                "target_system": "WMS",
                "requested_access_level": "VIEWER",
                "business_reason": "reporting",
                "approver_id": "MGR-104",
            },
        ),
        "REJECTED",
        "COMPLETED",
    )
)
guard_specs.append(
    (
        "GUARD-APPROVER",
        "Approver permission",
        "Request WMS VIEWER access for reporting. Approver AGT-301.",
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "access_request",
            "Access request with an unauthorized approver.",
            {
                "target_system": "WMS",
                "requested_access_level": "VIEWER",
                "business_reason": "reporting",
                "approver_id": "AGT-301",
            },
        ),
        "NEEDS_REVIEW",
        "COMPLETED",
    )
)
guard_specs.append(
    (
        "GUARD-OWNERSHIP",
        "Unauthorized case status",
        "Please show the current status of CASE-2026-0001.",
        AGENT_REQUESTER_ID,
        proposal(
            "status_request",
            "Status request for another requester case.",
            {"case_reference": "CASE-2026-0001"},
        ),
        "REJECTED",
        "COMPLETED",
    )
)
duplicate_subject = "Possible duplicate incident"
duplicate_message = "WMS is slow. Impact low and urgency low."
duplicate_fingerprint = hashlib.sha256(b"controlled-duplicate").hexdigest()
create_case(
    "DUPLICATE-BASE",
    duplicate_subject,
    duplicate_message,
    content_fingerprint=duplicate_fingerprint,
    analyzing=False,
)
guard_specs.append(
    (
        "GUARD-DUPLICATE",
        duplicate_subject,
        duplicate_message,
        EMPLOYEE_REQUESTER_ID,
        proposal(
            "incident_report",
            "Possible duplicate WMS slowness report.",
            {
                "affected_service": "WMS",
                "incident_description": "WMS is slow",
                "impact": "low",
                "urgency": "low",
            },
        ),
        "NEEDS_REVIEW",
        "COMPLETED",
    )
)
guard_specs.append(
    (
        "GUARD-REQUESTER",
        "Requester authorization",
        "WMS is slow. Impact low and urgency low.",
        ADMIN_ID,
        proposal(
            "incident_report",
            "Request from a user without the requester role.",
            {
                "affected_service": "WMS",
                "incident_description": "WMS is slow",
                "impact": "low",
                "urgency": "low",
            },
        ),
        "REJECTED",
        "COMPLETED",
    )
)

for label, subject, message, requester_id, configured, state, analysis_status in guard_specs:
    fingerprint = duplicate_fingerprint if label == "GUARD-DUPLICATE" else None
    guarded_case = create_case(
        label,
        subject,
        message,
        requester_id=requester_id,
        content_fingerprint=fingerprint,
    )
    guarded_execution = execute(
        guarded_case,
        fixture_provider(subject, message, result_response(configured)),
    )
    require(
        guarded_execution.current_state == state
        and guarded_execution.analysis_status == analysis_status,
        f"{label} guard was inconsistent: {guarded_execution}",
    )
    route_executions.append(guarded_execution)
print("[4/10] Evidence, enum, identifier, permission, ownership, and duplicate guards: PASS")

# Group 5: verify every accepted route and pending-only human approval boundary.
observed_states = {execution.current_state for execution in route_executions}
require(
    {
        "ANALYZING",
        "NEEDS_INFORMATION",
        "NEEDS_REVIEW",
        "REJECTED",
        "PENDING_APPROVAL",
        "READY_FOR_ACTION",
    }.issubset(observed_states),
    f"Safe state coverage was incomplete: {sorted(observed_states)}",
)
with psycopg.connect(DATABASE_URL) as connection:
    approval_evidence = connection.execute(
        """
        SELECT count(*), count(*) FILTER (
            WHERE decision = 'PENDING' AND decided_at IS NULL
        )
        FROM approvals
        """
    ).fetchone()
require(approval_evidence == (2, 2), f"Approval boundary failed: {approval_evidence}")
print("[5/10] Review, information, rejection, approval, retrieval, and action routes: PASS")

# Group 6: one provider call under concurrency and exact replay afterward.
concurrency_subject = "Concurrent incident"
concurrency_message = "WMS is unavailable. Impact high and urgency medium."
concurrency_case = create_case(
    "CONCURRENCY",
    concurrency_subject,
    concurrency_message,
)
concurrency_proposal = proposal(
    "incident_report",
    "Concurrent WMS incident.",
    {
        "affected_service": "WMS",
        "incident_description": "WMS is unavailable",
        "impact": "high",
        "urgency": "medium",
    },
)
concurrency_provider = fixture_provider(
    concurrency_subject,
    concurrency_message,
    result_response(concurrency_proposal, delay_ms=200),
)
barrier = threading.Barrier(2)


def concurrent_analysis_call() -> tuple[str, Any]:
    barrier.wait(timeout=5)
    try:
        return "result", execute(concurrency_case, concurrency_provider)
    except AnalysisInProgress as error:
        return "in_progress", error


with futures.ThreadPoolExecutor(max_workers=2) as executor:
    concurrent_results = list(
        executor.map(lambda _item: concurrent_analysis_call(), range(2))
    )
result_executions = [value for kind, value in concurrent_results if kind == "result"]
require(result_executions, "Concurrent analysis produced no completed invocation")
require(
    concurrency_provider.call_count(concurrency_subject, concurrency_message) == 1
    and sum(result.provider_called for result in result_executions) == 1,
    f"Concurrent provider calls were duplicated: {concurrent_results}",
)
replay = execute(concurrency_case, concurrency_provider)
require(
    replay.idempotent_replay
    and replay.provider_called is False
    and case_snapshot(concurrency_case["case_id"])["attempts"] == 1,
    "Exact replay repeated analysis work",
)
print("[6/10] Concurrent invocation and exact replay use 1 provider call: PASS")

# Group 7: retry, invalid output, and abandoned-attempt recovery.
transient_subject = "Transient provider incident"
transient_message = "WMS is unavailable. Impact high and urgency high."
transient_case = create_case("TRANSIENT", transient_subject, transient_message)
transient_proposal = proposal(
    "incident_report",
    "WMS incident after a controlled retry.",
    {
        "affected_service": "WMS",
        "incident_description": "WMS is unavailable",
        "impact": "high",
        "urgency": "high",
    },
)
transient_provider = fixture_provider(
    transient_subject,
    transient_message,
    FixtureResponse(
        kind="retryable_failure",
        error_code="FIXTURE_TIMEOUT",
        message="Controlled fixture timeout.",
        wall_time_ms=1,
    ),
    result_response(transient_proposal),
)
first_transient = execute(transient_case, transient_provider)
second_transient = execute(transient_case, transient_provider)
require(
    first_transient.outcome == "RETRYABLE_FAILURE"
    and first_transient.current_state == "ANALYZING"
    and second_transient.validation_decision == "READY"
    and second_transient.attempt_number == 2,
    "Bounded transient retry did not recover",
)

invalid_subject = "Invalid provider output"
invalid_message = "This controlled input receives an incomplete JSON object."
invalid_case = create_case("INVALID-OUTPUT", invalid_subject, invalid_message)
invalid_execution = execute(
    invalid_case,
    fixture_provider(
        invalid_subject,
        invalid_message,
        result_response({"request_type": "incident_report"}),
    ),
)
require(
    invalid_execution.analysis_status == "INVALID_OUTPUT"
    and invalid_execution.current_state == "NEEDS_REVIEW",
    "Invalid provider output did not stop at review",
)

abandoned_subject = "Abandoned provider attempt"
abandoned_message = "WMS is unavailable. Impact medium and urgency medium."
abandoned_case = create_case("ABANDONED", abandoned_subject, abandoned_message)
abandoned_proposal = proposal(
    "incident_report",
    "Recovered WMS incident.",
    {
        "affected_service": "WMS",
        "incident_description": "WMS is unavailable",
        "impact": "medium",
        "urgency": "medium",
    },
)
abandoned_provider = fixture_provider(
    abandoned_subject,
    abandoned_message,
    result_response(abandoned_proposal),
)
with psycopg.connect(DATABASE_URL) as connection:
    connection.execute(
        """
        INSERT INTO ai_analysis_runs (
            case_id, model_name, model_identifier, prompt_contract_version,
            input_sha256, proposal, evidence, status, wall_time_ms,
            input_tokens, output_tokens, attempt_number, created_at
        )
        VALUES (%s, %s, %s, 'analysis-v1', %s, '{}'::jsonb, '[]'::jsonb,
                'PROCESSING', 0, 0, 0, 1, now() - interval '10 minutes')
        """,
        (
            abandoned_case["case_id"],
            abandoned_provider.model_name,
            abandoned_provider.model_identifier,
            canonical_input_sha256(abandoned_subject, abandoned_message),
        ),
    )
abandoned_execution = execute(abandoned_case, abandoned_provider)
abandoned_attempts = attempt_rows(abandoned_case["case_id"])
require(
    abandoned_execution.attempt_number == 2
    and abandoned_execution.validation_decision == "READY"
    and [row["status"] for row in abandoned_attempts] == ["FAILED", "COMPLETED"]
    and abandoned_attempts[0]["proposal"]["error"]["code"] == "ANALYSIS_LEASE_EXPIRED",
    f"Abandoned attempt recovery was inconsistent: {abandoned_attempts}",
)
print("[7/10] Transient retry, invalid output, and abandoned recovery: PASS")

# Group 8: force final-transaction failure, verify no partial business write,
# then recover the honestly unknown provider outcome.
rollback_subject = "Atomic rollback incident"
rollback_message = "WMS is unavailable. Impact medium and urgency high."
rollback_case = create_case("ROLLBACK", rollback_subject, rollback_message)
rollback_proposal = proposal(
    "incident_report",
    "WMS incident used for atomic rollback.",
    {
        "affected_service": "WMS",
        "incident_description": "WMS is unavailable",
        "impact": "medium",
        "urgency": "high",
    },
)
rollback_provider = fixture_provider(
    rollback_subject,
    rollback_message,
    result_response(rollback_proposal),
    result_response(rollback_proposal),
)
with psycopg.connect(DATABASE_URL) as connection:
    connection.execute(
        """
        CREATE FUNCTION controlled_validation_failure()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'controlled validation persistence failure';
        END;
        $$
        """
    )
    connection.execute(
        """
        CREATE TRIGGER controlled_validation_failure
        BEFORE INSERT ON validation_runs
        FOR EACH ROW
        EXECUTE FUNCTION controlled_validation_failure()
        """
    )
try:
    execute(rollback_case, rollback_provider)
    raise AssertionError("The controlled finalization failure was not raised")
except psycopg.Error:
    pass
partial_snapshot = case_snapshot(rollback_case["case_id"])
partial_attempts = attempt_rows(rollback_case["case_id"])
require(
    partial_snapshot["current_state"] == "ANALYZING"
    and partial_snapshot["version"] == 2
    and partial_snapshot["validations"] == 0
    and partial_snapshot["details"] == 0
    and partial_snapshot["approvals"] == 0
    and partial_snapshot["events"] == 2
    and len(partial_attempts) == 1
    and partial_attempts[0]["status"] == "PROCESSING",
    f"Finalization failure left partial business data: {partial_snapshot}",
)
with psycopg.connect(DATABASE_URL) as connection:
    connection.execute("DROP TRIGGER controlled_validation_failure ON validation_runs")
    connection.execute("DROP FUNCTION controlled_validation_failure()")
rollback_recovery = execute(rollback_case, rollback_provider, lease_seconds=0)
require(
    rollback_recovery.attempt_number == 2
    and rollback_recovery.validation_decision == "READY"
    and rollback_provider.call_count(rollback_subject, rollback_message) == 2,
    "The rolled-back finalization did not recover safely",
)
print("[8/10] Final transaction rollback leaves no partial accepted business data: PASS")

# Group 9: stable append-only aggregates after replay, retry, and recovery.
with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = connection.execute(
        """
        SELECT
            (SELECT count(DISTINCT case_id) FROM ai_analysis_runs)
                AS analyzed_cases,
            (SELECT count(*) FROM ai_analysis_runs) AS attempts,
            (SELECT count(*) FROM validation_runs) AS validations,
            (SELECT count(*) FROM ai_analysis_runs
             WHERE status = 'PROCESSING') AS processing,
            (SELECT count(*) FROM ai_analysis_runs
             WHERE completed_at IS NULL) AS incomplete_time,
            (SELECT count(*) FROM (
                SELECT case_id, input_sha256, prompt_contract_version,
                       model_identifier, attempt_number
                FROM ai_analysis_runs
                GROUP BY case_id, input_sha256, prompt_contract_version,
                         model_identifier, attempt_number
                HAVING count(*) > 1
            ) AS duplicate_identity) AS duplicate_identities
        """
    ).fetchone()
    statuses = {
        row["status"]: row["count"]
        for row in connection.execute(
            "SELECT status, count(*) AS count FROM ai_analysis_runs GROUP BY status"
        ).fetchall()
    }
    decisions = {
        row["overall_decision"]: row["count"]
        for row in connection.execute(
            """
            SELECT overall_decision, count(*) AS count
            FROM validation_runs
            GROUP BY overall_decision
            """
        ).fetchall()
    }
require(
    dict(aggregate)
    == {
        "analyzed_cases": 23,
        "attempts": 26,
        "validations": 23,
        "processing": 0,
        "incomplete_time": 0,
        "duplicate_identities": 0,
    },
    f"Unexpected analysis aggregate: {dict(aggregate)}",
)
require(
    statuses == {"COMPLETED": 18, "FAILED": 3, "INVALID_OUTPUT": 4, "SKIPPED": 1},
    f"Unexpected analysis statuses: {statuses}",
)
require(
    decisions == {"READY": 10, "NEEDS_INFORMATION": 1, "NEEDS_REVIEW": 9, "REJECTED": 3},
    f"Unexpected validation decisions: {decisions}",
)
print("[9/10] Append-only attempts, validations, and aggregate counts: PASS")

# Group 10: confirm deferred boundaries were untouched.
with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    isolation = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM policy_documents) AS policy_documents,
            (SELECT count(*) FROM policy_chunks) AS policy_chunks,
            (SELECT count(*) FROM outbox_messages) AS outbox_messages,
            (SELECT count(*) FROM delivery_attempts) AS delivery_attempts,
            (SELECT count(*) FROM case_details) AS accepted_details,
            (SELECT count(*) FROM approvals) AS approvals,
            (SELECT count(*) FROM approvals
             WHERE decision <> 'PENDING' OR decided_at IS NOT NULL)
                AS decided_approvals,
            (SELECT count(*) FROM cases WHERE current_state = 'COMPLETED')
                AS completed_cases,
            (SELECT count(DISTINCT model_name) FROM ai_analysis_runs
             WHERE model_name <> 'fixture-provider') AS non_fixture_models
        """
    ).fetchone()
require(
    dict(isolation)
    == {
        "policy_documents": 0,
        "policy_chunks": 0,
        "outbox_messages": 0,
        "delivery_attempts": 0,
        "accepted_details": 10,
        "approvals": 2,
        "decided_approvals": 0,
        "completed_cases": 0,
        "non_fixture_models": 0,
    },
    f"Deferred boundary isolation failed: {dict(isolation)}",
)
print("[10/10] Retrieval, delivery, notification, and approval-decision isolation: PASS")

print("")
print("AI-analysis fixture integration summary")
print("  Integration groups: 10/10 PASS")
print("  Fictional analyzed cases: 23")
print("  Durable analysis attempts: 26")
print("  Deterministic validation records: 23")
print("  Accepted structured details: 10")
print("  Pending human approvals: 2")
print("  Duplicate attempt identities: 0")
print("  Unfinished analysis attempts: 0")
print("  External AI calls: 0")
print("  Fixture integration gate: PASS")
