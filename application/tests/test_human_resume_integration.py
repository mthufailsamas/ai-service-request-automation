"""Controlled committed-human-decision to local n8n resume integration check."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from human_resume import (
    ACTION_EVENT_ROUTE,
    ACTION_STATE,
    HumanResumeClient,
    claim_next_human_resume,
    enqueue_next_human_resume,
    process_one_human_resume,
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
N8N_URL = os.environ["N8N_HUMAN_RESUME_URL"]
N8N_TOKEN = os.environ["N8N_HUMAN_RESUME_TOKEN"]
PRIMARY_TOKEN = os.environ["PRIMARY_WORKFLOW_TOKEN"]


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
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers or {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw_body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw_body = error.read()
        status = error.code
    try:
        parsed = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except json.JSONDecodeError as error:
        raise AssertionError(f"HTTP {status} did not return JSON") from error
    require(isinstance(parsed, dict), "HTTP response was not a JSON object")
    return status, parsed


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


fixture_counter = 5000


def create_human_event(action: str) -> dict[str, Any]:
    """Create only the already-verified committed-event boundary."""

    global fixture_counter
    fixture_counter += 1
    event_type, resume_route = ACTION_EVENT_ROUTE[action]
    current_state = ACTION_STATE[action]
    initial_state = {
        "SUBMIT_INFORMATION": "NEEDS_INFORMATION",
        "CONFIRM_REVIEW": "NEEDS_REVIEW",
        "CORRECT_REVIEW": "NEEDS_REVIEW",
        "REJECT_REVIEW": "NEEDS_REVIEW",
        "APPROVE_REQUEST": "PENDING_APPROVAL",
        "REJECT_REQUEST": "PENDING_APPROVAL",
    }[action]
    case_reference = f"CASE-2026-{fixture_counter}"
    case_id = uuid4()
    command_id = uuid4()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        actor = connection.execute(
            "SELECT user_id FROM users WHERE is_active ORDER BY employee_reference LIMIT 1"
        ).fetchone()
        require(actor is not None, "The fictional identity seed is missing")
        connection.execute(
            """
            INSERT INTO cases (
                case_id, case_reference, source_channel, external_request_id,
                idempotency_key, content_fingerprint, requester_id, subject,
                original_message, attachment_metadata, current_state, version,
                received_at
            )
            VALUES (%s, %s, 'WEBHOOK', %s, %s, %s, %s, %s, %s, '[]', %s, 2, %s)
            """,
            (
                case_id,
                case_reference,
                f"HUMAN-RESUME-{fixture_counter}",
                _sha(f"idempotency-{fixture_counter}"),
                _sha(f"content-{fixture_counter}"),
                actor["user_id"],
                f"Human resume fixture {fixture_counter}",
                "Fictional committed human decision.",
                current_state,
                datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        )
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            )
            VALUES (%s, 1, NULL, %s, 'FIXTURE_CASE_PREPARED', 'SYSTEM',
                    'Controlled pre-decision state for the resume check.', '{}')
            """,
            (case_id, initial_state),
        )
        event = connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, actor_user_id, reason, event_payload
            )
            VALUES (%s, 2, %s, %s, %s, 'USER', %s,
                    'Previously verified fictional human decision.', %s)
            RETURNING event_id
            """,
            (
                case_id,
                initial_state,
                current_state,
                event_type,
                actor["user_id"],
                Jsonb(
                    {
                        "action": action,
                        "human_command_id": str(command_id),
                        "input_sha256": _sha(str(command_id)),
                        "result_case_version": 2,
                        "schema_version": "1",
                    }
                ),
            ),
        ).fetchone()
    require(event is not None, "The committed human event was not created")
    return {
        "case_id": case_id,
        "case_reference": case_reference,
        "event_id": event["event_id"],
        "action": action,
        "event_type": event_type,
        "resume_route": resume_route,
        "current_state": current_state,
    }


def enqueue_all() -> list[Any]:
    identifiers: list[Any] = []
    while True:
        identifier = enqueue_next_human_resume(DATABASE_URL)
        if identifier is None:
            return identifiers
        identifiers.append(identifier)


def fixture_outbox(fixture: dict[str, Any]) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT outbox_message_id, idempotency_key, payload, status,
                   attempt_count, max_attempts
            FROM outbox_messages
            WHERE message_type = 'HUMAN_DECISION_RESUME'
              AND payload->>'human_decision_reference' = %s
            """,
            (f"HD-{fixture['event_id']}",),
        ).fetchone()
    require(row is not None, "The human-resume intent is missing")
    return dict(row)


def direct_primary(
    fixture: dict[str, Any],
    outbox: dict[str, Any],
    *,
    token: str | None,
) -> tuple[int, dict[str, Any]]:
    payload = outbox["payload"]
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": outbox["idempotency_key"],
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return http_json(
        f"{API_URL}/internal/v1/cases/{fixture['case_id']}/human-resume",
        body={
            "schema_version": "1",
            "case_reference": fixture["case_reference"],
            "expected_case_version": 2,
            "human_decision_reference": payload["human_decision_reference"],
            "action": fixture["action"],
            "trigger_event": fixture["event_type"],
            "resume_route": fixture["resume_route"],
        },
        headers=headers,
    )


class QuietHandler(BaseHTTPRequestHandler):
    status_code = 503
    response_body: dict[str, Any] = {
        "error_code": "CONTROLLED_TEMPORARY_FAILURE",
        "message": "Controlled temporary human-resume failure.",
        "retryable": True,
    }

    def log_message(self, format_string: str, *args: Any) -> None:
        return None

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        encoded = json.dumps(self.response_body).encode("utf-8")
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class InvalidSuccessHandler(QuietHandler):
    status_code = 200
    response_body = {"status": "ACCEPTED"}


class LocalServer:
    def __init__(self, handler: type[BaseHTTPRequestHandler]):
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/human-resume"

    def __enter__(self) -> LocalServer:
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


client = HumanResumeClient(N8N_URL, N8N_TOKEN)

actions = list(ACTION_EVENT_ROUTE)
normal_fixtures = [create_human_event(action) for action in actions]
created = enqueue_all()
require(len(created) == 6, f"Expected 6 resume intents, received {len(created)}")
require(enqueue_all() == [], "Resume-intent derivation was not idempotent")
for fixture in normal_fixtures:
    payload = fixture_outbox(fixture)["payload"]
    require(
        payload
        == {
            "schema_version": "1",
            "case_id": str(fixture["case_id"]),
            "case_reference": fixture["case_reference"],
            "case_version": 2,
            "human_decision_reference": f"HD-{fixture['event_id']}",
            "action": fixture["action"],
            "trigger_event": fixture["event_type"],
            "resume_route": fixture["resume_route"],
        },
        "A derived resume intent did not preserve its exact source event",
    )
print("[1/6] Six committed actions derive exact immutable resume intents: PASS")

first_outbox = fixture_outbox(normal_fixtures[0])
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
    status == 422 and body.get("error_code") == "INVALID_HUMAN_RESUME",
    "n8n accepted a malformed human-resume intent",
)
status, body = direct_primary(normal_fixtures[0], first_outbox, token=None)
require(
    status == 401 and body.get("error_code") == "AUTHENTICATION_REQUIRED",
    "The primary endpoint accepted missing authentication",
)
with psycopg.connect(DATABASE_URL) as connection:
    acknowledgement_count = connection.execute(
        "SELECT COUNT(*) FROM case_events "
        "WHERE event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED'"
    ).fetchone()[0]
require(acknowledgement_count == 0, "Rejected inputs created an acknowledgement")
print("[2/6] Strict n8n input and distinct primary authentication: PASS")

for _fixture in normal_fixtures:
    execution = process_one_human_resume(DATABASE_URL, client, retry_delay_seconds=0)
    require(
        execution is not None
        and execution.outcome == "SUCCESS"
        and execution.final_status == "SENT",
        f"The six-action n8n handoff failed: {execution!r}",
    )
with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    route_counts = {
        row["resume_route"]: row["count"]
        for row in connection.execute(
            """
            SELECT event_payload->>'resume_route' AS resume_route,
                   COUNT(*) AS count
            FROM case_events
            WHERE event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
            GROUP BY event_payload->>'resume_route'
            """
        ).fetchall()
    }
require(
    route_counts
    == {
        "ANALYSIS_CONTINUATION": 3,
        "DOWNSTREAM_ACTION": 1,
        "TERMINAL_NOTIFICATION": 2,
    },
    f"The six-action route evidence is inconsistent: {route_counts}",
)
print("[3/6] Local n8n acknowledges all 3 bounded resume routes: PASS")

concurrent_fixture = create_human_event("SUBMIT_INFORMATION")
require(len(enqueue_all()) == 1, "The concurrent fixture was not enqueued once")


def run_dispatcher() -> Any:
    return process_one_human_resume(DATABASE_URL, client, retry_delay_seconds=0)


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    concurrent_results = list(executor.map(lambda _item: run_dispatcher(), range(2)))
completed = [result for result in concurrent_results if result is not None]
require(
    len(completed) == 1
    and completed[0].outcome == "SUCCESS"
    and completed[0].attempt_number == 1,
    f"Concurrent dispatchers did not preserve one claim: {concurrent_results!r}",
)
concurrent_outbox = fixture_outbox(concurrent_fixture)
status, first_replay = direct_primary(
    concurrent_fixture, concurrent_outbox, token=PRIMARY_TOKEN
)
status_2, second_replay = direct_primary(
    concurrent_fixture, concurrent_outbox, token=PRIMARY_TOKEN
)
require(
    status == status_2 == 200
    and first_replay == second_replay
    and first_replay["idempotent_replay"] is True,
    "Exact primary replay was not stable",
)
print("[4/6] Concurrent claim and exact acknowledgement replay are idempotent: PASS")

transient_fixture = create_human_event("APPROVE_REQUEST")
require(len(enqueue_all()) == 1, "The transient fixture was not enqueued")
with LocalServer(QuietHandler) as temporary_server:
    transient = process_one_human_resume(
        DATABASE_URL,
        HumanResumeClient(temporary_server.url, N8N_TOKEN),
        retry_delay_seconds=0,
    )
require(
    transient is not None
    and transient.outcome == "TRANSIENT_FAILURE"
    and transient.final_status == "PENDING"
    and transient.http_status == 503,
    "HTTP 503 was not recorded as retryable",
)
recovered = process_one_human_resume(DATABASE_URL, client, retry_delay_seconds=0)
require(
    recovered is not None
    and recovered.outcome == "SUCCESS"
    and recovered.attempt_number == 2,
    "The retryable intent did not recover on attempt 2",
)

invalid_fixture = create_human_event("REJECT_REQUEST")
require(len(enqueue_all()) == 1, "The invalid-response fixture was not enqueued")
with LocalServer(InvalidSuccessHandler) as invalid_server:
    invalid_client = HumanResumeClient(invalid_server.url, N8N_TOKEN)
    invalid_runs = [
        process_one_human_resume(
            DATABASE_URL, invalid_client, retry_delay_seconds=0
        )
        for _attempt in range(3)
    ]
require(
    [run.final_status for run in invalid_runs if run is not None]
    == ["PENDING", "PENDING", "FAILED"]
    and all(
        run is not None and run.outcome == "TRANSIENT_FAILURE"
        for run in invalid_runs
    ),
    "An invalid HTTP 200 acknowledgement did not stop at the fixed limit",
)

abandoned_fixture = create_human_event("REJECT_REVIEW")
require(len(enqueue_all()) == 1, "The abandoned fixture was not enqueued")
claimed = claim_next_human_resume(DATABASE_URL)
require(
    claimed is not None
    and claimed.outbox_message_id
    == fixture_outbox(abandoned_fixture)["outbox_message_id"],
    "The abandoned fixture was not reserved",
)
expired = process_one_human_resume(
    DATABASE_URL, client, retry_delay_seconds=0, lease_seconds=0
)
require(
    expired is not None
    and expired.outcome == "TRANSIENT_FAILURE"
    and expired.final_status == "PENDING",
    "The expired claim was not recovered honestly",
)
abandoned_recovery = process_one_human_resume(
    DATABASE_URL, client, retry_delay_seconds=0
)
require(
    abandoned_recovery is not None
    and abandoned_recovery.outcome == "SUCCESS"
    and abandoned_recovery.attempt_number == 2,
    "The expired claim did not recover on attempt 2",
)
print("[5/6] Retry, invalid response, fixed limit, and expired lease: PASS")

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = dict(connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM cases
           WHERE external_request_id LIKE 'HUMAN-RESUME-%') AS cases,
          (SELECT COUNT(*) FROM case_events
           WHERE event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED') AS acks,
          (SELECT COUNT(*) FROM outbox_messages
           WHERE message_type = 'HUMAN_DECISION_RESUME') AS outbox,
          (SELECT COUNT(*) FROM delivery_attempts) AS attempts,
          (SELECT COUNT(*) FROM outbox_messages
           WHERE message_type = 'HUMAN_DECISION_RESUME'
             AND status = 'SENT') AS sent,
          (SELECT COUNT(*) FROM outbox_messages
           WHERE message_type = 'HUMAN_DECISION_RESUME'
             AND status = 'FAILED') AS failed,
          (SELECT COUNT(*) FROM outbox_messages
           WHERE status IN ('PENDING', 'PROCESSING')) AS unfinished,
          (SELECT COUNT(*) FROM outbox_messages
           WHERE message_type <> 'HUMAN_DECISION_RESUME') AS unrelated_outbox,
          (SELECT COUNT(*) FROM ai_analysis_runs) AS ai_runs
        """
    ).fetchone())
    duplicate_acks = connection.execute(
        """
        SELECT COUNT(*) AS duplicate_count FROM (
            SELECT event_payload->>'human_decision_reference'
            FROM case_events
            WHERE event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
            GROUP BY event_payload->>'human_decision_reference'
            HAVING COUNT(*) > 1
        ) AS duplicates
        """
    ).fetchone()["duplicate_count"]
require(
    aggregate
    == {
        "cases": 10,
        "acks": 9,
        "outbox": 10,
        "attempts": 14,
        "sent": 9,
        "failed": 1,
        "unfinished": 0,
        "unrelated_outbox": 0,
        "ai_runs": 0,
    },
    f"Unexpected aggregate human-resume evidence: {aggregate}",
)
require(duplicate_acks == 0, "Duplicate resume acknowledgements were created")
print("[6/6] Aggregate evidence and deferred-route isolation: PASS")

print("")
print("Human-decision resume integration summary")
print("  Integration groups: 6/6 PASS")
print("  Fictional resume cases: 10")
print("  Durable resume intents: 10")
print("  Successful acknowledgements: 9")
print("  Append-only delivery attempts: 14")
print("  Terminal invalid acknowledgements: 1")
print("  Duplicate resume acknowledgements: 0")
print("  Unfinished resume intents: 0")
print("  External AI calls: 0")
print("  Human-decision resume gate: PASS")
