"""Focused disposable check for the primary web and webhook intake boundary."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from delivery import claim_next_message
from intake import IntakeRequest, RequesterSelector, create_or_replay_case
from main import SESSION_COOKIE_NAME, create_session_cookie


DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
API_URL = os.environ["PRIMARY_API_URL"].rstrip("/")
WEBHOOK_TOKEN = os.environ["INTAKE_WEBHOOK_TOKEN"]
SESSION_SECRET = os.environ["APP_SESSION_SECRET"]

REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


HTTP = urllib.request.build_opener(NoRedirect)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def http_request(
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with HTTP.open(request, timeout=5) as response:
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
            return response.status, response.read(), response_headers
    except urllib.error.HTTPError as error:
        response_headers = {
            key.lower(): value for key, value in error.headers.items()
        }
        return error.code, error.read(), response_headers


def webhook_payload(external_request_id: str) -> dict[str, Any]:
    return {
        "external_request_id": external_request_id,
        "requester_reference": "EMP-201",
        "subject": "Warehouse access request",
        "message": "Please grant read access to WMS for inventory checks.",
        "attachments": [
            {
                "name": "manager-note.txt",
                "media_type": "text/plain",
                "size_bytes": 128,
            }
        ],
        "received_at": (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
    }


def post_webhook(
    payload: dict[str, Any],
    *,
    token: str | None = WEBHOOK_TOKEN,
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    status, raw_body, _headers = http_request(
        "/api/v1/requests",
        method="POST",
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    return status, json.loads(raw_body.decode("utf-8"))


def primary_counts() -> tuple[int, int, int]:
    with psycopg.connect(DATABASE_URL) as connection:
        return connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM cases),
                (SELECT COUNT(*) FROM case_events),
                (SELECT COUNT(*) FROM outbox_messages)
            """
        ).fetchone()


def case_evidence(external_request_id: str) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT
                cases.case_id,
                cases.case_reference,
                cases.source_channel,
                cases.requester_id,
                cases.subject,
                cases.original_message,
                cases.attachment_metadata,
                cases.content_fingerprint,
                cases.current_state,
                cases.version,
                case_events.sequence_number,
                case_events.from_state,
                case_events.to_state,
                case_events.event_type,
                case_events.actor_type,
                case_events.actor_user_id,
                outbox_messages.message_type,
                outbox_messages.destination,
                outbox_messages.status AS outbox_status,
                outbox_messages.attempt_count,
                outbox_messages.max_attempts,
                outbox_messages.payload
            FROM cases
            JOIN case_events
              ON case_events.case_id = cases.case_id
             AND case_events.sequence_number = 1
            JOIN outbox_messages
              ON outbox_messages.case_id = cases.case_id
             AND outbox_messages.message_type = 'WORKFLOW_START'
            WHERE cases.external_request_id = %s
            """,
            (external_request_id,),
        ).fetchone()
    require(row is not None, f"No complete intake evidence for {external_request_id}")
    return dict(row)


print("AI Service Request Automation - primary intake integration check")
print("Scope: shared web and REST-webhook case creation")
print("Database: disposable PostgreSQL with fictional fixtures")
print("")

baseline_counts = primary_counts()
require(baseline_counts == (2, 2, 0), f"Unexpected fixture baseline: {baseline_counts}")

status, raw_body, _headers = http_request("/health")
require(status == 200, "Primary API health check failed")
require(json.loads(raw_body) == {"status": "ok"}, "Unexpected health body")
with psycopg.connect(DATABASE_URL) as connection:
    constraint_definition = connection.execute(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conname = 'outbox_messages_type_allowed'
        """
    ).fetchone()[0]
    sequence_exists = connection.execute(
        "SELECT to_regclass('public.case_reference_sequence')"
    ).fetchone()[0]
require(sequence_exists is not None, "The case-reference sequence is missing")
require(
    "WORKFLOW_START" in constraint_definition,
    "The outbox constraint does not permit workflow-start messages",
)
invalid_payload = webhook_payload("INTAKE-INVALID-0001")
invalid_payload["unexpected_field"] = "must be rejected"
status, response_body = post_webhook(invalid_payload)
require(
    status == 422 and response_body["error_code"] == "INVALID_REQUEST",
    "Unknown webhook fields were not rejected",
)
require(primary_counts() == baseline_counts, "Invalid input created database rows")
print("[1/10] Health, migration, and strict request shape: PASS")

status, response_body = post_webhook(
    webhook_payload("INTAKE-AUTH-0001"),
    token=None,
)
require(
    status == 401 and response_body["error_code"] == "AUTHENTICATION_REQUIRED",
    "Missing webhook authentication was not rejected",
)
require(primary_counts() == baseline_counts, "Unauthenticated input created rows")
print("[2/10] Invalid integration authentication creates no rows: PASS")

unauthorized_payload = webhook_payload("INTAKE-AUTH-0002")
unauthorized_payload["requester_reference"] = "UNKNOWN-999"
status, response_body = post_webhook(unauthorized_payload)
require(
    status == 403 and response_body["error_code"] == "REQUESTER_NOT_AUTHORIZED",
    "Unknown requester was not rejected generically",
)
require(primary_counts() == baseline_counts, "Unauthorized requester created rows")
print("[3/10] Requester authorization creates no rows on rejection: PASS")

webhook_request = webhook_payload("INTAKE-HOOK-0001")
status, created_body = post_webhook(webhook_request)
require(
    status == 201
    and created_body["current_state"] == "RECEIVED"
    and created_body["idempotent_replay"] is False,
    "The new webhook request did not return the creation contract",
)
webhook_evidence = case_evidence("INTAKE-HOOK-0001")
require(
    webhook_evidence["source_channel"] == "WEBHOOK"
    and webhook_evidence["current_state"] == "RECEIVED"
    and webhook_evidence["version"] == 1,
    "The webhook case foundation is inconsistent",
)
require(
    webhook_evidence["sequence_number"] == 1
    and webhook_evidence["from_state"] is None
    and webhook_evidence["to_state"] == "RECEIVED"
    and webhook_evidence["event_type"] == "CASE_RECEIVED"
    and webhook_evidence["actor_type"] == "INTEGRATION"
    and webhook_evidence["actor_user_id"] is None,
    "The webhook creation event is inconsistent",
)
require(
    webhook_evidence["message_type"] == "WORKFLOW_START"
    and webhook_evidence["destination"] == "N8N_REQUEST_INTAKE"
    and webhook_evidence["outbox_status"] == "PENDING"
    and webhook_evidence["attempt_count"] == 0
    and webhook_evidence["max_attempts"] == 3,
    "The workflow-start intent is inconsistent",
)
require(
    webhook_evidence["payload"]
    == {
        "case_id": str(webhook_evidence["case_id"]),
        "case_reference": webhook_evidence["case_reference"],
        "case_version": 1,
        "schema_version": "1",
        "trigger_event": "CASE_RECEIVED",
    },
    "The workflow-start payload is not minimal and stable",
)
require(
    primary_counts()
    == tuple(value + 1 for value in baseline_counts),
    "A new webhook did not commit exactly 3 related rows",
)
print("[4/10] Webhook commits 1 case, event, and workflow intent: PASS")

created_counts = primary_counts()
status, replay_body = post_webhook(webhook_request)
require(
    status == 200
    and replay_body["case_reference"] == created_body["case_reference"]
    and replay_body["idempotent_replay"] is True,
    "Exact replay did not return the existing case",
)
require(primary_counts() == created_counts, "Exact replay created extra rows")
print("[5/10] Exact webhook replay returns the same case without writes: PASS")

conflicting_request = dict(webhook_request)
conflicting_request["subject"] = "Different content under the same source ID"
status, conflict_body = post_webhook(conflicting_request)
require(
    status == 409 and conflict_body["error_code"] == "IDEMPOTENCY_CONFLICT",
    "Conflicting replay did not return HTTP 409",
)
require(primary_counts() == created_counts, "Conflicting replay mutated state")
print("[6/10] Conflicting replay is terminal and non-persistent: PASS")

status, _body, headers = http_request("/requests/new")
require(
    status == 303 and headers.get("location") == "/login?next=/requests/new",
    "Unauthenticated browser intake did not redirect to login",
)
session_cookie = create_session_cookie(REQUESTER_ID, SESSION_SECRET)
cookie_header = {"Cookie": f"{SESSION_COOKIE_NAME}={session_cookie}"}
status, form_body, _headers = http_request(
    "/requests/new",
    headers=cookie_header,
)
require(status == 200, "An authenticated requester could not open the form")
form_html = form_body.decode("utf-8")
submission_match = re.search(
    r'name="submission_id" value="([^"]+)"',
    form_html,
)
csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', form_html)
require(submission_match is not None, "The form submission ID is missing")
require(csrf_match is not None, "The form CSRF token is missing")
submission_id = submission_match.group(1)
csrf_token = csrf_match.group(1)
form_fields = {
    "submission_id": submission_id,
    "csrf_token": csrf_token,
    "subject": "VPN access request",
    "message": "Please provide standard VPN access for remote work.",
}
status, _body, headers = http_request(
    "/requests",
    method="POST",
    body=urllib.parse.urlencode(form_fields).encode("utf-8"),
    headers={
        **cookie_header,
        "Content-Type": "application/x-www-form-urlencoded",
    },
)
require(status == 303, "The valid web form did not redirect to its case")
case_location = headers.get("location")
require(case_location is not None, "The web form response omitted its case URL")
web_evidence = case_evidence(submission_id)
require(
    web_evidence["source_channel"] == "WEB"
    and web_evidence["actor_type"] == "USER"
    and web_evidence["actor_user_id"] == REQUESTER_ID,
    "The web adapter did not preserve its requester actor",
)
web_counts = primary_counts()
status, _body, replay_headers = http_request(
    "/requests",
    method="POST",
    body=urllib.parse.urlencode(form_fields).encode("utf-8"),
    headers={
        **cookie_header,
        "Content-Type": "application/x-www-form-urlencoded",
    },
)
require(
    status == 303 and replay_headers.get("location") == case_location,
    "The exact web replay did not redirect to the same case",
)
require(primary_counts() == web_counts, "The exact web replay created extra rows")
status, receipt_body, _headers = http_request(
    case_location,
    headers=cookie_header,
)
require(
    status == 200 and web_evidence["case_reference"] in receipt_body.decode("utf-8"),
    "The requester could not read the small case receipt",
)
invalid_csrf_fields = dict(form_fields)
invalid_csrf_fields["submission_id"] = (
    "WEB-10000000-0000-4000-8000-000000000099"
)
status, _body, _headers = http_request(
    "/requests",
    method="POST",
    body=urllib.parse.urlencode(invalid_csrf_fields).encode("utf-8"),
    headers={
        **cookie_header,
        "Content-Type": "application/x-www-form-urlencoded",
    },
)
require(status == 403, "An invalid form CSRF token was not rejected")
require(primary_counts() == web_counts, "Invalid CSRF input created rows")
print("[7/10] Signed-session web form shares safe case creation: PASS")

duplicate_request = webhook_payload("INTAKE-HOOK-0002")
status, duplicate_body = post_webhook(duplicate_request)
require(
    status == 201
    and duplicate_body["case_reference"] != created_body["case_reference"],
    "A different external ID did not create a separate case",
)
duplicate_evidence = case_evidence("INTAKE-HOOK-0002")
require(
    duplicate_evidence["case_id"] != webhook_evidence["case_id"]
    and duplicate_evidence["content_fingerprint"]
    == webhook_evidence["content_fingerprint"],
    "Possible-duplicate fingerprint behavior is inconsistent",
)
print("[8/10] Matching content remains a separate review candidate: PASS")

pre_rollback_counts = primary_counts()
with psycopg.connect(DATABASE_URL) as connection:
    connection.execute(
        """
        CREATE FUNCTION reject_test_workflow_start()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'Controlled workflow-start rejection';
        END;
        $$
        """
    )
    connection.execute(
        """
        CREATE TRIGGER reject_test_workflow_start
        BEFORE INSERT ON outbox_messages
        FOR EACH ROW
        WHEN (NEW.message_type = 'WORKFLOW_START')
        EXECUTE FUNCTION reject_test_workflow_start()
        """
    )
try:
    status, rollback_body = post_webhook(
        webhook_payload("INTAKE-ROLLBACK-0001")
    )
    require(
        status == 503
        and rollback_body["error_code"] == "INTAKE_UNAVAILABLE"
        and rollback_body["retryable"] is True,
        "A forced atomic failure did not return the retryable contract",
    )
finally:
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            "DROP TRIGGER reject_test_workflow_start ON outbox_messages"
        )
        connection.execute("DROP FUNCTION reject_test_workflow_start()")
require(primary_counts() == pre_rollback_counts, "Atomic failure left partial rows")

case_input_immutable = False
try:
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE cases SET original_message = 'mutated' WHERE case_id = %s",
            (webhook_evidence["case_id"],),
        )
except psycopg.errors.CheckViolation:
    case_input_immutable = True
require(case_input_immutable, "Original case input was mutable")

workflow_intent_immutable = False
try:
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            """
            UPDATE outbox_messages
            SET payload = payload || '{"mutated": true}'::jsonb
            WHERE case_id = %s AND message_type = 'WORKFLOW_START'
            """,
            (webhook_evidence["case_id"],),
        )
except psycopg.errors.CheckViolation:
    workflow_intent_immutable = True
require(workflow_intent_immutable, "Workflow-start intent was mutable")
print("[9/10] Atomic rollback and immutable inputs remain enforced: PASS")

concurrent_request = IntakeRequest(
    source_channel="WEBHOOK",
    external_request_id="INTAKE-CONCURRENT-0001",
    subject="Concurrent service request",
    message="Create only 1 case when the source retries concurrently.",
    attachment_metadata=[],
    received_at=datetime.now(timezone.utc) - timedelta(seconds=1),
)


def submit_concurrently() -> Any:
    return create_or_replay_case(
        DATABASE_URL,
        concurrent_request,
        RequesterSelector(employee_reference="EMP-201"),
    )


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    concurrent_results = list(executor.map(lambda _item: submit_concurrently(), range(2)))
require(
    sorted(result.idempotent_replay for result in concurrent_results)
    == [False, True],
    "Concurrent submissions did not resolve to 1 creation and 1 replay",
)
require(
    concurrent_results[0].case_reference == concurrent_results[1].case_reference,
    "Concurrent submissions returned different cases",
)
with psycopg.connect(DATABASE_URL) as connection:
    concurrent_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM cases
        WHERE source_channel = 'WEBHOOK'
          AND external_request_id = 'INTAKE-CONCURRENT-0001'
        """
    ).fetchone()[0]
require(concurrent_count == 1, "Concurrent submissions created duplicate cases")
require(
    claim_next_message(DATABASE_URL) is None,
    "The downstream worker incorrectly claimed a WORKFLOW_START message",
)
final_counts = primary_counts()
require(final_counts == (6, 6, 4), f"Unexpected final intake evidence: {final_counts}")
print("[10/10] Concurrent replay and delivery-worker isolation: PASS")

print("")
print("Primary intake integration summary")
print("  Integration groups: 10/10 PASS")
print("  Baseline fictional cases: 2")
print("  Newly committed intake cases: 4")
print("  New CASE_RECEIVED events: 4")
print("  New WORKFLOW_START messages: 4")
print("  Exact replay duplicate rows: 0")
print("  Conflicting replay mutations: 0")
print("  Forced rollback partial rows: 0")
print("  Intake integration gate: PASS")
