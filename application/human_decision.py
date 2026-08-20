"""Authenticated human commands for paused service-request cases."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


CASE_REFERENCE_PATTERN = re.compile(r"^CASE-[0-9]{4}-[0-9]{4,}$")
EMPLOYEE_REFERENCE_PATTERN = re.compile(r"^[A-Z][A-Z0-9-]{1,49}$")

REQUEST_TYPE_MAP = {
    "policy_question": "POLICY_QUESTION",
    "incident_report": "INCIDENT_REPORT",
    "access_request": "ACCESS_REQUEST",
    "data_change_request": "DATA_CHANGE_REQUEST",
    "status_request": "STATUS_REQUEST",
}

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

IMPACT_VALUES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


class HumanAcceptedFields(BaseModel):
    """Exact full field set used when a service agent corrects a proposal."""

    model_config = ConfigDict(extra="forbid", strict=True)

    policy_topic: str | None = Field(max_length=200)
    question: str | None = Field(max_length=2_000)
    affected_service: str | None = Field(max_length=200)
    incident_description: str | None = Field(max_length=2_000)
    impact: str | None = Field(max_length=20)
    urgency: str | None = Field(max_length=20)
    target_system: str | None = Field(max_length=200)
    requested_access_level: str | None = Field(max_length=80)
    business_reason: str | None = Field(max_length=2_000)
    approver_id: str | None = Field(max_length=50)
    record_reference: str | None = Field(max_length=100)
    requested_changes: str | None = Field(max_length=4_000)
    case_reference: str | None = Field(max_length=32)

    @field_validator("*")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("a populated accepted field must not be blank")
        return normalized


class HumanDecisionCommand(BaseModel):
    """Strict command shared by the signed-session HTTP adapter and domain."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]
    command_id: UUID
    expected_case_version: int = Field(ge=1)
    action: Literal[
        "SUBMIT_INFORMATION",
        "CONFIRM_REVIEW",
        "CORRECT_REVIEW",
        "REJECT_REVIEW",
        "APPROVE_REQUEST",
        "REJECT_REQUEST",
    ]
    note: str | None = Field(default=None, max_length=1_000)
    information: str | None = Field(default=None, max_length=4_000)
    request_type: Literal[
        "policy_question",
        "incident_report",
        "access_request",
        "data_change_request",
        "status_request",
    ] | None = None
    summary: str | None = Field(default=None, max_length=500)
    fields: HumanAcceptedFields | None = None

    @field_validator("note", "information", "summary")
    @classmethod
    def normalize_command_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("a populated command value must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_exact_action_shape(self) -> HumanDecisionCommand:
        review_values = (self.request_type, self.summary, self.fields)
        if self.action == "SUBMIT_INFORMATION":
            if self.information is None or self.note is not None or any(
                value is not None for value in review_values
            ):
                raise ValueError("SUBMIT_INFORMATION has an invalid field combination")
        elif self.action == "CONFIRM_REVIEW":
            if self.information is not None or any(
                value is not None for value in review_values
            ):
                raise ValueError("CONFIRM_REVIEW has an invalid field combination")
        elif self.action == "CORRECT_REVIEW":
            if (
                self.information is not None
                or self.request_type is None
                or self.summary is None
                or self.fields is None
            ):
                raise ValueError("CORRECT_REVIEW requires a complete correction")
        elif self.action == "REJECT_REVIEW":
            if (
                self.note is None
                or self.information is not None
                or any(value is not None for value in review_values)
            ):
                raise ValueError("REJECT_REVIEW requires only a decision note")
        else:
            if self.information is not None or any(
                value is not None for value in review_values
            ):
                raise ValueError("approval decisions have an invalid field combination")
            if self.action == "REJECT_REQUEST" and self.note is None:
                raise ValueError("REJECT_REQUEST requires a decision note")
        return self


class HumanDecisionNotFound(Exception):
    """The case reference does not identify a current case."""


class HumanDecisionNotAuthorized(Exception):
    """The signed-in user cannot perform this exact human command."""


class HumanDecisionConflict(Exception):
    """The case version, state, or command identity conflicts."""


class HumanDecisionInvalid(Exception):
    """The human-supplied business values cannot be accepted safely."""


@dataclass(frozen=True)
class HumanDecisionResult:
    """Stable acknowledgement for a committed or exactly replayed command."""

    human_decision_reference: str
    case_reference: str
    action: str
    current_state: str
    case_version: int
    idempotent_replay: bool


def _command_hash(
    case_reference: str,
    actor_user_id: UUID,
    command: HumanDecisionCommand,
) -> str:
    canonical = json.dumps(
        {
            "actor_user_id": str(actor_user_id),
            "case_reference": case_reference,
            "command": command.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_actor(
    connection: psycopg.Connection[Any], actor_user_id: UUID
) -> dict[str, Any]:
    actor = connection.execute(
        """
        SELECT users.user_id, users.is_active,
               COALESCE(
                   array_agg(user_roles.role_code)
                       FILTER (WHERE user_roles.role_code IS NOT NULL),
                   '{}'::varchar[]
               ) AS roles
        FROM users
        LEFT JOIN user_roles ON user_roles.user_id = users.user_id
        WHERE users.user_id = %s
        GROUP BY users.user_id
        """,
        (actor_user_id,),
    ).fetchone()
    if actor is None or not actor["is_active"]:
        raise HumanDecisionNotAuthorized("The signed-in user is inactive or unknown.")
    return dict(actor)


def _load_locked_case(
    connection: psycopg.Connection[Any], case_reference: str
) -> dict[str, Any]:
    if CASE_REFERENCE_PATTERN.fullmatch(case_reference) is None:
        raise HumanDecisionNotFound("The human-decision case was not found.")
    case = connection.execute(
        """
        SELECT case_id, case_reference, requester_id, request_type,
               current_state, version
        FROM cases
        WHERE case_reference = %s
        FOR UPDATE
        """,
        (case_reference,),
    ).fetchone()
    if case is None:
        raise HumanDecisionNotFound("The human-decision case was not found.")
    return dict(case)


def _find_replay(
    connection: psycopg.Connection[Any],
    case: dict[str, Any],
    actor_user_id: UUID,
    command: HumanDecisionCommand,
    input_sha256: str,
) -> HumanDecisionResult | None:
    event = connection.execute(
        """
        SELECT event_id, actor_user_id, to_state, event_payload
        FROM case_events
        WHERE case_id = %s
          AND event_payload->>'human_command_id' = %s
        ORDER BY event_id
        LIMIT 1
        """,
        (case["case_id"], str(command.command_id)),
    ).fetchone()
    if event is None:
        return None
    payload = event["event_payload"]
    if (
        event["actor_user_id"] != actor_user_id
        or payload.get("input_sha256") != input_sha256
        or payload.get("action") != command.action
    ):
        raise HumanDecisionConflict(
            "The human command identifier was already used for different input."
        )
    result_version = payload.get("result_case_version")
    if not isinstance(result_version, int) or result_version < 1:
        raise HumanDecisionConflict("The stored human command evidence is invalid.")
    return HumanDecisionResult(
        human_decision_reference=f"HD-{event['event_id']}",
        case_reference=case["case_reference"],
        action=command.action,
        current_state=event["to_state"],
        case_version=result_version,
        idempotent_replay=True,
    )


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


def _append_human_event(
    connection: psycopg.Connection[Any],
    *,
    case: dict[str, Any],
    actor_user_id: UUID,
    command: HumanDecisionCommand,
    input_sha256: str,
    to_state: str,
    event_type: str,
    reason: str,
    payload: dict[str, Any],
) -> HumanDecisionResult:
    result_version = case["version"] + 1
    event_payload = {
        "action": command.action,
        "human_command_id": str(command.command_id),
        "input_sha256": input_sha256,
        "result_case_version": result_version,
        "schema_version": "1",
        **payload,
    }
    event = connection.execute(
        """
        INSERT INTO case_events (
            case_id,
            sequence_number,
            from_state,
            to_state,
            event_type,
            actor_type,
            actor_user_id,
            reason,
            event_payload
        )
        VALUES (%s, %s, %s, %s, %s, 'USER', %s, %s, %s)
        RETURNING event_id
        """,
        (
            case["case_id"],
            _next_event_sequence(connection, case["case_id"]),
            case["current_state"],
            to_state,
            event_type,
            actor_user_id,
            reason,
            Jsonb(event_payload),
        ),
    ).fetchone()
    connection.execute(
        """
        UPDATE cases
        SET current_state = %s,
            version = %s,
            updated_at = now()
        WHERE case_id = %s
        """,
        (to_state, result_version, case["case_id"]),
    )
    return HumanDecisionResult(
        human_decision_reference=f"HD-{event['event_id']}",
        case_reference=case["case_reference"],
        action=command.action,
        current_state=to_state,
        case_version=result_version,
        idempotent_replay=False,
    )


def _require_state_and_version(
    case: dict[str, Any], command: HumanDecisionCommand, expected_state: str
) -> None:
    if (
        case["current_state"] != expected_state
        or case["version"] != command.expected_case_version
    ):
        raise HumanDecisionConflict(
            "The case state or version does not permit this human command."
        )


def _require_role(actor: dict[str, Any], role_code: str) -> None:
    if role_code not in set(actor["roles"]):
        raise HumanDecisionNotAuthorized(
            f"The signed-in user does not have the {role_code} role."
        )


def _submit_information(
    connection: psycopg.Connection[Any],
    *,
    actor: dict[str, Any],
    case: dict[str, Any],
    command: HumanDecisionCommand,
    input_sha256: str,
) -> HumanDecisionResult:
    _require_role(actor, "REQUESTER")
    if actor["user_id"] != case["requester_id"]:
        raise HumanDecisionNotAuthorized(
            "Only the case requester may supply its missing information."
        )
    _require_state_and_version(case, command, "NEEDS_INFORMATION")
    assert command.information is not None
    return _append_human_event(
        connection,
        case=case,
        actor_user_id=actor["user_id"],
        command=command,
        input_sha256=input_sha256,
        to_state="ANALYZING",
        event_type="REQUESTER_INFORMATION_SUBMITTED",
        reason="The requester supplied additional information for analysis.",
        payload={"information": command.information},
    )


def _active_system(
    connection: psycopg.Connection[Any], supplied_value: str
) -> dict[str, Any]:
    matches = connection.execute(
        """
        SELECT system_id, system_code
        FROM managed_systems
        WHERE is_active
          AND (
              lower(system_code) = lower(%s)
              OR lower(system_name) = lower(%s)
              OR EXISTS (
                  SELECT 1
                  FROM unnest(aliases) AS alias_name
                  WHERE lower(alias_name) = lower(%s)
              )
          )
        """,
        (supplied_value, supplied_value, supplied_value),
    ).fetchall()
    if len(matches) != 1:
        raise HumanDecisionInvalid(
            "The accepted system must resolve to exactly 1 active reference."
        )
    return dict(matches[0])


def _requester_is_active(
    connection: psycopg.Connection[Any], requester_id: UUID
) -> bool:
    return connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM users
            JOIN user_roles ON user_roles.user_id = users.user_id
            WHERE users.user_id = %s
              AND users.is_active
              AND user_roles.role_code = 'REQUESTER'
        ) AS allowed
        """,
        (requester_id,),
    ).fetchone()["allowed"]


def _has_permission(
    connection: psycopg.Connection[Any],
    user_id: UUID,
    system_id: UUID,
    permission_code: str,
) -> bool:
    return connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM system_permissions
            WHERE user_id = %s
              AND system_id = %s
              AND permission_code = %s
              AND is_active
        ) AS allowed
        """,
        (user_id, system_id, permission_code),
    ).fetchone()["allowed"]


def _resolve_approver(
    connection: psycopg.Connection[Any],
    supplied_value: str,
    system_id: UUID,
    request_type: str,
) -> UUID:
    employee_reference = supplied_value.upper()
    if EMPLOYEE_REFERENCE_PATTERN.fullmatch(employee_reference) is None:
        raise HumanDecisionInvalid("The approver reference is invalid.")
    permission_code = (
        "APPROVE_ACCESS"
        if request_type == "access_request"
        else "APPROVE_DATA_CHANGE"
    )
    approver = connection.execute(
        """
        SELECT users.user_id
        FROM users
        JOIN user_roles ON user_roles.user_id = users.user_id
        JOIN system_permissions
          ON system_permissions.user_id = users.user_id
        WHERE users.employee_reference = %s
          AND users.is_active
          AND user_roles.role_code = 'APPROVER'
          AND system_permissions.system_id = %s
          AND system_permissions.permission_code = %s
          AND system_permissions.is_active
        """,
        (employee_reference, system_id, permission_code),
    ).fetchall()
    if len(approver) != 1:
        raise HumanDecisionInvalid(
            "The accepted approver must have the exact active approval permission."
        )
    return approver[0]["user_id"]


def _accepted_values_from_latest_proposal(
    connection: psycopg.Connection[Any], case_id: UUID
) -> tuple[str, str, HumanAcceptedFields]:
    row = connection.execute(
        """
        SELECT proposal
        FROM ai_analysis_runs
        WHERE case_id = %s
          AND status IN ('COMPLETED', 'INVALID_OUTPUT')
        ORDER BY attempt_number DESC, created_at DESC
        LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    if row is None or not isinstance(row["proposal"], dict):
        raise HumanDecisionInvalid(
            "No structured proposal is available to confirm; submit a correction."
        )
    proposal = row["proposal"]
    if set(proposal) != {"request_type", "summary", "fields", "evidence"}:
        raise HumanDecisionInvalid(
            "The latest proposal cannot be confirmed; submit a correction."
        )
    request_type = proposal.get("request_type")
    summary = proposal.get("summary")
    if request_type not in REQUEST_TYPE_MAP or not isinstance(summary, str):
        raise HumanDecisionInvalid(
            "The latest proposal cannot be confirmed; submit a correction."
        )
    try:
        fields = HumanAcceptedFields.model_validate(proposal.get("fields"))
    except ValidationError as error:
        raise HumanDecisionInvalid(
            "The latest proposal cannot be confirmed; submit a correction."
        ) from error
    normalized_summary = summary.strip()
    if not normalized_summary or len(normalized_summary) > 500:
        raise HumanDecisionInvalid(
            "The latest proposal summary cannot be accepted safely."
        )
    return request_type, normalized_summary, fields


def _resolve_accepted_details(
    connection: psycopg.Connection[Any],
    *,
    case: dict[str, Any],
    request_type: str,
    fields: HumanAcceptedFields,
) -> dict[str, Any]:
    values = fields.model_dump()
    required = REQUIRED_FIELDS[request_type]
    missing = sorted(name for name in required if values[name] is None)
    unrelated = sorted(
        name for name, value in values.items()
        if name not in required and value is not None
    )
    if missing:
        raise HumanDecisionInvalid(
            "The service-agent decision is missing: " + ", ".join(missing)
        )
    if unrelated:
        raise HumanDecisionInvalid(
            "The service-agent decision populated unrelated fields: "
            + ", ".join(unrelated)
        )
    if not _requester_is_active(connection, case["requester_id"]):
        raise HumanDecisionInvalid("The case requester is no longer active.")

    details: dict[str, Any] = {
        "policy_topic": None,
        "policy_question": None,
        "affected_system_id": None,
        "incident_description": None,
        "impact": None,
        "urgency": None,
        "target_system_id": None,
        "requested_access_level": None,
        "business_reason": None,
        "approver_user_id": None,
        "record_reference": None,
        "requested_changes": None,
        "referenced_case_id": None,
    }

    if request_type == "policy_question":
        details["policy_topic"] = values["policy_topic"]
        details["policy_question"] = values["question"]
    elif request_type == "incident_report":
        system = _active_system(connection, values["affected_service"])
        impact = values["impact"].upper()
        urgency = values["urgency"].upper()
        if impact not in IMPACT_VALUES or urgency not in IMPACT_VALUES:
            raise HumanDecisionInvalid("Impact and urgency must use the v1 enum.")
        details["affected_system_id"] = system["system_id"]
        details["incident_description"] = values["incident_description"]
        details["impact"] = impact
        details["urgency"] = urgency
    elif request_type in {"access_request", "data_change_request"}:
        system = _active_system(connection, values["target_system"])
        requester_permission = (
            "REQUEST_ACCESS"
            if request_type == "access_request"
            else "REQUEST_DATA_CHANGE"
        )
        if not _has_permission(
            connection,
            case["requester_id"],
            system["system_id"],
            requester_permission,
        ):
            raise HumanDecisionInvalid(
                "The requester lacks the exact active request permission."
            )
        details["target_system_id"] = system["system_id"]
        details["business_reason"] = values["business_reason"]
        details["approver_user_id"] = _resolve_approver(
            connection,
            values["approver_id"],
            system["system_id"],
            request_type,
        )
        if request_type == "access_request":
            details["requested_access_level"] = values[
                "requested_access_level"
            ]
        else:
            details["record_reference"] = values["record_reference"]
            details["requested_changes"] = values["requested_changes"]
    else:
        referenced = connection.execute(
            """
            SELECT case_id
            FROM cases
            WHERE case_reference = %s
              AND requester_id = %s
            """,
            (values["case_reference"].upper(), case["requester_id"]),
        ).fetchall()
        if len(referenced) != 1:
            raise HumanDecisionInvalid(
                "The referenced case is not visible to this requester."
            )
        details["referenced_case_id"] = referenced[0]["case_id"]
    return details


def _store_service_agent_details(
    connection: psycopg.Connection[Any],
    *,
    case: dict[str, Any],
    actor_user_id: UUID,
    request_type: str,
    summary: str,
    details: dict[str, Any],
) -> None:
    existing_approval = connection.execute(
        "SELECT 1 FROM approvals WHERE case_id = %s",
        (case["case_id"],),
    ).fetchone()
    if existing_approval is not None:
        raise HumanDecisionConflict(
            "A review correction cannot replace an existing approval intent."
        )
    connection.execute(
        """
        UPDATE cases
        SET request_type = %s,
            ai_summary = %s,
            updated_at = now()
        WHERE case_id = %s
        """,
        (REQUEST_TYPE_MAP[request_type], summary, case["case_id"]),
    )
    connection.execute(
        """
        INSERT INTO case_details (
            case_id,
            policy_topic,
            policy_question,
            affected_system_id,
            incident_description,
            impact,
            urgency,
            target_system_id,
            requested_access_level,
            business_reason,
            approver_user_id,
            record_reference,
            requested_changes,
            referenced_case_id,
            accepted_by_type,
            accepted_by_user_id,
            accepted_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, 'SERVICE_AGENT', %s, now()
        )
        ON CONFLICT (case_id) DO UPDATE SET
            policy_topic = EXCLUDED.policy_topic,
            policy_question = EXCLUDED.policy_question,
            affected_system_id = EXCLUDED.affected_system_id,
            incident_description = EXCLUDED.incident_description,
            impact = EXCLUDED.impact,
            urgency = EXCLUDED.urgency,
            target_system_id = EXCLUDED.target_system_id,
            requested_access_level = EXCLUDED.requested_access_level,
            business_reason = EXCLUDED.business_reason,
            approver_user_id = EXCLUDED.approver_user_id,
            record_reference = EXCLUDED.record_reference,
            requested_changes = EXCLUDED.requested_changes,
            referenced_case_id = EXCLUDED.referenced_case_id,
            accepted_by_type = EXCLUDED.accepted_by_type,
            accepted_by_user_id = EXCLUDED.accepted_by_user_id,
            accepted_at = EXCLUDED.accepted_at
        """,
        (
            case["case_id"],
            details["policy_topic"],
            details["policy_question"],
            details["affected_system_id"],
            details["incident_description"],
            details["impact"],
            details["urgency"],
            details["target_system_id"],
            details["requested_access_level"],
            details["business_reason"],
            details["approver_user_id"],
            details["record_reference"],
            details["requested_changes"],
            details["referenced_case_id"],
            actor_user_id,
        ),
    )


def _review_case(
    connection: psycopg.Connection[Any],
    *,
    actor: dict[str, Any],
    case: dict[str, Any],
    command: HumanDecisionCommand,
    input_sha256: str,
) -> HumanDecisionResult:
    _require_role(actor, "SERVICE_AGENT")
    _require_state_and_version(case, command, "NEEDS_REVIEW")
    if command.action == "REJECT_REVIEW":
        assert command.note is not None
        return _append_human_event(
            connection,
            case=case,
            actor_user_id=actor["user_id"],
            command=command,
            input_sha256=input_sha256,
            to_state="REJECTED",
            event_type="SERVICE_AGENT_REJECTED",
            reason=command.note,
            payload={},
        )

    if command.action == "CONFIRM_REVIEW":
        request_type, summary, fields = _accepted_values_from_latest_proposal(
            connection, case["case_id"]
        )
        event_type = "SERVICE_AGENT_REVIEW_CONFIRMED"
    else:
        assert command.request_type is not None
        assert command.summary is not None
        assert command.fields is not None
        request_type = command.request_type
        summary = command.summary
        fields = command.fields
        event_type = "SERVICE_AGENT_CORRECTION_ACCEPTED"

    details = _resolve_accepted_details(
        connection,
        case=case,
        request_type=request_type,
        fields=fields,
    )
    _store_service_agent_details(
        connection,
        case=case,
        actor_user_id=actor["user_id"],
        request_type=request_type,
        summary=summary,
        details=details,
    )
    return _append_human_event(
        connection,
        case=case,
        actor_user_id=actor["user_id"],
        command=command,
        input_sha256=input_sha256,
        to_state="ANALYZING",
        event_type=event_type,
        reason=(
            command.note
            or "The service agent accepted structured details for deterministic reanalysis."
        ),
        payload={
            "accepted_fields": sorted(REQUIRED_FIELDS[request_type]),
            "request_type": REQUEST_TYPE_MAP[request_type],
        },
    )


def _decide_approval(
    connection: psycopg.Connection[Any],
    *,
    actor: dict[str, Any],
    case: dict[str, Any],
    command: HumanDecisionCommand,
    input_sha256: str,
) -> HumanDecisionResult:
    _require_role(actor, "APPROVER")
    _require_state_and_version(case, command, "PENDING_APPROVAL")
    approval = connection.execute(
        """
        SELECT approvals.approval_id,
               approvals.approver_user_id,
               approvals.request_type,
               approvals.decision,
               case_details.target_system_id
        FROM approvals
        JOIN case_details ON case_details.case_id = approvals.case_id
        WHERE approvals.case_id = %s
        FOR UPDATE OF approvals
        """,
        (case["case_id"],),
    ).fetchone()
    if (
        approval is None
        or approval["decision"] != "PENDING"
        or approval["request_type"] != case["request_type"]
    ):
        raise HumanDecisionConflict("No compatible pending approval exists.")
    if approval["approver_user_id"] != actor["user_id"]:
        raise HumanDecisionNotAuthorized(
            "Only the assigned approver may decide this request."
        )
    permission_code = (
        "APPROVE_ACCESS"
        if approval["request_type"] == "ACCESS_REQUEST"
        else "APPROVE_DATA_CHANGE"
    )
    if approval["target_system_id"] is None or not _has_permission(
        connection,
        actor["user_id"],
        approval["target_system_id"],
        permission_code,
    ):
        raise HumanDecisionNotAuthorized(
            "The assigned approver lacks the active system permission."
        )

    approved = command.action == "APPROVE_REQUEST"
    decision = "APPROVED" if approved else "REJECTED"
    to_state = "READY_FOR_ACTION" if approved else "REJECTED"
    event_type = "APPROVAL_APPROVED" if approved else "APPROVAL_REJECTED"
    connection.execute(
        """
        UPDATE approvals
        SET decision = %s,
            decision_note = %s,
            decided_at = now()
        WHERE approval_id = %s
          AND decision = 'PENDING'
        """,
        (decision, command.note, approval["approval_id"]),
    )
    return _append_human_event(
        connection,
        case=case,
        actor_user_id=actor["user_id"],
        command=command,
        input_sha256=input_sha256,
        to_state=to_state,
        event_type=event_type,
        reason=(
            command.note
            or "The assigned approver authorized the requested action."
        ),
        payload={
            "approval_id": str(approval["approval_id"]),
            "decision": decision,
        },
    )


def execute_human_decision(
    database_url: str,
    *,
    case_reference: str,
    actor_user_id: UUID,
    command: HumanDecisionCommand,
) -> HumanDecisionResult:
    """Commit exactly 1 role-authorized transition or return its exact replay."""

    input_sha256 = _command_hash(case_reference, actor_user_id, command)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        actor = _load_actor(connection, actor_user_id)
        case = _load_locked_case(connection, case_reference)
        replay = _find_replay(
            connection,
            case,
            actor_user_id,
            command,
            input_sha256,
        )
        if replay is not None:
            return replay

        if command.action == "SUBMIT_INFORMATION":
            return _submit_information(
                connection,
                actor=actor,
                case=case,
                command=command,
                input_sha256=input_sha256,
            )
        if command.action in {
            "CONFIRM_REVIEW",
            "CORRECT_REVIEW",
            "REJECT_REVIEW",
        }:
            return _review_case(
                connection,
                actor=actor,
                case=case,
                command=command,
                input_sha256=input_sha256,
            )
        return _decide_approval(
            connection,
            actor=actor,
            case=case,
            command=command,
            input_sha256=input_sha256,
        )
