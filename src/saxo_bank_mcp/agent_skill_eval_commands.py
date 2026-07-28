from __future__ import annotations

from collections.abc import Iterable
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


def non_router_model_command(
    *,
    harness: Harness,
    prompt: str,
    resolved_grants: tuple[str, ...],
    plugin_root: Path,
    codex_home: Path | None,
) -> tuple[str, ...]:
    match harness:
        case "codex":
            return codex_non_router_command(
                prompt,
                plugin_root=plugin_root,
                codex_home=codex_home,
            )
        case "claude":
            return claude_non_router_command(
                prompt,
                plugin_root=plugin_root,
                resolved_grants=resolved_grants,
            )


def codex_non_router_command(
    prompt: str,
    *,
    plugin_root: Path,
    codex_home: Path | None,
) -> tuple[str, ...]:
    """Ephemeral read-only Codex: shell/apps/web/hooks/multi-agent/goals off; Saxo MCP kept."""
    mcp_overrides = _codex_unrelated_mcp_overrides(codex_home)
    return (
        "codex",
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
    plugin_root: Path,
    resolved_grants: tuple[str, ...],
) -> tuple[str, ...]:
    """Claude SIM non-router: exact allowedTools only, no builtins, autonomous granted tools."""
    allowed = ",".join(resolved_grants)
    return (
        "claude",
        "--plugin-dir",
        str(plugin_root),
        "--no-session-persistence",
        "--no-chrome",
        "--disable-slash-commands",
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        "",
        "--allowedTools",
        allowed,
        "--output-format",
        "stream-json",
        "--verbose",
        "--print",
        prompt,
    )


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
