BEGIN;

CREATE TABLE cases (
    case_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_reference varchar(32) NOT NULL UNIQUE,
    source_channel varchar(20) NOT NULL,
    external_request_id varchar(100) NOT NULL,
    idempotency_key char(64) NOT NULL UNIQUE,
    content_fingerprint char(64) NOT NULL,
    requester_id uuid NOT NULL REFERENCES users(user_id),
    subject varchar(200) NOT NULL,
    original_message text NOT NULL,
    attachment_metadata jsonb NOT NULL DEFAULT '[]'::jsonb,
    request_type varchar(30),
    ai_summary text,
    current_state varchar(30) NOT NULL DEFAULT 'RECEIVED',
    version integer NOT NULL DEFAULT 1,
    received_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT cases_reference_format CHECK (
        case_reference = upper(btrim(case_reference))
        AND case_reference ~ '^CASE-[0-9]{4}-[0-9]{4,}$'
    ),
    CONSTRAINT cases_source_channel_allowed CHECK (
        source_channel IN ('WEB', 'WEBHOOK')
    ),
    CONSTRAINT cases_external_request_not_blank CHECK (
        btrim(external_request_id) <> ''
    ),
    CONSTRAINT cases_idempotency_key_sha256 CHECK (
        idempotency_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT cases_content_fingerprint_sha256 CHECK (
        content_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT cases_subject_not_blank CHECK (btrim(subject) <> ''),
    CONSTRAINT cases_original_message_not_blank CHECK (
        btrim(original_message) <> ''
    ),
    CONSTRAINT cases_attachments_are_array CHECK (
        jsonb_typeof(attachment_metadata) = 'array'
    ),
    CONSTRAINT cases_request_type_allowed CHECK (
        request_type IS NULL OR request_type IN (
            'POLICY_QUESTION',
            'INCIDENT_REPORT',
            'ACCESS_REQUEST',
            'DATA_CHANGE_REQUEST',
            'STATUS_REQUEST'
        )
    ),
    CONSTRAINT cases_current_state_allowed CHECK (
        current_state IN (
            'RECEIVED',
            'ANALYZING',
            'NEEDS_INFORMATION',
            'NEEDS_REVIEW',
            'PENDING_APPROVAL',
            'READY_FOR_ACTION',
            'COMPLETED',
            'REJECTED',
            'FAILED'
        )
    ),
    CONSTRAINT cases_version_positive CHECK (version > 0),
    CONSTRAINT cases_received_before_created CHECK (received_at <= created_at),
    CONSTRAINT cases_updated_after_created CHECK (updated_at >= created_at),
    CONSTRAINT cases_unique_external_request UNIQUE (
        source_channel,
        external_request_id
    )
);

CREATE INDEX cases_content_fingerprint_idx ON cases (content_fingerprint);
CREATE INDEX cases_requester_state_idx ON cases (requester_id, current_state);

CREATE TABLE case_details (
    case_id uuid PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
    policy_topic text,
    policy_question text,
    affected_system_id uuid REFERENCES managed_systems(system_id),
    incident_description text,
    impact varchar(20),
    urgency varchar(20),
    target_system_id uuid REFERENCES managed_systems(system_id),
    requested_access_level varchar(80),
    business_reason text,
    approver_user_id uuid REFERENCES users(user_id),
    record_reference varchar(100),
    requested_changes text,
    referenced_case_id uuid REFERENCES cases(case_id),
    accepted_by_type varchar(20) NOT NULL,
    accepted_by_user_id uuid REFERENCES users(user_id),
    accepted_at timestamptz NOT NULL,
    CONSTRAINT case_details_impact_allowed CHECK (
        impact IS NULL OR impact IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
    ),
    CONSTRAINT case_details_urgency_allowed CHECK (
        urgency IS NULL OR urgency IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
    ),
    CONSTRAINT case_details_acceptor_allowed CHECK (
        accepted_by_type IN ('SYSTEM_RULE', 'REQUESTER', 'SERVICE_AGENT')
    ),
    CONSTRAINT case_details_acceptor_consistent CHECK (
        (accepted_by_type = 'SYSTEM_RULE' AND accepted_by_user_id IS NULL)
        OR
        (accepted_by_type IN ('REQUESTER', 'SERVICE_AGENT')
            AND accepted_by_user_id IS NOT NULL)
    ),
    CONSTRAINT case_details_policy_topic_not_blank CHECK (
        policy_topic IS NULL OR btrim(policy_topic) <> ''
    ),
    CONSTRAINT case_details_policy_question_not_blank CHECK (
        policy_question IS NULL OR btrim(policy_question) <> ''
    ),
    CONSTRAINT case_details_record_reference_not_blank CHECK (
        record_reference IS NULL OR btrim(record_reference) <> ''
    )
);

CREATE TABLE case_events (
    event_id bigserial PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES cases(case_id),
    sequence_number integer NOT NULL,
    from_state varchar(30),
    to_state varchar(30) NOT NULL,
    event_type varchar(50) NOT NULL,
    actor_type varchar(20) NOT NULL,
    actor_user_id uuid REFERENCES users(user_id),
    reason text NOT NULL,
    event_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT case_events_sequence_positive CHECK (sequence_number > 0),
    CONSTRAINT case_events_creation_sequence_consistent CHECK (
        (sequence_number = 1 AND from_state IS NULL)
        OR
        (sequence_number > 1 AND from_state IS NOT NULL)
    ),
    CONSTRAINT case_events_from_state_allowed CHECK (
        from_state IS NULL OR from_state IN (
            'RECEIVED',
            'ANALYZING',
            'NEEDS_INFORMATION',
            'NEEDS_REVIEW',
            'PENDING_APPROVAL',
            'READY_FOR_ACTION',
            'COMPLETED',
            'REJECTED',
            'FAILED'
        )
    ),
    CONSTRAINT case_events_to_state_allowed CHECK (
        to_state IN (
            'RECEIVED',
            'ANALYZING',
            'NEEDS_INFORMATION',
            'NEEDS_REVIEW',
            'PENDING_APPROVAL',
            'READY_FOR_ACTION',
            'COMPLETED',
            'REJECTED',
            'FAILED'
        )
    ),
    CONSTRAINT case_events_type_not_blank CHECK (btrim(event_type) <> ''),
    CONSTRAINT case_events_actor_type_allowed CHECK (
        actor_type IN ('USER', 'SYSTEM', 'INTEGRATION')
    ),
    CONSTRAINT case_events_actor_consistent CHECK (
        (actor_type = 'USER' AND actor_user_id IS NOT NULL)
        OR
        (actor_type IN ('SYSTEM', 'INTEGRATION') AND actor_user_id IS NULL)
    ),
    CONSTRAINT case_events_reason_not_blank CHECK (btrim(reason) <> ''),
    CONSTRAINT case_events_payload_is_object CHECK (
        jsonb_typeof(event_payload) = 'object'
    ),
    CONSTRAINT case_events_unique_sequence UNIQUE (case_id, sequence_number)
);

CREATE INDEX case_events_case_time_idx ON case_events (case_id, occurred_at);

CREATE FUNCTION preserve_original_case_input()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        NEW.case_reference,
        NEW.source_channel,
        NEW.external_request_id,
        NEW.idempotency_key,
        NEW.content_fingerprint,
        NEW.requester_id,
        NEW.subject,
        NEW.original_message,
        NEW.attachment_metadata,
        NEW.received_at,
        NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.case_reference,
        OLD.source_channel,
        OLD.external_request_id,
        OLD.idempotency_key,
        OLD.content_fingerprint,
        OLD.requester_id,
        OLD.subject,
        OLD.original_message,
        OLD.attachment_metadata,
        OLD.received_at,
        OLD.created_at
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Original case input is immutable';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER cases_preserve_original_input
BEFORE UPDATE ON cases
FOR EACH ROW
EXECUTE FUNCTION preserve_original_case_input();

CREATE FUNCTION prevent_case_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'Case events are append-only';
END;
$$;

CREATE TRIGGER case_events_are_append_only
BEFORE UPDATE OR DELETE ON case_events
FOR EACH ROW
EXECUTE FUNCTION prevent_case_event_mutation();

COMMENT ON TABLE cases IS
    'Original service requests and their current workflow position.';
COMMENT ON TABLE case_details IS
    'Structured request values accepted by rules or an authorized person.';
COMMENT ON TABLE case_events IS
    'Append-only audit history for case state and decision changes.';

COMMIT;
