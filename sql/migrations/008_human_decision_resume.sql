BEGIN;

ALTER TABLE outbox_messages
DROP CONSTRAINT outbox_messages_type_allowed;

ALTER TABLE outbox_messages
ADD CONSTRAINT outbox_messages_type_allowed CHECK (
    message_type IN (
        'WORKFLOW_START',
        'HUMAN_DECISION_RESUME',
        'DOWNSTREAM_ACTION',
        'REQUESTER_NOTIFICATION'
    )
);

COMMENT ON CONSTRAINT outbox_messages_type_allowed ON outbox_messages IS
    'Accepted durable delivery intents, including committed human-decision resume.';

COMMIT;
