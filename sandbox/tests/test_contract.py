"""Runtime contract check for the disposable Service Desk Sandbox stack."""

from __future__ import annotations

import copy
import json
import os
import urllib.error
import urllib.request
from typing import Any

import psycopg
from psycopg.rows import dict_row


BASE_URL = os.environ["SERVICE_DESK_SANDBOX_BASE_URL"]
DATABASE_URL = os.environ["SERVICE_DESK_SANDBOX_DATABASE_URL"]
TOKEN = os.environ["SERVICE_DESK_SANDBOX_TOKEN"]

NORMAL_KEY = "1" * 64
TRANSIENT_KEY = "2" * 64
PERMANENT_KEY = "3" * 64
UNUSED_KEY = "4" * 64

INCIDENT_BODY = {
    "case_reference": "CASE-2026-0002",
    "case_version": 3,
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


def call_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = TOKEN,
    idempotency_key: str | None = None,
    test_outcome: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if test_outcome is not None:
        headers["X-Sandbox-Test-Outcome"] = test_outcome

    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as result:
            return result.status, json.loads(result.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def database_counts() -> tuple[int, int]:
    with psycopg.connect(DATABASE_URL) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM service_records"
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM service_record_events"
        ).fetchone()[0]
    return record_count, event_count


def require_database_rejection(statement: str) -> None:
    try:
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(statement)
    except psycopg.errors.CheckViolation:
        return
    raise AssertionError(f"Database unexpectedly accepted: {statement}")


print("AI Service Request Automation - Service Desk Sandbox check")
print("Contract: accepted v1")
print("Fixtures: fictional local records")
print("")

status, body = call_json("GET", "/health", token=None)
require(status == 200 and body == {"status": "ok"}, "Health check failed")
print("[1/8] Health and isolated 2-table schema: PASS")

status, body = call_json(
    "POST",
    "/api/v1/service-records",
    body=INCIDENT_BODY,
    token="wrong-token",
    idempotency_key=NORMAL_KEY,
)
require(status == 401, "Invalid authentication did not return HTTP 401")
require(body["error_code"] == "INVALID_SERVICE_CREDENTIAL", "Wrong auth error")
require(database_counts() == (0, 0), "Invalid authentication persisted data")
print("[2/8] Authentication rejects invalid credentials without data: PASS")

status, created = call_json(
    "POST",
    "/api/v1/service-records",
    body=INCIDENT_BODY,
    idempotency_key=NORMAL_KEY,
)
require(status == 201, "First accepted request did not return HTTP 201")
require(created["idempotent_replay"] is False, "Creation was marked as replay")
reference = created["service_record_reference"]

status, read_record = call_json(
    "GET", f"/api/v1/service-records/{reference}"
)
require(status == 200, "Created record could not be read")
require(read_record["case_reference"] == INCIDENT_BODY["case_reference"], "Wrong case")
require(read_record["details"] == INCIDENT_BODY["details"], "Wrong stored details")
print("[3/8] Authenticated creation and record read: PASS")

status, replay = call_json(
    "POST",
    "/api/v1/service-records",
    body=INCIDENT_BODY,
    idempotency_key=NORMAL_KEY,
)
require(status == 200, "Exact replay did not return HTTP 200")
require(replay["idempotent_replay"] is True, "Exact replay was not identified")
require(replay["service_record_reference"] == reference, "Replay changed reference")
require(database_counts()[0] == 1, "Exact replay created a duplicate record")
print("[4/8] Exact idempotent replay: PASS")

conflicting_body = copy.deepcopy(INCIDENT_BODY)
conflicting_body["summary"] = "Different content for the same delivery key."
status, conflict = call_json(
    "POST",
    "/api/v1/service-records",
    body=conflicting_body,
    idempotency_key=NORMAL_KEY,
)
require(status == 409, "Conflicting replay did not return HTTP 409")
require(conflict["error_code"] == "IDEMPOTENCY_CONFLICT", "Wrong conflict code")
require(database_counts()[0] == 1, "Conflicting replay created a record")
print("[5/8] Conflicting replay remains auditable and duplicate-free: PASS")

status, transient = call_json(
    "POST",
    "/api/v1/service-records",
    body=INCIDENT_BODY,
    idempotency_key=TRANSIENT_KEY,
    test_outcome="TRANSIENT_ONCE",
)
require(status == 503 and transient["retryable"] is True, "Wrong transient result")
status, recovered = call_json(
    "POST",
    "/api/v1/service-records",
    body=INCIDENT_BODY,
    idempotency_key=TRANSIENT_KEY,
    test_outcome="TRANSIENT_ONCE",
)
require(status == 201, "Bounded transient retry did not recover")
require(recovered["idempotent_replay"] is False, "Recovery was marked as replay")
print("[6/8] Controlled transient failure succeeds on retry: PASS")

status, permanent = call_json(
    "POST",
    "/api/v1/service-records",
    body=INCIDENT_BODY,
    idempotency_key=PERMANENT_KEY,
    test_outcome="PERMANENT_FAILURE",
)
require(status == 422 and permanent["retryable"] is False, "Wrong permanent result")
status, permanent_replay = call_json(
    "POST",
    "/api/v1/service-records",
    body=INCIDENT_BODY,
    idempotency_key=PERMANENT_KEY,
)
require(status == 422, "Permanent failure did not remain terminal")
require(permanent_replay["retryable"] is False, "Terminal replay became retryable")
print("[7/8] Controlled permanent failure remains terminal: PASS")

invalid_body = copy.deepcopy(INCIDENT_BODY)
del invalid_body["details"]["urgency"]
status, invalid = call_json(
    "POST",
    "/api/v1/service-records",
    body=invalid_body,
    idempotency_key=UNUSED_KEY,
)
require(status == 400 and invalid["error_code"] == "INVALID_REQUEST", "Wrong shape result")
status, invalid_outcome = call_json(
    "POST",
    "/api/v1/service-records",
    body=INCIDENT_BODY,
    idempotency_key=UNUSED_KEY,
    test_outcome="UNSUPPORTED",
)
require(
    status == 400
    and invalid_outcome["error_code"] == "INVALID_SANDBOX_TEST_OUTCOME",
    "Unsupported test outcome was not rejected",
)
status, missing = call_json("GET", "/api/v1/service-records/SR-2026-9999")
require(status == 404, "Unknown record did not return HTTP 404")

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    tables = {
        row["table_name"]
        for row in connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            """
        ).fetchall()
    }
    event_types = {
        row["event_type"]: row["count"]
        for row in connection.execute(
            """
            SELECT event_type, COUNT(*) AS count
            FROM service_record_events
            GROUP BY event_type
            """
        ).fetchall()
    }

require(
    tables == {"service_records", "service_record_events"},
    f"Sandbox database has an unexpected table boundary: {sorted(tables)}",
)
require(
    event_types
    == {
        "RECORD_CREATED": 2,
        "IDEMPOTENT_REPLAY": 1,
        "IDEMPOTENCY_CONFLICT": 1,
        "TRANSIENT_FAILURE": 1,
        "PERMANENT_FAILURE": 2,
    },
    f"Unexpected audit event evidence: {event_types}",
)
require(database_counts() == (2, 7), "Unexpected final record or event count")

require_database_rejection("UPDATE service_records SET title = 'Changed'")
require_database_rejection("UPDATE service_record_events SET event_payload = '{}'")
require_database_rejection("DELETE FROM service_record_events")
print("[8/8] Shape, not-found, isolation, immutability, and append-only rules: PASS")

print("")
print("Service Desk Sandbox summary")
print("  HTTP contract checks: 8/8 PASS")
print("  Service records: 2")
print("  Append-only events: 7")
print("  Controlled transient recovery: PASS")
print("  Controlled permanent rejection: PASS")
print("  Sandbox suitability gate: PASS")
