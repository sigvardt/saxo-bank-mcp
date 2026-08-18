from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import CommandResult, run_command
from saxo_bank_mcp.agent_skill_install_models import (
    CommandReceipt,
    StartupCheck,
    StartupEvidence,
)
from saxo_bank_mcp.agent_skill_install_paths import PLUGIN_NAME, scrub_runtime_artifacts

JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


class ProbePayloadError(ValueError):
    """Fail-closed parse error for a startup probe payload."""

    def __init__(self, reason: str) -> None:  # noqa: D107
        super().__init__(reason)
        self.reason = reason


def probe_root_stdio(
    name: str,
    root: Path,
    *,
    env: dict[str, str],
    probe_env: Path,
    offline: bool = False,
) -> CommandResult:
    """Start MCP via each root's .mcp.json stdio contract and list tools."""
    probe_env.mkdir(parents=True, exist_ok=True)
    mcp_path = root / ".mcp.json"
    if not mcp_path.is_file():
        msg = f"missing_mcp_json:{root}"
        raise FileNotFoundError(msg)
    code = f"""
import anyio, json
from pathlib import Path
from fastmcp import Client

root = Path({str(root.resolve())!r})
payload = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
server = payload["mcpServers"][{PLUGIN_NAME!r}]
args = list(server.get("args", []))
fixed = []
skip = False
for index, arg in enumerate(args):
    if skip:
        skip = False
        continue
    if arg == "--project" and index + 1 < len(args):
        fixed.extend(["--project", str(root)])
        skip = True
        continue
    fixed.append(arg)
config = {{
    "mcpServers": {{
        {PLUGIN_NAME!r}: {{
            "command": server["command"],
            "args": fixed,
            "cwd": str(root),
        }}
    }}
}}
async def main() -> None:
    async with Client(config) as client:
        tools = await client.list_tools()
    missing = [tool.name for tool in tools if getattr(tool, "annotations", None) is None]
    print(json.dumps({{
        "tool_count": len(tools),
        "annotations_missing": missing,
        "tool_names": [tool.name for tool in tools],
    }}))
anyio.run(main)
"""
    merged = dict(env)
    merged["UV_PROJECT_ENVIRONMENT"] = str(probe_env)
    merged["UV_NO_MODIFY_PATH"] = "1"
    result = run_command(
        name,
        (
            "uv",
            "run",
            *(("--offline",) if offline else ()),
            "--project",
            str(root),
            "python",
            "-c",
            code,
        ),
        cwd=root,
        env=merged,
        timeout_seconds=300,
    )
    scrub_runtime_artifacts(root)
    return result


def reuse_probe(  # noqa: PLR0913
    started: dict[Path, CommandResult],
    name: str,
    root: Path,
    *,
    env: dict[str, str],
    probe_env: Path,
    probe: Callable[..., CommandResult] | None = None,
) -> CommandResult:
    """Start each distinct root once and reuse that result for duplicate claims.

    Independent proof is preserved: distinct roots are always started separately. A second
    claim about the same exact root (for example a list_tools claim about an already started
    cache) reuses the first successful result instead of starting the same root again.
    """
    key = root.resolve()
    existing = started.get(key)
    if existing is not None:
        return existing
    start = probe_root_stdio if probe is None else probe
    result = start(name, root, env=env, probe_env=probe_env)
    started[key] = result
    return result


def distinct_probe_receipts(*results: CommandResult) -> tuple[CommandReceipt, ...]:
    """Receipts for the given probes, keeping one receipt per actually started process."""
    ordered: list[CommandReceipt] = []
    for result in results:
        if any(result.receipt is seen for seen in ordered):
            continue
        ordered.append(result.receipt)
    return tuple(ordered)


def startup_from_probes(
    source_probe: CommandResult,
    cache_probe: CommandResult,
    list_tools_probe: CommandResult,
) -> StartupEvidence:
    """Parse source, cache, and independent list_tools probes into typed StartupEvidence.

    Fail-closed: missing/null/wrong-type/non-string annotations_missing raise ProbePayloadError.
    Nonempty annotations on any of the three probes also fail closed.
    """
    source = startup_check_from_payload(_probe_payload(source_probe.stdout))
    cache = startup_check_from_payload(_probe_payload(cache_probe.stdout))
    list_tools = startup_check_from_payload(_probe_payload(list_tools_probe.stdout))
    for label, check in (
        ("source", source),
        ("cache", cache),
        ("list_tools", list_tools),
    ):
        if check.annotations_missing:
            raise ProbePayloadError(f"{label}_annotations_missing")
    return StartupEvidence(source=source, cache=cache, list_tools=list_tools)


def startup_check_from_payload(payload: dict[str, JsonValue]) -> StartupCheck:
    """Build a StartupCheck from a probe JSON object. Does not soft-map missing to empty."""
    tool_count = _require_tool_count(payload)
    annotations_missing = _require_annotations_missing(payload)
    return StartupCheck(
        status="passed",
        tool_count=tool_count,
        annotations_missing=tuple(annotations_missing),
    )


def _probe_payload(raw: str) -> dict[str, JsonValue]:
    for raw_line in reversed(raw.splitlines()):
        stripped = raw_line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            return JSON_OBJECT_ADAPTER.validate_json(stripped)
        except ValidationError:
            continue
    try:
        return JSON_OBJECT_ADAPTER.validate_json(raw)
    except ValidationError as exc:
        raise ProbePayloadError("probe_payload_invalid") from exc


def _require_tool_count(payload: dict[str, JsonValue]) -> int:
    if "tool_count" not in payload:
        raise ProbePayloadError("tool_count_missing")
    value = payload["tool_count"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProbePayloadError("tool_count_invalid")
    return value


def _require_annotations_missing(payload: dict[str, JsonValue]) -> list[str]:
    if "annotations_missing" not in payload:
        raise ProbePayloadError("annotations_missing_missing")
    value = payload["annotations_missing"]
    if value is None:
        raise ProbePayloadError("annotations_missing_null")
    if not isinstance(value, list):
        raise ProbePayloadError("annotations_missing_not_list")
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ProbePayloadError("annotations_missing_non_string")
        strings.append(item)
    return strings
