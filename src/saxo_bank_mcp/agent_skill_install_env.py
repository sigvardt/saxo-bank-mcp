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
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SYSTEMROOT",
    "ComSpec",
    "WINDIR",
)
_UV_KEYS: Final = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_NATIVE_TLS")
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


def build_isolated_env(
    *,
    home: Path,
    codex_home: Path,
    run_root: Path,
    probe_env: Path,
    auth_targets: dict[str, Path] | None = None,
) -> dict[str, str]:
    """Build an explicit allowlisted environment rooted under run_root."""
    tmp = run_root / "tmp"
    xdg_config = home / ".config"
    xdg_cache = home / ".cache"
    xdg_data = home / ".local" / "share"
    xdg_state = home / ".local" / "state"
    for path in (tmp, xdg_config, xdg_cache, xdg_data, xdg_state, probe_env):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    env: dict[str, str] = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "TMPDIR": str(tmp),
        "TEMP": str(tmp),
        "TMP": str(tmp),
        "UV_PROJECT_ENVIRONMENT": str(probe_env),
        "UV_NO_MODIFY_PATH": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }
    for key in (*_PATH_KEYS, *_RUNTIME_KEYS, *_UV_KEYS):
        value = os.environ.get(key)
        if value:
            env[key] = value
    if "PATH" not in env:
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    # Keep caller PATH order (tests prepend fakes). Append known binary dirs if missing.
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
            env[key] = str(target)
    return env


def parent_env_is_blocked(key: str) -> bool:
    return any(key == prefix or key.startswith(prefix) for prefix in _BLOCKED_PREFIXES)


def auth_env_keys() -> tuple[str, ...]:
    return _AUTH_FILE_ENV_KEYS
