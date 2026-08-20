"""Controlled primary-database to sandbox delivery integration check."""

from __future__ import annotations

import copy
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from delivery import ServiceDeskClient, process_one_message


PRIMARY_DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
SANDBOX_DATABASE_URL = os.environ["SERVICE_DESK_SANDBOX_DATABASE_URL"]
SANDBOX_URL = os.environ["SERVICE_DESK_SANDBOX_URL"]
SANDBOX_TOKEN = os.environ["SERVICE_DESK_SANDBOX_TOKEN"]

CASE_ID = "40000000-0000-4000-8000-000000000002"

SUCCESS_ID = "92000000-0000-4000-8000-000000000001"
REPLAY_ID = "92000000-0000-4000-8000-000000000002"
CONFLICT_ID = "92000000-0000-4000-8000-000000000003"
TRANSIENT_ID = "92000000-0000-4000-8000-000000000004"
PERMANENT_ID = "92000000-0000-4000-8000-000000000005"

SUCCESS_KEY = "a" * 64
REPLAY_KEY = "b" * 64
CONFLICT_KEY = "c" * 64
TRANSIENT_KEY = "d" * 64
PERMANENT_KEY = "e" * 64

INCIDENT_PAYLOAD = {
    "case_reference": "CASE-2026-0002",
    "case_version": 1,
    "action_type": "INCIDENT_TICKET",
    "title": "Warehouse portal unavailable",
    "summary": "The warehouse portal has been unavailable since 09:15.",
    "details": {
        "affected_service": "Warehouse Management System",
        "impact": "HIGH",
        "urgency": "HIGH",
        "description": "Users cannot open the warehouse portal.",
    },
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def insert_outbox(
    message_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = 3,
) -> None:
    with psycopg.connect(PRIMARY_DATABASE_URL) as connection:
        connection.execute(
            """
            INSERT INTO outbox_messages (
                outbox_message_id,
                case_id,
                message_type,
                destination,
                idempotency_key,
                payload,
                max_attempts,
                available_at,
                created_at
            )
            VALUES (%s, %s, 'DOWNSTREAM_ACTION', 'service-desk-sandbox',
                    %s, %s, %s, now(), now())
            """,
            (
                message_id,
                CASE_ID,
                idempotency_key,
                Jsonb(payload),
                max_attempts,
            ),
        )


def outbox_state(message_id: str) -> dict[str, Any]:
    with psycopg.connect(PRIMARY_DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT status, attempt_count, locked_at, last_error, completed_at
            FROM outbox_messages
            WHERE outbox_message_id = %s
            """,
            (message_id,),
        ).fetchone()
    require(row is not None, f"Outbox fixture {message_id} was not found")
    return dict(row)


client = ServiceDeskClient(SANDBOX_URL, SANDBOX_TOKEN)

print("AI Service Request Automation - primary delivery integration check")
print("Scope: primary outbox worker to local Service Desk Sandbox")
print("Fixtures: 5 fictional delivery intents")
print("")

with psycopg.connect(PRIMARY_DATABASE_URL) as primary_connection:
    primary_counts = primary_connection.execute(
        "SELECT (SELECT COUNT(*) FROM outbox_messages), "
        "       (SELECT COUNT(*) FROM delivery_attempts)"
    ).fetchone()
    primary_has_sandbox_table = primary_connection.execute(
        "SELECT to_regclass('public.service_records')"
    ).fetchone()[0]

with psycopg.connect(SANDBOX_DATABASE_URL) as sandbox_connection:
    sandbox_counts = sandbox_connection.execute(
        "SELECT (SELECT COUNT(*) FROM service_records), "
        "       (SELECT COUNT(*) FROM service_record_events)"
    ).fetchone()
    sandbox_has_primary_table = sandbox_connection.execute(
        "SELECT to_regclass('public.outbox_messages')"
    ).fetchone()[0]

require(primary_counts == (0, 0), "Primary delivery tables did not start empty")
require(sandbox_counts == (0, 0), "Sandbox tables did not start empty")
require(primary_has_sandbox_table is None, "Sandbox table leaked into primary DB")
require(sandbox_has_primary_table is None, "Primary table leaked into sandbox DB")
print("[1/6] Separate primary and sandbox database boundaries: PASS")

insert_outbox(SUCCESS_ID, SUCCESS_KEY, INCIDENT_PAYLOAD)
execution = process_one_message(PRIMARY_DATABASE_URL, client, retry_delay_seconds=0)
require(execution is not None, "The success fixture was not claimed")
require(
    execution.outcome == "SUCCESS"
    and execution.http_status == 201
    and execution.final_status == "SENT",
    "The new downstream record was not completed successfully",
)
state = outbox_state(SUCCESS_ID)
require(
    state["attempt_count"] == 1
    and state["locked_at"] is None
    and state["completed_at"] is not None,
    "The successful outbox state is inconsistent",
)
print("[2/6] HTTP 201 delivery records 1 successful attempt: PASS")

precreated = client.create_service_record(INCIDENT_PAYLOAD, REPLAY_KEY)
require(
    precreated.outcome == "SUCCESS" and precreated.http_status == 201,
    "The exact-replay precondition could not be created",
)
insert_outbox(REPLAY_ID, REPLAY_KEY, INCIDENT_PAYLOAD)
execution = process_one_message(PRIMARY_DATABASE_URL, client, retry_delay_seconds=0)
require(execution is not None, "The replay fixture was not claimed")
require(
    execution.outcome == "SUCCESS"
    and execution.http_status == 200
    and execution.downstream_reference == precreated.downstream_reference,
    "The exact replay did not reuse the downstream reference",
)
require(outbox_state(REPLAY_ID)["status"] == "SENT", "Replay was not marked sent")
print("[3/6] HTTP 200 exact replay completes the original outbox intent: PASS")

precreated = client.create_service_record(INCIDENT_PAYLOAD, CONFLICT_KEY)
require(precreated.http_status == 201, "The conflict precondition could not be created")
conflicting_payload = copy.deepcopy(INCIDENT_PAYLOAD)
conflicting_payload["summary"] = "Different content using the same delivery key."
insert_outbox(CONFLICT_ID, CONFLICT_KEY, conflicting_payload)
execution = process_one_message(PRIMARY_DATABASE_URL, client, retry_delay_seconds=0)
require(execution is not None, "The conflict fixture was not claimed")
require(
    execution.outcome == "PERMANENT_FAILURE"
    and execution.http_status == 409
    and execution.final_status == "FAILED",
    "The idempotency conflict was not terminal",
)
state = outbox_state(CONFLICT_ID)
require(
    state["attempt_count"] == 1
    and state["locked_at"] is None
    and state["completed_at"] is not None,
    "The conflicting outbox state is inconsistent",
)
print("[4/6] HTTP 409 conflict becomes 1 permanent delivery failure: PASS")

insert_outbox(TRANSIENT_ID, TRANSIENT_KEY, INCIDENT_PAYLOAD, max_attempts=2)
execution = process_one_message(
    PRIMARY_DATABASE_URL,
    client,
    test_outcome="TRANSIENT_ONCE",
    retry_delay_seconds=0,
)
require(execution is not None, "The transient fixture was not claimed")
require(
    execution.outcome == "TRANSIENT_FAILURE"
    and execution.http_status == 503
    and execution.final_status == "PENDING",
    "The transient failure was not returned to the bounded queue",
)
state = outbox_state(TRANSIENT_ID)
require(
    state["attempt_count"] == 1
    and state["completed_at"] is None
    and state["last_error"],
    "The transient pending state is inconsistent",
)

execution = process_one_message(
    PRIMARY_DATABASE_URL,
    client,
    test_outcome="TRANSIENT_ONCE",
    retry_delay_seconds=0,
)
require(execution is not None, "The transient retry was not claimed")
require(
    execution.outcome == "SUCCESS"
    and execution.http_status == 201
    and execution.attempt_number == 2
    and execution.final_status == "SENT",
    "The bounded transient retry did not recover",
)
state = outbox_state(TRANSIENT_ID)
require(
    state["status"] == "SENT"
    and state["attempt_count"] == 2
    and state["locked_at"] is None
    and state["last_error"] is None
    and state["completed_at"] is not None,
    "The recovered transient outbox state is inconsistent",
)
print("[5/6] HTTP 503 records 1 failure and succeeds on attempt 2: PASS")

insert_outbox(PERMANENT_ID, PERMANENT_KEY, INCIDENT_PAYLOAD)
execution = process_one_message(
    PRIMARY_DATABASE_URL,
    client,
    test_outcome="PERMANENT_FAILURE",
    retry_delay_seconds=0,
)
require(execution is not None, "The permanent fixture was not claimed")
require(
    execution.outcome == "PERMANENT_FAILURE"
    and execution.http_status == 422
    and execution.final_status == "FAILED",
    "The permanent failure was not terminal",
)
state = outbox_state(PERMANENT_ID)
require(
    state["attempt_count"] == 1
    and state["locked_at"] is None
    and state["last_error"]
    and state["completed_at"] is not None,
    "The permanently failed outbox state is inconsistent",
)

with psycopg.connect(PRIMARY_DATABASE_URL, row_factory=dict_row) as connection:
    primary_count_row = connection.execute(
        "SELECT (SELECT COUNT(*) FROM outbox_messages) AS outbox_count, "
        "       (SELECT COUNT(*) FROM delivery_attempts) AS attempt_count"
    ).fetchone()
    final_primary_counts = (
        primary_count_row["outbox_count"],
        primary_count_row["attempt_count"],
    )
    statuses = {
        row["status"]: row["count"]
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM outbox_messages GROUP BY status"
        ).fetchall()
    }
    outcomes = {
        row["outcome"]: row["count"]
        for row in connection.execute(
            "SELECT outcome, COUNT(*) AS count FROM delivery_attempts GROUP BY outcome"
        ).fetchall()
    }
    processing_count = connection.execute(
        "SELECT COUNT(*) AS count "
        "FROM outbox_messages WHERE status = 'PROCESSING'"
    ).fetchone()["count"]

with psycopg.connect(SANDBOX_DATABASE_URL, row_factory=dict_row) as connection:
    sandbox_count_row = connection.execute(
        "SELECT (SELECT COUNT(*) FROM service_records) AS record_count, "
        "       (SELECT COUNT(*) FROM service_record_events) AS event_count"
    ).fetchone()
    final_sandbox_counts = (
        sandbox_count_row["record_count"],
        sandbox_count_row["event_count"],
    )

require(final_primary_counts == (5, 6), "Unexpected primary delivery evidence")
require(statuses == {"SENT": 3, "FAILED": 2}, f"Unexpected outbox states: {statuses}")
require(
    outcomes
    == {"SUCCESS": 3, "TRANSIENT_FAILURE": 1, "PERMANENT_FAILURE": 2},
    f"Unexpected delivery outcomes: {outcomes}",
)
require(processing_count == 0, "A claimed message remained locked")
require(final_sandbox_counts == (4, 8), "Unexpected sandbox delivery evidence")
print("[6/6] HTTP 422 and aggregate cross-database consistency: PASS")

print("")
print("Primary delivery integration summary")
print("  Integration groups: 6/6 PASS")
print("  Fictional outbox messages: 5")
print("  Append-only delivery attempts: 6")
print("  Successful outbox messages: 3")
print("  Terminal outbox messages: 2")
print("  Sandbox service records: 4")
print("  Sandbox audit events: 8")
print("  Delivery integration gate: PASS")
