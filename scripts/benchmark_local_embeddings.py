"""Evaluate the planned local embedding model before stage 4 begins.

The benchmark embeds a small fixed fictional policy corpus and 10 bilingual
queries through local Ollama. It ranks chunks by cosine similarity in memory,
so this checkpoint does not create database tables or application retrieval
code. The optional pull flag downloads only the accepted zero-cost local model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
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
CASES_FILE = PROJECT_ROOT / "evaluation" / "embedding_suitability_cases.json"
OUTPUT_DIRECTORY = PROJECT_ROOT / "output" / "embedding_suitability"
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
MODEL_NAME = os.environ.get(
    "AI_AUTOMATION_EMBEDDING_MODEL", "qwen3-embedding:0.6b"
)
KEEP_ALIVE = "10m"


def parse_arguments() -> argparse.Namespace:
    """Read the 2 explicit ways this script can be used."""

    parser = argparse.ArgumentParser(
        description="Benchmark qwen3-embedding:0.6b through local Ollama."
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the fixed benchmark contract without invoking Ollama.",
    )
    parser.add_argument(
        "--pull-if-missing",
        action="store_true",
        help="Download the accepted local model when Ollama does not have it.",
    )
    return parser.parse_args()


def load_contract() -> dict[str, Any]:
    """Load the fixed benchmark corpus, cases, and acceptance thresholds."""

    with CASES_FILE.open("r", encoding="utf-8") as file_handle:
        contract = json.load(file_handle)

    if not isinstance(contract, dict):
        raise ValueError("The embedding benchmark file must contain 1 JSON object.")
    return contract


def require_non_blank(value: Any, location: str) -> None:
    """Reject missing or blank fixture strings with a precise message."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-blank string.")


def validate_contract(contract: dict[str, Any]) -> None:
    """Validate the small fixed benchmark before any model is invoked."""

    if contract.get("benchmark_contract_version") != "1":
        raise ValueError("benchmark_contract_version must be 1.")
    require_non_blank(contract.get("task_instruction"), "task_instruction")

    acceptance = contract.get("acceptance")
    if not isinstance(acceptance, dict):
        raise ValueError("acceptance must be an object.")

    expected_acceptance_keys = {
        "expected_vector_dimensions",
        "top_k",
        "minimum_recall_at_3",
        "minimum_recall_at_3_per_language",
        "maximum_median_warm_query_seconds",
        "maximum_p95_warm_query_seconds",
    }
    if set(acceptance) != expected_acceptance_keys:
        raise ValueError("acceptance contains missing or unexpected fields.")
    if acceptance["expected_vector_dimensions"] != 1024:
        raise ValueError("expected_vector_dimensions must remain 1024.")
    if acceptance["top_k"] != 3:
        raise ValueError("top_k must remain 3 for Recall@3.")
    for threshold_name in (
        "minimum_recall_at_3",
        "minimum_recall_at_3_per_language",
    ):
        threshold = acceptance[threshold_name]
        if not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            raise ValueError(f"{threshold_name} must be between 0 and 1.")
    for latency_name in (
        "maximum_median_warm_query_seconds",
        "maximum_p95_warm_query_seconds",
    ):
        latency = acceptance[latency_name]
        if not isinstance(latency, (int, float)) or latency <= 0:
            raise ValueError(f"{latency_name} must be greater than 0.")

    documents = contract.get("documents")
    queries = contract.get("queries")
    if not isinstance(documents, list) or len(documents) != 10:
        raise ValueError("The fixed benchmark must contain exactly 10 documents.")
    if not isinstance(queries, list) or len(queries) != 10:
        raise ValueError("The fixed benchmark must contain exactly 10 queries.")

    chunk_ids: list[str] = []
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise ValueError(f"documents[{index}] must be an object.")
        if set(document) != {
            "chunk_id",
            "policy_code",
            "title",
            "visibility",
            "text",
        }:
            raise ValueError(f"documents[{index}] has missing or unexpected fields.")
        for field_name in document:
            require_non_blank(document[field_name], f"documents[{index}].{field_name}")
        chunk_ids.append(document["chunk_id"])

    if len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Document chunk_id values must be unique.")

    query_ids: list[str] = []
    languages: list[str] = []
    for index, query in enumerate(queries):
        if not isinstance(query, dict):
            raise ValueError(f"queries[{index}] must be an object.")
        if set(query) != {
            "query_id",
            "language",
            "query",
            "relevant_chunk_ids",
        }:
            raise ValueError(f"queries[{index}] has missing or unexpected fields.")
        require_non_blank(query.get("query_id"), f"queries[{index}].query_id")
        require_non_blank(query.get("query"), f"queries[{index}].query")
        if query.get("language") not in {"en", "id"}:
            raise ValueError(f"queries[{index}].language must be en or id.")
        relevant_ids = query.get("relevant_chunk_ids")
        if not isinstance(relevant_ids, list) or len(relevant_ids) != 1:
            raise ValueError(
                f"queries[{index}].relevant_chunk_ids must contain exactly 1 ID."
            )
        if relevant_ids[0] not in chunk_ids:
            raise ValueError(
                f"queries[{index}] references an unknown relevant chunk ID."
            )
        query_ids.append(query["query_id"])
        languages.append(query["language"])

    if len(set(query_ids)) != len(query_ids):
        raise ValueError("Query query_id values must be unique.")
    if languages.count("en") != 5 or languages.count("id") != 5:
        raise ValueError("The fixed queries must contain 5 English and 5 Indonesian cases.")


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Send 1 JSON request to the local Ollama API."""

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
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Ollama returned a JSON value that is not an object.")
    return result


def installed_models() -> dict[str, dict[str, Any]]:
    """Return installed Ollama model records keyed by their visible names."""

    response = request_json("GET", "/api/tags", timeout_seconds=10)
    records: dict[str, dict[str, Any]] = {}
    for model in response.get("models", []):
        if isinstance(model, dict) and isinstance(model.get("name"), str):
            records[model["name"]] = model
    return records


def find_model_record(
    model_name: str, records: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the exact candidate, allowing Ollama's explicit latest suffix."""

    return records.get(model_name) or records.get(f"{model_name}:latest")


def pull_model(model_name: str) -> None:
    """Download the accepted zero-cost local model through the Ollama CLI."""

    print(f"\n{model_name} is not installed. Downloading it through Ollama...")
    try:
        completed = subprocess.run(["ollama", "pull", model_name], check=False)
    except FileNotFoundError as error:
        raise RuntimeError("The ollama command is not available on PATH.") from error
    if completed.returncode != 0:
        raise RuntimeError(f"ollama pull exited with code {completed.returncode}.")


def stop_model(model_name: str) -> None:
    """Unload the candidate before measuring cold corpus indexing."""

    try:
        subprocess.run(
            ["ollama", "stop", model_name],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return


def ollama_process_snapshot() -> str:
    """Capture Ollama's processor and memory placement summary."""

    try:
        result = subprocess.run(
            ["ollama", "ps"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "ollama command unavailable"
    return result.stdout.strip()


def embed_texts(model_name: str, texts: list[str]) -> dict[str, Any]:
    """Embed a batch through Ollama and retain wall-clock runtime metadata."""

    payload = {
        "model": model_name,
        "input": texts,
        "truncate": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"num_ctx": 4096},
    }
    started_at = time.perf_counter()
    response = request_json("POST", "/api/embed", payload)
    wall_time_seconds = time.perf_counter() - started_at
    return {
        "embeddings": response.get("embeddings"),
        "wall_time_seconds": wall_time_seconds,
        "total_duration_seconds": response.get("total_duration", 0)
        / 1_000_000_000,
        "load_duration_seconds": response.get("load_duration", 0)
        / 1_000_000_000,
        "prompt_tokens": response.get("prompt_eval_count", 0),
    }


def validate_vectors(
    vectors: Any,
    expected_count: int,
    expected_dimensions: int,
    label: str,
) -> list[list[float]]:
    """Require finite, non-zero vectors with the accepted fixed dimension."""

    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise ValueError(f"{label} returned an unexpected vector count.")

    validated: list[list[float]] = []
    for index, vector in enumerate(vectors):
        if not isinstance(vector, list) or len(vector) != expected_dimensions:
            raise ValueError(
                f"{label}[{index}] must contain {expected_dimensions} dimensions."
            )
        if any(
            not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in vector
        ):
            raise ValueError(f"{label}[{index}] contains a non-finite value.")
        converted = [float(value) for value in vector]
        if math.sqrt(sum(value * value for value in converted)) == 0:
            raise ValueError(f"{label}[{index}] is a zero vector.")
        validated.append(converted)
    return validated


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Calculate cosine similarity without assuming normalized model output."""

    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm)


def nearest_rank_percentile(values: list[float], percentile: float) -> float:
    """Return a transparent nearest-rank percentile for the small sample."""

    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def query_input(task_instruction: str, query: str) -> str:
    """Apply Qwen's retrieval instruction to queries but not documents."""

    return f"Instruct: {task_instruction}\nQuery: {query}"


def run_benchmark(
    model_name: str,
    model_record: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Run the fixed in-memory retrieval suitability benchmark."""

    acceptance = contract["acceptance"]
    documents = contract["documents"]
    queries = contract["queries"]
    expected_dimensions = acceptance["expected_vector_dimensions"]
    top_k = acceptance["top_k"]

    print(f"\nBenchmarking {model_name}")
    print(f"Corpus: {len(documents)} fictional policy chunks")
    print(f"Queries: {len(queries)} (5 English, 5 Indonesian)")

    stop_model(model_name)
    document_result = embed_texts(
        model_name, [document["text"] for document in documents]
    )
    document_vectors = validate_vectors(
        document_result.pop("embeddings"),
        len(documents),
        expected_dimensions,
        "document embeddings",
    )

    query_results: list[dict[str, Any]] = []
    warm_latencies: list[float] = []
    for position, query in enumerate(queries, start=1):
        embedded_query = query_input(contract["task_instruction"], query["query"])
        query_result = embed_texts(model_name, [embedded_query])
        query_vector = validate_vectors(
            query_result.pop("embeddings"),
            1,
            expected_dimensions,
            f"query embedding {query['query_id']}",
        )[0]
        warm_latencies.append(query_result["wall_time_seconds"])

        scored_documents = [
            {
                "rank": 0,
                "chunk_id": document["chunk_id"],
                "similarity": cosine_similarity(query_vector, document_vector),
            }
            for document, document_vector in zip(documents, document_vectors)
        ]
        scored_documents.sort(key=lambda item: item["similarity"], reverse=True)
        for rank, item in enumerate(scored_documents, start=1):
            item["rank"] = rank

        retrieved = scored_documents[:top_k]
        retrieved_ids = {item["chunk_id"] for item in retrieved}
        relevant_ids = set(query["relevant_chunk_ids"])
        relevant_hits = len(retrieved_ids & relevant_ids)
        query_recall = relevant_hits / len(relevant_ids)

        result = {
            "query_id": query["query_id"],
            "language": query["language"],
            "relevant_chunk_ids": query["relevant_chunk_ids"],
            "retrieved_top_3": retrieved,
            "recall_at_3": query_recall,
            "top_1_correct": scored_documents[0]["chunk_id"] in relevant_ids,
            **query_result,
        }
        query_results.append(result)
        print(
            f"[{position:02d}/{len(queries):02d}] {query['query_id']}  "
            f"top1={scored_documents[0]['chunk_id']}  "
            f"recall@3={query_recall:.0%}  "
            f"latency={query_result['wall_time_seconds']:.3f}s"
        )

    total_relevant = sum(len(query["relevant_chunk_ids"]) for query in queries)
    total_retrieved_relevant = sum(
        result["recall_at_3"] * len(result["relevant_chunk_ids"])
        for result in query_results
    )
    recall_at_3 = total_retrieved_relevant / total_relevant
    top_1_accuracy = sum(result["top_1_correct"] for result in query_results) / len(
        query_results
    )

    recall_by_language: dict[str, float] = {}
    for language in ("en", "id"):
        language_results = [
            result for result in query_results if result["language"] == language
        ]
        recall_by_language[language] = statistics.mean(
            result["recall_at_3"] for result in language_results
        )

    median_warm = statistics.median(warm_latencies)
    p95_warm = nearest_rank_percentile(warm_latencies, 0.95)
    quality_gate_passed = (
        recall_at_3 >= acceptance["minimum_recall_at_3"]
        and all(
            recall >= acceptance["minimum_recall_at_3_per_language"]
            for recall in recall_by_language.values()
        )
    )
    latency_gate_passed = (
        median_warm <= acceptance["maximum_median_warm_query_seconds"]
        and p95_warm <= acceptance["maximum_p95_warm_query_seconds"]
    )

    summary = {
        "model": model_name,
        "completed_queries": len(query_results),
        "total_queries": len(queries),
        "vector_integrity_passed": True,
        "vector_dimensions": expected_dimensions,
        "recall_at_3": recall_at_3,
        "recall_at_3_by_language": recall_by_language,
        "top_1_accuracy": top_1_accuracy,
        "cold_corpus_indexing_seconds": document_result["wall_time_seconds"],
        "cold_model_load_seconds": document_result["load_duration_seconds"],
        "median_warm_query_seconds": median_warm,
        "p95_warm_query_seconds": p95_warm,
        "quality_gate_passed": quality_gate_passed,
        "latency_gate_passed": latency_gate_passed,
        "suitability_gate_passed": quality_gate_passed and latency_gate_passed,
        "ollama_process_snapshot": ollama_process_snapshot(),
    }
    return {
        "summary": summary,
        "model_record": model_record,
        "document_embedding_run": document_result,
        "queries": query_results,
    }


def print_summary(summary: dict[str, Any]) -> None:
    """Print only the measures required for the checkpoint decision."""

    print("\nEmbedding suitability summary")
    print(f"  Completed queries: {summary['completed_queries']}/{summary['total_queries']}")
    print(f"  Vector integrity: {'PASS' if summary['vector_integrity_passed'] else 'FAIL'}")
    print(f"  Vector dimensions: {summary['vector_dimensions']}")
    print(f"  Recall@3: {summary['recall_at_3']:.1%}")
    print(f"  Recall@3 English: {summary['recall_at_3_by_language']['en']:.1%}")
    print(f"  Recall@3 Indonesian: {summary['recall_at_3_by_language']['id']:.1%}")
    print(f"  Top-1 accuracy: {summary['top_1_accuracy']:.1%}")
    print(f"  Cold corpus indexing: {summary['cold_corpus_indexing_seconds']:.3f}s")
    print(f"  Median warm query: {summary['median_warm_query_seconds']:.3f}s")
    print(f"  P95 warm query: {summary['p95_warm_query_seconds']:.3f}s")
    print(f"  Quality gate: {'PASS' if summary['quality_gate_passed'] else 'CHECK'}")
    print(f"  Latency gate: {'PASS' if summary['latency_gate_passed'] else 'CHECK'}")
    print(
        "  Suitability gate: "
        f"{'PASS' if summary['suitability_gate_passed'] else 'CHECK'}"
    )
    print("  Ollama processor placement:")
    print(summary["ollama_process_snapshot"] or "    unavailable")


def write_evidence(contract: dict[str, Any], result: dict[str, Any]) -> Path:
    """Save compact reproducible evidence while excluding 1,024-value vectors."""

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_file = OUTPUT_DIRECTORY / f"local-embedding-suitability-{timestamp}.json"
    case_bytes = CASES_FILE.read_bytes()
    evidence = {
        "status": "USER_EXECUTED_RESULT",
        "benchmark_contract_version": contract["benchmark_contract_version"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "ollama_base_url": OLLAMA_BASE_URL,
        "case_file": str(CASES_FILE),
        "case_file_sha256": hashlib.sha256(case_bytes).hexdigest(),
        "task_instruction": contract["task_instruction"],
        "acceptance": contract["acceptance"],
        **result,
    }
    output_file.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return output_file


def main() -> int:
    """Validate the contract or execute 1 bounded local model checkpoint."""

    arguments = parse_arguments()
    try:
        contract = load_contract()
        validate_contract(contract)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Benchmark contract validation failed: {error}")
        return 1

    print("AI Service Request Automation - local embedding suitability benchmark")
    print(f"Benchmark contract: v{contract['benchmark_contract_version']}")
    print("Static contract: PASS")
    if arguments.validate_only:
        print("No model was downloaded or invoked.")
        return 0

    print(f"Ollama: {OLLAMA_BASE_URL}")
    try:
        records = installed_models()
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        print("\nOllama is not reachable.")
        print("Start Ollama for Windows, then run this command again.")
        return 2

    model_record = find_model_record(MODEL_NAME, records)
    if model_record is None and arguments.pull_if_missing:
        try:
            pull_model(MODEL_NAME)
            model_record = find_model_record(MODEL_NAME, installed_models())
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as error:
            print(f"\nModel download failed: {error}")
            return 2

    if model_record is None:
        print(f"\n{MODEL_NAME} is not installed.")
        print("Run the benchmark with --pull-if-missing to download it explicitly.")
        return 2

    try:
        result = run_benchmark(MODEL_NAME, model_record, contract)
    except (
        OSError,
        TypeError,
        ValueError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
    ) as error:
        print(f"\nEmbedding benchmark failed: {error}")
        return 3

    print_summary(result["summary"])
    evidence_file = write_evidence(contract, result)
    print(f"\nEvidence: {evidence_file}")
    print("Return this terminal summary to Codex for review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
