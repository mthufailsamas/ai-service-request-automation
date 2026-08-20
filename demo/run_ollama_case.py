"""Run 1 idempotent fictional request through the accepted Ollama adapter."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from workflow_start import (
    WorkflowStartMessage,
    WorkflowStartResponse,
    claim_next_workflow_start,
    finalize_workflow_start,
)


API_URL = os.environ.get("DEMO_PRIMARY_API_URL", "").rstrip("/")
DATABASE_URL = os.environ.get("DEMO_DATABASE_URL", "")
INTAKE_TOKEN = os.environ.get("DEMO_INTAKE_TOKEN", "")
WORKFLOW_TOKEN = os.environ.get("DEMO_WORKFLOW_TOKEN", "")
EXTERNAL_REQUEST_ID = "GUIDED-DEMO-OLLAMA-01"
SUBJECT = "Guided Ollama incident"
MESSAGE = (
    "Sejak pukul 09.15 WMS tidak menerima pemindaian barang. "
    "Dampaknya tinggi untuk tim gudang dan urgensinya tinggi."
)


def require_configuration() -> None:
    values = (API_URL, DATABASE_URL, INTAKE_TOKEN, WORKFLOW_TOKEN)
    if not all(values):
        raise RuntimeError("The guided Ollama demo is not configured.")


def post_json(
    url: str, body: dict[str, Any], headers: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=200) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read())
        return error.code, payload


def require_status(
    label: str,
    status: int,
    payload: dict[str, Any],
    accepted: set[int],
) -> None:
    if status not in accepted:
        bounded = json.dumps(payload, separators=(",", ":"))[:800]
        raise RuntimeError(f"{label} failed with HTTP {status}: {bounded}")


def main() -> None:
    require_configuration()
    status, intake = post_json(
        f"{API_URL}/api/v1/requests",
        {
            "external_request_id": EXTERNAL_REQUEST_ID,
            "requester_reference": "EMP-201",
            "subject": SUBJECT,
            "message": MESSAGE,
            "attachments": [],
            "received_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        },
        {"Authorization": f"Bearer {INTAKE_TOKEN}"},
    )
    require_status("intake", status, intake, {200, 201})

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT cases.case_id, cases.case_reference,
                   message.outbox_message_id, message.idempotency_key,
                   message.payload, message.status, message.attempt_count,
                   message.max_attempts
            FROM cases
            JOIN outbox_messages AS message USING (case_id)
            WHERE cases.external_request_id = %s
              AND message.message_type = 'WORKFLOW_START'
            """,
            (EXTERNAL_REQUEST_ID,),
        ).fetchone()
        next_pending = connection.execute(
            """
            SELECT outbox_message_id
            FROM outbox_messages
            WHERE status = 'PENDING'
              AND message_type = 'WORKFLOW_START'
              AND destination = 'N8N_REQUEST_INTAKE'
              AND available_at <= now()
              AND attempt_count < max_attempts
            ORDER BY available_at, created_at
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise RuntimeError("The guided request did not create its workflow intent.")

    workflow_payload = row["payload"]
    claimed: WorkflowStartMessage | None = None
    if row["status"] == "PENDING":
        if (
            next_pending is None
            or next_pending["outbox_message_id"] != row["outbox_message_id"]
        ):
            raise RuntimeError("The reserved request is not the next safe claim.")
        claimed = claim_next_workflow_start(DATABASE_URL)
        if claimed is None or claimed.outbox_message_id != row["outbox_message_id"]:
            raise RuntimeError("The guided workflow intent was not the next safe claim.")
    elif row["status"] == "PROCESSING":
        claimed = WorkflowStartMessage(
            outbox_message_id=row["outbox_message_id"],
            idempotency_key=row["idempotency_key"],
            payload=workflow_payload,
            attempt_number=row["attempt_count"],
            max_attempts=row["max_attempts"],
        )
    elif row["status"] != "SENT":
        raise RuntimeError(
            f"The guided workflow intent has terminal status {row['status']}."
        )

    handoff_started_at = datetime.now(timezone.utc)
    status, started = post_json(
        f"{API_URL}/internal/v1/cases/{row['case_id']}/analysis-start",
        {
            "schema_version": workflow_payload["schema_version"],
            "case_reference": workflow_payload["case_reference"],
            "expected_case_version": workflow_payload["case_version"],
            "trigger_event": workflow_payload["trigger_event"],
        },
        {
            "Authorization": f"Bearer {WORKFLOW_TOKEN}",
            "Idempotency-Key": row["idempotency_key"],
        },
    )
    require_status("analysis start", status, started, {200})

    if claimed is not None:
        finalize_workflow_start(
            DATABASE_URL,
            claimed,
            WorkflowStartResponse(
                outcome="SUCCESS",
                http_status=status,
                downstream_reference=started["workflow_start_reference"],
                response_payload=started,
                error_code=None,
                error_message=None,
            ),
            started_at=handoff_started_at,
            finished_at=datetime.now(timezone.utc),
            retry_delay_seconds=0,
        )

    status, analyzed = post_json(
        f"{API_URL}/internal/v1/cases/{row['case_id']}/analysis",
        {
            "schema_version": "1",
            "case_reference": row["case_reference"],
            "expected_case_version": workflow_payload["case_version"] + 1,
            "workflow_start_reference": started["workflow_start_reference"],
        },
        {"Authorization": f"Bearer {WORKFLOW_TOKEN}"},
    )
    require_status("Ollama analysis", status, analyzed, {200})

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        result = connection.execute(
            """
            SELECT case_reference, request_type, ai_summary, current_state
            FROM cases WHERE case_id = %s
            """,
            (row["case_id"],),
        ).fetchone()
    if result is None:
        raise RuntimeError("The guided analysis result was not stored.")

    print("AI Service Request Automation - guided Ollama case")
    print("Scope: 1 fictional request; local qwen3:4b-instruct; no n8n call.")
    print(f"Case: {result['case_reference']}")
    print(f"Provider called: {'YES' if analyzed['provider_called'] else 'NO (exact replay)'}")
    print(f"Request type: {result['request_type'] or 'Pending review'}")
    print(f"State: {result['current_state']}")
    print(f"AI summary: {result['ai_summary'] or 'No accepted summary'}")
    print("Open http://127.0.0.1:8000/cases after requester login to inspect it.")


if __name__ == "__main__":
    main()
