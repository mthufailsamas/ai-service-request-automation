"""Read-only role-filtered portal queries and local password verification."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


EMPLOYEE_REFERENCE_PATTERN = re.compile(r"^[A-Z][A-Z0-9-]{1,49}$")


def authenticate_portal_user(
    database_url: str, employee_reference: str, password: str
) -> dict[str, Any] | None:
    """Verify an active user with PostgreSQL crypt without returning its hash."""

    normalized_reference = employee_reference.strip().upper()
    if (
        EMPLOYEE_REFERENCE_PATTERN.fullmatch(normalized_reference) is None
        or not 8 <= len(password) <= 256
    ):
        return None
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        user = connection.execute(
            """
            SELECT user_id, employee_reference, display_name
            FROM users
            WHERE employee_reference = %s
              AND is_active
              AND password_hash = crypt(%s, password_hash)
            """,
            (normalized_reference, password),
        ).fetchone()
    return dict(user) if user is not None else None


def load_portal_user(database_url: str, user_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        user = connection.execute(
            """
            SELECT users.user_id, users.employee_reference, users.display_name,
                   COALESCE(
                       array_agg(user_roles.role_code ORDER BY user_roles.role_code)
                           FILTER (WHERE user_roles.role_code IS NOT NULL),
                       '{}'::varchar[]
                   ) AS roles
            FROM users
            LEFT JOIN user_roles ON user_roles.user_id = users.user_id
            WHERE users.user_id = %s AND users.is_active
            GROUP BY users.user_id
            """,
            (user_id,),
        ).fetchone()
    return dict(user) if user is not None else None


def _visibility(role_set: set[str]) -> tuple[bool, bool, bool]:
    return (
        "ADMIN" in role_set,
        "SERVICE_AGENT" in role_set,
        "APPROVER" in role_set,
    )


def list_visible_cases(
    database_url: str, user: dict[str, Any], *, limit: int = 100
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100:
        raise ValueError("portal case limit must be between 1 and 100")
    role_set = set(user["roles"])
    is_admin, is_agent, is_approver = _visibility(role_set)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            """
            SELECT cases.case_reference, cases.subject, cases.request_type,
                   cases.current_state, cases.version, cases.updated_at,
                   cases.requester_id = %s AS requester_owned,
                   EXISTS (
                       SELECT 1 FROM approvals
                       WHERE approvals.case_id = cases.case_id
                         AND approvals.approver_user_id = %s
                   ) AS approver_assigned
            FROM cases
            WHERE %s
               OR cases.requester_id = %s
               OR (%s AND cases.current_state = 'NEEDS_REVIEW')
               OR (
                    %s AND EXISTS (
                        SELECT 1 FROM approvals
                        WHERE approvals.case_id = cases.case_id
                          AND approvals.approver_user_id = %s
                    )
               )
            ORDER BY cases.updated_at DESC, cases.case_reference DESC
            LIMIT %s
            """,
            (
                user["user_id"],
                user["user_id"],
                is_admin,
                user["user_id"],
                is_agent,
                is_approver,
                user["user_id"],
                limit,
            ),
        ).fetchall()
    return [dict(row) for row in rows]


def load_visible_case(
    database_url: str, user: dict[str, Any], case_reference: str
) -> dict[str, Any] | None:
    role_set = set(user["roles"])
    is_admin, is_agent, is_approver = _visibility(role_set)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        case = connection.execute(
            """
            SELECT cases.case_id, cases.case_reference, cases.requester_id,
                   cases.subject, cases.original_message, cases.request_type,
                   cases.ai_summary, cases.current_state, cases.version,
                   cases.received_at, cases.updated_at,
                   requester.employee_reference AS requester_reference,
                   requester.display_name AS requester_name,
                   approvals.approver_user_id AS assigned_approver_id,
                   approvals.decision AS approval_decision
            FROM cases
            JOIN users AS requester ON requester.user_id = cases.requester_id
            LEFT JOIN approvals ON approvals.case_id = cases.case_id
            WHERE cases.case_reference = %s
              AND (
                   %s
                   OR cases.requester_id = %s
                   OR (%s AND cases.current_state = 'NEEDS_REVIEW')
                   OR (%s AND approvals.approver_user_id = %s)
              )
            """,
            (
                case_reference,
                is_admin,
                user["user_id"],
                is_agent,
                is_approver,
                user["user_id"],
            ),
        ).fetchone()
        if case is None:
            return None
        events = connection.execute(
            """
            SELECT event_type, actor_type, reason, occurred_at
            FROM case_events
            WHERE case_id = %s
            ORDER BY sequence_number DESC
            LIMIT 20
            """,
            (case["case_id"],),
        ).fetchall()
    result = dict(case)
    result["events"] = [dict(event) for event in events]
    return result
