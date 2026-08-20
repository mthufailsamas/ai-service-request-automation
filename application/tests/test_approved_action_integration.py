"""Controlled approved-human-route to Service Desk integration check."""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from delivery import ServiceDeskClient, process_one_message
from downstream_route import (
    ApprovedActionConflict,
    ApprovedActionNotFound,
    queue_approved_action,
    queue_next_approved_action,
    reconcile_approved_action,
    reconcile_next_terminal_action,
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

PRIMARY_DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
SANDBOX_DATABASE_URL = os.environ["SERVICE_DESK_SANDBOX_DATABASE_URL"]
SANDBOX_URL = os.environ["SERVICE_DESK_SANDBOX_URL"]
SANDBOX_TOKEN = os.environ["SERVICE_DESK_SANDBOX_TOKEN"]

REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("10000000-0000-4000-8000-000000000002")
APPROVER_ID = UUID("10000000-0000-4000-8000-000000000003")
SYSTEM_ID = UUID("20000000-0000-4000-8000-000000000001")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_approved_fixture(
    request_type: str,
    sequence: int,
) -> dict[str, Any]:
    case_id = uuid4()
    approval_id = uuid4()
    command_id = uuid4()
    case_reference = f"CASE-2026-{sequence}"
    is_access = request_type == "ACCESS_REQUEST"
    title = (
        "Approved warehouse access"
        if is_access
        else "Approved warehouse record correction"
    )
    summary = (
        "Grant the requester approved viewer access to WMS."
        if is_access
        else "Apply the approved fictional warehouse record correction."
    )

    with psycopg.connect(PRIMARY_DATABASE_URL, row_factory=dict_row) as connection:
        connection.execute(
            """
            INSERT INTO cases (
                case_id, case_reference, source_channel, external_request_id,
                idempotency_key, content_fingerprint, requester_id, subject,
                original_message, attachment_metadata, request_type,
                ai_summary, current_state, version, received_at
            )
            VALUES (
                %s, %s, 'WEBHOOK', %s, %s, %s, %s, %s,
                'Fictional approved downstream action.', '[]', %s,
                %s, 'READY_FOR_ACTION', 2, %s
            )
            """,
            (
                case_id,
                case_reference,
                f"APPROVED-ACTION-{sequence}",
                sha(f"approved-idempotency-{sequence}"),
                sha(f"approved-content-{sequence}"),
                REQUESTER_ID if is_access else AGENT_ID,
                title,
                request_type,
                summary,
                datetime.now(timezone.utc) - timedelta(seconds=2),
            ),
        )
        connection.execute(
            """
            INSERT INTO case_details (
                case_id, target_system_id, requested_access_level,
                business_reason, approver_user_id, record_reference,
                requested_changes, accepted_by_type, accepted_by_user_id,
                accepted_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    'SERVICE_AGENT', %s, now())
            """,
            (
                case_id,
                SYSTEM_ID,
                "Viewer" if is_access else None,
                "Controlled fictional business need.",
                APPROVER_ID,
                None if is_access else f"WMS-REC-{sequence}",
                None if is_access else "Correct the fictional location code.",
                AGENT_ID,
            ),
        )
        connection.execute(
            """
            INSERT INTO approvals (
                approval_id, case_id, approver_user_id, request_type,
                decision, decision_note, requested_at, decided_at
            )
            VALUES (%s, %s, %s, %s, 'APPROVED',
                    'Approved controlled fixture.', %s, %s)
            """,
            (
                approval_id,
                case_id,
                APPROVER_ID,
                request_type,
                datetime.now(timezone.utc) - timedelta(seconds=2),
                datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        )
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            )
            VALUES (
                %s, 1, NULL, 'PENDING_APPROVAL', 'FIXTURE_APPROVAL_PENDING',
                'SYSTEM', 'Controlled approved-action fixture.', '{}'
            )
            """,
            (case_id,),
        )
        decision = connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, actor_user_id, reason, event_payload
            )
            VALUES (
                %s, 2, 'PENDING_APPROVAL', 'READY_FOR_ACTION',
                'APPROVAL_APPROVED', 'USER', %s,
                'The assigned fictional approver authorized the action.', %s
            )
            RETURNING event_id
            """,
            (
                case_id,
                APPROVER_ID,
                Jsonb(
                    {
                        "action": "APPROVE_REQUEST",
                        "approval_id": str(approval_id),
                        "decision": "APPROVED",
                        "human_command_id": str(command_id),
                        "input_sha256": sha(str(command_id)),
                        "result_case_version": 2,
                        "schema_version": "1",
                    }
                ),
            ),
        ).fetchone()
        acknowledgement = connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            )
            VALUES (
                %s, 3, 'READY_FOR_ACTION', 'READY_FOR_ACTION',
                'HUMAN_DECISION_RESUME_ACKNOWLEDGED', 'INTEGRATION',
                'Local n8n accepted the approved downstream route.', %s
            )
            RETURNING event_id
            """,
            (
                case_id,
                Jsonb(
                    {
                        "action": "APPROVE_REQUEST",
                        "case_version": 2,
                        "human_decision_reference": f"HD-{decision['event_id']}",
                        "outbox_idempotency_key": sha(
                            f"resume-{case_id}-{decision['event_id']}"
                        ),
                        "resume_route": "DOWNSTREAM_ACTION",
                        "schema_version": "1",
                    }
                ),
            ),
        ).fetchone()
    return {
        "case_id": case_id,
        "case_reference": case_reference,
        "request_type": request_type,
        "human_resume_reference": f"HDRESUME-{acknowledgement['event_id']}",
        "title": title,
        "summary": summary,
        "approval_id": approval_id,
    }


def outbox_evidence(case_id: UUID) -> dict[str, Any]:
    with psycopg.connect(PRIMARY_DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT outbox_message_id, idempotency_key, payload, status,
                   attempt_count, max_attempts
            FROM outbox_messages
            WHERE case_id = %s
              AND message_type = 'DOWNSTREAM_ACTION'
            """,
            (case_id,),
        ).fetchone()
    require(row is not None, "The downstream action intent is missing")
    return dict(row)


def case_evidence(case_id: UUID) -> dict[str, Any]:
    with psycopg.connect(PRIMARY_DATABASE_URL, row_factory=dict_row) as connection:
        case = connection.execute(
            "SELECT current_state, version FROM cases WHERE case_id = %s",
            (case_id,),
        ).fetchone()
        events = connection.execute(
            """
            SELECT event_id, event_type, from_state, to_state, event_payload
            FROM case_events
            WHERE case_id = %s
              AND event_type IN (
                  'DOWNSTREAM_ACTION_COMPLETED',
                  'DOWNSTREAM_ACTION_FAILED'
              )
            ORDER BY event_id
            """,
            (case_id,),
        ).fetchall()
    require(case is not None, "The approved fixture case is missing")
    return {**dict(case), "events": [dict(event) for event in events]}


client = ServiceDeskClient(SANDBOX_URL, SANDBOX_TOKEN)
access = create_approved_fixture("ACCESS_REQUEST", 10001)
data_change = create_approved_fixture("DATA_CHANGE_REQUEST", 10002)

try:
    queue_approved_action(
        PRIMARY_DATABASE_URL,
        case_id=access["case_id"],
        human_resume_reference=data_change["human_resume_reference"],
    )
except ApprovedActionNotFound:
    pass
else:
    raise AssertionError("A cross-case acknowledgement was accepted")
try:
    queue_approved_action(
        PRIMARY_DATABASE_URL,
        case_id=access["case_id"],
        human_resume_reference="invalid-reference",
    )
except ApprovedActionConflict:
    pass
else:
    raise AssertionError("An invalid acknowledgement reference was accepted")
with psycopg.connect(PRIMARY_DATABASE_URL) as connection:
    connection.execute(
        """
        UPDATE system_permissions
        SET is_active = false
        WHERE user_id = %s
          AND system_id = %s
          AND permission_code = 'REQUEST_ACCESS'
        """,
        (REQUESTER_ID, SYSTEM_ID),
    )
try:
    queue_approved_action(
        PRIMARY_DATABASE_URL,
        case_id=access["case_id"],
        human_resume_reference=access["human_resume_reference"],
    )
except ApprovedActionConflict:
    pass
else:
    raise AssertionError("A revoked requester permission was accepted")
with psycopg.connect(PRIMARY_DATABASE_URL) as connection:
    connection.execute(
        """
        UPDATE system_permissions
        SET is_active = true
        WHERE user_id = %s
          AND system_id = %s
          AND permission_code = 'REQUEST_ACCESS'
        """,
        (REQUESTER_ID, SYSTEM_ID),
    )
with psycopg.connect(PRIMARY_DATABASE_URL, row_factory=dict_row) as connection:
    require(
        connection.execute(
            "SELECT COUNT(*) AS message_count FROM outbox_messages"
        ).fetchone()["message_count"]
        == 0,
        "A rejected authority check created an outbox intent",
    )
print("[1/6] Trusted acknowledgement, approval, case, and state guards: PASS")


def queue_access() -> Any:
    return queue_approved_action(
        PRIMARY_DATABASE_URL,
        case_id=access["case_id"],
        human_resume_reference=access["human_resume_reference"],
    )


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    concurrent_results = list(executor.map(lambda _item: queue_access(), range(2)))
require(
    len({result.outbox_message_id for result in concurrent_results}) == 1
    and sorted(result.idempotent_replay for result in concurrent_results)
    == [False, True],
    "Concurrent materialization did not produce one exact intent",
)
data_queued = queue_approved_action(
    PRIMARY_DATABASE_URL,
    case_id=data_change["case_id"],
    human_resume_reference=data_change["human_resume_reference"],
)
require(data_queued.idempotent_replay is False, "Fresh data action was a replay")

access_outbox = outbox_evidence(access["case_id"])
data_outbox = outbox_evidence(data_change["case_id"])
require(
    access_outbox["payload"]
    == {
        "case_reference": access["case_reference"],
        "case_version": 2,
        "action_type": "ACCESS_ACTION",
        "title": access["title"],
        "summary": access["summary"],
        "details": {
            "target_system": "WMS",
            "access_level": "Viewer",
            "approver_reference": "MGR-104",
            "approval_reference": f"APPROVAL-{access['approval_id']}",
        },
    },
    "The approved access payload is not exact",
)
require(
    data_outbox["payload"]["action_type"] == "DATA_CHANGE_ACTION"
    and set(data_outbox["payload"]["details"])
    == {
        "target_system",
        "record_reference",
        "requested_changes",
        "approver_reference",
        "approval_reference",
    },
    "The approved data-change payload is not exact",
)
print("[2/6] Concurrent materialization and both exact payloads: PASS")

with psycopg.connect(SANDBOX_DATABASE_URL) as connection:
    connection.execute(
        "SELECT setval('service_record_reference_sequence', 9999, true)"
    )

successful_executions = []
for fixture in (access, data_change):
    execution = process_one_message(
        PRIMARY_DATABASE_URL,
        client,
        retry_delay_seconds=0,
    )
    require(
        execution is not None
        and execution.outcome == "SUCCESS"
        and execution.final_status == "SENT"
        and execution.http_status == 201,
        f"A new approved action did not deliver: {execution!r}",
    )
    successful_executions.append(execution)
    reconciliation = reconcile_approved_action(
        PRIMARY_DATABASE_URL,
        outbox_message_id=execution.outbox_message_id,
    )
    require(
        reconciliation.current_state == "COMPLETED"
        and reconciliation.case_version == 3
        and reconciliation.idempotent_replay is False,
        "Successful delivery did not complete the case exactly once",
    )
    evidence = case_evidence(fixture["case_id"])
    require(
        evidence["current_state"] == "COMPLETED"
        and evidence["version"] == 3
        and len(evidence["events"]) == 1,
        "The completed case evidence is inconsistent",
    )
require(
    all(
        re.fullmatch(r"SR-[0-9]{4}-[0-9]{5,}", result.downstream_reference or "")
        for result in successful_executions
    ),
    "The additive Service Desk reference scale was not exercised",
)
print("[3/6] Both actions deliver and transition to COMPLETED: PASS")

replay_fixture = create_approved_fixture("ACCESS_REQUEST", 10003)
replay_queued = queue_approved_action(
    PRIMARY_DATABASE_URL,
    case_id=replay_fixture["case_id"],
    human_resume_reference=replay_fixture["human_resume_reference"],
)
replay_outbox = outbox_evidence(replay_fixture["case_id"])
precreated = client.create_service_record(
    replay_outbox["payload"], replay_outbox["idempotency_key"]
)
require(
    precreated.outcome == "SUCCESS" and precreated.http_status == 201,
    "The downstream replay precondition was not created",
)
replay_execution = process_one_message(
    PRIMARY_DATABASE_URL,
    client,
    retry_delay_seconds=0,
)
require(
    replay_execution is not None
    and replay_execution.outbox_message_id == replay_queued.outbox_message_id
    and replay_execution.outcome == "SUCCESS"
    and replay_execution.http_status == 200
    and replay_execution.downstream_reference == precreated.downstream_reference,
    "The exact downstream replay did not recover the same record",
)
first_reconciliation = reconcile_approved_action(
    PRIMARY_DATABASE_URL,
    outbox_message_id=replay_execution.outbox_message_id,
)
second_reconciliation = reconcile_approved_action(
    PRIMARY_DATABASE_URL,
    outbox_message_id=replay_execution.outbox_message_id,
)
require(
    first_reconciliation.event_reference == second_reconciliation.event_reference
    and second_reconciliation.idempotent_replay is True
    and len(case_evidence(replay_fixture["case_id"])["events"]) == 1,
    "Terminal reconciliation replay duplicated the case transition",
)
print("[4/6] Downstream replay reuses 1 record and 1 case transition: PASS")

failure_fixture = create_approved_fixture("DATA_CHANGE_REQUEST", 10004)
failure_queued = queue_approved_action(
    PRIMARY_DATABASE_URL,
    case_id=failure_fixture["case_id"],
    human_resume_reference=failure_fixture["human_resume_reference"],
)
failure_execution = process_one_message(
    PRIMARY_DATABASE_URL,
    client,
    test_outcome="PERMANENT_FAILURE",
    retry_delay_seconds=0,
)
require(
    failure_execution is not None
    and failure_execution.outbox_message_id == failure_queued.outbox_message_id
    and failure_execution.outcome == "PERMANENT_FAILURE"
    and failure_execution.final_status == "FAILED",
    "The permanent delivery failure was not terminal",
)
failure_reconciliation = reconcile_approved_action(
    PRIMARY_DATABASE_URL,
    outbox_message_id=failure_execution.outbox_message_id,
)
failure_evidence = case_evidence(failure_fixture["case_id"])
require(
    failure_reconciliation.current_state == "FAILED"
    and failure_evidence["current_state"] == "FAILED"
    and failure_evidence["version"] == 3
    and failure_evidence["events"][0]["event_type"]
    == "DOWNSTREAM_ACTION_FAILED",
    "Terminal delivery failure did not create the exact FAILED transition",
)
print("[5/6] Permanent delivery failure transitions exactly to FAILED: PASS")

with psycopg.connect(PRIMARY_DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = dict(
        connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM cases
               WHERE external_request_id LIKE 'APPROVED-ACTION-%') AS cases,
              (SELECT COUNT(*) FROM outbox_messages
               WHERE message_type = 'DOWNSTREAM_ACTION') AS action_intents,
              (SELECT COUNT(*) FROM delivery_attempts) AS delivery_attempts,
              (SELECT COUNT(*) FROM case_events
               WHERE event_type = 'DOWNSTREAM_ACTION_COMPLETED') AS completed,
              (SELECT COUNT(*) FROM case_events
               WHERE event_type = 'DOWNSTREAM_ACTION_FAILED') AS failed,
              (SELECT COUNT(*) FROM outbox_messages
               WHERE status IN ('PENDING', 'PROCESSING')) AS unfinished,
              (SELECT COUNT(*) FROM outbox_messages
               WHERE message_type = 'REQUESTER_NOTIFICATION') AS notifications,
              (SELECT COUNT(*) FROM ai_analysis_runs) AS ai_runs
            """
        ).fetchone()
    )
    duplicate_terminal_events = connection.execute(
        """
        SELECT COUNT(*) AS duplicate_count FROM (
            SELECT event_payload->>'outbox_message_id'
            FROM case_events
            WHERE event_type IN (
                'DOWNSTREAM_ACTION_COMPLETED',
                'DOWNSTREAM_ACTION_FAILED'
            )
            GROUP BY event_payload->>'outbox_message_id'
            HAVING COUNT(*) > 1
        ) AS duplicates
        """
    ).fetchone()["duplicate_count"]
with psycopg.connect(SANDBOX_DATABASE_URL, row_factory=dict_row) as connection:
    sandbox_aggregate = dict(
        connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM service_records) AS records,
              (SELECT COUNT(*) FROM service_record_events) AS events,
              (SELECT COUNT(*) FROM service_records
               WHERE service_record_reference ~ '^SR-[0-9]{4}-[0-9]{5,}$')
                   AS scaled_references
            """
        ).fetchone()
    )
require(
    aggregate
    == {
        "cases": 4,
        "action_intents": 4,
        "delivery_attempts": 4,
        "completed": 3,
        "failed": 1,
        "unfinished": 0,
        "notifications": 0,
        "ai_runs": 0,
    },
    f"Unexpected approved-action aggregate: {aggregate}",
)
require(
    sandbox_aggregate == {"records": 3, "events": 5, "scaled_references": 3},
    f"Unexpected Service Desk aggregate: {sandbox_aggregate}",
)
require(
    duplicate_terminal_events == 0
    and queue_next_approved_action(PRIMARY_DATABASE_URL) is None
    and reconcile_next_terminal_action(PRIMARY_DATABASE_URL) is None,
    "Duplicate or unfinished approved-action work remains",
)
print("[6/6] Aggregate evidence, reference scale, and isolation: PASS")

print("")
print("Approved downstream-action integration summary")
print("  Integration groups: 6/6 PASS")
print("  Fictional approved cases: 4")
print("  Durable downstream intents: 4")
print("  Append-only delivery attempts: 4")
print("  Completed cases: 3")
print("  Terminal failed cases: 1")
print("  Duplicate terminal transitions: 0")
print("  Unfinished downstream actions: 0")
print("  External AI calls: 0")
print("  Approved downstream-action gate: PASS")
