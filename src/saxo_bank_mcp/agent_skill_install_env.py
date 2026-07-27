from __future__ import annotations

import os
import shutil
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


class EnvironmentContainmentError(ValueError):
    def __init__(self, reason: str) -> None:  # noqa: D107
        super().__init__(reason)
        self.reason = reason


def build_isolated_env(  # noqa: C901
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
    for binary in ("uv", "codex", "claude", "git"):
        located = shutil.which(binary, path=env["PATH"]) or shutil.which(binary)
        if located:
            directory = str(Path(located).resolve().parent)
            if directory not in path_parts:
                path_parts.append(directory)
    env["PATH"] = os.pathsep.join(path_parts)
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
