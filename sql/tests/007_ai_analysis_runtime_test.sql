\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
    IF (SELECT count(*) FROM ai_analysis_runs) <> 0 THEN
        RAISE EXCEPTION 'AI-analysis runtime check requires an empty attempt table';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'ai_analysis_runs'
          AND column_name = 'attempt_number'
          AND is_nullable = 'NO'
    ) OR NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'ai_analysis_runs'
          AND column_name = 'completed_at'
          AND data_type = 'timestamp with time zone'
    ) THEN
        RAISE EXCEPTION 'The additive attempt lifecycle columns are incomplete';
    END IF;
END;
$$;

UPDATE cases
SET current_state = 'ANALYZING',
    version = 2,
    updated_at = now()
WHERE case_reference = 'CASE-2026-0002';

INSERT INTO case_events (
    case_id,
    sequence_number,
    from_state,
    to_state,
    event_type,
    actor_type,
    reason
) VALUES (
    '40000000-0000-4000-8000-000000000002',
    2,
    'RECEIVED',
    'ANALYZING',
    'ANALYSIS_STARTED',
    'SYSTEM',
    'The controlled fixture started bounded AI analysis.'
);

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
    output_tokens,
    attempt_number
) VALUES (
    '50000000-0000-4000-8000-000000000011',
    '40000000-0000-4000-8000-000000000002',
    'fixture-provider',
    'fixture-ai-analysis-v1',
    'analysis-v1',
    encode(digest('CASE-2026-0002|analysis-v1', 'sha256'), 'hex'),
    '{}'::jsonb,
    '[]'::jsonb,
    'PROCESSING',
    0,
    0,
    0,
    1
);

UPDATE ai_analysis_runs
SET proposal = '{
        "error": {
            "code": "PROVIDER_TIMEOUT",
            "message": "The local fixture timed out."
        }
    }'::jsonb,
    status = 'FAILED',
    wall_time_ms = 120,
    completed_at = now()
WHERE analysis_run_id = '50000000-0000-4000-8000-000000000011';

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
    output_tokens,
    attempt_number
) VALUES (
    '50000000-0000-4000-8000-000000000012',
    '40000000-0000-4000-8000-000000000002',
    'fixture-provider',
    'fixture-ai-analysis-v1',
    'analysis-v1',
    encode(digest('CASE-2026-0002|analysis-v1', 'sha256'), 'hex'),
    '{}'::jsonb,
    '[]'::jsonb,
    'PROCESSING',
    0,
    0,
    0,
    2
);

UPDATE ai_analysis_runs
SET proposal = '{
        "error": {
            "code": "PROVIDER_UNAVAILABLE",
            "message": "The local fixture remained unavailable."
        }
    }'::jsonb,
    status = 'FAILED',
    wall_time_ms = 80,
    completed_at = now()
WHERE analysis_run_id = '50000000-0000-4000-8000-000000000012';

UPDATE cases
SET current_state = 'FAILED',
    version = 3,
    updated_at = now()
WHERE case_reference = 'CASE-2026-0002';

INSERT INTO case_events (
    case_id,
    sequence_number,
    from_state,
    to_state,
    event_type,
    actor_type,
    reason
) VALUES (
    '40000000-0000-4000-8000-000000000002',
    3,
    'ANALYZING',
    'FAILED',
    'ANALYSIS_FAILED',
    'SYSTEM',
    'Both bounded provider attempts ended in retryable failure.'
);

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
    output_tokens,
    attempt_number,
    completed_at
) VALUES (
    '50000000-0000-4000-8000-000000000013',
    '40000000-0000-4000-8000-000000000001',
    'fixture-provider',
    'fixture-ai-analysis-v1',
    'analysis-v1',
    encode(digest('CASE-2026-0001|oversized', 'sha256'), 'hex'),
    '{"error": {"code": "INPUT_TOO_LARGE"}}'::jsonb,
    '[]'::jsonb,
    'SKIPPED',
    0,
    0,
    0,
    1,
    now()
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
            output_tokens,
            attempt_number,
            completed_at
        ) VALUES (
            '40000000-0000-4000-8000-000000000001',
            'fixture-provider',
            'fixture-ai-analysis-v1',
            'analysis-v1',
            encode(digest('processing with completion time', 'sha256'), 'hex'),
            '{}'::jsonb,
            '[]'::jsonb,
            'PROCESSING',
            0,
            0,
            0,
            1,
            now()
        );
        RAISE EXCEPTION 'PROCESSING with a completion time was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

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
            output_tokens,
            attempt_number
        ) VALUES (
            '40000000-0000-4000-8000-000000000001',
            'fixture-provider',
            'fixture-ai-analysis-v1',
            'analysis-v1',
            encode(digest('terminal without completion time', 'sha256'), 'hex'),
            '{"error": {"code": "INVALID_OUTPUT"}}'::jsonb,
            '[]'::jsonb,
            'INVALID_OUTPUT',
            1,
            0,
            0,
            1
        );
        RAISE EXCEPTION 'A terminal attempt without completion time was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

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
            output_tokens,
            attempt_number
        ) VALUES (
            '40000000-0000-4000-8000-000000000001',
            'fixture-provider',
            'fixture-ai-analysis-v1',
            'analysis-v1',
            encode(digest('attempt beyond v1 limit', 'sha256'), 'hex'),
            '{}'::jsonb,
            '[]'::jsonb,
            'PROCESSING',
            0,
            0,
            0,
            3
        );
        RAISE EXCEPTION 'Attempt 3 was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

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
            output_tokens,
            attempt_number,
            completed_at
        ) VALUES (
            '40000000-0000-4000-8000-000000000001',
            'fixture-provider',
            'fixture-ai-analysis-v1',
            'analysis-v1',
            encode(digest('skipped with model tokens', 'sha256'), 'hex'),
            '{"error": {"code": "INPUT_TOO_LARGE"}}'::jsonb,
            '[]'::jsonb,
            'SKIPPED',
            0,
            1,
            0,
            1,
            now()
        );
        RAISE EXCEPTION 'A skipped attempt with model tokens was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

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
            output_tokens,
            attempt_number
        ) VALUES (
            '40000000-0000-4000-8000-000000000002',
            'fixture-provider',
            'fixture-ai-analysis-v1',
            'analysis-v1',
            encode(digest('CASE-2026-0002|analysis-v1', 'sha256'), 'hex'),
            '{}'::jsonb,
            '[]'::jsonb,
            'PROCESSING',
            0,
            0,
            0,
            2
        );
        RAISE EXCEPTION 'A duplicate analysis attempt identity was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;
END;
$$;

DO $$
BEGIN
    IF (
        SELECT count(*)
        FROM ai_analysis_runs
        WHERE case_id = '40000000-0000-4000-8000-000000000002'
          AND status = 'FAILED'
          AND completed_at IS NOT NULL
    ) <> 2 THEN
        RAISE EXCEPTION 'The 2 bounded failed attempts were not retained';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM cases AS c
        JOIN case_events AS e USING (case_id)
        WHERE c.case_reference = 'CASE-2026-0002'
          AND c.current_state = 'FAILED'
          AND c.version = 3
          AND e.sequence_number = 3
          AND e.from_state = 'ANALYZING'
          AND e.to_state = 'FAILED'
          AND e.event_type = 'ANALYSIS_FAILED'
    ) THEN
        RAISE EXCEPTION 'The ANALYZING to FAILED evidence is incomplete';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM ai_analysis_runs
        WHERE analysis_run_id = '50000000-0000-4000-8000-000000000013'
          AND status = 'SKIPPED'
          AND input_tokens = 0
          AND output_tokens = 0
          AND completed_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'The zero-model-call SKIPPED outcome is incomplete';
    END IF;
END;
$$;

SELECT 'PASS: AI-analysis runtime database foundation checks' AS result;

ROLLBACK;
