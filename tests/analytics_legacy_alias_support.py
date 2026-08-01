from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import duckdb

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_migrations import LATEST_SCHEMA_VERSION, migrate_store

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def migrate_v2_store_with_unbound_alias(
    config: AnalyticsConfig,
    alias: str,
    *,
    data_kind: Literal["snapshot", "transaction"],
) -> None:
    """Create one historical v2 alias, then migrate it without inventing selectors."""
    store_path = config.paths.store_path
    store_path.unlink()
    connection = duckdb.connect(str(store_path))
    try:
        connection.execute(
            (_PROJECT_ROOT / "data/analytics/migrations/0001_initial.sql").read_text(
                encoding="utf-8",
            ),
        )
        connection.execute(
            (_PROJECT_ROOT / "data/analytics/migrations/0002_source_page_identity.sql").read_text(
                encoding="utf-8",
            ),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, 'initial', ?, current_timestamp)",
            ("0" * 64,),
        )
        connection.execute(
            """
            INSERT INTO schema_migrations
            VALUES (2, 'source_page_identity', ?, current_timestamp)
            """,
            ("1" * 64,),
        )
        connection.execute("INSERT INTO analytics_schema VALUES (TRUE, 2)")
        if data_kind == "transaction":
            connection.execute(
                """
                INSERT INTO transactions (
                    transaction_id, page_id, account_scope, instrument_handle,
                    source_revision, effective_at, amount_value, currency,
                    fingerprint_sha256, payload_json
                )
                VALUES (?, ?, ?, NULL, ?, ?, 1.0, 'DKK', ?, '{}')
                """,
                (
                    "tx_00000000000040008000000000000000",
                    "sp_00000000000040008000000000000000",
                    alias,
                    f"row:{'0' * 64}",
                    datetime(2026, 7, 1, tzinfo=UTC),
                    "2" * 64,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO account_snapshots (
                    snapshot_id, dataset_id, page_id, snapshot_kind, account_scope,
                    source_revision, as_of, byte_count, fingerprint_sha256, payload_json
                )
                VALUES (?, ?, NULL, 'portfolio', ?, ?, ?, 2, ?, '{}')
                """,
                (
                    "ps_00000000000040008000000000000000",
                    "ds_00000000000040008000000000000000",
                    alias,
                    f"capture:{'0' * 64}",
                    datetime(2026, 7, 1, tzinfo=UTC),
                    "2" * 64,
                ),
            )
    finally:
        connection.close()
    store_path.chmod(0o600)
    migrate_store(store_path, LATEST_SCHEMA_VERSION)
