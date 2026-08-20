BEGIN;

-- These fictional passages are copied from the accepted embedding benchmark.
-- Their vectors and token counts are database-test fixtures, not Qwen output
-- and not retrieval-quality evidence.
INSERT INTO policy_documents (
    policy_document_id,
    policy_code,
    title,
    visibility,
    version,
    content_sha256,
    is_active,
    valid_from,
    created_at
) VALUES
    (
        '80000000-0000-4000-8000-000000000001',
        'POL-REMOTE-01',
        'Remote Work',
        'ALL_EMPLOYEES',
        1,
        encode(digest(
            'Employees assigned to Jakarta may work remotely for up to 2 days in a calendar week. The employee must agree the schedule with their line manager before each remote day.',
            'sha256'
        ), 'hex'),
        true,
        now() - interval '1 day',
        now() - interval '1 day'
    ),
    (
        '80000000-0000-4000-8000-000000000002',
        'POL-PRIVILEGED-01',
        'Temporary Privileged Access',
        'SERVICE_AGENTS',
        1,
        encode(digest(
            'Temporary administrator access requires approval from both the service owner and Information Security. Each privileged session is limited to 8 hours and must be linked to an approved case.',
            'sha256'
        ), 'hex'),
        true,
        now() - interval '1 day',
        now() - interval '1 day'
    ),
    (
        '80000000-0000-4000-8000-000000000003',
        'POL-DATA-CHANGE-01',
        'Master Data Changes',
        'APPROVERS',
        1,
        encode(digest(
            'A master-data change requires a verified change form and an approver who did not prepare the request. Supplier bank-account changes also require a 2nd verification against the registered bank letter.',
            'sha256'
        ), 'hex'),
        true,
        now() - interval '1 day',
        now() - interval '1 day'
    );

INSERT INTO policy_chunks (
    policy_chunk_id,
    policy_document_id,
    chunk_number,
    chunk_text,
    token_count,
    embedding_model,
    embedding,
    created_at
) VALUES
    (
        '81000000-0000-4000-8000-000000000001',
        '80000000-0000-4000-8000-000000000001',
        0,
        'Employees assigned to Jakarta may work remotely for up to 2 days in a calendar week. The employee must agree the schedule with their line manager before each remote day.',
        1,
        'database-test-fixture-1024d-v1',
        array_fill(0.001::real, ARRAY[1024])::vector,
        now() - interval '1 day'
    ),
    (
        '81000000-0000-4000-8000-000000000002',
        '80000000-0000-4000-8000-000000000002',
        0,
        'Temporary administrator access requires approval from both the service owner and Information Security. Each privileged session is limited to 8 hours and must be linked to an approved case.',
        1,
        'database-test-fixture-1024d-v1',
        (
            array_fill(0.001::real, ARRAY[512])
            || array_fill((-0.001)::real, ARRAY[512])
        )::vector,
        now() - interval '1 day'
    ),
    (
        '81000000-0000-4000-8000-000000000003',
        '80000000-0000-4000-8000-000000000003',
        0,
        'A master-data change requires a verified change form and an approver who did not prepare the request. Supplier bank-account changes also require a 2nd verification against the registered bank letter.',
        1,
        'database-test-fixture-1024d-v1',
        array_fill((-0.001)::real, ARRAY[1024])::vector,
        now() - interval '1 day'
    );

COMMIT;
