from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from saxo_bank_mcp import qa
from saxo_bank_mcp.streaming import (
    SIM_STREAMING_ENDPOINT,
    STREAMING_LIMITS,
    build_streaming_connect_url,
    make_saxo_binary_message,
    parse_streaming_frame,
    register_local_subscription,
    reset_local_subscriptions,
    token_in_streaming_query_url,
    validate_context_id,
    validate_reference_id,
)

EXPECTED_CONNECTIONS = 4
EXPECTED_PRICE_INSTRUMENTS = 200
PRICE_MESSAGE_ID = 100
HEARTBEAT_MESSAGE_ID = 101
EXPECTED_MESSAGE_COUNT = 2
ACTUAL_STREAMING_CONNECT_SOURCE = "actual_streaming_connect_url"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_context_and_reference_ids_follow_saxo_constraints() -> None:
    assert validate_context_id("ctx-ABC_123") == "ctx-ABC_123"
    assert validate_reference_id("price-ref_1") == "price-ref_1"

    with pytest.raises(ValueError, match="context_id_invalid"):
        validate_context_id("bad context")
    with pytest.raises(ValueError, match="context_id_too_long"):
        validate_context_id("x" * 51)
    with pytest.raises(ValueError, match="reference_id_reserved_control_prefix"):
        validate_reference_id("_heartbeat")


def test_streaming_connect_url_keeps_token_out_of_query() -> None:
    url = build_streaming_connect_url("ctx1", last_message_id=42)

    assert SIM_STREAMING_ENDPOINT in url
    assert "contextId=ctx1" in url
    assert "messageid=42" in url
    assert "authorization" not in url.lower()
    assert "token" not in url.lower()
    assert token_in_streaming_query_url(url) is False
    assert token_in_streaming_query_url(f"{url}&access_token=abc") is True


def test_binary_streaming_frame_parser_handles_data_and_control_messages() -> None:
    frame = (
        make_saxo_binary_message(PRICE_MESSAGE_ID, "price-ref", {"Quote": {"Bid": 10.0}})
        + make_saxo_binary_message(
            HEARTBEAT_MESSAGE_ID,
            "_heartbeat",
            {"OriginatingReferenceId": "price-ref"},
        )
    )

    parsed = parse_streaming_frame(frame)

    assert parsed.last_message_id == HEARTBEAT_MESSAGE_ID
    assert len(parsed.messages) == EXPECTED_MESSAGE_COUNT
    data, heartbeat = parsed.messages
    assert data.message_id == PRICE_MESSAGE_ID
    assert data.reference_id == "price-ref"
    assert data.control_message is False
    assert data.payload_json == {"Quote": {"Bid": 10.0}}
    assert heartbeat.reference_id == "_heartbeat"
    assert heartbeat.control_message is True
    assert parsed.control_references == ("_heartbeat",)


def test_streaming_limits_are_pinned_to_current_official_evidence() -> None:
    assert STREAMING_LIMITS.expected_connections == EXPECTED_CONNECTIONS
    assert STREAMING_LIMITS.expected_price_instruments == EXPECTED_PRICE_INSTRUMENTS
    assert "openapi.help.saxo" in " ".join(STREAMING_LIMITS.sources)


@pytest.mark.anyio
async def test_streaming_tools_return_auth_required_without_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    module = __import__("saxo_bank_mcp.server", fromlist=["mcp"])

    async with Client(module.mcp) as client:
        tools = {tool.name for tool in await client.list_tools()}
        result = await client.call_tool(
            "saxo_create_streaming_price_subscription",
            {
                "context_id": "ctx1",
                "reference_id": "prices1",
                "uics": [21],
                "asset_type": "Stock",
            },
            raise_on_error=False,
        )

    assert "saxo_create_streaming_price_subscription" in tools
    assert "saxo_cleanup_streaming_subscriptions" in tools
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["fastmcp_called"] is True
    assert payload["streaming_completion_claim_allowed"] is False
    assert payload["network_call_made"] is False
    assert payload["token_in_query_url"] is False
    assert payload["token_query_url_check_source"] == ACTUAL_STREAMING_CONNECT_SOURCE


@pytest.mark.anyio
async def test_cleanup_removes_local_leak_without_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    reset_local_subscriptions()
    register_local_subscription(
        context_id="ctx-leak",
        reference_id="prices1",
        operation_id="post.trade.v1.prices.subscriptions",
        endpoint_path="/trade/v1/prices/subscriptions",
    )
    module = __import__("saxo_bank_mcp.server", fromlist=["mcp"])

    async with Client(module.mcp) as client:
        result = await client.call_tool(
            "saxo_cleanup_streaming_subscriptions",
            {"context_id": "ctx-leak"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["local_registry_before_count"] == 1
    assert payload["local_registry_after_count"] == 0
    assert payload["open_subscription_left"] is False
    assert payload["cleanup_attempted"] is False
    assert payload["network_call_made"] is False


def test_stream_qa_records_incomplete_auth_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    out = tmp_path / "stream.json"

    result = qa.main(
        [
            "stream",
            "--require-frame",
            "--expect-connections",
            str(EXPECTED_CONNECTIONS),
            "--expect-price-instruments",
            str(EXPECTED_PRICE_INSTRUMENTS),
            "--out",
            str(out),
        ],
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "incomplete_auth_required"
    assert report["command"] == "stream"
    assert report["fastmcp_called"] is True
    assert report["official_docs_checked"] is True
    assert report["streaming_endpoint"] == SIM_STREAMING_ENDPOINT
    assert report["limit_expected_connections"] == EXPECTED_CONNECTIONS
    assert report["limit_expected_price_instruments"] == EXPECTED_PRICE_INSTRUMENTS
    assert report["streaming_completion_claim_allowed"] is False
    assert report["stream_live_verified"] is False
    assert report["qa_status_is_authoritative"] is True
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_stream_cleanup_qa_removes_simulated_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    out = tmp_path / "cleanup.json"

    result = qa.main(["stream-cleanup", "--simulate-leak", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "incomplete_auth_required"
    assert report["command"] == "stream-cleanup"
    assert report["simulate_leak"] is True
    assert report["malformed_context_denied"] is True
    assert report["malformed_reference_denied"] is True
    assert report["local_registry_before_count"] > 0
    assert report["local_registry_after_count"] == 0
    assert report["open_subscription_left"] is False
    assert report["cleanup_remote_verified"] is False
    assert report["qa_status_is_authoritative"] is True
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
