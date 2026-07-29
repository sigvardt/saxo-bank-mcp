from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from saxo_bank_mcp.agent_skill_eval_models import Harness
from saxo_bank_mcp.agent_skill_router_eval_protocol import configured_codex_mcp_server_names
from saxo_bank_mcp.server_eval_tool_filter import derive_eval_tool_filter_env
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS

# Codex MCP server names that must remain enabled for the installed Saxo plugin.
SAXO_CODEX_MCP_SERVER_NAMES: Final = frozenset(
    {
        "saxo-bank-mcp",
        "saxo_bank_mcp",
        "plugin_saxo_bank_mcp_saxo_bank_mcp",
    },
)
CLAUDE_STDIO_SERVER_NAME: Final = "saxo-bank-mcp"
# Claude 2.1.x: `--tools ""` disables built-ins but also leaves MCP tools unusable
# (init tools=[], server pending). Exhaustive disallowedTools is the working containment.
CLAUDE_DISALLOWED_BUILTINS: Final = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TaskStop",
    "WebFetch",
    "WebSearch",
    "Agent",
    "Computer",
    "CronCreate",
    "CronDelete",
    "CronList",
    "DesignSync",
    "EnterWorktree",
    "ExitWorktree",
    "Monitor",
    "PushNotification",
    "RemoteTrigger",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "TodoWrite",
    "TodoRead",
    "Workflow",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
)
_SIM_ENV_KEYS: Final = (
    "SAXO_MCP_ENVIRONMENT",
    "SAXO_MCP_ENABLE_LIVE_READS",
    "SAXO_MCP_ENABLE_LIVE_WRITES",
    "SAXO_MCP_SIM_CREDENTIAL_FILE",
    "SAXO_MCP_SIM_REDIRECT_URI",
    "SAXO_MCP_TOKEN_CACHE_PATH",
    "SAXO_MCP_SIM_AUTH_URL",
    "SAXO_MCP_SIM_TOKEN_URL",
    # Case-scoped safety allowlists (account always when bound; instrument for lifecycle).
    "SAXO_MCP_ACCOUNT_ALLOWLIST",
    "SAXO_MCP_INSTRUMENT_ALLOWLIST",
    "PATH",
    "HOME",
    "UV_CACHE_DIR",
    "UV_PROJECT_ENVIRONMENT",
)


def non_router_model_command(  # noqa: PLR0913
    *,
    harness: Harness,
    prompt: str,
    resolved_grants: tuple[str, ...],
    plugin_root: Path,
    codex_home: Path | None,
    claude_mcp_config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    path_env = env or {}
    match harness:
        case "codex":
            return codex_non_router_command(
                prompt,
                plugin_root=plugin_root,
                codex_home=codex_home,
                env=path_env,
            )
        case "claude":
            if claude_mcp_config_path is None:
                msg = "claude_mcp_config_path_required"
                raise ValueError(msg)
            return claude_non_router_command(
                prompt,
                mcp_config_path=claude_mcp_config_path,
                resolved_grants=resolved_grants,
                env=path_env,
            )


def codex_non_router_command(
    prompt: str,
    *,
    plugin_root: Path,
    codex_home: Path | None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Ephemeral read-only Codex: shell/apps/web/hooks/multi-agent/goals off; Saxo MCP kept."""
    path_env = enrich_eval_cli_env(env or {})
    mcp_overrides = _codex_unrelated_mcp_overrides(codex_home)
    codex_bin = resolve_cli_executable("codex", path_env)
    return (
        codex_bin,
        "exec",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--disable",
        "shell_tool",
        "--disable",
        "apps",
        "--disable",
        "hooks",
        "--disable",
        "multi_agent",
        "--disable",
        "goals",
        "-c",
        'web_search="disabled"',
        "-c",
        'approval_policy="never"',
        *mcp_overrides,
        "--color",
        "never",
        "-C",
        str(plugin_root),
        "--json",
        prompt,
    )


def claude_non_router_command(
    prompt: str,
    *,
    mcp_config_path: Path,
    resolved_grants: tuple[str, ...],
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Claude 2.x SIM non-router: built-ins denied, strict mcp-config, exact allowedTools.

    Claude 2.1.220 help documents ``--tools ""`` as disabling the built-in set. In practice
    that form also leaves MCP tools unusable (empty init tools, server stuck pending).
    Containment therefore uses exhaustive ``--disallowedTools`` so Bash/Read/Edit and other
    side channels are unavailable while exact ``--allowedTools`` MCP grants stay callable.
    """
    allowed = ",".join(resolved_grants)
    disallowed = ",".join(CLAUDE_DISALLOWED_BUILTINS)
    path_env = enrich_eval_cli_env(env or {})
    claude_bin = resolve_cli_executable("claude", path_env)
    return (
        claude_bin,
        "--no-session-persistence",
        "--no-chrome",
        "--disable-slash-commands",
        "--permission-mode",
        "bypassPermissions",
        "--disallowedTools",
        disallowed,
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools",
        allowed,
        "--output-format",
        "stream-json",
        "--verbose",
        "--print",
        prompt,
    )


def write_claude_sim_mcp_config(
    *,
    plugin_root: Path,
    dest: Path,
    env: Mapping[str, str],
) -> Path:
    """Write owner-only Claude mcp-config for the installed Saxo stdio server."""
    plugin = plugin_root.resolve()
    path_env = enrich_eval_cli_env(env)
    server_env = {
        key: path_env[key] for key in _SIM_ENV_KEYS if key in path_env and path_env[key] != ""
    }
    # Preserve explicit empty LIVE writes disablement.
    if "SAXO_MCP_ENABLE_LIVE_WRITES" in path_env:
        server_env["SAXO_MCP_ENABLE_LIVE_WRITES"] = path_env["SAXO_MCP_ENABLE_LIVE_WRITES"]
    # MCP child must find node/uv on PATH for shebang and project runners.
    server_env["PATH"] = path_env["PATH"]
    uv_bin = resolve_cli_executable("uv", path_env)
    payload = {
        "mcpServers": {
            CLAUDE_STDIO_SERVER_NAME: {
                "command": uv_bin,
                "args": [
                    "run",
                    "--project",
                    str(plugin),
                    "saxo-bank-mcp",
                    "--transport",
                    "stdio",
                ],
                "cwd": str(plugin),
                "env": server_env,
            }
        }
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    dest.chmod(0o600)
    return dest


def resolve_cli_executable(name: str, env: Mapping[str, str]) -> str:
    """Resolve a CLI binary to an absolute path when possible (deterministic Popen).

    Search order: EVAL_CLI_<NAME>_BIN override, env PATH, process PATH.
    Keeps the which()/override path absolute without following symlinks: Homebrew
    Claude Code is ``/opt/homebrew/bin/claude`` -> ``.../claude.exe``, and launching
    via the realpath breaks MCP + structured plan mode while bare/symlink still works.
    Falls back to the bare name only when nothing executable is found.
    """
    override_key = f"EVAL_CLI_{name.upper().replace('-', '_')}_BIN"
    candidates: list[str] = []
    override = env.get(override_key) or os.environ.get(override_key)
    if override:
        candidates.append(override)
    env_path = env.get("PATH") or ""
    if env_path:
        found = shutil.which(name, path=env_path)
        if found:
            candidates.append(found)
    host_found = shutil.which(name)
    if host_found:
        candidates.append(host_found)
    for candidate in candidates:
        absolute = _absolute_executable(candidate)
        if absolute is not None:
            return absolute
    return name


def path_with_cli_dirs(path_value: str, *binaries: str) -> str:
    """Append parent dirs of resolvable CLIs (and node) onto PATH without reordering."""
    parts = [part for part in path_value.split(os.pathsep) if part]
    extras: list[str] = []
    seen = set(parts)
    for binary in (*binaries, "node", "uv", "git"):
        located = shutil.which(binary, path=path_value) or shutil.which(binary)
        if not located:
            continue
        # Parent of the which path (symlink location). Do not realpath: callers must
        # keep brew wrapper dirs on PATH ahead of package-internal claude.exe dirs.
        directory = str(_absolute_path(Path(located)).parent)
        if directory in seen:
            continue
        seen.add(directory)
        extras.append(directory)
    if not extras:
        return path_value
    return os.pathsep.join([*parts, *extras])


def enrich_eval_cli_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy env with PATH/CLI overrides hardened for model and MCP child processes."""
    out = dict(env)
    path = out.get("PATH") or os.environ.get("PATH") or "/usr/bin:/bin"
    path = path_with_cli_dirs(path, "uv", "codex", "claude", "git", "node")
    out["PATH"] = path
    for name in ("uv", "codex", "claude"):
        key = f"EVAL_CLI_{name.upper()}_BIN"
        # Prefer live which() path over a stored pin so brew symlink launchers win
        # over package-internal realpaths (claude.exe) left from older candidates.
        via_path = shutil.which(name, path=out["PATH"]) or shutil.which(name)
        if via_path:
            absolute = _absolute_executable(via_path)
            if absolute is not None:
                out[key] = absolute
                continue
        existing = out.get(key)
        if existing:
            absolute = _absolute_executable(existing)
            if absolute is not None:
                out[key] = absolute
                continue
            out.pop(key, None)
        resolved = resolve_cli_executable(name, out)
        if resolved != name:
            out[key] = resolved
    return out


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else expanded.absolute()


def _absolute_executable(candidate: str) -> str | None:
    """Return an absolute executable path without following symlinks to realpath."""
    try:
        path = _absolute_path(Path(candidate))
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        # is_file()/access follow the symlink target; keep the public path string.
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    except OSError:
        return None
    return None


def child_env_for_case(
    base_env: dict[str, str],
    logical_grants: Iterable[str],
) -> dict[str, str]:
    """Fresh child env with SIM-only server-side tool filter for the case grants."""
    tools = tuple(tool for tool in logical_grants if tool in ALL_LOGICAL_TOOL_IDS)
    return derive_eval_tool_filter_env(base_env, tools)


def _codex_unrelated_mcp_overrides(codex_home: Path | None) -> tuple[str, ...]:
    if codex_home is None:
        return ()
    config_path = codex_home / "config.toml"
    names = configured_codex_mcp_server_names(config_path)
    overrides: list[str] = []
    for name in names:
        if _is_saxo_mcp_server(name):
            continue
        overrides.extend(("-c", f"mcp_servers.{name}.enabled=false"))
    return tuple(overrides)


def _is_saxo_mcp_server(name: str) -> bool:
    if name in SAXO_CODEX_MCP_SERVER_NAMES:
        return True
    lowered = name.lower().replace("-", "_")
    return "saxo" in lowered and "mcp" in lowered
