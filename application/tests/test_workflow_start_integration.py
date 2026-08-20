"""Controlled primary-outbox to local n8n workflow-start integration check."""

from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sys
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import psycopg
from psycopg.rows import dict_row

from delivery import claim_next_message
from workflow_start import (
    WorkflowStartClient,
    claim_next_workflow_start,
    process_one_workflow_start,
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
INTAKE_TOKEN = os.environ["INTAKE_WEBHOOK_TOKEN"]
N8N_URL = os.environ["N8N_WORKFLOW_START_URL"]
N8N_TOKEN = os.environ["N8N_WORKFLOW_START_TOKEN"]
PRIMARY_WORKFLOW_TOKEN = os.environ["PRIMARY_WORKFLOW_TOKEN"]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def http_json(
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=(
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        ),
        headers=headers or {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw_body = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        raw_body = error.read()
        status = error.code
        content_type = error.headers.get("Content-Type", "")
    decoded_body = raw_body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(decoded_body) if raw_body else {}
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"HTTP {status} response from {url} was not JSON "
            f"(content-type={content_type or 'missing'}; "
            f"body={decoded_body[:500]!r})"
        ) from error
    require(isinstance(parsed, dict), "HTTP response was not a JSON object")
    return status, parsed


def create_case(external_request_id: str) -> dict[str, Any]:
    payload = {
        "external_request_id": external_request_id,
        "requester_reference": "EMP-201",
        "subject": "Workflow start integration request",
        "message": "Start deterministic analysis for this fictional request.",
        "attachments": [],
        "received_at": (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
    }
    status, response = http_json(
        f"{API_URL}/api/v1/requests",
        body=payload,
        headers={
            "Authorization": f"Bearer {INTAKE_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    require(status == 201, f"Could not create {external_request_id}: {response}")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT
                cases.case_id,
                cases.case_reference,
                cases.current_state,
                cases.version,
                outbox_messages.outbox_message_id,
                outbox_messages.idempotency_key,
                outbox_messages.payload
            FROM cases
            JOIN outbox_messages
              ON outbox_messages.case_id = cases.case_id
             AND outbox_messages.message_type = 'WORKFLOW_START'
            WHERE cases.external_request_id = %s
            """,
            (external_request_id,),
        ).fetchone()
    require(row is not None, f"Missing durable fixture for {external_request_id}")
    return dict(row)


def case_evidence(case_id: Any) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        case = connection.execute(
            """
            SELECT current_state, version
            FROM cases
            WHERE case_id = %s
            """,
            (case_id,),
        ).fetchone()
        events = connection.execute(
            """
            SELECT event_id, sequence_number, from_state, to_state,
                   actor_type, event_payload
            FROM case_events
            WHERE case_id = %s AND event_type = 'ANALYSIS_STARTED'
            ORDER BY sequence_number
            """,
            (case_id,),
        ).fetchall()
    require(case is not None, "Case evidence was not found")
    return {
        "current_state": case["current_state"],
        "version": case["version"],
        "events": [dict(event) for event in events],
    }


def outbox_evidence(outbox_message_id: Any) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        outbox = connection.execute(
            """
            SELECT status, attempt_count, locked_at, last_error, completed_at
            FROM outbox_messages
            WHERE outbox_message_id = %s
            """,
            (outbox_message_id,),
        ).fetchone()
        attempts = connection.execute(
            """
            SELECT attempt_number, outcome, http_status, downstream_reference,
                   response_payload, error_code, error_message
            FROM delivery_attempts
            WHERE outbox_message_id = %s
            ORDER BY attempt_number
            """,
            (outbox_message_id,),
        ).fetchall()
    require(outbox is not None, "Outbox evidence was not found")
    return {
        **dict(outbox),
        "attempts": [dict(attempt) for attempt in attempts],
    }


def concise_latest_attempt(outbox_message_id: Any) -> str:
    """Keep one failed transport record useful without flooding the terminal."""

    evidence = outbox_evidence(outbox_message_id)
    attempts = evidence["attempts"]
    if not attempts:
        return '{"attempt":"missing"}'

    latest = attempts[-1]
    useful_fields = {
        "outbox_status": evidence["status"],
        "attempt_number": latest["attempt_number"],
        "outcome": latest["outcome"],
        "http_status": latest["http_status"],
        "response_payload": latest["response_payload"],
        "error_code": latest["error_code"],
    }
    encoded = json.dumps(
        useful_fields,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return encoded[:1_000]


def direct_analysis_start(
    fixture: dict[str, Any],
    *,
    token: str | None,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": fixture["idempotency_key"],
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = fixture["payload"]
    return http_json(
        f"{API_URL}/internal/v1/cases/{fixture['case_id']}/analysis-start",
        body={
            "schema_version": payload["schema_version"],
            "case_reference": payload["case_reference"],
            "expected_case_version": payload["case_version"],
            "trigger_event": payload["trigger_event"],
        },
        headers=headers,
    )


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format_string: str, *args: Any) -> None:
        return None


class FixedResponseHandler(QuietHandler):
    status_code = 503
    response_body: dict[str, Any] = {
        "error_code": "CONTROLLED_TEMPORARY_FAILURE",
        "message": "Controlled temporary n8n failure.",
        "retryable": True,
    }

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        encoded = json.dumps(self.response_body).encode("utf-8")
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class InvalidSuccessHandler(FixedResponseHandler):
    status_code = 200
    response_body = {"status": "ACCEPTED"}


class ForwardThenDropHandler(QuietHandler):
    forwarded_status: int | None = None
    forwarded_body: dict[str, Any] | None = None

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        forwarded = urllib.request.Request(
            N8N_URL,
            data=body,
            headers={
                "Authorization": self.headers["Authorization"],
                "Content-Type": "application/json",
                "Idempotency-Key": self.headers["Idempotency-Key"],
            },
            method="POST",
        )
        with urllib.request.urlopen(forwarded, timeout=5) as response:
            raw_response = response.read()
            type(self).forwarded_status = response.status
            type(self).forwarded_body = json.loads(raw_response.decode("utf-8"))

        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class LocalServer:
    def __init__(self, handler: type[BaseHTTPRequestHandler]):
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/workflow-start"

    def __enter__(self) -> LocalServer:
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


client = WorkflowStartClient(N8N_URL, N8N_TOKEN)

print("AI Service Request Automation - workflow-start integration check")
print("Scope: primary outbox, local n8n, and guarded analysis start")
print("Fixtures: fictional cases and temporary local credentials")
print("")

status, body = http_json(f"{API_URL}/health")
require(status == 200 and body == {"status": "ok"}, "Primary health failed")
with psycopg.connect(DATABASE_URL) as connection:
    baseline = connection.execute(
        "SELECT (SELECT COUNT(*) FROM cases), "
        "       (SELECT COUNT(*) FROM case_events), "
        "       (SELECT COUNT(*) FROM outbox_messages), "
        "       (SELECT COUNT(*) FROM delivery_attempts)"
    ).fetchone()
require(baseline == (2, 2, 0, 0), f"Unexpected database baseline: {baseline}")

status, body = http_json(
    N8N_URL,
    body={"schema_version": "1", "unexpected": "rejected"},
    headers={
        "Authorization": f"Bearer {N8N_TOKEN}",
        "Content-Type": "application/json",
        "Idempotency-Key": "f" * 64,
    },
)
require(
    status == 422 and body["error_code"] == "INVALID_WORKFLOW_START",
    "n8n did not reject a malformed workflow-start payload",
)
with psycopg.connect(DATABASE_URL) as connection:
    after_invalid_shape = connection.execute(
        "SELECT (SELECT COUNT(*) FROM cases), "
        "       (SELECT COUNT(*) FROM case_events), "
        "       (SELECT COUNT(*) FROM outbox_messages), "
        "       (SELECT COUNT(*) FROM delivery_attempts)"
    ).fetchone()
require(after_invalid_shape == baseline, "Invalid n8n input changed primary data")

primary_auth = create_case("WORKFLOW-PRIMARY-AUTH-0001")
status, body = direct_analysis_start(primary_auth, token=None)
require(
    status == 401 and body["error_code"] == "AUTHENTICATION_REQUIRED",
    "The primary workflow endpoint accepted missing authentication",
)
require(
    case_evidence(primary_auth["case_id"])["current_state"] == "RECEIVED",
    "Rejected primary authentication changed the case",
)
execution = process_one_workflow_start(DATABASE_URL, client, retry_delay_seconds=0)
handoff_succeeded = (
    execution is not None
    and execution.outcome == "SUCCESS"
    and execution.final_status == "SENT"
)
if not handoff_succeeded:
    attempt = concise_latest_attempt(primary_auth["outbox_message_id"])
    raise AssertionError(
        "The authenticated n8n-to-primary handoff did not succeed: "
        f"execution={execution!r}; attempt={attempt}"
    )
print("[1/8] Strict n8n input and distinct primary authentication: PASS")

n8n_auth = create_case("WORKFLOW-N8N-AUTH-0001")
wrong_n8n_client = WorkflowStartClient(N8N_URL, "wrong-" + N8N_TOKEN)
execution = process_one_workflow_start(
    DATABASE_URL,
    wrong_n8n_client,
    retry_delay_seconds=0,
)
require(
    execution is not None
    and execution.outcome == "PERMANENT_FAILURE"
    and execution.http_status in {401, 403}
    and execution.final_status == "FAILED",
    "Invalid n8n webhook authentication was not terminal",
)
require(
    case_evidence(n8n_auth["case_id"])
    == {"current_state": "RECEIVED", "version": 1, "events": []},
    "Invalid n8n authentication changed the case",
)
print("[2/8] Invalid n8n authentication is recorded without a transition: PASS")

concurrent_fixture = create_case("WORKFLOW-CONCURRENT-0001")


def run_dispatcher() -> Any:
    return process_one_workflow_start(DATABASE_URL, client, retry_delay_seconds=0)


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    concurrent_results = list(executor.map(lambda _item: run_dispatcher(), range(2)))
completed = [result for result in concurrent_results if result is not None]
require(len(completed) == 1, "Concurrent dispatchers processed more than 1 claim")
require(
    completed[0].outcome == "SUCCESS" and completed[0].final_status == "SENT",
    "The single concurrent claim did not complete",
)
concurrent_case = case_evidence(concurrent_fixture["case_id"])
concurrent_outbox = outbox_evidence(concurrent_fixture["outbox_message_id"])
require(
    concurrent_case["current_state"] == "ANALYZING"
    and concurrent_case["version"] == 2
    and len(concurrent_case["events"]) == 1
    and concurrent_case["events"][0]["sequence_number"] == 2
    and concurrent_case["events"][0]["from_state"] == "RECEIVED"
    and concurrent_case["events"][0]["to_state"] == "ANALYZING"
    and concurrent_case["events"][0]["actor_type"] == "INTEGRATION",
    "The durable fresh transition evidence is inconsistent",
)
require(
    concurrent_outbox["attempt_count"] == 1
    and len(concurrent_outbox["attempts"]) == 1
    and concurrent_outbox["attempts"][0]["outcome"] == "SUCCESS",
    "The fresh workflow-start attempt evidence is inconsistent",
)
print("[3/8] Concurrent dispatchers create 1 durable analysis start: PASS")

lost_response = create_case("WORKFLOW-LOST-RESPONSE-0001")
ForwardThenDropHandler.forwarded_status = None
ForwardThenDropHandler.forwarded_body = None
with LocalServer(ForwardThenDropHandler) as proxy:
    execution = process_one_workflow_start(
        DATABASE_URL,
        WorkflowStartClient(proxy.url, N8N_TOKEN),
        retry_delay_seconds=0,
    )
require(
    execution is not None
    and execution.outcome == "TRANSIENT_FAILURE"
    and execution.final_status == "PENDING"
    and ForwardThenDropHandler.forwarded_status == 200,
    "The controlled lost response was not recorded as transient",
)
first_start = case_evidence(lost_response["case_id"])
require(
    first_start["current_state"] == "ANALYZING"
    and first_start["version"] == 2
    and len(first_start["events"]) == 1,
    "The lost response did not preserve its committed primary transition",
)
event_reference = f"WFSTART-{first_start['events'][0]['event_id']}"
execution = process_one_workflow_start(DATABASE_URL, client, retry_delay_seconds=0)
require(
    execution is not None
    and execution.outcome == "SUCCESS"
    and execution.attempt_number == 2
    and execution.downstream_reference == event_reference,
    "The lost-response retry did not recover the stable event reference",
)
lost_evidence = outbox_evidence(lost_response["outbox_message_id"])
require(
    len(case_evidence(lost_response["case_id"])["events"]) == 1
    and lost_evidence["attempts"][1]["response_payload"]["idempotent_replay"]
    is True,
    "The lost-response retry duplicated the transition or missed replay",
)
print("[4/8] Lost response retries to the same start event without duplication: PASS")

transient = create_case("WORKFLOW-TRANSIENT-0001")
with LocalServer(FixedResponseHandler) as temporary_server:
    execution = process_one_workflow_start(
        DATABASE_URL,
        WorkflowStartClient(temporary_server.url, N8N_TOKEN),
        retry_delay_seconds=0,
    )
require(
    execution is not None
    and execution.outcome == "TRANSIENT_FAILURE"
    and execution.http_status == 503
    and execution.final_status == "PENDING",
    "The controlled HTTP 503 was not retryable",
)
require(
    case_evidence(transient["case_id"])["current_state"] == "RECEIVED",
    "The simulated transient response changed the case",
)
execution = process_one_workflow_start(DATABASE_URL, client, retry_delay_seconds=0)
require(
    execution is not None
    and execution.outcome == "SUCCESS"
    and execution.attempt_number == 2,
    "The transient workflow-start retry did not recover",
)
print("[5/8] HTTP 503 records 1 failure and succeeds on attempt 2: PASS")

invalid_success = create_case("WORKFLOW-INVALID-ACK-0001")
with LocalServer(InvalidSuccessHandler) as invalid_server:
    invalid_client = WorkflowStartClient(invalid_server.url, N8N_TOKEN)
    invalid_executions = [
        process_one_workflow_start(
            DATABASE_URL,
            invalid_client,
            retry_delay_seconds=0,
        )
        for _attempt in range(3)
    ]
require(
    all(execution is not None for execution in invalid_executions)
    and [execution.final_status for execution in invalid_executions]
    == ["PENDING", "PENDING", "FAILED"]
    and all(
        execution.outcome == "TRANSIENT_FAILURE"
        for execution in invalid_executions
    ),
    "Invalid success acknowledgement did not stop at 3 attempts",
)
invalid_evidence = outbox_evidence(invalid_success["outbox_message_id"])
require(
    case_evidence(invalid_success["case_id"])["current_state"] == "RECEIVED"
    and invalid_evidence["attempt_count"] == 3
    and len(invalid_evidence["attempts"]) == 3,
    "Invalid acknowledgement left inconsistent bounded evidence",
)
print("[6/8] Invalid HTTP 200 acknowledgement stops at the fixed limit: PASS")

abandoned = create_case("WORKFLOW-ABANDONED-0001")
claimed = claim_next_workflow_start(DATABASE_URL)
require(
    claimed is not None
    and claimed.outbox_message_id == abandoned["outbox_message_id"],
    "The abandoned-claim fixture was not reserved",
)
execution = process_one_workflow_start(
    DATABASE_URL,
    client,
    retry_delay_seconds=0,
    lease_seconds=0,
)
require(
    execution is not None
    and execution.outcome == "TRANSIENT_FAILURE"
    and execution.attempt_number == 1
    and execution.final_status == "PENDING",
    "The expired claim was not recovered honestly",
)
lease_evidence = outbox_evidence(abandoned["outbox_message_id"])
require(
    lease_evidence["attempts"][0]["error_code"] == "DISPATCH_LEASE_EXPIRED"
    and lease_evidence["attempts"][0]["response_payload"]
    == {"transport_outcome": "UNKNOWN"},
    "The expired claim invented a known transport outcome",
)
execution = process_one_workflow_start(DATABASE_URL, client, retry_delay_seconds=0)
require(
    execution is not None
    and execution.outcome == "SUCCESS"
    and execution.attempt_number == 2,
    "The recovered claim did not succeed on its next invocation",
)
print("[7/8] Expired dispatcher claim is recorded and safely recovered: PASS")

isolated = create_case("WORKFLOW-WORKER-ISOLATION-0001")
require(
    claim_next_message(DATABASE_URL) is None,
    "The Service Desk worker claimed a WORKFLOW_START message",
)
execution = process_one_workflow_start(DATABASE_URL, client, retry_delay_seconds=0)
require(
    execution is not None
    and execution.outbox_message_id == isolated["outbox_message_id"]
    and execution.outcome == "SUCCESS",
    "The isolated workflow-start message did not complete",
)

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    final = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM cases
             WHERE external_request_id LIKE 'WORKFLOW-%') AS workflow_cases,
            (SELECT COUNT(*) FROM case_events
             WHERE event_type = 'ANALYSIS_STARTED') AS start_events,
            (SELECT COUNT(*) FROM outbox_messages
             WHERE message_type = 'WORKFLOW_START') AS workflow_outbox,
            (SELECT COUNT(*) FROM delivery_attempts) AS attempts,
            (SELECT COUNT(*) FROM outbox_messages
             WHERE status IN ('PENDING', 'PROCESSING')) AS unfinished
        """
    ).fetchone()
    statuses = {
        row["status"]: row["count"]
        for row in connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM outbox_messages
            WHERE message_type = 'WORKFLOW_START'
            GROUP BY status
            """
        ).fetchall()
    }
    outcomes = {
        row["outcome"]: row["count"]
        for row in connection.execute(
            "SELECT outcome, COUNT(*) AS count "
            "FROM delivery_attempts GROUP BY outcome"
        ).fetchall()
    }
require(
    tuple(final.values()) == (8, 6, 8, 13, 0),
    f"Unexpected aggregate workflow evidence: {dict(final)}",
)
require(statuses == {"SENT": 6, "FAILED": 2}, f"Unexpected states: {statuses}")
require(
    outcomes
    == {"SUCCESS": 6, "TRANSIENT_FAILURE": 6, "PERMANENT_FAILURE": 1},
    f"Unexpected attempt outcomes: {outcomes}",
)
print("[8/8] Service Desk isolation and aggregate workflow evidence: PASS")

print("")
print("Workflow-start integration summary")
print("  Integration groups: 8/8 PASS")
print("  Fictional workflow cases: 8")
print("  ANALYSIS_STARTED events: 6")
print("  Workflow-start outbox messages: 8")
print("  Append-only delivery attempts: 13")
print("  Successful workflow starts: 6")
print("  Terminal rejected starts: 2")
print("  Duplicate analysis-start events: 0")
print("  Unfinished workflow messages: 0")
print("  Workflow-start integration gate: PASS")
