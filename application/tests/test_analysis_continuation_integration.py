"""Focused n8n post-response continuation integration check."""

from __future__ import annotations

import concurrent.futures
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

import psycopg
from psycopg.rows import dict_row

from workflow_start import WorkflowStartClient, process_one_workflow_start


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
WORKFLOW_FILE = Path(os.environ["N8N_WORKFLOW_FILE"])


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def http_json(
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5,
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw_body = error.read(2_000)
    try:
        parsed = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssertionError(f"HTTP {status} response was not bounded JSON") from error
    require(isinstance(parsed, dict), "HTTP response was not a JSON object")
    return status, parsed


def create_case(
    external_request_id: str,
    subject: str,
    message: str,
) -> dict[str, Any]:
    status, response = http_json(
        f"{API_URL}/api/v1/requests",
        body={
            "external_request_id": external_request_id,
            "requester_reference": "EMP-201",
            "subject": subject,
            "message": message,
            "attachments": [],
            "received_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        },
        headers={
            "Authorization": f"Bearer {INTAKE_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    require(status == 201, f"case creation failed: HTTP {status} {response}")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT
                cases.case_id,
                cases.case_reference,
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
    require(row is not None, "the created continuation case is missing")
    return dict(row)


def analysis_evidence(case_id: Any) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        case = connection.execute(
            """
            SELECT current_state, version
            FROM cases
            WHERE case_id = %s
            """,
            (case_id,),
        ).fetchone()
        starts = connection.execute(
            """
            SELECT event_id, sequence_number
            FROM case_events
            WHERE case_id = %s AND event_type = 'ANALYSIS_STARTED'
            ORDER BY sequence_number
            """,
            (case_id,),
        ).fetchall()
        attempts = connection.execute(
            """
            SELECT analysis_run_id, status, attempt_number, completed_at
            FROM ai_analysis_runs
            WHERE case_id = %s
            ORDER BY attempt_number
            """,
            (case_id,),
        ).fetchall()
        validations = connection.execute(
            """
            SELECT validation_runs.overall_decision
            FROM validation_runs
            JOIN ai_analysis_runs USING (analysis_run_id)
            WHERE ai_analysis_runs.case_id = %s
            ORDER BY validation_runs.created_at
            """,
            (case_id,),
        ).fetchall()
        outbox = connection.execute(
            """
            SELECT status, attempt_count
            FROM outbox_messages
            WHERE case_id = %s AND message_type = 'WORKFLOW_START'
            """,
            (case_id,),
        ).fetchone()
    require(case is not None and outbox is not None, "continuation evidence is missing")
    return {
        "case": dict(case),
        "starts": [dict(row) for row in starts],
        "attempts": [dict(row) for row in attempts],
        "validations": [dict(row) for row in validations],
        "outbox": dict(outbox),
    }


def wait_for_terminal_analysis(case_id: Any, timeout_seconds: float = 8) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        evidence = analysis_evidence(case_id)
        attempts = evidence["attempts"]
        if attempts and attempts[-1]["completed_at"] is not None:
            return evidence
        time.sleep(0.1)
    raise AssertionError("the post-response analysis did not finish within 8 seconds")


def replay_workflow_start(case: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return http_json(
        N8N_URL,
        body=case["payload"],
        headers={
            "Authorization": f"Bearer {N8N_TOKEN}",
            "Content-Type": "application/json",
            "Idempotency-Key": case["idempotency_key"],
        },
        timeout=5,
    )


def workflow_contract() -> dict[str, Any]:
    document = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    require(isinstance(document, dict), "the n8n workflow is not a JSON object")
    return document


print("AI Service Request Automation - analysis-continuation integration check")
print("Scope: 5 focused groups; fictional data; fixture provider; 0 Ollama calls.")
print("")

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    baseline = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM policy_documents) AS policies,
            (SELECT count(*) FROM policy_chunks) AS chunks,
            (SELECT count(*) FROM approvals) AS approvals,
            (SELECT count(*) FROM outbox_messages
             WHERE message_type = 'DOWNSTREAM_ACTION') AS downstream,
            (SELECT count(*) FROM outbox_messages
             WHERE message_type = 'REQUESTER_NOTIFICATION') AS notifications
        """
    ).fetchone()

client = WorkflowStartClient(N8N_URL, N8N_TOKEN)
delayed_case = create_case(
    "CONTINUATION-DELAYED-0001",
    "Delayed continuation incident",
    "WMS is unavailable. Impact high and urgency high.",
)
execution = process_one_workflow_start(DATABASE_URL, client, retry_delay_seconds=0)
require(
    execution is not None
    and execution.outcome == "SUCCESS"
    and execution.final_status == "SENT",
    f"the workflow-start acknowledgement failed: {execution!r}",
)
immediate = analysis_evidence(delayed_case["case_id"])
require(
    immediate["outbox"]["status"] == "SENT"
    and not any(row["completed_at"] is not None for row in immediate["attempts"]),
    "analysis completed before the workflow-start acknowledgement was durable",
)
completed = wait_for_terminal_analysis(delayed_case["case_id"])
require(
    completed["attempts"][0]["status"] == "COMPLETED"
    and completed["validations"] == [{"overall_decision": "READY"}],
    "the delayed fixture did not finalize through deterministic validation",
)
print("[1/5] Acknowledgement returns before delayed fixture analysis: PASS")

workflow = workflow_contract()
nodes = {node["name"]: node for node in workflow["nodes"]}
analysis_node = nodes.get("Run Primary Analysis")
require(analysis_node is not None, "the analysis continuation node is missing")
body_expression = analysis_node["parameters"].get("jsonBody", "")
for field_name in (
    "schema_version",
    "case_reference",
    "expected_case_version",
    "workflow_start_reference",
):
    require(field_name in body_expression, f"the analysis body omitted {field_name}")
require(
    analysis_node["credentials"]["httpBearerAuth"]["id"]
    == "primary-workflow-api-auth-v1"
    and analysis_node["parameters"]["options"]["timeout"] == 200_000
    and not analysis_node.get("retryOnFail", False),
    "the continuation authentication, timeout, or no-retry guard changed",
)
require(
    len(completed["starts"]) == 1
    and completed["starts"][0]["sequence_number"] == 2
    and len(completed["attempts"]) == 1
    and len(completed["validations"]) == 1,
    "the stable 4-field command did not create singular durable evidence",
)
print("[2/5] Authenticated 4-field command creates 1 durable result: PASS")

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    replay_results = list(executor.map(lambda _item: replay_workflow_start(delayed_case), range(2)))
require(
    all(
        status == 200
        and body.get("status") == "ACCEPTED"
        and body.get("idempotent_replay") is True
        for status, body in replay_results
    ),
    f"concurrent workflow-start replay failed: {replay_results}",
)
time.sleep(1)
replayed = analysis_evidence(delayed_case["case_id"])
require(
    len(replayed["starts"]) == 1
    and len(replayed["attempts"]) == 1
    and len(replayed["validations"]) == 1,
    "concurrent continuation replay duplicated provider or durable evidence",
)
print("[3/5] Concurrent continuation uses exact no-call replay: PASS")

retryable_case = create_case(
    "CONTINUATION-RETRYABLE-0001",
    "Retryable continuation incident",
    "WMS is unavailable. Impact high and urgency high.",
)
execution = process_one_workflow_start(DATABASE_URL, client, retry_delay_seconds=0)
require(
    execution is not None
    and execution.outcome == "SUCCESS"
    and execution.final_status == "SENT",
    "the retryable fixture did not preserve the start acknowledgement",
)
retryable = wait_for_terminal_analysis(retryable_case["case_id"])
require(
    retryable["outbox"]["status"] == "SENT"
    and retryable["case"] == {"current_state": "ANALYZING", "version": 2}
    and len(retryable["attempts"]) == 1
    and retryable["attempts"][0]["status"] == "FAILED"
    and retryable["attempts"][0]["attempt_number"] == 1
    and retryable["validations"] == [],
    "the retryable provider result triggered inline retry or changed start state",
)
print("RUNTIME_RETRYABLE_CLASSIFICATION: PASS")

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    final = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM cases
             WHERE external_request_id LIKE 'CONTINUATION-%') AS cases,
            (SELECT count(*) FROM case_events
             WHERE event_type = 'ANALYSIS_STARTED'
               AND case_id IN (
                   SELECT case_id FROM cases
                   WHERE external_request_id LIKE 'CONTINUATION-%'
               )) AS starts,
            (SELECT count(*) FROM ai_analysis_runs
             WHERE case_id IN (
                 SELECT case_id FROM cases
                 WHERE external_request_id LIKE 'CONTINUATION-%'
             )) AS attempts,
            (SELECT count(*) FROM validation_runs
             WHERE case_id IN (
                 SELECT case_id FROM cases
                 WHERE external_request_id LIKE 'CONTINUATION-%'
             )) AS validations,
            (SELECT count(*) FROM ai_analysis_runs
             WHERE completed_at IS NULL) AS unfinished,
            (SELECT count(*) FROM outbox_messages
             WHERE message_type = 'WORKFLOW_START'
               AND status <> 'SENT') AS unfinished_starts,
            (SELECT count(*) FROM policy_documents) AS policies,
            (SELECT count(*) FROM policy_chunks) AS chunks,
            (SELECT count(*) FROM approvals) AS approvals,
            (SELECT count(*) FROM outbox_messages
             WHERE message_type = 'DOWNSTREAM_ACTION') AS downstream,
            (SELECT count(*) FROM outbox_messages
             WHERE message_type = 'REQUESTER_NOTIFICATION') AS notifications
        """
    ).fetchone()

require(
    final["cases"] == 2
    and final["starts"] == 2
    and final["attempts"] == 2
    and final["validations"] == 1
    and final["unfinished"] == 0
    and final["unfinished_starts"] == 0,
    f"continuation aggregate evidence is inconsistent: {dict(final)}",
)
require(
    final["policies"] == baseline["policies"]
    and final["chunks"] == baseline["chunks"]
    and final["approvals"] == baseline["approvals"]
    and final["downstream"] == baseline["downstream"]
    and final["notifications"] == baseline["notifications"],
    "a deferred capability changed during continuation testing",
)
print("[5/5] Aggregate evidence and deferred-boundary isolation: PASS")
print("Analysis-continuation integration summary")
print("  Integration groups: 5/5 PASS")
print("  Fictional continuation cases: 2")
print("  Workflow-start acknowledgements: 2")
print("  Durable analysis attempts: 2")
print("  Deterministic validation records: 1")
print("  Duplicate analysis identities: 0")
print("  Unfinished analysis attempts: 0")
print("  External AI calls: 0")
print("  Analysis-continuation gate: PASS")
