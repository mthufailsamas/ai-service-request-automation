BEGIN;

ALTER SEQUENCE service_record_reference_sequence NO MAXVALUE;

ALTER TABLE service_records
DROP CONSTRAINT service_records_reference_format;

ALTER TABLE service_records
ADD CONSTRAINT service_records_reference_format CHECK (
    service_record_reference ~ '^SR-[0-9]{4}-[0-9]{4,}$'
);

ALTER TABLE service_records
DROP CONSTRAINT service_records_case_reference_format;

ALTER TABLE service_records
ADD CONSTRAINT service_records_case_reference_format CHECK (
    source_case_reference ~ '^CASE-[0-9]{4}-[0-9]{4,}$'
);

COMMENT ON SEQUENCE service_record_reference_sequence IS
    'Collision-safe numeric source for SR-YYYY-NNNN references.';

COMMIT;
