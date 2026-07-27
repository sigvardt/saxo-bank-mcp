from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import CommandResult, run_command
from saxo_bank_mcp.agent_skill_install_models import StartupCheck, StartupEvidence
from saxo_bank_mcp.agent_skill_install_paths import PLUGIN_NAME, scrub_runtime_artifacts

JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def probe_root_stdio(
    name: str,
    root: Path,
    *,
    env: dict[str, str],
    probe_env: Path,
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
    print(json.dumps({{"tool_count": len(tools), "annotations_missing": missing}}))
anyio.run(main)
"""
    merged = dict(env)
    merged["UV_PROJECT_ENVIRONMENT"] = str(probe_env)
    merged["UV_NO_MODIFY_PATH"] = "1"
    result = run_command(
        name,
        ("uv", "run", "--project", str(root), "python", "-c", code),
        cwd=root,
        env=merged,
        timeout_seconds=300,
    )
    scrub_runtime_artifacts(root)
    return result


def startup_from_probes(
    source_probe: CommandResult,
    cache_probe: CommandResult,
    list_tools_probe: CommandResult,
) -> tuple[StartupEvidence, list[str], list[str]]:
    source_payload = _probe_payload(source_probe.stdout)
    cache_payload = _probe_payload(cache_probe.stdout)
    list_payload = _probe_payload(list_tools_probe.stdout)
    source_missing = _string_list(source_payload.get("annotations_missing"))
    cache_missing = _string_list(cache_payload.get("annotations_missing"))
    startup = StartupEvidence(
        source=StartupCheck(status="passed", tool_count=_tool_count(source_payload)),
        cache=StartupCheck(status="passed", tool_count=_tool_count(cache_payload)),
        list_tools=StartupCheck(status="passed", tool_count=_tool_count(list_payload)),
    )
    return startup, source_missing, cache_missing


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
    except ValidationError:
        return {}


def _tool_count(payload: dict[str, JsonValue]) -> int:
    value = payload.get("tool_count")
    return value if isinstance(value, int) else 0


def _string_list(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return []
        strings.append(item)
    return strings
