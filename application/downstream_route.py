"""Materialize and reconcile approved human decisions for downstream delivery."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from delivery import ServiceDeskClient, process_one_message


HUMAN_RESUME_REFERENCE_PATTERN = re.compile(r"^HDRESUME-([1-9][0-9]*)$")
HUMAN_DECISION_REFERENCE_PATTERN = re.compile(r"^HD-([1-9][0-9]*)$")
CASE_REFERENCE_PATTERN = re.compile(r"^CASE-[0-9]{4}-[0-9]{4,}$")
DOWNSTREAM_DESTINATION = "service-desk-sandbox"


class ApprovedActionNotFound(Exception):
    """The referenced case or durable acknowledgement does not exist."""


class ApprovedActionConflict(Exception):
    """The authoritative approval evidence cannot produce this action."""


class ApprovedActionNotReady(Exception):
    """The downstream delivery has not reached a terminal state."""


@dataclass(frozen=True)
class ApprovedActionQueued:
    outbox_message_id: UUID
    case_reference: str
    action_type: str
    idempotency_key: str
    idempotent_replay: bool


@dataclass(frozen=True)
class ApprovedActionReconciliation:
    case_reference: str
    current_state: str
    case_version: int
    event_reference: str
    downstream_reference: str | None
    idempotent_replay: bool


def _action_idempotency_key(case_id: UUID, acknowledgement_event_id: int) -> str:
    source = f"DOWNSTREAM_ACTION|{case_id}|{acknowledgement_event_id}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _nonblank(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApprovedActionConflict(f"The approved {field_name} is missing.")
    return value.strip()


def _next_event_sequence(
    connection: psycopg.Connection[Any], case_id: UUID
) -> int:
    return connection.execute(
        """
        SELECT COALESCE(MAX(sequence_number), 0) + 1 AS sequence_number
        FROM case_events
        WHERE case_id = %s
        """,
        (case_id,),
    ).fetchone()["sequence_number"]


def _load_authority(
    connection: psycopg.Connection[Any],
    *,
    case_id: UUID,
    acknowledgement_event_id: int,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            cases.case_id,
            cases.case_reference,
            cases.subject,
            cases.ai_summary,
            cases.request_type,
            cases.requester_id,
            cases.current_state,
            cases.version,
            acknowledgement.event_id AS acknowledgement_event_id,
            acknowledgement.from_state AS acknowledgement_from_state,
            acknowledgement.to_state AS acknowledgement_to_state,
            acknowledgement.actor_type AS acknowledgement_actor_type,
            acknowledgement.event_payload AS acknowledgement_payload,
            details.policy_topic,
            details.policy_question,
            details.affected_system_id,
            details.incident_description,
            details.impact,
            details.urgency,
            details.target_system_id,
            details.requested_access_level,
            details.business_reason,
            details.approver_user_id AS details_approver_user_id,
            details.record_reference,
            details.requested_changes,
            details.referenced_case_id,
            systems.system_code,
            systems.is_active AS system_is_active,
            approvals.approval_id,
            approvals.approver_user_id AS approval_approver_user_id,
            approvals.request_type AS approval_request_type,
            approvals.decision AS approval_decision,
            approvals.decided_at,
            approvers.employee_reference AS approver_reference,
            approvers.is_active AS approver_is_active,
            requesters.is_active AS requester_is_active,
            EXISTS (
                SELECT 1
                FROM user_roles
                WHERE user_roles.user_id = approvals.approver_user_id
                  AND user_roles.role_code = 'APPROVER'
            ) AS approver_role_active,
            EXISTS (
                SELECT 1
                FROM user_roles
                WHERE user_roles.user_id = cases.requester_id
                  AND user_roles.role_code = 'REQUESTER'
            ) AS requester_role_active,
            EXISTS (
                SELECT 1
                FROM system_permissions
                WHERE system_permissions.user_id = approvals.approver_user_id
                  AND system_permissions.system_id = details.target_system_id
                  AND system_permissions.permission_code = CASE
                      WHEN cases.request_type = 'ACCESS_REQUEST'
                          THEN 'APPROVE_ACCESS'
                      ELSE 'APPROVE_DATA_CHANGE'
                  END
                  AND system_permissions.is_active
            ) AS approver_permission_active,
            EXISTS (
                SELECT 1
                FROM system_permissions
                WHERE system_permissions.user_id = cases.requester_id
                  AND system_permissions.system_id = details.target_system_id
                  AND system_permissions.permission_code = CASE
                      WHEN cases.request_type = 'ACCESS_REQUEST'
                          THEN 'REQUEST_ACCESS'
                      ELSE 'REQUEST_DATA_CHANGE'
                  END
                  AND system_permissions.is_active
            ) AS requester_permission_active
        FROM cases
        JOIN case_events AS acknowledgement
          ON acknowledgement.case_id = cases.case_id
         AND acknowledgement.event_id = %s
         AND acknowledgement.event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
        LEFT JOIN case_details AS details
          ON details.case_id = cases.case_id
        LEFT JOIN managed_systems AS systems
          ON systems.system_id = details.target_system_id
        LEFT JOIN approvals
          ON approvals.case_id = cases.case_id
        LEFT JOIN users AS approvers
          ON approvers.user_id = approvals.approver_user_id
        LEFT JOIN users AS requesters
          ON requesters.user_id = cases.requester_id
        WHERE cases.case_id = %s
        FOR UPDATE OF cases
        """,
        (acknowledgement_event_id, case_id),
    ).fetchone()
    if row is None:
        raise ApprovedActionNotFound(
            "The approved-action acknowledgement was not found."
        )
    return dict(row)


def _validate_authority(
    connection: psycopg.Connection[Any], row: dict[str, Any]
) -> None:
    acknowledgement = row["acknowledgement_payload"]
    if (
        row["current_state"] != "READY_FOR_ACTION"
        or row["acknowledgement_from_state"] != "READY_FOR_ACTION"
        or row["acknowledgement_to_state"] != "READY_FOR_ACTION"
        or row["acknowledgement_actor_type"] != "INTEGRATION"
        or acknowledgement.get("schema_version") != "1"
        or acknowledgement.get("action") != "APPROVE_REQUEST"
        or acknowledgement.get("resume_route") != "DOWNSTREAM_ACTION"
        or acknowledgement.get("case_version") != row["version"]
    ):
        raise ApprovedActionConflict(
            "The acknowledgement is not a current approved downstream route."
        )

    decision_reference = acknowledgement.get("human_decision_reference")
    decision_match = (
        HUMAN_DECISION_REFERENCE_PATTERN.fullmatch(decision_reference)
        if isinstance(decision_reference, str)
        else None
    )
    if decision_match is None:
        raise ApprovedActionConflict("The approval decision reference is invalid.")
    decision = connection.execute(
        """
        SELECT event_type, from_state, to_state, actor_type,
               actor_user_id, event_payload
        FROM case_events
        WHERE event_id = %s
          AND case_id = %s
        """,
        (int(decision_match.group(1)), row["case_id"]),
    ).fetchone()
    if (
        decision is None
        or decision["event_type"] != "APPROVAL_APPROVED"
        or decision["from_state"] != "PENDING_APPROVAL"
        or decision["to_state"] != "READY_FOR_ACTION"
        or decision["actor_type"] != "USER"
        or decision["actor_user_id"] != row["approval_approver_user_id"]
        or decision["event_payload"].get("schema_version") != "1"
        or decision["event_payload"].get("action") != "APPROVE_REQUEST"
        or decision["event_payload"].get("decision") != "APPROVED"
        or decision["event_payload"].get("approval_id")
        != str(row["approval_id"])
        or decision["event_payload"].get("result_case_version")
        != row["version"]
    ):
        raise ApprovedActionConflict(
            "The referenced approval event is incompatible."
        )

    if row["request_type"] not in {"ACCESS_REQUEST", "DATA_CHANGE_REQUEST"}:
        raise ApprovedActionConflict("The approved request type is unsupported.")
    if (
        row["approval_id"] is None
        or row["approval_decision"] != "APPROVED"
        or row["decided_at"] is None
        or row["approval_request_type"] != row["request_type"]
        or row["approval_approver_user_id"]
        != row["details_approver_user_id"]
        or row["target_system_id"] is None
        or row["system_is_active"] is not True
        or row["approver_is_active"] is not True
        or row["requester_is_active"] is not True
        or row["approver_role_active"] is not True
        or row["requester_role_active"] is not True
        or row["approver_permission_active"] is not True
        or row["requester_permission_active"] is not True
    ):
        raise ApprovedActionConflict(
            "The assigned approval and accepted details do not match."
        )

    unrelated_fields = (
        "policy_topic",
        "policy_question",
        "affected_system_id",
        "incident_description",
        "impact",
        "urgency",
        "referenced_case_id",
    )
    if any(row[field_name] is not None for field_name in unrelated_fields):
        raise ApprovedActionConflict(
            "The approved action contains unrelated accepted details."
        )
    _nonblank(row["business_reason"], "business reason")

    if row["request_type"] == "ACCESS_REQUEST":
        _nonblank(row["requested_access_level"], "access level")
        if row["record_reference"] is not None or row["requested_changes"] is not None:
            raise ApprovedActionConflict(
                "The access action contains data-change details."
            )
    else:
        _nonblank(row["record_reference"], "record reference")
        _nonblank(row["requested_changes"], "requested changes")
        if row["requested_access_level"] is not None:
            raise ApprovedActionConflict(
                "The data-change action contains an access level."
            )


def _build_payload(row: dict[str, Any]) -> dict[str, Any]:
    if CASE_REFERENCE_PATTERN.fullmatch(row["case_reference"]) is None:
        raise ApprovedActionConflict("The case reference is invalid.")
    title = _nonblank(row["subject"], "title")
    summary = _nonblank(row["ai_summary"], "summary")
    target_system = _nonblank(row["system_code"], "target system")
    approver_reference = _nonblank(
        row["approver_reference"], "approver reference"
    )
    approval_reference = f"APPROVAL-{row['approval_id']}"

    if row["request_type"] == "ACCESS_REQUEST":
        action_type = "ACCESS_ACTION"
        details = {
            "target_system": target_system,
            "access_level": _nonblank(
                row["requested_access_level"], "access level"
            ),
            "approver_reference": approver_reference,
            "approval_reference": approval_reference,
        }
    else:
        action_type = "DATA_CHANGE_ACTION"
        details = {
            "target_system": target_system,
            "record_reference": _nonblank(
                row["record_reference"], "record reference"
            ),
            "requested_changes": _nonblank(
                row["requested_changes"], "requested changes"
            ),
            "approver_reference": approver_reference,
            "approval_reference": approval_reference,
        }
    return {
        "case_reference": row["case_reference"],
        "case_version": row["version"],
        "action_type": action_type,
        "title": title,
        "summary": summary,
        "details": details,
    }


def queue_approved_action(
    database_url: str,
    *,
    case_id: UUID,
    human_resume_reference: str,
) -> ApprovedActionQueued:
    """Create or replay 1 exact downstream action from durable approval evidence."""

    reference_match = HUMAN_RESUME_REFERENCE_PATTERN.fullmatch(
        human_resume_reference
    )
    if reference_match is None:
        raise ApprovedActionConflict("The human-resume reference is invalid.")
    acknowledgement_event_id = int(reference_match.group(1))
    idempotency_key = _action_idempotency_key(
        case_id, acknowledgement_event_id
    )

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        row = _load_authority(
            connection,
            case_id=case_id,
            acknowledgement_event_id=acknowledgement_event_id,
        )
        _validate_authority(connection, row)
        payload = _build_payload(row)

        existing_for_case = connection.execute(
            """
            SELECT outbox_message_id, idempotency_key, payload
            FROM outbox_messages
            WHERE case_id = %s
              AND message_type = 'DOWNSTREAM_ACTION'
              AND destination = %s
            """,
            (case_id, DOWNSTREAM_DESTINATION),
        ).fetchall()
        if existing_for_case:
            if len(existing_for_case) != 1:
                raise ApprovedActionConflict(
                    "The case has multiple downstream action intents."
                )
            existing = existing_for_case[0]
            if (
                existing["idempotency_key"] != idempotency_key
                or existing["payload"] != payload
            ):
                raise ApprovedActionConflict(
                    "The existing downstream action conflicts with the approval."
                )
            return ApprovedActionQueued(
                existing["outbox_message_id"],
                row["case_reference"],
                payload["action_type"],
                idempotency_key,
                True,
            )

        inserted = connection.execute(
            """
            INSERT INTO outbox_messages (
                case_id, message_type, destination, idempotency_key,
                payload, status, max_attempts, available_at
            )
            VALUES (
                %s, 'DOWNSTREAM_ACTION', %s, %s, %s,
                'PENDING', 3, now()
            )
            RETURNING outbox_message_id
            """,
            (
                case_id,
                DOWNSTREAM_DESTINATION,
                idempotency_key,
                Jsonb(payload),
            ),
        ).fetchone()
    return ApprovedActionQueued(
        inserted["outbox_message_id"],
        row["case_reference"],
        payload["action_type"],
        idempotency_key,
        False,
    )


def queue_next_approved_action(database_url: str) -> ApprovedActionQueued | None:
    """Find 1 unmaterialized current approved acknowledgement."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        candidate = connection.execute(
            """
            SELECT acknowledgement.event_id, acknowledgement.case_id
            FROM case_events AS acknowledgement
            JOIN cases ON cases.case_id = acknowledgement.case_id
            WHERE acknowledgement.event_type =
                    'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
              AND acknowledgement.event_payload->>'action' = 'APPROVE_REQUEST'
              AND acknowledgement.event_payload->>'resume_route' =
                    'DOWNSTREAM_ACTION'
              AND cases.current_state = 'READY_FOR_ACTION'
              AND cases.version::text =
                    acknowledgement.event_payload->>'case_version'
              AND NOT EXISTS (
                  SELECT 1
                  FROM outbox_messages AS message
                  WHERE message.idempotency_key = encode(
                      digest(
                          'DOWNSTREAM_ACTION|' || cases.case_id::text || '|' ||
                          acknowledgement.event_id::text,
                          'sha256'
                      ),
                      'hex'
                  )
              )
            ORDER BY acknowledgement.event_id
            LIMIT 1
            """
        ).fetchone()
    if candidate is None:
        return None
    return queue_approved_action(
        database_url,
        case_id=candidate["case_id"],
        human_resume_reference=f"HDRESUME-{candidate['event_id']}",
    )


def _matching_acknowledgement_event_id(
    connection: psycopg.Connection[Any],
    *,
    case_id: UUID,
    idempotency_key: str,
) -> int:
    rows = connection.execute(
        """
        SELECT event_id
        FROM case_events
        WHERE case_id = %s
          AND event_type = 'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
          AND event_payload->>'action' = 'APPROVE_REQUEST'
          AND event_payload->>'resume_route' = 'DOWNSTREAM_ACTION'
        ORDER BY event_id
        """,
        (case_id,),
    ).fetchall()
    matches = [
        row["event_id"]
        for row in rows
        if _action_idempotency_key(case_id, row["event_id"])
        == idempotency_key
    ]
    if len(matches) != 1:
        raise ApprovedActionConflict(
            "The delivery intent is not linked to one approved acknowledgement."
        )
    return matches[0]


def reconcile_approved_action(
    database_url: str, *, outbox_message_id: UUID
) -> ApprovedActionReconciliation:
    """Move 1 case only from exact terminal delivery evidence."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        outbox = connection.execute(
            """
            SELECT
                message.outbox_message_id,
                message.case_id,
                message.idempotency_key,
                message.payload,
                message.status,
                message.attempt_count,
                message.max_attempts,
                message.last_error,
                cases.case_reference,
                cases.current_state,
                cases.version
            FROM outbox_messages AS message
            JOIN cases ON cases.case_id = message.case_id
            WHERE message.outbox_message_id = %s
              AND message.message_type = 'DOWNSTREAM_ACTION'
              AND message.destination = %s
            FOR UPDATE OF cases
            """,
            (outbox_message_id, DOWNSTREAM_DESTINATION),
        ).fetchone()
        if outbox is None:
            raise ApprovedActionNotFound("The downstream action was not found.")

        # Recheck replay only after taking the case lock. A concurrent
        # reconciler may have committed while this transaction was waiting.
        existing_event = connection.execute(
            """
            SELECT event_id, to_state, event_payload
            FROM case_events
            WHERE case_id = %s
              AND event_type IN (
                  'DOWNSTREAM_ACTION_COMPLETED',
                  'DOWNSTREAM_ACTION_FAILED'
              )
              AND event_payload->>'outbox_message_id' = %s
            ORDER BY event_id
            LIMIT 1
            """,
            (outbox["case_id"], str(outbox_message_id)),
        ).fetchone()
        if existing_event is not None:
            payload = existing_event["event_payload"]
            return ApprovedActionReconciliation(
                payload["case_reference"],
                existing_event["to_state"],
                payload["result_case_version"],
                f"ACTION-{existing_event['event_id']}",
                payload.get("downstream_reference"),
                True,
            )

        if outbox["status"] not in {"SENT", "FAILED"}:
            raise ApprovedActionNotReady(
                "The downstream action has no terminal delivery evidence."
            )
        if (
            outbox["current_state"] != "READY_FOR_ACTION"
            or outbox["version"] != outbox["payload"].get("case_version")
            or outbox["case_reference"]
            != outbox["payload"].get("case_reference")
        ):
            raise ApprovedActionConflict(
                "The terminal delivery no longer matches the current case."
            )

        acknowledgement_event_id = _matching_acknowledgement_event_id(
            connection,
            case_id=outbox["case_id"],
            idempotency_key=outbox["idempotency_key"],
        )
        authority = _load_authority(
            connection,
            case_id=outbox["case_id"],
            acknowledgement_event_id=acknowledgement_event_id,
        )
        _validate_authority(connection, authority)
        if _build_payload(authority) != outbox["payload"]:
            raise ApprovedActionConflict(
                "The terminal delivery payload conflicts with authoritative data."
            )

        attempt = connection.execute(
            """
            SELECT attempt_number, outcome, http_status,
                   downstream_reference, response_payload,
                   error_code, error_message
            FROM delivery_attempts
            WHERE outbox_message_id = %s
            ORDER BY attempt_number DESC
            LIMIT 1
            """,
            (outbox_message_id,),
        ).fetchone()
        if attempt is None or attempt["attempt_number"] != outbox["attempt_count"]:
            raise ApprovedActionConflict(
                "The terminal delivery attempt evidence is incomplete."
            )

        if outbox["status"] == "SENT":
            if (
                attempt["outcome"] != "SUCCESS"
                or attempt["http_status"] is None
                or not 200 <= attempt["http_status"] <= 299
                or not isinstance(attempt["downstream_reference"], str)
                or not attempt["downstream_reference"].strip()
            ):
                raise ApprovedActionConflict(
                    "The successful delivery evidence is incompatible."
                )
            target_state = "COMPLETED"
            event_type = "DOWNSTREAM_ACTION_COMPLETED"
            reason = "The approved action was accepted by the Service Desk."
            downstream_reference = attempt["downstream_reference"]
        else:
            terminal_failure = (
                attempt["outcome"] == "PERMANENT_FAILURE"
                or (
                    attempt["outcome"] == "TRANSIENT_FAILURE"
                    and attempt["attempt_number"] == outbox["max_attempts"]
                )
            )
            if (
                not terminal_failure
                or not isinstance(attempt["error_code"], str)
                or not isinstance(attempt["error_message"], str)
                or not outbox["last_error"]
            ):
                raise ApprovedActionConflict(
                    "The failed delivery evidence is incompatible."
                )
            target_state = "FAILED"
            event_type = "DOWNSTREAM_ACTION_FAILED"
            reason = "The approved action reached a terminal delivery failure."
            downstream_reference = None

        result_version = outbox["version"] + 1
        event = connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            )
            VALUES (%s, %s, 'READY_FOR_ACTION', %s, %s,
                    'INTEGRATION', %s, %s)
            RETURNING event_id
            """,
            (
                outbox["case_id"],
                _next_event_sequence(connection, outbox["case_id"]),
                target_state,
                event_type,
                reason,
                Jsonb(
                    {
                        "action_type": outbox["payload"]["action_type"],
                        "case_reference": outbox["case_reference"],
                        "delivery_attempt_number": attempt["attempt_number"],
                        "delivery_outcome": attempt["outcome"],
                        "downstream_reference": downstream_reference,
                        "human_resume_reference": (
                            f"HDRESUME-{acknowledgement_event_id}"
                        ),
                        "idempotency_key": outbox["idempotency_key"],
                        "outbox_message_id": str(outbox_message_id),
                        "result_case_version": result_version,
                        "schema_version": "1",
                    }
                ),
            ),
        ).fetchone()
        connection.execute(
            """
            UPDATE cases
            SET current_state = %s, version = %s, updated_at = now()
            WHERE case_id = %s
            """,
            (target_state, result_version, outbox["case_id"]),
        )
    return ApprovedActionReconciliation(
        outbox["case_reference"],
        target_state,
        result_version,
        f"ACTION-{event['event_id']}",
        downstream_reference,
        False,
    )


def reconcile_next_terminal_action(
    database_url: str,
) -> ApprovedActionReconciliation | None:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        candidate = connection.execute(
            """
            SELECT message.outbox_message_id
            FROM outbox_messages AS message
            WHERE message.message_type = 'DOWNSTREAM_ACTION'
              AND message.destination = %s
              AND message.status IN ('SENT', 'FAILED')
              AND NOT EXISTS (
                  SELECT 1
                  FROM case_events AS event
                  WHERE event.event_type IN (
                      'DOWNSTREAM_ACTION_COMPLETED',
                      'DOWNSTREAM_ACTION_FAILED'
                  )
                    AND event.event_payload->>'outbox_message_id' =
                        message.outbox_message_id::text
              )
            ORDER BY message.completed_at, message.created_at
            LIMIT 1
            """,
            (DOWNSTREAM_DESTINATION,),
        ).fetchone()
    if candidate is None:
        return None
    return reconcile_approved_action(
        database_url,
        outbox_message_id=candidate["outbox_message_id"],
    )


def main() -> int:
    database_url = os.environ.get("PRIMARY_DATABASE_URL")
    sandbox_url = os.environ.get("SERVICE_DESK_SANDBOX_URL")
    sandbox_token = os.environ.get("SERVICE_DESK_SANDBOX_TOKEN")
    if not database_url or not sandbox_url or not sandbox_token:
        raise RuntimeError(
            "PRIMARY_DATABASE_URL, SERVICE_DESK_SANDBOX_URL, and "
            "SERVICE_DESK_SANDBOX_TOKEN are required."
        )
    queue_next_approved_action(database_url)
    execution = process_one_message(
        database_url,
        ServiceDeskClient(sandbox_url, sandbox_token),
    )
    if execution is not None and execution.final_status in {"SENT", "FAILED"}:
        reconcile_approved_action(
            database_url,
            outbox_message_id=execution.outbox_message_id,
        )
    return 0 if execution is None or execution.final_status != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
