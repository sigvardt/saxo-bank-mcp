from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from saxo_bank_mcp.agent_skill_install_env import (
    EnvironmentContainmentError,
    build_isolated_env,
    write_bearing_env_keys,
)
from saxo_bank_mcp.agent_skill_install_paths import ensure_owner_only
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config_credentials import DEFAULT_SIM_CREDENTIAL_FILE
from saxo_bank_mcp.token_cache import (
    TokenCachePathError,
    default_token_cache_path,
    load_token_cache,
    save_token_cache,
)
from saxo_bank_mcp.token_cache import (
    token_cache_path as resolve_token_cache_path,
)

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
    # Continuity markers for optimistic SIM promotion only — never evidence fields.
    sim_token_source: Path = field(repr=False)
    sim_token_source_digest: str = field(repr=False)


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
        expanded = candidate.expanduser()
    except OSError as exc:
        raise MatrixEnvError("child_out_invalid") from exc
    # Reject the leaf symlink before resolve() follows it to a different node.
    if expanded.is_symlink():
        raise MatrixEnvError("child_out_symlink")
    try:
        resolved = expanded.resolve()
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
    if resolved.is_symlink():
        raise MatrixEnvError("child_out_symlink")

    parent = resolved.parent
    try:
        ensure_owner_only(parent)
    except (OSError, PermissionError) as exc:
        raise MatrixEnvError("child_out_create_failed") from exc
    return resolved


def prepare_matrix_child_receipt_path(path: Path) -> Path:
    """Reject unsafe child out nodes and remove any prior regular receipt.

    Only a post-spawn newly created regular file at this absolute path may be
    consumed. Symlinks and non-regular nodes fail closed with stable reasons.
    """
    if path.is_symlink():
        raise MatrixEnvError("child_out_symlink")
    if not path.exists():
        return path
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise MatrixEnvError("child_out_stale_remove_failed") from exc
    if not stat.S_ISREG(mode):
        raise MatrixEnvError("child_out_not_regular")
    try:
        path.unlink()
    except OSError as exc:
        raise MatrixEnvError("child_out_stale_remove_failed") from exc
    if path.exists() or path.is_symlink():
        raise MatrixEnvError("child_out_stale_remove_failed")
    return path


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
        token_source, token_source_digest = _resolve_sim_token_source_with_digest()
        token_cache_path = _copy_owner_only_file(
            token_source,
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
            sim_token_source=token_source,
            sim_token_source_digest=token_source_digest,
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


def promote_rotated_sim_token_cache(runtime: MatrixIsolatedRuntime) -> None:
    """Optimistically promote a rotated SIM token from the disposable cache to source.

    No-op when the contained copy is unchanged. Fail closed with a stable sanitized
    reason when promotion is required but validation, concurrency, write, or
    verification fails. Never publishes tokens, account IDs, private paths, or digests.
    """
    contained_digest = _regular_file_digest(runtime.token_cache_path)
    if contained_digest is not None and contained_digest == runtime.sim_token_source_digest:
        return

    token = _load_promotable_sim_token(runtime.token_cache_path)
    destination = _resolve_promotion_destination(runtime.sim_token_source)
    _require_owner_only_regular_file(
        destination,
        reason="token_promote_destination_invalid",
    )
    if _regular_file_digest(destination) != runtime.sim_token_source_digest:
        raise MatrixEnvError("token_promote_source_changed")
    _persist_and_verify_promotion(destination, token)


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


def _load_promotable_sim_token(contained: Path) -> SaxoTokenSet:
    _require_owner_only_regular_file(
        contained,
        reason="token_promote_contained_invalid",
    )
    token = load_token_cache(contained)
    if token is None:
        raise MatrixEnvError("token_promote_token_invalid")
    if token.environment != "SIM":
        raise MatrixEnvError("token_promote_environment_not_sim")
    if token.refresh_material() is None:
        raise MatrixEnvError("token_promote_refresh_missing")
    return token


def _resolve_promotion_destination(source: Path) -> Path:
    try:
        return resolve_token_cache_path(source)
    except TokenCachePathError as exc:
        raise MatrixEnvError("token_promote_destination_refused") from exc


def _persist_and_verify_promotion(destination: Path, token: SaxoTokenSet) -> None:
    try:
        save_token_cache(destination, token)
    except OSError as exc:
        raise MatrixEnvError("token_promote_write_failed") from exc
    try:
        _require_owner_only_regular_file(
            destination,
            reason="token_promote_verify_failed",
        )
        reloaded = load_token_cache(destination)
    except MatrixEnvError:
        raise
    except OSError as exc:
        raise MatrixEnvError("token_promote_verify_failed") from exc
    if reloaded is None or reloaded != token:
        raise MatrixEnvError("token_promote_verify_failed")


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


def _resolve_sim_token_source_with_digest() -> tuple[Path, str]:
    source = _resolve_sim_token_cache_source()
    if source.is_symlink() or not source.is_file():
        raise MatrixEnvError("token_cache_missing")
    try:
        mode = source.lstat().st_mode
    except OSError as exc:
        raise MatrixEnvError("token_cache_missing") from exc
    if not stat.S_ISREG(mode):
        raise MatrixEnvError("token_cache_missing")
    try:
        resolved = source.resolve()
        digest = _regular_file_digest(resolved)
    except OSError as exc:
        raise MatrixEnvError("token_cache_copy_failed") from exc
    if digest is None:
        raise MatrixEnvError("token_cache_copy_failed")
    return resolved, digest


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


def _regular_file_digest(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return None
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _owner_only_regular_file_reason(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return "symlink"
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            return "not_regular"
        if (mode & 0o777) != OWNER_FILE_MODE:
            return "permissions"
    except OSError:
        return "os_error"
    return None


def _require_owner_only_regular_file(path: Path, *, reason: str) -> None:
    if _owner_only_regular_file_reason(path) is not None:
        raise MatrixEnvError(reason)


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
