\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
    IF (SELECT count(*) FROM cases) <> 2 THEN
        RAISE EXCEPTION 'Expected 2 fictional raw cases';
    END IF;

    IF (SELECT count(*) FROM case_details) <> 0 THEN
        RAISE EXCEPTION 'Raw intake must not create accepted details early';
    END IF;

    IF (SELECT count(*) FROM case_events) <> 2 THEN
        RAISE EXCEPTION 'Expected 1 creation event for each raw case';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM cases
        WHERE request_type IS NOT NULL
           OR ai_summary IS NOT NULL
           OR current_state <> 'RECEIVED'
           OR version <> 1
    ) THEN
        RAISE EXCEPTION 'Seed cases must remain unanalysed raw intake';
    END IF;
END;
$$;

DO $$
BEGIN
    BEGIN
        UPDATE cases
        SET original_message = 'Changed after intake'
        WHERE case_reference = 'CASE-2026-0001';
        RAISE EXCEPTION 'Original request text was changed';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        UPDATE case_events
        SET reason = 'Rewritten history'
        WHERE case_id = '40000000-0000-4000-8000-000000000001'
          AND sequence_number = 1;
        RAISE EXCEPTION 'Existing audit event was changed';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO cases (
            case_reference,
            source_channel,
            external_request_id,
            idempotency_key,
            content_fingerprint,
            requester_id,
            subject,
            original_message,
            attachment_metadata,
            received_at
        )
        SELECT
            'CASE-2026-9001',
            source_channel,
            external_request_id,
            encode(digest('different-idempotency-key', 'sha256'), 'hex'),
            content_fingerprint,
            requester_id,
            subject,
            original_message,
            attachment_metadata,
            now()
        FROM cases
        WHERE case_reference = 'CASE-2026-0001';
        RAISE EXCEPTION 'Duplicate external request was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO cases (
            case_reference,
            source_channel,
            external_request_id,
            idempotency_key,
            content_fingerprint,
            requester_id,
            subject,
            original_message,
            attachment_metadata,
            received_at
        ) VALUES (
            'CASE-2026-9002',
            'WEB',
            'WEB-2026-9002',
            encode(digest('WEB|WEB-2026-9002', 'sha256'), 'hex'),
            encode(digest('invalid attachment shape', 'sha256'), 'hex'),
            '10000000-0000-4000-8000-000000000001',
            'Invalid attachment shape',
            'This row must be rejected because attachments are not an array.',
            '{"name": "not-an-array.png"}'::jsonb,
            now()
        );
        RAISE EXCEPTION 'Non-array attachment metadata was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;
END;
$$;

-- A similar message is allowed when it comes from a distinct real request.
-- The shared content fingerprint can later trigger review without rejecting it.
INSERT INTO cases (
    case_reference,
    source_channel,
    external_request_id,
    idempotency_key,
    content_fingerprint,
    requester_id,
    subject,
    original_message,
    attachment_metadata,
    received_at
)
SELECT
    'CASE-2026-9003',
    'WEB',
    'WEB-2026-9003',
    encode(digest('WEB|WEB-2026-9003', 'sha256'), 'hex'),
    content_fingerprint,
    requester_id,
    subject,
    original_message,
    attachment_metadata,
    now()
FROM cases
WHERE case_reference = 'CASE-2026-0001';

UPDATE cases
SET request_type = 'POLICY_QUESTION',
    ai_summary = 'Question about the access policy.',
    current_state = 'READY_FOR_ACTION',
    version = 2,
    updated_at = now()
WHERE case_reference = 'CASE-2026-0001';

INSERT INTO case_details (
    case_id,
    policy_topic,
    policy_question,
    accepted_by_type,
    accepted_at
)
SELECT
    case_id,
    'System access',
    'What is the process for requesting system access?',
    'SYSTEM_RULE',
    now()
FROM cases
WHERE case_reference = 'CASE-2026-0001';

INSERT INTO case_events (
    case_id,
    sequence_number,
    from_state,
    to_state,
    event_type,
    actor_type,
    reason
)
SELECT
    case_id,
    2,
    'RECEIVED',
    'READY_FOR_ACTION',
    'DETAILS_ACCEPTED',
    'SYSTEM',
    'Structured policy details passed the controlled test rule.'
FROM cases
WHERE case_reference = 'CASE-2026-0001';

DO $$
BEGIN
    IF (
        SELECT count(*)
        FROM cases
        WHERE content_fingerprint = (
            SELECT content_fingerprint
            FROM cases
            WHERE case_reference = 'CASE-2026-0001'
        )
    ) <> 2 THEN
        RAISE EXCEPTION 'Similar-content requests should coexist for review';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM cases AS c
        JOIN case_details AS d USING (case_id)
        WHERE c.case_reference = 'CASE-2026-0001'
          AND c.current_state = 'READY_FOR_ACTION'
          AND c.version = 2
          AND d.accepted_by_type = 'SYSTEM_RULE'
    ) THEN
        RAISE EXCEPTION 'Accepted details were not linked to their case';
    END IF;

    IF (
        SELECT count(*)
        FROM case_events
        WHERE case_id = '40000000-0000-4000-8000-000000000001'
    ) <> 2 THEN
        RAISE EXCEPTION 'State transition did not append exactly 1 new event';
    END IF;
END;
$$;

SELECT 'PASS: stage 2 case foundation database checks' AS result;

ROLLBACK;
