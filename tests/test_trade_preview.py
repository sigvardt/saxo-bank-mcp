from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp import mcp_trade_tools as trade_tools
from saxo_bank_mcp import qa
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SaxoEnvironment
from saxo_bank_mcp.safety import reset_safety_state
from saxo_bank_mcp.server import mcp

FIXTURE_ACCOUNT = "SIM-ACCOUNT-1"
FIXTURE_INSTRUMENT = 21
JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def order_body() -> dict[str, JsonValue]:
    return {
        "AccountKey": FIXTURE_ACCOUNT,
        "Uic": FIXTURE_INSTRUMENT,
        "AssetType": "Stock",
        "Amount": 10,
        "BuySell": "Buy",
        "OrderType": "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        "ContractMultiplier": 1,
    }


def precheck_response() -> dict[str, JsonValue]:
    return {
        "PreCheckResult": "Ok",
        "EstimatedCashRequired": 500,
        "EstimatedCashRequiredCurrency": "USD",
        "EstimatedTotalCostInAccountCurrency": 500,
        "InstrumentToAccountConversionRate": 1,
        "CostInAccountCurrency": {"Amount": 500},
        "MarginImpactBuySell": {"MarginImpact": 20},
    }


@pytest.fixture(autouse=True)
def safety_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_safety_state()
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", FIXTURE_ACCOUNT)
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", str(FIXTURE_INSTRUMENT))
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))


@pytest.mark.anyio
async def test_order_preview_creates_token_from_known_precheck_fixture() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_order_preview",
            {
                "order_body": order_body(),
                "precheck_response": precheck_response(),
                "disclaimer_response_state": "none",
            },
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert payload["status"] == "preview_created"
    assert payload["tool_name"] == "saxo_create_order_preview"
    assert payload["precheck_endpoint"] == "/trade/v2/orders/precheck"
    assert payload["preview_created"] is True
    assert payload["account_key_redacted"] is True
    assert payload["network_call_made"] is False
    assert payload["order_placed"] is False
    assert payload["order_modified"] is False
    assert payload["order_cancelled"] is False


@pytest.mark.anyio
async def test_live_order_preview_uses_real_precheck_and_returns_one_chat_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_WRITES", "I_UNDERSTAND_REAL_MONEY_RISK")
    sent: list[dict[str, JsonValue]] = []

    def access(
        _tool_name: str,
        environment: SaxoEnvironment,
    ) -> trade_tools.TradePrecheckAccess:
        return trade_tools.TradePrecheckAccess(
            environment=environment,
            rest_base_url="https://gateway.saxobank.com/openapi/",
            token=SaxoTokenSet(
                access_token="live-access-token",  # noqa: S106
                environment="LIVE",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        )

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(JSON_OBJECT_ADAPTER.validate_json(request.content))
        return httpx2.Response(200, json=precheck_response(), request=request)

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
        retries: int | None = None,
    ) -> httpx2.AsyncClient:
        del retries
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr(trade_tools, "_precheck_access", access)
    monkeypatch.setattr(trade_tools, "create_async_client", client_factory)
    intended_order = {**order_body(), "ManualOrder": True}

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_order_preview",
            {
                "order_body": intended_order,
                "disclaimer_response_state": "none",
            },
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert payload["status"] == "preview_created"
    assert payload["environment"] == "LIVE"
    assert payload["approval_factor_mode"] == "one_exact_action_chat_approval"
    assert str(payload["request_fingerprint"]) in str(payload["approval_prompt"])
    summary = JSON_OBJECT_ADAPTER.validate_python(payload["approval_summary"])
    summary_body = JSON_OBJECT_ADAPTER.validate_python(summary["request_body"])
    assert summary["operation_id"] == "post.trade.v2.orders"
    assert summary["instrument_uic"] == intended_order["Uic"]
    assert summary["quantity"] == intended_order["Amount"]
    assert summary_body["AccountKey"] == "<redacted>"
    assert summary_body["BuySell"] == intended_order["BuySell"]
    assert payload["network_call_made"] is True
    assert payload["order_placed"] is False
    assert sent[0]["ManualOrder"] is False
    assert intended_order["ManualOrder"] is True


@pytest.mark.anyio
async def test_order_preview_normalizes_disclaimer_state_when_none_present() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_order_preview",
            {
                "order_body": order_body(),
                "precheck_response": precheck_response(),
                "disclaimer_response_state": "unknown",  # default is unknown
            },
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert payload["status"] == "preview_created"
    assert payload["disclaimer_response_state"] == "none"  # normalized to none on success


@pytest.mark.anyio
async def test_order_preview_refuses_unknown_risk_fields() -> None:
    incomplete = {
        key: value
        for key, value in precheck_response().items()
        if key not in {"InstrumentToAccountConversionRate", "MarginImpactBuySell"}
    }

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_order_preview",
            {"order_body": order_body(), "precheck_response": incomplete},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    reasons = string_list(payload["denial_reasons"])
    assert result.is_error is True
    assert payload["status"] == "denied"
    assert "account_currency_conversion_unknown" in reasons
    assert "margin_impact_unknown" in reasons
    assert payload["preview_created"] is False
    assert payload["order_placed"] is False


@pytest.mark.anyio
async def test_order_preview_fails_on_non_ok_or_missing_precheck_result() -> None:
    # 1. Non-OK PreCheckResult ("Rejected")
    rejected_precheck = {
        **precheck_response(),
        "PreCheckResult": "Rejected",
    }
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_order_preview",
            {
                "order_body": order_body(),
                "precheck_response": rejected_precheck,
                "disclaimer_response_state": "none",
            },
            raise_on_error=False,
        )
    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "denied"
    assert payload["preview_created"] is False
    assert "precheck_not_ok" in string_list(payload["denial_reasons"])

    # 2. Missing PreCheckResult
    missing_precheck = {
        key: val for key, val in precheck_response().items() if key != "PreCheckResult"
    }
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_order_preview",
            {
                "order_body": order_body(),
                "precheck_response": missing_precheck,
                "disclaimer_response_state": "none",
            },
            raise_on_error=False,
        )
    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "denied"
    assert payload["preview_created"] is False
    assert "precheck_result_unknown" in string_list(payload["denial_reasons"])


@pytest.mark.anyio
async def test_order_preview_blocks_required_disclaimer() -> None:
    precheck = {
        **precheck_response(),
        "PreTradeDisclaimers": {
            "DisclaimerContext": "fixture-context",
            "DisclaimerTokens": ["fixture-token"],
        },
    }
    details = {
        "Data": [
            {
                "Body": "Trading this instrument requires exchange rules acceptance.",
                "Conditions": [{"Type": "Checkbox", "Label": "I understand"}],
                "DisclaimerToken": "fixture-token",
                "IsBlocking": True,
                "ResponseOptions": [{"ResponseType": "Accepted", "Label": "I accept"}],
            },
        ],
    }

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_order_preview",
            {
                "order_body": order_body(),
                "precheck_response": precheck,
                "disclaimer_details": details,
                "disclaimer_response_state": "missing",
            },
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    reasons = string_list(payload["denial_reasons"])
    assert result.is_error is True
    assert payload["status"] == "denied"
    assert "blocking_disclaimer" in reasons
    assert "disclaimer_response_required" in reasons
    assert payload["response_endpoint_path"] == "/dm/v2/disclaimers"
    assert payload["exact_disclaimer_content_present"] is True
    assert payload["disclaimer_details_sanitized"] is True
    assert payload["disclaimer_response_submitted"] is False


@pytest.mark.anyio
async def test_order_preview_without_fixture_requires_auth_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("SAXO_MCP_SIM_CLIENT_ID", raising=False)
    monkeypatch.delenv("SAXO_MCP_SIM_APP_KEY", raising=False)
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "empty-token-cache.json"))
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_order_preview",
            {"order_body": order_body()},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "auth_required"
    assert payload["network_call_made"] is False
    assert payload["order_placed"] is False


@pytest.mark.anyio
async def test_sim_only_trade_read_helpers_refuse_in_live_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")

    async with Client(mcp) as client:
        defaults = await client.call_tool(
            "saxo_get_multileg_order_defaults",
            {
                "account_key": FIXTURE_ACCOUNT,
                "option_root_id": 123,
                "options_strategy_type": "Straddle",
            },
            raise_on_error=False,
        )
        disclaimers = await client.call_tool(
            "saxo_get_required_disclaimers",
            {"disclaimer_tokens": ["fixture-token"]},
            raise_on_error=False,
        )

    for result in (defaults, disclaimers):
        payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
        assert result.is_error is True
        assert payload["status"] == "denied"
        assert payload["reason"] == "sim_only_tool_in_live_environment"
        assert payload["network_call_made"] is False
        assert payload["live_write"] is False
        assert payload["order_placed"] is False


@pytest.mark.anyio
async def test_live_disclaimer_response_returns_exact_chat_approval_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_WRITES", "I_UNDERSTAND_REAL_MONEY_RISK")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_register_disclaimer_response",
            {
                "disclaimer_context": "sensitive-context",
                "disclaimer_token": "sensitive-token",
                "response_type": "Accepted",
            },
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert payload["status"] == "preview_created"
    assert payload["tool_name"] == "saxo_register_disclaimer_response"
    assert payload["environment"] == "LIVE"
    assert payload["approval_mode"] == "one_exact_action_chat_approval"
    assert payload["approval_required"] is True
    assert payload["network_call_made"] is False
    assert payload["disclaimer_response_submitted"] is False
    assert payload["execution_tool"] == "saxo_execute_trading_write"
    assert str(payload["request_fingerprint"]) in str(payload["approval_prompt"])
    assert "sensitive-context" not in str(payload)
    assert "sensitive-token" not in str(payload)


def test_trade_precheck_qa_probe(tmp_path: Path) -> None:
    out = tmp_path / "precheck.json"

    result = qa.main(["trade-precheck", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["tool_name"] == "saxo_create_order_preview"
    assert report["preview_created"] is True
    assert report["order_placed"] is False
    assert report["secret_scan"]["findings"] == []


def test_trade_disclaimer_blocked_qa_probe(tmp_path: Path) -> None:
    out = tmp_path / "disclaimer.json"

    result = qa.main(["trade-disclaimer-blocked", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "denied"
    assert report["mcp_is_error"] is True
    assert "blocking_disclaimer" in report["denial_reasons"]
    assert report["network_call_made"] is False
    assert report["preview_created"] is False
    assert report["secret_scan"]["findings"] == []


def string_list(value: JsonValue) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []
