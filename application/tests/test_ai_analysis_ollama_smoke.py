"""Two-case local Ollama smoke check for the accepted analysis boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


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
PRIMARY_WORKFLOW_TOKEN = os.environ["PRIMARY_WORKFLOW_TOKEN"]
MODEL_NAME = os.environ["AI_ANALYSIS_MODEL_NAME"]
MODEL_IDENTIFIER = os.environ["AI_ANALYSIS_MODEL_IDENTIFIER"]
CASES_FILE = Path(os.environ["AI_ANALYSIS_BENCHMARK_CASES_FILE"])

EMPLOYEE_REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
FIELD_NAMES = {
    "policy_topic",
    "question",
    "affected_service",
    "incident_description",
    "impact",
    "urgency",
    "target_system",
    "requested_access_level",
    "business_reason",
    "approver_id",
    "record_reference",
    "requested_changes",
    "case_reference",
}
SMOKE_CASES = (
    ("policy-english-clear", "English"),
    ("access-indonesian-missing-approver", "Indonesian"),
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def http_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {PRIMARY_WORKFLOW_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=210) as response:
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


def load_smoke_cases() -> list[tuple[dict[str, Any], str]]:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    require(isinstance(cases, list), "the accepted benchmark fixture is invalid")
    by_id = {
        item.get("case_id"): item
        for item in cases
        if isinstance(item, dict) and isinstance(item.get("case_id"), str)
    }
    selected: list[tuple[dict[str, Any], str]] = []
    for case_id, language in SMOKE_CASES:
        require(case_id in by_id, f"accepted benchmark case {case_id} is missing")
        case = by_id[case_id]
        require(
            set(case) == {
                "case_id",
                "subject",
                "message",
                "expected_request_type",
                "expected_keywords",
                "must_be_null",
                "expected_missing_fields",
            },
            f"accepted benchmark case {case_id} changed shape",
        )
        selected.append((case, language))
    return selected


def create_analysis_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id_label = case["case_id"].upper()
    external_request_id = f"OLLAMA-SMOKE-{case_id_label}"
    idempotency_key = hashlib.sha256(external_request_id.encode()).hexdigest()
    fingerprint = hashlib.sha256(
        f"{case['subject']}|{case['message']}".encode("utf-8")
    ).hexdigest()
    workflow_key = hashlib.sha256(
        f"WORKFLOW|{external_request_id}".encode()
    ).hexdigest()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        reference_number = connection.execute(
            "SELECT nextval('case_reference_sequence') AS number"
        ).fetchone()["number"]
        case_reference = f"CASE-2026-{reference_number:04d}"
        created = connection.execute(
            """
            INSERT INTO cases (
                case_reference,
                source_channel,
                external_request_id,
                idempotency_key,
                content_fingerprint,
                requester_id,
                subject,
                original_message,
                attachment_metadata,
                current_state,
                version,
                received_at
            )
            VALUES (
                %s, 'WEBHOOK', %s, %s, %s, %s, %s, %s,
                '[]'::jsonb, 'ANALYZING', 2, %s
            )
            RETURNING case_id
            """,
            (
                case_reference,
                external_request_id,
                idempotency_key,
                fingerprint,
                EMPLOYEE_REQUESTER_ID,
                case["subject"],
                case["message"],
                datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        ).fetchone()
        case_id = created["case_id"]
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state,
                event_type, actor_type, reason, event_payload
            )
            VALUES (
                %s, 1, NULL, 'RECEIVED', 'CASE_RECEIVED', 'INTEGRATION',
                'Controlled Ollama smoke fixture.', %s
            )
            """,
            (case_id, Jsonb({"source": "OLLAMA_SMOKE"})),
        )
        started_event = connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state,
                event_type, actor_type, reason, event_payload
            )
            VALUES (
                %s, 2, 'RECEIVED', 'ANALYZING', 'ANALYSIS_STARTED',
                'INTEGRATION', 'Controlled Ollama smoke workflow start.', %s
            )
            RETURNING event_id
            """,
            (
                case_id,
                Jsonb(
                    {
                        "schema_version": "1",
                        "trigger_event": "CASE_RECEIVED",
                        "workflow_start_idempotency_key": workflow_key,
                    }
                ),
            ),
        ).fetchone()

    return {
        "case_id": case_id,
        "case_reference": case_reference,
        "expected_case_version": 2,
        "workflow_start_reference": f"WFSTART-{started_event['event_id']}",
    }


def analysis_body(created: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "case_reference": created["case_reference"],
        "expected_case_version": created["expected_case_version"],
        "workflow_start_reference": created["workflow_start_reference"],
    }


def verify_durable_result(analysis_run_id: str) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT
                analysis_run_id,
                model_name,
                model_identifier,
                proposal,
                evidence,
                status,
                wall_time_ms,
                input_tokens,
                output_tokens,
                completed_at,
                (
                    SELECT count(*)
                    FROM validation_runs
                    WHERE validation_runs.analysis_run_id =
                          ai_analysis_runs.analysis_run_id
                ) AS validation_count,
                (
                    SELECT overall_decision
                    FROM validation_runs
                    WHERE validation_runs.analysis_run_id =
                          ai_analysis_runs.analysis_run_id
                    ORDER BY created_at
                    LIMIT 1
                ) AS validation_decision,
                (
                    SELECT reason
                    FROM validation_runs
                    WHERE validation_runs.analysis_run_id =
                          ai_analysis_runs.analysis_run_id
                    ORDER BY created_at
                    LIMIT 1
                ) AS validation_reason
            FROM ai_analysis_runs
            WHERE analysis_run_id = %s
            """,
            (analysis_run_id,),
        ).fetchone()

    require(row is not None, "the durable Ollama analysis row is missing")
    durable = dict(row)
    require(durable["model_name"] == MODEL_NAME, "the stored model name changed")
    require(
        durable["model_identifier"] == MODEL_IDENTIFIER,
        "the stored model identifier changed",
    )
    require(
        durable["status"] in {"COMPLETED", "INVALID_OUTPUT"},
        f"the provider output did not finalize safely: {durable['status']}",
    )
    require(durable["completed_at"] is not None, "the analysis remained unfinished")
    require(durable["wall_time_ms"] > 0, "the model wall time was not recorded")
    require(durable["input_tokens"] > 0, "the model input tokens were not recorded")
    require(durable["output_tokens"] > 0, "the model output tokens were not recorded")
    require(durable["validation_count"] == 1, "validation evidence was not singular")
    require(
        durable["validation_decision"]
        in {"READY", "NEEDS_INFORMATION", "NEEDS_REVIEW", "REJECTED"},
        "the deterministic validation decision was invalid",
    )
    require(
        isinstance(durable["validation_reason"], str)
        and bool(durable["validation_reason"].strip()),
        "the deterministic validation reason was missing",
    )
    validation_context = (
        f"status={durable['status']}; "
        f"decision={durable['validation_decision']}; "
        f"reason={durable['validation_reason'][:180]}"
    )

    proposal = durable["proposal"]
    evidence = durable["evidence"]
    require(
        isinstance(proposal, dict),
        f"the stored proposal was not an object ({validation_context})",
    )
    require(
        set(proposal) == {"request_type", "summary", "fields"},
        f"the stored proposal shape changed ({validation_context})",
    )
    require(
        isinstance(proposal["fields"], dict)
        and set(proposal["fields"]) == FIELD_NAMES,
        f"the stored field contract changed ({validation_context})",
    )
    require(isinstance(evidence, list) and len(evidence) <= 13, "evidence was invalid")
    require(
        all(
            isinstance(item, dict)
            and set(item) == {"field", "quote"}
            and item["field"] in FIELD_NAMES
            and isinstance(item["quote"], str)
            and 0 < len(item["quote"]) <= 500
            for item in evidence
        ),
        "the stored evidence shape changed",
    )
    serialized_size = len(
        json.dumps(
            {**proposal, "evidence": evidence},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    require(serialized_size <= 32 * 1024, "the stored proposal exceeded 32 KiB")
    return durable


selected_cases = load_smoke_cases()
durable_results: list[dict[str, Any]] = []

for position, (benchmark_case, language) in enumerate(selected_cases, start=1):
    created_case = create_analysis_case(benchmark_case)
    endpoint = f"{API_URL}/internal/v1/cases/{created_case['case_id']}/analysis"
    request_body = analysis_body(created_case)

    status, first = http_json(endpoint, request_body)
    require(status == 200, f"{benchmark_case['case_id']} returned HTTP {status}: {first}")
    require(first.get("provider_called") is True, "the real provider was not called")
    require(first.get("idempotent_replay") is False, "the first call was a replay")
    require(first.get("attempt_number") == 1, "the first attempt number changed")
    durable = verify_durable_result(first.get("analysis_run_id", ""))
    durable_results.append(durable)

    replay_status, replay = http_json(endpoint, request_body)
    require(replay_status == 200, "the exact replay did not return HTTP 200")
    require(replay.get("idempotent_replay") is True, "exact replay was not detected")
    require(replay.get("provider_called") is False, "exact replay called Ollama again")
    require(
        replay.get("analysis_run_id") == first.get("analysis_run_id"),
        "exact replay changed the durable analysis identity",
    )
    print(
        f"[{position}/2] {language} adapter, bounded proposal, persistence, and replay: PASS"
    )

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = connection.execute(
        """
        SELECT
            count(*) AS attempts,
            count(*) FILTER (
                WHERE status IN ('COMPLETED', 'INVALID_OUTPUT')
            ) AS finalized_outputs,
            count(*) FILTER (WHERE completed_at IS NULL) AS unfinished,
            count(DISTINCT case_id) AS cases,
            count(DISTINCT (
                case_id,
                input_sha256,
                prompt_contract_version,
                model_identifier,
                attempt_number
            )) AS identities
        FROM ai_analysis_runs
        WHERE model_identifier = %s
        """,
        (MODEL_IDENTIFIER,),
    ).fetchone()
    validations = connection.execute(
        """
        SELECT count(*) AS count
        FROM validation_runs
        JOIN ai_analysis_runs USING (analysis_run_id)
        WHERE ai_analysis_runs.model_identifier = %s
        """,
        (MODEL_IDENTIFIER,),
    ).fetchone()["count"]

require(aggregate["attempts"] == 2, "the smoke check did not persist exactly 2 attempts")
require(
    aggregate["finalized_outputs"] == 2,
    "a provider output did not finalize through deterministic validation",
)
require(aggregate["unfinished"] == 0, "a smoke attempt remained unfinished")
require(aggregate["cases"] == 2, "the smoke check did not use exactly 2 cases")
require(aggregate["identities"] == 2, "a duplicate attempt identity was stored")
require(validations == 2, "the smoke check did not persist exactly 2 validations")

outcome_counts: dict[str, int] = {}
for durable in durable_results:
    decision = durable["validation_decision"]
    outcome_counts[decision] = outcome_counts.get(decision, 0) + 1
outcome_summary = ", ".join(
    f"{decision}={outcome_counts[decision]}" for decision in sorted(outcome_counts)
)

print("AI-analysis Ollama smoke summary")
print("  Existing benchmark cases: 2/2 PASS")
print("  Languages: 1 English, 1 Indonesian")
print("  Local Ollama model calls: 2")
print("  Durable structured attempts: 2")
print(f"  Deterministic outcomes: {outcome_summary}")
print("  Exact replays without a model call: 2")
print("  Unfinished attempts: 0")
print("  Ollama adapter smoke gate: PASS")
