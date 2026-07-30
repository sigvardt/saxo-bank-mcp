from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

_APP_STATE_DIR: Final = "saxo-bank-mcp"
_ANALYTICS_DIR: Final = "analytics"
_ARTIFACTS_DIR: Final = "artifacts"
_STORE_FILE: Final = "analytics.duckdb"
_MIB: Final = 1024 * 1024
_GIB: Final = 1024 * _MIB
_DEFAULT_STORE_QUOTA_GIB: Final = 50
_MAX_STORE_QUOTA_GIB: Final = 1_024
_PATH_OVERRIDE: Final = "SAXO_MCP_ANALYTICS_ROOT"
_QUOTA_OVERRIDE: Final = "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB"


class AnalyticsConfigError(ValueError):
    """Raised when local analytics configuration is unsafe or invalid."""


class AnalyticsLimits(BaseModel):
    """Fixed analytics request limits and the configured storage quota."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    sync_instruments: int = Field(default=25, frozen=True)
    sync_rows: int = Field(default=50_000, frozen=True)
    job_instruments: int = Field(default=100, frozen=True)
    job_rows: int = Field(default=5_000_000, frozen=True)
    response_rows: int = Field(default=500, frozen=True)
    concurrent_jobs: int = Field(default=4, frozen=True)
    artifact_bytes: int = Field(default=25 * _MIB, frozen=True)
    store_quota_bytes: int = Field(default=_DEFAULT_STORE_QUOTA_GIB * _GIB, gt=0, frozen=True)

    @model_validator(mode="after")
    def _validate_public_quota(self) -> AnalyticsLimits:
        fixed_values = (
            (self.sync_instruments, 25),
            (self.sync_rows, 50_000),
            (self.job_instruments, 100),
            (self.job_rows, 5_000_000),
            (self.response_rows, 500),
            (self.concurrent_jobs, 4),
            (self.artifact_bytes, 25 * _MIB),
        )
        if any(actual != expected for actual, expected in fixed_values):
            raise ValueError("analytics request limits are fixed")
        if self.store_quota_bytes != _DEFAULT_STORE_QUOTA_GIB * _GIB:
            raise ValueError("analytics store quota must be loaded from configuration")
        return self

    def can_accept_ingestion(self, current_bytes: int, incoming_bytes: int) -> bool:
        """Return whether an ingestion fits without deleting stored data."""
        return (
            current_bytes >= 0
            and incoming_bytes >= 0
            and current_bytes <= self.store_quota_bytes
            and incoming_bytes <= self.store_quota_bytes - current_bytes
        )


class AnalyticsPaths(BaseModel):
    """Owner-only locations used by the local analytics runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    state_root: Path
    analytics_root: Path
    artifacts_dir: Path
    store_path: Path


class AnalyticsConfig(BaseModel):
    """Immutable analytics runtime configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    limits: AnalyticsLimits
    paths: AnalyticsPaths


def load_analytics_config(env: Mapping[str, str]) -> AnalyticsConfig:
    """Load fixed limits and prepare only paths below the Saxo state directory."""
    if _PATH_OVERRIDE in env:
        raise AnalyticsConfigError(f"{_PATH_OVERRIDE} is not supported")

    state_root = _prepare_owner_only_directory(_saxo_state_root(env))
    resolved_state_root = state_root.resolve(strict=True)
    analytics_root = _path_under_state_root(resolved_state_root, state_root / _ANALYTICS_DIR)
    analytics_root = _prepare_owner_only_directory(analytics_root)
    artifacts_dir = _path_under_state_root(resolved_state_root, analytics_root / _ARTIFACTS_DIR)
    artifacts_dir = _prepare_owner_only_directory(artifacts_dir)
    store_path = _path_under_state_root(resolved_state_root, analytics_root / _STORE_FILE)
    store_path = prepare_owner_only_path(store_path)

    return AnalyticsConfig.model_construct(
        limits=_configured_limits(_store_quota_bytes(env)),
        paths=AnalyticsPaths(
            state_root=resolved_state_root,
            analytics_root=analytics_root,
            artifacts_dir=artifacts_dir,
            store_path=store_path,
        ),
    )


def prepare_owner_only_path(path: Path) -> Path:
    """Create an owner-only regular file and its owner-only parent directory."""
    if path.is_symlink():
        raise AnalyticsConfigError(f"refusing symlink path: {path}")
    parent = _prepare_owner_only_directory(path.parent)
    candidate = parent / path.name
    if candidate.exists() and not candidate.is_file():
        raise AnalyticsConfigError(f"path is not a regular file: {candidate}")
    if not candidate.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(candidate, flags | nofollow, 0o600)
        except OSError as error:
            raise AnalyticsConfigError(f"cannot create owner-only file: {candidate}") from error
        os.close(descriptor)
    candidate.chmod(0o600)
    _require_mode(candidate, 0o600)
    return candidate.resolve(strict=True)


def _saxo_state_root(env: Mapping[str, str]) -> Path:
    configured = env.get("XDG_STATE_HOME", "").strip()
    state_home = Path(configured) if configured else Path.home() / ".local" / "state"
    return state_home.expanduser() / _APP_STATE_DIR


def _store_quota_bytes(env: Mapping[str, str]) -> int:
    value = env.get(_QUOTA_OVERRIDE, str(_DEFAULT_STORE_QUOTA_GIB))
    try:
        quota_gib = int(value)
    except ValueError as error:
        raise AnalyticsConfigError(f"{_QUOTA_OVERRIDE} must be an integer") from error
    if not 1 <= quota_gib <= _MAX_STORE_QUOTA_GIB:
        raise AnalyticsConfigError(
            f"{_QUOTA_OVERRIDE} must be between 1 and {_MAX_STORE_QUOTA_GIB}",
        )
    return quota_gib * _GIB


def _configured_limits(store_quota_bytes: int) -> AnalyticsLimits:
    return AnalyticsLimits.model_construct(store_quota_bytes=store_quota_bytes)


def _path_under_state_root(state_root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(state_root):
        raise AnalyticsConfigError("path escapes Saxo state root")
    return resolved


def _prepare_owner_only_directory(path: Path) -> Path:
    if path.is_symlink():
        raise AnalyticsConfigError(f"refusing symlink directory: {path}")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise AnalyticsConfigError(f"cannot create owner-only directory: {path}") from error
    if not path.is_dir() or path.is_symlink():
        raise AnalyticsConfigError(f"path is not a directory: {path}")
    path.chmod(0o700)
    _require_mode(path, 0o700)
    return path.resolve(strict=True)


def _require_mode(path: Path, expected: int) -> None:
    if stat.S_IMODE(path.stat().st_mode) != expected:
        raise AnalyticsConfigError(f"path mode is not {expected:04o}: {path}")
