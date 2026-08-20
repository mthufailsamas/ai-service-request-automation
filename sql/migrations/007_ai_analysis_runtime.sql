BEGIN;

ALTER TABLE ai_analysis_runs
    ADD COLUMN attempt_number integer;

WITH numbered_attempts AS (
    SELECT
        analysis_run_id,
        row_number() OVER (
            PARTITION BY
                case_id,
                input_sha256,
                prompt_contract_version,
                model_identifier
            ORDER BY created_at, analysis_run_id
        ) AS attempt_number
    FROM ai_analysis_runs
)
UPDATE ai_analysis_runs AS analysis
SET attempt_number = numbered.attempt_number
FROM numbered_attempts AS numbered
WHERE analysis.analysis_run_id = numbered.analysis_run_id;

ALTER TABLE ai_analysis_runs
    ALTER COLUMN attempt_number SET NOT NULL,
    ADD COLUMN completed_at timestamptz;

-- Before this lifecycle migration, terminal analysis rows were inserted only
-- after their provider outcome was known. Their creation time is therefore the
-- best available durable completion time for a truthful additive backfill.
UPDATE ai_analysis_runs
SET completed_at = created_at
WHERE status IN ('COMPLETED', 'INVALID_OUTPUT', 'FAILED');

ALTER TABLE ai_analysis_runs
    DROP CONSTRAINT ai_analysis_status_allowed,
    ADD CONSTRAINT ai_analysis_status_allowed CHECK (
        status IN (
            'PROCESSING',
            'COMPLETED',
            'INVALID_OUTPUT',
            'FAILED',
            'SKIPPED'
        )
    ),
    ADD CONSTRAINT ai_analysis_attempt_within_v1_limit CHECK (
        attempt_number BETWEEN 1 AND 2
    ),
    ADD CONSTRAINT ai_analysis_completion_consistent CHECK (
        (status = 'PROCESSING' AND completed_at IS NULL)
        OR
        (status <> 'PROCESSING' AND completed_at IS NOT NULL)
    ),
    ADD CONSTRAINT ai_analysis_completion_after_creation CHECK (
        completed_at IS NULL OR completed_at >= created_at
    ),
    ADD CONSTRAINT ai_analysis_skipped_has_no_model_tokens CHECK (
        status <> 'SKIPPED'
        OR (input_tokens = 0 AND output_tokens = 0)
    ),
    ADD CONSTRAINT ai_analysis_attempt_identity_unique UNIQUE (
        case_id,
        input_sha256,
        prompt_contract_version,
        model_identifier,
        attempt_number
    );

COMMENT ON COLUMN ai_analysis_runs.attempt_number IS
    'Bounded provider attempt number for one exact analysis identity.';
COMMENT ON COLUMN ai_analysis_runs.completed_at IS
    'UTC time when a processing attempt reached its durable terminal status.';

COMMIT;
