CREATE TABLE IF NOT EXISTS analytics_schema (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    version INTEGER NOT NULL CHECK (version >= 0)
);

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL,
    sha256 VARCHAR NOT NULL CHECK (length(sha256) = 64),
    applied_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE store_metadata (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision UBIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL
);

INSERT INTO store_metadata (singleton, revision, created_at)
VALUES (TRUE, 0, TIMESTAMPTZ '1970-01-01 00:00:00+00')
ON CONFLICT (singleton) DO NOTHING;

-- The typed store validates references itself. DuckDB 1.5 cannot delete child and parent rows
-- atomically when foreign-key indexes are present, which would break previewed cascade deletion.

CREATE TABLE safe_instruments (
    instrument_handle VARCHAR PRIMARY KEY,
    asset_type VARCHAR NOT NULL,
    safe_label VARCHAR,
    source_revision VARCHAR NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    metadata_json VARCHAR NOT NULL
);

CREATE TABLE universes (
    universe_id VARCHAR PRIMARY KEY,
    safe_name VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64)
);

CREATE TABLE universe_instruments (
    universe_id VARCHAR NOT NULL,
    instrument_handle VARCHAR NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (universe_id, instrument_handle)
);

CREATE TABLE source_contracts (
    contract_id VARCHAR PRIMARY KEY,
    source_scope VARCHAR NOT NULL CHECK (source_scope = 'saxo_openapi'),
    contract_name VARCHAR NOT NULL,
    contract_sha256 VARCHAR NOT NULL CHECK (length(contract_sha256) = 64),
    first_seen_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_scope, contract_name, contract_sha256)
);

CREATE TABLE source_pages (
    page_id VARCHAR PRIMARY KEY,
    source_kind VARCHAR NOT NULL,
    page_key VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    contract_id VARCHAR NOT NULL,
    account_scope VARCHAR,
    instrument_handle VARCHAR,
    source_timestamp TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    row_count UBIGINT NOT NULL,
    byte_count UBIGINT NOT NULL,
    logical_key_sha256 VARCHAR NOT NULL UNIQUE CHECK (length(logical_key_sha256) = 64),
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    payload_sha256 VARCHAR NOT NULL CHECK (length(payload_sha256) = 64),
    payload_json VARCHAR NOT NULL
);

CREATE TABLE price_bars (
    page_id VARCHAR NOT NULL,
    instrument_handle VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    bar_time TIMESTAMPTZ NOT NULL,
    duration VARCHAR NOT NULL,
    open_value DOUBLE,
    high_value DOUBLE,
    low_value DOUBLE,
    close_value DOUBLE,
    volume_value DOUBLE,
    currency VARCHAR,
    adjusted BOOLEAN NOT NULL,
    PRIMARY KEY (page_id, instrument_handle, bar_time, duration)
);

CREATE TABLE quotes (
    quote_id VARCHAR PRIMARY KEY,
    page_id VARCHAR NOT NULL,
    instrument_handle VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    bid_value DOUBLE,
    ask_value DOUBLE,
    mid_value DOUBLE,
    currency VARCHAR,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64)
);

CREATE TABLE option_snapshots (
    option_snapshot_id VARCHAR PRIMARY KEY,
    page_id VARCHAR NOT NULL,
    instrument_handle VARCHAR NOT NULL,
    underlying_handle VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    expiry_date DATE NOT NULL,
    strike_value DOUBLE NOT NULL,
    currency VARCHAR NOT NULL,
    put_call VARCHAR NOT NULL,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    payload_json VARCHAR NOT NULL
);

CREATE TABLE datasets (
    dataset_id VARCHAR PRIMARY KEY,
    account_scope VARCHAR NOT NULL,
    source_scope VARCHAR NOT NULL CHECK (source_scope = 'saxo_openapi'),
    source_revision VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    coverage_start TIMESTAMPTZ NOT NULL,
    coverage_end TIMESTAMPTZ NOT NULL,
    quality_state VARCHAR NOT NULL,
    row_count UBIGINT NOT NULL,
    byte_count UBIGINT NOT NULL,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64)
);

CREATE TABLE dataset_source_pages (
    dataset_id VARCHAR NOT NULL,
    page_id VARCHAR NOT NULL,
    PRIMARY KEY (dataset_id, page_id)
);

CREATE TABLE account_snapshots (
    snapshot_id VARCHAR PRIMARY KEY,
    dataset_id VARCHAR NOT NULL,
    page_id VARCHAR,
    snapshot_kind VARCHAR NOT NULL,
    account_scope VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    byte_count UBIGINT NOT NULL,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    payload_json VARCHAR NOT NULL
);

CREATE TABLE transactions (
    transaction_id VARCHAR PRIMARY KEY,
    page_id VARCHAR NOT NULL,
    account_scope VARCHAR NOT NULL,
    instrument_handle VARCHAR,
    source_revision VARCHAR NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    amount_value DOUBLE,
    currency VARCHAR,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    payload_json VARCHAR NOT NULL
);

CREATE TABLE bookings (
    booking_id VARCHAR PRIMARY KEY,
    page_id VARCHAR NOT NULL,
    account_scope VARCHAR NOT NULL,
    instrument_handle VARCHAR,
    source_revision VARCHAR NOT NULL,
    booked_at TIMESTAMPTZ NOT NULL,
    amount_value DOUBLE,
    currency VARCHAR,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    payload_json VARCHAR NOT NULL
);

CREATE TABLE closed_positions (
    closed_position_id VARCHAR PRIMARY KEY,
    page_id VARCHAR NOT NULL,
    account_scope VARCHAR NOT NULL,
    instrument_handle VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL,
    amount_value DOUBLE,
    currency VARCHAR,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    payload_json VARCHAR NOT NULL
);

CREATE TABLE costs (
    cost_id VARCHAR PRIMARY KEY,
    page_id VARCHAR NOT NULL,
    account_scope VARCHAR NOT NULL,
    instrument_handle VARCHAR,
    source_revision VARCHAR NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    amount_value DOUBLE,
    currency VARCHAR,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    payload_json VARCHAR NOT NULL
);

CREATE TABLE analyses (
    analysis_id VARCHAR PRIMARY KEY,
    dataset_id VARCHAR NOT NULL,
    account_scope VARCHAR NOT NULL,
    analysis_kind VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    byte_count UBIGINT NOT NULL,
    fingerprint_sha256 VARCHAR NOT NULL CHECK (length(fingerprint_sha256) = 64),
    result_json VARCHAR NOT NULL
);

CREATE TABLE metrics (
    analysis_id VARCHAR NOT NULL,
    metric_id VARCHAR NOT NULL,
    value DOUBLE NOT NULL,
    unit VARCHAR NOT NULL,
    unit_class VARCHAR NOT NULL,
    currency VARCHAR,
    metric_class VARCHAR NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    proof_profile_id VARCHAR NOT NULL,
    PRIMARY KEY (analysis_id, metric_id)
);

CREATE TABLE artifacts (
    artifact_id VARCHAR PRIMARY KEY,
    analysis_id VARCHAR NOT NULL,
    media_type VARCHAR NOT NULL,
    byte_count UBIGINT NOT NULL,
    sha256 VARCHAR NOT NULL CHECK (length(sha256) = 64),
    created_at TIMESTAMPTZ NOT NULL,
    description VARCHAR NOT NULL,
    visibility VARCHAR NOT NULL
);

CREATE TABLE jobs (
    job_id VARCHAR PRIMARY KEY,
    state VARCHAR NOT NULL,
    request_fingerprint VARCHAR NOT NULL CHECK (length(request_fingerprint) = 64),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    analysis_id VARCHAR,
    message VARCHAR,
    request_json VARCHAR NOT NULL
);

CREATE TABLE proof_receipts (
    analysis_id VARCHAR NOT NULL,
    proof_profile_id VARCHAR NOT NULL,
    analysis_kind VARCHAR NOT NULL,
    source_binding_json VARCHAR NOT NULL,
    engine_binding_json VARCHAR NOT NULL,
    checks_passed_json VARCHAR NOT NULL,
    PRIMARY KEY (analysis_id, proof_profile_id)
);

CREATE TABLE deletion_tokens (
    token_sha256 VARCHAR PRIMARY KEY CHECK (length(token_sha256) = 64),
    scope_fingerprint VARCHAR NOT NULL CHECK (length(scope_fingerprint) = 64),
    normalized_scope_json VARCHAR NOT NULL,
    deletion_plan_json VARCHAR NOT NULL,
    store_revision UBIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE TABLE deletion_receipts (
    receipt_id VARCHAR PRIMARY KEY,
    scope_fingerprint VARCHAR NOT NULL CHECK (length(scope_fingerprint) = 64),
    deleted_at TIMESTAMPTZ NOT NULL,
    table_counts_json VARCHAR NOT NULL,
    estimated_bytes UBIGINT NOT NULL,
    store_revision_before UBIGINT NOT NULL,
    store_revision_after UBIGINT NOT NULL
);
