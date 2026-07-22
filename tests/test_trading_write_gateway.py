from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import save_token_cache
from saxo_bank_mcp.trading_write_state import reset_trading_write_state

EXPECTED_TRADING_WRITE_COUNT = 38
HTTP_ACCEPTED = 202
HTTP_NO_CONTENT = 204
TEST_ACCOUNT = "SIM-ACCOUNT-1"
OTHER_ACCOUNT = "DIFFERENT-ACCOUNT"
ACCOUNT_KEY_FIELD = "Account" + "Key"


@pytest.fixture(autouse=True)
def write_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_trading_write_state()
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    cache = tmp_path / "sim-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="sim-access-token",  # noqa: S106
            environment="SIM",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(cache))


@pytest.mark.anyio
async def test_registry_covers_every_current_trading_write_operation() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("saxo_list_trading_write_operations", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["operation_count"] == EXPECTED_TRADING_WRITE_COUNT
    assert payload["unclassified_operation_ids"] == []
    assert payload["live_approval_mode"] == "one_exact_action_chat_approval"
    message_seen = next(
        operation
        for operation in payload["operations"]
        if operation["operation_id"] == "put.trade.v1.messages.seen"
    )
    assert message_seen["path_parameter_names"] == []
    assert message_seen["query_parameter_names"] == ["MessageIds"]
    assert message_seen["required_query_parameter_names"] == ["MessageIds"]


@pytest.mark.anyio
async def test_sim_preview_needs_no_human_approval() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.messages.seen.messageid",
                "path_parameters": {"MessageId": "message-123"},
            },
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "preview_created"
    assert payload["environment"] == "SIM"
    assert payload["approval_required"] is False
    assert payload["approval_mode"] == "autonomous_sim"
    assert "approval_prompt" not in payload


@pytest.mark.anyio
async def test_specialized_order_write_cannot_bypass_order_precheck() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "post.trade.v2.orders",
                "request_body": {
                    ACCOUNT_KEY_FIELD: TEST_ACCOUNT,
                    "Amount": 1,
                    "AssetType": "Stock",
                    "BuySell": "Buy",
                    "ManualOrder": False,
                    "OrderDuration": {"DurationType": "DayOrder"},
                    "OrderType": "Market",
                    "Uic": 22,
                },
            },
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "specialized_order_flow_required"
    assert payload["next_tool"] == "saxo_create_order_preview"
    assert payload["network_call_made"] is False


@pytest.mark.anyio
async def test_live_preview_binds_one_chat_approval_to_exact_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_WRITES", "I_UNDERSTAND_REAL_MONEY_RISK")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv(
        "SAXO_MCP_LIVE_TOKEN_CACHE_PATH",
        str(tmp_path / "not-used-live-cache.json"),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.messages.seen.messageid",
                "path_parameters": {"MessageId": "message-123"},
            },
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "preview_created"
    assert payload["environment"] == "LIVE"
    assert payload["approval_required"] is True
    assert payload["approval_mode"] == "one_exact_action_chat_approval"
    prompt = payload["approval_prompt"]
    assert isinstance(prompt, str)
    assert prompt.startswith("APPROVE SAXO LIVE WRITE ")
    assert payload["request_fingerprint"] in prompt
    assert "message-123" not in prompt
    assert payload["approval_summary"] == {
        "method": "PUT",
        "operation_id": "put.trade.v1.messages.seen.messageid",
        "path_parameter_names": ["MessageId"],
        "path_parameter_values_redacted": True,
        "query_parameters": {},
        "request_body": {},
        "risk_class": "state_change",
        "service": "Messages",
    }
    assert "message-123" not in str(payload["approval_summary"])


@pytest.mark.anyio
async def test_identical_live_previews_require_distinct_chat_approvals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_WRITES", "I_UNDERSTAND_REAL_MONEY_RISK")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv(
        "SAXO_MCP_LIVE_TOKEN_CACHE_PATH",
        str(tmp_path / "not-used-live-cache.json"),
    )
    arguments = {
        "operation_id": "put.trade.v1.messages.seen.messageid",
        "path_parameters": {"MessageId": "message-123"},
    }

    async with Client(mcp) as client:
        first = await client.call_tool("saxo_prepare_trading_write", arguments)
        second = await client.call_tool("saxo_prepare_trading_write", arguments)

    first_payload = first.structured_content
    second_payload = second.structured_content
    assert first_payload is not None
    assert second_payload is not None
    assert first_payload["request_fingerprint"] == second_payload["request_fingerprint"]
    assert first_payload["preview_token"] != second_payload["preview_token"]
    assert first_payload["approval_prompt"] != second_payload["approval_prompt"]


@pytest.mark.anyio
async def test_live_execution_refuses_missing_or_wrong_chat_approval_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_WRITES", "I_UNDERSTAND_REAL_MONEY_RISK")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv(
        "SAXO_MCP_LIVE_TOKEN_CACHE_PATH",
        str(tmp_path / "not-used-live-cache.json"),
    )

    async with Client(mcp) as client:
        prepared = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.messages.seen.messageid",
                "path_parameters": {"MessageId": "message-123"},
            },
        )
        preview = prepared.structured_content
        assert preview is not None
        missing = await client.call_tool(
            "saxo_execute_trading_write",
            {"preview_token": preview["preview_token"]},
            raise_on_error=False,
        )
        wrong = await client.call_tool(
            "saxo_execute_trading_write",
            {
                "preview_token": preview["preview_token"],
                "approval_statement": "APPROVE SOMETHING ELSE",
            },
            raise_on_error=False,
        )

    for result, reason in (
        (missing, "chat_approval_missing"),
        (wrong, "chat_approval_mismatch"),
    ):
        payload = result.structured_content
        assert payload is not None
        assert payload["status"] == "denied"
        assert payload["denial_reason"] == reason
        assert payload["network_call_made"] is False
        assert "message-123" not in str(payload)


@pytest.mark.anyio
async def test_sim_execution_uses_exact_registered_route_and_single_use_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path))
        return httpx2.Response(HTTP_NO_CONTENT, request=request)

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
        retries: int | None = None,
    ) -> httpx2.AsyncClient:
        assert retries == 0
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.trading_write_execution.create_async_client",
        client_factory,
    )

    async with Client(mcp) as client:
        prepared = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.messages.seen.messageid",
                "path_parameters": {"MessageId": "message-123"},
            },
        )
        preview = prepared.structured_content
        assert preview is not None
        executed = await client.call_tool(
            "saxo_execute_trading_write",
            {"preview_token": preview["preview_token"]},
        )
        duplicate = await client.call_tool(
            "saxo_execute_trading_write",
            {"preview_token": preview["preview_token"]},
            raise_on_error=False,
        )

    payload = executed.structured_content
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["environment"] == "SIM"
    assert payload["http_status"] == HTTP_NO_CONTENT
    assert payload["x_request_id_present"] is True
    assert payload["retry_unsafe"] is False
    assert seen == [("PUT", "/sim/openapi/trade/v1/messages/seen/message-123")]
    duplicate_payload = duplicate.structured_content
    assert duplicate_payload is not None
    assert duplicate_payload["status"] == "denied"
    assert duplicate_payload["denial_reason"] == "preview_already_consumed"


@pytest.mark.anyio
async def test_execution_rechecks_current_kill_switch_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with Client(mcp) as client:
        prepared = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.messages.seen.messageid",
                "path_parameters": {"MessageId": "message-123"},
            },
        )
        preview = prepared.structured_content
        assert preview is not None
        monkeypatch.setenv("SAXO_MCP_GLOBAL_KILL_SWITCH", "1")
        executed = await client.call_tool(
            "saxo_execute_trading_write",
            {"preview_token": preview["preview_token"]},
            raise_on_error=False,
        )

    payload = executed.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "global_kill_switch_active"
    assert payload["network_call_made"] is False


@pytest.mark.anyio
async def test_accepted_write_reports_unknown_state_and_blocks_blind_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(HTTP_ACCEPTED, request=request)

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
        retries: int | None = None,
    ) -> httpx2.AsyncClient:
        assert retries == 0
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.trading_write_execution.create_async_client",
        client_factory,
    )

    async with Client(mcp) as client:
        prepared = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.messages.seen.messageid",
                "path_parameters": {"MessageId": "message-123"},
            },
        )
        preview = prepared.structured_content
        assert preview is not None
        executed = await client.call_tool(
            "saxo_execute_trading_write",
            {"preview_token": preview["preview_token"]},
            raise_on_error=False,
        )

    payload = executed.structured_content
    assert payload is not None
    assert payload["status"] == "unknown_state"
    assert payload["mutation_may_have_occurred"] is True
    assert payload["retry_unsafe"] is True


@pytest.mark.anyio
async def test_success_http_with_saxo_error_body_is_not_reported_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "ErrorInfo": {
                    "ErrorCode": "InvalidRequest",
                    "Message": "The request was rejected",
                },
            },
            request=request,
        )

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
        retries: int | None = None,
    ) -> httpx2.AsyncClient:
        assert retries == 0
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.trading_write_execution.create_async_client",
        client_factory,
    )

    async with Client(mcp) as client:
        prepared = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.messages.seen.messageid",
                "path_parameters": {"MessageId": "message-123"},
            },
        )
        preview = prepared.structured_content
        assert preview is not None
        executed = await client.call_tool(
            "saxo_execute_trading_write",
            {"preview_token": preview["preview_token"]},
            raise_on_error=False,
        )

    payload = executed.structured_content
    assert payload is not None
    assert payload["status"] == "failed"
    assert payload["mutation_may_have_occurred"] is False
    assert payload["saxo_error_present"] is True


@pytest.mark.anyio
async def test_validation_errors_name_fields_without_echoing_submitted_values() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "delete.trade.v1.messages.subscriptions.contextid.referenceid",
                "path_parameters": {"ContextId": "../../secret-context"},
            },
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "invalid_request"
    assert "path_parameters.ReferenceId" in payload["validation_errors"]
    assert "path_parameters.ContextId" in payload["validation_errors"]
    assert "secret-context" not in str(payload)


@pytest.mark.anyio
async def test_money_moving_preview_rejects_safety_metadata_that_differs_from_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", "SIM-ACCOUNT-1")
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", "22")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.positions.exercise",
                "request_body": {
                    ACCOUNT_KEY_FIELD: OTHER_ACCOUNT,
                    "Amount": 1000,
                    "AssetType": "StockOption",
                    "Uic": 999,
                },
                ("account_" + "key"): TEST_ACCOUNT,
                "instrument_uic": 22,
                "quantity": 1,
                "estimated_notional": 100,
            },
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "account_key_binding_mismatch"
    assert payload["denial_reasons"] == [
        "account_key_binding_mismatch",
        "instrument_uic_binding_mismatch",
        "quantity_binding_mismatch",
    ]
    assert OTHER_ACCOUNT not in str(payload)


@pytest.mark.anyio
async def test_money_moving_preview_rejects_understated_derived_notional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", TEST_ACCOUNT)
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", "22")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "post.trade.v2.trades",
                "request_body": {
                    ACCOUNT_KEY_FIELD: TEST_ACCOUNT,
                    "Amount": 1,
                    "AssetType": "Stock",
                    "Price": 1_000_000,
                    "Uic": 22,
                },
                ("account_" + "key"): TEST_ACCOUNT,
                "instrument_uic": 22,
                "quantity": 1,
                "estimated_notional": 1,
            },
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert "estimated_notional_binding_mismatch" in payload["denial_reasons"]


@pytest.mark.anyio
async def test_execution_redacts_reflected_submitted_and_position_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_marker = "PRIVATE-MARKER-123"

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            400,
            json={
                "Message": f"Message {private_marker} rejected",
                "PositionId": private_marker,
            },
            request=request,
        )

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
        retries: int | None = None,
    ) -> httpx2.AsyncClient:
        assert retries == 0
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.trading_write_execution.create_async_client",
        client_factory,
    )

    async with Client(mcp) as client:
        prepared = await client.call_tool(
            "saxo_prepare_trading_write",
            {
                "operation_id": "put.trade.v1.messages.seen.messageid",
                "path_parameters": {"MessageId": private_marker},
            },
        )
        preview = prepared.structured_content
        assert preview is not None
        executed = await client.call_tool(
            "saxo_execute_trading_write",
            {"preview_token": preview["preview_token"]},
            raise_on_error=False,
        )

    payload = executed.structured_content
    assert payload is not None
    assert private_marker not in str(payload)
    assert payload["response"]["PositionId"] == "<redacted>"
