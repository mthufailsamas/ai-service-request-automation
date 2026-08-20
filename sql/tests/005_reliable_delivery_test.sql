\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
    IF (SELECT count(*) FROM outbox_messages) <> 0
       OR (SELECT count(*) FROM delivery_attempts) <> 0 THEN
        RAISE EXCEPTION 'Stage 5 delivery tables must start empty';
    END IF;
END;
$$;

INSERT INTO outbox_messages (
    outbox_message_id,
    case_id,
    message_type,
    destination,
    idempotency_key,
    payload,
    available_at,
    created_at
) VALUES (
    '90000000-0000-4000-8000-000000000001',
    '40000000-0000-4000-8000-000000000002',
    'DOWNSTREAM_ACTION',
    'service-desk-sandbox',
    encode(digest('CASE-2026-0002|DOWNSTREAM_ACTION|v1', 'sha256'), 'hex'),
    '{
        "case_reference": "CASE-2026-0002",
        "action": "CREATE_INCIDENT"
    }'::jsonb,
    now(),
    now()
);

WITH next_message AS (
    SELECT outbox_message_id
    FROM outbox_messages
    WHERE status = 'PENDING'
      AND available_at <= now()
    ORDER BY available_at, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE outbox_messages AS message
SET status = 'PROCESSING',
    attempt_count = message.attempt_count + 1,
    locked_at = now()
FROM next_message
WHERE message.outbox_message_id = next_message.outbox_message_id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM outbox_messages
        WHERE outbox_message_id = '90000000-0000-4000-8000-000000000001'
          AND status = 'PROCESSING'
          AND attempt_count = 1
          AND locked_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'The 1st delivery attempt was not claimed atomically';
    END IF;
END;
$$;

INSERT INTO delivery_attempts (
    delivery_attempt_id,
    outbox_message_id,
    attempt_number,
    outcome,
    http_status,
    response_payload,
    error_code,
    error_message,
    started_at,
    finished_at
) VALUES (
    '91000000-0000-4000-8000-000000000001',
    '90000000-0000-4000-8000-000000000001',
    1,
    'TRANSIENT_FAILURE',
    503,
    '{"status": "temporarily_unavailable"}'::jsonb,
    'DOWNSTREAM_UNAVAILABLE',
    'The sandbox was temporarily unavailable.',
    now() - interval '1 second',
    now()
);

UPDATE outbox_messages
SET status = 'PENDING',
    locked_at = NULL,
    last_error = 'The sandbox was temporarily unavailable.',
    available_at = now()
WHERE outbox_message_id = '90000000-0000-4000-8000-000000000001';

WITH next_message AS (
    SELECT outbox_message_id
    FROM outbox_messages
    WHERE status = 'PENDING'
      AND available_at <= now()
    ORDER BY available_at, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE outbox_messages AS message
SET status = 'PROCESSING',
    attempt_count = message.attempt_count + 1,
    locked_at = now()
FROM next_message
WHERE message.outbox_message_id = next_message.outbox_message_id;

INSERT INTO delivery_attempts (
    delivery_attempt_id,
    outbox_message_id,
    attempt_number,
    outcome,
    http_status,
    downstream_reference,
    response_payload,
    started_at,
    finished_at
) VALUES (
    '91000000-0000-4000-8000-000000000002',
    '90000000-0000-4000-8000-000000000001',
    2,
    'SUCCESS',
    201,
    'SR-2026-0001',
    '{"record_status": "OPEN"}'::jsonb,
    now() - interval '1 second',
    now()
);

UPDATE outbox_messages
SET status = 'SENT',
    locked_at = NULL,
    last_error = NULL,
    completed_at = now()
WHERE outbox_message_id = '90000000-0000-4000-8000-000000000001';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM outbox_messages AS message
        WHERE message.outbox_message_id = '90000000-0000-4000-8000-000000000001'
          AND message.status = 'SENT'
          AND message.attempt_count = 2
          AND message.completed_at IS NOT NULL
          AND (
              SELECT count(*)
              FROM delivery_attempts AS attempt
              WHERE attempt.outbox_message_id = message.outbox_message_id
          ) = 2
          AND (
              SELECT count(*)
              FROM delivery_attempts AS attempt
              WHERE attempt.outbox_message_id = message.outbox_message_id
                AND attempt.outcome = 'TRANSIENT_FAILURE'
          ) = 1
          AND (
              SELECT count(*)
              FROM delivery_attempts AS attempt
              WHERE attempt.outbox_message_id = message.outbox_message_id
                AND attempt.outcome = 'SUCCESS'
                AND attempt.downstream_reference = 'SR-2026-0001'
          ) = 1
    ) THEN
        RAISE EXCEPTION 'The retry-to-success delivery chain is incomplete';
    END IF;
END;
$$;

DO $$
BEGIN
    BEGIN
        INSERT INTO outbox_messages (
            case_id,
            message_type,
            destination,
            idempotency_key,
            payload,
            available_at
        ) VALUES (
            '40000000-0000-4000-8000-000000000002',
            'DOWNSTREAM_ACTION',
            'service-desk-sandbox',
            encode(digest('CASE-2026-0002|DOWNSTREAM_ACTION|v1', 'sha256'), 'hex'),
            '{"action": "CREATE_INCIDENT"}'::jsonb,
            now()
        );
        RAISE EXCEPTION 'A duplicate delivery idempotency key was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO outbox_messages (
            case_id,
            message_type,
            destination,
            idempotency_key,
            payload,
            available_at
        ) VALUES (
            '40000000-0000-4000-8000-000000000002',
            'DOWNSTREAM_ACTION',
            'service-desk-sandbox',
            encode(digest('invalid array payload', 'sha256'), 'hex'),
            '[]'::jsonb,
            now()
        );
        RAISE EXCEPTION 'A non-object delivery payload was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO outbox_messages (
            case_id,
            message_type,
            destination,
            idempotency_key,
            payload,
            status,
            attempt_count,
            available_at
        ) VALUES (
            '40000000-0000-4000-8000-000000000002',
            'DOWNSTREAM_ACTION',
            'service-desk-sandbox',
            encode(digest('processing without lock', 'sha256'), 'hex'),
            '{"action": "CREATE_INCIDENT"}'::jsonb,
            'PROCESSING',
            1,
            now()
        );
        RAISE EXCEPTION 'A processing message without a lock was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO outbox_messages (
            case_id,
            message_type,
            destination,
            idempotency_key,
            payload,
            attempt_count,
            max_attempts,
            available_at
        ) VALUES (
            '40000000-0000-4000-8000-000000000002',
            'DOWNSTREAM_ACTION',
            'service-desk-sandbox',
            encode(digest('attempt limit exceeded', 'sha256'), 'hex'),
            '{"action": "CREATE_INCIDENT"}'::jsonb,
            4,
            3,
            now()
        );
        RAISE EXCEPTION 'An outbox message exceeded its attempt limit';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        UPDATE outbox_messages
        SET payload = '{"action": "MUTATED_ACTION"}'::jsonb
        WHERE outbox_message_id = '90000000-0000-4000-8000-000000000001';
        RAISE EXCEPTION 'A queued delivery intent was mutated';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;
END;
$$;

DO $$
BEGIN
    BEGIN
        INSERT INTO delivery_attempts (
            outbox_message_id,
            attempt_number,
            outcome,
            http_status,
            downstream_reference,
            started_at,
            finished_at
        ) VALUES (
            '90000000-0000-4000-8000-000000000001',
            2,
            'SUCCESS',
            200,
            'SR-DUPLICATE',
            now(),
            now()
        );
        RAISE EXCEPTION 'A duplicate delivery attempt number was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO delivery_attempts (
            outbox_message_id,
            attempt_number,
            outcome,
            http_status,
            started_at,
            finished_at
        ) VALUES (
            '90000000-0000-4000-8000-000000000001',
            3,
            'SUCCESS',
            200,
            now(),
            now()
        );
        RAISE EXCEPTION 'A successful attempt without a reference was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO delivery_attempts (
            outbox_message_id,
            attempt_number,
            outcome,
            http_status,
            started_at,
            finished_at
        ) VALUES (
            '90000000-0000-4000-8000-000000000001',
            3,
            'TRANSIENT_FAILURE',
            503,
            now(),
            now()
        );
        RAISE EXCEPTION 'A failed attempt without error details was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO delivery_attempts (
            outbox_message_id,
            attempt_number,
            outcome,
            http_status,
            error_code,
            error_message,
            started_at,
            finished_at
        ) VALUES (
            '90000000-0000-4000-8000-000000000001',
            3,
            'TRANSIENT_FAILURE',
            700,
            'INVALID_STATUS',
            'The HTTP status is outside the valid range.',
            now(),
            now()
        );
        RAISE EXCEPTION 'An invalid HTTP status was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO delivery_attempts (
            outbox_message_id,
            attempt_number,
            outcome,
            http_status,
            downstream_reference,
            started_at,
            finished_at
        ) VALUES (
            '90000000-0000-4000-8000-000000000001',
            3,
            'SUCCESS',
            200,
            'SR-INVALID-TIME',
            now(),
            now() - interval '1 second'
        );
        RAISE EXCEPTION 'A delivery attempt finished before it started';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        UPDATE delivery_attempts
        SET response_payload = '{"mutated": true}'::jsonb
        WHERE delivery_attempt_id = '91000000-0000-4000-8000-000000000001';
        RAISE EXCEPTION 'An existing delivery attempt was updated';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        DELETE FROM delivery_attempts
        WHERE delivery_attempt_id = '91000000-0000-4000-8000-000000000001';
        RAISE EXCEPTION 'An existing delivery attempt was deleted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;
END;
$$;

SELECT 'PASS: stage 5 reliable delivery database checks' AS result;

ROLLBACK;
