from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError, run_command
from saxo_bank_mcp.agent_skill_eval_commands import resolve_cli_executable
from saxo_bank_mcp.agent_skill_install_paths import PLUGIN_NAME
from saxo_bank_mcp.agent_skill_install_probe import probe_root_stdio
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS

_JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


class _CodexPlugin(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    name: str
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


@dataclass(frozen=True, slots=True)
class CodexNativePreflightError(ValueError):
    reason: str
    mcp_started: bool | None = False

    def __str__(self) -> str:  # noqa: D105
        return self.reason


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
    _require_exact_disposable_binding(codex_home=codex_home, plugin_root=plugin_root, env=env)
    expected = _validated_grants(logical_grants)
    _require_enabled_plugin(plugin_root=plugin_root, env=env)
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
        ) from exc
    if annotations != [] or tool_count != len(visible):
        raise CodexNativePreflightError(
            "codex_native_tool_visibility_invalid",
            mcp_started=True,
        )
    if frozenset(visible) != frozenset(expected) or len(visible) != len(expected):
        raise CodexNativePreflightError(
            "codex_native_tool_visibility_mismatch",
            mcp_started=True,
        )
    return CodexNativePreflightReceipt(
        plugin_enabled=True,
        mcp_started=True,
        visible_logical_tools=tuple(sorted(visible)),
    )


def _require_exact_disposable_binding(
    *,
    codex_home: Path,
    plugin_root: Path,
    env: dict[str, str],
) -> None:
    try:
        home = codex_home.resolve(strict=True)
        configured = Path(env.get("CODEX_HOME", "")).resolve(strict=True)
        plugin = plugin_root.resolve(strict=True)
    except OSError as exc:
        raise CodexNativePreflightError("codex_native_registration_invalid") from exc
    if configured != home or not plugin.is_relative_to(home):
        raise CodexNativePreflightError("codex_native_registration_invalid")


def _validated_grants(logical_grants: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not logical_grants
        or len(logical_grants) != len(frozenset(logical_grants))
        or any(tool not in ALL_LOGICAL_TOOL_IDS for tool in logical_grants)
    ):
        raise CodexNativePreflightError("codex_native_tool_grants_invalid")
    return logical_grants


def _require_enabled_plugin(*, plugin_root: Path, env: dict[str, str]) -> None:
    codex = resolve_cli_executable("codex", env)
    try:
        result = run_command(
            "codex_native_plugin_list",
            (codex, "plugin", "list", "--json"),
            cwd=plugin_root,
            env=env,
            timeout_seconds=60,
        )
        parsed = _CodexPluginList.model_validate_json(result.stdout)
    except (CommandFailureError, OSError, ValidationError) as exc:
        raise CodexNativePreflightError("codex_native_registration_invalid") from exc
    matches = tuple(entry for entry in parsed.installed if entry.name == PLUGIN_NAME)
    if len(matches) != 1 or not matches[0].enabled or not matches[0].installed:
        raise CodexNativePreflightError("codex_native_registration_invalid")


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
