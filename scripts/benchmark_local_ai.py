"""Evaluate the preferred local model before the application is built.

This script calls a local Ollama server. It never downloads a model and never
uses a paid API. The benchmark checks whether the selected model can return the
structured service-request proposal required by the project.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = PROJECT_ROOT / "evaluation" / "model_suitability_cases.json"
OUTPUT_DIRECTORY = PROJECT_ROOT / "output" / "model_suitability"
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

MODEL_NAME = os.environ.get("AI_AUTOMATION_MODEL", "qwen3:4b-instruct")
BENCHMARK_CONTRACT_VERSION = "2"

REQUEST_TYPES = (
    "policy_question",
    "incident_report",
    "access_request",
    "data_change_request",
    "status_request",
)

FIELD_NAMES = (
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
)

REQUIRED_FIELDS = {
    "policy_question": ("policy_topic", "question"),
    "incident_report": (
        "affected_service",
        "incident_description",
        "impact",
        "urgency",
    ),
    "access_request": (
        "target_system",
        "requested_access_level",
        "business_reason",
        "approver_id",
    ),
    "data_change_request": (
        "target_system",
        "record_reference",
        "requested_changes",
        "business_reason",
        "approver_id",
    ),
    "status_request": ("case_reference",),
}


def nullable_string_schema() -> dict[str, Any]:
    """Return the JSON Schema used for a string that may be absent."""

    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "request_type": {"type": "string", "enum": list(REQUEST_TYPES)},
        "summary": {"type": "string"},
        "fields": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                field_name: nullable_string_schema() for field_name in FIELD_NAMES
            },
            "required": list(FIELD_NAMES),
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string", "enum": list(FIELD_NAMES)},
                    "quote": {"type": "string"},
                },
                "required": ["field", "quote"],
            },
        },
    },
    "required": [
        "request_type",
        "summary",
        "fields",
        "evidence",
    ],
}


SYSTEM_PROMPT = """You analyze internal service requests.

Return only the JSON object required by the supplied schema.

Allowed request types and required fields:
- policy_question: policy_topic, question
- incident_report: affected_service, incident_description, impact, urgency
- access_request: target_system, requested_access_level, business_reason, approver_id
- data_change_request: target_system, record_reference, requested_changes, business_reason, approver_id
- status_request: case_reference

Rules:
1. Extract only information explicitly present in the subject or message.
2. Use null when a field is absent. Never invent an identifier or default value.
3. Keep fields unrelated to the selected request type null.
4. affected_service is the named business application or service experiencing
   the failure. Prefer it over a device or tool when both are mentioned.
5. business_reason is an explicit justification or supporting basis introduced
   by wording such as because, so that, based on, karena, agar, or berdasarkan.
6. Evidence quotes must be exact text spans copied from the subject or message.
7. Keep the summary short and operational.
"""


def derive_missing_fields(request_type: Any, fields: Any) -> list[str]:
    """Find absent required fields with deterministic project rules."""

    if request_type not in REQUIRED_FIELDS or not isinstance(fields, dict):
        return []

    missing_fields: list[str] = []
    for field_name in REQUIRED_FIELDS[request_type]:
        value = fields.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing_fields.append(field_name)

    return missing_fields


def load_cases() -> list[dict[str, Any]]:
    """Load the fixed benchmark cases from disk."""

    with CASES_FILE.open("r", encoding="utf-8") as file_handle:
        cases = json.load(file_handle)

    if not isinstance(cases, list) or not cases:
        raise ValueError("The suitability case file must contain a non-empty list.")

    return cases


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Send one JSON request to the local Ollama API."""

    request_data = None
    headers: dict[str, str] = {}
    if payload is not None:
        request_data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url=f"{OLLAMA_BASE_URL}{path}",
        data=request_data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def installed_model_names() -> set[str]:
    """Return the model names already available in local Ollama."""

    response = request_json("GET", "/api/tags", timeout_seconds=10)
    return {
        model["name"]
        for model in response.get("models", [])
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    }


def model_is_installed(model_name: str, installed_names: set[str]) -> bool:
    """Accept either an exact tag or the same tag with Ollama's latest suffix."""

    return model_name in installed_names or f"{model_name}:latest" in installed_names


def stop_model(model_name: str) -> None:
    """Unload a model before its cold-start measurement."""

    subprocess.run(
        ["ollama", "stop", model_name],
        check=False,
        capture_output=True,
        text=True,
    )


def ollama_process_snapshot() -> str:
    """Capture Ollama's own processor and memory placement summary."""

    result = subprocess.run(
        ["ollama", "ps"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def analyze_case(model_name: str, case: dict[str, Any]) -> dict[str, Any]:
    """Ask one local model to analyze one fixed service request."""

    user_message = f"Subject: {case['subject']}\nMessage: {case['message']}"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "format": RESPONSE_SCHEMA,
        "think": False,
        "keep_alive": "10m",
        "options": {
            "temperature": 0,
            "num_ctx": 4096,
            "num_predict": 512,
        },
    }

    started_at = time.perf_counter()
    response = request_json("POST", "/api/chat", payload)
    wall_time_seconds = time.perf_counter() - started_at

    message = response.get("message", {})
    content = message.get("content", "") if isinstance(message, dict) else ""
    proposal = json.loads(content)
    if isinstance(proposal, dict):
        proposal["missing_fields"] = derive_missing_fields(
            proposal.get("request_type"), proposal.get("fields")
        )

    return {
        "proposal": proposal,
        "wall_time_seconds": wall_time_seconds,
        "total_duration_seconds": response.get("total_duration", 0) / 1_000_000_000,
        "load_duration_seconds": response.get("load_duration", 0) / 1_000_000_000,
        "prompt_tokens": response.get("prompt_eval_count", 0),
        "output_tokens": response.get("eval_count", 0),
        "output_duration_seconds": response.get("eval_duration", 0)
        / 1_000_000_000,
    }


def normalized_text(value: Any) -> str:
    """Normalize human text for a conservative keyword comparison."""

    if not isinstance(value, str):
        return ""
    lowered = value.casefold()
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(without_punctuation.split())


def keywords_match(value: Any, expected_keywords: list[str]) -> bool:
    """Check that every expected keyword is present in an extracted value."""

    normalized_value = normalized_text(value)
    return all(normalized_text(keyword) in normalized_value for keyword in expected_keywords)


def response_shape_is_valid(proposal: Any) -> bool:
    """Perform the project-side checks that matter after JSON Schema output."""

    if not isinstance(proposal, dict):
        return False
    if proposal.get("request_type") not in REQUEST_TYPES:
        return False
    if not isinstance(proposal.get("summary"), str):
        return False

    fields = proposal.get("fields")
    if not isinstance(fields, dict) or set(fields) != set(FIELD_NAMES):
        return False
    if any(value is not None and not isinstance(value, str) for value in fields.values()):
        return False

    missing_fields = proposal.get("missing_fields")
    if not isinstance(missing_fields, list):
        return False
    if any(field not in FIELD_NAMES for field in missing_fields):
        return False

    evidence_items = proposal.get("evidence")
    if not isinstance(evidence_items, list):
        return False
    for evidence_item in evidence_items:
        if not isinstance(evidence_item, dict):
            return False
        if evidence_item.get("field") not in FIELD_NAMES:
            return False
        if not isinstance(evidence_item.get("quote"), str):
            return False

    return True


def score_case(case: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """Compare one structured proposal with its fixed expected behavior."""

    shape_valid = response_shape_is_valid(proposal)
    if not shape_valid:
        return {
            "shape_valid": False,
            "classification_match": False,
            "field_matches": 0,
            "field_total": 0,
            "missing_fields_match": False,
            "fabricated_default_count": 0,
            "evidence_valid_count": 0,
            "evidence_total": 0,
            "evidence_coverage_count": 0,
            "evidence_coverage_total": 0,
        }

    fields = proposal["fields"]
    expected_keywords = case["expected_keywords"]
    must_be_null = case["must_be_null"]

    field_matches = sum(
        keywords_match(fields.get(field_name), keywords)
        for field_name, keywords in expected_keywords.items()
    )
    null_matches = sum(not fields.get(field_name) for field_name in must_be_null)
    fabricated_default_count = sum(
        bool(fields.get(field_name)) for field_name in must_be_null
    )

    source_text = normalized_text(f"{case['subject']} {case['message']}")
    evidence_items = proposal["evidence"]
    evidence_valid_count = sum(
        bool(normalized_text(item["quote"]))
        and normalized_text(item["quote"]) in source_text
        for item in evidence_items
    )
    evidence_fields = {item["field"] for item in evidence_items}
    expected_evidence_fields = set(expected_keywords)

    return {
        "shape_valid": True,
        "classification_match": proposal["request_type"]
        == case["expected_request_type"],
        "field_matches": field_matches + null_matches,
        "field_total": len(expected_keywords) + len(must_be_null),
        "missing_fields_match": set(proposal["missing_fields"])
        == set(case["expected_missing_fields"]),
        "fabricated_default_count": fabricated_default_count,
        "evidence_valid_count": evidence_valid_count,
        "evidence_total": len(evidence_items),
        "evidence_coverage_count": len(evidence_fields & expected_evidence_fields),
        "evidence_coverage_total": len(expected_evidence_fields),
    }


def safe_ratio(numerator: int, denominator: int) -> float:
    """Return a ratio while keeping an empty denominator explicit."""

    return numerator / denominator if denominator else 0.0


def benchmark_model(
    model_name: str,
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run all suitability cases against one model and summarize the result."""

    print(f"\nBenchmarking {model_name}")
    stop_model(model_name)
    case_results: list[dict[str, Any]] = []

    for position, case in enumerate(cases, start=1):
        result: dict[str, Any] = {
            "case_id": case["case_id"],
            "status": "FAILED",
        }
        try:
            model_result = analyze_case(model_name, case)
            proposal = model_result.pop("proposal")
            score = score_case(case, proposal)
            result.update(model_result)
            result.update(score)
            result["proposal"] = proposal
            result["status"] = "COMPLETED"
            print(
                f"[{position:02d}/{len(cases):02d}] "
                f"{case['case_id']}  "
                f"type={'PASS' if score['classification_match'] else 'CHECK'}  "
                f"fields={score['field_matches']}/{score['field_total']}  "
                f"latency={model_result['wall_time_seconds']:.2f}s"
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            result["error"] = f"Invalid model output: {error}"
            print(f"[{position:02d}/{len(cases):02d}] {case['case_id']}  FAILED")
        except (urllib.error.URLError, TimeoutError) as error:
            result["error"] = f"Ollama request failed: {error}"
            print(f"[{position:02d}/{len(cases):02d}] {case['case_id']}  FAILED")

        case_results.append(result)

    completed = [result for result in case_results if result["status"] == "COMPLETED"]
    shape_valid_count = sum(result.get("shape_valid", False) for result in completed)
    classification_matches = sum(
        result.get("classification_match", False) for result in completed
    )
    field_matches = sum(result.get("field_matches", 0) for result in completed)
    field_total = sum(result.get("field_total", 0) for result in completed)
    missing_matches = sum(
        result.get("missing_fields_match", False) for result in completed
    )
    fabricated_defaults = sum(
        result.get("fabricated_default_count", 0) for result in completed
    )
    evidence_valid = sum(
        result.get("evidence_valid_count", 0) for result in completed
    )
    evidence_total = sum(result.get("evidence_total", 0) for result in completed)
    evidence_coverage = sum(
        result.get("evidence_coverage_count", 0) for result in completed
    )
    evidence_coverage_total = sum(
        result.get("evidence_coverage_total", 0) for result in completed
    )

    latencies = [result["wall_time_seconds"] for result in completed]
    warm_latencies = latencies[1:] if len(latencies) > 1 else latencies

    summary = {
        "model": model_name,
        "completed_cases": len(completed),
        "total_cases": len(cases),
        "schema_valid_rate": safe_ratio(shape_valid_count, len(cases)),
        "classification_accuracy": safe_ratio(classification_matches, len(cases)),
        "field_accuracy": safe_ratio(field_matches, field_total),
        "missing_fields_accuracy": safe_ratio(missing_matches, len(cases)),
        "fabricated_default_count": fabricated_defaults,
        "evidence_validity": safe_ratio(evidence_valid, evidence_total),
        "evidence_coverage": safe_ratio(evidence_coverage, evidence_coverage_total),
        "cold_start_seconds": latencies[0] if latencies else None,
        "median_warm_seconds": statistics.median(warm_latencies)
        if warm_latencies
        else None,
        "max_warm_seconds": max(warm_latencies) if warm_latencies else None,
        "ollama_process_snapshot": ollama_process_snapshot(),
    }
    summary["quality_gate_passed"] = (
        summary["schema_valid_rate"] == 1.0
        and summary["classification_accuracy"] >= 0.9
        and summary["field_accuracy"] >= 0.9
        and summary["missing_fields_accuracy"] >= 0.9
        and summary["fabricated_default_count"] == 0
        and summary["evidence_validity"] >= 0.9
        and summary["evidence_coverage"] >= 0.8
    )

    return {
        "summary": summary,
        "cases": case_results,
    }


def print_model_summary(result: dict[str, Any]) -> None:
    """Print the small set of measures needed for the model decision."""

    summary = result["summary"]
    print(f"\n{summary['model']} summary")
    print(f"  Completed: {summary['completed_cases']}/{summary['total_cases']}")
    print(f"  Schema valid: {summary['schema_valid_rate']:.1%}")
    print(f"  Classification: {summary['classification_accuracy']:.1%}")
    print(f"  Required fields: {summary['field_accuracy']:.1%}")
    print(f"  Missing fields: {summary['missing_fields_accuracy']:.1%}")
    print(f"  Evidence validity: {summary['evidence_validity']:.1%}")
    print(f"  Evidence coverage: {summary['evidence_coverage']:.1%}")
    print(f"  Fabricated defaults: {summary['fabricated_default_count']}")
    print(f"  Cold start: {summary['cold_start_seconds']}")
    print(f"  Median warm latency: {summary['median_warm_seconds']}")
    print(f"  Quality gate: {'PASS' if summary['quality_gate_passed'] else 'CHECK'}")
    print("  Ollama processor placement:")
    print(summary["ollama_process_snapshot"] or "    unavailable")


def write_evidence(model_results: list[dict[str, Any]], recommendation: str | None) -> Path:
    """Write the full local result so the decision can be reproduced."""

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_file = OUTPUT_DIRECTORY / f"local-model-suitability-{timestamp}.json"
    evidence = {
        "status": "USER_EXECUTED_RESULT",
        "benchmark_contract_version": BENCHMARK_CONTRACT_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "ollama_base_url": OLLAMA_BASE_URL,
        "case_file": str(CASES_FILE),
        "recommended_model": recommendation,
        "models": model_results,
    }
    output_file.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return output_file


def main() -> int:
    """Check prerequisites, evaluate 1 model, and report the evidence."""

    print("AI Service Request Automation - local model suitability benchmark")
    print(f"Benchmark contract: v{BENCHMARK_CONTRACT_VERSION}")
    print(f"Ollama: {OLLAMA_BASE_URL}")

    try:
        installed_names = installed_model_names()
    except (urllib.error.URLError, TimeoutError):
        print("\nOllama is not reachable.")
        print("Install and start Ollama for Windows, then run this script again.")
        return 2

    if not model_is_installed(MODEL_NAME, installed_names):
        print("\nThe benchmark does not download models automatically.")
        print("Run this command first:")
        print(f"  ollama pull {MODEL_NAME}")
        return 2

    cases = load_cases()
    model_results = [benchmark_model(MODEL_NAME, cases)]

    for model_result in model_results:
        print_model_summary(model_result)

    recommendation = (
        MODEL_NAME if model_results[0]["summary"]["quality_gate_passed"] else None
    )
    evidence_file = write_evidence(model_results, recommendation)

    print("\nBenchmark decision")
    if recommendation:
        print(f"  Preferred model passed: {recommendation}")
    else:
        print("  The preferred model did not pass every quality gate.")
        print("  Review the evidence before testing a smaller fallback model.")
    print(f"  Evidence: {evidence_file}")
    print("\nReturn this terminal summary to Codex for review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
