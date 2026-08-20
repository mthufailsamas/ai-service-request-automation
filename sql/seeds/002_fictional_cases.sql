BEGIN;

-- Stage 2 stores raw requests before AI analysis. Request type and accepted
-- structured details therefore remain empty at intake. Content fingerprints
-- match the accepted intake contract: SHA-256 of canonical sorted-key JSON
-- containing normalized requester_id, subject, and message.
INSERT INTO cases (
    case_id,
    case_reference,
    source_channel,
    external_request_id,
    idempotency_key,
    content_fingerprint,
    requester_id,
    subject,
    original_message,
    attachment_metadata,
    received_at,
    created_at,
    updated_at
) VALUES
    (
        '40000000-0000-4000-8000-000000000001',
        'CASE-2026-0001',
        'WEB',
        'WEB-2026-0001',
        encode(digest('WEB|WEB-2026-0001', 'sha256'), 'hex'),
        'aaeac1e0260b95ae7cb6294799648072e8c9e85c1059dda268bf7becf6bad167',
        '10000000-0000-4000-8000-000000000001',
        'WMS viewer access',
        'Please give me WMS viewer access for weekly inventory reconciliation. MGR-104 is my approver.',
        '[]'::jsonb,
        now() - interval '10 minutes',
        now() - interval '10 minutes',
        now() - interval '10 minutes'
    ),
    (
        '40000000-0000-4000-8000-000000000002',
        'CASE-2026-0002',
        'WEBHOOK',
        'HOOK-2026-0001',
        encode(digest('WEBHOOK|HOOK-2026-0001', 'sha256'), 'hex'),
        '1c28276c931a46b3040538e4a6d273ae96b35f4dc3599aadfd0c5a826cb6ae08',
        '10000000-0000-4000-8000-000000000002',
        'Portal gudang tidak dapat dibuka',
        'Portal gudang tidak bisa dibuka sejak pukul 09.15 WIB.',
        '[{"name": "error-screen.png", "media_type": "image/png"}]'::jsonb,
        now() - interval '5 minutes',
        now() - interval '5 minutes',
        now() - interval '5 minutes'
    );

INSERT INTO case_events (
    case_id,
    sequence_number,
    from_state,
    to_state,
    event_type,
    actor_type,
    actor_user_id,
    reason,
    event_payload,
    occurred_at
) VALUES
    (
        '40000000-0000-4000-8000-000000000001',
        1,
        NULL,
        'RECEIVED',
        'CASE_RECEIVED',
        'USER',
        '10000000-0000-4000-8000-000000000001',
        'Request submitted through the web form.',
        '{"source": "WEB"}'::jsonb,
        now() - interval '10 minutes'
    ),
    (
        '40000000-0000-4000-8000-000000000002',
        1,
        NULL,
        'RECEIVED',
        'CASE_RECEIVED',
        'INTEGRATION',
        NULL,
        'Request accepted through the REST webhook.',
        '{"source": "WEBHOOK"}'::jsonb,
        now() - interval '5 minutes'
    );

COMMIT;
