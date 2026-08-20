BEGIN;

ALTER TABLE cases
    ADD CONSTRAINT cases_id_and_request_type_unique
    UNIQUE (case_id, request_type);

CREATE TABLE ai_analysis_runs (
    analysis_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases(case_id),
    model_name varchar(100) NOT NULL,
    model_identifier varchar(100) NOT NULL,
    prompt_contract_version varchar(20) NOT NULL,
    input_sha256 char(64) NOT NULL,
    proposal jsonb NOT NULL,
    evidence jsonb NOT NULL,
    status varchar(20) NOT NULL,
    wall_time_ms integer NOT NULL,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ai_analysis_model_name_not_blank CHECK (btrim(model_name) <> ''),
    CONSTRAINT ai_analysis_model_identifier_not_blank CHECK (
        btrim(model_identifier) <> ''
    ),
    CONSTRAINT ai_analysis_prompt_version_not_blank CHECK (
        btrim(prompt_contract_version) <> ''
    ),
    CONSTRAINT ai_analysis_input_sha256 CHECK (
        input_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ai_analysis_proposal_is_object CHECK (
        jsonb_typeof(proposal) = 'object'
    ),
    CONSTRAINT ai_analysis_evidence_is_array CHECK (
        jsonb_typeof(evidence) = 'array'
    ),
    CONSTRAINT ai_analysis_status_allowed CHECK (
        status IN ('COMPLETED', 'INVALID_OUTPUT', 'FAILED')
    ),
    CONSTRAINT ai_analysis_wall_time_nonnegative CHECK (wall_time_ms >= 0),
    CONSTRAINT ai_analysis_input_tokens_nonnegative CHECK (input_tokens >= 0),
    CONSTRAINT ai_analysis_output_tokens_nonnegative CHECK (output_tokens >= 0),
    CONSTRAINT ai_analysis_run_case_unique UNIQUE (analysis_run_id, case_id)
);

CREATE INDEX ai_analysis_case_time_idx
    ON ai_analysis_runs (case_id, created_at);

CREATE TABLE validation_runs (
    validation_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL,
    analysis_run_id uuid NOT NULL,
    overall_decision varchar(30) NOT NULL,
    missing_fields text[] NOT NULL DEFAULT '{}'::text[],
    rule_results jsonb NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT validation_analysis_case_fk FOREIGN KEY (
        analysis_run_id,
        case_id
    ) REFERENCES ai_analysis_runs (analysis_run_id, case_id),
    CONSTRAINT validation_decision_allowed CHECK (
        overall_decision IN (
            'READY',
            'NEEDS_INFORMATION',
            'NEEDS_REVIEW',
            'REJECTED'
        )
    ),
    CONSTRAINT validation_missing_fields_no_null CHECK (
        array_position(missing_fields, NULL) IS NULL
    ),
    CONSTRAINT validation_rule_results_is_array CHECK (
        jsonb_typeof(rule_results) = 'array'
    ),
    CONSTRAINT validation_reason_not_blank CHECK (btrim(reason) <> ''),
    CONSTRAINT validation_ready_has_no_missing_fields CHECK (
        overall_decision <> 'READY' OR cardinality(missing_fields) = 0
    )
);

CREATE INDEX validation_case_time_idx
    ON validation_runs (case_id, created_at);

CREATE FUNCTION validate_rule_result_contract()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(NEW.rule_results) AS rule_result
        WHERE jsonb_typeof(rule_result) <> 'object'
           OR NOT (
                rule_result ? 'rule_code'
                AND rule_result ? 'outcome'
                AND rule_result ? 'field_name'
                AND rule_result ? 'proposed_value'
                AND rule_result ? 'resolved_value'
                AND rule_result ? 'reason'
           )
           OR rule_result->>'outcome' NOT IN ('PASS', 'REVIEW', 'REJECT')
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Each validation rule result must follow the v1 contract';
    END IF;

    IF NEW.overall_decision = 'READY' AND (
        jsonb_array_length(NEW.rule_results) = 0
        OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.rule_results) AS rule_result
            WHERE rule_result->>'outcome' <> 'PASS'
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'READY requires at least 1 rule and every rule must pass';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER validation_rule_result_contract
BEFORE INSERT OR UPDATE ON validation_runs
FOR EACH ROW
EXECUTE FUNCTION validate_rule_result_contract();

CREATE TABLE approvals (
    approval_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL UNIQUE,
    approver_user_id uuid NOT NULL REFERENCES users(user_id),
    request_type varchar(30) NOT NULL,
    decision varchar(20) NOT NULL DEFAULT 'PENDING',
    decision_note text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    CONSTRAINT approvals_case_request_type_fk FOREIGN KEY (
        case_id,
        request_type
    ) REFERENCES cases (case_id, request_type),
    CONSTRAINT approvals_request_type_allowed CHECK (
        request_type IN ('ACCESS_REQUEST', 'DATA_CHANGE_REQUEST')
    ),
    CONSTRAINT approvals_decision_allowed CHECK (
        decision IN ('PENDING', 'APPROVED', 'REJECTED')
    ),
    CONSTRAINT approvals_decision_time_consistent CHECK (
        (decision = 'PENDING' AND decided_at IS NULL)
        OR
        (decision IN ('APPROVED', 'REJECTED') AND decided_at IS NOT NULL)
    ),
    CONSTRAINT approvals_decision_note_not_blank CHECK (
        decision_note IS NULL OR btrim(decision_note) <> ''
    ),
    CONSTRAINT approvals_decided_after_requested CHECK (
        decided_at IS NULL OR decided_at >= requested_at
    )
);

CREATE INDEX approvals_pending_approver_idx
    ON approvals (approver_user_id, requested_at)
    WHERE decision = 'PENDING';

COMMENT ON TABLE ai_analysis_runs IS
    'Untrusted structured proposals and evidence returned by the local model.';
COMMENT ON TABLE validation_runs IS
    'Deterministic rule outcomes that decide whether a proposal may proceed.';
COMMENT ON TABLE approvals IS
    'Consequential access or data-change decisions made by an approver.';

COMMIT;
