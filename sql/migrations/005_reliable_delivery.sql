BEGIN;

CREATE TABLE outbox_messages (
    outbox_message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases(case_id),
    message_type varchar(30) NOT NULL,
    destination varchar(100) NOT NULL,
    idempotency_key char(64) NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'PENDING',
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    available_at timestamptz NOT NULL,
    locked_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT outbox_messages_type_allowed CHECK (
        message_type IN ('DOWNSTREAM_ACTION', 'REQUESTER_NOTIFICATION')
    ),
    CONSTRAINT outbox_messages_destination_not_blank CHECK (
        btrim(destination) <> ''
    ),
    CONSTRAINT outbox_messages_idempotency_key_sha256 CHECK (
        idempotency_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT outbox_messages_payload_is_object CHECK (
        jsonb_typeof(payload) = 'object'
    ),
    CONSTRAINT outbox_messages_status_allowed CHECK (
        status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')
    ),
    CONSTRAINT outbox_messages_attempt_count_nonnegative CHECK (
        attempt_count >= 0
    ),
    CONSTRAINT outbox_messages_max_attempts_positive CHECK (max_attempts > 0),
    CONSTRAINT outbox_messages_attempt_limit CHECK (
        attempt_count <= max_attempts
    ),
    CONSTRAINT outbox_messages_available_after_created CHECK (
        available_at >= created_at
    ),
    CONSTRAINT outbox_messages_lock_after_created CHECK (
        locked_at IS NULL OR locked_at >= created_at
    ),
    CONSTRAINT outbox_messages_completed_after_created CHECK (
        completed_at IS NULL OR completed_at >= created_at
    ),
    CONSTRAINT outbox_messages_last_error_not_blank CHECK (
        last_error IS NULL OR btrim(last_error) <> ''
    ),
    CONSTRAINT outbox_messages_state_consistent CHECK (
        (
            status = 'PENDING'
            AND locked_at IS NULL
            AND completed_at IS NULL
        )
        OR
        (
            status = 'PROCESSING'
            AND attempt_count > 0
            AND locked_at IS NOT NULL
            AND completed_at IS NULL
        )
        OR
        (
            status = 'SENT'
            AND attempt_count > 0
            AND locked_at IS NULL
            AND last_error IS NULL
            AND completed_at IS NOT NULL
        )
        OR
        (
            status = 'FAILED'
            AND attempt_count > 0
            AND locked_at IS NULL
            AND last_error IS NOT NULL
            AND completed_at IS NOT NULL
        )
    )
);

CREATE INDEX outbox_messages_ready_idx
    ON outbox_messages (available_at, created_at)
    WHERE status = 'PENDING';

CREATE INDEX outbox_messages_case_time_idx
    ON outbox_messages (case_id, created_at);

CREATE TABLE delivery_attempts (
    delivery_attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    outbox_message_id uuid NOT NULL REFERENCES outbox_messages(
        outbox_message_id
    ),
    attempt_number integer NOT NULL,
    outcome varchar(30) NOT NULL,
    http_status integer,
    downstream_reference varchar(100),
    response_payload jsonb,
    error_code varchar(100),
    error_message text,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    CONSTRAINT delivery_attempts_number_positive CHECK (attempt_number > 0),
    CONSTRAINT delivery_attempts_outcome_allowed CHECK (
        outcome IN ('SUCCESS', 'TRANSIENT_FAILURE', 'PERMANENT_FAILURE')
    ),
    CONSTRAINT delivery_attempts_http_status_valid CHECK (
        http_status IS NULL OR http_status BETWEEN 100 AND 599
    ),
    CONSTRAINT delivery_attempts_reference_not_blank CHECK (
        downstream_reference IS NULL OR btrim(downstream_reference) <> ''
    ),
    CONSTRAINT delivery_attempts_response_is_object CHECK (
        response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'
    ),
    CONSTRAINT delivery_attempts_error_code_not_blank CHECK (
        error_code IS NULL OR btrim(error_code) <> ''
    ),
    CONSTRAINT delivery_attempts_error_message_not_blank CHECK (
        error_message IS NULL OR btrim(error_message) <> ''
    ),
    CONSTRAINT delivery_attempts_finished_after_started CHECK (
        finished_at >= started_at
    ),
    CONSTRAINT delivery_attempts_outcome_consistent CHECK (
        (
            outcome = 'SUCCESS'
            AND http_status IS NOT NULL
            AND http_status BETWEEN 200 AND 299
            AND downstream_reference IS NOT NULL
            AND error_code IS NULL
            AND error_message IS NULL
        )
        OR
        (
            outcome IN ('TRANSIENT_FAILURE', 'PERMANENT_FAILURE')
            AND downstream_reference IS NULL
            AND error_code IS NOT NULL
            AND error_message IS NOT NULL
        )
    ),
    CONSTRAINT delivery_attempts_unique_number UNIQUE (
        outbox_message_id,
        attempt_number
    )
);

CREATE INDEX delivery_attempts_outbox_time_idx
    ON delivery_attempts (outbox_message_id, started_at);

CREATE FUNCTION preserve_outbox_message_intent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        NEW.case_id,
        NEW.message_type,
        NEW.destination,
        NEW.idempotency_key,
        NEW.payload,
        NEW.max_attempts,
        NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.case_id,
        OLD.message_type,
        OLD.destination,
        OLD.idempotency_key,
        OLD.payload,
        OLD.max_attempts,
        OLD.created_at
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Queued delivery intent is immutable';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER outbox_messages_preserve_intent
BEFORE UPDATE ON outbox_messages
FOR EACH ROW
EXECUTE FUNCTION preserve_outbox_message_intent();

CREATE FUNCTION prevent_delivery_attempt_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'Delivery attempts are append-only';
END;
$$;

CREATE TRIGGER delivery_attempts_are_append_only
BEFORE UPDATE OR DELETE ON delivery_attempts
FOR EACH ROW
EXECUTE FUNCTION prevent_delivery_attempt_mutation();

COMMENT ON TABLE outbox_messages IS
    'Committed delivery intents with idempotency, retry, and lock state.';
COMMENT ON TABLE delivery_attempts IS
    'Append-only evidence for each downstream delivery call.';

COMMIT;
