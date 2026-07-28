from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS

EVAL_TOOL_FILTER_FLAG: Final = "SAXO_MCP_EVAL_TOOL_FILTER"
EVAL_ALLOWED_TOOLS_ENV: Final = "SAXO_MCP_EVAL_ALLOWED_TOOLS"
FILTER_FLAG_VALUE: Final = "1"
_WILDCARD_PATTERN: Final = re.compile(r"[*?]")
_QUALIFIED_PATTERN: Final = re.compile(r"(?:__|/|:|\s)")
_LOGICAL_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class EvalToolFilterError(ValueError):
    reason: str

    def __str__(self) -> str:  # noqa: D105
        return self.reason


def resolve_eval_tool_filter(env: Mapping[str, str]) -> frozenset[str] | None:
    """Return allowed logical tool IDs when the SIM-only eval filter is active.

    Returns None when the filter flag is absent (production: all tools).
    Raises EvalToolFilterError for any invalid configuration before server start.
    """
    flag = env.get(EVAL_TOOL_FILTER_FLAG, "")
    raw_tools = env.get(EVAL_ALLOWED_TOOLS_ENV, "")
    if flag == "":
        if raw_tools.strip():
            raise EvalToolFilterError("eval_tool_filter_flag_required")
        return None
    if flag != FILTER_FLAG_VALUE:
        raise EvalToolFilterError("eval_tool_filter_flag_invalid")
    _require_sim_only_env(env)
    return frozenset(_parse_allowed_tools(raw_tools))


def derive_eval_tool_filter_env(
    base_env: Mapping[str, str],
    logical_tools: Iterable[str],
) -> dict[str, str]:
    """Fresh child env with locked SIM-only eval filter for the granted logical tools."""
    child = dict(base_env)
    tools = tuple(logical_tools)
    if not tools:
        child.pop(EVAL_TOOL_FILTER_FLAG, None)
        child.pop(EVAL_ALLOWED_TOOLS_ENV, None)
        return child
    # Validate before the MCP child starts; duplicates / unknowns fail closed here.
    validated = _parse_allowed_tools(",".join(tools))
    child[EVAL_TOOL_FILTER_FLAG] = FILTER_FLAG_VALUE
    child[EVAL_ALLOWED_TOOLS_ENV] = ",".join(validated)
    # Re-check SIM lock on the child env the process will inherit.
    _require_sim_only_env(child)
    return child


def _require_sim_only_env(env: Mapping[str, str]) -> None:
    if env.get("SAXO_MCP_ENVIRONMENT") != "SIM":
        raise EvalToolFilterError("eval_tool_filter_requires_sim")
    if env.get("SAXO_MCP_ENABLE_LIVE_READS", "0") not in {"0", ""}:
        raise EvalToolFilterError("eval_tool_filter_requires_live_reads_off")
    if env.get("SAXO_MCP_ENABLE_LIVE_WRITES", "") != "":
        raise EvalToolFilterError("eval_tool_filter_requires_live_writes_empty")


def _parse_allowed_tools(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        raise EvalToolFilterError("eval_tool_filter_empty")
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise EvalToolFilterError("eval_tool_filter_empty_entry")
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        _reject_invalid_name(part)
        if part in seen:
            raise EvalToolFilterError("eval_tool_filter_duplicate")
        if part not in ALL_LOGICAL_TOOL_IDS:
            raise EvalToolFilterError("eval_tool_filter_unknown_tool")
        seen.add(part)
        ordered.append(part)
    return tuple(ordered)


def _reject_invalid_name(name: str) -> None:
    if _WILDCARD_PATTERN.search(name):
        raise EvalToolFilterError("eval_tool_filter_wildcard")
    if _QUALIFIED_PATTERN.search(name):
        raise EvalToolFilterError("eval_tool_filter_qualified_name")
    if not _LOGICAL_ID_PATTERN.fullmatch(name):
        raise EvalToolFilterError("eval_tool_filter_invalid_name")
