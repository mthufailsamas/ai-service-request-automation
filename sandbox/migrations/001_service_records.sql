BEGIN;

CREATE SEQUENCE service_record_reference_sequence
    AS bigint
    START WITH 1
    MAXVALUE 9999
    NO CYCLE;

CREATE TABLE service_records (
    service_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_record_reference varchar(32) NOT NULL UNIQUE,
    delivery_idempotency_key char(64) NOT NULL UNIQUE,
    request_sha256 char(64) NOT NULL,
    source_case_reference varchar(32) NOT NULL,
    source_case_version integer NOT NULL,
    action_type varchar(30) NOT NULL,
    title varchar(200) NOT NULL,
    summary text NOT NULL,
    details jsonb NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'ACCEPTED',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT service_records_reference_format CHECK (
        service_record_reference ~ '^SR-[0-9]{4}-[0-9]{4}$'
    ),
    CONSTRAINT service_records_idempotency_key_sha256 CHECK (
        delivery_idempotency_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT service_records_request_sha256 CHECK (
        request_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT service_records_case_reference_format CHECK (
        source_case_reference ~ '^CASE-[0-9]{4}-[0-9]{4}$'
    ),
    CONSTRAINT service_records_case_version_positive CHECK (
        source_case_version > 0
    ),
    CONSTRAINT service_records_action_type_allowed CHECK (
        action_type IN (
            'POLICY_RESPONSE',
            'INCIDENT_TICKET',
            'ACCESS_ACTION',
            'DATA_CHANGE_ACTION',
            'STATUS_RESPONSE'
        )
    ),
    CONSTRAINT service_records_title_not_blank CHECK (btrim(title) <> ''),
    CONSTRAINT service_records_summary_not_blank CHECK (btrim(summary) <> ''),
    CONSTRAINT service_records_details_is_object CHECK (
        jsonb_typeof(details) = 'object'
    ),
    CONSTRAINT service_records_status_allowed CHECK (status = 'ACCEPTED')
);

CREATE TABLE service_record_events (
    service_record_event_id bigserial PRIMARY KEY,
    service_record_id uuid REFERENCES service_records(service_record_id),
    delivery_idempotency_key char(64) NOT NULL,
    request_sha256 char(64) NOT NULL,
    sequence_number integer NOT NULL,
    event_type varchar(30) NOT NULL,
    event_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT service_record_events_idempotency_key_sha256 CHECK (
        delivery_idempotency_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT service_record_events_request_sha256 CHECK (
        request_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT service_record_events_sequence_positive CHECK (
        sequence_number > 0
    ),
    CONSTRAINT service_record_events_type_allowed CHECK (
        event_type IN (
            'RECORD_CREATED',
            'IDEMPOTENT_REPLAY',
            'IDEMPOTENCY_CONFLICT',
            'TRANSIENT_FAILURE',
            'PERMANENT_FAILURE'
        )
    ),
    CONSTRAINT service_record_events_payload_is_object CHECK (
        jsonb_typeof(event_payload) = 'object'
    ),
    CONSTRAINT service_record_events_key_sequence_unique UNIQUE (
        delivery_idempotency_key,
        sequence_number
    )
);

CREATE INDEX service_record_events_key_time_idx
    ON service_record_events (delivery_idempotency_key, occurred_at);

CREATE FUNCTION prevent_service_record_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'Accepted service records are immutable';
END;
$$;

CREATE TRIGGER service_records_are_immutable
BEFORE UPDATE OR DELETE ON service_records
FOR EACH ROW
EXECUTE FUNCTION prevent_service_record_mutation();

CREATE FUNCTION prevent_service_record_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'Service record events are append-only';
END;
$$;

CREATE TRIGGER service_record_events_are_append_only
BEFORE UPDATE OR DELETE ON service_record_events
FOR EACH ROW
EXECUTE FUNCTION prevent_service_record_event_mutation();

COMMENT ON TABLE service_records IS
    'Immutable outcomes accepted by the local Service Desk Sandbox.';
COMMENT ON TABLE service_record_events IS
    'Append-only evidence for creation, replay, conflict, and controlled failure calls.';

COMMIT;
