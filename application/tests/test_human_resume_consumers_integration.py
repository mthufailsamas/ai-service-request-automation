"""Focused integration check for the 2 remaining human-resume consumers."""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_analysis import (
    AnalysisInProgress,
    FixtureAnalysisProvider,
    FixtureResponse,
    analyze_resumed_case,
    canonical_input_sha256,
)
from human_decision import HumanDecisionCommand, execute_human_decision
from human_resume import ACTION_EVENT_ROUTE
from human_resume_consumers import (
    LocalNotificationClient,
    ResumeConsumerConflict,
    materialize_terminal_notification,
    process_one_notification,
    reconcile_terminal_notification,
    route_reviewed_case,
)


def concise_exception_hook(
    kind: type[BaseException], error: BaseException, tb: Any
) -> None:
    frames = traceback.extract_tb(tb)
    location = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "unknown"
    print(f"FAIL: {kind.__name__}: {error} [{location}]", file=sys.stderr)


sys.excepthook = concise_exception_hook

DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("10000000-0000-4000-8000-000000000002")
APPROVER_ID = UUID("10000000-0000-4000-8000-000000000003")
WMS_ID = UUID("20000000-0000-4000-8000-000000000001")


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


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
    subject: str,
    message: str,
    requester_id: UUID = REQUESTER_ID,
    request_type: str | None = None,
) -> dict[str, Any]:
    case_id = uuid4()
    case_reference = f"CASE-2026-{9000 + number:04d}"
    external_id = f"HUMAN-CONSUMER-{number:02d}"
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        connection.execute(
            """
            INSERT INTO cases (
                case_id, case_reference, source_channel, external_request_id,
                idempotency_key, content_fingerprint, requester_id, subject,
                original_message, attachment_metadata, request_type,
                current_state, version, received_at
            )
            VALUES (
                %s, %s, 'WEBHOOK', %s, %s, %s, %s, %s, %s,
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
                subject,
                message,
                request_type,
                state,
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
                %s, 1, NULL, %s, 'HUMAN_CONSUMER_FIXTURE_CREATED',
                'INTEGRATION', 'Fictional focused-check state.', '{}'
            )
            """,
            (case_id, state),
        )
    return {
        "case_id": case_id,
        "case_reference": case_reference,
        "subject": subject,
        "message": message,
    }


def add_confirmable_access_proposal(case_id: UUID) -> None:
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
                case_id, model_name, model_identifier, prompt_contract_version,
                input_sha256, proposal, evidence, status, wall_time_ms,
                input_tokens, output_tokens, attempt_number, completed_at
            )
            VALUES (
                %s, 'fixture-provider', 'review-proposal-fixture-v1',
                'analysis-v1', %s, %s, '[]', 'COMPLETED', 1, 0, 0, 1, now()
            )
            """,
            (
                case_id,
                hashlib.sha256(str(case_id).encode("utf-8")).hexdigest(),
                Jsonb(proposal),
            ),
        )


def add_pending_data_approval(case_id: UUID) -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            """
            INSERT INTO case_details (
                case_id, target_system_id, business_reason, approver_user_id,
                record_reference, requested_changes, accepted_by_type,
                accepted_at
            )
            VALUES (
                %s, %s, 'Controlled data correction.', %s,
                'REC-9001', 'Update the fictional owner.', 'SYSTEM_RULE', now()
            )
            """,
            (case_id, WMS_ID, APPROVER_ID),
        )
        connection.execute(
            """
            INSERT INTO approvals (
                case_id, approver_user_id, request_type, decision, requested_at
            )
            VALUES (%s, %s, 'DATA_CHANGE_REQUEST', 'PENDING', now())
            """,
            (case_id, APPROVER_ID),
        )


def decide(
    case: dict[str, Any], actor_id: UUID, action: str, **values: Any
) -> Any:
    return execute_human_decision(
        DATABASE_URL,
        case_reference=case["case_reference"],
        actor_user_id=actor_id,
        command=HumanDecisionCommand(
            schema_version="1",
            command_id=uuid4(),
            expected_case_version=1,
            action=action,
            **values,
        ),
    )


def acknowledge(case_id: UUID, decision: Any) -> str:
    trigger_event, resume_route = ACTION_EVENT_ROUTE[decision.action]
    event_id = int(decision.human_decision_reference.removeprefix("HD-"))
    outbox_key = hashlib.sha256(
        f"human-resume-v1:{case_id}:{event_id}".encode("utf-8")
    ).hexdigest()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        inserted = connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            )
            VALUES (
                %s,
                (SELECT COALESCE(MAX(sequence_number), 0) + 1
                 FROM case_events WHERE case_id = %s),
                %s, %s, 'HUMAN_DECISION_RESUME_ACKNOWLEDGED', 'INTEGRATION',
                'The verified local handoff acknowledged this route.', %s
            )
            RETURNING event_id
            """,
            (
                case_id,
                case_id,
                decision.current_state,
                decision.current_state,
                Jsonb(
                    {
                        "action": decision.action,
                        "case_version": decision.case_version,
                        "human_decision_reference": decision.human_decision_reference,
                        "outbox_idempotency_key": outbox_key,
                        "resume_route": resume_route,
                        "schema_version": "1",
                    }
                ),
            ),
        ).fetchone()
    return f"HDRESUME-{inserted['event_id']}"


print("AI Service Request Automation - human-resume consumers integration check")
print("Scope: 6 focused groups; fictional data; fixture provider; 0 Ollama calls.")
print("")

requester_case = make_case(
    1,
    "NEEDS_INFORMATION",
    subject="Complete WMS access request",
    message="I need WMS access for inventory work.",
)
requester_information = (
    "Requested access level is STANDARD. Support inventory reconciliation. "
    "Approver MGR-104."
)
requester_decision = decide(
    requester_case,
    REQUESTER_ID,
    "SUBMIT_INFORMATION",
    information=requester_information,
)
requester_resume = acknowledge(requester_case["case_id"], requester_decision)

confirm_case = make_case(
    2,
    "NEEDS_REVIEW",
    subject="Review WMS access request",
    message="Controlled ambiguous access proposal.",
)
add_confirmable_access_proposal(confirm_case["case_id"])
confirm_decision = decide(
    confirm_case,
    AGENT_ID,
    "CONFIRM_REVIEW",
    note="Confirmed against the fictional request.",
)
confirm_resume = acknowledge(confirm_case["case_id"], confirm_decision)

correction_case = make_case(
    3,
    "NEEDS_REVIEW",
    subject="Correct incident review",
    message="Controlled ambiguous incident proposal.",
)
incident_fields = empty_fields()
incident_fields.update(
    {
        "affected_service": "WMS",
        "incident_description": "Inventory synchronization is delayed.",
        "impact": "HIGH",
        "urgency": "MEDIUM",
    }
)
correction_decision = decide(
    correction_case,
    AGENT_ID,
    "CORRECT_REVIEW",
    request_type="incident_report",
    summary="Corrected inventory synchronization incident.",
    fields=incident_fields,
)
correction_resume = acknowledge(correction_case["case_id"], correction_decision)

agent_reject_case = make_case(
    4,
    "NEEDS_REVIEW",
    subject="Rejected review request",
    message="Controlled rejected review.",
)
agent_reject_decision = decide(
    agent_reject_case,
    AGENT_ID,
    "REJECT_REVIEW",
    note="The request is invalid after review.",
)
agent_reject_resume = acknowledge(
    agent_reject_case["case_id"], agent_reject_decision
)

approval_reject_case = make_case(
    5,
    "PENDING_APPROVAL",
    subject="Rejected data change",
    message="Controlled rejected data-change approval.",
    requester_id=AGENT_ID,
    request_type="DATA_CHANGE_REQUEST",
)
add_pending_data_approval(approval_reject_case["case_id"])
approval_reject_decision = decide(
    approval_reject_case,
    APPROVER_ID,
    "REJECT_REQUEST",
    note="The fictional data change is not authorized.",
)
approval_reject_resume = acknowledge(
    approval_reject_case["case_id"], approval_reject_decision
)

# Group 1: the two consumers accept only their exact durable route authority.
for function, reference in (
    (route_reviewed_case, agent_reject_resume),
    (materialize_terminal_notification, confirm_resume),
):
    try:
        function(DATABASE_URL, human_resume_reference=reference)
    except ResumeConsumerConflict:
        pass
    else:
        raise AssertionError("an incompatible human-resume route was consumed")
with psycopg.connect(DATABASE_URL) as connection:
    require(
        connection.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == 0,
        "a rejected consumer guard created an outbox intent",
    )
print("[1/6] Exact acknowledgement, decision, route, state, and version guards: PASS")

# Groups 2-3: requester information becomes one bounded, replayable fixture analysis.
combined_message = (
    f"{requester_case['message']}\n\n"
    f"Requester additional information:\n{requester_information}"
)
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
    "summary": "Grant standard WMS access for inventory reconciliation.",
    "fields": fields,
    "evidence": [
        {"field": "target_system", "quote": "WMS"},
        {"field": "requested_access_level", "quote": "STANDARD"},
        {"field": "business_reason", "quote": "Support inventory reconciliation."},
        {"field": "approver_id", "quote": "MGR-104"},
    ],
}
input_hash = canonical_input_sha256(requester_case["subject"], combined_message)
provider = FixtureAnalysisProvider(
    {
        input_hash: [
            FixtureResponse(
                kind="result",
                proposal=proposal,
                wall_time_ms=100,
                delay_ms=100,
            )
        ]
    },
    model_identifier="human-resume-fixture-v1",
)


def run_resumed_analysis() -> Any:
    for _attempt in range(10):
        try:
            return analyze_resumed_case(
                DATABASE_URL,
                provider,
                case_id=requester_case["case_id"],
                case_reference=requester_case["case_reference"],
                expected_case_version=requester_decision.case_version,
                human_resume_reference=requester_resume,
            )
        except AnalysisInProgress:
            time.sleep(0.05)
    raise AssertionError("the bounded concurrent analysis did not become replayable")


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    analysis_results = list(executor.map(lambda _item: run_resumed_analysis(), range(2)))
require(
    {result.outcome for result in analysis_results} == {"READY", "REPLAY"}
    and provider.call_count(requester_case["subject"], combined_message) == 1,
    f"resumed analysis was not singular: {analysis_results}",
)
with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    requester_evidence = connection.execute(
        """
        SELECT cases.current_state, cases.version,
               (SELECT count(*) FROM ai_analysis_runs
                WHERE case_id = cases.case_id
                  AND model_identifier = 'human-resume-fixture-v1') AS runs,
               (SELECT count(*) FROM validation_runs
                WHERE case_id = cases.case_id) AS validations,
               (SELECT count(*) FROM approvals
                WHERE case_id = cases.case_id) AS approvals,
               (SELECT count(*) FROM case_events
                WHERE case_id = cases.case_id
                  AND event_payload->>'analysis_trigger_reference' = %s) AS linked_events
        FROM cases WHERE cases.case_id = %s
        """,
        (requester_resume, requester_case["case_id"]),
    ).fetchone()
require(
    dict(requester_evidence)
    == {
        "current_state": "PENDING_APPROVAL",
        "version": 3,
        "runs": 1,
        "validations": 1,
        "approvals": 1,
        "linked_events": 1,
    },
    f"requester reanalysis evidence is inconsistent: {dict(requester_evidence)}",
)
print("[2/6] Requester information becomes 1 bounded fixture re-analysis: PASS")
print("[3/6] Concurrent invocation and exact replay use 1 provider call: PASS")

# Group 4: accepted agent details route deterministically without another model call.
confirmed = route_reviewed_case(
    DATABASE_URL, human_resume_reference=confirm_resume
)
confirmed_replay = route_reviewed_case(
    DATABASE_URL, human_resume_reference=confirm_resume
)
corrected = route_reviewed_case(
    DATABASE_URL, human_resume_reference=correction_resume
)
corrected_replay = route_reviewed_case(
    DATABASE_URL, human_resume_reference=correction_resume
)
require(
    confirmed.next_route == "APPROVAL"
    and confirmed.current_state == "PENDING_APPROVAL"
    and confirmed_replay.idempotent_replay
    and corrected.next_route == "DOWNSTREAM_ACTION"
    and corrected.current_state == "READY_FOR_ACTION"
    and corrected_replay.idempotent_replay,
    "service-agent reanalysis did not produce exact deterministic routes",
)
print("[4/6] Agent confirmation and correction route without a model call: PASS")

# Group 5: both terminal rejection sources create, deliver, and reconcile once.
agent_intent = materialize_terminal_notification(
    DATABASE_URL, human_resume_reference=agent_reject_resume
)
approval_intent = materialize_terminal_notification(
    DATABASE_URL, human_resume_reference=approval_reject_resume
)
agent_replay = materialize_terminal_notification(
    DATABASE_URL, human_resume_reference=agent_reject_resume
)
require(
    agent_replay.idempotent_replay
    and agent_replay.outbox_message_id == agent_intent.outbox_message_id,
    "terminal notification materialization replay was not exact",
)
client = LocalNotificationClient()
deliveries = [
    process_one_notification(DATABASE_URL, client, retry_delay_seconds=0),
    process_one_notification(DATABASE_URL, client, retry_delay_seconds=0),
]
require(
    all(
        result is not None
        and result.outcome == "SUCCESS"
        and result.final_status == "SENT"
        for result in deliveries
    ),
    f"local terminal notification delivery failed: {deliveries}",
)
for intent in (agent_intent, approval_intent):
    first = reconcile_terminal_notification(
        DATABASE_URL, outbox_message_id=intent.outbox_message_id
    )
    replay = reconcile_terminal_notification(
        DATABASE_URL, outbox_message_id=intent.outbox_message_id
    )
    require(
        first.event_type == "REQUESTER_NOTIFICATION_SENT"
        and not first.idempotent_replay
        and replay.idempotent_replay,
        "terminal notification reconciliation was not exact",
    )
print("[5/6] Both terminal rejection routes notify once with exact replay: PASS")

# Group 6: aggregate evidence is complete and unrelated boundaries stayed isolated.
with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = connection.execute(
        """
        SELECT
          (SELECT count(*) FROM cases
           WHERE external_request_id LIKE 'HUMAN-CONSUMER-%') AS cases,
          (SELECT count(*) FROM case_events
           WHERE event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED') AS acknowledgements,
          (SELECT count(*) FROM case_events
           WHERE event_type = 'HUMAN_REVIEW_REANALYZED') AS agent_routes,
          (SELECT count(*) FROM outbox_messages
           WHERE message_type = 'REQUESTER_NOTIFICATION') AS notifications,
          (SELECT count(*) FROM delivery_attempts) AS delivery_attempts,
          (SELECT count(*) FROM case_events
           WHERE event_type = 'REQUESTER_NOTIFICATION_SENT') AS sent_events,
          (SELECT count(*) FROM outbox_messages
           WHERE status IN ('PENDING', 'PROCESSING')) AS unfinished,
          (SELECT count(*) FROM outbox_messages
           WHERE message_type = 'DOWNSTREAM_ACTION') AS downstream_actions,
          (SELECT count(*) FROM case_events
           WHERE event_type = 'POLICY_RETRIEVAL_STARTED') AS retrievals,
          (SELECT count(*) FROM (
               SELECT event_payload->>'human_resume_reference'
               FROM case_events
               WHERE event_type = 'HUMAN_REVIEW_REANALYZED'
               GROUP BY event_payload->>'human_resume_reference'
               HAVING count(*) > 1
           ) AS duplicates) AS duplicate_routes
        """
    ).fetchone()
require(
    dict(aggregate)
    == {
        "cases": 5,
        "acknowledgements": 5,
        "agent_routes": 2,
        "notifications": 2,
        "delivery_attempts": 2,
        "sent_events": 2,
        "unfinished": 0,
        "downstream_actions": 0,
        "retrievals": 0,
        "duplicate_routes": 0,
    },
    f"unexpected human-resume consumer aggregate: {dict(aggregate)}",
)
print("[6/6] Aggregate evidence, no unfinished work, and boundary isolation: PASS")
print("Human-resume consumers integration summary")
print("  Integration groups: 6/6 PASS")
print("  Fictional consumer cases: 5")
print("  Durable resume acknowledgements: 5")
print("  Fixture AI calls: 1")
print("  Deterministic agent routes: 2")
print("  Delivered terminal notifications: 2")
print("  Duplicate consumer effects: 0")
print("  Unfinished consumer work: 0")
print("  Hosted or paid AI calls: 0")
print("  Human-resume consumers gate: PASS")
