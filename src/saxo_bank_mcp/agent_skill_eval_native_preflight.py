from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError, run_command
from saxo_bank_mcp.agent_skill_eval_commands import resolve_cli_executable
from saxo_bank_mcp.agent_skill_install_paths import MARKETPLACE_NAME, PLUGIN_NAME
from saxo_bank_mcp.agent_skill_install_probe import probe_root_stdio
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS

_JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


class _CodexPlugin(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    name: str
    version: str
    marketplace_name: str = Field(alias="marketplaceName")
    plugin_id: str = Field(alias="pluginId")
    enabled: bool
    installed: bool


class _CodexPluginList(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    installed: tuple[_CodexPlugin, ...]


@dataclass(frozen=True, slots=True)
class CodexNativePreflightReceipt:
    plugin_enabled: bool
    mcp_started: bool
    visible_logical_tools: tuple[str, ...]
    plugin_list_exit_code: int
    plugin_list_stdout_schema_sha256: str


@dataclass(frozen=True, slots=True)
class CodexNativePreflightError(ValueError):
    reason: str
    mcp_started: bool | None = False
    plugin_list_exit_code: int | None = None
    plugin_list_stdout_schema_sha256: str | None = None

    def __str__(self) -> str:  # noqa: D105
        return self.reason


@dataclass(frozen=True, slots=True)
class _ExpectedPluginIdentity:
    name: str
    version: str
    marketplace_name: str
    plugin_id: str


def preflight_codex_native_case(
    *,
    codex_home: Path,
    plugin_root: Path,
    logical_grants: tuple[str, ...],
    env: dict[str, str],
    probe_env: Path,
) -> CodexNativePreflightReceipt:
    """Prove native plugin registration and exact SIM-filtered tool visibility.

    This boundary performs only local CLI inspection and an MCP ``list_tools`` call. It
    never invokes a model or a Saxo logical tool.
    """
    identity = _require_exact_disposable_binding(
        codex_home=codex_home,
        plugin_root=plugin_root,
        env=env,
    )
    expected = _validated_grants(logical_grants)
    plugin_list = _require_enabled_plugin(
        plugin_root=plugin_root,
        expected=identity,
        env=env,
    )
    probe_env_vars = dict(env)
    probe_env_vars["UV_OFFLINE"] = "1"
    try:
        result = probe_root_stdio(
            "codex_native_case_list_tools",
            plugin_root,
            env=probe_env_vars,
            probe_env=probe_env,
            offline=True,
        )
    except (CommandFailureError, OSError, ValueError) as exc:
        raise CodexNativePreflightError(
            "codex_native_mcp_start_failed",
            mcp_started=None,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
        ) from exc
    try:
        payload = _last_json_object(result.stdout)
        visible = _strict_tool_names(payload)
        annotations = payload.get("annotations_missing")
        tool_count = payload.get("tool_count")
    except (ValidationError, ValueError) as exc:
        raise CodexNativePreflightError(
            "codex_native_tool_visibility_invalid",
            mcp_started=True,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
        ) from exc
    if annotations != [] or tool_count != len(visible):
        raise CodexNativePreflightError(
            "codex_native_tool_visibility_invalid",
            mcp_started=True,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
        )
    if frozenset(visible) != frozenset(expected) or len(visible) != len(expected):
        raise CodexNativePreflightError(
            "codex_native_tool_visibility_mismatch",
            mcp_started=True,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
        )
    return CodexNativePreflightReceipt(
        plugin_enabled=True,
        mcp_started=True,
        visible_logical_tools=tuple(sorted(visible)),
        plugin_list_exit_code=plugin_list.exit_code,
        plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
    )


def _require_exact_disposable_binding(
    *,
    codex_home: Path,
    plugin_root: Path,
    env: dict[str, str],
) -> _ExpectedPluginIdentity:
    try:
        home = codex_home.resolve(strict=True)
        configured = Path(env.get("CODEX_HOME", "")).resolve(strict=True)
        plugin = plugin_root.resolve(strict=True)
    except OSError as exc:
        raise CodexNativePreflightError("codex_native_disposable_binding_invalid") from exc
    if configured != home or not plugin.is_relative_to(home):
        raise CodexNativePreflightError("codex_native_disposable_binding_invalid")
    relative = plugin.relative_to(home)
    parts = relative.parts
    if (
        len(parts) != 5  # noqa: PLR2004 - exact Codex cache layout contract
        or parts[:2] != ("plugins", "cache")
        or parts[2] != MARKETPLACE_NAME
        or parts[3] != PLUGIN_NAME
        or not parts[4]
    ):
        raise CodexNativePreflightError("codex_native_disposable_binding_invalid")
    return _ExpectedPluginIdentity(
        name=PLUGIN_NAME,
        version=parts[4],
        marketplace_name=MARKETPLACE_NAME,
        plugin_id=f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
    )


def _validated_grants(logical_grants: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not logical_grants
        or len(logical_grants) != len(frozenset(logical_grants))
        or any(tool not in ALL_LOGICAL_TOOL_IDS for tool in logical_grants)
    ):
        raise CodexNativePreflightError("codex_native_tool_grants_invalid")
    return logical_grants


@dataclass(frozen=True, slots=True)
class _PluginListEvidence:
    exit_code: int
    stdout_schema_sha256: str


def _require_enabled_plugin(
    *,
    plugin_root: Path,
    expected: _ExpectedPluginIdentity,
    env: dict[str, str],
) -> _PluginListEvidence:
    codex = resolve_cli_executable("codex", env)
    try:
        result = run_command(
            "codex_native_plugin_list",
            (codex, "plugin", "list", "--json"),
            cwd=plugin_root,
            env=env,
            timeout_seconds=60,
        )
    except CommandFailureError as exc:
        raise CodexNativePreflightError(
            "codex_native_plugin_list_command_failed",
            plugin_list_exit_code=exc.receipt.exit_code,
            plugin_list_stdout_schema_sha256=_stdout_schema_sha256(exc.stdout),
        ) from exc
    except OSError as exc:
        raise CodexNativePreflightError("codex_native_plugin_list_command_failed") from exc
    evidence = _PluginListEvidence(
        exit_code=result.receipt.exit_code,
        stdout_schema_sha256=_stdout_schema_sha256(result.stdout),
    )
    try:
        parsed = _CodexPluginList.model_validate_json(result.stdout)
    except ValidationError as exc:
        raise CodexNativePreflightError(
            "codex_native_plugin_list_schema_invalid",
            plugin_list_exit_code=evidence.exit_code,
            plugin_list_stdout_schema_sha256=evidence.stdout_schema_sha256,
        ) from exc
    matches = tuple(
        entry
        for entry in parsed.installed
        if entry.name == expected.name
        and entry.version == expected.version
        and entry.marketplace_name == expected.marketplace_name
        and entry.plugin_id == expected.plugin_id
    )
    if len(matches) != 1:
        raise CodexNativePreflightError(
            "codex_native_plugin_identity_cardinality_invalid",
            plugin_list_exit_code=evidence.exit_code,
            plugin_list_stdout_schema_sha256=evidence.stdout_schema_sha256,
        )
    if not matches[0].installed:
        raise CodexNativePreflightError(
            "codex_native_plugin_not_installed",
            plugin_list_exit_code=evidence.exit_code,
            plugin_list_stdout_schema_sha256=evidence.stdout_schema_sha256,
        )
    if not matches[0].enabled:
        raise CodexNativePreflightError(
            "codex_native_plugin_not_enabled",
            plugin_list_exit_code=evidence.exit_code,
            plugin_list_stdout_schema_sha256=evidence.stdout_schema_sha256,
        )
    return evidence


def _stdout_schema_sha256(raw: str) -> str:
    try:
        value = cast("object", json.loads(raw))
    except json.JSONDecodeError:
        schema: JsonValue = {"type": "invalid_json"}
    else:
        schema = _json_schema(value)
    encoded = json.dumps(schema, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json_schema(value: object) -> JsonValue:
    if isinstance(value, list):
        unique: dict[str, JsonValue] = {}
        for item in cast("list[object]", value):
            item_schema = _json_schema(item)
            key = json.dumps(
                item_schema,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            unique[key] = item_schema
        return {"type": "array", "items": tuple(unique[key] for key in sorted(unique))}
    if isinstance(value, dict):
        raw = cast("dict[object, object]", value)
        properties = {
            key: _json_schema(item)
            for key, item in raw.items()
            if isinstance(key, str)
        }
        return {"type": "object", "properties": properties}
    return _scalar_json_schema(value)


def _scalar_json_schema(value: object) -> str:
    if value is None:
        schema = "null"
    elif isinstance(value, bool):
        schema = "boolean"
    elif isinstance(value, str):
        schema = "string"
    elif isinstance(value, (int, float)):
        schema = "number"
    else:
        schema = "unknown"
    return schema


def _last_json_object(raw: str) -> dict[str, JsonValue]:
    for line in reversed(raw.splitlines()):
        if not line.strip().startswith("{"):
            continue
        try:
            return _JSON_OBJECT.validate_json(line)
        except ValidationError:
            continue
    raise ValueError("probe_payload_invalid")


def _strict_tool_names(payload: dict[str, JsonValue]) -> tuple[str, ...]:
    raw = payload.get("tool_names")
    if not isinstance(raw, list) or any(not isinstance(name, str) or not name for name in raw):
        raise ValueError("tool_names_invalid")
    names = tuple(cast("str", name) for name in raw)
    if len(names) != len(frozenset(names)):
        raise ValueError("tool_names_duplicate")
    return names
