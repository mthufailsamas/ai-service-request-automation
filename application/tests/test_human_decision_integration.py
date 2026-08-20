"""Focused signed-session human-decision integration check."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from human_decision import (
    HumanDecisionCommand,
    HumanDecisionConflict,
    execute_human_decision,
)
from main import (
    SESSION_COOKIE_NAME,
    create_csrf_token,
    create_session_cookie,
)


def concise_exception_hook(
    kind: type[BaseException], error: BaseException, tb: Any
) -> None:
    frames = traceback.extract_tb(tb)
    location = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "unknown"
    print(f"FAIL: {kind.__name__}: {error} [{location}]", file=sys.stderr)


sys.excepthook = concise_exception_hook

DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
API_URL = os.environ["PRIMARY_API_URL"].rstrip("/")
SESSION_SECRET = os.environ["APP_SESSION_SECRET"]

REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("10000000-0000-4000-8000-000000000002")
APPROVER_ID = UUID("10000000-0000-4000-8000-000000000003")
ADMIN_ID = UUID("10000000-0000-4000-8000-000000000004")
WMS_ID = UUID("20000000-0000-4000-8000-000000000001")
CRM_ID = UUID("20000000-0000-4000-8000-000000000002")


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def post_command(
    case_reference: str,
    body: dict[str, Any],
    *,
    user_id: UUID | None,
    csrf_override: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if user_id is not None:
        cookie = create_session_cookie(user_id, SESSION_SECRET)
        headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie}"
        headers["X-CSRF-Token"] = (
            csrf_override
            if csrf_override is not None
            else create_csrf_token(cookie, str(body["command_id"]), SESSION_SECRET)
        )
    request = urllib.request.Request(
        f"{API_URL}/api/v1/cases/{case_reference}/human-decisions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def command(action: str, **values: Any) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "command_id": str(uuid4()),
        "expected_case_version": 1,
        "action": action,
        **values,
    }


def empty_fields() -> dict[str, str | None]:
    return {
        "policy_topic": None,
        "question": None,
        "affected_service": None,
        "incident_description": None,
        "impact": None,
        "urgency": None,
        "target_system": None,
        "requested_access_level": None,
        "business_reason": None,
        "approver_id": None,
        "record_reference": None,
        "requested_changes": None,
        "case_reference": None,
    }


def make_case(
    number: int,
    state: str,
    *,
    requester_id: UUID = REQUESTER_ID,
    request_type: str | None = None,
    target_system_id: UUID | None = None,
    approval_user_id: UUID | None = None,
) -> dict[str, Any]:
    case_id = uuid4()
    case_reference = f"CASE-2026-{7000 + number:04d}"
    external_id = f"HUMAN-DECISION-{number:02d}"
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        connection.execute(
            """
            INSERT INTO cases (
                case_id,
                case_reference,
                source_channel,
                external_request_id,
                idempotency_key,
                content_fingerprint,
                requester_id,
                subject,
                original_message,
                attachment_metadata,
                request_type,
                current_state,
                version,
                received_at
            )
            VALUES (
                %s, %s, 'WEBHOOK', %s, %s, %s, %s,
                'Fictional human-decision fixture',
                'Controlled local human-decision input.',
                '[]', %s, %s, 1, %s
            )
            """,
            (
                case_id,
                case_reference,
                external_id,
                digest,
                digest,
                requester_id,
                request_type,
                state,
                datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
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
                reason,
                event_payload
            )
            VALUES (
                %s, 1, NULL, %s, 'HUMAN_DECISION_FIXTURE_CREATED',
                'INTEGRATION', 'Fictional focused-check state.', '{}'
            )
            """,
            (case_id, state),
        )
        if approval_user_id is not None:
            connection.execute(
                """
                INSERT INTO case_details (
                    case_id,
                    target_system_id,
                    requested_access_level,
                    business_reason,
                    approver_user_id,
                    record_reference,
                    requested_changes,
                    accepted_by_type,
                    accepted_at
                )
                VALUES (
                    %s, %s,
                    CASE WHEN %s = 'ACCESS_REQUEST' THEN 'STANDARD' END,
                    'Controlled business reason.', %s,
                    CASE WHEN %s = 'DATA_CHANGE_REQUEST' THEN 'REC-7001' END,
                    CASE WHEN %s = 'DATA_CHANGE_REQUEST' THEN 'Update owner.' END,
                    'SYSTEM_RULE', now()
                )
                """,
                (
                    case_id,
                    target_system_id,
                    request_type,
                    approval_user_id,
                    request_type,
                    request_type,
                ),
            )
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
                (case_id, approval_user_id, request_type),
            )
    return {"case_id": case_id, "case_reference": case_reference}


def add_confirmable_access_proposal(case_id: UUID) -> None:
    analysis_id = uuid4()
    digest = hashlib.sha256(str(case_id).encode("utf-8")).hexdigest()
    fields = empty_fields()
    fields.update(
        {
            "target_system": "WMS",
            "requested_access_level": "STANDARD",
            "business_reason": "Support inventory reconciliation.",
            "approver_id": "MGR-104",
        }
    )
    proposal = {
        "request_type": "access_request",
        "summary": "Grant standard WMS access for inventory work.",
        "fields": fields,
        "evidence": [],
    }
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            """
            INSERT INTO ai_analysis_runs (
                analysis_run_id,
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
                %s, %s, 'fixture-provider', 'human-decision-fixture-v1',
                'analysis-v1', %s, %s, '[]', 'COMPLETED', 1, 0, 0, 1, now()
            )
            """,
            (analysis_id, case_id, digest, Jsonb(proposal)),
        )


def case_snapshot(case_id: UUID) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT cases.request_type,
                   cases.current_state,
                   cases.version,
                   (SELECT count(*) FROM case_events
                    WHERE case_id = cases.case_id
                      AND event_payload ? 'human_command_id') AS human_events,
                   (SELECT count(*) FROM case_details
                    WHERE case_id = cases.case_id) AS details
            FROM cases
            WHERE case_id = %s
            """,
            (case_id,),
        ).fetchone()
    return dict(row)


print("AI Service Request Automation - human-decision integration check")
print("Scope: 6 focused groups; fictional data; signed sessions; 0 AI calls.")
print("")

guard_case = make_case(1, "NEEDS_INFORMATION")
requester_case = make_case(2, "NEEDS_INFORMATION")
confirm_case = make_case(3, "NEEDS_REVIEW")
add_confirmable_access_proposal(confirm_case["case_id"])
correction_case = make_case(4, "NEEDS_REVIEW")
agent_reject_case = make_case(5, "NEEDS_REVIEW")
approve_case = make_case(
    6,
    "PENDING_APPROVAL",
    request_type="ACCESS_REQUEST",
    target_system_id=WMS_ID,
    approval_user_id=APPROVER_ID,
)
reject_approval_case = make_case(
    7,
    "PENDING_APPROVAL",
    requester_id=AGENT_ID,
    request_type="DATA_CHANGE_REQUEST",
    target_system_id=WMS_ID,
    approval_user_id=APPROVER_ID,
)
wrong_assignee_case = make_case(
    8,
    "PENDING_APPROVAL",
    request_type="ACCESS_REQUEST",
    target_system_id=WMS_ID,
    approval_user_id=AGENT_ID,
)
missing_permission_case = make_case(
    9,
    "PENDING_APPROVAL",
    request_type="ACCESS_REQUEST",
    target_system_id=CRM_ID,
    approval_user_id=APPROVER_ID,
)
same_command_case = make_case(10, "NEEDS_INFORMATION")
competing_command_case = make_case(11, "NEEDS_REVIEW")

with psycopg.connect(DATABASE_URL) as connection:
    analysis_count_before = connection.execute(
        "SELECT count(*) FROM ai_analysis_runs"
    ).fetchone()[0]

# Group 1: the HTTP adapter and role boundary reject unsafe entry.
guard_body = command(
    "SUBMIT_INFORMATION",
    information="The missing controlled value is WMS.",
)
status, body = post_command(
    guard_case["case_reference"], guard_body, user_id=None
)
require(status == 401 and body["error_code"] == "AUTHENTICATION_REQUIRED", "missing session was accepted")
status, body = post_command(
    guard_case["case_reference"],
    guard_body,
    user_id=REQUESTER_ID,
    csrf_override="invalid",
)
require(status == 403 and body["error_code"] == "INVALID_CSRF_TOKEN", "invalid CSRF was accepted")
invalid_shape = dict(guard_body)
invalid_shape["unexpected"] = True
status, body = post_command(
    guard_case["case_reference"], invalid_shape, user_id=REQUESTER_ID
)
require(status == 422 and body["error_code"] == "INVALID_HUMAN_DECISION", "an extra command field was accepted")
status, body = post_command(
    guard_case["case_reference"], guard_body, user_id=ADMIN_ID
)
require(status == 403 and body["error_code"] == "HUMAN_DECISION_NOT_AUTHORIZED", "an unrelated role was accepted")
require(case_snapshot(guard_case["case_id"])["human_events"] == 0, "a rejected entry changed the case")
print("[1/6] Strict session, CSRF, command, state, and role guards: PASS")

# Group 2: only the owner supplies information; exact replay has no 2nd effect.
requester_body = command(
    "SUBMIT_INFORMATION",
    information="The requested access level is standard.",
)
status, first_requester = post_command(
    requester_case["case_reference"], requester_body, user_id=REQUESTER_ID
)
require(
    status == 200
    and first_requester["current_state"] == "ANALYZING"
    and first_requester["case_version"] == 2
    and first_requester["idempotent_replay"] is False,
    f"requester information did not commit: {first_requester}",
)
status, requester_replay = post_command(
    requester_case["case_reference"], requester_body, user_id=REQUESTER_ID
)
require(
    status == 200
    and requester_replay["idempotent_replay"] is True
    and requester_replay["human_decision_reference"]
    == first_requester["human_decision_reference"],
    "requester replay was not exact",
)
altered_requester = dict(requester_body)
altered_requester["information"] = "A different value."
status, body = post_command(
    requester_case["case_reference"], altered_requester, user_id=REQUESTER_ID
)
require(status == 409 and body["error_code"] == "HUMAN_DECISION_CONFLICT", "changed replay input was accepted")
owner_guard = command("SUBMIT_INFORMATION", information="Unauthorized owner input.")
status, body = post_command(
    guard_case["case_reference"], owner_guard, user_id=AGENT_ID
)
require(status == 403 and body["error_code"] == "HUMAN_DECISION_NOT_AUTHORIZED", "another requester changed the case")
snapshot = case_snapshot(requester_case["case_id"])
require(snapshot["human_events"] == 1 and snapshot["version"] == 2, "requester command duplicated durable effects")
print("[2/6] Requester ownership, durable information, and exact replay: PASS")

# Group 3: a service agent confirms, corrects, or rejects only review cases.
confirm_body = command("CONFIRM_REVIEW", note="Confirmed after checking the request.")
status, confirmed = post_command(
    confirm_case["case_reference"], confirm_body, user_id=AGENT_ID
)
require(status == 200 and confirmed["current_state"] == "ANALYZING", f"review confirmation failed: {confirmed}")
with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    confirmed_details = connection.execute(
        """
        SELECT cases.request_type,
               case_details.target_system_id,
               case_details.approver_user_id,
               case_details.accepted_by_type,
               case_details.accepted_by_user_id,
               (SELECT count(*) FROM approvals WHERE case_id = cases.case_id)
                   AS approvals
        FROM cases
        JOIN case_details ON case_details.case_id = cases.case_id
        WHERE cases.case_id = %s
        """,
        (confirm_case["case_id"],),
    ).fetchone()
require(
    confirmed_details["request_type"] == "ACCESS_REQUEST"
    and confirmed_details["target_system_id"] == WMS_ID
    and confirmed_details["approver_user_id"] == APPROVER_ID
    and confirmed_details["accepted_by_type"] == "SERVICE_AGENT"
    and confirmed_details["accepted_by_user_id"] == AGENT_ID
    and confirmed_details["approvals"] == 0,
    f"confirmed details were unsafe: {dict(confirmed_details)}",
)

incident_fields = empty_fields()
incident_fields.update(
    {
        "affected_service": "WMS",
        "incident_description": "Inventory sync is delayed.",
        "impact": "high",
        "urgency": "medium",
    }
)
invalid_fields = dict(incident_fields)
invalid_fields["policy_topic"] = "unrelated"
invalid_correction = command(
    "CORRECT_REVIEW",
    request_type="incident_report",
    summary="Corrected inventory synchronization incident.",
    fields=invalid_fields,
)
status, body = post_command(
    correction_case["case_reference"], invalid_correction, user_id=AGENT_ID
)
require(status == 422 and body["error_code"] == "INVALID_HUMAN_DECISION", "unrelated corrected data was accepted")
require(case_snapshot(correction_case["case_id"])["details"] == 0, "invalid correction left partial details")
correction_body = command(
    "CORRECT_REVIEW",
    request_type="incident_report",
    summary="Corrected inventory synchronization incident.",
    fields=incident_fields,
)
status, corrected = post_command(
    correction_case["case_reference"], correction_body, user_id=AGENT_ID
)
require(status == 200 and corrected["current_state"] == "ANALYZING", f"review correction failed: {corrected}")
reject_body = command("REJECT_REVIEW", note="The request is not valid after review.")
status, rejected = post_command(
    agent_reject_case["case_reference"], reject_body, user_id=AGENT_ID
)
require(status == 200 and rejected["current_state"] == "REJECTED", f"agent rejection failed: {rejected}")
print("[3/6] Service-agent confirmation, correction, rejection, and rollback: PASS")

# Group 4: only the assigned and permitted approver can decide pending work.
approve_body = command("APPROVE_REQUEST", note="Approved for the controlled fixture.")
status, approved = post_command(
    approve_case["case_reference"], approve_body, user_id=APPROVER_ID
)
require(status == 200 and approved["current_state"] == "READY_FOR_ACTION", f"approval failed: {approved}")
reject_approval_body = command("REJECT_REQUEST", note="The requested change is not authorized.")
status, rejected_approval = post_command(
    reject_approval_case["case_reference"],
    reject_approval_body,
    user_id=APPROVER_ID,
)
require(status == 200 and rejected_approval["current_state"] == "REJECTED", f"approval rejection failed: {rejected_approval}")
wrong_assignee_body = command("APPROVE_REQUEST")
status, body = post_command(
    wrong_assignee_case["case_reference"],
    wrong_assignee_body,
    user_id=APPROVER_ID,
)
require(status == 403 and body["error_code"] == "HUMAN_DECISION_NOT_AUTHORIZED", "an unassigned approver was accepted")
missing_permission_body = command("APPROVE_REQUEST")
status, body = post_command(
    missing_permission_case["case_reference"],
    missing_permission_body,
    user_id=APPROVER_ID,
)
require(status == 403 and body["error_code"] == "HUMAN_DECISION_NOT_AUTHORIZED", "an approver without system permission was accepted")
with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    approval_rows = connection.execute(
        """
        SELECT cases.external_request_id, approvals.decision,
               approvals.decision_note, approvals.decided_at
        FROM approvals
        JOIN cases ON cases.case_id = approvals.case_id
        ORDER BY cases.external_request_id
        """
    ).fetchall()
require(
    [row["decision"] for row in approval_rows]
    == ["APPROVED", "REJECTED", "PENDING", "PENDING"],
    f"approval evidence changed unexpectedly: {approval_rows}",
)
print("[4/6] Assigned-approver identity, permission, approval, and rejection: PASS")

# Group 5: row locking makes same-command replay singular and competitors safe.
same_body = command(
    "SUBMIT_INFORMATION",
    information="Concurrency fixture information.",
)
same_model = HumanDecisionCommand.model_validate_json(json.dumps(same_body))
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    same_results = list(
        executor.map(
            lambda _index: execute_human_decision(
                DATABASE_URL,
                case_reference=same_command_case["case_reference"],
                actor_user_id=REQUESTER_ID,
                command=same_model,
            ),
            range(2),
        )
    )
require(
    sorted(result.idempotent_replay for result in same_results) == [False, True]
    and len(
        {
            result.human_decision_reference
            for result in same_results
        }
    )
    == 1,
    f"same-command concurrency was not singular: {same_results}",
)
competing_bodies = [
    command("REJECT_REVIEW", note="Competing rejection A."),
    command("REJECT_REVIEW", note="Competing rejection B."),
]
competing_models = [
    HumanDecisionCommand.model_validate_json(json.dumps(body))
    for body in competing_bodies
]


def execute_competing(command_model: HumanDecisionCommand) -> str:
    try:
        execute_human_decision(
            DATABASE_URL,
            case_reference=competing_command_case["case_reference"],
            actor_user_id=AGENT_ID,
            command=command_model,
        )
        return "COMMITTED"
    except HumanDecisionConflict:
        return "CONFLICT"


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    futures = [executor.submit(execute_competing, item) for item in competing_models]
    competing_results = [future.result() for future in futures]
require(
    sorted(competing_results) == ["COMMITTED", "CONFLICT"]
    and case_snapshot(competing_command_case["case_id"])["human_events"] == 1,
    f"competing commands were not serialized safely: {competing_results}",
)
print("[5/6] Same-command replay and competing-command concurrency: PASS")

# Group 6: aggregate human evidence is exact and deferred boundaries are idle.
with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM cases
             WHERE external_request_id LIKE 'HUMAN-DECISION-%') AS cases,
            (SELECT count(*) FROM case_events
             WHERE event_payload ? 'human_command_id') AS human_events,
            (SELECT count(*) FROM case_events
             WHERE event_type = 'REQUESTER_INFORMATION_SUBMITTED')
                AS requester_submissions,
            (SELECT count(*) FROM case_events
             WHERE event_type IN (
                 'SERVICE_AGENT_REVIEW_CONFIRMED',
                 'SERVICE_AGENT_CORRECTION_ACCEPTED'
             )) AS agent_resumptions,
            (SELECT count(*) FROM case_events
             WHERE event_type = 'SERVICE_AGENT_REJECTED') AS agent_rejections,
            (SELECT count(*) FROM case_events
             WHERE event_type IN ('APPROVAL_APPROVED', 'APPROVAL_REJECTED'))
                AS approval_decisions,
            (SELECT count(*) FROM outbox_messages) AS outbox_messages,
            (SELECT count(*) FROM delivery_attempts) AS delivery_attempts,
            (SELECT count(*) FROM ai_analysis_runs) AS analysis_runs
        """
    ).fetchone()
require(
    dict(aggregate)
    == {
        "cases": 11,
        "human_events": 8,
        "requester_submissions": 2,
        "agent_resumptions": 2,
        "agent_rejections": 2,
        "approval_decisions": 2,
        "outbox_messages": 0,
        "delivery_attempts": 0,
        "analysis_runs": analysis_count_before,
    },
    f"aggregate or deferred-boundary evidence changed: {dict(aggregate)}",
)
print("[6/6] Aggregate evidence and AI, delivery, and notification isolation: PASS")

print("")
print("Human-decision integration summary")
print("  Integration groups: 6/6 PASS")
print("  Fictional human-decision cases: 11")
print("  Durable human-command events: 8")
print("  Requester information submissions: 2")
print("  Service-agent resumptions: 2")
print("  Service-agent rejections: 2")
print("  Approval decisions: 2")
print("  Duplicate command effects: 0")
print("  External AI calls: 0")
print("  Human-decision gate: PASS")
