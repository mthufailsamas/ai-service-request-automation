BEGIN;

-- A database sequence gives concurrent case creation 1 collision-safe source
-- for readable references. Gaps after a rolled-back transaction are valid.
CREATE SEQUENCE case_reference_sequence START WITH 1;

SELECT setval(
    'case_reference_sequence',
    COALESCE(
        MAX(substring(case_reference FROM '([0-9]+)$')::bigint),
        0
    ) + 1,
    false
)
FROM cases;

ALTER TABLE outbox_messages
DROP CONSTRAINT outbox_messages_type_allowed;

ALTER TABLE outbox_messages
ADD CONSTRAINT outbox_messages_type_allowed CHECK (
    message_type IN (
        'WORKFLOW_START',
        'DOWNSTREAM_ACTION',
        'REQUESTER_NOTIFICATION'
    )
);

COMMENT ON SEQUENCE case_reference_sequence IS
    'Collision-safe numeric source for CASE-YYYY-NNNN references.';

COMMIT;
