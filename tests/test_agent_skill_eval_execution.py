from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from saxo_bank_mcp.agent_skill_eval_models import RouterDecision
from saxo_bank_mcp.agent_skill_router_eval_protocol import (
    claude_router_command,
    codex_router_command,
    configured_codex_mcp_server_names,
    parse_claude_router_output,
    parse_codex_router_output,
)

EXPECTED_TOOL_EVENTS: Final = 2


def test_codex_router_output_parses_structure_and_zero_tool_events() -> None:
    # Given: a Codex JSONL stream with one structured agent message.
    decision = _decision()
    stream = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "fixture"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "answer",
                        "type": "agent_message",
                        "text": decision.model_dump_json(),
                    },
                },
            ),
        ),
    )

    # When: the stream is parsed.
    parsed = parse_codex_router_output(stream)

    # Then: structure is retained and every model tool count is zero.
    assert parsed.decision == decision
    assert parsed.tool_event_count == 0
    assert parsed.command_event_count == 0
    assert parsed.mcp_event_count == 0
    assert parsed.saxo_event_count == 0


def test_claude_router_output_counts_command_mcp_and_saxo_tool_events() -> None:
    # Given: a Claude stream with one shell tool and one Saxo MCP tool.
    decision = _decision()
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "shell", "name": "Bash"},
                            {
                                "type": "tool_use",
                                "id": "saxo",
                                "name": "mcp__saxo_bank_mcp__saxo_health",
                            },
                        ],
                    },
                },
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "structured_output": decision.model_dump(mode="json"),
                },
            ),
        ),
    )

    # When: the stream is parsed.
    parsed = parse_claude_router_output(stream)

    # Then: unique model tool events are classified without reading prose.
    assert parsed.decision == decision
    assert parsed.tool_event_count == EXPECTED_TOOL_EVENTS
    assert parsed.command_event_count == 1
    assert parsed.mcp_event_count == 1
    assert parsed.saxo_event_count == 1


def test_claude_structured_output_is_not_an_execution_tool_event() -> None:
    # Given: Claude returns the requested schema through its protocol-only helper.
    decision = _decision()
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "structured",
                                "name": "StructuredOutput",
                            },
                        ],
                    },
                },
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "structured_output": decision.model_dump(mode="json"),
                },
            ),
        ),
    )

    # When: the stream is parsed.
    parsed = parse_claude_router_output(stream)

    # Then: the schema envelope is not classified as model execution.
    assert parsed.decision == decision
    assert parsed.tool_event_count == 0
    assert parsed.command_event_count == 0
    assert parsed.mcp_event_count == 0
    assert parsed.saxo_event_count == 0


def test_native_router_commands_disable_tools_and_mcp(tmp_path: Path) -> None:
    # Given: a schema path and a source-equivalent prompt.
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("{}", encoding="utf-8")
    prompt = "Classify only."

    # When: both native commands are built.
    codex = codex_router_command(
        prompt,
        schema_path,
        tmp_path,
        mcp_server_names=("cloud-run", "playwright"),
    )
    claude = claude_router_command(prompt, schema_path)

    # Then: Codex retains user config while disabling shell, apps, and web.
    assert codex[:2] == ("codex", "exec")
    assert "--ignore-user-config" not in codex
    assert "--ignore-rules" in codex
    assert _adjacent_pair(codex, "shell_tool") == ("--disable", "shell_tool")
    assert 'web_search="disabled"' in codex
    assert "mcp_servers.cloud-run.enabled=false" in codex
    assert "mcp_servers.playwright.enabled=false" in codex

    # Then: Claude exposes no built-in tools or MCP servers.
    assert claude[0] == "claude"
    assert _adjacent_pair(claude, "") == ("--tools", "")
    assert "--safe-mode" in claude
    assert "--strict-mcp-config" in claude
    assert '{"mcpServers":{}}' in claude


def test_codex_mcp_names_and_response_schema_are_strict(tmp_path: Path) -> None:
    # Given: a minimal retained Codex config and the router response model.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers."cloud-run"]\nurl = "https://invalid.example"\n'
        '[mcp_servers.playwright]\ncommand = "false"\n',
        encoding="utf-8",
    )
    schema = RouterDecision.model_json_schema()

    # When: configured server names and required response fields are inspected.
    names = configured_codex_mcp_server_names(config_path)
    properties = set(schema["properties"])
    required = set(schema["required"])

    # Then: every server can be disabled and every response field is required.
    assert names == ("cloud-run", "playwright")
    assert required == properties


def _decision() -> RouterDecision:
    return RouterDecision(
        environment="SIM",
        intent="read",
        mutation_risk="none",
        evidence_need="plan-only",
        primary_skill="saxo-reads",
        follow_on_skills=("saxo-trading",),
        requires_environment_clarification=False,
        approval_bypass_refused=False,
        trade_choice_refused=False,
        execution_allowed=False,
    )


def _adjacent_pair(command: tuple[str, ...], value: str) -> tuple[str, str]:
    index = command.index(value)
    return command[index - 1], command[index]
