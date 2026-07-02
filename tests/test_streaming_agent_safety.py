from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from fastmcp import Client

from saxo_bank_mcp import qa
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.server import mcp

type JsonObject = dict[str, JsonValue]
TK: Final = "tok-a"
RF: Final = "tok-r"
OFFICIAL_CONNECTIONS: Final = 4
MISMATCHED_CONNECTIONS: Final = 5


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def token_set() -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token=TK,
        refresh_token=RF,
        code_verifier="verifier-value",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_stream_cleanup_qa_reports_remote_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    token = token_set()

    def fake_cached_token(_tool_name: str) -> SaxoTokenSet | JsonObject:
        return token

    async def fake_delete_root_subscription(
        _token: SaxoTokenSet,
        _context_id: str,
    ) -> JsonObject:
        return {
            "network_call_made": True,
            "cleanup_status": "http_error",
            "cleanup_http_status": 500,
            "response": "Internal Server Error",
        }

    monkeypatch.setattr("saxo_bank_mcp.streaming_execution.cached_token", fake_cached_token)
    monkeypatch.setattr(
        "saxo_bank_mcp.streaming_execution.delete_root_subscription",
        fake_delete_root_subscription,
    )
    out = tmp_path / "cleanup_failed.json"

    result = qa.main(["stream-cleanup", "--simulate-leak", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "cleanup_remote_failed"
    assert report["tool_result"]["status"] == "cleanup_remote_failed"
    assert report["cleanup_attempted"] is True
    assert report["remote_cleanup_confirmed"] is False
    assert report["remote_subscription_may_remain"] is True
    assert report["open_subscription_left"] is False
    assert report["open_subscription_left_scope"] == "local_registry_only"


def test_stream_qa_fails_when_requested_limits_do_not_match_official(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    out = tmp_path / "stream_mismatch.json"

    result = qa.main(
        [
            "stream",
            "--require-frame",
            "--expect-connections",
            str(MISMATCHED_CONNECTIONS),
            "--expect-price-instruments",
            "200",
            "--out",
            str(out),
        ],
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["limit_expected_connections"] == OFFICIAL_CONNECTIONS
    assert report["requested_expect_connections"] == MISMATCHED_CONNECTIONS
    assert report["expect_connections_match"] is False
    assert report["limits_match_official"] is False


@pytest.mark.anyio
async def test_streaming_tools_deny_live_environment_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    async with Client(mcp) as client:
        stream_result = await client.call_tool(
            "saxo_create_streaming_price_subscription",
            {
                "context_id": "ctx1",
                "reference_id": "prices1",
                "uics": [21],
                "asset_type": "Stock",
            },
            raise_on_error=False,
        )
        cleanup_result = await client.call_tool(
            "saxo_cleanup_streaming_subscriptions",
            {"context_id": "ctx1"},
            raise_on_error=False,
        )

    stream_payload = stream_result.structured_content
    cleanup_payload = cleanup_result.structured_content
    assert stream_payload is not None
    assert cleanup_payload is not None
    assert stream_payload["status"] == "denied"
    assert stream_payload["denial_reason"] == "streaming_sim_only"
    assert stream_payload["environment"] == "LIVE"
    assert stream_payload["network_call_made"] is False
    assert stream_payload["context_id_validated"] is False
    assert cleanup_payload["status"] == "denied"
    assert cleanup_payload["denial_reason"] == "streaming_sim_only"
    assert cleanup_payload["environment"] == "LIVE"
    assert cleanup_payload["network_call_made"] is False


@pytest.mark.anyio
async def test_streaming_identifier_denials_report_failed_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    async with Client(mcp) as client:
        context_result = await client.call_tool(
            "saxo_create_streaming_price_subscription",
            {
                "context_id": "bad context",
                "reference_id": "prices1",
                "uics": [21],
                "asset_type": "Stock",
            },
            raise_on_error=False,
        )
        reference_result = await client.call_tool(
            "saxo_create_streaming_price_subscription",
            {
                "context_id": "ctx1",
                "reference_id": "_heartbeat",
                "uics": [21],
                "asset_type": "Stock",
            },
            raise_on_error=False,
        )

    context_payload = context_result.structured_content
    reference_payload = reference_result.structured_content
    assert context_payload is not None
    assert reference_payload is not None
    assert context_payload["status"] == "denied"
    assert context_payload["context_id_validated"] is False
    assert context_payload["reference_id_validated"] is False
    assert reference_payload["status"] == "denied"
    assert reference_payload["context_id_validated"] is True
    assert reference_payload["reference_id_validated"] is False


@pytest.mark.anyio
async def test_cleanup_without_local_record_reports_remote_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_cleanup_streaming_subscriptions",
            {"context_id": "ctx-empty"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["local_registry_before_count"] == 0
    assert payload["local_registry_after_count"] == 0
    assert payload["remote_cleanup_confirmed"] is False
    assert payload["remote_cleanup_status_known"] is False
    assert payload["remote_subscription_may_remain"] is True
