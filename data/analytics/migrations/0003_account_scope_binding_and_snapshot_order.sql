CREATE TABLE account_scope_bindings (
    account_scope VARCHAR PRIMARY KEY,
    account_selector_sha256 VARCHAR NOT NULL CHECK (
        length(account_selector_sha256) = 64
    ),
    client_selector_sha256 VARCHAR NOT NULL CHECK (
        length(client_selector_sha256) = 64
    )
);

ALTER TABLE account_snapshots ADD COLUMN created_order UBIGINT;

CREATE INDEX account_snapshots_scope_recency_idx
ON account_snapshots (account_scope, snapshot_kind, as_of, created_order);

UPDATE account_snapshots AS snapshots
SET created_order = ordered.created_order
FROM (
    SELECT
        snapshot_id,
        row_number() OVER (ORDER BY as_of, snapshot_id) AS created_order
    FROM account_snapshots
) AS ordered
WHERE snapshots.snapshot_id = ordered.snapshot_id;
