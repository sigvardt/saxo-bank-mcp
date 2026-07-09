from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from saxo_bank_mcp import nontrade_policy, qa, qa_read_probes
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import save_token_cache

EXPECTED_OPERATION_COUNT = 294
EXPECTED_SERVICE_GROUPS = 17
TRADING_IMPLEMENTED_READS = 7
TRADING_OPERATION_COUNT = 45
TRADING_PAGE_LIMIT = 25
TRADING_REFUSED_OPERATIONS = 38


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_read_tools_are_registered_with_agent_safe_descriptions() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    descriptions = {tool.name: tool.description or "" for tool in tools}
    assert "saxo_list_registered_endpoints" in descriptions
    assert "saxo_call_registered_endpoint" in descriptions
    assert (
        "registered Saxo OpenAPI GET/read operations only"
        in descriptions["saxo_call_registered_endpoint"]
    )
    assert "explicitly enabled LIVE read mode" in descriptions["saxo_call_registered_endpoint"]
    assert "safe SIM GET read operations" not in descriptions["saxo_call_registered_endpoint"]
    assert (
        "denies unregistered, arbitrary-host, and write-class operations before any network call"
        in descriptions["saxo_call_registered_endpoint"]
    )


@pytest.mark.anyio
async def test_list_registered_endpoints_uses_registry_safe_status_and_counts() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_list_registered_endpoints",
            {"service_group": "Trading", "limit": TRADING_PAGE_LIMIT},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "metadata_only_not_ready_for_trading"
    assert payload["registry_load_state"] == "loaded"
    assert payload["inventory_validation_status"] == "self_consistent"
    assert payload["total_operation_count"] == EXPECTED_OPERATION_COUNT
    assert payload["matched_count"] == TRADING_OPERATION_COUNT
    assert payload["returned_count"] == TRADING_PAGE_LIMIT
    assert payload["next_offset"] == TRADING_PAGE_LIMIT
    assert payload["truncated"] is True
    assert payload["live_source_fetched"] is False
    assert payload["registry_only"] is True
    assert payload["execution_readiness_verified"] is False
    assert payload["live_access"] is False
    assert payload["account_access_verified"] is False
    assert payload["trading_ready"] is False
    assert payload["official_group_completeness_verified"] is False
    assert payload["official_group_source_url"] == (
        "https://www.developer.saxo/openapi/referencedocs"
    )
    assert payload["official_group_reconciliation"] == "not_run"
    assert payload["coverage_basis"] == "curated_snapshot_not_reconciled_with_live"
    assert payload["warning_notice"].startswith("Registry metadata only")
    assert isinstance(payload["snapshot_age_days"], int)
    assert isinstance(payload["snapshot_stale"], bool)
    assert payload["snapshot_completeness_scope"] == (
        "statically_checked_in_not_reconciled_with_live"
    )
    assert payload["unknown_service_group"] is False
    assert "snapshot_date" in payload
    assert "retrieved_at" not in payload
    for missing_check in (
        "Saxo connectivity",
        "credentials/session",
        "account access",
        "catalog completeness/freshness vs live Saxo",
    ):
        assert missing_check in payload["does_not_verify"]
    support = {item["service_group"]: item for item in payload["service_group_support"]}
    assert support["Trading"]["registered_read_definitions"] == TRADING_IMPLEMENTED_READS
    assert support["Trading"]["refused_operations"] == TRADING_REFUSED_OPERATIONS
    assert all(
        "implemented" not in key and "ready" not in key and "available" not in key
        for item in payload["service_group_support"]
        for key in item
    )
    assert all("status" not in operation for operation in payload["operations"])
    assert all("mcp_support_status" not in operation for operation in payload["operations"])
    assert all("mcp_support_policy" in operation for operation in payload["operations"])
    assert all(
        "available" not in str(operation["mcp_support_policy"])
        and "ready" not in str(operation["mcp_support_policy"])
        for operation in payload["operations"]
    )
    assert all(
        "todo" not in str(operation["refusal_reason"]) for operation in payload["operations"]
    )
    assert all(
        operation["live_reachability_verified"] is False for operation in payload["operations"]
    )


@pytest.mark.anyio
async def test_list_registered_endpoints_flags_unknown_service_group() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_list_registered_endpoints",
            {"service_group": "Not A Saxo Group", "limit": 5},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "metadata_only_not_ready_for_trading"
    assert payload["registry_load_state"] == "loaded"
    assert payload["unknown_service_group"] is True
    assert payload["service_group_error"] == "unknown_service_group"
    assert isinstance(payload["service_group_suggestions"], list)
    assert payload["matched_count"] == 0
    assert payload["returned_count"] == 0
    assert "Trading" in payload["valid_service_groups"]


@pytest.mark.anyio
async def test_registered_endpoint_denials_are_errors() -> None:
    operation = nontrade_policy.first_nontrade_write_operation("Asset Transfers")
    assert operation is not None

    async with Client(mcp) as client:
        write_result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": operation.method, "path": operation.path_template},
            raise_on_error=False,
        )
        get_result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/not-a-registered-saxo-path"},
            raise_on_error=False,
        )

    write_payload = write_result.structured_content
    get_payload = get_result.structured_content
    assert write_result.is_error is True
    assert write_payload is not None
    assert write_payload["status"] == "denied"
    assert write_payload["denied_class"] == "write"
    assert write_payload["network_call_made"] is False
    assert get_result.is_error is True
    assert get_payload is not None
    assert get_payload["status"] == "denied"
    assert get_payload["denial_reason"] == "unregistered_endpoint"


@pytest.mark.anyio
async def test_registered_endpoint_denials_raise_by_default() -> None:
    operation = nontrade_policy.first_nontrade_write_operation("Asset Transfers")
    assert operation is not None

    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "saxo_call_registered_endpoint",
                {"method": operation.method, "path": operation.path_template},
            )
        with pytest.raises(ToolError):
            await client.call_tool(
                "saxo_call_registered_endpoint",
                {"method": "GET", "path": "/not-a-registered-saxo-path"},
            )


def test_read_smoke_probe_records_every_group_or_reason(tmp_path: Path) -> None:
    out = tmp_path / "read-smoke.json"

    result = qa_read_probes.handle_read_smoke(out, "all")

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["groups"] == "all"
    assert report["fastmcp_tools"]["saxo_call_registered_endpoint"] is True
    assert len(report["per_group"]) == EXPECTED_SERVICE_GROUPS
    assert report["no_arbitrary_url_call"] is True
    assert report["live_write"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_list_registry_stdout_probe_calls_fastmcp_without_out(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = qa.main(
        ["list-registry-stdout", "--service-group", "Trading", "--limit", "1"],
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "metadata_only_not_ready_for_trading"
    assert payload["matched_count"] == TRADING_OPERATION_COUNT
    assert payload["returned_count"] == 1
    assert payload["registry_only"] is True


def test_registered_endpoint_denied_probe_uses_real_fastmcp_tool(tmp_path: Path) -> None:
    out = tmp_path / "denied.json"

    result = qa_read_probes.handle_registered_endpoint_denied(
        out,
        method="GET",
        path="/not-a-registered-saxo-path",
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "denied"
    assert report["tool_name"] == "saxo_call_registered_endpoint"
    assert report["network_call_made"] is False
    assert report["denial_reason"] == "unregistered_endpoint"
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


@pytest.mark.anyio
async def test_registered_endpoint_can_call_live_read_when_live_read_gates_are_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "live-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-access-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_CLIENT_ID", "live-client-id")
    monkeypatch.setenv("SAXO_MCP_LIVE_CLIENT_SECRET", "live-client-secret")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))

    def create_live_client(**_kwargs: object) -> _LiveClient:
        return _LiveClient()

    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", create_live_client)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/root/v1/diagnostics/get"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["environment"] == "LIVE"
    assert payload["live_access"] is True
    assert payload["network_call_made"] is True
    assert payload["live_write"] is False
    assert payload["response"] == '{"Status":"Ok"}'


@pytest.mark.anyio
async def test_registered_endpoint_live_read_uses_token_for_authenticated_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "live-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-access-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))

    def create_live_client(**_kwargs: object) -> _AuthenticatedLiveClient:
        return _AuthenticatedLiveClient()

    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", create_live_client)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/port/v1/accounts/me"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["environment"] == "LIVE"
    assert payload["auth_exercised"] is True
    assert payload["response"] == '{"Data":[]}'


class _LiveClient:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        assert path == "root/v1/diagnostics/get"
        assert params == {}
        assert headers == {"Accept": "application/json"}
        return httpx2.Response(
            200,
            text='{"Status":"Ok"}',
            request=httpx2.Request(
                "GET",
                "https://gateway.saxobank.com/openapi/root/v1/diagnostics/get",
            ),
        )


class _AuthenticatedLiveClient:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        assert path == "port/v1/accounts/me"
        assert params == {}
        assert headers == {
            "Accept": "application/json",
            "Authorization": "Bearer live-access-token",
        }
        return httpx2.Response(
            200,
            json={"Data": []},
            headers={"content-type": "application/json"},
            request=httpx2.Request(
                "GET",
                "https://gateway.saxobank.com/openapi/port/v1/accounts/me",
            ),
        )
