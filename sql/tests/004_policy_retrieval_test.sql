\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
    IF (SELECT extversion FROM pg_extension WHERE extname = 'vector') <> '0.8.6' THEN
        RAISE EXCEPTION 'The stage 4 runner must use pgvector 0.8.6';
    END IF;

    IF (SELECT count(*) FROM policy_documents) <> 3
       OR (SELECT count(*) FROM policy_chunks) <> 3 THEN
        RAISE EXCEPTION 'Stage 4 must load 3 fictional policy fixtures';
    END IF;

    IF (
        SELECT count(DISTINCT visibility)
        FROM policy_documents
        WHERE visibility IN ('ALL_EMPLOYEES', 'SERVICE_AGENTS', 'APPROVERS')
    ) <> 3 THEN
        RAISE EXCEPTION 'The fixtures must exercise 3 visibility levels';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM policy_chunks AS c
        JOIN policy_documents AS d USING (policy_document_id)
        WHERE c.embedding_model <> 'database-test-fixture-1024d-v1'
           OR vector_dims(c.embedding) <> 1024
           OR vector_norm(c.embedding) <= 0
           OR d.content_sha256 <> encode(digest(c.chunk_text, 'sha256'), 'hex')
    ) THEN
        RAISE EXCEPTION 'A policy fixture lost its traceable vector contract';
    END IF;

    IF (
        SELECT count(*)
        FROM policy_chunks AS c
        JOIN policy_documents AS d USING (policy_document_id)
    ) <> 3 THEN
        RAISE EXCEPTION 'Every policy chunk must resolve to its document';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM policy_chunks
        WHERE (embedding <=> embedding) IS NULL
           OR abs(embedding <=> embedding) > 0.000001
    ) THEN
        RAISE EXCEPTION 'Exact cosine distance must work for every fixture';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE tablename = 'policy_chunks'
          AND (
              indexdef ILIKE '%USING hnsw%'
              OR indexdef ILIKE '%USING ivfflat%'
          )
    ) THEN
        RAISE EXCEPTION 'The small v1 corpus must not create an approximate index';
    END IF;
END;
$$;

DO $$
BEGIN
    BEGIN
        INSERT INTO policy_documents (
            policy_code,
            title,
            visibility,
            version,
            content_sha256,
            valid_from
        ) SELECT
            policy_code,
            title,
            visibility,
            version,
            encode(digest('duplicate policy version', 'sha256'), 'hex'),
            valid_from
        FROM policy_documents
        WHERE policy_code = 'POL-REMOTE-01';
        RAISE EXCEPTION 'A duplicate policy version was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO policy_documents (
            policy_code,
            title,
            visibility,
            version,
            content_sha256,
            valid_from
        ) VALUES (
            'POL-INVALID-VISIBILITY',
            'Invalid visibility fixture',
            'REQUESTERS',
            1,
            encode(digest('invalid visibility fixture', 'sha256'), 'hex'),
            now()
        );
        RAISE EXCEPTION 'An invalid policy visibility was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO policy_documents (
            policy_code,
            title,
            visibility,
            version,
            content_sha256,
            valid_from,
            valid_until
        ) VALUES (
            'POL-INVALID-DATES',
            'Invalid dates fixture',
            'ALL_EMPLOYEES',
            1,
            encode(digest('invalid dates fixture', 'sha256'), 'hex'),
            now(),
            now() - interval '1 minute'
        );
        RAISE EXCEPTION 'A policy with reversed validity dates was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        UPDATE policy_documents
        SET version = 2
        WHERE policy_code = 'POL-REMOTE-01';
        RAISE EXCEPTION 'An existing policy version was mutated';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;
END;
$$;

UPDATE policy_documents
SET is_active = false,
    valid_until = now()
WHERE policy_code = 'POL-REMOTE-01';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM policy_documents
        WHERE policy_code = 'POL-REMOTE-01'
          AND NOT is_active
          AND valid_until IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'A policy version could not be retired safely';
    END IF;

    BEGIN
        INSERT INTO policy_chunks (
            policy_document_id,
            chunk_number,
            chunk_text,
            token_count,
            embedding_model,
            embedding
        ) VALUES (
            '80000000-0000-4000-8000-000000000001',
            1,
            '   ',
            1,
            'database-test-fixture-1024d-v1',
            array_fill(0.001::real, ARRAY[1024])::vector
        );
        RAISE EXCEPTION 'A blank policy chunk was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO policy_chunks (
            policy_document_id,
            chunk_number,
            chunk_text,
            token_count,
            embedding_model,
            embedding
        ) VALUES (
            '80000000-0000-4000-8000-000000000001',
            0,
            'Duplicate chunk number fixture',
            1,
            'database-test-fixture-1024d-v1',
            array_fill(0.001::real, ARRAY[1024])::vector
        );
        RAISE EXCEPTION 'A duplicate document chunk number was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO policy_chunks (
            policy_document_id,
            chunk_number,
            chunk_text,
            token_count,
            embedding_model,
            embedding
        ) VALUES (
            '80000000-0000-4000-8000-000000000001',
            1,
            'Zero vector fixture',
            1,
            'database-test-fixture-1024d-v1',
            array_fill(0::real, ARRAY[1024])::vector
        );
        RAISE EXCEPTION 'A zero policy vector was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO policy_chunks (
            policy_document_id,
            chunk_number,
            chunk_text,
            token_count,
            embedding_model,
            embedding
        ) VALUES (
            '80000000-0000-4000-8000-000000000001',
            1,
            'Wrong dimension fixture',
            1,
            'database-test-fixture-3d-v1',
            ARRAY[0.1::real, 0.2::real, 0.3::real]::vector
        );
        RAISE EXCEPTION 'A policy vector with 3 dimensions was accepted';
    EXCEPTION
        WHEN data_exception THEN NULL;
    END;

    BEGIN
        INSERT INTO policy_chunks (
            policy_document_id,
            chunk_number,
            chunk_text,
            token_count,
            embedding_model,
            embedding
        ) VALUES (
            '80000000-0000-4000-8000-000000000099',
            0,
            'Missing document fixture',
            1,
            'database-test-fixture-1024d-v1',
            array_fill(0.001::real, ARRAY[1024])::vector
        );
        RAISE EXCEPTION 'A chunk without a policy document was accepted';
    EXCEPTION
        WHEN foreign_key_violation THEN NULL;
    END;

    BEGIN
        UPDATE policy_chunks
        SET chunk_text = 'Mutated indexed content'
        WHERE policy_chunk_id = '81000000-0000-4000-8000-000000000001';
        RAISE EXCEPTION 'An indexed policy chunk was updated';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        DELETE FROM policy_chunks
        WHERE policy_chunk_id = '81000000-0000-4000-8000-000000000001';
        RAISE EXCEPTION 'An indexed policy chunk was deleted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;
END;
$$;

SELECT 'PASS: stage 4 policy retrieval database checks' AS result;

ROLLBACK;
