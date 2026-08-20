from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import time
import unicodedata
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import psycopg
from psycopg.rows import dict_row

from ai_analysis import OllamaAnalysisProvider, ProviderResult, analyze_case
from delivery import ServiceDeskClient, claim_next_message, process_one_message
from downstream_route import queue_approved_action, reconcile_approved_action
from human_decision import (
    HumanDecisionCommand,
    execute_human_decision,
)
from human_resume import (
    HumanResumeCommand,
    HumanResumeResponse,
    acknowledge_human_resume,
    claim_next_human_resume,
    enqueue_next_human_resume,
    finalize_human_resume,
)
from human_resume_consumers import (
    LocalNotificationClient,
    materialize_terminal_notification,
    process_one_notification,
    reconcile_terminal_notification,
)
from intake import (
    IdempotencyConflict,
    IntakeRequest,
    RequesterSelector,
    content_fingerprint_for,
    create_or_replay_case,
)
from policy_retrieval import (
    RETRIEVAL_INSTRUCTION,
    OllamaPolicyProvider,
    RetrievalProviderError,
    retrieve_policy,
)
from recovery import recover_expired_claims
from safe_action import queue_safe_action, reconcile_safe_action
from workflow_start import (
    WorkflowStartResponse,
    claim_next_workflow_start,
    finalize_workflow_start,
    start_or_replay_analysis,
)


DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
SANDBOX_URL = os.environ["SERVICE_DESK_SANDBOX_URL"].rstrip("/")
SANDBOX_TOKEN = os.environ["SERVICE_DESK_SANDBOX_TOKEN"]
OLLAMA_URL = os.environ.get(
    "EVALUATION_OLLAMA_BASE_URL", "http://host.docker.internal:11434"
).rstrip("/")
ANALYSIS_IDENTIFIER = os.environ["EVALUATION_ANALYSIS_IDENTIFIER"]
EMBEDDING_IDENTIFIER = os.environ["EVALUATION_EMBEDDING_IDENTIFIER"]
CORPUS_FILE = Path(os.environ["EVALUATION_CORPUS_FILE"])
EMBEDDING_CORPUS_FILE = Path(os.environ["EMBEDDING_CORPUS_FILE"])
SUITABILITY_CASES_FILE = Path(os.environ["SUITABILITY_CASES_FILE"])
EXPECTED_CORPUS_SHA256 = os.environ["EVALUATION_CORPUS_SHA256"]
EVIDENCE_DIRECTORY = Path(os.environ.get("EVALUATION_EVIDENCE_DIRECTORY", "/evidence"))
EVIDENCE_NAME = "locked-system-evaluation-v1.json"
RUN_STARTED_AT_UTC = datetime.now(timezone.utc).isoformat()

APPROVER_USER_ID = UUID("10000000-0000-4000-8000-000000000003")
REQUEST_TYPES = (
    "policy_question",
    "incident_report",
    "access_request",
    "data_change_request",
    "status_request",
)
REQUIRED_FIELDS = {
    "policy_question": {"policy_topic", "question"},
    "incident_report": {
        "affected_service",
        "incident_description",
        "impact",
        "urgency",
    },
    "access_request": {
        "target_system",
        "requested_access_level",
        "business_reason",
        "approver_id",
    },
    "data_change_request": {
        "target_system",
        "record_reference",
        "requested_changes",
        "business_reason",
        "approver_id",
    },
    "status_request": {"case_reference"},
}
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
CONTROL_NAMES = (
    "exact_intake_replay",
    "idempotency_conflict",
    "possible_duplicate_isolated",
    "analysis_exact_replay",
    "downstream_exact_replay",
    "approval_approved",
    "approval_rejected",
    "transient_delivery_retry",
    "permanent_delivery_failure",
    "expired_claim_recovery",
)


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def normalized(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


def field_matches(value: Any, alternatives: list[list[str]]) -> bool:
    candidate = normalized(value)
    return bool(candidate) and any(
        all(normalized(keyword) in candidate for keyword in keywords)
        for keywords in alternatives
    )


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(len(ordered) * fraction)) - 1]


def load_contract() -> tuple[dict[str, Any], str]:
    raw = CORPUS_FILE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    require(digest == EXPECTED_CORPUS_SHA256, "the locked corpus hash changed")
    contract = json.loads(raw)
    require(contract.get("contract_version") == "1", "contract version changed")
    require(
        contract.get("accepted_models")
        == {
            "analysis": "qwen3:4b-instruct",
            "embedding": "qwen3-embedding:0.6b",
        },
        "accepted model contract changed",
    )
    semantic = contract.get("semantic_cases")
    controls = contract.get("workflow_cases")
    require(isinstance(semantic, list) and len(semantic) == 40, "semantic corpus is not 40")
    require(isinstance(controls, list) and len(controls) == 10, "control corpus is not 10")
    counts = Counter(case["expected"]["request_type"] for case in semantic)
    require(counts == Counter({name: 8 for name in REQUEST_TYPES}), "request-type balance changed")
    all_ids = [case["case_id"] for case in semantic + controls]
    require(len(all_ids) == len(set(all_ids)) == 50, "evaluation case IDs are not unique")
    require(tuple(case["control"] for case in controls) == CONTROL_NAMES, "control order changed")
    control_inputs = [(case["subject"], case["message"]) for case in controls]
    require(
        len(control_inputs) == len(set(control_inputs)),
        "workflow controls share a content fingerprint",
    )
    semantic_inputs = {(case["subject"], case["message"]) for case in semantic}
    require(
        not any(item in semantic_inputs for item in control_inputs),
        "a workflow control duplicates a semantic evaluation input",
    )
    for case in semantic:
        expected = case["expected"]
        populated = set(expected["field_expectations"])
        null_fields = set(expected["null_fields"])
        require(
            populated.isdisjoint(null_fields)
            and populated | null_fields == REQUIRED_FIELDS[expected["request_type"]],
            f"{case['case_id']} required-field contract is incomplete",
        )
        require(
            (expected["request_type"] == "policy_question")
            == bool(expected["retrieval_relevant"]),
            f"{case['case_id']} retrieval scope is inconsistent",
        )

    old_cases = json.loads(SUITABILITY_CASES_FILE.read_text(encoding="utf-8"))
    old_inputs = {(case["subject"], case["message"]) for case in old_cases}
    require(
        not any((case["subject"], case["message"]) in old_inputs for case in semantic),
        "a model-selection prompt leaked into the locked semantic corpus",
    )
    embedding_contract = json.loads(EMBEDDING_CORPUS_FILE.read_text(encoding="utf-8"))
    old_queries = {query["query"] for query in embedding_contract["queries"]}
    require(
        not any(
            case["message"] in old_queries
            for case in semantic
            if case["expected"]["request_type"] == "policy_question"
        ),
        "an embedding-selection query leaked into the locked policy cases",
    )
    for case in controls:
        source_text = f"{case['subject']}\n{case['message']}"
        proposal = fixture_proposal(case)
        require(
            all(item["quote"] in source_text for item in proposal["evidence"]),
            f"{case['case_id']} fixture evidence is not grounded in its input",
        )
    return contract, digest


def vector_literal(vector: list[float]) -> str:
    require(len(vector) == 1024, "the accepted embedding dimension changed")
    require(all(math.isfinite(float(value)) for value in vector), "an embedding is non-finite")
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


def batch_embed(texts: list[str]) -> list[list[float]]:
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps(
            {
                "model": "qwen3-embedding:0.6b",
                "input": texts,
                "truncate": False,
                "keep_alive": 0,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=180) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    require(len(raw) <= 8 * 1024 * 1024, "the embedding response exceeded 8 MiB")
    payload = json.loads(raw)
    embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
    require(
        isinstance(payload, dict)
        and payload.get("model") == "qwen3-embedding:0.6b"
        and isinstance(embeddings, list)
        and len(embeddings) == len(texts),
        "the local batch embedding response was invalid",
    )
    return embeddings


def index_policy_corpus(
    documents: list[dict[str, Any]],
    vectors: list[list[float]],
    embedding_identifier: str,
) -> set[str]:
    citation_ids: set[str] = set()
    for document, vector in zip(documents, vectors, strict=True):
        embedding = vector_literal(vector)
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
            existing = connection.execute(
                "SELECT policy_document_id FROM policy_documents WHERE policy_code=%s AND version=1",
                (document["policy_code"],),
            ).fetchone()
            if existing is None:
                document_id = uuid5(NAMESPACE_URL, f"locked-eval:{document['policy_code']}")
                connection.execute(
                    """
                    INSERT INTO policy_documents(
                        policy_document_id,policy_code,title,visibility,version,
                        content_sha256,is_active,valid_from
                    ) VALUES(%s,%s,%s,%s,1,%s,true,now()-interval '1 day')
                    """,
                    (
                        document_id,
                        document["policy_code"],
                        document["title"],
                        document["visibility"],
                        hashlib.sha256(document["text"].encode("utf-8")).hexdigest(),
                    ),
                )
                chunk_number = 0
            else:
                document_id = existing["policy_document_id"]
                chunk_number = 1
            connection.execute(
                """
                INSERT INTO policy_chunks(
                    policy_document_id,chunk_number,chunk_text,token_count,
                    embedding_model,embedding
                ) VALUES(%s,%s,%s,%s,%s,%s::vector)
                """,
                (
                    document_id,
                    chunk_number,
                    document["text"],
                    max(1, len(document["text"].split())),
                    embedding_identifier,
                    embedding,
                ),
            )
        citation_ids.add(f"{document['policy_code']}#{chunk_number}")
    return citation_ids


class CachedPolicyProvider:
    def __init__(
        self,
        answer_provider: OllamaPolicyProvider,
        query_vectors: dict[str, list[float]],
    ) -> None:
        self.embedding_identifier = answer_provider.embedding_identifier
        self.answer_identifier = answer_provider.answer_identifier
        self._answer_provider = answer_provider
        self._query_vectors = query_vectors
        self.answer_calls = 0

    def embed(self, text: str) -> list[float]:
        try:
            return list(self._query_vectors[text])
        except KeyError as error:
            raise RetrievalProviderError(
                "the locked query embedding was not precomputed"
            ) from error

    def answer(self, query: str, chunks: Any) -> Any:
        self.answer_calls += 1
        return self._answer_provider.answer(query, chunks)


def intake_request(case: dict[str, Any], *, message: str | None = None) -> IntakeRequest:
    return IntakeRequest(
        source_channel="WEBHOOK",
        external_request_id=f"EVAL-{case['case_id']}",
        subject=case["subject"],
        message=case["message"] if message is None else message,
        attachment_metadata=[],
        received_at=datetime.now(timezone.utc),
    )


def create_case(case: dict[str, Any]) -> tuple[Any, IntakeRequest]:
    request = intake_request(case)
    result = create_or_replay_case(
        DATABASE_URL,
        request,
        RequesterSelector(employee_reference=case["requester_reference"]),
    )
    require(not result.idempotent_replay, f"{case['case_id']} unexpectedly replayed intake")
    return result, request


def start_case(case_result: Any) -> tuple[str, int]:
    message = claim_next_workflow_start(DATABASE_URL)
    require(message is not None, f"{case_result.case_reference} workflow intent was not claimed")
    require(
        message.payload.get("case_id") == str(case_result.case_id),
        f"{case_result.case_reference} claimed another case's workflow intent",
    )
    started = start_or_replay_analysis(
        DATABASE_URL,
        case_id=case_result.case_id,
        case_reference=case_result.case_reference,
        expected_case_version=1,
        trigger_event="CASE_RECEIVED",
        idempotency_key=message.idempotency_key,
    )
    now = datetime.now(timezone.utc)
    finalized = finalize_workflow_start(
        DATABASE_URL,
        message,
        WorkflowStartResponse(
            outcome="SUCCESS",
            http_status=200,
            downstream_reference=started.workflow_start_reference,
            response_payload={
                "schema_version": "1",
                "status": "ACCEPTED",
                "workflow_start_reference": started.workflow_start_reference,
                "case_reference": case_result.case_reference,
                "case_version": started.case_version,
            },
            error_code=None,
            error_message=None,
        ),
        started_at=now,
        finished_at=now,
        retry_delay_seconds=0,
    )
    require(finalized.final_status == "SENT", "workflow start did not finalize")
    return started.workflow_start_reference, started.case_version


def analysis_evidence(analysis_run_id: UUID) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT proposal,evidence,status,wall_time_ms,input_tokens,output_tokens
            FROM ai_analysis_runs WHERE analysis_run_id=%s
            """,
            (analysis_run_id,),
        ).fetchone()
    require(row is not None, "analysis evidence is missing")
    return dict(row)


def evaluate_semantic_cases(
    cases: list[dict[str, Any]],
    analysis_provider: OllamaAnalysisProvider,
    answer_provider: OllamaPolicyProvider,
) -> tuple[list[dict[str, Any]], dict[str, float], set[str]]:
    working: list[dict[str, Any]] = []
    for case in cases:
        started_at = time.perf_counter()
        created, _request = create_case(case)
        workflow_reference, analysis_version = start_case(created)
        execution = analyze_case(
            DATABASE_URL,
            analysis_provider,
            case_id=created.case_id,
            case_reference=created.case_reference,
            expected_case_version=analysis_version,
            workflow_start_reference=workflow_reference,
        )
        evidence = analysis_evidence(execution.analysis_run_id)
        working.append(
            {
                "case": case,
                "created": created,
                "execution": execution,
                "evidence": evidence,
                "analysis_seconds": time.perf_counter() - started_at,
            }
        )

    embedding_contract = json.loads(
        EMBEDDING_CORPUS_FILE.read_text(encoding="utf-8")
    )
    documents = embedding_contract["documents"]
    indexing_started = time.perf_counter()
    document_vectors = batch_embed([document["text"] for document in documents])
    indexed_ids = index_policy_corpus(
        documents,
        document_vectors,
        answer_provider.embedding_identifier,
    )
    indexing_seconds = time.perf_counter() - indexing_started

    policy_working = [
        item
        for item in working
        if item["case"]["expected"]["retrieval_relevant"]
        and item["execution"].current_state == "ANALYZING"
    ]
    query_inputs: list[str] = []
    for item in policy_working:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
            row = connection.execute(
                "SELECT policy_question FROM case_details WHERE case_id=%s",
                (item["created"].case_id,),
            ).fetchone()
        require(
            row is not None
            and isinstance(row["policy_question"], str)
            and row["policy_question"].strip(),
            f"{item['case']['case_id']} accepted policy question is missing",
        )
        item["policy_question"] = row["policy_question"].strip()
        query_inputs.append(
            f"Instruct: {RETRIEVAL_INSTRUCTION}\nQuery: {item['policy_question']}"
        )
    query_embedding_started = time.perf_counter()
    query_vectors = batch_embed(query_inputs) if query_inputs else []
    query_embedding_seconds = time.perf_counter() - query_embedding_started
    cached_provider = CachedPolicyProvider(
        answer_provider,
        dict(zip(query_inputs, query_vectors, strict=True)),
    )
    query_share = (
        query_embedding_seconds / len(query_inputs) if query_inputs else 0.0
    )

    results: list[dict[str, Any]] = []
    for item in working:
        case = item["case"]
        created = item["created"]
        execution = item["execution"]
        evidence = item["evidence"]
        proposal = evidence["proposal"] if isinstance(evidence["proposal"], dict) else {}
        fields = proposal.get("fields") if isinstance(proposal.get("fields"), dict) else {}
        predicted_type = proposal.get("request_type", "INVALID")
        expected = case["expected"]
        field_checks = {
            name: field_matches(fields.get(name), alternatives)
            for name, alternatives in expected["field_expectations"].items()
        }
        field_checks.update(
            {name: fields.get(name) in {None, ""} for name in expected["null_fields"]}
        )
        final_state = execution.current_state
        retrieved_ids: list[str] = []
        citation_ids: list[str] = []
        retrieval_hit: bool | None = None
        citation_valid: bool | None = None
        retrieval_seconds = 0.0
        if expected["retrieval_relevant"]:
            if execution.current_state == "ANALYZING":
                retrieval_started = time.perf_counter()
                retrieval = retrieve_policy(
                    DATABASE_URL,
                    cached_provider,
                    case_id=created.case_id,
                    case_reference=created.case_reference,
                    expected_case_version=execution.case_version,
                    analysis_run_id=execution.analysis_run_id,
                )
                final_state = retrieval.current_state
                retrieved_ids = list(retrieval.retrieved_chunk_ids)
                citation_ids = list(retrieval.citation_ids)
                retrieval_seconds = time.perf_counter() - retrieval_started + query_share
            relevant = set(expected["retrieval_relevant"])
            retrieval_hit = bool(relevant & set(retrieved_ids))
            citation_valid = bool(citation_ids) and set(citation_ids).issubset(
                set(retrieved_ids)
            ) and bool(relevant & set(citation_ids))
        classification_match = predicted_type == expected["request_type"]
        fields_match = all(field_checks.values())
        route_match = final_state == expected["state"]
        task_success = classification_match and fields_match and route_match
        if retrieval_hit is not None:
            task_success = task_success and retrieval_hit and citation_valid is True
        results.append(
            {
                "case_id": case["case_id"],
                "language": case["language"],
                "expected_request_type": expected["request_type"],
                "predicted_request_type": predicted_type,
                "classification_match": classification_match,
                "field_checks": field_checks,
                "expected_state": expected["state"],
                "final_state": final_state,
                "route_match": route_match,
                "retrieved_chunk_ids": retrieved_ids,
                "citation_ids": citation_ids,
                "retrieval_hit_at_3": retrieval_hit,
                "citation_valid": citation_valid,
                "task_success": task_success,
                "processing_seconds": round(
                    item["analysis_seconds"] + retrieval_seconds, 3
                ),
                "analysis_status": evidence["status"],
                "analysis_wall_time_ms": evidence["wall_time_ms"],
                "input_tokens": evidence["input_tokens"],
                "output_tokens": evidence["output_tokens"],
                "proposal": proposal,
                "evidence": evidence["evidence"],
            }
        )
    return (
        results,
        {
            "cold_policy_indexing_seconds": round(indexing_seconds, 3),
            "policy_query_embedding_batch_seconds": round(
                query_embedding_seconds, 3
            ),
            "local_ollama_calls": 41
            + (1 if query_inputs else 0)
            + cached_provider.answer_calls,
        },
        indexed_ids,
    )


def fixture_proposal(case: dict[str, Any]) -> dict[str, Any]:
    fields = {name: None for name in FIELD_NAMES}
    profile = case["fixture_profile"]
    if profile == "incident_ready":
        fields.update(
            affected_service="WMS",
            incident_description="WMS is unavailable",
            impact="high",
            urgency="high",
        )
        evidence = [
            {"field": "affected_service", "quote": "WMS"},
            {"field": "incident_description", "quote": "WMS is unavailable"},
            {"field": "impact", "quote": "Impact high"},
            {"field": "urgency", "quote": "urgency high"},
        ]
        return {
            "request_type": "incident_report",
            "summary": "Controlled WMS outage.",
            "fields": fields,
            "evidence": evidence,
        }
    if profile == "access_ready":
        reason = (
            "weekly inventory reconciliation"
            if "weekly inventory reconciliation" in case["message"]
            else "controlled evaluation report"
        )
        fields.update(
            target_system="WMS",
            requested_access_level="viewer",
            business_reason=reason,
            approver_id="MGR-104",
        )
        return {
            "request_type": "access_request",
            "summary": "Controlled WMS viewer access.",
            "fields": fields,
            "evidence": [
                {"field": "target_system", "quote": "WMS"},
                {"field": "requested_access_level", "quote": "viewer"},
                {"field": "business_reason", "quote": reason},
                {"field": "approver_id", "quote": "MGR-104"},
            ],
        }
    require(profile == "data_change_ready", f"unknown fixture profile {profile}")
    fields.update(
        target_system="WMS",
        record_reference="REC-700",
        requested_changes="value TEST-7",
        business_reason="verified control form",
        approver_id="MGR-104",
    )
    return {
        "request_type": "data_change_request",
        "summary": "Controlled WMS record change.",
        "fields": fields,
        "evidence": [
            {"field": "target_system", "quote": "WMS"},
            {"field": "record_reference", "quote": "REC-700"},
            {"field": "requested_changes", "quote": "value TEST-7"},
            {"field": "business_reason", "quote": "verified control form"},
            {"field": "approver_id", "quote": "MGR-104"},
        ],
    }


class LockedFixtureProvider:
    model_name = "locked-control-fixture-v1"
    model_identifier = "f" * 64

    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.calls = 0

    def analyze(self, subject: str, original_message: str) -> ProviderResult:
        require(subject == self.case["subject"], "fixture subject changed")
        require(original_message == self.case["message"], "fixture message changed")
        self.calls += 1
        return ProviderResult(
            model_name=self.model_name,
            model_identifier=self.model_identifier,
            proposal=fixture_proposal(self.case),
            wall_time_ms=1,
            input_tokens=0,
            output_tokens=0,
        )


def prepare_control(case: dict[str, Any]) -> tuple[Any, str, int, LockedFixtureProvider, Any]:
    created, _request = create_case(case)
    workflow_reference, analysis_version = start_case(created)
    provider = LockedFixtureProvider(case)
    execution = analyze_case(
        DATABASE_URL,
        provider,
        case_id=created.case_id,
        case_reference=created.case_reference,
        expected_case_version=analysis_version,
        workflow_start_reference=workflow_reference,
    )
    return created, workflow_reference, analysis_version, provider, execution


def outbox_payload(outbox_message_id: UUID) -> tuple[dict[str, Any], str]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT payload,idempotency_key FROM outbox_messages WHERE outbox_message_id=%s",
            (outbox_message_id,),
        ).fetchone()
    require(row is not None, "downstream outbox evidence is missing")
    return row["payload"], row["idempotency_key"]


def deliver_safe(case_id: UUID, client: ServiceDeskClient) -> Any:
    queued = queue_safe_action(DATABASE_URL, case_id=case_id)
    execution = process_one_message(DATABASE_URL, client, retry_delay_seconds=0)
    require(
        execution is not None
        and execution.outbox_message_id == queued.outbox_message_id
        and execution.final_status == "SENT",
        "safe action did not deliver",
    )
    return reconcile_safe_action(DATABASE_URL, outbox_message_id=queued.outbox_message_id)


def acknowledge_decision(case_result: Any, action: str, note: str | None) -> tuple[Any, str]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        state = connection.execute(
            "SELECT version FROM cases WHERE case_id=%s", (case_result.case_id,)
        ).fetchone()
    require(state is not None, "human-decision case is missing")
    decision = execute_human_decision(
        DATABASE_URL,
        case_reference=case_result.case_reference,
        actor_user_id=APPROVER_USER_ID,
        command=HumanDecisionCommand(
            schema_version="1",
            command_id=uuid4(),
            expected_case_version=state["version"],
            action=action,
            note=note,
        ),
    )
    outbox_id = enqueue_next_human_resume(DATABASE_URL)
    require(outbox_id is not None, "human-resume intent was not created")
    message = claim_next_human_resume(DATABASE_URL)
    require(message is not None and message.outbox_message_id == outbox_id, "resume claim mismatch")
    payload = message.payload
    command = HumanResumeCommand(
        schema_version="1",
        case_reference=payload["case_reference"],
        expected_case_version=payload["case_version"],
        human_decision_reference=payload["human_decision_reference"],
        action=payload["action"],
        trigger_event=payload["trigger_event"],
        resume_route=payload["resume_route"],
    )
    acknowledgement = acknowledge_human_resume(
        DATABASE_URL,
        case_id=case_result.case_id,
        command=command,
        idempotency_key=message.idempotency_key,
    )
    now = datetime.now(timezone.utc)
    finalized = finalize_human_resume(
        DATABASE_URL,
        message,
        HumanResumeResponse(
            outcome="SUCCESS",
            http_status=200,
            downstream_reference=acknowledgement.human_resume_reference,
            response_payload={
                "schema_version": "1",
                "status": "ACCEPTED",
                "human_resume_reference": acknowledgement.human_resume_reference,
                "case_reference": case_result.case_reference,
                "resume_route": command.resume_route,
                "current_state": decision.current_state,
                "case_version": decision.case_version,
                "idempotent_replay": False,
            },
            error_code=None,
            error_message=None,
        ),
        started_at=now,
        finished_at=now,
        retry_delay_seconds=0,
    )
    require(finalized.final_status == "SENT", "human resume did not finalize")
    return decision, acknowledgement.human_resume_reference


def run_workflow_controls(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    client = ServiceDeskClient(SANDBOX_URL, SANDBOX_TOKEN)
    results: list[dict[str, Any]] = []
    control_fingerprints: list[str] = []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        for case in cases:
            requester = connection.execute(
                "SELECT user_id FROM users WHERE employee_reference=%s",
                (case["requester_reference"],),
            ).fetchone()
            require(requester is not None, f"{case['case_id']} requester precondition is missing")
            fingerprint = content_fingerprint_for(
                requester["user_id"], intake_request(case)
            )
            control_fingerprints.append(fingerprint)
            existing_count = connection.execute(
                "SELECT count(*) AS value FROM cases WHERE content_fingerprint=%s",
                (fingerprint,),
            ).fetchone()["value"]
            expected_existing = 1 if case["control"] == "possible_duplicate_isolated" else 0
            require(
                existing_count == expected_existing,
                f"{case['case_id']} duplicate precondition changed: expected {expected_existing}, found {existing_count}",
            )
    require(
        len(control_fingerprints) == len(set(control_fingerprints)),
        "normalized workflow-control fingerprints are not unique",
    )
    for case in cases:
        control = case["control"]
        if control == "exact_intake_replay":
            created, request = create_case(case)
            replay = create_or_replay_case(
                DATABASE_URL,
                request,
                RequesterSelector(employee_reference=case["requester_reference"]),
            )
            require(replay.idempotent_replay and replay.case_id == created.case_id, "intake replay failed")
            workflow_reference, analysis_version = start_case(created)
            provider = LockedFixtureProvider(case)
            analysis = analyze_case(
                DATABASE_URL, provider, case_id=created.case_id,
                case_reference=created.case_reference,
                expected_case_version=analysis_version,
                workflow_start_reference=workflow_reference,
            )
            require(analysis.current_state == "READY_FOR_ACTION", "control analysis did not route")
            require(deliver_safe(created.case_id, client).current_state == "COMPLETED", "replay control did not complete")
        elif control == "idempotency_conflict":
            created, request = create_case(case)
            conflict_seen = False
            try:
                create_or_replay_case(
                    DATABASE_URL,
                    request.model_copy(update={"message": request.message + " Conflicting change."}),
                    RequesterSelector(employee_reference=case["requester_reference"]),
                )
            except IdempotencyConflict:
                conflict_seen = True
            require(conflict_seen, "conflicting intake identity was accepted")
            workflow_reference, analysis_version = start_case(created)
            provider = LockedFixtureProvider(case)
            analyze_case(
                DATABASE_URL, provider, case_id=created.case_id,
                case_reference=created.case_reference,
                expected_case_version=analysis_version,
                workflow_start_reference=workflow_reference,
            )
            require(deliver_safe(created.case_id, client).current_state == "COMPLETED", "conflict control did not complete")
        elif control == "possible_duplicate_isolated":
            created, _request = create_case(case)
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
                duplicate_count = connection.execute(
                    "SELECT count(*) AS value FROM cases WHERE content_fingerprint=(SELECT content_fingerprint FROM cases WHERE case_id=%s)",
                    (created.case_id,),
                ).fetchone()["value"]
            require(duplicate_count >= 2, "possible duplicate was not preserved as distinct evidence")
            workflow_reference, analysis_version = start_case(created)
            analysis = analyze_case(
                DATABASE_URL, LockedFixtureProvider(case), case_id=created.case_id,
                case_reference=created.case_reference,
                expected_case_version=analysis_version,
                workflow_start_reference=workflow_reference,
            )
            require(
                analysis.current_state == "NEEDS_REVIEW",
                "possible duplicate did not route to service-agent review",
            )
        elif control == "analysis_exact_replay":
            created, workflow_reference, analysis_version, provider, analysis = prepare_control(case)
            replay = analyze_case(
                DATABASE_URL, provider, case_id=created.case_id,
                case_reference=created.case_reference,
                expected_case_version=analysis_version,
                workflow_start_reference=workflow_reference,
            )
            require(replay.idempotent_replay and not replay.provider_called and provider.calls == 1, "analysis replay called the provider")
            require(deliver_safe(created.case_id, client).current_state == "COMPLETED", "analysis replay control did not complete")
        elif control == "downstream_exact_replay":
            created, _reference, _version, _provider, _analysis = prepare_control(case)
            queued = queue_safe_action(DATABASE_URL, case_id=created.case_id)
            payload, key = outbox_payload(queued.outbox_message_id)
            precreated = client.create_service_record(payload, key)
            require(precreated.outcome == "SUCCESS", "downstream replay precondition failed")
            execution = process_one_message(DATABASE_URL, client, retry_delay_seconds=0)
            require(execution is not None and execution.downstream_reference == precreated.downstream_reference, "downstream replay changed the record")
            first = reconcile_safe_action(DATABASE_URL, outbox_message_id=queued.outbox_message_id)
            second = reconcile_safe_action(DATABASE_URL, outbox_message_id=queued.outbox_message_id)
            require(first.current_state == "COMPLETED" and second.idempotent_replay, "reconciliation replay duplicated work")
        elif control == "approval_approved":
            created, _reference, _version, _provider, analysis = prepare_control(case)
            require(analysis.current_state == "PENDING_APPROVAL", "approval control did not pause")
            _decision, resume_reference = acknowledge_decision(created, "APPROVE_REQUEST", "Approved locked evaluation control.")
            queued = queue_approved_action(DATABASE_URL, case_id=created.case_id, human_resume_reference=resume_reference)
            execution = process_one_message(DATABASE_URL, client, retry_delay_seconds=0)
            require(execution is not None and execution.outbox_message_id == queued.outbox_message_id, "approved action did not deliver")
            require(reconcile_approved_action(DATABASE_URL, outbox_message_id=queued.outbox_message_id).current_state == "COMPLETED", "approved action did not complete")
        elif control == "approval_rejected":
            created, _reference, _version, _provider, analysis = prepare_control(case)
            require(analysis.current_state == "PENDING_APPROVAL", "rejection control did not pause")
            _decision, resume_reference = acknowledge_decision(created, "REJECT_REQUEST", "Rejected locked evaluation control.")
            intent = materialize_terminal_notification(DATABASE_URL, human_resume_reference=resume_reference)
            notification = process_one_notification(DATABASE_URL, LocalNotificationClient())
            require(notification is not None and notification.outbox_message_id == intent.outbox_message_id, "rejection notification did not deliver")
            require(reconcile_terminal_notification(DATABASE_URL, outbox_message_id=intent.outbox_message_id).event_type == "REQUESTER_NOTIFICATION_SENT", "notification evidence is missing")
        elif control == "transient_delivery_retry":
            created, _reference, _version, _provider, _analysis = prepare_control(case)
            queued = queue_safe_action(DATABASE_URL, case_id=created.case_id)
            first = process_one_message(DATABASE_URL, client, test_outcome="TRANSIENT_ONCE", retry_delay_seconds=0)
            second = process_one_message(DATABASE_URL, client, test_outcome="TRANSIENT_ONCE", retry_delay_seconds=0)
            require(
                first is not None and first.final_status == "PENDING"
                and second is not None and second.final_status == "SENT"
                and second.attempt_number == 2,
                "transient delivery did not recover within the bound",
            )
            require(reconcile_safe_action(DATABASE_URL, outbox_message_id=queued.outbox_message_id).current_state == "COMPLETED", "transient control did not complete")
        elif control == "permanent_delivery_failure":
            created, _reference, _version, _provider, _analysis = prepare_control(case)
            queued = queue_safe_action(DATABASE_URL, case_id=created.case_id)
            execution = process_one_message(DATABASE_URL, client, test_outcome="PERMANENT_FAILURE", retry_delay_seconds=0)
            require(execution is not None and execution.final_status == "FAILED", "permanent delivery was not terminal")
            require(reconcile_safe_action(DATABASE_URL, outbox_message_id=queued.outbox_message_id).current_state == "FAILED", "permanent failure did not reconcile")
        elif control == "expired_claim_recovery":
            created, _reference, _version, _provider, _analysis = prepare_control(case)
            queued = queue_safe_action(DATABASE_URL, case_id=created.case_id)
            claimed = claim_next_message(DATABASE_URL)
            require(claimed is not None and claimed.outbox_message_id == queued.outbox_message_id, "recovery claim mismatch")
            time.sleep(1.25)
            sweep = recover_expired_claims(
                DATABASE_URL, lease_seconds=1, retry_delay_seconds=0, limit=1
            )
            require(
                len(sweep.recovered_claims) == 1
                and sweep.recovered_claims[0].outbox_message_id == queued.outbox_message_id
                and sweep.pending_retries == 1,
                "expired claim did not return to bounded retry",
            )
            execution = process_one_message(DATABASE_URL, client, retry_delay_seconds=0)
            require(execution is not None and execution.final_status == "SENT" and execution.attempt_number == 2, "recovered claim did not deliver")
            require(reconcile_safe_action(DATABASE_URL, outbox_message_id=queued.outbox_message_id).current_state == "COMPLETED", "recovered action did not complete")
        else:
            raise AssertionError(f"unknown control {control}")
        results.append({"case_id": case["case_id"], "control": control, "passed": True})
    return results


def aggregate_evidence(expected_evaluation_cases: int) -> dict[str, int]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM cases WHERE external_request_id LIKE 'EVAL-%') AS evaluation_cases,
              (SELECT count(*) FROM outbox_messages m JOIN cases c USING(case_id)
               WHERE c.external_request_id LIKE 'EVAL-%'
                 AND m.status IN ('PENDING','PROCESSING')) AS unfinished_outbox,
              (SELECT count(*) FROM (
                 SELECT e.case_id,e.event_type,count(*)
                 FROM case_events e JOIN cases c USING(case_id)
                 WHERE c.external_request_id LIKE 'EVAL-%'
                   AND e.event_type IN ('DOWNSTREAM_ACTION_COMPLETED','DOWNSTREAM_ACTION_FAILED')
                 GROUP BY e.case_id,e.event_type HAVING count(*) > 1
               ) duplicates) AS duplicate_terminal_effects
            """
        ).fetchone()
    require(row is not None, "aggregate evaluation evidence is missing")
    evidence = {name: int(value) for name, value in dict(row).items()}
    require(
        evidence
        == {
            "evaluation_cases": expected_evaluation_cases,
            "unfinished_outbox": 0,
            "duplicate_terminal_effects": 0,
        },
        f"aggregate evaluation evidence changed: {evidence}",
    )
    return {
        "current_database_evaluation_cases": evidence["evaluation_cases"],
        "unfinished_outbox": evidence["unfinished_outbox"],
        "duplicate_terminal_effects": evidence["duplicate_terminal_effects"],
    }


def macro_f1(results: list[dict[str, Any]]) -> float:
    scores: list[float] = []
    for request_type in REQUEST_TYPES:
        true_positive = sum(
            row["expected_request_type"] == request_type
            and row["predicted_request_type"] == request_type
            for row in results
        )
        false_positive = sum(
            row["expected_request_type"] != request_type
            and row["predicted_request_type"] == request_type
            for row in results
        )
        false_negative = sum(
            row["expected_request_type"] == request_type
            and row["predicted_request_type"] != request_type
            for row in results
        )
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append((2 * true_positive / denominator) if denominator else 0.0)
    return statistics.mean(scores)


def summarize(
    contract: dict[str, Any],
    semantic: list[dict[str, Any]],
    controls: list[dict[str, Any]],
) -> dict[str, Any]:
    field_values = [value for row in semantic for value in row["field_checks"].values()]
    retrieval = [row for row in semantic if row["retrieval_hit_at_3"] is not None]
    latencies = [row["processing_seconds"] for row in semantic]
    summary = {
        "semantic_cases": len(semantic),
        "workflow_control_cases": len(controls),
        "classification_macro_f1": macro_f1(semantic),
        "required_field_accuracy": sum(field_values) / len(field_values),
        "retrieval_recall_at_3": sum(row["retrieval_hit_at_3"] is True for row in retrieval) / len(retrieval),
        "citation_validity": sum(row["citation_valid"] is True for row in retrieval) / len(retrieval),
        "route_accuracy": sum(row["route_match"] for row in semantic) / len(semantic),
        "semantic_task_success": sum(row["task_success"] for row in semantic) / len(semantic),
        "workflow_control_pass_rate": sum(row["passed"] for row in controls) / len(controls),
        "recoverable_failure_pass_rate": sum(
            row["passed"] and row["control"] in {"transient_delivery_retry", "expired_claim_recovery"}
            for row in controls
        ) / 2,
        "median_processing_seconds": statistics.median(latencies),
        "p95_processing_seconds": percentile(latencies, 0.95),
        "hosted_or_paid_ai_calls": 0,
    }
    targets = contract["acceptance_targets"]
    summary["gate_passed"] = (
        summary["classification_macro_f1"] >= targets["classification_macro_f1_min"]
        and summary["required_field_accuracy"] >= targets["required_field_accuracy_min"]
        and summary["retrieval_recall_at_3"] >= targets["retrieval_recall_at_3_min"]
        and summary["citation_validity"] == targets["citation_validity_required"]
        and summary["route_accuracy"] >= targets["route_accuracy_min"]
        and summary["semantic_task_success"] >= targets["semantic_task_success_min"]
        and summary["workflow_control_pass_rate"] == targets["workflow_control_pass_rate_required"]
        and summary["recoverable_failure_pass_rate"] == targets["recoverable_failure_pass_rate_required"]
    )
    return summary


def load_resumable_evidence(
    corpus_hash: str,
    contract: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], str, str] | None:
    path = EVIDENCE_DIRECTORY / EVIDENCE_NAME
    if not path.exists():
        return None
    raw = path.read_bytes()
    partial_sha256 = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw)
    require(
        payload.get("status") == "USER_EXECUTED_PARTIAL_EVALUATION",
        "existing evidence is not a resumable partial evaluation",
    )
    require(payload.get("contract_version") == contract["contract_version"], "partial contract version changed")
    require(payload.get("corpus_sha256") == corpus_hash, "partial corpus hash changed")
    require(payload.get("acceptance_targets") == contract["acceptance_targets"], "partial acceptance targets changed")
    require(
        payload.get("accepted_models")
        == {
            "analysis": f"qwen3:4b-instruct@{ANALYSIS_IDENTIFIER}",
            "embedding": f"qwen3-embedding:0.6b@{EMBEDDING_IDENTIFIER}",
        },
        "partial accepted-model evidence changed",
    )
    boundary = payload.get("completed_boundary")
    if boundary == "STARTED":
        return None
    require(boundary == "SEMANTIC_AND_RETRIEVAL", "partial completion boundary is invalid")

    semantic = payload.get("semantic_results")
    require(isinstance(semantic, list) and len(semantic) == 40, "partial semantic evidence is incomplete")
    require(payload.get("workflow_control_results") == [], "partial evidence already contains workflow controls")
    expected_cases = contract["semantic_cases"]
    required_result_keys = {
        "case_id", "language", "expected_request_type", "predicted_request_type",
        "classification_match", "field_checks", "expected_state", "final_state",
        "route_match", "retrieved_chunk_ids", "citation_ids", "retrieval_hit_at_3",
        "citation_valid", "task_success", "processing_seconds", "analysis_status",
        "analysis_wall_time_ms", "input_tokens", "output_tokens", "proposal", "evidence",
    }
    for expected_case, result in zip(expected_cases, semantic, strict=True):
        expected = expected_case["expected"]
        require(isinstance(result, dict) and required_result_keys.issubset(result), "partial result shape changed")
        require(result["case_id"] == expected_case["case_id"], "partial result order or identity changed")
        require(result["language"] == expected_case["language"], "partial result language changed")
        require(result["expected_request_type"] == expected["request_type"], "partial expected type changed")
        require(result["expected_state"] == expected["state"], "partial expected state changed")
        require(
            isinstance(result["field_checks"], dict)
            and set(result["field_checks"]) == REQUIRED_FIELDS[expected["request_type"]]
            and all(isinstance(value, bool) for value in result["field_checks"].values()),
            "partial field-check evidence changed",
        )
        require(
            result["classification_match"]
            is (result["predicted_request_type"] == expected["request_type"]),
            "partial classification evidence is inconsistent",
        )
        require(result["route_match"] is (result["final_state"] == expected["state"]), "partial route evidence is inconsistent")
        require(
            isinstance(result["retrieved_chunk_ids"], list)
            and all(isinstance(value, str) for value in result["retrieved_chunk_ids"])
            and isinstance(result["citation_ids"], list)
            and all(isinstance(value, str) for value in result["citation_ids"]),
            "partial citation evidence changed",
        )
        retrieval_expected = bool(expected["retrieval_relevant"])
        require(
            (isinstance(result["retrieval_hit_at_3"], bool) and isinstance(result["citation_valid"], bool))
            if retrieval_expected
            else (result["retrieval_hit_at_3"] is None and result["citation_valid"] is None),
            "partial retrieval evidence is inconsistent",
        )
        expected_success = (
            result["classification_match"]
            and all(result["field_checks"].values())
            and result["route_match"]
        )
        if retrieval_expected:
            expected_success = (
                expected_success
                and result["retrieval_hit_at_3"]
                and result["citation_valid"]
            )
        require(result["task_success"] is expected_success, "partial task-success evidence is inconsistent")
        require(
            isinstance(result["processing_seconds"], (int, float))
            and not isinstance(result["processing_seconds"], bool)
            and math.isfinite(float(result["processing_seconds"]))
            and result["processing_seconds"] >= 0,
            "partial latency evidence is invalid",
        )
        for counter in ("analysis_wall_time_ms", "input_tokens", "output_tokens"):
            require(
                isinstance(result[counter], int)
                and not isinstance(result[counter], bool)
                and result[counter] >= 0,
                f"partial {counter} evidence is invalid",
            )
        require(isinstance(result["proposal"], dict) and isinstance(result["evidence"], list), "partial proposal evidence changed")

    summary = payload.get("summary")
    require(
        isinstance(summary, dict)
        and summary.get("semantic_cases") == 40
        and summary.get("workflow_control_cases") == 0,
        "partial summary count changed",
    )
    runtime_metrics = {
        "cold_policy_indexing_seconds": summary.get("cold_policy_indexing_seconds"),
        "policy_query_embedding_batch_seconds": summary.get("policy_query_embedding_batch_seconds"),
        "local_ollama_calls": summary.get("local_ollama_calls"),
    }
    for name in ("cold_policy_indexing_seconds", "policy_query_embedding_batch_seconds"):
        value = runtime_metrics[name]
        require(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and value >= 0,
            f"partial {name} evidence is invalid",
        )
    require(
        isinstance(runtime_metrics["local_ollama_calls"], int)
        and not isinstance(runtime_metrics["local_ollama_calls"], bool)
        and 41 <= runtime_metrics["local_ollama_calls"] <= 50,
        "partial local-call count is invalid",
    )
    started_at = payload.get("run_started_at_utc")
    require(isinstance(started_at, str) and started_at.strip(), "partial start timestamp is missing")
    return semantic, runtime_metrics, started_at, partial_sha256


def write_partial_evidence(
    corpus_hash: str,
    contract: dict[str, Any],
    semantic: list[dict[str, Any]],
    runtime_metrics: dict[str, Any],
    completed_boundary: str,
) -> str:
    EVIDENCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "USER_EXECUTED_PARTIAL_EVALUATION",
        "contract_version": contract["contract_version"],
        "run_started_at_utc": RUN_STARTED_AT_UTC,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "corpus_file": "evaluation/locked_system_evaluation_v1.json",
        "corpus_sha256": corpus_hash,
        "accepted_models": {
            "analysis": f"qwen3:4b-instruct@{ANALYSIS_IDENTIFIER}",
            "embedding": f"qwen3-embedding:0.6b@{EMBEDDING_IDENTIFIER}",
        },
        "acceptance_targets": contract["acceptance_targets"],
        "completed_boundary": completed_boundary,
        "summary": {
            "semantic_cases": len(semantic),
            "workflow_control_cases": 0,
            **runtime_metrics,
        },
        "semantic_results": semantic,
        "workflow_control_results": [],
    }
    (EVIDENCE_DIRECTORY / EVIDENCE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return EVIDENCE_NAME


def write_evidence(
    corpus_hash: str,
    contract: dict[str, Any],
    semantic: list[dict[str, Any]],
    controls: list[dict[str, Any]],
    summary: dict[str, Any],
    semantic_run_started_at_utc: str,
    resumed_from_partial: bool,
    partial_evidence_sha256: str | None,
) -> str:
    EVIDENCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "USER_EXECUTED_CONTROLLED_EVALUATION",
        "contract_version": contract["contract_version"],
        "run_started_at_utc": semantic_run_started_at_utc,
        "control_resume_started_at_utc": (
            RUN_STARTED_AT_UTC if resumed_from_partial else None
        ),
        "execution_segments": 2 if resumed_from_partial else 1,
        "resumed_partial_evidence_sha256": partial_evidence_sha256,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "corpus_file": "evaluation/locked_system_evaluation_v1.json",
        "corpus_sha256": corpus_hash,
        "accepted_models": {
            "analysis": f"qwen3:4b-instruct@{ANALYSIS_IDENTIFIER}",
            "embedding": f"qwen3-embedding:0.6b@{EMBEDDING_IDENTIFIER}",
        },
        "acceptance_targets": contract["acceptance_targets"],
        "summary": summary,
        "semantic_results": semantic,
        "workflow_control_results": controls,
    }
    (EVIDENCE_DIRECTORY / EVIDENCE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return EVIDENCE_NAME


print("AI Service Request Automation - locked 50-case system evaluation")
print("Scope: 40 new semantic cases; 10 workflow controls; installed local models only.")
print("No model download and no hosted or paid AI call.")
print()

contract, corpus_hash = load_contract()
print("[1/5] Locked post-selection corpus and fixed acceptance targets: PASS")
resumable = load_resumable_evidence(corpus_hash, contract)
resumed_from_partial = resumable is not None
if resumable is None:
    semantic_run_started_at_utc = RUN_STARTED_AT_UTC
    partial_evidence_sha256 = None
    write_partial_evidence(corpus_hash, contract, [], {}, "STARTED")
    analysis_provider = OllamaAnalysisProvider(
        base_url=OLLAMA_URL,
        model_identifier=ANALYSIS_IDENTIFIER,
    )
    policy_provider = OllamaPolicyProvider(
        base_url=OLLAMA_URL,
        embedding_identifier=f"qwen3-embedding:0.6b@{EMBEDDING_IDENTIFIER}",
        answer_identifier=f"qwen3:4b-instruct@{ANALYSIS_IDENTIFIER}",
    )
    expected_policy_ids = {
        citation
        for case in contract["semantic_cases"]
        for citation in case["expected"]["retrieval_relevant"]
    }
    semantic_results, runtime_metrics, indexed_ids = evaluate_semantic_cases(
        contract["semantic_cases"], analysis_provider, policy_provider
    )
    require(expected_policy_ids.issubset(indexed_ids), "evaluation policy citations were not indexed")
    write_partial_evidence(
        corpus_hash,
        contract,
        semantic_results,
        runtime_metrics,
        "SEMANTIC_AND_RETRIEVAL",
    )
    print("[2/5] Forty bilingual semantic cases completed through deterministic validation: PASS")
    print("[3/5] Eight policy-case outcomes completed for fixed-gate scoring: PASS")
else:
    (
        semantic_results,
        runtime_metrics,
        semantic_run_started_at_utc,
        partial_evidence_sha256,
    ) = resumable
    print("[2/5] Preserved 40-case semantic evidence validated; model rerun skipped: PASS")
    print("[3/5] Preserved 8 policy-case outcomes validated for fixed-gate scoring: PASS")

policy_results = [
    row for row in semantic_results
    if row["expected_request_type"] == "policy_question"
]
require(
    len(policy_results) == 8
    and all(isinstance(row["retrieval_hit_at_3"], bool) for row in policy_results),
    "the 8 policy-case outcomes are incomplete",
)

control_results = run_workflow_controls(contract["workflow_cases"])
aggregate = aggregate_evidence(10 if resumed_from_partial else 50)
print("[4/5] Ten deterministic workflow-control cases completed: PASS")

summary = summarize(contract, semantic_results, control_results)
summary.update(runtime_metrics)
summary.update(aggregate)
summary["execution_segments"] = 2 if resumed_from_partial else 1
summary["resume_model_calls"] = 0
evidence_name = write_evidence(
    corpus_hash,
    contract,
    semantic_results,
    control_results,
    summary,
    semantic_run_started_at_utc,
    resumed_from_partial,
    partial_evidence_sha256,
)
print("[5/5] Metrics, fixed gates, and reproducible evidence completed: PASS")
print("Locked 50-case evaluation summary")
print(f"  Evaluation cases: {summary['semantic_cases'] + summary['workflow_control_cases']}/50")
print(f"  Classification macro F1: {summary['classification_macro_f1']:.1%}")
print(f"  Required-field accuracy: {summary['required_field_accuracy']:.1%}")
print(f"  Policy retrieval Recall@3: {summary['retrieval_recall_at_3']:.1%}")
print(f"  Citation validity: {summary['citation_validity']:.1%}")
print(f"  Route and final-state accuracy: {summary['route_accuracy']:.1%}")
print(f"  Semantic task success: {summary['semantic_task_success']:.1%}")
print(f"  Workflow controls: {summary['workflow_control_pass_rate']:.1%}")
print(f"  Recoverable failures: {summary['recoverable_failure_pass_rate']:.1%}")
print(f"  Median processing latency: {summary['median_processing_seconds']:.3f}s")
print(f"  P95 processing latency: {summary['p95_processing_seconds']:.3f}s")
print(f"  Cold policy indexing: {summary['cold_policy_indexing_seconds']:.3f}s")
print(f"  Local Ollama calls: {summary['local_ollama_calls']}")
if resumed_from_partial:
    print("  Additional Ollama calls during resume: 0")
print("  Hosted or paid AI calls: 0")
print(f"  Locked evaluation gate: {'PASS' if summary['gate_passed'] else 'CHECK'}")
print(f"  Evidence file: {evidence_name}")
