from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_text
from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError, run_command
from saxo_bank_mcp.agent_skill_eval_commands import resolve_cli_executable
from saxo_bank_mcp.agent_skill_install_paths import MARKETPLACE_NAME, PLUGIN_NAME
from saxo_bank_mcp.agent_skill_install_probe import probe_root_stdio
from saxo_bank_mcp.server_eval_tool_filter import resolve_eval_tool_filter
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS

_JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])
_OWNER_FILE_MODE: Final = 0o600
_CASE_SERVER_ENV_KEYS: Final = frozenset(
    {
        "HOME",
        "PATH",
        "TMPDIR",
        "TMP",
        "TEMP",
        "UV_CACHE_DIR",
        "UV_OFFLINE",
        "UV_PROJECT_ENVIRONMENT",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
    }
)
_CASE_SAXO_ENV_KEYS: Final = frozenset(
    {
        "SAXO_MCP_ENVIRONMENT",
        "SAXO_MCP_ENABLE_LIVE_READS",
        "SAXO_MCP_ENABLE_LIVE_WRITES",
        "SAXO_MCP_SIM_CREDENTIAL_FILE",
        "SAXO_MCP_SIM_REDIRECT_URI",
        "SAXO_MCP_TOKEN_CACHE_PATH",
        "SAXO_MCP_SIM_AUTH_URL",
        "SAXO_MCP_SIM_TOKEN_URL",
        "SAXO_MCP_ACCOUNT_ALLOWLIST",
        "SAXO_MCP_INSTRUMENT_ALLOWLIST",
        "SAXO_MCP_EVAL_TOOL_FILTER",
        "SAXO_MCP_EVAL_ALLOWED_TOOLS",
    }
)
type McpProbeStage = Literal[
    "runtime_binding",
    "contract_validation",
    "command_start",
    "command_exit",
    "payload_parse",
    "tool_visibility",
    "complete",
]


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
    mcp_probe_stage: Literal["complete"]
    mcp_probe_exit_code: int
    mcp_probe_stdout_schema_sha256: str
    mcp_config_sha256: str | None = None
    mcp_config_path_identity_sha256: str | None = None


@dataclass(slots=True)
class CodexNativePreflightError(ValueError):
    reason: str
    mcp_started: bool | None = False
    plugin_list_exit_code: int | None = None
    plugin_list_stdout_schema_sha256: str | None = None
    mcp_probe_stage: McpProbeStage | None = None
    mcp_probe_exit_code: int | None = None
    mcp_probe_stdout_schema_sha256: str | None = None

    def __str__(self) -> str:  # noqa: D105
        return self.reason


@dataclass(frozen=True, slots=True)
class _ExpectedPluginIdentity:
    name: str
    version: str
    marketplace_name: str
    plugin_id: str


@dataclass(frozen=True, slots=True)
class CodexNativeCaseMcpBinding:
    """Private path plus public digests for one case-scoped contained MCP config."""

    path: Path
    interpreter: Path
    config_sha256: str
    path_identity_sha256: str
    original_bytes: bytes
    original_mode: int


@contextmanager
def codex_native_case_mcp_config(
    *,
    plugin_root: Path,
    logical_grants: tuple[str, ...],
    env: Mapping[str, str],
    interpreter: Path,
) -> Generator[CodexNativeCaseMcpBinding]:
    """Install one owner-only case config, then restore the registered plugin bytes."""
    expected = _validated_grants(logical_grants)
    try:
        root = plugin_root.resolve(strict=True)
        executable = interpreter.absolute()
        executable_meta = executable.stat()
        path = root / ".mcp.json"
        metadata = os.lstat(path)
        original_bytes = path.read_bytes()
    except OSError as exc:
        raise CodexNativePreflightError(
            "codex_native_mcp_config_binding_invalid",
            mcp_probe_stage="runtime_binding",
        ) from exc
    invalid_source = (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or not stat.S_ISREG(executable_meta.st_mode)
        or not os.access(executable, os.X_OK)
    )
    if invalid_source:
        raise CodexNativePreflightError(
            "codex_native_mcp_config_binding_invalid",
            mcp_probe_stage="runtime_binding",
        )
    original_mode = stat.S_IMODE(metadata.st_mode)
    payload = _case_mcp_payload(
        root=root,
        interpreter=executable,
        logical_grants=expected,
        env=env,
    )
    encoded = json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    path_identity_sha256 = _path_identity_sha256(path)
    config_sha256 = hashlib.sha256(encoded.encode()).hexdigest()
    binding = CodexNativeCaseMcpBinding(
        path=path,
        interpreter=executable,
        config_sha256=config_sha256,
        path_identity_sha256=path_identity_sha256,
        original_bytes=original_bytes,
        original_mode=original_mode,
    )
    try:
        write_text(path, encoded)
        path.chmod(_OWNER_FILE_MODE)
        verify_codex_native_case_mcp_config(binding, logical_grants=expected, env=env)
        yield binding
    except CodexNativePreflightError:
        raise
    except OSError as exc:
        raise CodexNativePreflightError(
            "codex_native_mcp_config_binding_invalid",
            mcp_probe_stage="contract_validation",
        ) from exc
    finally:
        try:
            write_text(path, original_bytes.decode("utf-8"))
            path.chmod(original_mode)
        except (OSError, UnicodeDecodeError) as exc:
            raise CodexNativePreflightError(
                "codex_native_mcp_config_restore_failed",
                mcp_probe_stage="contract_validation",
            ) from exc


def verify_codex_native_case_mcp_config(
    binding: CodexNativeCaseMcpBinding,
    *,
    logical_grants: tuple[str, ...],
    env: Mapping[str, str],
) -> None:
    """Fail closed when the installed case config changed after its binding."""
    expected = _validated_grants(logical_grants)
    try:
        metadata = os.lstat(binding.path)
        raw = binding.path.read_bytes()
        payload = _JSON_OBJECT.validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise CodexNativePreflightError(
            "codex_native_mcp_config_binding_invalid",
            mcp_probe_stage="contract_validation",
        ) from exc
    invalid_file = (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != _OWNER_FILE_MODE
    )
    expected_payload = _case_mcp_payload(
        root=binding.path.parent.resolve(),
        interpreter=binding.interpreter,
        logical_grants=expected,
        env=env,
    )
    if (
        invalid_file
        or hashlib.sha256(raw).hexdigest() != binding.config_sha256
        or _path_identity_sha256(binding.path) != binding.path_identity_sha256
        or payload != expected_payload
    ):
        raise CodexNativePreflightError(
            "codex_native_mcp_config_binding_invalid",
            mcp_probe_stage="contract_validation",
        )


def _case_mcp_payload(
    *,
    root: Path,
    interpreter: Path,
    logical_grants: tuple[str, ...],
    env: Mapping[str, str],
) -> dict[str, JsonValue]:
    if resolve_eval_tool_filter(env) != frozenset(logical_grants):
        raise CodexNativePreflightError("codex_native_mcp_config_binding_invalid")
    if (
        env.get("SAXO_MCP_ENVIRONMENT") != "SIM"
        or env.get("SAXO_MCP_ENABLE_LIVE_READS", "0") not in {"0", ""}
        or env.get("SAXO_MCP_ENABLE_LIVE_WRITES", "") != ""
        or any(
            key.startswith("SAXO_MCP_") and key not in _CASE_SAXO_ENV_KEYS and value
            for key, value in env.items()
        )
    ):
        raise CodexNativePreflightError("codex_native_mcp_config_binding_invalid")
    server_env = {
        key: value
        for key, value in env.items()
        if key in _CASE_SAXO_ENV_KEYS or key in _CASE_SERVER_ENV_KEYS
    }
    required = {
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": env.get("SAXO_MCP_ENABLE_LIVE_READS", "0"),
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
        "SAXO_MCP_EVAL_TOOL_FILTER": "1",
        "SAXO_MCP_EVAL_ALLOWED_TOOLS": ",".join(logical_grants),
    }
    server_env.update(required)
    return {
        "mcpServers": {
            PLUGIN_NAME: {
                "command": str(interpreter.absolute()),
                "args": [
                    "-I",
                    "-B",
                    "-m",
                    "saxo_bank_mcp",
                    "--transport",
                    "stdio",
                ],
                "cwd": str(root),
                "env": server_env,
            }
        }
    }


def _path_identity_sha256(path: Path) -> str:
    return hashlib.sha256(str(path.absolute()).encode()).hexdigest()


def preflight_codex_native_case(  # noqa: PLR0913
    *,
    codex_home: Path,
    plugin_root: Path,
    logical_grants: tuple[str, ...],
    env: dict[str, str],
    probe_env: Path,
    retained_project_environment: Path | None = None,
    retained_interpreter: Path | None = None,
    mcp_config_binding: CodexNativeCaseMcpBinding | None = None,
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
    interpreter = _require_retained_interpreter(
        retained_project_environment=retained_project_environment,
        retained_interpreter=retained_interpreter,
    )
    if mcp_config_binding is not None:
        verify_codex_native_case_mcp_config(
            mcp_config_binding,
            logical_grants=expected,
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
            interpreter=interpreter,
        )
    except CommandFailureError as exc:
        raise CodexNativePreflightError(
            "codex_native_mcp_start_failed",
            mcp_started=None,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
            mcp_probe_stage="command_exit",
            mcp_probe_exit_code=exc.receipt.exit_code,
            mcp_probe_stdout_schema_sha256=_probe_stdout_schema_sha256(exc.stdout),
        ) from exc
    except FileNotFoundError as exc:
        raise CodexNativePreflightError(
            "codex_native_mcp_start_failed",
            mcp_started=False,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
            mcp_probe_stage="contract_validation",
        ) from exc
    except (OSError, ValueError) as exc:
        raise CodexNativePreflightError(
            "codex_native_mcp_start_failed",
            mcp_started=None,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
            mcp_probe_stage="command_start",
        ) from exc
    probe_exit_code = result.receipt.exit_code
    probe_schema_sha256 = _probe_stdout_schema_sha256(result.stdout)
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
            mcp_probe_stage="payload_parse",
            mcp_probe_exit_code=probe_exit_code,
            mcp_probe_stdout_schema_sha256=probe_schema_sha256,
        ) from exc
    if annotations != [] or tool_count != len(visible):
        raise CodexNativePreflightError(
            "codex_native_tool_visibility_invalid",
            mcp_started=True,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
            mcp_probe_stage="tool_visibility",
            mcp_probe_exit_code=probe_exit_code,
            mcp_probe_stdout_schema_sha256=probe_schema_sha256,
        )
    if frozenset(visible) != frozenset(expected) or len(visible) != len(expected):
        raise CodexNativePreflightError(
            "codex_native_tool_visibility_mismatch",
            mcp_started=True,
            plugin_list_exit_code=plugin_list.exit_code,
            plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
            mcp_probe_stage="tool_visibility",
            mcp_probe_exit_code=probe_exit_code,
            mcp_probe_stdout_schema_sha256=probe_schema_sha256,
        )
    return CodexNativePreflightReceipt(
        plugin_enabled=True,
        mcp_started=True,
        visible_logical_tools=tuple(sorted(visible)),
        plugin_list_exit_code=plugin_list.exit_code,
        plugin_list_stdout_schema_sha256=plugin_list.stdout_schema_sha256,
        mcp_probe_stage="complete",
        mcp_probe_exit_code=probe_exit_code,
        mcp_probe_stdout_schema_sha256=probe_schema_sha256,
        mcp_config_sha256=(
            None if mcp_config_binding is None else mcp_config_binding.config_sha256
        ),
        mcp_config_path_identity_sha256=(
            None if mcp_config_binding is None else mcp_config_binding.path_identity_sha256
        ),
    )


def _require_retained_interpreter(
    *,
    retained_project_environment: Path | None,
    retained_interpreter: Path | None,
) -> Path:
    project_environment = retained_project_environment or Path(sys.prefix)
    interpreter = retained_interpreter or Path(sys.executable)
    try:
        expected_environment = Path(sys.prefix).resolve(strict=True)
        actual_environment = project_environment.resolve(strict=True)
        expected_interpreter = Path(sys.executable).absolute()
        actual_interpreter = interpreter.absolute()
        metadata = os.lstat(actual_interpreter)
    except OSError as exc:
        raise CodexNativePreflightError(
            "codex_native_mcp_runtime_binding_invalid",
            mcp_probe_stage="runtime_binding",
        ) from exc
    if (
        actual_environment != expected_environment
        or actual_interpreter != expected_interpreter
        or actual_interpreter.parent.parent.resolve() != actual_environment
        or not os.access(actual_interpreter, os.X_OK)
        or not (metadata.st_mode & 0o100)
    ):
        raise CodexNativePreflightError(
            "codex_native_mcp_runtime_binding_invalid",
            mcp_probe_stage="runtime_binding",
        )
    return actual_interpreter


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


def _probe_stdout_schema_sha256(raw: str) -> str:
    try:
        schema = _json_schema(_last_json_object(raw))
    except ValueError:
        schema = {"type": "invalid_json"}
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
        properties = {key: _json_schema(item) for key, item in raw.items() if isinstance(key, str)}
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
