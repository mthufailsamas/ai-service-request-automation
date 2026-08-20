"""Consume verified human-resume acknowledgements without volatile triggers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from delivery import (
    DeliveryExecution,
    DeliveryResponse,
    claim_next_message,
    finalize_delivery,
)


HUMAN_RESUME_REFERENCE_PATTERN = re.compile(r"^HDRESUME-([1-9][0-9]*)$")
HUMAN_DECISION_REFERENCE_PATTERN = re.compile(r"^HD-([1-9][0-9]*)$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NOTIFICATION_MESSAGE_TYPE = "REQUESTER_NOTIFICATION"
NOTIFICATION_DESTINATION = "local-requester-inbox"


class ResumeConsumerNotFound(Exception):
    """The acknowledged case or decision evidence does not exist."""


class ResumeConsumerConflict(Exception):
    """Durable evidence does not authorize the requested consumer effect."""


@dataclass(frozen=True)
class ReviewRouteExecution:
    case_reference: str
    human_resume_reference: str
    next_route: str
    current_state: str
    case_version: int
    idempotent_replay: bool


@dataclass(frozen=True)
class NotificationIntent:
    outbox_message_id: UUID
    case_reference: str
    idempotent_replay: bool


@dataclass(frozen=True)
class NotificationReconciliation:
    outbox_message_id: UUID
    event_type: str
    idempotent_replay: bool


def _event_id(reference: str, pattern: re.Pattern[str], label: str) -> int:
    match = pattern.fullmatch(reference)
    if match is None:
        raise ResumeConsumerConflict(f"The {label} reference is invalid.")
    return int(match.group(1))


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
    connection: psycopg.Connection[Any], human_resume_reference: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    acknowledgement = connection.execute(
        """
        SELECT acknowledgement.case_id,
               acknowledgement.to_state AS acknowledgement_state,
               acknowledgement.event_payload AS acknowledgement_payload,
               cases.case_reference,
               cases.requester_id,
               cases.subject,
               cases.request_type,
               cases.current_state,
               cases.version
        FROM case_events AS acknowledgement
        JOIN cases ON cases.case_id = acknowledgement.case_id
        WHERE acknowledgement.event_id = %s
          AND acknowledgement.event_type =
              'HUMAN_DECISION_RESUME_ACKNOWLEDGED'
        FOR UPDATE OF cases
        """,
        (
            _event_id(
                human_resume_reference,
                HUMAN_RESUME_REFERENCE_PATTERN,
                "human-resume",
            ),
        ),
    ).fetchone()
    if acknowledgement is None:
        raise ResumeConsumerNotFound(
            "The human-resume acknowledgement was not found."
        )
    acknowledgement = dict(acknowledgement)
    acknowledgement_payload = acknowledgement["acknowledgement_payload"]
    decision_reference = str(
        acknowledgement_payload.get("human_decision_reference", "")
    )
    decision = connection.execute(
        """
        SELECT event_id, event_type, actor_type, actor_user_id, reason,
               to_state, event_payload
        FROM case_events
        WHERE event_id = %s AND case_id = %s
        """,
        (
            _event_id(
                decision_reference,
                HUMAN_DECISION_REFERENCE_PATTERN,
                "human-decision",
            ),
            acknowledgement["case_id"],
        ),
    ).fetchone()
    if decision is None:
        raise ResumeConsumerNotFound("The acknowledged human decision was not found.")
    decision = dict(decision)
    if (
        acknowledgement_payload.get("schema_version") != "1"
        or IDEMPOTENCY_KEY_PATTERN.fullmatch(
            str(acknowledgement_payload.get("outbox_idempotency_key", ""))
        )
        is None
        or decision["actor_type"] != "USER"
        or decision["event_payload"].get("schema_version") != "1"
        or decision["event_payload"].get("action")
        != acknowledgement_payload.get("action")
        or decision["event_payload"].get("result_case_version")
        != acknowledgement_payload.get("case_version")
        or decision["to_state"] != acknowledgement["acknowledgement_state"]
    ):
        raise ResumeConsumerConflict(
            "The acknowledgement and human decision evidence conflict."
        )
    return acknowledgement, acknowledgement_payload, decision


def _require_active_permission(
    connection: psycopg.Connection[Any],
    *,
    user_id: UUID,
    role_code: str,
    system_id: UUID,
    permission_code: str,
) -> None:
    allowed = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM users
            JOIN user_roles ON user_roles.user_id = users.user_id
            JOIN system_permissions
              ON system_permissions.user_id = users.user_id
            WHERE users.user_id = %s
              AND users.is_active
              AND user_roles.role_code = %s
              AND system_permissions.system_id = %s
              AND system_permissions.permission_code = %s
              AND system_permissions.is_active
        ) AS allowed
        """,
        (user_id, role_code, system_id, permission_code),
    ).fetchone()["allowed"]
    if not allowed:
        raise ResumeConsumerConflict(
            "Current role or system permission no longer authorizes this route."
        )


def route_reviewed_case(
    database_url: str, *, human_resume_reference: str
) -> ReviewRouteExecution:
    """Route confirmed or corrected details without another model call."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        case, acknowledgement, decision = _load_authority(
            connection, human_resume_reference
        )
        existing = connection.execute(
            """
            SELECT to_state, event_payload
            FROM case_events
            WHERE case_id = %s
              AND event_type = 'HUMAN_REVIEW_REANALYZED'
              AND event_payload->>'human_resume_reference' = %s
            ORDER BY event_id
            LIMIT 1
            """,
            (case["case_id"], human_resume_reference),
        ).fetchone()
        if existing is not None:
            return ReviewRouteExecution(
                case_reference=case["case_reference"],
                human_resume_reference=human_resume_reference,
                next_route=existing["event_payload"]["next_route"],
                current_state=existing["to_state"],
                case_version=existing["event_payload"]["result_case_version"],
                idempotent_replay=True,
            )

        action = acknowledgement.get("action")
        expected_event = {
            "CONFIRM_REVIEW": "SERVICE_AGENT_REVIEW_CONFIRMED",
            "CORRECT_REVIEW": "SERVICE_AGENT_CORRECTION_ACCEPTED",
        }.get(action)
        expected_version = acknowledgement.get("case_version")
        if (
            acknowledgement.get("resume_route") != "ANALYSIS_CONTINUATION"
            or expected_event is None
            or decision["event_type"] != expected_event
            or case["acknowledgement_state"] != "ANALYZING"
            or case["current_state"] != "ANALYZING"
            or case["version"] != expected_version
            or decision["actor_user_id"] is None
        ):
            raise ResumeConsumerConflict(
                "The reviewed case is not eligible for deterministic reanalysis."
            )

        details = connection.execute(
            """
            SELECT policy_topic, policy_question, affected_system_id,
                   incident_description, impact, urgency, target_system_id,
                   requested_access_level, business_reason, approver_user_id,
                   record_reference, requested_changes, referenced_case_id,
                   accepted_by_type, accepted_by_user_id
            FROM case_details
            WHERE case_id = %s
            """,
            (case["case_id"],),
        ).fetchone()
        if (
            details is None
            or details["accepted_by_type"] != "SERVICE_AGENT"
            or details["accepted_by_user_id"] != decision["actor_user_id"]
        ):
            raise ResumeConsumerConflict(
                "The service-agent accepted details are missing or incompatible."
            )

        request_type = case["request_type"]
        target_state: str
        next_route: str
        if request_type in {"ACCESS_REQUEST", "DATA_CHANGE_REQUEST"}:
            system_id = details["target_system_id"]
            approver_id = details["approver_user_id"]
            if system_id is None or approver_id is None:
                raise ResumeConsumerConflict("Approval routing details are incomplete.")
            suffix = "ACCESS" if request_type == "ACCESS_REQUEST" else "DATA_CHANGE"
            _require_active_permission(
                connection,
                user_id=case["requester_id"],
                role_code="REQUESTER",
                system_id=system_id,
                permission_code=f"REQUEST_{suffix}",
            )
            _require_active_permission(
                connection,
                user_id=approver_id,
                role_code="APPROVER",
                system_id=system_id,
                permission_code=f"APPROVE_{suffix}",
            )
            connection.execute(
                """
                INSERT INTO approvals (
                    case_id, approver_user_id, request_type, decision, requested_at
                )
                VALUES (%s, %s, %s, 'PENDING', now())
                """,
                (case["case_id"], approver_id, request_type),
            )
            target_state, next_route = "PENDING_APPROVAL", "APPROVAL"
        elif request_type == "POLICY_QUESTION":
            if not details["policy_topic"] or not details["policy_question"]:
                raise ResumeConsumerConflict("Policy routing details are incomplete.")
            target_state, next_route = "ANALYZING", "POLICY_RETRIEVAL"
        elif request_type == "INCIDENT_REPORT":
            if any(
                not details[field]
                for field in (
                    "affected_system_id",
                    "incident_description",
                    "impact",
                    "urgency",
                )
            ):
                raise ResumeConsumerConflict("Incident routing details are incomplete.")
            target_state, next_route = "READY_FOR_ACTION", "DOWNSTREAM_ACTION"
        elif request_type == "STATUS_REQUEST":
            if details["referenced_case_id"] is None:
                raise ResumeConsumerConflict("Status routing details are incomplete.")
            target_state, next_route = "READY_FOR_ACTION", "DOWNSTREAM_ACTION"
        else:
            raise ResumeConsumerConflict("The accepted request type is unsupported.")

        result_version = case["version"] + (target_state != case["current_state"])
        if target_state != case["current_state"]:
            connection.execute(
                """
                UPDATE cases
                SET current_state = %s, version = %s, updated_at = now()
                WHERE case_id = %s
                """,
                (target_state, result_version, case["case_id"]),
            )
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            )
            VALUES (
                %s, %s, 'ANALYZING', %s, 'HUMAN_REVIEW_REANALYZED',
                'SYSTEM', 'Accepted service-agent details were routed deterministically.',
                %s
            )
            """,
            (
                case["case_id"],
                _next_event_sequence(connection, case["case_id"]),
                target_state,
                Jsonb(
                    {
                        "human_decision_reference": str(
                            acknowledgement["human_decision_reference"]
                        ),
                        "human_resume_reference": human_resume_reference,
                        "next_route": next_route,
                        "result_case_version": result_version,
                        "schema_version": "1",
                    }
                ),
            ),
        )
    return ReviewRouteExecution(
        case_reference=case["case_reference"],
        human_resume_reference=human_resume_reference,
        next_route=next_route,
        current_state=target_state,
        case_version=result_version,
        idempotent_replay=False,
    )


def _notification_key(human_resume_reference: str) -> str:
    return hashlib.sha256(
        f"terminal-notification-v1:{human_resume_reference}".encode("utf-8")
    ).hexdigest()


def materialize_terminal_notification(
    database_url: str, *, human_resume_reference: str
) -> NotificationIntent:
    """Create 1 immutable local requester-notification intent after rejection."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        case, acknowledgement, decision = _load_authority(
            connection, human_resume_reference
        )
        expected_event = {
            "REJECT_REVIEW": "SERVICE_AGENT_REJECTED",
            "REJECT_REQUEST": "APPROVAL_REJECTED",
        }.get(acknowledgement.get("action"))
        if (
            acknowledgement.get("resume_route") != "TERMINAL_NOTIFICATION"
            or expected_event is None
            or decision["event_type"] != expected_event
            or case["acknowledgement_state"] != "REJECTED"
            or case["current_state"] != "REJECTED"
            or case["version"] != acknowledgement.get("case_version")
        ):
            raise ResumeConsumerConflict(
                "The terminal rejection evidence does not authorize notification."
            )

        requester = connection.execute(
            "SELECT employee_reference FROM users WHERE user_id = %s",
            (case["requester_id"],),
        ).fetchone()
        if requester is None:
            raise ResumeConsumerNotFound("The notification requester was not found.")
        payload = {
            "case_reference": case["case_reference"],
            "case_version": case["version"],
            "human_decision_reference": str(
                acknowledgement["human_decision_reference"]
            ),
            "human_resume_reference": human_resume_reference,
            "notification_type": "REQUEST_REJECTED",
            "outcome": "REJECTED",
            "reason": decision["reason"],
            "requester_reference": requester["employee_reference"],
            "schema_version": "1",
            "subject": case["subject"],
        }
        idempotency_key = _notification_key(human_resume_reference)
        existing = connection.execute(
            """
            SELECT outbox_message_id, payload
            FROM outbox_messages
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["payload"] != payload:
                raise ResumeConsumerConflict(
                    "The existing terminal notification intent conflicts."
                )
            return NotificationIntent(
                outbox_message_id=existing["outbox_message_id"],
                case_reference=case["case_reference"],
                idempotent_replay=True,
            )

        inserted = connection.execute(
            """
            INSERT INTO outbox_messages (
                case_id, message_type, destination, idempotency_key, payload,
                status, attempt_count, max_attempts, available_at
            )
            VALUES (
                %s, 'REQUESTER_NOTIFICATION', %s, %s, %s,
                'PENDING', 0, 3, now()
            )
            RETURNING outbox_message_id
            """,
            (
                case["case_id"],
                NOTIFICATION_DESTINATION,
                idempotency_key,
                Jsonb(payload),
            ),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            )
            VALUES (
                %s, %s, 'REJECTED', 'REJECTED',
                'REQUESTER_NOTIFICATION_QUEUED', 'SYSTEM',
                'The terminal requester notification was queued durably.', %s
            )
            """,
            (
                case["case_id"],
                _next_event_sequence(connection, case["case_id"]),
                Jsonb(
                    {
                        "human_resume_reference": human_resume_reference,
                        "outbox_message_id": str(inserted["outbox_message_id"]),
                        "schema_version": "1",
                    }
                ),
            ),
        )
    return NotificationIntent(
        outbox_message_id=inserted["outbox_message_id"],
        case_reference=case["case_reference"],
        idempotent_replay=False,
    )


class LocalNotificationClient:
    """A zero-cost local sink used until a real notification provider exists."""

    def send(
        self, payload: dict[str, Any], idempotency_key: str
    ) -> DeliveryResponse:
        required = {
            "case_reference",
            "case_version",
            "human_decision_reference",
            "human_resume_reference",
            "notification_type",
            "outcome",
            "reason",
            "requester_reference",
            "schema_version",
            "subject",
        }
        if (
            set(payload) != required
            or payload.get("schema_version") != "1"
            or payload.get("notification_type") != "REQUEST_REJECTED"
            or payload.get("outcome") != "REJECTED"
        ):
            return DeliveryResponse(
                outcome="PERMANENT_FAILURE",
                http_status=422,
                downstream_reference=None,
                response_payload={"status": "REJECTED"},
                error_code="INVALID_NOTIFICATION_PAYLOAD",
                error_message="The local notification payload is invalid.",
            )
        reference = f"NOTICE-{idempotency_key[:16].upper()}"
        return DeliveryResponse(
            outcome="SUCCESS",
            http_status=200,
            downstream_reference=reference,
            response_payload={
                "notification_reference": reference,
                "schema_version": "1",
                "status": "DELIVERED",
            },
            error_code=None,
            error_message=None,
        )


def process_one_notification(
    database_url: str,
    client: LocalNotificationClient,
    *,
    retry_delay_seconds: int = 30,
) -> DeliveryExecution | None:
    message = claim_next_message(
        database_url,
        message_type=NOTIFICATION_MESSAGE_TYPE,
        destination=NOTIFICATION_DESTINATION,
    )
    if message is None:
        return None
    started_at = datetime.now(timezone.utc)
    result = client.send(message.payload, message.idempotency_key)
    finished_at = datetime.now(timezone.utc)
    return finalize_delivery(
        database_url,
        message,
        result,
        started_at=started_at,
        finished_at=finished_at,
        retry_delay_seconds=retry_delay_seconds,
    )


def reconcile_terminal_notification(
    database_url: str, *, outbox_message_id: UUID
) -> NotificationReconciliation:
    """Append 1 same-state audit result from terminal delivery evidence."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        outbox = connection.execute(
            """
            SELECT message.case_id, message.status, message.attempt_count,
                   cases.current_state
            FROM outbox_messages AS message
            JOIN cases ON cases.case_id = message.case_id
            WHERE message.outbox_message_id = %s
              AND message.message_type = 'REQUESTER_NOTIFICATION'
              AND message.destination = %s
            FOR UPDATE OF cases
            """,
            (outbox_message_id, NOTIFICATION_DESTINATION),
        ).fetchone()
        if outbox is None:
            raise ResumeConsumerNotFound("The notification outbox was not found.")
        existing = connection.execute(
            """
            SELECT event_type
            FROM case_events
            WHERE case_id = %s
              AND event_type IN (
                  'REQUESTER_NOTIFICATION_SENT',
                  'REQUESTER_NOTIFICATION_FAILED'
              )
              AND event_payload->>'outbox_message_id' = %s
            ORDER BY event_id
            LIMIT 1
            """,
            (outbox["case_id"], str(outbox_message_id)),
        ).fetchone()
        if existing is not None:
            return NotificationReconciliation(
                outbox_message_id=outbox_message_id,
                event_type=existing["event_type"],
                idempotent_replay=True,
            )
        if outbox["current_state"] != "REJECTED" or outbox["status"] not in {
            "SENT",
            "FAILED",
        }:
            raise ResumeConsumerConflict(
                "The notification is not terminal or the case is not rejected."
            )
        attempt = connection.execute(
            """
            SELECT attempt_number, outcome, downstream_reference, error_code
            FROM delivery_attempts
            WHERE outbox_message_id = %s AND attempt_number = %s
            """,
            (outbox_message_id, outbox["attempt_count"]),
        ).fetchone()
        if attempt is None:
            raise ResumeConsumerConflict("Terminal notification evidence is missing.")
        if outbox["status"] == "SENT" and attempt["outcome"] == "SUCCESS":
            event_type = "REQUESTER_NOTIFICATION_SENT"
        elif outbox["status"] == "FAILED" and attempt["outcome"] != "SUCCESS":
            event_type = "REQUESTER_NOTIFICATION_FAILED"
        else:
            raise ResumeConsumerConflict("Notification terminal evidence conflicts.")
        connection.execute(
            """
            INSERT INTO case_events (
                case_id, sequence_number, from_state, to_state, event_type,
                actor_type, reason, event_payload
            )
            VALUES (
                %s, %s, 'REJECTED', 'REJECTED', %s, 'SYSTEM',
                'Terminal notification delivery evidence was reconciled.', %s
            )
            """,
            (
                outbox["case_id"],
                _next_event_sequence(connection, outbox["case_id"]),
                event_type,
                Jsonb(
                    {
                        "attempt_number": attempt["attempt_number"],
                        "downstream_reference": attempt["downstream_reference"],
                        "error_code": attempt["error_code"],
                        "outbox_message_id": str(outbox_message_id),
                        "schema_version": "1",
                    }
                ),
            ),
        )
    return NotificationReconciliation(
        outbox_message_id=outbox_message_id,
        event_type=event_type,
        idempotent_replay=False,
    )
