from __future__ import annotations

import stat
from pathlib import Path

import pytest

from saxo_bank_mcp.analytics_config import (
    AnalyticsConfigError,
    load_analytics_config,
    prepare_owner_only_path,
)

_SYNC_INSTRUMENTS = 25
_SYNC_ROWS = 50_000
_JOB_INSTRUMENTS = 100
_JOB_ROWS = 5_000_000
_RESPONSE_ROWS = 500
_CONCURRENT_JOBS = 4
_OWNER_DIRECTORY_MODE = 0o700
_OWNER_FILE_MODE = 0o600


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _env(state_home: Path, **overrides: str) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(state_home), **overrides}


def test_config_has_fixed_request_limits_and_refuses_over_quota(tmp_path: Path) -> None:
    config = load_analytics_config(_env(tmp_path / "state"))

    assert config.limits.sync_instruments == _SYNC_INSTRUMENTS
    assert config.limits.sync_rows == _SYNC_ROWS
    assert config.limits.job_instruments == _JOB_INSTRUMENTS
    assert config.limits.job_rows == _JOB_ROWS
    assert config.limits.response_rows == _RESPONSE_ROWS
    assert config.limits.concurrent_jobs == _CONCURRENT_JOBS
    assert config.limits.artifact_bytes == _SYNC_INSTRUMENTS * 1024 * 1024
    assert config.limits.store_quota_bytes == 50 * 1024 * 1024 * 1024
    assert config.limits.can_accept_ingestion(config.limits.store_quota_bytes - 1, 1)
    assert not config.limits.can_accept_ingestion(config.limits.store_quota_bytes - 1, 2)


def test_config_creates_owner_only_directories_and_files(tmp_path: Path) -> None:
    config = load_analytics_config(_env(tmp_path / "state"))

    assert _mode(config.paths.state_root) == _OWNER_DIRECTORY_MODE
    assert _mode(config.paths.analytics_root) == _OWNER_DIRECTORY_MODE
    assert _mode(config.paths.artifacts_dir) == _OWNER_DIRECTORY_MODE
    assert _mode(config.paths.store_path) == _OWNER_FILE_MODE


def test_prepare_owner_only_path_creates_an_owner_only_file(tmp_path: Path) -> None:
    path = prepare_owner_only_path(tmp_path / "owner" / "result.duckdb")

    assert path == (tmp_path / "owner" / "result.duckdb").resolve()
    assert _mode(path.parent) == _OWNER_DIRECTORY_MODE
    assert _mode(path) == _OWNER_FILE_MODE


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "not-a-number", "51.5"],
)
def test_config_rejects_invalid_quota_override(tmp_path: Path, value: str) -> None:
    with pytest.raises(AnalyticsConfigError, match="SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB"):
        load_analytics_config(_env(tmp_path / "state", SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB=value))


def test_config_accepts_bounded_quota_override(tmp_path: Path) -> None:
    config = load_analytics_config(
        _env(tmp_path / "state", SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB="75"),
    )

    assert config.limits.store_quota_bytes == 75 * 1024 * 1024 * 1024


def test_config_rejects_arbitrary_analytics_path_override(tmp_path: Path) -> None:
    with pytest.raises(AnalyticsConfigError, match="SAXO_MCP_ANALYTICS_ROOT"):
        load_analytics_config(
            _env(
                tmp_path / "state",
                SAXO_MCP_ANALYTICS_ROOT=str(tmp_path / "outside"),
            ),
        )


def test_config_rejects_symlink_that_escapes_owner_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "saxo-bank-mcp"
    state_root.mkdir(parents=True)
    (state_root / "analytics").symlink_to(tmp_path / "outside")

    with pytest.raises(AnalyticsConfigError, match="path escapes Saxo state root"):
        load_analytics_config(_env(tmp_path / "state"))
