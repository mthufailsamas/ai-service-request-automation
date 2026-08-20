"""Focused scheduled-recovery and operational-evidence integration check."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
PRIMARY_API_URL = os.environ["PRIMARY_API_URL"].rstrip("/")
PRIMARY_TOKEN = os.environ["PRIMARY_WORKFLOW_TOKEN"]
N8N_URL = os.environ["N8N_RECOVERY_URL"]
N8N_TOKEN = os.environ["N8N_RECOVERY_TOKEN"]
WORKFLOW_FILE = Path(os.environ["RECOVERY_WORKFLOW_FILE"])
REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
TEST_PASSWORD = "controlled-recovery-password"


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


def decode_json_object(raw_body: bytes) -> dict[str, Any]:
    if not raw_body:
        return {}
    try:
        decoded = json.loads(raw_body)
    except json.JSONDecodeError:
        return {
            "unparsed_body": raw_body.decode("utf-8", errors="replace")[:500]
        }
    return decoded if isinstance(decoded, dict) else {"unexpected_json": decoded}


def post_json(
    url: str, body: dict[str, Any], token: str | None
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, decode_json_object(response.read())
    except urllib.error.HTTPError as error:
        return error.code, decode_json_object(error.read())


def make_case_and_claim(
    number: int,
    message_type: str,
    destination: str,
    *,
    max_attempts: int = 3,
    expired: bool = True,
) -> UUID:
    case_id = uuid4()
    outbox_id = uuid4()
    external_id = f"RECOVERY-{number:02d}"
    digest = hashlib.sha256(external_id.encode()).hexdigest()
    created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    locked_at = datetime.now(timezone.utc) - timedelta(
        minutes=2 if expired else 0
    )
    if not expired:
        locked_at = datetime.now(timezone.utc)
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            """
            INSERT INTO cases (
                case_id, case_reference, source_channel, external_request_id,
                idempotency_key, content_fingerprint, requester_id, subject,
                original_message, attachment_metadata, current_state, version,
                received_at, created_at, updated_at
            ) VALUES (
                %s, %s, 'WEB', %s, %s, %s, %s,
                'Controlled recovery fixture', 'Fictional local data.', '[]',
                'ANALYZING', 1, %s, %s, %s
            )
            """,
            (
                case_id,
                f"CASE-2026-{9700 + number:04d}",
                external_id,
                digest,
                digest,
                REQUESTER_ID,
                created_at,
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload, occurred_at
            ) VALUES (
                %s, 1, NULL, 'ANALYZING', 'RECOVERY_FIXTURE_CREATED',
                'INTEGRATION', 'Controlled recovery fixture.', '{}', %s
            )
            """,
            (case_id, created_at),
        )
        connection.execute(
            """
            INSERT INTO outbox_messages (
                outbox_message_id, case_id, message_type, destination,
                idempotency_key, payload, status, attempt_count, max_attempts,
                available_at, locked_at, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'PROCESSING', 1, %s, %s, %s, %s
            )
            """,
            (
                outbox_id,
                case_id,
                message_type,
                destination,
                hashlib.sha256(str(outbox_id).encode()).hexdigest(),
                Jsonb({"schema_version": "1", "fixture": number}),
                max_attempts,
                created_at,
                locked_at,
                created_at,
            ),
        )
    return outbox_id


def load_claims(ids: list[UUID]) -> list[dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT message.outbox_message_id, message.message_type,
                       message.status, message.attempt_count,
                       count(attempt.delivery_attempt_id) AS attempts
                FROM outbox_messages AS message
                LEFT JOIN delivery_attempts AS attempt
                  ON attempt.outbox_message_id = message.outbox_message_id
                WHERE message.outbox_message_id = ANY(%s)
                GROUP BY message.outbox_message_id
                ORDER BY message.message_type
                """,
                (ids,),
            ).fetchall()
        ]


def browser() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


def browser_request(
    opener: urllib.request.OpenerDirector,
    path: str,
    *,
    data: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        PRIMARY_API_URL + path,
        data=(urllib.parse.urlencode(data).encode() if data is not None else None),
        method="POST" if data is not None else "GET",
    )
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def login(employee_reference: str) -> urllib.request.OpenerDirector:
    opener = browser()
    status, page = browser_request(opener, "/login?next=/cases")
    require(status == 200, "login form was unavailable")
    match = re.search(rb'name="csrf_token" value="([0-9a-f]{64})"', page)
    require(match is not None, "login CSRF token was missing")
    status, _body = browser_request(
        opener,
        "/login",
        data={
            "csrf_token": match.group(1).decode(),
            "employee_reference": employee_reference,
            "next": "/cases",
            "password": TEST_PASSWORD,
        },
    )
    require(status == 200, "portal login did not reach the case list")
    return opener


print("AI Service Request Automation - recovery and operations integration check")
print("Scope: 6 focused groups; fictional data; local n8n; 0 AI calls.")
print()

workflow = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
node_types = {node["type"] for node in workflow["nodes"]}
require("n8n-nodes-base.scheduleTrigger" in node_types, "schedule trigger missing")
require(
    workflow["connections"]["Every Five Minutes"]["main"][0][0]["node"]
    == "Run Scheduled Recovery",
    "the schedule is not connected to the primary recovery endpoint",
)
status, body = post_json(
    PRIMARY_API_URL + "/internal/v1/recovery/sweep",
    {"schema_version": "1", "lease_seconds": 60,
     "retry_delay_seconds": 0, "limit": 20},
    None,
)
require(status == 401 and body.get("error_code") == "AUTHENTICATION_REQUIRED", "primary auth guard failed")
status, body = post_json(
    PRIMARY_API_URL + "/internal/v1/recovery/sweep",
    {"schema_version": "1", "lease_seconds": 60,
     "retry_delay_seconds": 0, "limit": 20, "extra": True},
    PRIMARY_TOKEN,
)
require(status == 422 and body.get("error_code") == "INVALID_RECOVERY_COMMAND", "strict recovery command failed")
print("[1/6] Scheduled definition, primary authentication, and strict command: PASS")

status, _body = post_json(N8N_URL, {"schema_version": "1"}, None)
require(status in {401, 403}, "n8n accepted missing authentication")
status, body = post_json(N8N_URL, {"schema_version": "2"}, N8N_TOKEN)
require(status == 422 and body.get("error_code") == "INVALID_RECOVERY_TRIGGER", "n8n trigger guard failed")
print("[2/6] Distinct n8n authentication and trigger validation: PASS")

accepted = [
    make_case_and_claim(1, "WORKFLOW_START", "N8N_REQUEST_INTAKE"),
    make_case_and_claim(2, "HUMAN_DECISION_RESUME", "N8N_HUMAN_DECISION_RESUME"),
    make_case_and_claim(3, "DOWNSTREAM_ACTION", "service-desk-sandbox"),
    make_case_and_claim(4, "REQUESTER_NOTIFICATION", "local-requester-inbox", max_attempts=1),
]
wrong_destination = make_case_and_claim(5, "DOWNSTREAM_ACTION", "wrong-destination")
unexpired = make_case_and_claim(6, "WORKFLOW_START", "N8N_REQUEST_INTAKE", expired=False)
status, body = post_json(N8N_URL, {"schema_version": "1"}, N8N_TOKEN)
require(
    status == 200
    and body.get("recovered_claims") == 4
    and body.get("pending_retries") == 3
    and body.get("terminal_failures") == 1
    and set(body.get("recovered_by_type", {})) == {
        "WORKFLOW_START", "HUMAN_DECISION_RESUME",
        "DOWNSTREAM_ACTION", "REQUESTER_NOTIFICATION"
    },
    f"unexpected recovery acknowledgement: {body}",
)
evidence = load_claims(accepted + [wrong_destination, unexpired])
accepted_rows = [row for row in evidence if row["outbox_message_id"] in accepted]
isolated_rows = [row for row in evidence if row["outbox_message_id"] not in accepted]
require(
    sum(row["status"] == "PENDING" for row in accepted_rows) == 3
    and sum(row["status"] == "FAILED" for row in accepted_rows) == 1
    and all(row["attempts"] == 1 for row in accepted_rows),
    "accepted expired claims were not durably recovered",
)
require(all(row["status"] == "PROCESSING" and row["attempts"] == 0 for row in isolated_rows), "recovery crossed its isolation boundary")
print("[3/6] Four accepted claim types recover within fixed limits: PASS")

status, body = post_json(N8N_URL, {"schema_version": "1"}, N8N_TOKEN)
require(status == 200 and body.get("recovered_claims") == 0, "exact recovery replay changed durable evidence")
print("[4/6] Exact replay is a no-op and preserves isolation: PASS")

concurrent_ids = [
    make_case_and_claim(7, "DOWNSTREAM_ACTION", "service-desk-sandbox"),
    make_case_and_claim(8, "REQUESTER_NOTIFICATION", "local-requester-inbox"),
]
with ThreadPoolExecutor(max_workers=2) as executor:
    responses = list(executor.map(
        lambda _index: post_json(N8N_URL, {"schema_version": "1"}, N8N_TOKEN),
        range(2),
    ))
require(all(status == 200 for status, _body in responses), "concurrent recovery request failed")
require(sum(body.get("recovered_claims", -100) for _status, body in responses) == 2, "concurrent sweeps duplicated or lost a claim")
require(all(row["attempts"] == 1 for row in load_claims(concurrent_ids)), "concurrent recovery duplicated append-only evidence")
print("[5/6] Concurrent sweeps append one durable recovery per claim: PASS")

with psycopg.connect(DATABASE_URL) as connection:
    connection.execute(
        "UPDATE users SET password_hash = crypt(%s, gen_salt('bf', 4)), updated_at = now()",
        (TEST_PASSWORD,),
    )
anonymous_status, _ = browser_request(browser(), "/api/v1/operations/summary")
requester_status, _ = browser_request(login("EMP-201"), "/api/v1/operations/summary")
admin = login("ADM-001")
admin_status, raw_summary = browser_request(admin, "/api/v1/operations/summary")
dashboard_status, dashboard = browser_request(admin, "/operations")
summary = json.loads(raw_summary)
require(anonymous_status == 401 and requester_status == 403, "operations access guard failed")
require(
    admin_status == 200
    and dashboard_status == 200
    and b"Local operational evidence" in dashboard
    and summary["totals"]["recovered_expired_claims"] == 6
    and summary["totals"]["delivery_attempts"] == 6
    and summary["totals"]["claims_processing"] == 2
    and summary["totals"]["analyses_processing"] == 0,
    f"unexpected operational evidence: {summary}",
)
print("[6/6] Admin-only dashboard and aggregate durable evidence: PASS")

print("Recovery and operations integration summary")
print("  Integration groups: 6/6 PASS")
print("  Fictional recovery claims: 8")
print("  Recovered expired claims: 6")
print("  Pending bounded retries: 5")
print("  Terminal attempt-limit failures: 1")
print("  Duplicate recovery attempts: 0")
print("  Isolated active or unknown claims: 2")
print("  External AI calls: 0")
print("  Recovery and operations gate: PASS")
