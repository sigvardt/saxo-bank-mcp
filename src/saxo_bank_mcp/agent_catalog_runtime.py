from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from mcp.types import Tool as McpTool
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.tool_metadata import tool_metadata

STANDARD_ANNOTATIONS: Final = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)
JSON_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class RuntimeTool:
    name: str
    description: str
    input_schema: JsonValue
    annotations: JsonValue
    metadata: JsonValue


@dataclass(frozen=True, slots=True)
class CatalogIssue:
    code: str
    target: str


@dataclass(frozen=True, slots=True)
class CatalogValidationError(Exception):
    issues: tuple[CatalogIssue, ...]

    def message(self) -> str:
        return "\n".join(f"{issue.code}: {issue.target}" for issue in self.issues)


def runtime_tools() -> tuple[RuntimeTool, ...]:
    return anyio.run(_runtime_tools)


def load_json_file(path: Path) -> JsonValue:
    return JSON_ADAPTER.validate_python(json.loads(path.read_text(encoding="utf-8")))


def source_hash(root: Path, paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update((root / path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


async def _runtime_tools() -> tuple[RuntimeTool, ...]:
    metadata = tool_metadata()
    async with Client(mcp) as client:
        tools = tuple(await client.list_tools())
    return tuple(
        _runtime_tool(tool, metadata)
        for tool in sorted(tools, key=lambda item: item.name)
    )


def _runtime_tool(tool: McpTool, metadata: dict[str, dict[str, JsonValue]]) -> RuntimeTool:
    annotations = (
        {}
        if tool.annotations is None
        else tool.annotations.model_dump(exclude_none=False)
    )
    return RuntimeTool(
        name=tool.name,
        description=tool.description or "",
        input_schema=JSON_ADAPTER.validate_python(tool.inputSchema),
        annotations=JSON_ADAPTER.validate_python(annotations),
        metadata=JSON_ADAPTER.validate_python(metadata.get(tool.name, {})),
    )
