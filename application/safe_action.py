"""Materialize and reconcile deterministic safe outcomes for downstream delivery."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


DOWNSTREAM_DESTINATION = "service-desk-sandbox"
SAFE_REQUEST_TYPES = {"POLICY_QUESTION", "INCIDENT_REPORT", "STATUS_REQUEST"}


class SafeActionNotFound(Exception):
    """The case or downstream action does not exist."""


class SafeActionConflict(Exception):
    """Current deterministic authority does not permit the action."""


class SafeActionNotReady(Exception):
    """The action has no terminal delivery evidence."""


@dataclass(frozen=True)
class SafeActionQueued:
    outbox_message_id: UUID
    case_reference: str
    action_type: str
    idempotent_replay: bool


@dataclass(frozen=True)
class SafeActionReconciliation:
    case_reference: str
    current_state: str
    case_version: int
    downstream_reference: str | None
    idempotent_replay: bool


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SafeActionConflict(f"The accepted {label} is missing.")
    return value.strip()


def _next_sequence(connection: psycopg.Connection[Any], case_id: UUID) -> int:
    return connection.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS value FROM case_events WHERE case_id = %s",
        (case_id,),
    ).fetchone()["value"]


def _idempotency_key(case_id: UUID, source_event_id: int) -> str:
    return hashlib.sha256(
        f"SAFE_DOWNSTREAM_ACTION|{case_id}|{source_event_id}".encode("utf-8")
    ).hexdigest()


def _load_authority(
    connection: psycopg.Connection[Any],
    case_id: UUID,
    *,
    source_event_id: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = connection.execute(
        """
        SELECT cases.case_id, cases.case_reference, cases.subject,
               cases.ai_summary, cases.request_type, cases.requester_id,
               cases.current_state, cases.version,
               requester.is_active AS requester_is_active,
               EXISTS (
                   SELECT 1 FROM user_roles
                   WHERE user_roles.user_id = cases.requester_id
                     AND user_roles.role_code = 'REQUESTER'
               ) AS requester_role_active,
               details.policy_topic, details.policy_question,
               details.affected_system_id, details.incident_description,
               details.impact, details.urgency, details.target_system_id,
               details.requested_access_level, details.business_reason,
               details.approver_user_id, details.record_reference,
               details.requested_changes, details.referenced_case_id,
               details.accepted_by_type, details.accepted_by_user_id,
               affected.system_code AS affected_system_code,
               affected.is_active AS affected_system_active,
               referenced.case_reference AS referenced_case_reference,
               referenced.current_state AS referenced_case_state,
               referenced.requester_id AS referenced_requester_id
        FROM cases
        JOIN users AS requester ON requester.user_id = cases.requester_id
        JOIN case_details AS details USING (case_id)
        LEFT JOIN managed_systems AS affected
          ON affected.system_id = details.affected_system_id
        LEFT JOIN cases AS referenced
          ON referenced.case_id = details.referenced_case_id
        WHERE cases.case_id = %s
        FOR UPDATE OF cases
        """,
        (case_id,),
    ).fetchone()
    if row is None:
        raise SafeActionNotFound("The safe-action case was not found.")
    case = dict(row)
    if (
        case["request_type"] not in SAFE_REQUEST_TYPES
        or case["current_state"] != "READY_FOR_ACTION"
        or case["requester_is_active"] is not True
        or case["requester_role_active"] is not True
    ):
        raise SafeActionConflict("The case is not a current safe-action route.")

    event_types = (
        ["POLICY_RETRIEVAL_READY"]
        if case["request_type"] == "POLICY_QUESTION"
        else ["ANALYSIS_READY", "HUMAN_REVIEW_REANALYZED"]
    )
    source = connection.execute(
        """
        SELECT event_id, event_type, from_state, to_state, actor_type, event_payload
        FROM case_events
        WHERE case_id = %s AND event_type = ANY(%s)
          AND event_id = COALESCE(%s::bigint, event_id)
        ORDER BY event_id DESC
        LIMIT 1
        """,
        (case_id, event_types, source_event_id),
    ).fetchone()
    if (
        source is None
        or source["to_state"] != "READY_FOR_ACTION"
        or source["actor_type"] != "SYSTEM"
    ):
        raise SafeActionConflict("The safe route has no compatible source event.")
    return case, dict(source)


def _require_unrelated_null(case: dict[str, Any], names: tuple[str, ...]) -> None:
    if any(case[name] is not None for name in names):
        raise SafeActionConflict("The safe action contains unrelated accepted data.")


def _build_payload(
    connection: psycopg.Connection[Any],
    case: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    title = _nonblank(case["subject"], "title")
    summary = _nonblank(case["ai_summary"], "summary")
    request_type = case["request_type"]

    if request_type == "POLICY_QUESTION":
        _require_unrelated_null(
            case,
            (
                "affected_system_id", "incident_description", "impact", "urgency",
                "target_system_id", "requested_access_level", "business_reason",
                "approver_user_id", "record_reference", "requested_changes",
                "referenced_case_id",
            ),
        )
        _nonblank(case["policy_topic"], "policy topic")
        _nonblank(case["policy_question"], "policy question")
        if (
            case["accepted_by_type"] not in {"SYSTEM_RULE", "SERVICE_AGENT"}
            or (
                case["accepted_by_type"] == "SERVICE_AGENT"
                and case["accepted_by_user_id"] is None
            )
        ):
            raise SafeActionConflict("The accepted policy details are incompatible.")
        response = source["event_payload"].get("response", {})
        retrieval = source["event_payload"].get("retrieval", [])
        citations = response.get("citation_ids")
        retrieved_ids = {
            item.get("citation_id") for item in retrieval if isinstance(item, dict)
        }
        if (
            response.get("schema_version") != "1"
            or response.get("outcome") != "READY"
            or response.get("current_state") != "READY_FOR_ACTION"
            or response.get("case_reference") != case["case_reference"]
            or response.get("case_version") != case["version"]
            or not isinstance(citations, list)
            or not citations
            or not all(isinstance(value, str) and value.strip() for value in citations)
            or len(set(citations)) != len(citations)
            or not set(citations).issubset(retrieved_ids)
        ):
            raise SafeActionConflict("The grounded policy evidence is incompatible.")
        action_type = "POLICY_RESPONSE"
        details = {
            "answer": _nonblank(response.get("answer"), "policy answer"),
            "citation_ids": citations,
        }
    else:
        payload = source["event_payload"]
        if source["event_type"] == "ANALYSIS_READY":
            analysis_run_id = payload.get("analysis_run_id")
            valid = connection.execute(
                """
                SELECT 1
                FROM validation_runs
                JOIN ai_analysis_runs USING (analysis_run_id)
                WHERE validation_runs.case_id = %s
                  AND validation_runs.analysis_run_id::text = %s
                  AND validation_runs.overall_decision = 'READY'
                  AND ai_analysis_runs.status = 'COMPLETED'
                """,
                (case["case_id"], analysis_run_id),
            ).fetchone()
            if (
                payload.get("validation_decision") != "READY"
                or valid is None
                or case["accepted_by_type"] != "SYSTEM_RULE"
            ):
                raise SafeActionConflict(
                    "The accepted analysis evidence is incompatible."
                )
        elif (
            source["event_type"] != "HUMAN_REVIEW_REANALYZED"
            or payload.get("next_route") != "DOWNSTREAM_ACTION"
            or payload.get("result_case_version") != case["version"]
            or case["accepted_by_type"] != "SERVICE_AGENT"
            or case["accepted_by_user_id"] is None
        ):
            raise SafeActionConflict("The accepted review evidence is incompatible.")

        if request_type == "INCIDENT_REPORT":
            _require_unrelated_null(
                case,
                (
                    "policy_topic", "policy_question", "target_system_id",
                    "requested_access_level", "business_reason", "approver_user_id",
                    "record_reference", "requested_changes", "referenced_case_id",
                ),
            )
            if case["affected_system_active"] is not True:
                raise SafeActionConflict("The affected service is not active.")
            action_type = "INCIDENT_TICKET"
            details = {
                "affected_service": _nonblank(
                    case["affected_system_code"], "affected service"
                ),
                "impact": _nonblank(case["impact"], "impact"),
                "urgency": _nonblank(case["urgency"], "urgency"),
                "description": _nonblank(
                    case["incident_description"], "incident description"
                ),
            }
        else:
            _require_unrelated_null(
                case,
                (
                    "policy_topic", "policy_question", "affected_system_id",
                    "incident_description", "impact", "urgency", "target_system_id",
                    "requested_access_level", "business_reason", "approver_user_id",
                    "record_reference", "requested_changes",
                ),
            )
            if (
                case["referenced_case_id"] is None
                or case["referenced_requester_id"] != case["requester_id"]
            ):
                raise SafeActionConflict("The status target is not requester-owned.")
            action_type = "STATUS_RESPONSE"
            visible_state = _nonblank(case["referenced_case_state"], "visible state")
            details = {
                "referenced_case": _nonblank(
                    case["referenced_case_reference"], "referenced case"
                ),
                "visible_state": visible_state,
                "public_update": f"The referenced case is currently {visible_state}.",
            }

    return {
        "case_reference": case["case_reference"],
        "case_version": case["version"],
        "action_type": action_type,
        "title": title,
        "summary": summary,
        "details": details,
    }


def queue_safe_action(database_url: str, *, case_id: UUID) -> SafeActionQueued:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        case, source = _load_authority(connection, case_id)
        payload = _build_payload(connection, case, source)
        key = _idempotency_key(case_id, source["event_id"])
        existing = connection.execute(
            """
            SELECT outbox_message_id, idempotency_key, payload
            FROM outbox_messages
            WHERE case_id = %s AND message_type = 'DOWNSTREAM_ACTION'
            """,
            (case_id,),
        ).fetchall()
        if existing:
            if (
                len(existing) != 1
                or existing[0]["idempotency_key"] != key
                or existing[0]["payload"] != payload
            ):
                raise SafeActionConflict("The existing safe action conflicts.")
            return SafeActionQueued(
                existing[0]["outbox_message_id"],
                case["case_reference"],
                payload["action_type"],
                True,
            )
        inserted = connection.execute(
            """
            INSERT INTO outbox_messages (
                case_id, message_type, destination, idempotency_key, payload,
                status, max_attempts, available_at
            ) VALUES (
                %s, 'DOWNSTREAM_ACTION', %s, %s, %s, 'PENDING', 3, now()
            ) RETURNING outbox_message_id
            """,
            (case_id, DOWNSTREAM_DESTINATION, key, Jsonb(payload)),
        ).fetchone()
    return SafeActionQueued(
        inserted["outbox_message_id"],
        case["case_reference"],
        payload["action_type"],
        False,
    )


def queue_next_safe_action(database_url: str) -> SafeActionQueued | None:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        candidate = connection.execute(
            """
            SELECT cases.case_id
            FROM cases
            WHERE cases.current_state = 'READY_FOR_ACTION'
              AND cases.request_type = ANY(%s)
              AND NOT EXISTS (
                  SELECT 1 FROM outbox_messages
                  WHERE outbox_messages.case_id = cases.case_id
                    AND outbox_messages.message_type = 'DOWNSTREAM_ACTION'
              )
            ORDER BY cases.updated_at, cases.case_reference
            LIMIT 1
            """,
            (list(sorted(SAFE_REQUEST_TYPES)),),
        ).fetchone()
    return (
        None
        if candidate is None
        else queue_safe_action(database_url, case_id=candidate["case_id"])
    )


def _source_event_for_key(
    connection: psycopg.Connection[Any], case_id: UUID, key: str
) -> int:
    rows = connection.execute(
        """
        SELECT event_id
        FROM case_events
        WHERE case_id = %s
          AND event_type IN (
              'ANALYSIS_READY',
              'POLICY_RETRIEVAL_READY',
              'HUMAN_REVIEW_REANALYZED'
          )
        ORDER BY event_id
        """,
        (case_id,),
    ).fetchall()
    matches = [
        row["event_id"]
        for row in rows
        if _idempotency_key(case_id, row["event_id"]) == key
    ]
    if len(matches) != 1:
        raise SafeActionConflict("The safe action has no unique source event.")
    return matches[0]


def reconcile_safe_action(
    database_url: str, *, outbox_message_id: UUID
) -> SafeActionReconciliation:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        outbox = connection.execute(
            """
            SELECT message.*, cases.case_reference, cases.current_state,
                   cases.version
            FROM outbox_messages AS message
            JOIN cases USING (case_id)
            WHERE message.outbox_message_id = %s
              AND message.message_type = 'DOWNSTREAM_ACTION'
              AND message.destination = %s
            FOR UPDATE OF cases
            """,
            (outbox_message_id, DOWNSTREAM_DESTINATION),
        ).fetchone()
        if outbox is None:
            raise SafeActionNotFound("The safe downstream action was not found.")
        existing = connection.execute(
            """
            SELECT to_state, event_payload
            FROM case_events
            WHERE case_id = %s
              AND event_type IN ('DOWNSTREAM_ACTION_COMPLETED','DOWNSTREAM_ACTION_FAILED')
              AND event_payload->>'outbox_message_id' = %s
            ORDER BY event_id LIMIT 1
            """,
            (outbox["case_id"], str(outbox_message_id)),
        ).fetchone()
        if existing is not None:
            return SafeActionReconciliation(
                outbox["case_reference"],
                existing["to_state"],
                existing["event_payload"]["result_case_version"],
                existing["event_payload"].get("downstream_reference"),
                True,
            )
        if outbox["status"] not in {"SENT", "FAILED"}:
            raise SafeActionNotReady("The safe action is not terminal.")
        if (
            outbox["current_state"] != "READY_FOR_ACTION"
            or outbox["payload"].get("case_reference") != outbox["case_reference"]
            or outbox["payload"].get("case_version") != outbox["version"]
        ):
            raise SafeActionConflict("The terminal action no longer matches the case.")
        source_event_id = _source_event_for_key(
            connection, outbox["case_id"], outbox["idempotency_key"]
        )
        case, source = _load_authority(
            connection,
            outbox["case_id"],
            source_event_id=source_event_id,
        )
        if _build_payload(connection, case, source) != outbox["payload"]:
            raise SafeActionConflict("The terminal payload changed from authority.")
        attempt = connection.execute(
            """
            SELECT * FROM delivery_attempts
            WHERE outbox_message_id = %s AND attempt_number = %s
            """,
            (outbox_message_id, outbox["attempt_count"]),
        ).fetchone()
        if attempt is None:
            raise SafeActionConflict("Terminal delivery evidence is missing.")
        if outbox["status"] == "SENT" and attempt["outcome"] == "SUCCESS":
            target_state = "COMPLETED"
            event_type = "DOWNSTREAM_ACTION_COMPLETED"
            downstream_reference = attempt["downstream_reference"]
            if (
                attempt["http_status"] is None
                or not 200 <= attempt["http_status"] <= 299
                or not isinstance(downstream_reference, str)
                or not downstream_reference.strip()
            ):
                raise SafeActionConflict(
                    "The successful delivery evidence is incompatible."
                )
        elif outbox["status"] == "FAILED" and (
            attempt["outcome"] == "PERMANENT_FAILURE"
            or (
                attempt["outcome"] == "TRANSIENT_FAILURE"
                and attempt["attempt_number"] == outbox["max_attempts"]
            )
        ):
            if (
                not isinstance(attempt["error_code"], str)
                or not attempt["error_code"].strip()
                or not isinstance(attempt["error_message"], str)
                or not attempt["error_message"].strip()
                or not outbox["last_error"]
            ):
                raise SafeActionConflict(
                    "The failed delivery evidence is incompatible."
                )
            target_state = "FAILED"
            event_type = "DOWNSTREAM_ACTION_FAILED"
            downstream_reference = None
        else:
            raise SafeActionConflict("The terminal delivery evidence conflicts.")
        result_version = outbox["version"] + 1
        connection.execute(
            """
            UPDATE cases SET current_state = %s, version = %s, updated_at = now()
            WHERE case_id = %s
            """,
            (target_state, result_version, outbox["case_id"]),
        )
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            ) VALUES (
                %s, %s, 'READY_FOR_ACTION', %s, %s, 'INTEGRATION',
                'The deterministic safe action reached terminal delivery.', %s
            )
            """,
            (
                outbox["case_id"],
                _next_sequence(connection, outbox["case_id"]),
                target_state,
                event_type,
                Jsonb(
                    {
                        "action_type": outbox["payload"]["action_type"],
                        "authority": "SAFE_AUTOMATION",
                        "delivery_attempt_number": attempt["attempt_number"],
                        "delivery_outcome": attempt["outcome"],
                        "downstream_reference": downstream_reference,
                        "idempotency_key": outbox["idempotency_key"],
                        "outbox_message_id": str(outbox_message_id),
                        "result_case_version": result_version,
                        "schema_version": "1",
                    }
                ),
            ),
        )
    return SafeActionReconciliation(
        outbox["case_reference"],
        target_state,
        result_version,
        downstream_reference,
        False,
    )
