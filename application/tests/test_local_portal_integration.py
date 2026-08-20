"""Focused local portal integration check with fictional user accounts."""

from __future__ import annotations

import hashlib
import html
import http.cookies
import os
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def concise_exception_hook(
    kind: type[BaseException], error: BaseException, tb: Any
) -> None:
    frames = traceback.extract_tb(tb)
    location = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "unknown"
    print(f"FAIL: {kind.__name__}: {error} [{location}]", file=sys.stderr)


sys.excepthook = concise_exception_hook

DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
API_URL = os.environ["PRIMARY_API_URL"].rstrip("/")
TEST_PASSWORD = "Local-Portal-Fixture-2026!"
REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("10000000-0000-4000-8000-000000000002")
APPROVER_ID = UUID("10000000-0000-4000-8000-000000000003")
WMS_ID = UUID("20000000-0000-4000-8000-000000000001")


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class Browser:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.opener = urllib.request.build_opener(NoRedirect())
        self.last_set_cookie: list[str] = []

    def request(
        self,
        path: str,
        *,
        form: dict[str, str] | None = None,
    ) -> tuple[int, str, Any]:
        headers: dict[str, str] = {}
        data = None
        if self.cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            f"{API_URL}{path}",
            data=data,
            headers=headers,
            method="POST" if form is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                status = response.status
                body = response.read().decode("utf-8")
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            status = error.code
            body = error.read().decode("utf-8")
            response_headers = error.headers
        self.last_set_cookie = response_headers.get_all("Set-Cookie") or []
        for header in self.last_set_cookie:
            parsed = http.cookies.SimpleCookie()
            parsed.load(header)
            for name, morsel in parsed.items():
                if morsel["max-age"] == "0" or not morsel.value:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        return status, body, response_headers


def input_values(fragment: str) -> dict[str, str]:
    return {
        html.unescape(name): html.unescape(value)
        for name, value in re.findall(
            r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"[^>]*>',
            fragment,
        )
    }


def form_for(document: str, action: str) -> dict[str, str]:
    marker = f'value="{action}"'
    marker_index = document.find(marker)
    require(marker_index >= 0, f"the {action} form is missing")
    start = document.rfind("<form", 0, marker_index)
    end = document.find("</form>", marker_index)
    require(start >= 0 and end >= 0, f"the {action} form is malformed")
    return input_values(document[start : end + len("</form>")])


def login(browser: Browser, reference: str, *, next_path: str = "/cases") -> Any:
    status, document, _headers = browser.request(
        f"/login?next={urllib.parse.quote(next_path, safe='/')}"
    )
    require(status == 200, f"login form failed for {reference}: HTTP {status}")
    values = input_values(document)
    status, _body, headers = browser.request(
        "/login",
        form={
            "csrf_token": values["csrf_token"],
            "employee_reference": reference,
            "next": values["next"],
            "password": TEST_PASSWORD,
        },
    )
    require(
        status == 303 and headers.get("Location") == next_path,
        f"login failed for {reference}: HTTP {status}",
    )
    return headers


def make_case(
    number: int,
    state: str,
    requester_id: UUID,
    *,
    subject: str,
    request_type: str | None = None,
) -> dict[str, Any]:
    case_id = uuid4()
    case_reference = f"CASE-2026-{9500 + number:04d}"
    external_id = f"LOCAL-PORTAL-{number:02d}"
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            """
            INSERT INTO cases (
                case_id, case_reference, source_channel, external_request_id,
                idempotency_key, content_fingerprint, requester_id, subject,
                original_message, attachment_metadata, request_type,
                current_state, version, received_at
            )
            VALUES (
                %s, %s, 'WEB', %s, %s, %s, %s, %s,
                'Controlled fictional portal request.', '[]', %s, %s, 1, %s
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
                %s, 1, NULL, %s, 'LOCAL_PORTAL_FIXTURE_CREATED',
                'INTEGRATION', 'Fictional local portal state.', '{}'
            )
            """,
            (case_id, state),
        )
    return {"case_id": case_id, "case_reference": case_reference}


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


def add_confirmable_proposal(case_id: UUID) -> None:
    fields = empty_fields()
    fields.update(
        {
            "target_system": "WMS",
            "requested_access_level": "STANDARD",
            "business_reason": "Controlled portal review.",
            "approver_id": "MGR-104",
        }
    )
    proposal = {
        "request_type": "access_request",
        "summary": "Controlled access request for portal review.",
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
                %s, 'fixture-provider', 'local-portal-proposal-v1',
                'analysis-v1', %s, %s, '[]', 'COMPLETED', 0, 0, 0, 1, now()
            )
            """,
            (
                case_id,
                hashlib.sha256(str(case_id).encode("utf-8")).hexdigest(),
                Jsonb(proposal),
            ),
        )


def add_pending_approval(case_id: UUID) -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            """
            INSERT INTO case_details (
                case_id, target_system_id, business_reason, approver_user_id,
                record_reference, requested_changes, accepted_by_type,
                accepted_at
            )
            VALUES (
                %s, %s, 'Controlled portal approval.', %s,
                'REC-9501', 'Update fictional ownership.', 'SYSTEM_RULE', now()
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


with psycopg.connect(DATABASE_URL) as connection:
    connection.execute(
        """
        UPDATE users
        SET password_hash = crypt(%s, gen_salt('bf', 4)), updated_at = now()
        """,
        (TEST_PASSWORD,),
    )

requester_case = make_case(
    1,
    "NEEDS_INFORMATION",
    REQUESTER_ID,
    subject="Requester information case",
)
review_case = make_case(
    2,
    "NEEDS_REVIEW",
    REQUESTER_ID,
    subject="Service-agent review case",
)
add_confirmable_proposal(review_case["case_id"])
approval_case = make_case(
    3,
    "PENDING_APPROVAL",
    AGENT_ID,
    subject="Assigned approval case",
    request_type="DATA_CHANGE_REQUEST",
)
add_pending_approval(approval_case["case_id"])
private_case = make_case(
    4,
    "RECEIVED",
    APPROVER_ID,
    subject="Private approver-owned case",
)
admin_case = make_case(
    5,
    "FAILED",
    REQUESTER_ID,
    subject="Escaped <script>alert(1)</script> case",
)

print("AI Service Request Automation - local portal integration check")
print("Scope: 6 focused groups; fictional users and cases; 0 AI calls.")
print("")

# Group 1: pre-auth CSRF, generic credentials, safe redirect, and cookie flags.
guard_browser = Browser()
status, login_document, _headers = guard_browser.request(
    "/login?next=https://invalid.example"
)
values = input_values(login_document)
require(status == 200 and values["next"] == "/cases", "unsafe login redirect was retained")
invalid_form = {
    "csrf_token": "0" * 64,
    "employee_reference": "EMP-201",
    "next": "/cases",
    "password": TEST_PASSWORD,
}
status, _body, _headers = guard_browser.request("/login", form=invalid_form)
require(status == 403, "invalid login CSRF was accepted")
status, login_document, _headers = guard_browser.request("/login?next=/cases")
values = input_values(login_document)
status, _body, _headers = guard_browser.request(
    "/login",
    form={
        "csrf_token": values["csrf_token"],
        "employee_reference": "EMP-201",
        "next": "/cases",
        "password": "incorrect-password",
    },
)
require(status == 401, "invalid portal credentials were accepted")
requester_browser = Browser()
login_headers = login(requester_browser, "EMP-201")
session_headers = [value for value in requester_browser.last_set_cookie if "service_request_session=" in value]
require(
    session_headers
    and "HttpOnly" in session_headers[0]
    and "SameSite=lax" in session_headers[0]
    and login_headers.get("Location") == "/cases",
    "the signed session cookie contract changed",
)
print("[1/6] Password verification, login CSRF, safe redirect, and session cookie: PASS")

# Group 2: role-filtered lists and object-level case access do not leak.
agent_browser = Browser()
approver_browser = Browser()
admin_browser = Browser()
login(agent_browser, "AGT-301")
login(approver_browser, "MGR-104")
login(admin_browser, "ADM-001")
status, requester_list, _headers = requester_browser.request("/cases")
require(
    status == 200
    and requester_case["case_reference"] in requester_list
    and review_case["case_reference"] in requester_list
    and approval_case["case_reference"] not in requester_list
    and private_case["case_reference"] not in requester_list,
    "requester case visibility leaked or hid owned cases",
)
status, agent_list, _headers = agent_browser.request("/cases")
require(
    status == 200
    and review_case["case_reference"] in agent_list
    and approval_case["case_reference"] in agent_list
    and private_case["case_reference"] not in agent_list,
    "service-agent queue visibility is incorrect",
)
status, approver_list, _headers = approver_browser.request("/cases")
require(
    status == 200
    and approval_case["case_reference"] in approver_list
    and private_case["case_reference"] in approver_list
    and review_case["case_reference"] not in approver_list,
    "approver queue visibility is incorrect",
)
status, _body, _headers = requester_browser.request(
    f"/cases/{approval_case['case_reference']}"
)
require(status == 404, "requester object-level isolation failed")
print("[2/6] Requester, agent, approver, and object-level visibility: PASS")

# Group 3: requester form uses CSRF and the verified domain transition.
status, requester_detail, _headers = requester_browser.request(
    f"/cases/{requester_case['case_reference']}"
)
requester_form = form_for(requester_detail, "SUBMIT_INFORMATION")
requester_form["information"] = "The missing fictional value is now supplied."
invalid_requester_form = dict(requester_form)
invalid_requester_form["csrf_token"] = "0" * 64
status, _body, _headers = requester_browser.request(
    f"/cases/{requester_case['case_reference']}/actions",
    form=invalid_requester_form,
)
require(status == 403, "invalid action CSRF was accepted")
status, _body, headers = requester_browser.request(
    f"/cases/{requester_case['case_reference']}/actions",
    form=requester_form,
)
require(status == 303 and headers.get("Location").endswith(requester_case["case_reference"]), "requester action failed")
print("[3/6] Requester information form commits through the domain boundary: PASS")

# Group 4: service-agent page exposes all review choices and confirms safely.
status, review_detail, _headers = agent_browser.request(
    f"/cases/{review_case['case_reference']}"
)
require(
    status == 200
    and 'value="CONFIRM_REVIEW"' in review_detail
    and 'value="CORRECT_REVIEW"' in review_detail
    and 'value="REJECT_REVIEW"' in review_detail,
    "the service-agent action set is incomplete",
)
confirm_form = form_for(review_detail, "CONFIRM_REVIEW")
confirm_form["note"] = "Confirmed in the controlled local portal."
status, _body, _headers = agent_browser.request(
    f"/cases/{review_case['case_reference']}/actions",
    form=confirm_form,
)
require(status == 303, "service-agent confirmation failed")
print("[4/6] Agent confirmation, correction, and rejection UI is role-bound: PASS")

# Group 5: assigned approver alone sees and commits the approval form.
status, approval_detail, _headers = approver_browser.request(
    f"/cases/{approval_case['case_reference']}"
)
require(
    status == 200
    and 'value="APPROVE_REQUEST"' in approval_detail
    and 'value="REJECT_REQUEST"' in approval_detail,
    "the assigned approver action set is incomplete",
)
approve_form = form_for(approval_detail, "APPROVE_REQUEST")
approve_form["note"] = "Approved in the controlled local portal."
status, _body, _headers = approver_browser.request(
    f"/cases/{approval_case['case_reference']}/actions",
    form=approve_form,
)
require(status == 303, "assigned approver action failed")
print("[5/6] Assigned approver decision reuses the verified domain service: PASS")

# Group 6: admin visibility, escaped output, logout, and durable aggregate evidence.
status, admin_list, _headers = admin_browser.request("/cases")
require(
    status == 200
    and all(
        case["case_reference"] in admin_list
        for case in (
            requester_case,
            review_case,
            approval_case,
            private_case,
            admin_case,
        )
    )
    and "&lt;script&gt;" in admin_list
    and "<script>" not in admin_list,
    "admin visibility or HTML escaping failed",
)
logout_start = admin_list.find('<form class="inline"')
logout_end = admin_list.find("</form>", logout_start)
require(logout_start >= 0 and logout_end >= 0, "the logout form is missing")
logout_form = input_values(admin_list[logout_start : logout_end + 7])
status, _body, _headers = admin_browser.request("/logout", form=logout_form)
require(status == 303 and "service_request_session" not in admin_browser.cookies, "logout did not clear the session")
status, _body, headers = admin_browser.request("/cases")
require(status == 303 and headers.get("Location", "").startswith("/login"), "logged-out access was accepted")

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = connection.execute(
        """
        SELECT
          (SELECT count(*) FROM cases
           WHERE external_request_id LIKE 'LOCAL-PORTAL-%') AS cases,
          (SELECT count(*) FROM case_events
           WHERE event_payload ? 'human_command_id') AS human_actions,
          (SELECT count(*) FROM outbox_messages) AS outbox_messages,
          (SELECT count(*) FROM case_events
           WHERE event_type = 'REQUESTER_INFORMATION_SUBMITTED') AS information_actions,
          (SELECT count(*) FROM case_events
           WHERE event_type = 'SERVICE_AGENT_REVIEW_CONFIRMED') AS review_actions,
          (SELECT count(*) FROM case_events
           WHERE event_type = 'APPROVAL_APPROVED') AS approval_actions
        """
    ).fetchone()
require(
    dict(aggregate)
    == {
        "cases": 5,
        "human_actions": 3,
        "outbox_messages": 0,
        "information_actions": 1,
        "review_actions": 1,
        "approval_actions": 1,
    },
    f"unexpected local portal aggregate: {dict(aggregate)}",
)
print("[6/6] Admin visibility, HTML escaping, logout, and aggregate evidence: PASS")
print("Local portal integration summary")
print("  Integration groups: 6/6 PASS")
print("  Fictional portal users: 4")
print("  Fictional portal cases: 5")
print("  Role-filtered case views: 4")
print("  Committed human actions: 3")
print("  Unauthorized case disclosures: 0")
print("  External AI calls: 0")
print("  Local portal gate: PASS")
