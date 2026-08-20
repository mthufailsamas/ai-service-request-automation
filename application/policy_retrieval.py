"""Access-filtered policy retrieval and grounded-answer persistence."""

from __future__ import annotations

import hashlib
import json
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


EMBEDDING_MODEL = "qwen3-embedding:0.6b"
ANSWER_MODEL = "qwen3:4b-instruct"
VECTOR_DIMENSIONS = 1024
TOP_K = 3
MAX_QUERY_CHARACTERS = 12_000
MAX_ANSWER_CHARACTERS = 4_000
RETRIEVAL_INSTRUCTION = (
    "Given a service-request policy question, retrieve the relevant policy or "
    "procedure passage that answers the request."
)


class RetrievalNotFound(Exception):
    pass


class RetrievalConflict(Exception):
    pass


class RetrievalInProgress(Exception):
    pass


class RetrievalProviderError(Exception):
    pass


@dataclass(frozen=True)
class RetrievedChunk:
    citation_id: str
    policy_code: str
    title: str
    visibility: str
    text: str
    distance: float


@dataclass(frozen=True)
class GroundedAnswer:
    answer: str
    citation_ids: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalExecution:
    case_reference: str
    outcome: str
    current_state: str
    case_version: int
    answer: str | None
    citation_ids: tuple[str, ...]
    retrieved_chunk_ids: tuple[str, ...]
    idempotent_replay: bool
    provider_called: bool


class PolicyProvider(Protocol):
    embedding_identifier: str
    answer_identifier: str

    def embed(self, text: str) -> list[float]: ...

    def answer(self, query: str, chunks: tuple[RetrievedChunk, ...]) -> GroundedAnswer: ...


class FixturePolicyProvider:
    """Small deterministic provider used only by focused integration tests."""

    def __init__(
        self,
        *,
        vector: list[float],
        answer: str,
        citation_ids: tuple[str, ...],
        delay_ms: int = 0,
    ) -> None:
        self.embedding_identifier = "database-test-fixture-1024d-v1"
        self.answer_identifier = "fixture-policy-answer-v1"
        self._vector = vector
        self._answer = answer
        self._citation_ids = citation_ids
        self._delay_ms = delay_ms
        self._lock = threading.Lock()
        self.embed_calls = 0
        self.answer_calls = 0

    def embed(self, text: str) -> list[float]:
        with self._lock:
            self.embed_calls += 1
        if self._delay_ms:
            time.sleep(self._delay_ms / 1_000)
        return list(self._vector)

    def answer(self, query: str, chunks: tuple[RetrievedChunk, ...]) -> GroundedAnswer:
        with self._lock:
            self.answer_calls += 1
        return GroundedAnswer(self._answer, self._citation_ids)


class OllamaPolicyProvider:
    """Use only the accepted local embedding and answer models."""

    def __init__(
        self,
        *,
        base_url: str,
        embedding_identifier: str,
        answer_identifier: str,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}
            or parsed.port != 11434
            or parsed.path not in {"", "/"}
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RetrievalProviderError("Ollama must be local HTTP port 11434")
        if not embedding_identifier or not answer_identifier:
            raise RetrievalProviderError("accepted model identifiers are required")
        if not all(
            len(value) >= 12
            and len(value) <= 100
            and all(character.isalnum() or character in ":@._-" for character in value)
            for value in (embedding_identifier, answer_identifier)
        ):
            raise RetrievalProviderError("accepted model identifiers are invalid")
        self.base_url = base_url.rstrip("/")
        self.embedding_identifier = embedding_identifier
        self.answer_identifier = answer_identifier
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, OSError) as error:
            raise RetrievalProviderError("local Ollama policy call failed") from error
        if len(raw) > 1024 * 1024:
            raise RetrievalProviderError("local Ollama response exceeded 1 MiB")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RetrievalProviderError("local Ollama returned invalid JSON") from error
        if not isinstance(value, dict):
            raise RetrievalProviderError("local Ollama response was not an object")
        return value

    def embed(self, text: str) -> list[float]:
        response = self._post(
            "/api/embed",
            {"model": EMBEDDING_MODEL, "input": [text], "truncate": False, "keep_alive": 0},
            180,
        )
        embeddings = response.get("embeddings")
        if (
            response.get("model") != EMBEDDING_MODEL
            or not isinstance(embeddings, list)
            or len(embeddings) != 1
            or not isinstance(embeddings[0], list)
        ):
            raise RetrievalProviderError("local Ollama embedding shape was invalid")
        return embeddings[0]

    def answer(self, query: str, chunks: tuple[RetrievedChunk, ...]) -> GroundedAnswer:
        context = "\n\n".join(
            f"[{chunk.citation_id}] {chunk.text}" for chunk in chunks
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer", "citation_ids"],
            "properties": {
                "answer": {"type": "string", "minLength": 1, "maxLength": MAX_ANSWER_CHARACTERS},
                "citation_ids": {"type": "array", "minItems": 0, "maxItems": TOP_K, "uniqueItems": True, "items": {"type": "string"}},
            },
        }
        response = self._post(
            "/api/chat",
            {
                "model": ANSWER_MODEL,
                "messages": [
                    {"role": "system", "content": "Answer only from the supplied policy chunks. Cite only their bracketed IDs. If context is insufficient, return an empty citation_ids list."},
                    {"role": "user", "content": f"Question:\n{query}\n\nPolicy chunks:\n{context}"},
                ],
                "stream": False,
                "format": schema,
                "think": False,
                "keep_alive": "10m",
                "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 384},
            },
            180,
        )
        try:
            if response.get("model") != ANSWER_MODEL or response.get("done") is not True:
                raise TypeError("answer model identity or completion marker changed")
            content = json.loads(response["message"]["content"])
            return GroundedAnswer(content["answer"], tuple(content["citation_ids"]))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise RetrievalProviderError("local Ollama answer shape was invalid") from error


def _vector_literal(vector: list[float]) -> str:
    if (
        len(vector) != VECTOR_DIMENSIONS
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)
        or math.sqrt(sum(float(value) ** 2 for value in vector)) == 0
    ):
        raise RetrievalProviderError("embedding must be finite, non-zero, and 1,024-dimensional")
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


def _visibility(roles: list[str]) -> tuple[str, ...]:
    allowed = {"ALL_EMPLOYEES"}
    if "SERVICE_AGENT" in roles:
        allowed.add("SERVICE_AGENTS")
    if "APPROVER" in roles:
        allowed.add("APPROVERS")
    if "ADMIN" in roles:
        allowed.update({"SERVICE_AGENTS", "APPROVERS", "ADMINS"})
    return tuple(sorted(allowed))


def _execution(payload: dict[str, Any], replay: bool, called: bool) -> RetrievalExecution:
    return RetrievalExecution(
        case_reference=payload["case_reference"], outcome=payload["outcome"],
        current_state=payload["current_state"], case_version=payload["case_version"],
        answer=payload.get("answer"), citation_ids=tuple(payload.get("citation_ids", [])),
        retrieved_chunk_ids=tuple(payload.get("retrieved_chunk_ids", [])),
        idempotent_replay=replay, provider_called=called,
    )


def retrieve_policy(
    database_url: str,
    provider: PolicyProvider,
    *,
    case_id: UUID,
    case_reference: str,
    expected_case_version: int,
    analysis_run_id: UUID,
) -> RetrievalExecution:
    """Claim once, call outside the transaction, and persist 1 safe outcome."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        case = connection.execute(
            """
            SELECT c.*, d.policy_question, u.is_active AS requester_is_active
            FROM cases c
            JOIN users u ON u.user_id = c.requester_id
            LEFT JOIN case_details d USING (case_id)
            WHERE c.case_id = %s
            FOR UPDATE OF c
            """,
            (case_id,),
        ).fetchone()
        if case is None:
            raise RetrievalNotFound
        if case["case_reference"] != case_reference:
            raise RetrievalConflict("case reference does not match")
        terminal = connection.execute(
            """
            SELECT event_payload FROM case_events
            WHERE case_id=%s AND event_type IN ('POLICY_RETRIEVAL_READY','POLICY_RETRIEVAL_NEEDS_REVIEW')
              AND event_payload->>'analysis_run_id'=%s
            ORDER BY sequence_number DESC LIMIT 1
            """, (case_id, str(analysis_run_id)),
        ).fetchone()
        if terminal:
            response = terminal["event_payload"].get("response", {})
            if (
                response.get("case_reference") != case_reference
                or response.get("case_version") != expected_case_version + 1
            ):
                raise RetrievalConflict("replay command does not match the terminal retrieval")
            return _execution(response, True, False)
        started = connection.execute(
            "SELECT event_payload FROM case_events WHERE case_id=%s AND event_type='POLICY_RETRIEVAL_STARTED' AND event_payload->>'analysis_run_id'=%s",
            (case_id, str(analysis_run_id)),
        ).fetchone()
        if started:
            if started["event_payload"].get("expected_case_version") != expected_case_version:
                raise RetrievalConflict("replay version does not match the retrieval claim")
            raise RetrievalInProgress
        active_claim = connection.execute(
            """
            SELECT started.event_payload
            FROM case_events AS started
            WHERE started.case_id=%s
              AND started.event_type='POLICY_RETRIEVAL_STARTED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM case_events AS terminal
                  WHERE terminal.case_id=started.case_id
                    AND terminal.event_type IN ('POLICY_RETRIEVAL_READY','POLICY_RETRIEVAL_NEEDS_REVIEW')
                    AND terminal.event_payload->>'analysis_run_id'
                        = started.event_payload->>'analysis_run_id'
              )
            ORDER BY started.sequence_number DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        if active_claim:
            raise RetrievalInProgress
        valid = connection.execute(
            """
            SELECT 1 FROM validation_runs v JOIN ai_analysis_runs a USING (analysis_run_id)
            WHERE v.analysis_run_id=%s AND v.case_id=%s AND v.overall_decision='READY'
              AND a.status='COMPLETED'
            """, (analysis_run_id, case_id),
        ).fetchone()
        if (
            case["current_state"] != "ANALYZING"
            or case["version"] != expected_case_version or case["request_type"] != "POLICY_QUESTION"
            or case["requester_is_active"] is not True
            or not isinstance(case["policy_question"], str) or not case["policy_question"].strip()
            or not valid
        ):
            raise RetrievalConflict("case state or accepted analysis evidence is incompatible")
        query = f"Subject: {case['subject']}\nMessage: {case['original_message']}\nChecked question: {case['policy_question']}"
        if len(query) > MAX_QUERY_CHARACTERS:
            raise RetrievalConflict("retrieval query exceeds the fixed character limit")
        sequence = connection.execute("SELECT COALESCE(max(sequence_number),0)+1 AS n FROM case_events WHERE case_id=%s", (case_id,)).fetchone()["n"]
        connection.execute(
            """INSERT INTO case_events(case_id,sequence_number,from_state,to_state,event_type,actor_type,reason,event_payload)
               VALUES(%s,%s,'ANALYZING','ANALYZING','POLICY_RETRIEVAL_STARTED','SYSTEM',%s,%s)""",
            (
                case_id,
                sequence,
                "Policy retrieval was durably claimed.",
                Jsonb(
                    {
                        "analysis_run_id": str(analysis_run_id),
                        "case_reference": case_reference,
                        "expected_case_version": expected_case_version,
                    }
                ),
            ),
        )
        roles = [
            row["role_code"]
            for row in connection.execute(
                "SELECT role_code FROM user_roles WHERE user_id=%s",
                (case["requester_id"],),
            ).fetchall()
        ]
        allowed_visibility = _visibility(roles)

    outcome = "NEEDS_REVIEW"
    answer_text: str | None = None
    citations: tuple[str, ...] = ()
    chunks: tuple[RetrievedChunk, ...] = ()
    reason = "Policy retrieval could not produce verified grounded evidence."
    embedding_query = f"Instruct: {RETRIEVAL_INSTRUCTION}\nQuery: {query}"
    try:
        vector = _vector_literal(provider.embed(embedding_query))
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """
                SELECT d.policy_code || '#' || c.chunk_number AS citation_id,
                       d.policy_code,d.title,d.visibility,c.chunk_text,
                       c.embedding <=> %s::vector AS distance
                FROM policy_chunks c JOIN policy_documents d USING(policy_document_id)
                WHERE c.embedding_model=%s AND d.is_active AND d.valid_from<=now()
                  AND (d.valid_until IS NULL OR d.valid_until>now()) AND d.visibility=ANY(%s)
                ORDER BY c.embedding <=> %s::vector, d.policy_code, c.chunk_number LIMIT 3
                """, (vector, provider.embedding_identifier, list(allowed_visibility), vector),
            ).fetchall()
        chunks = tuple(RetrievedChunk(row["citation_id"], row["policy_code"], row["title"], row["visibility"], row["chunk_text"], float(row["distance"])) for row in rows)
        proposed = provider.answer(query, chunks) if chunks else GroundedAnswer("", ())
        retrieved_ids = tuple(chunk.citation_id for chunk in chunks)
        if (
            isinstance(proposed.answer, str) and 0 < len(proposed.answer.strip()) <= MAX_ANSWER_CHARACTERS
            and proposed.citation_ids and len(proposed.citation_ids) <= TOP_K
            and all(isinstance(value, str) and 0 < len(value) <= 100 for value in proposed.citation_ids)
            and len(set(proposed.citation_ids)) == len(proposed.citation_ids)
            and set(proposed.citation_ids).issubset(retrieved_ids)
        ):
            outcome, answer_text, citations = "READY", proposed.answer.strip(), proposed.citation_ids
            reason = "The policy answer cites only retrieved visible policy chunks."
    except (RetrievalProviderError, TypeError, ValueError, AttributeError):
        pass

    target = "READY_FOR_ACTION" if outcome == "READY" else "NEEDS_REVIEW"
    event_type = "POLICY_RETRIEVAL_READY" if outcome == "READY" else "POLICY_RETRIEVAL_NEEDS_REVIEW"
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        locked = connection.execute("SELECT current_state,version FROM cases WHERE case_id=%s FOR UPDATE", (case_id,)).fetchone()
        if locked is None or locked["current_state"] != "ANALYZING" or locked["version"] != expected_case_version:
            raise RetrievalConflict("case changed before retrieval finalization")
        new_version = expected_case_version + 1
        response = {
            "schema_version": "1", "case_reference": case_reference, "outcome": outcome,
            "current_state": target, "case_version": new_version, "answer": answer_text,
            "citation_ids": list(citations), "retrieved_chunk_ids": [c.citation_id for c in chunks],
        }
        sequence = connection.execute("SELECT COALESCE(max(sequence_number),0)+1 AS n FROM case_events WHERE case_id=%s", (case_id,)).fetchone()["n"]
        connection.execute("UPDATE cases SET current_state=%s,version=%s,updated_at=now() WHERE case_id=%s", (target, new_version, case_id))
        connection.execute(
            """INSERT INTO case_events(case_id,sequence_number,from_state,to_state,event_type,actor_type,reason,event_payload)
               VALUES(%s,%s,'ANALYZING',%s,%s,'SYSTEM',%s,%s)""",
            (
                case_id,
                sequence,
                target,
                event_type,
                reason,
                Jsonb(
                    {
                        "analysis_run_id": str(analysis_run_id),
                        "query_sha256": hashlib.sha256(
                            embedding_query.encode("utf-8")
                        ).hexdigest(),
                        "allowed_visibility": list(allowed_visibility),
                        "embedding_identifier": provider.embedding_identifier,
                        "answer_identifier": provider.answer_identifier,
                        "retrieval": [
                            {
                                "citation_id": chunk.citation_id,
                                "cosine_distance": round(chunk.distance, 6),
                            }
                            for chunk in chunks
                        ],
                        "response": response,
                    }
                ),
            ),
        )
    return _execution(response, False, True)
