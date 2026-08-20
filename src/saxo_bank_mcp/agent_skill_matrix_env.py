from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, cast

from saxo_bank_mcp.agent_skill_install_env import (
    EnvironmentContainmentError,
    build_isolated_env,
    write_bearing_env_keys,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    ensure_owner_only,
    export_publishable_tree,
    publishable_tracked_files,
)
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config_credentials import DEFAULT_SIM_CREDENTIAL_FILE
from saxo_bank_mcp.qa_codex_native_policy import HarnessPolicy
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
EVAL_RUNTIME_NAME: Final = "eval-runtime"
DEFAULT_SIM_REDIRECT_URI: Final = "http://localhost:8080/callback"
SIM_ORDER_LIFECYCLE_CASE_ID: Final = "sim-order-lifecycle"
SIM_ORDER_LIFECYCLE_INSTRUMENT_UIC: Final = "211"
_AUTH_DIR_NAME: Final = "auth"
_SIM_CREDENTIAL_NAME: Final = "sim-credentials"
_TOKEN_CACHE_BASENAME: Final = "token-cache.json"  # noqa: S105 - filename, not a secret
OWNER_FILE_MODE: Final = 0o600
OWNER_DIR_MODE: Final = 0o700
MAX_CLAUDE_CREDENTIAL_BYTES: Final = 1_048_576
MIN_CLAUDE_OAUTH_MATERIAL_LENGTH: Final = 20
# Actual CLI auth sources only — never settings, hooks, MCP config, projects/history, or sessions.
_CODEX_AUTH_SEED_FILES: Final = ("auth.json",)
_CLAUDE_AUTH_SEED_RELATIVES: Final = (Path(".claude") / ".credentials.json",)
# Retained install plugin registration/artifacts only (options.codex_home / options.claude_home).
_CODEX_PLUGIN_SEED_FILES: Final = ("config.toml",)
_CODEX_PLUGIN_SEED_RELATIVES: Final = (Path("plugins") / "index.json",)
_CLAUDE_PLUGIN_SEED_RELATIVES: Final = (
    Path(".claude") / "plugins" / "installed_plugins.json",
    Path(".claude") / "plugins" / "known_marketplaces.json",
)
# Exact basenames only — never substring-match plugin package names.
_EXCLUDED_SEED_NAMES: Final = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "history",
        "history.jsonl",
        "node_modules",
        "transcripts",
        "transcript",
        "sessions",
        "session-store",
        "hooks",
        "logs",
        "debug",
    },
)


class MatrixEnvError(ValueError):
    """Sanitized failure for Todo 15 matrix/eval isolated environment setup/cleanup."""

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
    claude_auth_source: Path | None = field(default=None, repr=False)
    claude_auth_source_digest: str | None = field(default=None, repr=False)


def matrix_runtime_root(evidence_root: Path) -> Path:
    """Disposable Todo 15 matrix runtime under the evidence root that owns options.out."""
    return isolated_runtime_root(evidence_root, MATRIX_RUNTIME_NAME)


def eval_runtime_root(evidence_root: Path) -> Path:
    """Disposable dual-harness eval runtime under the evidence root that owns options.out."""
    return isolated_runtime_root(evidence_root, EVAL_RUNTIME_NAME)


def isolated_runtime_root(evidence_root: Path, runtime_name: str) -> Path:
    """Disposable isolated runtime directory under the evidence root."""
    invalid = (
        not runtime_name
        or "/" in runtime_name
        or "\\" in runtime_name
        or runtime_name in {".", ".."}
    )
    if invalid:
        raise MatrixEnvError("runtime_name_invalid")
    return evidence_root.resolve() / runtime_name


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


def prepare_matrix_isolated_runtime(
    evidence_root: Path,
    *,
    runtime_name: str = MATRIX_RUNTIME_NAME,
    include_claude_client: bool = True,
) -> MatrixIsolatedRuntime:
    """Create owner-only disposable roots and SIM-only auth copies under evidence_root."""
    run_root = isolated_runtime_root(evidence_root, runtime_name)
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
            include_claude_client=include_claude_client,
        )
        env["SAXO_MCP_ENVIRONMENT"] = "SIM"
        env["SAXO_MCP_ENABLE_LIVE_READS"] = "0"
        env["SAXO_MCP_ENABLE_LIVE_WRITES"] = ""
        env["SAXO_MCP_SIM_REDIRECT_URI"] = _resolve_sim_redirect_uri()
        _assert_no_live_auth(env)
        _assert_write_bearing_under_run_root(env, run_root)
        _assert_parent_secrets_stripped(env)
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


def prepare_eval_isolated_runtime(  # noqa: PLR0913
    evidence_root: Path,
    *,
    source_codex_home: Path | None,
    source_claude_home: Path | None,
    retained_codex_home: Path | None = None,
    retained_claude_home: Path | None = None,
    retained_codex_plugin_root: Path | None = None,
    harness_policy: HarnessPolicy = "dual_v1",
) -> MatrixIsolatedRuntime:
    """SIM-only eval runtime under evidence_root with task-created CLI homes.

    Auth sources (source_*) default to actual global CLI roots when omitted.
    Retained install homes seed plugin registration/artifacts only. Never conflate.
    Sources are never published as evidence fields. Validated credential rotations are
    promoted optimistically after child cleanup so one-time refresh material is not lost.
    Account discovery remains inside the logical FastMCP matrix call path.
    """
    if harness_policy not in ("dual_v1", "codex_native_v1"):
        raise MatrixEnvError("eval_harness_policy_unknown")
    runtime = prepare_matrix_isolated_runtime(
        evidence_root,
        runtime_name=EVAL_RUNTIME_NAME,
        include_claude_client=harness_policy != "codex_native_v1",
    )
    try:
        if harness_policy == "codex_native_v1":
            _seed_codex_native_cli_home(
                runtime,
                source_codex_home=source_codex_home,
                retained_codex_plugin_root=retained_codex_plugin_root,
            )
            claude_auth_source = None
            claude_auth_source_digest = None
        elif harness_policy == "dual_v1":
            claude_auth_source, claude_auth_source_digest = seed_isolated_cli_homes(
                runtime,
                source_codex_home=source_codex_home,
                source_claude_home=source_claude_home,
                retained_codex_home=retained_codex_home,
                retained_claude_home=retained_claude_home,
                retained_codex_plugin_root=retained_codex_plugin_root,
            )
    except MatrixEnvError:
        cleanup_matrix_isolated_runtime(runtime.run_root)
        raise
    except OSError as exc:
        cleanup_matrix_isolated_runtime(runtime.run_root)
        raise MatrixEnvError("eval_cli_home_seed_failed") from exc
    return replace(
        runtime,
        claude_auth_source=claude_auth_source,
        claude_auth_source_digest=claude_auth_source_digest,
    )


def _seed_codex_native_cli_home(
    runtime: MatrixIsolatedRuntime,
    *,
    source_codex_home: Path | None,
    retained_codex_plugin_root: Path | None,
) -> None:
    """Seed Codex auth, then register the retained plugin via the proven CLI flow."""
    codex_auth = _resolve_codex_source_home(source_codex_home)
    for name in _CODEX_AUTH_SEED_FILES:
        copied = _copy_optional_owner_only_file(
            codex_auth / name,
            runtime.codex_home / name,
            copy_reason="codex_auth_copy_failed",
        )
        if copied is None:
            raise MatrixEnvError("codex_file_auth_missing")
    if retained_codex_plugin_root is not None:
        _register_codex_native_plugin(runtime, retained_codex_plugin_root)
    for path in (runtime.home, runtime.codex_home):
        path.chmod(OWNER_DIR_MODE)


def _register_codex_native_plugin(
    runtime: MatrixIsolatedRuntime,
    retained_plugin_root: Path,
) -> Path:
    """Use the same marketplace-add/plugin-add flow as the isolated installer."""
    from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError  # noqa: PLC0415
    from saxo_bank_mcp.agent_skill_install_cli_driver import (  # noqa: PLC0415
        run_codex_install,
    )
    from saxo_bank_mcp.agent_skill_install_paths import (  # noqa: PLC0415
        MARKETPLACE_NAME,
        PLUGIN_NAME,
    )

    source = runtime.codex_home / "marketplace-source"
    try:
        _copy_plugin_source_tree(
            retained_plugin_root.expanduser().resolve(strict=True),
            source,
        )
        marketplace, plugin, version = codex_plugin_identity(source)
        if marketplace != MARKETPLACE_NAME or plugin != PLUGIN_NAME:
            raise MatrixEnvError("codex_plugin_registration_invalid")  # noqa: TRY301
        run_codex_install(source, runtime.env)
        installed = (
            runtime.codex_home / "plugins" / "cache" / marketplace / plugin / version
        ).resolve(strict=True)
        if not installed.is_dir() or not installed.is_relative_to(runtime.codex_home.resolve()):
            raise MatrixEnvError("codex_plugin_registration_invalid")  # noqa: TRY301
        _tighten_owner_only_tree(installed)
    except MatrixEnvError:
        raise
    except (CommandFailureError, OSError, TypeError, ValueError) as exc:
        raise MatrixEnvError("codex_plugin_registration_failed") from exc
    return installed


def codex_plugin_identity(root: Path) -> tuple[str, str, str]:
    marketplace_raw: object = json.loads(
        (root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    plugin_raw: object = json.loads(
        (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    if not isinstance(marketplace_raw, dict) or not isinstance(plugin_raw, dict):
        raise MatrixEnvError("codex_plugin_registration_invalid")
    marketplace = cast("dict[str, object]", marketplace_raw)
    plugin_document = cast("dict[str, object]", plugin_raw)
    marketplace_name = marketplace.get("name")
    plugin_name = plugin_document.get("name")
    version = plugin_document.get("version")
    entries = marketplace.get("plugins")
    matching_entry = isinstance(entries, list) and any(
        isinstance(entry, dict)
        and cast("dict[str, object]", entry).get("name") == plugin_name
        and cast("dict[str, object]", entry).get("version") == version
        for entry in cast("list[object]", entries)
    )
    if (
        not isinstance(marketplace_name, str)
        or not marketplace_name
        or not isinstance(plugin_name, str)
        or not plugin_name
        or not isinstance(version, str)
        or not version
        or not matching_entry
    ):
        raise MatrixEnvError("codex_plugin_registration_invalid")
    return marketplace_name, plugin_name, version


def _tighten_owner_only_tree(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise MatrixEnvError("codex_plugin_registration_invalid")
        if path.is_dir():
            path.chmod(OWNER_DIR_MODE)
        elif path.is_file():
            path.chmod(OWNER_FILE_MODE)
        else:
            raise MatrixEnvError("codex_plugin_registration_invalid")


def apply_case_eval_allowlists(env: dict[str, str], *, case_id: str) -> dict[str, str]:
    """Return env with case-specific SIM safety allowlists (instrument for lifecycle)."""
    out = dict(env)
    if case_id == SIM_ORDER_LIFECYCLE_CASE_ID:
        out["SAXO_MCP_INSTRUMENT_ALLOWLIST"] = SIM_ORDER_LIFECYCLE_INSTRUMENT_UIC
    return out


def seed_isolated_cli_homes(  # noqa: PLR0913
    runtime: MatrixIsolatedRuntime,
    *,
    source_codex_home: Path | None,
    source_claude_home: Path | None,
    retained_codex_home: Path | None = None,
    retained_claude_home: Path | None = None,
    retained_codex_plugin_root: Path | None = None,
) -> tuple[Path, str]:
    """Seed auth from actual CLI roots; seed plugin state from retained install homes."""
    claude_auth_source, claude_auth_source_digest = _seed_auth_files(
        runtime,
        source_codex_home,
        source_claude_home,
    )
    _seed_retained_plugin_registration(runtime, retained_codex_home, retained_claude_home)
    if retained_codex_plugin_root is not None:
        _seed_codex_plugin_tree(
            runtime,
            retained_codex_home=retained_codex_home,
            retained_plugin_root=retained_codex_plugin_root,
        )
    for path in (runtime.home, runtime.codex_home, runtime.home / ".claude"):
        if path.exists():
            path.chmod(OWNER_DIR_MODE)
    return claude_auth_source, claude_auth_source_digest


def _seed_auth_files(
    runtime: MatrixIsolatedRuntime,
    source_codex_home: Path | None,
    source_claude_home: Path | None,
) -> tuple[Path, str]:
    codex_auth = _resolve_codex_source_home(source_codex_home)
    claude_auth = _resolve_claude_source_home(source_claude_home)
    for name in _CODEX_AUTH_SEED_FILES:
        copied = _copy_optional_owner_only_file(
            codex_auth / name,
            runtime.codex_home / name,
            copy_reason="codex_auth_copy_failed",
        )
        if copied is None:
            raise MatrixEnvError("codex_file_auth_missing")
    for relative in _CLAUDE_AUTH_SEED_RELATIVES:
        declared = claude_auth / relative
        if declared.is_symlink():
            raise MatrixEnvError("cli_auth_source_symlink")
        if not os.path.lexists(declared):
            raise MatrixEnvError("claude_file_auth_missing")
        source = declared.resolve()
        _require_owner_only_single_link_file(
            source,
            reason="claude_auth_source_unsafe",
        )
        digest = _regular_file_digest(source)
        if digest is None:
            raise MatrixEnvError("claude_auth_source_unsafe")
        copied = _copy_optional_owner_only_file(
            source,
            runtime.home / relative,
            copy_reason="claude_auth_copy_failed",
        )
        if copied is None:
            raise MatrixEnvError("claude_file_auth_missing")
        if _regular_file_digest(source) != digest or _regular_file_digest(copied) != digest:
            raise MatrixEnvError("claude_auth_source_unsafe")
        return source, digest
    raise MatrixEnvError("claude_file_auth_missing")


def _seed_retained_plugin_registration(
    runtime: MatrixIsolatedRuntime,
    retained_codex_home: Path | None,
    retained_claude_home: Path | None,
) -> None:
    if retained_codex_home is not None:
        retained_codex = retained_codex_home.expanduser()
        for name in _CODEX_PLUGIN_SEED_FILES:
            _copy_optional_owner_only_file(
                retained_codex / name,
                runtime.codex_home / name,
                copy_reason="codex_plugin_state_copy_failed",
            )
        for relative in _CODEX_PLUGIN_SEED_RELATIVES:
            _copy_optional_owner_only_file(
                retained_codex / relative,
                runtime.codex_home / relative,
                copy_reason="codex_plugin_state_copy_failed",
            )
    if retained_claude_home is not None:
        retained_claude = retained_claude_home.expanduser()
        for relative in _CLAUDE_PLUGIN_SEED_RELATIVES:
            _copy_optional_owner_only_file(
                retained_claude / relative,
                runtime.home / relative,
                copy_reason="claude_plugin_state_copy_failed",
            )


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


def promote_rotated_claude_credentials(runtime: MatrixIsolatedRuntime) -> None:
    """Promote one validated file-backed Claude OAuth rotation before cleanup."""
    source = runtime.claude_auth_source
    source_digest = runtime.claude_auth_source_digest
    if source is None or source_digest is None:
        return
    contained = runtime.home / ".claude" / ".credentials.json"
    contained_digest = _regular_file_digest(contained)
    if contained_digest is not None and contained_digest == source_digest:
        return
    payload = _load_promotable_claude_credentials(contained)
    _require_owner_only_single_link_file(
        source,
        reason="claude_auth_promote_destination_invalid",
    )
    if _regular_file_digest(source) != source_digest:
        raise MatrixEnvError("claude_auth_promote_source_changed")
    _persist_and_verify_claude_credentials(source, payload)


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


def _load_promotable_claude_credentials(contained: Path) -> bytes:
    _require_owner_only_single_link_file(
        contained,
        reason="claude_auth_promote_contained_invalid",
    )
    try:
        payload = contained.read_bytes()
        raw_document = json.loads(payload)
    except (OSError, TypeError, ValueError) as exc:
        raise MatrixEnvError("claude_auth_promote_credentials_invalid") from exc
    if not isinstance(raw_document, dict):
        raise MatrixEnvError("claude_auth_promote_credentials_invalid")
    document = cast("dict[str, object]", raw_document)
    if len(payload) > MAX_CLAUDE_CREDENTIAL_BYTES:
        raise MatrixEnvError("claude_auth_promote_credentials_invalid")
    raw_oauth = document.get("claudeAiOauth")
    if not isinstance(raw_oauth, dict):
        raise MatrixEnvError("claude_auth_promote_credentials_invalid")
    oauth = cast("dict[str, object]", raw_oauth)
    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    expires_at = oauth.get("expiresAt")
    refresh_expires_at = oauth.get("refreshTokenExpiresAt")
    if (
        not isinstance(access, str)
        or len(access) < MIN_CLAUDE_OAUTH_MATERIAL_LENGTH
        or not isinstance(refresh, str)
        or len(refresh) < MIN_CLAUDE_OAUTH_MATERIAL_LENGTH
        or not isinstance(expires_at, int)
        or expires_at <= 0
        or not isinstance(refresh_expires_at, int)
        or refresh_expires_at <= 0
    ):
        raise MatrixEnvError("claude_auth_promote_credentials_invalid")
    return payload


def _persist_and_verify_claude_credentials(destination: Path, payload: bytes) -> None:
    pending: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, raw_pending = tempfile.mkstemp(
            prefix=f".{destination.name}.promotion-",
            dir=destination.parent,
        )
        pending = Path(raw_pending)
        os.fchmod(descriptor, OWNER_FILE_MODE)
        _write_all_and_fsync(descriptor, payload)
        os.close(descriptor)
        descriptor = None
        pending.replace(destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if pending is not None:
            with suppress(OSError):
                pending.unlink(missing_ok=True)
        raise MatrixEnvError("claude_auth_promote_write_failed") from exc
    _require_owner_only_single_link_file(
        destination,
        reason="claude_auth_promote_verify_failed",
    )
    try:
        verified = destination.read_bytes()
    except OSError as exc:
        raise MatrixEnvError("claude_auth_promote_verify_failed") from exc
    if verified != payload:
        raise MatrixEnvError("claude_auth_promote_verify_failed")


def _write_all_and_fsync(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("credential promotion write made no progress")
        written += count
    os.fsync(descriptor)


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
    if source.is_symlink():
        raise MatrixEnvError("auth_source_symlink")
    try:
        mode = source.lstat().st_mode
    except OSError as exc:
        raise MatrixEnvError(missing_reason) from exc
    if not stat.S_ISREG(mode):
        raise MatrixEnvError(missing_reason)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.chmod(OWNER_DIR_MODE)
        shutil.copy2(source, target, follow_symlinks=False)
        target.chmod(OWNER_FILE_MODE)
    except OSError as exc:
        raise MatrixEnvError(copy_reason) from exc
    try:
        target_mode = target.lstat().st_mode
    except OSError as exc:
        raise MatrixEnvError("auth_permissions_invalid") from exc
    if not stat.S_ISREG(target_mode) or (target_mode & 0o777) != OWNER_FILE_MODE:
        raise MatrixEnvError("auth_permissions_invalid")
    if target.is_symlink():
        raise MatrixEnvError("auth_permissions_invalid")
    return target.resolve()


def _copy_optional_owner_only_file(
    source: Path,
    target: Path,
    *,
    copy_reason: str,
) -> Path | None:
    """Copy a regular source file when present. Symlinks/non-regular nodes fail closed."""
    if not os.path.lexists(source):
        return None
    if source.is_symlink():
        raise MatrixEnvError("cli_auth_source_symlink")
    try:
        mode = source.lstat().st_mode
    except OSError as exc:
        raise MatrixEnvError(copy_reason) from exc
    if not stat.S_ISREG(mode):
        raise MatrixEnvError("cli_auth_source_not_regular")
    return _copy_owner_only_file(
        source,
        target,
        missing_reason=copy_reason,
        copy_reason=copy_reason,
    )


def _resolve_codex_source_home(preferred: Path | None) -> Path:
    """Actual global CLI auth root. Never falls back to retained install homes."""
    if preferred is not None:
        return preferred.expanduser()
    raw = os.environ.get("CODEX_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def _resolve_claude_source_home(preferred: Path | None) -> Path:
    """Actual global CLI auth root. Never falls back to retained install homes."""
    if preferred is not None:
        return preferred.expanduser()
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().parent
    return Path.home()


def resolve_actual_codex_auth_home(preferred: Path | None = None) -> Path:
    return _resolve_codex_source_home(preferred)


def resolve_actual_claude_auth_home(preferred: Path | None = None) -> Path:
    return _resolve_claude_source_home(preferred)


def _seed_codex_plugin_tree(
    runtime: MatrixIsolatedRuntime,
    *,
    retained_codex_home: Path | None,
    retained_plugin_root: Path,
) -> Path:
    """Byte-exact owner-only copy of retained installed plugin into disposable CODEX_HOME."""
    plugin = retained_plugin_root.expanduser()
    if plugin.is_symlink():
        raise MatrixEnvError("codex_plugin_source_symlink")
    try:
        plugin_resolved = plugin.resolve()
    except OSError as exc:
        raise MatrixEnvError("codex_plugin_seed_failed") from exc
    if not plugin_resolved.is_dir() or plugin_resolved.is_symlink():
        raise MatrixEnvError("codex_plugin_source_not_dir")
    if retained_codex_home is not None:
        home = retained_codex_home.expanduser().resolve()
        if plugin_resolved.is_relative_to(home):
            relative = plugin_resolved.relative_to(home)
            target = runtime.codex_home / relative
            _copy_plugin_source_tree(plugin_resolved, target)
            return target
    # Fallback placement preserves plugins/cache layout without absolute path leakage.
    target = runtime.codex_home / "plugins" / "cache" / "retained" / plugin_resolved.name
    _copy_plugin_source_tree(plugin_resolved, target)
    return target


def _copy_plugin_source_tree(source: Path, target: Path) -> None:
    """Copy a checkout's publishable files, or a retained cache's safe tree."""
    publishable = publishable_tracked_files(source)
    if publishable:
        export_publishable_tree(source, target, publishable=publishable)
        _tighten_owner_only_tree(target)
        return
    _copy_owner_only_tree(source, target)


def _copy_owner_only_tree(source: Path, target: Path) -> None:
    """Copy regular files only; reject symlinks/special nodes; skip histories/hooks noise."""
    _require_owner_seed_dir(source)
    try:
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(OWNER_DIR_MODE)
        for root, dirnames, filenames in os.walk(source, followlinks=False):
            root_path = Path(root)
            dirnames[:] = _filter_seed_dirnames(root_path, dirnames)
            dest_root = _dest_root_for(source, target, root_path)
            dest_root.mkdir(parents=True, exist_ok=True)
            dest_root.chmod(OWNER_DIR_MODE)
            for name in filenames:
                if _is_excluded_seed_name(name):
                    continue
                _copy_seed_regular_file(root_path / name, dest_root / name)
    except MatrixEnvError:
        raise
    except OSError as exc:
        raise MatrixEnvError("codex_plugin_seed_failed") from exc


def _require_owner_seed_dir(source: Path) -> None:
    if source.is_symlink():
        raise MatrixEnvError("codex_plugin_source_symlink")
    try:
        source_mode = source.lstat().st_mode
    except OSError as exc:
        raise MatrixEnvError("codex_plugin_seed_failed") from exc
    if not stat.S_ISDIR(source_mode):
        raise MatrixEnvError("codex_plugin_source_not_dir")


def _filter_seed_dirnames(root_path: Path, dirnames: list[str]) -> list[str]:
    return [
        name
        for name in dirnames
        if not _is_excluded_seed_name(name) and not (root_path / name).is_symlink()
    ]


def _dest_root_for(source: Path, target: Path, root_path: Path) -> Path:
    rel_root = root_path.relative_to(source)
    return target if rel_root == Path() else target / rel_root


def _copy_seed_regular_file(src_file: Path, dest_file: Path) -> None:
    if src_file.is_symlink():
        raise MatrixEnvError("codex_plugin_source_symlink")
    try:
        mode = src_file.lstat().st_mode
    except OSError as exc:
        raise MatrixEnvError("codex_plugin_seed_failed") from exc
    if not stat.S_ISREG(mode):
        raise MatrixEnvError("codex_plugin_source_not_regular")
    try:
        shutil.copy2(src_file, dest_file, follow_symlinks=False)
        dest_file.chmod(OWNER_FILE_MODE)
    except OSError as exc:
        raise MatrixEnvError("codex_plugin_seed_failed") from exc


def _is_excluded_seed_name(name: str) -> bool:
    return name.lower() in _EXCLUDED_SEED_NAMES


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


def _require_owner_only_single_link_file(path: Path, *, reason: str) -> None:
    _require_owner_only_regular_file(path, reason=reason)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MatrixEnvError(reason) from exc
    if metadata.st_nlink != 1 or metadata.st_uid != os.getuid():
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


def _assert_parent_secrets_stripped(env: dict[str, str]) -> None:
    """Child env must never carry parent LIVE/SAXO/OPENAI/ANTHROPIC secrets."""
    blocked_prefixes = ("SAXO_", "OPENAI_", "ANTHROPIC_", "LIVE_")
    allowlisted = {
        "SAXO_MCP_ENVIRONMENT",
        "SAXO_MCP_ENABLE_LIVE_READS",
        "SAXO_MCP_ENABLE_LIVE_WRITES",
        "SAXO_MCP_SIM_CREDENTIAL_FILE",
        "SAXO_MCP_TOKEN_CACHE_PATH",
        "SAXO_MCP_SIM_REDIRECT_URI",
    }
    for key, value in env.items():
        if key in allowlisted or not value:
            continue
        if any(key == prefix or key.startswith(prefix) for prefix in blocked_prefixes):
            raise MatrixEnvError("parent_secret_inherited")


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
