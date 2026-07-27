from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, TypeAdapter

from saxo_bank_mcp.agent_skill_eval_models import RouterDecision

CODEX_TOOL_ITEM_TYPES: Final = frozenset(
    {"command_execution", "file_change", "mcp_tool_call", "web_search", "dynamic_tool_call"},
)
CODEX_COMMAND_ITEM_TYPES: Final = frozenset({"command_execution", "file_change"})
CLAUDE_COMMAND_TOOLS: Final = frozenset(
    {"bash", "shell", "computer", "edit", "write", "read", "notebookedit"},
)
CLAUDE_PROTOCOL_TOOLS: Final = frozenset({"structuredoutput"})


class _CodexItem(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    id: str = ""
    type: str = ""
    text: str = ""
    name: str = ""
    server: str = ""
    tool: str = ""
    command: str = ""


class _CodexEvent(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    type: str
    item: _CodexItem | None = None


class _ClaudeContent(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    type: str
    id: str = ""
    name: str = ""


class _ClaudeMessage(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    content: tuple[_ClaudeContent, ...] = ()


class _ClaudeEvent(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    type: str
    message: _ClaudeMessage | None = None
    result: str = ""
    structured_output: RouterDecision | None = None


@dataclass(frozen=True, slots=True)
class RouterOutput:
    decision: RouterDecision
    tool_event_count: int
    command_event_count: int
    mcp_event_count: int
    saxo_event_count: int


def parse_codex_router_output(stream: str) -> RouterOutput:
    events = tuple(
        _CodexEvent.model_validate_json(line)
        for line in stream.splitlines()
        if line.strip()
    )
    tool_ids: set[str] = set()
    command_ids: set[str] = set()
    mcp_ids: set[str] = set()
    saxo_ids: set[str] = set()
    answer = ""
    for event in events:
        item = event.item
        if item is None:
            continue
        if event.type == "item.completed" and item.type == "agent_message":
            answer = item.text
        if item.type not in CODEX_TOOL_ITEM_TYPES:
            continue
        identity = item.id or f"{item.type}:{item.name}:{item.tool}:{item.command}"
        tool_ids.add(identity)
        if item.type in CODEX_COMMAND_ITEM_TYPES:
            command_ids.add(identity)
        if item.type == "mcp_tool_call":
            mcp_ids.add(identity)
        event_name = f"{item.name} {item.server} {item.tool} {item.command}".lower()
        if "saxo" in event_name:
            saxo_ids.add(identity)
    return RouterOutput(
        decision=RouterDecision.model_validate_json(answer),
        tool_event_count=len(tool_ids),
        command_event_count=len(command_ids),
        mcp_event_count=len(mcp_ids),
        saxo_event_count=len(saxo_ids),
    )


def parse_claude_router_output(stream: str) -> RouterOutput:
    events = tuple(
        _ClaudeEvent.model_validate_json(line)
        for line in stream.splitlines()
        if line.strip()
    )
    decision: RouterDecision | None = None
    for event in events:
        if event.structured_output is not None:
            decision = event.structured_output
        if event.type == "result" and decision is None and event.result:
            decision = RouterDecision.model_validate_json(event.result)
    if decision is None:
        decision = RouterDecision.model_validate_json("")
    tool_ids, command_ids, mcp_ids, saxo_ids = _claude_tool_events(events)
    return RouterOutput(
        decision=decision,
        tool_event_count=len(tool_ids),
        command_event_count=len(command_ids),
        mcp_event_count=len(mcp_ids),
        saxo_event_count=len(saxo_ids),
    )


def _claude_tool_events(
    events: tuple[_ClaudeEvent, ...],
) -> tuple[set[str], set[str], set[str], set[str]]:
    tool_ids: set[str] = set()
    command_ids: set[str] = set()
    mcp_ids: set[str] = set()
    saxo_ids: set[str] = set()
    for event in events:
        if event.message is None:
            continue
        for content in event.message.content:
            if content.type != "tool_use":
                continue
            identity = content.id or content.name
            name = content.name.lower()
            if name in CLAUDE_PROTOCOL_TOOLS:
                continue
            tool_ids.add(identity)
            if name in CLAUDE_COMMAND_TOOLS:
                command_ids.add(identity)
            if name.startswith("mcp__"):
                mcp_ids.add(identity)
            if "saxo" in name:
                saxo_ids.add(identity)
    return tool_ids, command_ids, mcp_ids, saxo_ids


def codex_router_command(
    prompt: str,
    schema_path: Path,
    workdir: Path,
    *,
    mcp_server_names: tuple[str, ...] = (),
) -> tuple[str, ...]:
    mcp_overrides = tuple(
        value
        for name in mcp_server_names
        for value in ("-c", f"mcp_servers.{name}.enabled=false")
    )
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
        "--disable",
        "remote_plugin",
        "-c",
        'web_search="disabled"',
        *mcp_overrides,
        "--color",
        "never",
        "-C",
        str(workdir),
        "--output-schema",
        str(schema_path),
        "--json",
        prompt,
    )


def configured_codex_mcp_server_names(config_path: Path) -> tuple[str, ...]:
    if not config_path.is_file():
        return ()
    document = TypeAdapter(dict[str, object]).validate_python(
        tomllib.loads(config_path.read_text(encoding="utf-8")),
    )
    raw_servers = document.get("mcp_servers")
    if raw_servers is None:
        return ()
    servers = TypeAdapter(dict[str, object]).validate_python(raw_servers)
    return tuple(sorted(servers))


def claude_router_command(
    prompt: str,
    schema_path: Path,
) -> tuple[str, ...]:
    return (
        "claude",
        "--safe-mode",
        "--no-session-persistence",
        "--no-chrome",
        "--disable-slash-commands",
        "--permission-mode",
        "plan",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        schema_path.read_text(encoding="utf-8"),
        "--print",
        prompt,
    )
