"""Focused policy-retrieval application integration and local-model smoke."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from policy_retrieval import (
    FixturePolicyProvider,
    OllamaPolicyProvider,
    RetrievalConflict,
    RetrievalInProgress,
    retrieve_policy,
)


def concise_exception_hook(kind: type[BaseException], error: BaseException, tb: Any) -> None:
    frames = traceback.extract_tb(tb)
    location = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "unknown"
    print(f"FAIL: {kind.__name__}: {error} [{location}]", file=sys.stderr)


sys.excepthook = concise_exception_hook
DATABASE_URL = os.environ["PRIMARY_DATABASE_URL"]
API_URL = os.environ["PRIMARY_API_URL"].rstrip("/")
WORKFLOW_TOKEN = os.environ["PRIMARY_WORKFLOW_TOKEN"]
OLLAMA_URL = os.environ.get("POLICY_OLLAMA_BASE_URL", "http://host.docker.internal:11434")
EMBEDDING_ID = os.environ.get("POLICY_EMBEDDING_IDENTIFIER", "ac6da0dfba84")
ANSWER_ID = os.environ.get("POLICY_ANSWER_IDENTIFIER", "0edcdef34593")


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def post(case_id: UUID, body: dict[str, Any], token: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{API_URL}/internal/v1/cases/{case_id}/policy-retrieval",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def make_case(reference_number: int, requester: str, question: str) -> dict[str, Any]:
    case_id, analysis_id = uuid4(), uuid4()
    case_reference = f"CASE-2026-{reference_number:04d}"
    external = f"POLICY-RETRIEVAL-{reference_number:04d}"
    digest = hashlib.sha256(external.encode()).hexdigest()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        requester_id = connection.execute(
            "SELECT user_id FROM users WHERE employee_reference=%s", (requester,)
        ).fetchone()["user_id"]
        connection.execute(
            """
            INSERT INTO cases(case_id,case_reference,source_channel,external_request_id,
              idempotency_key,content_fingerprint,requester_id,subject,original_message,
              attachment_metadata,request_type,ai_summary,current_state,version,received_at)
            VALUES(%s,%s,'WEBHOOK',%s,%s,%s,%s,%s,%s,'[]','POLICY_QUESTION',%s,'ANALYZING',2,%s)
            """,
            (case_id, case_reference, external, digest, digest, requester_id,
             "Policy guidance request", question, "Checked fictional policy question.",
             datetime.now(timezone.utc) - timedelta(seconds=1)),
        )
        connection.execute(
            """INSERT INTO case_details(case_id,policy_topic,policy_question,accepted_by_type,accepted_at)
               VALUES(%s,'work policy',%s,'SYSTEM_RULE',now())""", (case_id, question)
        )
        connection.execute(
            """INSERT INTO case_events(case_id,sequence_number,from_state,to_state,event_type,actor_type,reason,event_payload)
               VALUES(%s,1,NULL,'RECEIVED','CASE_RECEIVED','INTEGRATION','Fictional retrieval fixture.','{}'),
                     (%s,2,'RECEIVED','ANALYZING','ANALYSIS_STARTED','INTEGRATION','Fictional retrieval fixture.',%s),
                     (%s,3,'ANALYZING','ANALYZING','ANALYSIS_READY_FOR_RETRIEVAL','SYSTEM','Accepted policy analysis.',%s)""",
            (case_id, case_id, Jsonb({"workflow_start_idempotency_key": digest}), case_id, Jsonb({"analysis_run_id": str(analysis_id)})),
        )
        connection.execute(
            """INSERT INTO ai_analysis_runs(analysis_run_id,case_id,model_name,model_identifier,
              prompt_contract_version,input_sha256,proposal,evidence,status,wall_time_ms,
              input_tokens,output_tokens,attempt_number,completed_at)
              VALUES(%s,%s,'fixture-provider','fixture-analysis-v1','ai-analysis-v1',%s,
              '{}','[]','COMPLETED',1,0,0,1,now())""",
            (analysis_id, case_id, digest),
        )
        connection.execute(
            """INSERT INTO validation_runs(case_id,analysis_run_id,overall_decision,missing_fields,rule_results,reason)
              VALUES(%s,%s,'READY','{}',%s,'Policy question passed deterministic analysis.')""",
            (case_id, analysis_id, Jsonb([{"rule_code":"POLICY_FIELDS","outcome":"PASS","field_name":"policy_question","proposed_value":question,"resolved_value":question,"reason":"Checked."}])),
        )
    return {"case_id": case_id, "case_reference": case_reference, "analysis_run_id": analysis_id, "expected_case_version": 2}


def command(case: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version":"1", "case_reference":case["case_reference"], "expected_case_version":2, "analysis_run_id":str(case["analysis_run_id"])}


positive = [0.001] * 1024
split = [0.001] * 512 + [-0.001] * 512
print("AI Service Request Automation - policy-retrieval integration check")
print("Scope: 6 focused groups; fictional data; 1 smallest installed-model smoke.")
print("")

guard = make_case(6001, "EMP-201", "How many remote days are allowed in Jakarta?")
status, body = post(guard["case_id"], command(guard), "wrong-token")
require(status == 401 and body["error_code"] == "AUTHENTICATION_REQUIRED", "invalid authentication was accepted")
status, body = post(guard["case_id"], {**command(guard), "extra": True}, WORKFLOW_TOKEN)
require(status == 422, "an extra command field was accepted")
provider = FixturePolicyProvider(vector=positive, answer="unused", citation_ids=("POL-REMOTE-01#0",))
try:
    retrieve_policy(DATABASE_URL, provider, case_id=guard["case_id"], case_reference=guard["case_reference"], expected_case_version=9, analysis_run_id=guard["analysis_run_id"])
    raise AssertionError("an invalid version was accepted")
except RetrievalConflict:
    pass
require(provider.embed_calls == 0, "a rejected command called the provider")
print("[1/6] Authentication, exact command, state, and no-call guards: PASS")

status, ready = post(guard["case_id"], command(guard), WORKFLOW_TOKEN)
require(status == 200 and ready["outcome"] == "READY" and ready["current_state"] == "READY_FOR_ACTION", f"grounded endpoint failed: {ready}")
require(ready["citation_ids"] == ["POL-REMOTE-01#0"] and ready["retrieved_chunk_ids"] == ["POL-REMOTE-01#0"], "grounded citation evidence changed")
status, mismatch = post(guard["case_id"], {**command(guard), "expected_case_version": 9}, WORKFLOW_TOKEN)
require(status == 409 and mismatch["error_code"] == "POLICY_RETRIEVAL_CONFLICT", "a mismatched terminal replay was accepted")
print("[2/6] Exact top-3 retrieval and grounded visible citation: PASS")

restricted = make_case(6002, "EMP-201", "What does temporary administrator access require?")
restricted_provider = FixturePolicyProvider(vector=split, answer="Privileged access requires approvals.", citation_ids=("POL-PRIVILEGED-01#0",))
restricted_result = retrieve_policy(DATABASE_URL, restricted_provider, **restricted)
require(restricted_result.outcome == "NEEDS_REVIEW" and "POL-PRIVILEGED-01#0" not in restricted_result.retrieved_chunk_ids, "requester visibility leaked privileged policy")
agent = make_case(6003, "AGT-301", "What does temporary administrator access require?")
agent_provider = FixturePolicyProvider(vector=split, answer="It requires service-owner and Information Security approval.", citation_ids=("POL-PRIVILEGED-01#0",))
agent_result = retrieve_policy(DATABASE_URL, agent_provider, **agent)
require(agent_result.outcome == "READY" and agent_result.citation_ids == ("POL-PRIVILEGED-01#0",), "service-agent visibility did not permit the matching policy")
print("[3/6] Role visibility prevents leakage and permits authorized retrieval: PASS")

invalid = make_case(6004, "EMP-201", "How many remote days are allowed in Jakarta?")
invalid_provider = FixturePolicyProvider(vector=positive, answer="Unsupported answer.", citation_ids=("POL-NOT-RETRIEVED#0",))
invalid_result = retrieve_policy(DATABASE_URL, invalid_provider, **invalid)
require(invalid_result.outcome == "NEEDS_REVIEW" and invalid_result.answer is None and invalid_result.citation_ids == (), "invalid citations were accepted")
print("[4/6] Invalid or invisible citations route safely to review: PASS")

concurrent_case = make_case(6005, "EMP-201", "How many remote days are allowed in Jakarta?")
delayed = FixturePolicyProvider(vector=positive, answer="Up to 2 days per week.", citation_ids=("POL-REMOTE-01#0",), delay_ms=300)
def invoke() -> Any:
    try:
        return retrieve_policy(DATABASE_URL, delayed, **concurrent_case)
    except RetrievalInProgress:
        return "IN_PROGRESS"
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    outcomes = list(executor.map(lambda _: invoke(), range(2)))
require(delayed.embed_calls == 1 and delayed.answer_calls == 1, f"concurrency made duplicate provider calls: {outcomes}")
replay = retrieve_policy(DATABASE_URL, delayed, **concurrent_case)
require(replay.idempotent_replay and not replay.provider_called and delayed.embed_calls == 1, "exact replay called the provider")
print("[5/6] Concurrent claim and exact replay use 1 provider call: PASS")

ollama = OllamaPolicyProvider(base_url=OLLAMA_URL, embedding_identifier=f"qwen3-embedding:0.6b@{EMBEDDING_ID}", answer_identifier=f"qwen3:4b-instruct@{ANSWER_ID}")
with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=10) as response:
    installed = json.loads(response.read()).get("models", [])
installed_by_name = {item.get("name"): item for item in installed if isinstance(item, dict)}
for model_name, model_id in (("qwen3-embedding:0.6b", EMBEDDING_ID), ("qwen3:4b-instruct", ANSWER_ID)):
    record = installed_by_name.get(model_name) or installed_by_name.get(f"{model_name}:latest")
    require(record is not None and str(record.get("digest", "")).startswith(model_id), f"accepted installed model is missing or changed: {model_name}")
local_text = "Jakarta employees may work remotely for up to 2 days in a calendar week after agreeing each remote day with their line manager."
local_vector = ollama.embed(local_text)
vector_literal = "[" + ",".join(format(float(value), ".9g") for value in local_vector) + "]"
with psycopg.connect(DATABASE_URL) as connection:
    document_id = uuid4()
    connection.execute("INSERT INTO policy_documents(policy_document_id,policy_code,title,visibility,version,content_sha256,is_active,valid_from) VALUES(%s,'POL-LOCAL-REMOTE-01','Local remote work smoke','ALL_EMPLOYEES',1,%s,true,now()-interval '1 day')", (document_id, hashlib.sha256(local_text.encode()).hexdigest()))
    connection.execute("INSERT INTO policy_chunks(policy_document_id,chunk_number,chunk_text,token_count,embedding_model,embedding) VALUES(%s,0,%s,24,%s,%s::vector)", (document_id, local_text, ollama.embedding_identifier, vector_literal))
local_case = make_case(6006, "EMP-201", "How many remote days may Jakarta employees take each week?")
local_result = retrieve_policy(DATABASE_URL, ollama, **local_case)
require(local_result.outcome == "READY" and local_result.citation_ids == ("POL-LOCAL-REMOTE-01#0",), f"local-model grounded smoke failed: {local_result}")
require(
    local_result.answer is not None
    and ("2" in local_result.answer or "two" in local_result.answer.lower()),
    f"local-model answer did not preserve the grounded day limit: {local_result.answer!r}",
)

with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
    aggregate = connection.execute("""
      SELECT
        (SELECT count(*) FROM cases WHERE external_request_id LIKE 'POLICY-RETRIEVAL-%') AS cases,
        (SELECT count(*) FROM case_events WHERE event_type='POLICY_RETRIEVAL_STARTED') AS starts,
        (SELECT count(*) FROM case_events WHERE event_type IN ('POLICY_RETRIEVAL_READY','POLICY_RETRIEVAL_NEEDS_REVIEW')) AS finals,
        (SELECT count(*) FROM approvals) AS approvals,
        (SELECT count(*) FROM outbox_messages WHERE message_type IN ('DOWNSTREAM_ACTION','REQUESTER_NOTIFICATION')) AS deferred_outbox
    """).fetchone()
require(aggregate["cases"] == 6 and aggregate["starts"] == 6 and aggregate["finals"] == 6, f"aggregate retrieval evidence changed: {dict(aggregate)}")
require(aggregate["approvals"] == 0 and aggregate["deferred_outbox"] == 0, "a deferred capability changed")
print("[6/6] Installed-model smoke, aggregate evidence, and isolation: PASS")
print("Policy-retrieval integration summary")
print("  Integration groups: 6/6 PASS")
print("  Fictional policy cases: 6")
print("  Durable retrieval claims: 6")
print("  Durable terminal outcomes: 6")
print("  Exact no-call replays: 1")
print("  Duplicate provider executions: 0")
print("  Local Ollama calls: 3")
print("  Hosted or paid AI calls: 0")
print("  Policy-retrieval gate: PASS")
