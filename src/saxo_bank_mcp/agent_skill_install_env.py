from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Final

# Parent env keys allowed into the isolated install environment.
_PATH_KEYS: Final = ("PATH", "HOMEBREW_PREFIX", "HOMEBREW_CELLAR", "HOMEBREW_REPOSITORY")
_RUNTIME_KEYS: Final = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SYSTEMROOT",
    "ComSpec",
    "WINDIR",
)
# Write-bearing keys are never inherited from the caller; forced under run_root.
_WRITE_BEARING_KEYS: Final = (
    "TMPDIR",
    "TEMP",
    "TMP",
    "HOME",
    "CODEX_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PROJECT_ENVIRONMENT",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "CLAUDE_CONFIG_DIR",
    "SAXO_MCP_SIM_CREDENTIAL_FILE",
    "SAXO_MCP_LIVE_CREDENTIAL_FILE",
    "SAXO_MCP_TOKEN_CACHE_PATH",
)
_AUTH_FILE_ENV_KEYS: Final = (
    "SAXO_MCP_SIM_CREDENTIAL_FILE",
    "SAXO_MCP_LIVE_CREDENTIAL_FILE",
    "SAXO_MCP_TOKEN_CACHE_PATH",
)
_BLOCKED_PREFIXES: Final = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "CODEX_",
    "CLAUDE_",
    "ANTHROPIC_",
    "OPENAI_",
    "SAXO_",
    "AWS_",
    "GOOGLE_",
    "GH_",
    "GITHUB_",
)
# Task-created ephemeral trees only. Never includes clone, caches, or registration roots.
_DISPOSABLE_RUN_ROOT_RELATIVES: Final = (
    "marketplace-source",
    "probe-env",
    "verify-probe-env",
    "verify-home",
    "verify-codex-home",
    "tmp",
    "uv-cache",
    "uv-python",
)
_DISPOSABLE_HOME_RELATIVES: Final = (
    ".cache",
    ".config",
    ".local",
    "Library/Application Support/fastmcp",
    ".claude/backups",
)
_DISPOSABLE_CODEX_HOME_RELATIVES: Final = (
    ".tmp",
    "tmp",
)
VERIFY_HOME_NAME: Final = "verify-home"
VERIFY_CODEX_HOME_NAME: Final = "verify-codex-home"
VERIFY_PROBE_ENV_NAME: Final = "verify-probe-env"
# Verifier-only scratch: throwaway homes/probe plus shared run-root env dirs created by probes.
# Never includes run_root/home/** or run_root/codex-home/**.
_VERIFY_SCRATCH_RUN_ROOT_RELATIVES: Final = (
    VERIFY_HOME_NAME,
    VERIFY_CODEX_HOME_NAME,
    VERIFY_PROBE_ENV_NAME,
    "tmp",
    "uv-cache",
    "uv-python",
)


class EnvironmentContainmentError(ValueError):
    def __init__(self, reason: str) -> None:  # noqa: D107
        super().__init__(reason)
        self.reason = reason


class DisposableCleanupError(ValueError):
    """Fail-closed residue after removing enumerated disposable paths."""

    def __init__(self, residual_paths: Sequence[str]) -> None:  # noqa: D107
        super().__init__("disposable_cleanup_residue")
        self.reason = "disposable_cleanup_residue"
        self.residual_paths = tuple(residual_paths)


def build_isolated_env(  # noqa: C901, PLR0912
    *,
    home: Path,
    codex_home: Path,
    run_root: Path,
    probe_env: Path,
    auth_targets: dict[str, Path] | None = None,
) -> dict[str, str]:
    """Build an explicit allowlisted environment rooted under run_root."""
    root = run_root.resolve()
    home_r = home.resolve()
    codex_r = codex_home.resolve()
    probe_r = probe_env.resolve()
    for path, label in (
        (home_r, "HOME"),
        (codex_r, "CODEX_HOME"),
        (probe_r, "UV_PROJECT_ENVIRONMENT"),
    ):
        if not path.is_relative_to(root):
            raise EnvironmentContainmentError(f"path_outside_run_root:{label}")

    tmp = root / "tmp"
    uv_cache = root / "uv-cache"
    uv_python = root / "uv-python"
    xdg_config = home_r / ".config"
    xdg_cache = home_r / ".cache"
    xdg_data = home_r / ".local" / "share"
    xdg_state = home_r / ".local" / "state"
    claude_config = home_r / ".claude"
    for path in (
        tmp,
        uv_cache,
        uv_python,
        xdg_config,
        xdg_cache,
        xdg_data,
        xdg_state,
        claude_config,
        probe_r,
    ):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)

    env: dict[str, str] = {
        "HOME": str(home_r),
        "CODEX_HOME": str(codex_r),
        "CLAUDE_CONFIG_DIR": str(claude_config),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "TMPDIR": str(tmp),
        "TEMP": str(tmp),
        "TMP": str(tmp),
        "UV_CACHE_DIR": str(uv_cache),
        "UV_PYTHON_INSTALL_DIR": str(uv_python),
        "UV_PROJECT_ENVIRONMENT": str(probe_r),
        "UV_NO_MODIFY_PATH": "1",
        "UV_NATIVE_TLS": os.environ.get("UV_NATIVE_TLS", "1"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }
    for key in (*_PATH_KEYS, *_RUNTIME_KEYS):
        value = os.environ.get(key)
        if value:
            env[key] = value
    if "PATH" not in env:
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    path_parts = env["PATH"].split(os.pathsep)
    # node is required for Claude Code / some Codex Node shebang wrappers when
    # the CLI is launched by absolute path and the shebang is `#!/usr/bin/env node`.
    # Keep which() parent dirs (symlink locations), not realpath package bins:
    # Claude Code's brew entry is a symlink to claude.exe and must stay the launcher.
    for binary in ("uv", "codex", "claude", "git", "node"):
        located = shutil.which(binary, path=env["PATH"]) or shutil.which(binary)
        if located:
            located_path = Path(located).expanduser()
            directory = str(
                located_path.parent
                if located_path.is_absolute()
                else located_path.absolute().parent
            )
            if directory not in path_parts:
                path_parts.append(directory)
    env["PATH"] = os.pathsep.join(path_parts)
    # Pin absolute CLI paths under a non-SAXO_ prefix so model children keep a
    # stable argv0 without tripping parent-secret strip (SAXO_* is blocked).
    # Do not follow symlinks to realpath (Claude Code brew wrapper).
    for binary in ("uv", "codex", "claude"):
        located = shutil.which(binary, path=env["PATH"]) or shutil.which(binary)
        if not located:
            continue
        located_path = Path(located).expanduser()
        absolute = located_path if located_path.is_absolute() else located_path.absolute()
        try:
            if absolute.is_file() and os.access(absolute, os.X_OK):
                env[f"EVAL_CLI_{binary.upper()}_BIN"] = str(absolute)
        except OSError:
            continue
    if auth_targets:
        for key, target in auth_targets.items():
            resolved = target.resolve()
            if not resolved.is_relative_to(root):
                raise EnvironmentContainmentError(f"auth_path_outside_run_root:{key}")
            env[key] = str(resolved)
    _assert_write_bearing_contained(env, root)
    return env


def parent_env_is_blocked(key: str) -> bool:
    return any(key == prefix or key.startswith(prefix) for prefix in _BLOCKED_PREFIXES)


def auth_env_keys() -> tuple[str, ...]:
    return _AUTH_FILE_ENV_KEYS


def write_bearing_env_keys() -> tuple[str, ...]:
    return _WRITE_BEARING_KEYS


def verify_throwaway_roots(run_root: Path) -> tuple[Path, Path, Path]:
    """Owner-only throwaway HOME / CODEX_HOME / probe env under run_root for verify probes."""
    root = run_root.resolve()
    return (
        root / VERIFY_HOME_NAME,
        root / VERIFY_CODEX_HOME_NAME,
        root / VERIFY_PROBE_ENV_NAME,
    )


def enumerated_disposable_paths(run_root: Path) -> tuple[Path, ...]:
    """Exact task-created ephemeral paths eligible for pre-privacy cleanup."""
    root = run_root.resolve()
    home = root / "home"
    codex_home = root / "codex-home"
    paths = [root / relative for relative in _DISPOSABLE_RUN_ROOT_RELATIVES]
    paths.extend(home / relative for relative in _DISPOSABLE_HOME_RELATIVES)
    paths.extend(codex_home / relative for relative in _DISPOSABLE_CODEX_HOME_RELATIVES)
    return tuple(paths)


def cleanup_disposable_isolated_state(
    run_root: Path,
    *,
    extra_paths: Sequence[Path] = (),
) -> list[str]:
    """Remove producer pre-privacy disposable paths.

    Call only after process cleanup for commands that may hold open files under these trees.
    Does not touch source-clone, installed caches, registration files, or the six ledger roots
    themselves (only listed subtrees under home/codex-home).
    """
    targets = list(enumerated_disposable_paths(run_root))
    targets.extend(path.resolve() for path in extra_paths)
    return _cleanup_path_list(targets)


def enumerated_verify_scratch_paths(run_root: Path) -> tuple[Path, ...]:
    """Verifier-created scratch only. Never includes retained home/ or codex-home/."""
    root = run_root.resolve()
    return tuple(root / relative for relative in _VERIFY_SCRATCH_RUN_ROOT_RELATIVES)


def cleanup_verify_scratch_state(run_root: Path) -> list[str]:
    """Remove verifier scratch trees only. Return residual absolute paths (empty = clean).

    Safe after privacy production: must not delete or mutate run_root/home/** or
    run_root/codex-home/** (retained fixture state for privacy re-scan).
    """
    return _cleanup_path_list(list(enumerated_verify_scratch_paths(run_root)))


def require_disposable_cleanup(
    run_root: Path,
    *,
    extra_paths: Sequence[Path] = (),
) -> list[str]:
    """Remove disposable paths and raise DisposableCleanupError if residue remains."""
    residual = cleanup_disposable_isolated_state(run_root, extra_paths=extra_paths)
    if residual:
        raise DisposableCleanupError(residual)
    return residual


def require_verify_scratch_cleanup(run_root: Path) -> list[str]:
    """Remove verifier scratch and raise DisposableCleanupError if residue remains."""
    residual = cleanup_verify_scratch_state(run_root)
    if residual:
        raise DisposableCleanupError(residual)
    return residual


def _cleanup_path_list(targets: Sequence[Path]) -> list[str]:
    # Deepest paths first so nested removals do not race parents.
    ordered = sorted(set(targets), key=lambda item: len(str(item)), reverse=True)
    residual: list[str] = []
    for path in ordered:
        if not os.path.lexists(path):
            continue
        try:
            _remove_path_tree(path)
        except OSError:
            residual.append(str(path.resolve() if path.exists() else path))
            continue
        if os.path.lexists(path):
            residual.append(str(path.resolve() if path.exists() else path))
    return sorted(set(residual))


def _remove_path_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    # Special node (fifo/socket/device): unlink without following.
    path.unlink()


def _assert_write_bearing_contained(env: dict[str, str], run_root: Path) -> None:
    for key in _WRITE_BEARING_KEYS:
        value = env.get(key)
        if not value:
            continue
        path = Path(value).expanduser()
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise EnvironmentContainmentError(f"path_unresolvable:{key}") from exc
        if not resolved.is_relative_to(run_root):
            raise EnvironmentContainmentError(f"path_outside_run_root:{key}")
