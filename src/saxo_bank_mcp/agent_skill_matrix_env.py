from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from saxo_bank_mcp.agent_skill_install_env import (
    EnvironmentContainmentError,
    build_isolated_env,
    write_bearing_env_keys,
)
from saxo_bank_mcp.agent_skill_install_paths import ensure_owner_only
from saxo_bank_mcp.config_credentials import DEFAULT_SIM_CREDENTIAL_FILE
from saxo_bank_mcp.token_cache import default_token_cache_path

MATRIX_RUNTIME_NAME: Final = "matrix-runtime"
DEFAULT_SIM_REDIRECT_URI: Final = "http://localhost:8080/callback"
_AUTH_DIR_NAME: Final = "auth"
_SIM_CREDENTIAL_NAME: Final = "sim-credentials"
_TOKEN_CACHE_BASENAME: Final = "token-cache.json"  # noqa: S105 - filename, not a secret
OWNER_FILE_MODE: Final = 0o600
OWNER_DIR_MODE: Final = 0o700


class MatrixEnvError(ValueError):
    """Sanitized failure for Todo 15 matrix isolated environment setup/cleanup."""

    def __init__(self, reason: str) -> None:  # noqa: D107
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MatrixIsolatedRuntime:
    run_root: Path
    env: dict[str, str]
    home: Path
    codex_home: Path
    probe_env: Path
    auth_dir: Path
    sim_credential_path: Path
    token_cache_path: Path


def matrix_runtime_root(evidence_root: Path) -> Path:
    """Disposable Todo 15 runtime under the evidence root that owns options.out."""
    return evidence_root.resolve() / MATRIX_RUNTIME_NAME


def resolve_matrix_child_evidence_path(
    candidate: Path,
    *,
    evidence_root: Path,
    installed_cache: Path,
    runtime_root: Path,
) -> Path:
    """Resolve a child subprocess out/evidence path under the Todo 15 evidence root.

    Always returns an absolute path resolved in the producer process (never relative
    to the installed-cache cwd). Fails closed with a stable sanitized reason when
    the path escapes the evidence root, aliases the installed cache or disposable
    auth runtime, or cannot be prepared owner-only.
    """
    try:
        root = evidence_root.resolve()
        cache = installed_cache.resolve()
        runtime = runtime_root.resolve()
        resolved = candidate.expanduser().resolve()
    except OSError as exc:
        raise MatrixEnvError("child_out_invalid") from exc

    if not resolved.is_relative_to(root):
        raise MatrixEnvError("child_out_outside_evidence_root")
    if resolved == cache or resolved.is_relative_to(cache):
        raise MatrixEnvError("child_out_aliases_installed_cache")
    if resolved == runtime or resolved.is_relative_to(runtime):
        raise MatrixEnvError("child_out_aliases_matrix_runtime")
    if not resolved.is_absolute():
        raise MatrixEnvError("child_out_invalid")

    parent = resolved.parent
    try:
        ensure_owner_only(parent)
    except (OSError, PermissionError) as exc:
        raise MatrixEnvError("child_out_create_failed") from exc
    return resolved


def prepare_matrix_isolated_runtime(evidence_root: Path) -> MatrixIsolatedRuntime:
    """Create owner-only disposable roots and SIM-only auth copies under evidence_root."""
    run_root = matrix_runtime_root(evidence_root)
    if run_root.exists():
        residual = cleanup_matrix_isolated_runtime(run_root)
        if residual:
            raise MatrixEnvError("matrix_runtime_cleanup_residue")
    try:
        run_root.mkdir(parents=True, exist_ok=True)
        ensure_owner_only(run_root)
        home = run_root / "home"
        codex_home = run_root / "codex-home"
        probe_env = run_root / "probe-env"
        auth_dir = run_root / _AUTH_DIR_NAME
        for path in (home, codex_home, probe_env, auth_dir):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(OWNER_DIR_MODE)
        sim_credential_path = _copy_owner_only_file(
            _resolve_sim_credential_source(),
            auth_dir / _SIM_CREDENTIAL_NAME,
            missing_reason="sim_credentials_missing",
            copy_reason="sim_credential_copy_failed",
        )
        token_cache_path = _copy_owner_only_file(
            _resolve_sim_token_cache_source(),
            auth_dir / _TOKEN_CACHE_BASENAME,
            missing_reason="token_cache_missing",
            copy_reason="token_cache_copy_failed",
        )
        auth_targets = {
            "SAXO_MCP_SIM_CREDENTIAL_FILE": sim_credential_path,
            "SAXO_MCP_TOKEN_CACHE_PATH": token_cache_path,
        }
        env = build_isolated_env(
            home=home,
            codex_home=codex_home,
            run_root=run_root,
            probe_env=probe_env,
            auth_targets=auth_targets,
        )
        env["SAXO_MCP_ENVIRONMENT"] = "SIM"
        env["SAXO_MCP_ENABLE_LIVE_READS"] = "0"
        env["SAXO_MCP_ENABLE_LIVE_WRITES"] = ""
        env["SAXO_MCP_SIM_REDIRECT_URI"] = _resolve_sim_redirect_uri()
        _assert_no_live_auth(env)
        _assert_write_bearing_under_run_root(env, run_root)
        return MatrixIsolatedRuntime(
            run_root=run_root,
            env=env,
            home=home,
            codex_home=codex_home,
            probe_env=probe_env,
            auth_dir=auth_dir,
            sim_credential_path=sim_credential_path,
            token_cache_path=token_cache_path,
        )
    except MatrixEnvError:
        cleanup_matrix_isolated_runtime(run_root)
        raise
    except EnvironmentContainmentError as exc:
        cleanup_matrix_isolated_runtime(run_root)
        raise MatrixEnvError(str(exc.reason)) from exc
    except OSError as exc:
        cleanup_matrix_isolated_runtime(run_root)
        raise MatrixEnvError("matrix_runtime_setup_failed") from exc


def cleanup_matrix_isolated_runtime(run_root: Path) -> list[str]:
    """Remove the disposable matrix runtime tree. Returns residual path labels only."""
    if not run_root.exists() and not run_root.is_symlink():
        return []
    residual: list[str] = []
    try:
        if run_root.is_dir() and not run_root.is_symlink():
            shutil.rmtree(run_root)
        else:
            run_root.unlink(missing_ok=True)
    except OSError:
        residual.append("matrix_runtime")
    if run_root.exists() or run_root.is_symlink():
        residual.append("matrix_runtime")
    return sorted(set(residual))


def require_matrix_runtime_cleanup(run_root: Path) -> None:
    residual = cleanup_matrix_isolated_runtime(run_root)
    if residual:
        raise MatrixEnvError("matrix_runtime_cleanup_residue")


def _resolve_sim_credential_source() -> Path:
    raw = os.environ.get("SAXO_MCP_SIM_CREDENTIAL_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_SIM_CREDENTIAL_FILE


def _resolve_sim_token_cache_source() -> Path:
    raw = os.environ.get("SAXO_MCP_TOKEN_CACHE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_token_cache_path()


def _resolve_sim_redirect_uri() -> str:
    configured = os.environ.get("SAXO_MCP_SIM_REDIRECT_URI", "").strip()
    return configured or DEFAULT_SIM_REDIRECT_URI


def _copy_owner_only_file(
    source: Path,
    target: Path,
    *,
    missing_reason: str,
    copy_reason: str,
) -> Path:
    if not source.is_file():
        raise MatrixEnvError(missing_reason)
    try:
        shutil.copy2(source, target)
        target.chmod(OWNER_FILE_MODE)
    except OSError as exc:
        raise MatrixEnvError(copy_reason) from exc
    mode = target.stat().st_mode & 0o777
    if mode != OWNER_FILE_MODE:
        raise MatrixEnvError("auth_permissions_invalid")
    if not stat.S_ISREG(target.stat().st_mode):
        raise MatrixEnvError("auth_permissions_invalid")
    return target.resolve()


def _assert_no_live_auth(env: dict[str, str]) -> None:
    live_keys = (
        "SAXO_MCP_LIVE_CREDENTIAL_FILE",
        "SAXO_MCP_LIVE_APP_KEY",
        "SAXO_MCP_LIVE_CLIENT_ID",
        "SAXO_MCP_LIVE_TOKEN_CACHE_PATH",
    )
    for key in live_keys:
        if env.get(key):
            raise MatrixEnvError("live_auth_inherited")


def _assert_write_bearing_under_run_root(env: dict[str, str], run_root: Path) -> None:
    root = run_root.resolve()
    for key in write_bearing_env_keys():
        value = env.get(key)
        if not value:
            continue
        path = Path(value).expanduser()
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise MatrixEnvError(f"path_unresolvable:{key}") from exc
        if not resolved.is_relative_to(root):
            raise MatrixEnvError(f"path_outside_run_root:{key}")
