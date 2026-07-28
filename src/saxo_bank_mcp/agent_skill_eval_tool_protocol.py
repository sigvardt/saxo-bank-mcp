from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS

CODEX_TOOL_ITEM_TYPES: Final = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "dynamic_tool_call",
        "app_tool_call",
    },
)
CODEX_COMMAND_ITEM_TYPES: Final = frozenset({"command_execution"})
CODEX_FILE_ITEM_TYPES: Final = frozenset({"file_change"})
CODEX_WEB_ITEM_TYPES: Final = frozenset({"web_search"})
CODEX_APP_ITEM_TYPES: Final = frozenset({"app_tool_call", "dynamic_tool_call"})
CLAUDE_COMMAND_TOOLS: Final = frozenset(
    {"bash", "shell", "computer", "edit", "write", "read", "notebookedit", "grep", "glob"},
)
CLAUDE_PROTOCOL_TOOLS: Final = frozenset({"structuredoutput"})
CODEX_HARNESS_PREFIX: Final = "mcp__saxo_bank_mcp__"
CLAUDE_HARNESS_PREFIX: Final = "mcp__plugin_saxo_bank_mcp_saxo_bank_mcp__"
CLAUDE_STDIO_HARNESS_PREFIX: Final = "mcp__saxo-bank-mcp__"
# Claude 2.x discovery helpers (not shell/files and not foreign MCP servers).
CLAUDE_DISCOVERY_TOOLS: Final = frozenset(
    {
        "toolsearch",
        "listmcpresourcestool",
        "listmcpresources",
        "readmcpresourcetool",
    },
)
# Known installed-plugin / stdio server ids for Saxo MCP (not foreign MCPs).
SAXO_MCP_SERVER_MARKERS: Final = frozenset(
    {
        "saxo-bank-mcp",
        "saxo_bank_mcp",
        "plugin_saxo_bank_mcp_saxo_bank_mcp",
        "plugin-saxo-bank-mcp-saxo-bank-mcp",
    },
)
# Saxo-server protocol wrappers that are not tradable logical tools.
SAXO_SERVER_PROTOCOL_TOOLS: Final = frozenset(
    {
        "",
        "list_resources",
        "list_tools",
        "list_prompts",
        "read_resource",
        "complete",
        "ping",
    },
)
type HarnessName = Literal["codex", "claude"]


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
    text: str = ""


class _ClaudeMessage(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    content: tuple[_ClaudeContent, ...] = ()
    role: str = ""


class _ClaudeEvent(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    type: str
    message: _ClaudeMessage | None = None
    result: str = ""


@dataclass(frozen=True, slots=True)
class ModelToolTrace:
    assistant_text: str
    invoked_logical_tools: tuple[str, ...]
    tool_event_count: int
    command_event_count: int
    file_event_count: int
    web_event_count: int
    app_event_count: int
    mcp_event_count: int
    saxo_event_count: int
    non_saxo_mcp_event_count: int
    parse_error: str = ""


def parse_codex_model_output(stream: str) -> ModelToolTrace:
    try:
        events = tuple(
            _CodexEvent.model_validate_json(line) for line in stream.splitlines() if line.strip()
        )
    except ValidationError:
        return _empty_trace(parse_error="malformed_output")
    seen_ids: set[str] = set()
    order: list[str] = []
    counts = _EventCounts()
    answer_parts: list[str] = []
    for event in events:
        item = event.item
        if item is None:
            continue
        if event.type == "item.completed" and item.type == "agent_message" and item.text:
            answer_parts.append(item.text)
        if item.type not in CODEX_TOOL_ITEM_TYPES:
            continue
        identity = item.id or f"{item.type}:{item.name}:{item.server}:{item.tool}:{item.command}"
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        counts.tool += 1
        _classify_codex_item(item, counts, order)
    return _trace_from_counts(counts, order, "\n".join(answer_parts))


def parse_claude_model_output(stream: str) -> ModelToolTrace:
    try:
        events = tuple(
            _ClaudeEvent.model_validate_json(line) for line in stream.splitlines() if line.strip()
        )
    except ValidationError:
        return _empty_trace(parse_error="malformed_output")
    seen_ids: set[str] = set()
    order: list[str] = []
    counts = _EventCounts()
    answer_parts: list[str] = []
    for event in events:
        if event.type == "result" and event.result:
            answer_parts.append(event.result)
        if event.message is None:
            continue
        for content in event.message.content:
            if content.type == "text" and content.text:
                answer_parts.append(content.text)
            if content.type != "tool_use":
                continue
            identity = content.id or content.name
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            name = content.name
            lowered = name.lower()
            if lowered in CLAUDE_PROTOCOL_TOOLS:
                continue
            counts.tool += 1
            _classify_claude_tool(name, counts, order)
    return _trace_from_counts(counts, order, "\n".join(answer_parts))


def normalize_logical_tool_name(raw: str, *, harness: HarnessName) -> str | None:
    """Map harness-qualified MCP names to logical Saxo IDs; reject unknown names."""
    del harness  # retained for call-site clarity across harnesses
    name = raw.strip()
    if not name:
        return None
    if name in ALL_LOGICAL_TOOL_IDS:
        return name
    stripped = _strip_known_mcp_prefix(name)
    if stripped is not None:
        return stripped if stripped in ALL_LOGICAL_TOOL_IDS else None
    if not name.startswith("mcp__") or "__" not in name:
        return None
    logical = name.rsplit("__", 1)[-1]
    if logical in ALL_LOGICAL_TOOL_IDS and "saxo" in name.lower():
        return logical
    return None


def _strip_known_mcp_prefix(name: str) -> str | None:
    for prefix in (
        CODEX_HARNESS_PREFIX,
        CLAUDE_HARNESS_PREFIX,
        CLAUDE_STDIO_HARNESS_PREFIX,
    ):
        if name.startswith(prefix):
            return name.removeprefix(prefix)
    return None


def logical_tools_from_grants(grants: tuple[str, ...]) -> tuple[str, ...]:
    """Strip harness MCP prefixes from resolved grant names; keep logical IDs."""
    result: list[str] = []
    seen: set[str] = set()
    for grant in grants:
        if grant.startswith(CLAUDE_HARNESS_PREFIX):
            logical = grant.removeprefix(CLAUDE_HARNESS_PREFIX)
        elif grant.startswith(CLAUDE_STDIO_HARNESS_PREFIX):
            logical = grant.removeprefix(CLAUDE_STDIO_HARNESS_PREFIX)
        elif grant.startswith(CODEX_HARNESS_PREFIX):
            logical = grant.removeprefix(CODEX_HARNESS_PREFIX)
        elif grant.startswith("mcp__") and "__" in grant:
            logical = grant.rsplit("__", 1)[-1]
        else:
            logical = grant
        if logical not in seen:
            seen.add(logical)
            result.append(logical)
    return tuple(result)


@dataclass(slots=True)
class _EventCounts:
    tool: int = 0
    command: int = 0
    file: int = 0
    web: int = 0
    app: int = 0
    mcp: int = 0
    saxo: int = 0
    non_saxo_mcp: int = 0
    parse_error: str = ""


def _classify_codex_item(item: _CodexItem, counts: _EventCounts, order: list[str]) -> None:
    if item.type in CODEX_COMMAND_ITEM_TYPES:
        counts.command += 1
    if item.type in CODEX_FILE_ITEM_TYPES:
        counts.file += 1
    if item.type in CODEX_WEB_ITEM_TYPES:
        counts.web += 1
    if item.type in CODEX_APP_ITEM_TYPES:
        counts.app += 1
    if item.type != "mcp_tool_call":
        return
    counts.mcp += 1
    classification = _codex_mcp_classification(item)
    if classification == "saxo":
        logical = _codex_mcp_logical(item)
        if logical is None:
            # Saxo server protocol wrapper/noise: not a foreign MCP.
            return
        counts.saxo += 1
        order.append(logical)
        return
    if classification == "saxo_protocol":
        return
    counts.non_saxo_mcp += 1
    counts.parse_error = counts.parse_error or "non_saxo_mcp_event"


def _codex_mcp_logical(item: _CodexItem) -> str | None:
    candidates = (item.tool, item.name)
    for candidate in candidates:
        if not candidate:
            continue
        logical = normalize_logical_tool_name(candidate, harness="codex")
        if logical is not None:
            return logical
        if candidate in ALL_LOGICAL_TOOL_IDS:
            return candidate
    if item.tool in ALL_LOGICAL_TOOL_IDS:
        return item.tool
    return None


def _is_saxo_mcp_server(server: str) -> bool:
    name = server.strip()
    if not name:
        return False
    if name in SAXO_MCP_SERVER_MARKERS:
        return True
    lowered = name.lower().replace("-", "_")
    return "saxo" in lowered and "mcp" in lowered


def _codex_mcp_classification(item: _CodexItem) -> str:
    """Return saxo | saxo_protocol | non_saxo for an MCP tool event."""
    logical = _codex_mcp_logical(item)
    if logical is not None:
        return "saxo"
    server = item.server.strip()
    tool = (item.tool or "").strip()
    name = (item.name or "").strip()
    if not server and not tool and not name:
        # Empty MCP wrapper rows are harness noise, not a foreign server.
        return "saxo_protocol"
    if _is_saxo_mcp_server(server) or _is_saxo_mcp_qualified_name(tool, name):
        # Saxo server wrapper/protocol noise is not a foreign MCP. Real tools still
        # grade via logical invocation + exact grants; unknown servers stay fail-closed.
        return "saxo_protocol"
    return "non_saxo"


def _is_saxo_mcp_qualified_name(*candidates: str) -> bool:
    """Return True when tool/name carries a known Saxo MCP prefix or markers."""
    for candidate in candidates:
        text = candidate.strip()
        if not text:
            continue
        lowered = text.lower()
        if any(
            lowered.startswith(prefix)
            for prefix in (
                CODEX_HARNESS_PREFIX,
                CLAUDE_HARNESS_PREFIX,
                CLAUDE_STDIO_HARNESS_PREFIX,
            )
        ):
            return True
        if "saxo" in lowered and "mcp" in lowered:
            return True
        bare = text.rsplit("__", 1)[-1].lower() if "__" in text else lowered
        if bare in SAXO_SERVER_PROTOCOL_TOOLS and "saxo" in lowered:
            return True
    return False


def _classify_claude_tool(name: str, counts: _EventCounts, order: list[str]) -> None:
    lowered = name.lower()
    if lowered in CLAUDE_COMMAND_TOOLS:
        counts.command += 1
        return
    if lowered in CLAUDE_DISCOVERY_TOOLS:
        # Discovery helpers are protocol noise, not foreign MCP/shell.
        return
    if name.startswith("mcp__") or lowered.startswith("mcp__"):
        counts.mcp += 1
        logical = normalize_logical_tool_name(name, harness="claude")
        if logical is None:
            counts.non_saxo_mcp += 1
            counts.parse_error = counts.parse_error or "non_saxo_mcp_event"
            return
        counts.saxo += 1
        order.append(logical)
        return
    # Unknown built-in or non-Saxo tool name is a forbidden non-MCP event.
    counts.app += 1
    counts.parse_error = counts.parse_error or "non_grant_tool_event"


def _trace_from_counts(
    counts: _EventCounts,
    order: list[str],
    assistant_text: str,
) -> ModelToolTrace:
    return ModelToolTrace(
        assistant_text=assistant_text,
        invoked_logical_tools=tuple(order),
        tool_event_count=counts.tool,
        command_event_count=counts.command,
        file_event_count=counts.file,
        web_event_count=counts.web,
        app_event_count=counts.app,
        mcp_event_count=counts.mcp,
        saxo_event_count=counts.saxo,
        non_saxo_mcp_event_count=counts.non_saxo_mcp,
        parse_error=counts.parse_error,
    )


def _empty_trace(*, parse_error: str) -> ModelToolTrace:
    return ModelToolTrace(
        assistant_text="",
        invoked_logical_tools=(),
        tool_event_count=0,
        command_event_count=0,
        file_event_count=0,
        web_event_count=0,
        app_event_count=0,
        mcp_event_count=0,
        saxo_event_count=0,
        non_saxo_mcp_event_count=0,
        parse_error=parse_error,
    )
