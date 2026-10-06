from __future__ import annotations

import tomllib
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Final

import pytest
from pydantic import TypeAdapter

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type PackagingSet = tuple[JsonObject, JsonObject, JsonObject, JsonObject, JsonObject]

PLUGIN_NAME: Final = "saxo-bank-mcp"
DISPLAY_NAME: Final = "Saxo Bank MCP"
MARKETPLACE_NAME: Final = "sig" + "vardt"
REPOSITORY_URL: Final = f"https://github.com/{MARKETPLACE_NAME}/saxo-bank-mcp"
MCP_CONFIG_PATH: Final = "./.mcp.json"
EXPECTED_MCP_ARGS: Final = (
    "run",
    "--project",
    ".",
    "saxo-bank-mcp",
    "--transport",
    "stdio",
)
CLAUDE_MCP_SERVERS: Final[JsonObject] = {
    PLUGIN_NAME: {
        "command": "uv",
        "args": [
            "--directory",
            "${CLAUDE_PLUGIN_ROOT}",
            "run",
            "saxo-bank-mcp",
            "--transport",
            "stdio",
        ],
    },
}
CLAUDE_MCP_ERROR: Final = "claude plugin.json mcpServers must launch from ${CLAUDE_PLUGIN_ROOT}"
SECRET_PATTERNS: Final = ("token", "secret", "password", "api_key", "apikey", "bearer")

JSON_OBJECT_ADAPTER: Final[TypeAdapter[JsonObject]] = TypeAdapter(dict[str, JsonValue])


def test_plugin_packaging_files_match_project_identity_and_version() -> None:
    # Given: the repository project version is the release source of truth.
    project_version = _project_version()

    # When: the Codex and Claude plugin manifests and marketplaces are loaded.
    codex_manifest = _json_object(Path(".codex-plugin/plugin.json"))
    claude_manifest = _json_object(Path(".claude-plugin/plugin.json"))
    codex_marketplace = _json_object(Path(".agents/plugins/marketplace.json"))
    claude_marketplace = _json_object(Path(".claude-plugin/marketplace.json"))

    # Then: every publication file has the same identity and version.
    assert _string(codex_manifest, "name", ".codex-plugin/plugin.json.name") == PLUGIN_NAME
    assert _string(claude_manifest, "name", ".claude-plugin/plugin.json.name") == PLUGIN_NAME
    assert _string(codex_manifest, "version", ".codex-plugin/plugin.json.version") == (
        project_version
    )
    assert _string(claude_manifest, "version", ".claude-plugin/plugin.json.version") == (
        project_version
    )
    assert _string(codex_marketplace, "name", ".agents/plugins/marketplace.json.name") == (
        MARKETPLACE_NAME
    )
    assert _string(claude_marketplace, "name", ".claude-plugin/marketplace.json.name") == (
        MARKETPLACE_NAME
    )
    assert _string(
        claude_marketplace,
        "description",
        ".claude-plugin/marketplace.json.description",
    )
    assert _plugin_entry(codex_marketplace, ".agents/plugins/marketplace.json")[
        "version"
    ] == project_version
    assert _plugin_entry(claude_marketplace, ".claude-plugin/marketplace.json")[
        "version"
    ] == project_version


def test_codex_manifest_and_marketplace_use_root_shared_mcp_config() -> None:
    # Given: the Codex plugin is distributed from the repository root.
    manifest = _json_object(Path(".codex-plugin/plugin.json"))
    marketplace = _json_object(Path(".agents/plugins/marketplace.json"))

    # When: the Codex publication contract is inspected.
    entry = _plugin_entry(marketplace, ".agents/plugins/marketplace.json")
    source = _object(entry, "source", ".agents/plugins/marketplace.json.plugins[0].source")
    interface = _object(manifest, "interface", ".codex-plugin/plugin.json.interface")

    # Then: Codex points at the shared MCP file and root plugin source only.
    assert _string(manifest, "repository", ".codex-plugin/plugin.json.repository") == (
        REPOSITORY_URL
    )
    assert _string(manifest, "mcpServers", ".codex-plugin/plugin.json.mcpServers") == (
        MCP_CONFIG_PATH
    )
    assert _string(interface, "displayName", ".codex-plugin/plugin.json.interface.displayName") == (
        DISPLAY_NAME
    )
    assert entry["name"] == PLUGIN_NAME
    assert entry["repository"] == REPOSITORY_URL
    assert source == {"source": "local", "path": "."}
    assert "skills" not in manifest
    assert "permissions" not in manifest


def test_claude_manifest_and_marketplace_validate_against_native_shape() -> None:
    # Given: the Claude plugin uses its own manifest and marketplace files.
    manifest = _json_object(Path(".claude-plugin/plugin.json"))
    marketplace = _json_object(Path(".claude-plugin/marketplace.json"))

    # When: the Claude publication contract is inspected.
    entry = _plugin_entry(marketplace, ".claude-plugin/marketplace.json")

    # Then: Claude uses supported fields, repository-root source, and a plugin-root MCP launch,
    # because Claude Code resolves a relative `cwd` against the session, not the plugin.
    assert set(manifest) == {
        "name",
        "displayName",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "mcpServers",
    }
    assert _string(manifest, "displayName", ".claude-plugin/plugin.json.displayName") == (
        DISPLAY_NAME
    )
    assert _string(manifest, "repository", ".claude-plugin/plugin.json.repository") == (
        REPOSITORY_URL
    )
    assert manifest["mcpServers"] == CLAUDE_MCP_SERVERS
    assert entry["name"] == PLUGIN_NAME
    assert entry["source"] == "."
    assert entry["description"] == manifest["description"]
    assert "allowedTools" not in manifest
    assert "userConfig" not in manifest


def test_shared_mcp_config_is_credential_free_stdio_launch_contract() -> None:
    # Given: the Codex manifest and in-repository sessions use the root MCP config.
    mcp_config = _json_object(Path(".mcp.json"))

    # When: the server launch contract is inspected.
    servers = _object(mcp_config, "mcpServers", ".mcp.json.mcpServers")
    server = _object(servers, PLUGIN_NAME, ".mcp.json.mcpServers.saxo-bank-mcp")

    # Then: there is one relative, credential-free stdio server entry.
    assert set(mcp_config) == {"mcpServers"}
    assert set(servers) == {PLUGIN_NAME}
    assert server == {
        "command": "uv",
        "args": list(EXPECTED_MCP_ARGS),
        "cwd": ".",
    }
    assert _packaging_errors(
        (
            _json_object(Path(".codex-plugin/plugin.json")),
            _json_object(Path(".claude-plugin/plugin.json")),
            _json_object(Path(".agents/plugins/marketplace.json")),
            _json_object(Path(".claude-plugin/marketplace.json")),
            mcp_config,
        ),
        _project_version(),
    ) == []


@pytest.mark.parametrize(
    ("fixture_name", "expected_error"),
    [
        ("custom_codex_mcp_path", "codex plugin.json mcpServers must be ./.mcp.json"),
        ("claude_session_relative_mcp", CLAUDE_MCP_ERROR),
        ("stale_version", "claude plugin.json version must equal project.version"),
        ("absolute_cwd", "mcpServers.saxo-bank-mcp.cwd must be ."),
        ("embedded_token", "mcpServers.saxo-bank-mcp must not contain secret-like config"),
    ],
)
def test_packaging_contract_rejects_field_specific_failure_fixtures(
    fixture_name: str,
    expected_error: str,
) -> None:
    # Given: a valid product packaging set and one adversarial fixture mutation.
    project_version = _project_version()
    codex_manifest = deepcopy(_json_object(Path(".codex-plugin/plugin.json")))
    claude_manifest = deepcopy(_json_object(Path(".claude-plugin/plugin.json")))
    codex_marketplace = deepcopy(_json_object(Path(".agents/plugins/marketplace.json")))
    claude_marketplace = deepcopy(_json_object(Path(".claude-plugin/marketplace.json")))
    mcp_config = deepcopy(_json_object(Path(".mcp.json")))
    credential_fixture = "xoxb-SENSITIVE-VALUE-123"

    # When: the selected bad fixture is validated.
    match fixture_name:
        case "custom_codex_mcp_path":
            codex_manifest["mcpServers"] = "./custom.mcp.json"
        case "claude_session_relative_mcp":
            claude_manifest["mcpServers"] = MCP_CONFIG_PATH
        case "stale_version":
            claude_manifest["version"] = "0.1.1"
        case "absolute_cwd":
            _server(mcp_config)["cwd"] = str(Path("saxo-bank-mcp").resolve())
        case "embedded_token":
            _server(mcp_config)["env"] = {"SAXO_ACCESS_TOKEN": credential_fixture}
        case unreachable:
            pytest.fail(f"unknown fixture: {unreachable}")
    errors = _packaging_errors(
        (codex_manifest, claude_manifest, codex_marketplace, claude_marketplace, mcp_config),
        project_version,
    )

    # Then: validation fails on the intended field without echoing raw secrets.
    assert expected_error in errors
    assert credential_fixture not in "\n".join(errors)


def _packaging_errors(
    packaging: PackagingSet,
    project_version: str,
) -> list[str]:
    codex_manifest, claude_manifest, codex_marketplace, claude_marketplace, mcp_config = packaging
    errors: list[str] = []
    if codex_manifest.get("mcpServers") != MCP_CONFIG_PATH:
        errors.append("codex plugin.json mcpServers must be ./.mcp.json")
    if claude_manifest.get("mcpServers") != CLAUDE_MCP_SERVERS:
        errors.append(CLAUDE_MCP_ERROR)
    for label, payload in (
        ("codex plugin.json", codex_manifest),
        ("claude plugin.json", claude_manifest),
        ("codex marketplace", _plugin_entry(codex_marketplace, ".agents/plugins/marketplace.json")),
        (
            "claude marketplace",
            _plugin_entry(claude_marketplace, ".claude-plugin/marketplace.json"),
        ),
    ):
        if payload.get("version") != project_version:
            errors.append(f"{label} version must equal project.version")
    server = _server(mcp_config)
    if server.get("command") != "uv":
        errors.append("mcpServers.saxo-bank-mcp.command must be uv")
    if server.get("args") != list(EXPECTED_MCP_ARGS):
        errors.append("mcpServers.saxo-bank-mcp.args must launch stdio")
    if server.get("cwd") != ".":
        errors.append("mcpServers.saxo-bank-mcp.cwd must be .")
    if "env" in server or _has_secret_like_string(server):
        errors.append("mcpServers.saxo-bank-mcp must not contain secret-like config")
    return errors


def _project_version() -> str:
    payload = JSON_OBJECT_ADAPTER.validate_python(
        tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    )
    project = _object(payload, "project", "pyproject.toml.project")
    return _string(project, "version", "pyproject.toml.project.version")


def _json_object(path: Path) -> JsonObject:
    assert path.is_file(), f"{path} must exist"
    return JSON_OBJECT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def _plugin_entry(marketplace: JsonObject, label: str) -> JsonObject:
    plugins = _list(marketplace, "plugins", f"{label}.plugins")
    assert len(plugins) == 1, f"{label}.plugins must contain exactly one plugin"
    entry = plugins[0]
    assert isinstance(entry, dict), f"{label}.plugins[0] must be an object"
    return entry


def _server(mcp_config: JsonObject) -> JsonObject:
    servers = _object(mcp_config, "mcpServers", ".mcp.json.mcpServers")
    return _object(servers, PLUGIN_NAME, ".mcp.json.mcpServers.saxo-bank-mcp")


def _object(payload: JsonObject, key: str, label: str) -> JsonObject:
    value = payload.get(key)
    assert isinstance(value, dict), f"{label} must be an object"
    return value


def _list(payload: JsonObject, key: str, label: str) -> list[JsonValue]:
    value = payload.get(key)
    assert isinstance(value, list), f"{label} must be a list"
    return value


def _string(payload: JsonObject, key: str, label: str) -> str:
    value = payload.get(key)
    assert isinstance(value, str), f"{label} must be a string"
    return value


def _has_secret_like_string(value: JsonValue) -> bool:
    return any(pattern in item.lower() for item in _strings(value) for pattern in SECRET_PATTERNS)


def _strings(value: JsonValue) -> Iterator[str]:
    match value:
        case str():
            yield value
        case list():
            for item in value:
                yield from _strings(item)
        case dict():
            for key, item in value.items():
                yield key
                yield from _strings(item)
        case _:
            return
