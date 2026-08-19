from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp.agent_skill_eval_commands import (
    claude_non_router_command,
    codex_non_router_command,
)
from saxo_bank_mcp.agent_skill_eval_tool_protocol import (
    parse_claude_model_output,
    parse_codex_model_output,
)

TWO_EVENTS: Final = 2
ROOT: Final = Path(__file__).resolve().parents[1]


def test_codex_parser_dedupes_start_completion_and_keeps_order() -> None:
    # Given: Codex JSONL with start/completion for the same tool-use id plus a second tool.
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "call-1",
                        "type": "mcp_tool_call",
                        "server": "saxo_bank_mcp",
                        "tool": "saxo_auth_status",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "call-1",
                        "type": "mcp_tool_call",
                        "server": "saxo_bank_mcp",
                        "tool": "saxo_auth_status",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "call-2",
                        "type": "mcp_tool_call",
                        "server": "saxo_bank_mcp",
                        "tool": "saxo_refresh_token",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "answer",
                        "type": "agent_message",
                        "text": "session recovered without secrets",
                    },
                },
            ),
        ),
    )

    # When: the stream is parsed.
    trace = parse_codex_model_output(stream)

    # Then: first-call order is preserved and completion is not double-counted.
    assert trace.invoked_logical_tools == ("saxo_auth_status", "saxo_refresh_token")
    assert trace.saxo_event_count == TWO_EVENTS
    assert trace.mcp_event_count == TWO_EVENTS
    assert trace.tool_event_count == TWO_EVENTS
    assert "session recovered" in trace.assistant_text


def test_codex_0147_parser_fixture_reads_completed_mcp_identity() -> None:
    stream = (ROOT / "tests/fixtures/codex-0.147-mcp-events.jsonl").read_text(encoding="utf-8")

    trace = parse_codex_model_output(stream)

    assert trace.parse_error == ""
    assert trace.invoked_logical_tools == ("saxo_health",)
    assert trace.mcp_event_count == 1
    assert trace.saxo_event_count == 1


@pytest.mark.parametrize(
    "item",
    [
        {"id": "unknown-empty", "type": "mcp_tool_call"},
        {
            "id": "unknown-saxo",
            "type": "mcp_tool_call",
            "server": "saxo-bank-mcp",
            "tool": "unknown_tool",
        },
    ],
)
def test_codex_parser_fails_closed_for_unknown_or_empty_mcp_identity(
    item: dict[str, str],
) -> None:
    stream = json.dumps({"type": "item.completed", "item": item})

    trace = parse_codex_model_output(stream)

    assert trace.invoked_logical_tools == ()
    assert trace.mcp_event_count == 1
    assert trace.non_saxo_mcp_event_count == 1
    assert trace.parse_error == "unknown_mcp_event"


def test_codex_parser_rejects_malicious_prose_without_events() -> None:
    # Given: assistant prose that names a tool but no MCP event exists.
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "answer",
                        "type": "agent_message",
                        "text": "I called saxo_place_order successfully",
                    },
                },
            ),
        ),
    )

    # When: the stream is parsed.
    trace = parse_codex_model_output(stream)

    # Then: prose is not treated as a tool call.
    assert trace.invoked_logical_tools == ()
    assert trace.saxo_event_count == 0
    assert "saxo_place_order" in trace.assistant_text


def test_codex_parser_counts_forbidden_command_and_non_saxo_mcp() -> None:
    # Given: a shell command and a non-Saxo MCP call.
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "sh",
                        "type": "command_execution",
                        "command": "curl https://example.test",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "other",
                        "type": "mcp_tool_call",
                        "server": "playwright",
                        "tool": "browser_navigate",
                    },
                },
            ),
        ),
    )

    # When: the stream is parsed.
    trace = parse_codex_model_output(stream)

    # Then: command and non-Saxo MCP are classified without storing payloads.
    assert trace.command_event_count == 1
    assert trace.non_saxo_mcp_event_count == 1
    assert trace.parse_error == "non_saxo_mcp_event"
    assert trace.invoked_logical_tools == ()
    assert len(trace.non_saxo_event_descriptors) == 1
    descriptor = trace.non_saxo_event_descriptors[0]
    assert descriptor.outer_event_type == "item.completed"
    assert descriptor.item_type == "mcp_tool_call"
    assert descriptor.server_category == "foreign_mcp"
    assert descriptor.protocol_name is None
    assert descriptor.server_sha256 == hashlib.sha256(b"playwright").hexdigest()
    assert descriptor.tool_sha256 == hashlib.sha256(b"browser_navigate").hexdigest()
    assert descriptor.name_sha256 == hashlib.sha256(b"").hexdigest()
    assert "playwright" not in descriptor.model_dump_json()
    assert "browser_navigate" not in descriptor.model_dump_json()


def test_codex_parser_describes_allowlisted_protocol_without_raw_identity() -> None:
    stream = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "protocol",
                "type": "mcp_tool_call",
                "server": "codex",
                "tool": "list_mcp_resources",
            },
        },
    )

    trace = parse_codex_model_output(stream)

    assert trace.parse_error == ""
    assert trace.non_saxo_mcp_event_count == 0
    assert len(trace.non_saxo_event_descriptors) == 1
    descriptor = trace.non_saxo_event_descriptors[0]
    assert descriptor.server_category == "codex_protocol"
    assert descriptor.protocol_name == "list_mcp_resources"
    assert descriptor.server_sha256 is None
    assert descriptor.tool_sha256 is None
    assert descriptor.name_sha256 is None


def test_codex_parser_describes_empty_unknown_mcp_identity_with_hashes() -> None:
    trace = parse_codex_model_output(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "empty", "type": "mcp_tool_call"},
            },
        ),
    )

    assert trace.parse_error == "unknown_mcp_event"
    assert len(trace.non_saxo_event_descriptors) == 1
    descriptor = trace.non_saxo_event_descriptors[0]
    assert descriptor.server_category == "identity_unknown"
    assert descriptor.protocol_name is None
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    assert descriptor.server_sha256 == empty_sha256
    assert descriptor.tool_sha256 == empty_sha256
    assert descriptor.name_sha256 == empty_sha256


def test_claude_parser_successful_required_sequence() -> None:
    # Given: Claude stream-json with two Saxo MCP tool_use events and final text.
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "mcp__plugin_saxo_bank_mcp_saxo_bank_mcp__saxo_health",
                            },
                            {
                                "type": "tool_use",
                                "id": "t2",
                                "name": (
                                    "mcp__plugin_saxo_bank_mcp_saxo_bank_mcp__"
                                    "saxo_list_registered_endpoints"
                                ),
                            },
                        ],
                    },
                },
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "matched natural prompts 60 logical tools cleanup",
                },
            ),
        ),
    )

    # When: the stream is parsed.
    trace = parse_claude_model_output(stream)

    # Then: logical names and assistant text are retained without args/results.
    assert trace.invoked_logical_tools == (
        "saxo_health",
        "saxo_list_registered_endpoints",
    )
    assert trace.saxo_event_count == TWO_EVENTS
    assert "60 logical tools" in trace.assistant_text


def test_claude_parser_dedupes_and_flags_forbidden_builtin() -> None:
    # Given: duplicate tool_use id and a Bash tool event.
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "mcp__plugin_saxo_bank_mcp_saxo_bank_mcp__saxo_health",
                            },
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "mcp__plugin_saxo_bank_mcp_saxo_bank_mcp__saxo_health",
                            },
                            {"type": "tool_use", "id": "bash", "name": "Bash"},
                        ],
                    },
                },
            ),
        ),
    )

    # When: the stream is parsed.
    trace = parse_claude_model_output(stream)

    # Then: Saxo call is unique and Bash is a command event.
    assert trace.invoked_logical_tools == ("saxo_health",)
    assert trace.saxo_event_count == 1
    assert trace.command_event_count == 1


def test_malformed_output_is_parse_error() -> None:
    # Given: non-JSONL process output.
    # When: both parsers read it.
    codex = parse_codex_model_output("not-json {")
    claude = parse_claude_model_output("not-json {")

    # Then: both fail closed without inventing tool events.
    assert codex.parse_error == "malformed_output"
    assert claude.parse_error == "malformed_output"
    assert codex.invoked_logical_tools == ()
    assert claude.invoked_logical_tools == ()


def test_non_router_commands_have_no_broad_grants(tmp_path: Path) -> None:
    # Given: a plugin root and exact Claude grants.
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers."cloud-run"]\nurl = "https://invalid.example"\n'
        '[mcp_servers.saxo_bank_mcp]\ncommand = "uv"\n',
        encoding="utf-8",
    )
    grants = (
        "mcp__saxo-bank-mcp__saxo_auth_status",
        "mcp__saxo-bank-mcp__saxo_health",
    )
    mcp_config = tmp_path / "claude-mcp.json"
    mcp_config.write_text("{}", encoding="utf-8")

    # When: both non-router commands are built.
    codex = codex_non_router_command("prompt", plugin_root=plugin, codex_home=codex_home)
    claude = claude_non_router_command(
        "prompt",
        mcp_config_path=mcp_config,
        resolved_grants=grants,
    )

    # Then: Codex is ephemeral/read-only with approval never and unrelated MCP disabled.
    assert "--ephemeral" in codex
    assert (codex[codex.index("--sandbox")], "read-only") == ("--sandbox", "read-only")
    assert 'approval_policy="never"' in codex
    assert 'cli_auth_credentials_store="file"' in codex
    assert 'mcp_oauth_credentials_store="file"' in codex
    assert not any(
        unsafe in argument.lower()
        for argument in codex
        for unsafe in ("keychain", "keyring", "security", "browser", "login")
    )
    assert (
        codex[codex.index("shell_tool") - 1],
        codex[codex.index("shell_tool")],
    ) == ("--disable", "shell_tool")
    assert "mcp_servers.cloud-run.enabled=false" in codex
    assert "mcp_servers.saxo_bank_mcp.enabled=false" not in codex
    assert "*" not in " ".join(codex)

    # Then: Claude denies built-ins, keeps strict mcp-config + exact grants.
    assert (
        claude[claude.index("--permission-mode")],
        claude[claude.index("--permission-mode") + 1],
    ) == ("--permission-mode", "bypassPermissions")
    assert "--strict-mcp-config" in claude
    assert "--no-chrome" in claude
    assert claude[claude.index("--mcp-config") + 1] == str(mcp_config)
    disallowed = claude[claude.index("--disallowedTools") + 1]
    assert "Bash" in disallowed
    assert "Read" in disallowed
    allowed = claude[claude.index("--allowedTools") + 1]
    assert allowed == ",".join(grants)
    assert "*" not in allowed
    assert "--output-format" in claude
    assert "stream-json" in claude
    assert "plan" not in claude
    assert not any(
        unsafe in argument.lower()
        for argument in claude
        for unsafe in ("keychain", "keyring", "security", "browser", "login")
    )
