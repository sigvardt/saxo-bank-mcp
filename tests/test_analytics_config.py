from __future__ import annotations

import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_config import (
    AnalyticsConfig,
    AnalyticsConfigError,
    AnalyticsLimits,
    AnalyticsPaths,
    load_analytics_config,
    prepare_owner_only_path,
)

_SYNC_INSTRUMENTS = 25
_SYNC_ROWS = 50_000
_JOB_INSTRUMENTS = 100
_JOB_ROWS = 5_000_000
_RESPONSE_ROWS = 500
_CONCURRENT_JOBS = 4
_ARTIFACT_BYTES = 25 * 1024 * 1024
_GIB = 1024 * 1024 * 1024
_DEFAULT_QUOTA_BYTES = 50 * 1024 * 1024 * 1024
_MAX_QUOTA_BYTES = 1_024 * _GIB
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
    assert config.limits.artifact_bytes == _ARTIFACT_BYTES
    assert config.limits.store_quota_bytes == _DEFAULT_QUOTA_BYTES
    assert config.limits.can_accept_ingestion(config.limits.store_quota_bytes - 1, 1)
    assert not config.limits.can_accept_ingestion(config.limits.store_quota_bytes - 1, 2)


def test_public_limits_construction_rejects_changed_fixed_limits() -> None:
    with pytest.raises(ValidationError):
        AnalyticsLimits(sync_rows=1, artifact_bytes=1)


@pytest.mark.parametrize(
    "store_quota_bytes",
    [_GIB, 75 * _GIB, _MAX_QUOTA_BYTES],
)
def test_public_limits_construction_accepts_bounded_store_quota(
    store_quota_bytes: int,
) -> None:
    limits = AnalyticsLimits(store_quota_bytes=store_quota_bytes)

    assert limits.store_quota_bytes == store_quota_bytes


@pytest.mark.parametrize(
    "store_quota_bytes",
    [_GIB - 1, _MAX_QUOTA_BYTES + 1],
)
def test_public_limits_construction_rejects_out_of_bounds_store_quota(
    store_quota_bytes: int,
) -> None:
    with pytest.raises(ValidationError):
        AnalyticsLimits(store_quota_bytes=store_quota_bytes)


def test_fixed_limit_instance_mutation_is_rejected() -> None:
    limits = AnalyticsLimits()

    with pytest.raises(ValidationError):
        limits.sync_rows = 1


def test_fixed_limit_class_reassignment_cannot_change_new_instance_values() -> None:
    AnalyticsLimits.sync_rows = 1
    try:
        assert AnalyticsLimits().sync_rows == _SYNC_ROWS
    finally:
        del AnalyticsLimits.sync_rows


def test_limits_model_dump_contains_every_fixed_limit_and_bounded_quota(tmp_path: Path) -> None:
    dumped = load_analytics_config(
        _env(tmp_path / "state", SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB="75"),
    ).limits.model_dump()

    assert dumped == {
        "sync_instruments": _SYNC_INSTRUMENTS,
        "sync_rows": _SYNC_ROWS,
        "job_instruments": _JOB_INSTRUMENTS,
        "job_rows": _JOB_ROWS,
        "response_rows": _RESPONSE_ROWS,
        "concurrent_jobs": _CONCURRENT_JOBS,
        "artifact_bytes": _ARTIFACT_BYTES,
        "store_quota_bytes": 75 * 1024 * 1024 * 1024,
    }


def test_loaded_config_with_bounded_quota_round_trips_from_json(tmp_path: Path) -> None:
    config = load_analytics_config(
        _env(tmp_path / "state", SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB="75"),
    )

    assert AnalyticsConfig.model_validate_json(config.model_dump_json()) == config


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
    ["0", "-1", "1025", "not-a-number", "51.5"],
)
def test_config_rejects_invalid_quota_override(tmp_path: Path, value: str) -> None:
    with pytest.raises(AnalyticsConfigError, match="SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB"):
        load_analytics_config(_env(tmp_path / "state", SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB=value))


def test_config_accepts_bounded_quota_override(tmp_path: Path) -> None:
    config = load_analytics_config(
        _env(tmp_path / "state", SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB="75"),
    )

    assert config.limits.store_quota_bytes == 75 * 1024 * 1024 * 1024


def test_config_rejects_relative_xdg_state_home_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(AnalyticsConfigError, match="absolute"):
        load_analytics_config({"XDG_STATE_HOME": "relative-state"})

    assert not (tmp_path / "relative-state").exists()


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


def _paths_payload(state_root: Path) -> dict[str, Path]:
    analytics_root = state_root / "analytics"
    return {
        "state_root": state_root,
        "analytics_root": analytics_root,
        "artifacts_dir": analytics_root / "artifacts",
        "store_path": analytics_root / "analytics.duckdb",
    }


def test_direct_paths_construction_rejects_relative_state_root() -> None:
    payload = _paths_payload(Path("relative-state"))

    with pytest.raises(ValidationError, match="absolute"):
        AnalyticsPaths(**payload)


@pytest.mark.parametrize(
    "child_field",
    ["analytics_root", "artifacts_dir", "store_path"],
)
def test_direct_paths_construction_rejects_children_outside_state_root(
    tmp_path: Path,
    child_field: str,
) -> None:
    payload = _paths_payload(tmp_path / "state" / "saxo-bank-mcp")
    payload[child_field] = tmp_path / "outside" / child_field

    with pytest.raises(ValidationError, match="state root"):
        AnalyticsPaths(**payload)


def test_direct_paths_construction_rejects_symlink_escape(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "saxo-bank-mcp"
    state_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    analytics_root = state_root / "analytics"
    analytics_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError, match="state root"):
        AnalyticsPaths(
            state_root=state_root,
            analytics_root=analytics_root,
            artifacts_dir=analytics_root / "artifacts",
            store_path=analytics_root / "analytics.duckdb",
        )


def test_direct_config_construction_revalidates_forged_paths(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "saxo-bank-mcp"
    forged_paths = AnalyticsPaths.model_construct(
        **{
            **_paths_payload(state_root),
            "store_path": tmp_path / "outside" / "analytics.duckdb",
        },
    )

    with pytest.raises(ValidationError, match="state root"):
        AnalyticsConfig(limits=AnalyticsLimits(), paths=forged_paths)
