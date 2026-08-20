"""Create disposable fictional users and cases for the guided local portal."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from intake import IntakeRequest, RequesterSelector, create_or_replay_case


DATABASE_URL = os.environ.get("DEMO_DATABASE_URL", "")
DEMO_PASSWORD = "Demo-Local-Only-2026!"
REQUESTER_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("10000000-0000-4000-8000-000000000002")
APPROVER_ID = UUID("10000000-0000-4000-8000-000000000003")
WMS_ID = UUID("20000000-0000-4000-8000-000000000001")
OLLAMA_EXTERNAL_ID = "GUIDED-DEMO-OLLAMA-01"
OLLAMA_SUBJECT = "Guided Ollama incident"
OLLAMA_MESSAGE = (
    "Sejak pukul 09.15 WMS tidak menerima pemindaian barang. "
    "Dampaknya tinggi untuk tim gudang dan urgensinya tinggi."
)


def require_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DEMO_DATABASE_URL is required.")
    return DATABASE_URL


def empty_fields() -> dict[str, str | None]:
    return {
        "policy_topic": None,
        "question": None,
        "affected_service": None,
        "incident_description": None,
        "impact": None,
        "urgency": None,
        "target_system": None,
        "requested_access_level": None,
        "business_reason": None,
        "approver_id": None,
        "record_reference": None,
        "requested_changes": None,
        "case_reference": None,
    }


def add_case(
    connection: psycopg.Connection[Any],
    number: int,
    state: str,
    requester_id: UUID,
    *,
    subject: str,
    message: str,
    request_type: str | None,
    ai_summary: str | None,
) -> UUID:
    case_id = uuid4()
    case_reference = f"CASE-2026-{9800 + number:04d}"
    external_id = f"GUIDED-DEMO-{number:02d}"
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO cases (
            case_id, case_reference, source_channel, external_request_id,
            idempotency_key, content_fingerprint, requester_id, subject,
            original_message, attachment_metadata, request_type, ai_summary,
            current_state, version, received_at
        )
        VALUES (
            %s, %s, 'WEB', %s, %s, %s, %s, %s, %s, '[]', %s, %s,
            %s, 1, %s
        )
        """,
        (
            case_id,
            case_reference,
            external_id,
            digest,
            digest,
            requester_id,
            subject,
            message,
            request_type,
            ai_summary,
            state,
            datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
    )
    connection.execute(
        """
        INSERT INTO case_events (
            case_id, sequence_number, from_state, to_state, event_type,
            actor_type, reason, event_payload
        )
        VALUES (
            %s, 1, NULL, %s, 'GUIDED_DEMO_CASE_CREATED',
            'INTEGRATION', 'A disposable fictional learning case was created.', '{}'
        )
        """,
        (case_id, state),
    )
    return case_id


def add_review_proposal(
    connection: psycopg.Connection[Any], case_id: UUID
) -> None:
    fields = empty_fields()
    fields.update(
        {
            "target_system": "WMS",
            "requested_access_level": "STANDARD",
            "business_reason": "Prepare the weekly inventory report.",
            "approver_id": "MGR-104",
        }
    )
    proposal = {
        "request_type": "access_request",
        "summary": "Proposed WMS standard access for weekly inventory reporting.",
        "fields": fields,
        "evidence": [],
    }
    connection.execute(
        """
        INSERT INTO ai_analysis_runs (
            case_id, model_name, model_identifier, prompt_contract_version,
            input_sha256, proposal, evidence, status, wall_time_ms,
            input_tokens, output_tokens, attempt_number, completed_at
        )
        VALUES (
            %s, 'guided-demo-fixture', 'guided-demo-proposal-v1',
            'analysis-v1', %s, %s, '[]', 'COMPLETED', 0, 0, 0, 1, now()
        )
        """,
        (
            case_id,
            hashlib.sha256(str(case_id).encode("utf-8")).hexdigest(),
            Jsonb(proposal),
        ),
    )


def add_pending_approval(
    connection: psycopg.Connection[Any], case_id: UUID
) -> None:
    connection.execute(
        """
        INSERT INTO case_details (
            case_id, target_system_id, business_reason, approver_user_id,
            record_reference, requested_changes, accepted_by_type, accepted_at
        )
        VALUES (
            %s, %s, 'Correct a fictional supplier record.', %s,
            'SUP-448', 'Change the fictional routing code to TEST-02.',
            'SYSTEM_RULE', now()
        )
        """,
        (case_id, WMS_ID, APPROVER_ID),
    )
    connection.execute(
        """
        INSERT INTO approvals (
            case_id, approver_user_id, request_type, decision, requested_at
        )
        VALUES (%s, %s, 'DATA_CHANGE_REQUEST', 'PENDING', now())
        """,
        (case_id, APPROVER_ID),
    )


def main() -> None:
    database_url = require_database_url()
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE users
            SET password_hash = crypt(%s, gen_salt('bf', 4)), updated_at = now()
            """,
            (DEMO_PASSWORD,),
        )
        existing = connection.execute(
            """
            SELECT count(*) FROM cases
            WHERE external_request_id IN (
                'GUIDED-DEMO-01', 'GUIDED-DEMO-02', 'GUIDED-DEMO-03'
            )
            """
        ).fetchone()[0]
        if existing == 0:
            add_case(
                connection,
                1,
                "NEEDS_INFORMATION",
                REQUESTER_ID,
                subject="Demo: missing incident information",
                message="WMS login is failing for the warehouse team. Impact is high.",
                request_type="INCIDENT_REPORT",
                ai_summary="The incident is missing the urgency needed for deterministic routing.",
            )
            review_case_id = add_case(
                connection,
                2,
                "NEEDS_REVIEW",
                REQUESTER_ID,
                subject="Demo: service-agent review",
                message="Please give me standard WMS access for weekly inventory reporting. Approver MGR-104.",
                request_type=None,
                ai_summary="AI proposed a structured access request that requires service-agent confirmation.",
            )
            add_review_proposal(connection, review_case_id)
            approval_case_id = add_case(
                connection,
                3,
                "PENDING_APPROVAL",
                AGENT_ID,
                subject="Demo: assigned approval",
                message="Change supplier SUP-448 routing code to TEST-02 in WMS.",
                request_type="DATA_CHANGE_REQUEST",
                ai_summary="The checked data-change request requires the assigned approver.",
            )
            add_pending_approval(connection, approval_case_id)
        elif existing != 3:
            raise RuntimeError(
                "The guided demo database contains incomplete learning fixtures."
            )

    create_or_replay_case(
        database_url,
        IntakeRequest(
            source_channel="WEBHOOK",
            external_request_id=OLLAMA_EXTERNAL_ID,
            subject=OLLAMA_SUBJECT,
            message=OLLAMA_MESSAGE,
            attachment_metadata=[],
            received_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
        RequesterSelector(employee_reference="EMP-201"),
    )
    print("Guided demo fixtures: READY")


if __name__ == "__main__":
    main()
