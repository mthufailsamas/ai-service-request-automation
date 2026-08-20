BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE policy_documents (
    policy_document_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_code varchar(50) NOT NULL,
    title varchar(200) NOT NULL,
    visibility varchar(30) NOT NULL,
    version integer NOT NULL,
    content_sha256 char(64) NOT NULL UNIQUE,
    is_active boolean NOT NULL DEFAULT true,
    valid_from timestamptz NOT NULL,
    valid_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT policy_documents_code_format CHECK (
        policy_code = upper(btrim(policy_code))
        AND policy_code ~ '^[A-Z][A-Z0-9-]{1,49}$'
    ),
    CONSTRAINT policy_documents_title_not_blank CHECK (btrim(title) <> ''),
    CONSTRAINT policy_documents_visibility_allowed CHECK (
        visibility IN (
            'ALL_EMPLOYEES',
            'SERVICE_AGENTS',
            'APPROVERS',
            'ADMINS'
        )
    ),
    CONSTRAINT policy_documents_version_positive CHECK (version > 0),
    CONSTRAINT policy_documents_content_sha256 CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT policy_documents_validity_order CHECK (
        valid_until IS NULL OR valid_until > valid_from
    ),
    CONSTRAINT policy_documents_unique_version UNIQUE (policy_code, version)
);

CREATE INDEX policy_documents_active_visibility_idx
    ON policy_documents (visibility, policy_code)
    WHERE is_active;

CREATE TABLE policy_chunks (
    policy_chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_document_id uuid NOT NULL REFERENCES policy_documents(
        policy_document_id
    ),
    chunk_number integer NOT NULL,
    chunk_text text NOT NULL,
    token_count integer NOT NULL,
    embedding_model varchar(100) NOT NULL,
    embedding vector(1024) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT policy_chunks_number_nonnegative CHECK (chunk_number >= 0),
    CONSTRAINT policy_chunks_text_not_blank CHECK (btrim(chunk_text) <> ''),
    CONSTRAINT policy_chunks_token_count_positive CHECK (token_count > 0),
    CONSTRAINT policy_chunks_embedding_model_not_blank CHECK (
        btrim(embedding_model) <> ''
    ),
    CONSTRAINT policy_chunks_embedding_nonzero CHECK (
        vector_norm(embedding) > 0
    ),
    CONSTRAINT policy_chunks_unique_number UNIQUE (
        policy_document_id,
        chunk_number
    )
);

CREATE INDEX policy_chunks_document_idx
    ON policy_chunks (policy_document_id, chunk_number);

CREATE FUNCTION preserve_policy_document_version()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        NEW.policy_code,
        NEW.title,
        NEW.version,
        NEW.content_sha256,
        NEW.valid_from,
        NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.policy_code,
        OLD.title,
        OLD.version,
        OLD.content_sha256,
        OLD.valid_from,
        OLD.created_at
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Published policy identity and content are immutable';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER policy_documents_preserve_version
BEFORE UPDATE ON policy_documents
FOR EACH ROW
EXECUTE FUNCTION preserve_policy_document_version();

CREATE FUNCTION prevent_policy_chunk_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'Indexed policy chunks are immutable';
END;
$$;

CREATE TRIGGER policy_chunks_are_immutable
BEFORE UPDATE OR DELETE ON policy_chunks
FOR EACH ROW
EXECUTE FUNCTION prevent_policy_chunk_mutation();

COMMENT ON TABLE policy_documents IS
    'Versioned fictional policies with deterministic visibility metadata.';
COMMENT ON TABLE policy_chunks IS
    'Traceable immutable policy passages and their fixed-dimension embeddings.';
COMMENT ON COLUMN policy_chunks.embedding_model IS
    'Exact model or explicit fixture identifier used to create the vector.';

COMMIT;
