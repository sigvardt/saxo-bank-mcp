CREATE TABLE source_pages_v2 (
    page_id VARCHAR PRIMARY KEY,
    source_kind VARCHAR NOT NULL,
    page_key VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    source_native_revision VARCHAR NOT NULL,
    contract_id VARCHAR NOT NULL,
    account_scope VARCHAR,
    instrument_handle VARCHAR,
    instrument_scope_sha256 VARCHAR CHECK (
        instrument_scope_sha256 IS NULL OR length(instrument_scope_sha256) = 64
    ),
    source_timestamp TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    row_count UBIGINT NOT NULL,
    byte_count UBIGINT NOT NULL,
    logical_key_sha256 VARCHAR NOT NULL UNIQUE CHECK (length(logical_key_sha256) = 64),
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    payload_sha256 VARCHAR NOT NULL CHECK (length(payload_sha256) = 64),
    payload_json VARCHAR NOT NULL
);

INSERT INTO source_pages_v2
SELECT
    page_id,
    source_kind,
    page_key,
    source_revision,
    source_revision,
    contract_id,
    account_scope,
    instrument_handle,
    NULL,
    source_timestamp,
    ingested_at,
    row_count,
    byte_count,
    sha256('legacy-v1-logical:' || page_id),
    sha256('legacy-v1-material:' || page_id || ':' || payload_sha256),
    payload_sha256,
    payload_json
FROM source_pages;

DROP TABLE source_pages;
ALTER TABLE source_pages_v2 RENAME TO source_pages;
