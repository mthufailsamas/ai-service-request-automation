\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
    IF (SELECT count(*) FROM ai_analysis_runs) <> 0
       OR (SELECT count(*) FROM validation_runs) <> 0
       OR (SELECT count(*) FROM approvals) <> 0 THEN
        RAISE EXCEPTION 'Stage 3 tables must start empty before analysis';
    END IF;
END;
$$;

INSERT INTO ai_analysis_runs (
    analysis_run_id,
    case_id,
    model_name,
    model_identifier,
    prompt_contract_version,
    input_sha256,
    proposal,
    evidence,
    status,
    wall_time_ms,
    input_tokens,
    output_tokens
) SELECT
    '50000000-0000-4000-8000-000000000001',
    case_id,
    'fixture-model',
    'fixture-only-no-model-call',
    'stage3-fixture-v1',
    encode(digest(subject || E'\n' || original_message, 'sha256'), 'hex'),
    jsonb_build_object(
        'request_type', 'ACCESS_REQUEST',
        'summary', 'WMS viewer access for weekly inventory reconciliation.',
        'fields', jsonb_build_object(
            'target_system_code', 'WMS',
            'requested_access_level', 'VIEWER',
            'business_reason', 'weekly inventory reconciliation',
            'approver_reference', 'MGR-104'
        )
    ),
    jsonb_build_array(
        jsonb_build_object(
            'field_name', 'target_system_code',
            'quote', 'WMS'
        ),
        jsonb_build_object(
            'field_name', 'business_reason',
            'quote', 'weekly inventory reconciliation'
        ),
        jsonb_build_object(
            'field_name', 'approver_reference',
            'quote', 'MGR-104'
        )
    ),
    'COMPLETED',
    0,
    0,
    0
FROM cases
WHERE case_reference = 'CASE-2026-0001';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM ai_analysis_runs AS a
        JOIN cases AS c USING (case_id)
        CROSS JOIN LATERAL jsonb_array_elements(a.evidence) AS evidence_item
        WHERE a.analysis_run_id = '50000000-0000-4000-8000-000000000001'
          AND position(evidence_item->>'quote' IN c.original_message) = 0
    ) THEN
        RAISE EXCEPTION 'Every fixture evidence quote must exist in the source';
    END IF;

    IF (
        SELECT count(*)
        FROM users AS u
        JOIN user_roles AS r USING (user_id)
        JOIN system_permissions AS p USING (user_id)
        JOIN managed_systems AS s USING (system_id)
        WHERE u.employee_reference = 'MGR-104'
          AND u.is_active
          AND r.role_code = 'APPROVER'
          AND p.permission_code = 'APPROVE_ACCESS'
          AND p.is_active
          AND s.system_code = 'WMS'
          AND s.is_active
    ) <> 1 THEN
        RAISE EXCEPTION 'MGR-104 must resolve to exactly 1 authorized approver';
    END IF;

    IF EXISTS (SELECT 1 FROM users WHERE employee_reference = 'MGR-10') THEN
        RAISE EXCEPTION 'Truncated approver reference MGR-10 must not resolve';
    END IF;
END;
$$;

INSERT INTO validation_runs (
    validation_run_id,
    case_id,
    analysis_run_id,
    overall_decision,
    missing_fields,
    rule_results,
    reason
) VALUES (
    '60000000-0000-4000-8000-000000000001',
    '40000000-0000-4000-8000-000000000001',
    '50000000-0000-4000-8000-000000000001',
    'READY',
    '{}'::text[],
    '[
        {
            "rule_code": "TARGET_SYSTEM_EXACT",
            "outcome": "PASS",
            "field_name": "target_system_code",
            "proposed_value": "WMS",
            "resolved_value": "WMS",
            "reason": "The active system code matched exactly."
        },
        {
            "rule_code": "APPROVER_EXACT_AND_AUTHORIZED",
            "outcome": "PASS",
            "field_name": "approver_reference",
            "proposed_value": "MGR-104",
            "resolved_value": "MGR-104",
            "reason": "The exact active approver has WMS approval permission."
        }
    ]'::jsonb,
    'All required fields and consequential references passed.'
);

UPDATE cases
SET request_type = 'ACCESS_REQUEST',
    ai_summary = 'WMS viewer access for weekly inventory reconciliation.',
    current_state = 'PENDING_APPROVAL',
    version = 2,
    updated_at = now()
WHERE case_reference = 'CASE-2026-0001';

INSERT INTO case_details (
    case_id,
    target_system_id,
    requested_access_level,
    business_reason,
    approver_user_id,
    accepted_by_type,
    accepted_at
) VALUES (
    '40000000-0000-4000-8000-000000000001',
    '20000000-0000-4000-8000-000000000001',
    'VIEWER',
    'weekly inventory reconciliation',
    '10000000-0000-4000-8000-000000000003',
    'SYSTEM_RULE',
    now()
);

INSERT INTO approvals (
    approval_id,
    case_id,
    approver_user_id,
    request_type,
    decision,
    requested_at
) VALUES (
    '70000000-0000-4000-8000-000000000001',
    '40000000-0000-4000-8000-000000000001',
    '10000000-0000-4000-8000-000000000003',
    'ACCESS_REQUEST',
    'PENDING',
    now()
);

INSERT INTO case_events (
    case_id,
    sequence_number,
    from_state,
    to_state,
    event_type,
    actor_type,
    reason
) VALUES (
    '40000000-0000-4000-8000-000000000001',
    2,
    'RECEIVED',
    'PENDING_APPROVAL',
    'APPROVAL_REQUESTED',
    'SYSTEM',
    'The structured access request passed deterministic validation.'
);

DO $$
BEGIN
    BEGIN
        INSERT INTO ai_analysis_runs (
            case_id,
            model_name,
            model_identifier,
            prompt_contract_version,
            input_sha256,
            proposal,
            evidence,
            status,
            wall_time_ms,
            input_tokens,
            output_tokens
        ) VALUES (
            '40000000-0000-4000-8000-000000000002',
            'fixture-model',
            'fixture-only-no-model-call',
            'stage3-fixture-v1',
            encode(digest('invalid proposal shape', 'sha256'), 'hex'),
            '[]'::jsonb,
            '[]'::jsonb,
            'COMPLETED',
            0,
            0,
            0
        );
        RAISE EXCEPTION 'Non-object AI proposal was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO validation_runs (
            case_id,
            analysis_run_id,
            overall_decision,
            missing_fields,
            rule_results,
            reason
        ) VALUES (
            '40000000-0000-4000-8000-000000000001',
            '50000000-0000-4000-8000-000000000001',
            'READY',
            ARRAY['business_reason'],
            '[]'::jsonb,
            'This invalid row must be rejected.'
        );
        RAISE EXCEPTION 'READY validation with missing fields was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO validation_runs (
            case_id,
            analysis_run_id,
            overall_decision,
            missing_fields,
            rule_results,
            reason
        ) VALUES (
            '40000000-0000-4000-8000-000000000001',
            '50000000-0000-4000-8000-000000000001',
            'READY',
            '{}'::text[],
            '[{
                "rule_code": "APPROVER_EXACT_AND_AUTHORIZED",
                "outcome": "REVIEW",
                "field_name": "approver_reference",
                "proposed_value": "MGR-10",
                "resolved_value": null,
                "reason": "The truncated identifier did not resolve."
            }]'::jsonb,
            'This invalid row must be rejected.'
        );
        RAISE EXCEPTION 'READY validation with a review rule was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        UPDATE approvals
        SET decision = 'APPROVED'
        WHERE approval_id = '70000000-0000-4000-8000-000000000001';
        RAISE EXCEPTION 'Approval without a decision timestamp was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;
END;
$$;

UPDATE approvals
SET decision = 'APPROVED',
    decision_note = 'Approved for viewer access only.',
    decided_at = now()
WHERE approval_id = '70000000-0000-4000-8000-000000000001';

UPDATE cases
SET current_state = 'READY_FOR_ACTION',
    version = 3,
    updated_at = now()
WHERE case_reference = 'CASE-2026-0001';

INSERT INTO case_events (
    case_id,
    sequence_number,
    from_state,
    to_state,
    event_type,
    actor_type,
    actor_user_id,
    reason
) VALUES (
    '40000000-0000-4000-8000-000000000001',
    3,
    'PENDING_APPROVAL',
    'READY_FOR_ACTION',
    'ACCESS_APPROVED',
    'USER',
    '10000000-0000-4000-8000-000000000003',
    'The assigned approver authorized viewer access.'
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM cases AS c
        JOIN ai_analysis_runs AS a USING (case_id)
        JOIN validation_runs AS v USING (case_id, analysis_run_id)
        JOIN approvals AS p USING (case_id)
        WHERE c.case_reference = 'CASE-2026-0001'
          AND c.current_state = 'READY_FOR_ACTION'
          AND c.version = 3
          AND a.status = 'COMPLETED'
          AND v.overall_decision = 'READY'
          AND p.decision = 'APPROVED'
    ) THEN
        RAISE EXCEPTION 'Validated and approved request chain is incomplete';
    END IF;

    IF (
        SELECT count(*)
        FROM case_events
        WHERE case_id = '40000000-0000-4000-8000-000000000001'
    ) <> 3 THEN
        RAISE EXCEPTION 'The approved request must have 3 ordered events';
    END IF;
END;
$$;

SELECT 'PASS: stage 3 AI and decision database checks' AS result;

ROLLBACK;
