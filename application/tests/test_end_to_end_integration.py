"""Controlled full-lifecycle integration across the five accepted request types."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from ai_analysis import FixtureAnalysisProvider, analyze_resumed_case
from delivery import ServiceDeskClient, process_one_message
from downstream_route import queue_approved_action, reconcile_approved_action
from human_resume import (
    HumanResumeClient,
    enqueue_next_human_resume,
    process_one_human_resume,
)
from human_resume_consumers import (
    LocalNotificationClient,
    materialize_terminal_notification,
    process_one_notification,
    reconcile_terminal_notification,
    route_reviewed_case,
)
from main import (
    SESSION_COOKIE_NAME,
    create_csrf_token,
    create_session_cookie,
)
from policy_retrieval import FixturePolicyProvider, retrieve_policy
from safe_action import queue_safe_action, reconcile_safe_action
from workflow_start import WorkflowStartClient, process_one_workflow_start


DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
API_URL = os.environ["PRIMARY_API_URL"].rstrip("/")
INTAKE_TOKEN = os.environ["INTAKE_WEBHOOK_TOKEN"]
SESSION_SECRET = os.environ["APP_SESSION_SECRET"]
WORKFLOW_START_URL = os.environ["N8N_WORKFLOW_START_URL"]
HUMAN_RESUME_URL = os.environ["N8N_HUMAN_RESUME_URL"]
N8N_TOKEN = os.environ["N8N_TOKEN"]
SANDBOX_URL = os.environ["SERVICE_DESK_SANDBOX_URL"]
SANDBOX_TOKEN = os.environ["SERVICE_DESK_SANDBOX_TOKEN"]
SANDBOX_DATABASE_URL = os.environ["SERVICE_DESK_SANDBOX_DATABASE_URL"]
FIXTURE_FILE = Path(os.environ["E2E_FIXTURE_FILE"])
RESULT_FILE = os.environ.get("E2E_RESULT_FILE", "").strip()
REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("10000000-0000-4000-8000-000000000002")
APPROVER_ID = UUID("10000000-0000-4000-8000-000000000003")


def concise_exception(
    exception_type: type[BaseException],
    exception: BaseException,
    exception_traceback: Any,
) -> None:
    frames = traceback.extract_tb(exception_traceback)
    location = f" [{frames[-1].name}:{frames[-1].lineno}]" if frames else ""
    print(f"FAIL: {exception_type.__name__}: {exception}{location}")


sys.excepthook = concise_exception


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def decode_object(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"unparsed_body": raw.decode(errors="replace")[:500]}
    return value if isinstance(value, dict) else {"unexpected_json": value}


def http_json(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, decode_object(response.read())
    except urllib.error.HTTPError as error:
        return error.code, decode_object(error.read())


def create_case(
    number: int,
    requester_reference: str,
    subject: str,
    message: str,
) -> dict[str, Any]:
    external_id = f"E2E-{number:02d}"
    status, body = http_json(
        f"{API_URL}/api/v1/requests",
        {
            "external_request_id": external_id,
            "requester_reference": requester_reference,
            "subject": subject,
            "message": message,
            "attachments": [],
            "received_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        },
        headers={"Authorization": f"Bearer {INTAKE_TOKEN}"},
    )
    require(status == 201, f"intake failed: HTTP {status} {body}")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT cases.case_id, cases.case_reference,
                   message.outbox_message_id, message.payload
            FROM cases
            JOIN outbox_messages AS message USING (case_id)
            WHERE cases.external_request_id = %s
              AND message.message_type = 'WORKFLOW_START'
            """,
            (external_id,),
        ).fetchone()
    require(row is not None, "intake did not create its workflow intent")
    result = dict(row)
    result["external_id"] = external_id
    return result


def snapshot(case_id: UUID) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT case_reference, request_type, current_state, version
            FROM cases WHERE case_id = %s
            """,
            (case_id,),
        ).fetchone()
    require(row is not None, "case snapshot is missing")
    return dict(row)


def start_and_wait(case: dict[str, Any]) -> dict[str, Any]:
    execution = process_one_workflow_start(
        DATABASE_URL,
        WorkflowStartClient(WORKFLOW_START_URL, N8N_TOKEN),
        retry_delay_seconds=0,
    )
    require(
        execution is not None
        and execution.outcome == "SUCCESS"
        and execution.final_status == "SENT",
        f"workflow start failed: {execution!r}",
    )
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT analysis_run_id, status, completed_at
                FROM ai_analysis_runs
                WHERE case_id = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (case["case_id"],),
            ).fetchone()
        if row is not None and row["completed_at"] is not None:
            return {**snapshot(case["case_id"]), **dict(row)}
        time.sleep(0.1)
    raise AssertionError("post-response analysis did not finish within 12 seconds")


def post_human(
    case: dict[str, Any],
    actor_id: UUID,
    action: str,
    **values: Any,
) -> dict[str, Any]:
    command_id = str(uuid4())
    body = {
        "schema_version": "1",
        "command_id": command_id,
        "expected_case_version": snapshot(case["case_id"])["version"],
        "action": action,
        **values,
    }
    cookie = create_session_cookie(actor_id, SESSION_SECRET)
    status, response = http_json(
        f"{API_URL}/api/v1/cases/{case['case_reference']}/human-decisions",
        body,
        headers={
            "Cookie": f"{SESSION_COOKIE_NAME}={cookie}",
            "X-CSRF-Token": create_csrf_token(cookie, command_id, SESSION_SECRET),
        },
    )
    require(status == 200, f"human decision failed: HTTP {status} {response}")
    return response


def resume_latest(case: dict[str, Any]) -> str:
    outbox_id = enqueue_next_human_resume(DATABASE_URL)
    require(outbox_id is not None, "human decision did not create a resume intent")
    execution = process_one_human_resume(
        DATABASE_URL,
        HumanResumeClient(HUMAN_RESUME_URL, N8N_TOKEN),
        retry_delay_seconds=0,
    )
    require(
        execution is not None
        and execution.outcome == "SUCCESS"
        and execution.final_status == "SENT",
        f"human resume failed: {execution!r}",
    )
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        event = connection.execute(
            """
            SELECT event_id FROM case_events
            WHERE case_id = %s
              AND event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
            ORDER BY event_id DESC LIMIT 1
            """,
            (case["case_id"],),
        ).fetchone()
    require(event is not None, "human resume acknowledgement is missing")
    return f"HDRESUME-{event['event_id']}"


delivery_client = ServiceDeskClient(SANDBOX_URL, SANDBOX_TOKEN)


def deliver_safe(case: dict[str, Any], expected_action: str) -> None:
    queued = queue_safe_action(DATABASE_URL, case_id=case["case_id"])
    require(
        queued.action_type == expected_action and not queued.idempotent_replay,
        "safe action did not materialize exactly once",
    )
    queued_replay = queue_safe_action(DATABASE_URL, case_id=case["case_id"])
    require(
        queued_replay.outbox_message_id == queued.outbox_message_id
        and queued_replay.idempotent_replay,
        "safe action replay created another intent",
    )
    case["downstream_outbox_id"] = queued.outbox_message_id
    execution = process_one_message(
        DATABASE_URL, delivery_client, retry_delay_seconds=0
    )
    require(
        execution is not None
        and execution.outbox_message_id == queued.outbox_message_id
        and execution.final_status == "SENT",
        f"safe delivery failed: {execution!r}",
    )
    reconciled = reconcile_safe_action(
        DATABASE_URL, outbox_message_id=queued.outbox_message_id
    )
    require(
        reconciled.current_state == "COMPLETED"
        and not reconciled.idempotent_replay,
        "safe delivery did not complete the case",
    )


def deliver_notification(case: dict[str, Any], resume_reference: str) -> None:
    intent = materialize_terminal_notification(
        DATABASE_URL, human_resume_reference=resume_reference
    )
    execution = process_one_notification(DATABASE_URL, LocalNotificationClient())
    require(
        execution is not None
        and execution.outbox_message_id == intent.outbox_message_id
        and execution.final_status == "SENT",
        "terminal notification did not deliver",
    )
    result = reconcile_terminal_notification(
        DATABASE_URL, outbox_message_id=intent.outbox_message_id
    )
    require(result.event_type == "REQUESTER_NOTIFICATION_SENT", "notification evidence is missing")
    require(snapshot(case["case_id"])["current_state"] == "REJECTED", "notification changed terminal rejection")


print("AI Service Request Automation - full end-to-end integration check")
print("Scope: 7 focused groups; 7 fictional cases; local services; fixture AI.")
print("Hosted or paid AI calls: 0")
print()

fixture_document = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
require(len(fixture_document["fixtures"]) == 8, "the fixed end-to-end fixture set changed")

incident = create_case(1, "EMP-201", "E2E incident", "WMS is unavailable. Impact high and urgency high.")
incident_analysis = start_and_wait(incident)
require(incident_analysis["current_state"] == "READY_FOR_ACTION", "incident did not route safely")
deliver_safe(incident, "INCIDENT_TICKET")

policy = create_case(2, "EMP-201", "E2E policy", "What is the remote work policy and how many remote days are allowed?")
policy_analysis = start_and_wait(policy)
require(policy_analysis["current_state"] == "ANALYZING", "policy did not await retrieval")
retrieval = retrieve_policy(
    DATABASE_URL,
    FixturePolicyProvider(
        vector=[0.001] * 1024,
        answer="Jakarta employees may work remotely for up to 2 days per week with line-manager agreement.",
        citation_ids=("POL-REMOTE-01#0",),
    ),
    case_id=policy["case_id"],
    case_reference=policy["case_reference"],
    expected_case_version=policy_analysis["version"],
    analysis_run_id=policy_analysis["analysis_run_id"],
)
require(retrieval.outcome == "READY", "policy retrieval was not grounded")
deliver_safe(policy, "POLICY_RESPONSE")

status_case = create_case(3, "EMP-201", "E2E status", "Please show the status of CASE-2026-0001.")
status_analysis = start_and_wait(status_case)
require(status_analysis["current_state"] == "READY_FOR_ACTION", "owned status request did not route")
deliver_safe(status_case, "STATUS_RESPONSE")
print("[1/7] Incident, grounded policy, and owned status complete downstream: PASS")

access = create_case(4, "EMP-201", "E2E access", "Request WMS VIEWER access for weekly inventory reporting. Approver MGR-104.")
access_analysis = start_and_wait(access)
require(access_analysis["current_state"] == "PENDING_APPROVAL", "access did not require approval")
post_human(access, APPROVER_ID, "APPROVE_REQUEST", note="Approved for controlled E2E.")
access_resume = resume_latest(access)
queued_access = queue_approved_action(
    DATABASE_URL, case_id=access["case_id"], human_resume_reference=access_resume
)
access_delivery = process_one_message(DATABASE_URL, delivery_client, retry_delay_seconds=0)
require(access_delivery is not None and access_delivery.outbox_message_id == queued_access.outbox_message_id and access_delivery.final_status == "SENT", "approved access did not deliver")
access_final = reconcile_approved_action(DATABASE_URL, outbox_message_id=queued_access.outbox_message_id)
require(access_final.current_state == "COMPLETED", "approved access did not complete")
print("[2/7] Access request waits for assigned approval then completes: PASS")

data_change = create_case(5, "AGT-301", "E2E data change", "Change WMS record SUP-448 to bank route TEST-02 for supplier correction. Approver MGR-104.")
data_analysis = start_and_wait(data_change)
require(data_analysis["current_state"] == "PENDING_APPROVAL", "data change did not require approval")
post_human(data_change, APPROVER_ID, "REJECT_REQUEST", note="Rejected controlled data change.")
data_resume = resume_latest(data_change)
deliver_notification(data_change, data_resume)
print("[3/7] Rejected data change remains rejected and notifies requester: PASS")

missing = create_case(6, "EMP-201", "E2E missing information", "WMS login is failing. Impact high.")
missing_analysis = start_and_wait(missing)
require(missing_analysis["current_state"] == "NEEDS_INFORMATION", "missing urgency did not pause")
post_human(missing, REQUESTER_ID, "SUBMIT_INFORMATION", information="Urgency medium.")
missing_resume = resume_latest(missing)
resumed = analyze_resumed_case(
    DATABASE_URL,
    FixtureAnalysisProvider.from_json_file(FIXTURE_FILE),
    case_id=missing["case_id"],
    case_reference=missing["case_reference"],
    expected_case_version=snapshot(missing["case_id"])["version"],
    human_resume_reference=missing_resume,
)
require(resumed.current_state == "READY_FOR_ACTION", "requester information did not resume safely")
deliver_safe(missing, "INCIDENT_TICKET")
print("[4/7] Missing information resumes through a distinct checked analysis: PASS")

review = create_case(7, "EMP-201", "E2E agent review", "Unknown portal is slow. Impact medium and urgency low.")
review_analysis = start_and_wait(review)
require(review_analysis["current_state"] == "NEEDS_REVIEW", "ambiguous service did not pause for review")
fields = {
    "policy_topic": None, "question": None, "affected_service": "WMS",
    "incident_description": "WMS is slow", "impact": "MEDIUM", "urgency": "LOW",
    "target_system": None, "requested_access_level": None, "business_reason": None,
    "approver_id": None, "record_reference": None, "requested_changes": None,
    "case_reference": None,
}
post_human(
    review,
    AGENT_ID,
    "CORRECT_REVIEW",
    note="Corrected the fictional affected service.",
    request_type="incident_report",
    summary="WMS performance incident after service-agent correction.",
    fields=fields,
)
review_resume = resume_latest(review)
routed = route_reviewed_case(DATABASE_URL, human_resume_reference=review_resume)
require(routed.next_route == "DOWNSTREAM_ACTION" and routed.current_state == "READY_FOR_ACTION", "agent correction did not route deterministically")
deliver_safe(review, "INCIDENT_TICKET")
print("[5/7] Ambiguous case uses agent correction without another AI call: PASS")

replay_status, replay_body = http_json(
    f"{API_URL}/api/v1/requests",
    {
        "external_request_id": incident["external_id"],
        "requester_reference": "EMP-201",
        "subject": "E2E incident",
        "message": "WMS is unavailable. Impact high and urgency high.",
        "attachments": [],
        "received_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    },
    headers={"Authorization": f"Bearer {INTAKE_TOKEN}"},
)
require(replay_status == 200 and replay_body.get("idempotent_replay") is True, "exact intake replay was not stable")
replayed_reconcile = reconcile_safe_action(
    DATABASE_URL, outbox_message_id=incident["downstream_outbox_id"]
)
require(replayed_reconcile.idempotent_replay, "terminal replay duplicated work")
print("[6/7] Exact intake, action, and reconciliation replays are no-ops: PASS")

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = connection.execute(
        """
        SELECT
          (SELECT count(*) FROM cases WHERE external_request_id LIKE 'E2E-%') AS cases,
          (SELECT count(*) FROM cases WHERE external_request_id LIKE 'E2E-%' AND current_state='COMPLETED') AS completed,
          (SELECT count(*) FROM cases WHERE external_request_id LIKE 'E2E-%' AND current_state='REJECTED') AS rejected,
          (SELECT count(*) FROM ai_analysis_runs a JOIN cases c USING(case_id) WHERE c.external_request_id LIKE 'E2E-%') AS analyses,
          (SELECT count(*) FROM outbox_messages m JOIN cases c USING(case_id) WHERE c.external_request_id LIKE 'E2E-%' AND m.status IN ('PENDING','PROCESSING')) AS unfinished,
          (SELECT count(*) FROM case_events e JOIN cases c USING(case_id) WHERE c.external_request_id LIKE 'E2E-%' AND e.event_type='DOWNSTREAM_ACTION_COMPLETED') AS completed_events,
          (SELECT count(*) FROM case_events e JOIN cases c USING(case_id) WHERE c.external_request_id LIKE 'E2E-%' AND e.event_type='REQUESTER_NOTIFICATION_SENT') AS notifications
        """
    ).fetchone()
with psycopg.connect(SANDBOX_DATABASE_URL, row_factory=dict_row) as connection:
    sandbox_records = connection.execute("SELECT count(*) AS value FROM service_records").fetchone()["value"]
require(
    dict(aggregate) == {
        "cases": 7, "completed": 6, "rejected": 1, "analyses": 8,
        "unfinished": 0, "completed_events": 6, "notifications": 1,
    }
    and sandbox_records == 6,
    f"unexpected end-to-end aggregate: {dict(aggregate)}, sandbox={sandbox_records}",
)
print("[7/7] Aggregate lifecycle evidence and 0 unfinished work: PASS")

print("Full end-to-end integration summary")
print("  Integration groups: 7/7 PASS")
print("  Fictional lifecycle cases: 7")
print("  Completed cases: 6")
print("  Rejected and notified cases: 1")
print("  Durable analysis attempts: 8")
print("  Service Desk records: 6")
print("  Duplicate terminal effects: 0")
print("  Unfinished workflow work: 0")
print("  Hosted or paid AI calls: 0")
print("  Full end-to-end gate: PASS")

if RESULT_FILE:
    case_results = [
        {
            "case_reference": incident["case_reference"],
            "subject": "WMS unavailable",
            "request_type": "INCIDENT_REPORT",
            "human_gate": "None",
            "route": "Incident ticket",
            "final_state": snapshot(incident["case_id"])["current_state"],
        },
        {
            "case_reference": policy["case_reference"],
            "subject": "Remote-work policy question",
            "request_type": "POLICY_QUESTION",
            "human_gate": "Grounded citation",
            "route": "Policy response",
            "final_state": snapshot(policy["case_id"])["current_state"],
        },
        {
            "case_reference": status_case["case_reference"],
            "subject": "Owned case status",
            "request_type": "STATUS_REQUEST",
            "human_gate": "Ownership check",
            "route": "Status response",
            "final_state": snapshot(status_case["case_id"])["current_state"],
        },
        {
            "case_reference": access["case_reference"],
            "subject": "WMS viewer access",
            "request_type": "ACCESS_REQUEST",
            "human_gate": "Assigned approval",
            "route": "Approved access action",
            "final_state": snapshot(access["case_id"])["current_state"],
        },
        {
            "case_reference": data_change["case_reference"],
            "subject": "Supplier data change",
            "request_type": "DATA_CHANGE_REQUEST",
            "human_gate": "Assigned rejection",
            "route": "Requester notification",
            "final_state": snapshot(data_change["case_id"])["current_state"],
        },
        {
            "case_reference": missing["case_reference"],
            "subject": "Missing incident urgency",
            "request_type": "INCIDENT_REPORT",
            "human_gate": "Requester information",
            "route": "Incident ticket",
            "final_state": snapshot(missing["case_id"])["current_state"],
        },
        {
            "case_reference": review["case_reference"],
            "subject": "Ambiguous affected service",
            "request_type": "INCIDENT_REPORT",
            "human_gate": "Service-agent correction",
            "route": "Incident ticket",
            "final_state": snapshot(review["case_id"])["current_state"],
        },
    ]
    result = {
        "schema_version": "showcase-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "ai_provider": "Controlled fixture",
            "hosted_or_paid_ai_calls": 0,
            "data_scope": "Fictional local data",
        },
        "summary": {
            "integration_groups_passed": 7,
            "integration_groups_total": 7,
            "fictional_cases": int(aggregate["cases"]),
            "completed_cases": int(aggregate["completed"]),
            "rejected_cases": int(aggregate["rejected"]),
            "analysis_attempts": int(aggregate["analyses"]),
            "service_desk_records": int(sandbox_records),
            "unfinished_work": int(aggregate["unfinished"]),
            "duplicate_terminal_effects": 0,
        },
        "cases": case_results,
        "verified_controls": [
            "Authenticated intake and orchestration handoff",
            "Deterministic validation before business acceptance",
            "Requester information, service-agent correction, and assigned approval",
            "Grounded policy citation and requester ownership isolation",
            "Idempotent intake, action materialization, and reconciliation",
            "Downstream Service Desk delivery, notification, and 0 unfinished work",
        ],
    }
    result_path = Path(RESULT_FILE)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print("  Showcase result export: PASS")
