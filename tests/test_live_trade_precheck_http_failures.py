from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from live_precheck_test_support import (
    JSON_OBJECT_ADAPTER,
    LIVE_PRECHECK_TOOL,
    HttpFailureCase,
    configure_live,
    install_transport,
    order_payload,
    read_body,
)

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.server import mcp

HTTP_BAD_REQUEST = 400
_HTTP_FAILURE_PATHS = (
    ("/port/v1/accounts/me", "account_lookup"),
    ("/ref/v1/instruments/details/30031/Stock", "instrument_lookup"),
    ("/trade/v2/orders/precheck", "precheck"),
)
_VALID_ERROR_INFO = (
    b'{"ErrorCode":"InvalidModelState","Message":"caller account DISPLAY-1 rejected for '
    b'order 987654","ModelState":{"AccountKey":["AccountKey abc-secret must not be returned"]}}'
)
_INVALID_ERROR_INFO = (
    b"",
    b"not-json",
    b'{"ErrorCode":"Invalid Request","Message":"sensitive"}',
    b'{"ErrorCode":"InvalidRequest","Message":"sensitive","Unknown":"value"}',
)


async def _call_precheck(account_ref: str | None = None) -> dict[str, JsonValue]:
    async with Client(mcp) as client:
        result = await client.call_tool(
            LIVE_PRECHECK_TOOL,
            {"order": order_payload(account_ref)},
            raise_on_error=False,
        )
    assert result.is_error is True
    return JSON_OBJECT_ADAPTER.validate_python(result.structured_content)


@pytest.mark.anyio
async def test_refresh_network_call_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(tmp_path / "live-token.json"))

    async def rejected_refresh(
        _tool_name: str,
        _settings: SimAuthSettings,
    ) -> dict[str, JsonValue]:
        return {
            "status": "auth_required",
            "reason": "token_refresh_rejected",
            "network_call_made": True,
        }

    monkeypatch.setattr(
        "saxo_bank_mcp.mcp_live_trade_tools.live_token_for_tool",
        rejected_refresh,
    )
    payload = await _call_precheck("live-account-fixture")
    assert payload["network_call_made"] is True
    assert payload["order_placement_endpoint_called"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        HttpFailureCase(401, {}, "authentication_required", None, retry_known=False),
        HttpFailureCase(403, {}, "forbidden", None, retry_known=False),
        HttpFailureCase(
            429,
            {"X-RateLimit-Trade-Reset": "7"},
            "rate_limited",
            7.0,
            retry_known=True,
        ),
        HttpFailureCase(429, {}, "rate_limited", None, retry_known=False),
        HttpFailureCase(
            429,
            {"X-RateLimit-Trade-Reset": "Infinity"},
            "rate_limited",
            None,
            retry_known=False,
        ),
        HttpFailureCase(503, {}, "precheck_unavailable", None, retry_known=False),
    ],
)
async def test_precheck_classifies_http_failures_before_body_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: HttpFailureCase,
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json=read_body(request), request=request)
        return httpx2.Response(
            case.http_status,
            content=b"not-json",
            headers=case.headers,
            request=request,
        )

    install_transport(monkeypatch, handler)
    payload = await _call_precheck()
    assert payload["status"] == case.expected_status
    assert payload["retry_after_seconds"] == case.retry_seconds
    assert payload["retry_known"] is case.retry_known
    assert payload["live_write_called"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        ("/port/v1/accounts/me", "account_lookup_unavailable", "account_lookup"),
        (
            "/ref/v1/instruments/details/30031/Stock",
            "instrument_lookup_unavailable",
            "instrument_lookup",
        ),
        ("/trade/v2/orders/precheck", "precheck_unavailable", "precheck"),
    ],
)
async def test_server_errors_report_the_failed_precheck_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, str],
) -> None:
    configure_live(tmp_path, monkeypatch)
    failure_path_suffix, expected_status, expected_stage = case
    expected_instrument_called = expected_stage != "account_lookup"
    expected_precheck_called = expected_stage == "precheck"
    requested_paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith(failure_path_suffix):
            return httpx2.Response(503, json={"Message": "unavailable"}, request=request)
        if request.method == "GET":
            return httpx2.Response(200, json=read_body(request), request=request)
        return httpx2.Response(200, json={"PreCheckResult": "Ok"}, request=request)

    install_transport(monkeypatch, handler)
    payload = await _call_precheck()
    assert payload["status"] == expected_status
    assert payload["failure_stage"] == expected_stage
    assert payload["account_lookup_endpoint_called"] is True
    assert payload["instrument_lookup_endpoint_called"] is expected_instrument_called
    assert payload["precheck_endpoint_called"] is expected_precheck_called
    assert payload["instrument_tradable"] is expected_precheck_called
    assert payload["order_placement_endpoint_called"] is False
    assert not any(path.endswith("/trade/v2/orders") for path in requested_paths)


@pytest.mark.anyio
@pytest.mark.parametrize("failure_case", _HTTP_FAILURE_PATHS)
@pytest.mark.parametrize(
    ("error_body", "expected_error_code"),
    [(_VALID_ERROR_INFO, "InvalidModelState"), *[(body, None) for body in _INVALID_ERROR_INFO]],
)
async def test_http_400_exposes_only_validated_saxo_error_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_case: tuple[str, str],
    error_body: bytes,
    expected_error_code: str | None,
) -> None:
    configure_live(tmp_path, monkeypatch)
    failure_path_suffix, expected_stage = failure_case

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(failure_path_suffix):
            return httpx2.Response(HTTP_BAD_REQUEST, content=error_body, request=request)
        if request.method == "GET":
            return httpx2.Response(200, json=read_body(request), request=request)
        return httpx2.Response(200, json={"PreCheckResult": "Ok"}, request=request)

    install_transport(monkeypatch, handler)
    payload = await _call_precheck()
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert payload["status"] == "http_error"
    assert payload["failure_stage"] == expected_stage
    assert payload["http_status"] == HTTP_BAD_REQUEST
    assert payload["saxo_error_code"] == expected_error_code
    assert not any(
        marker in serialized_payload
        for marker in (
            "sensitive",
            "987654",
            "abc-secret",
            "DISPLAY-1",
            "FIXTURE_ACCOUNT_1",
            "30031",
        )
    )
    assert "Message" not in payload
    assert "ModelState" not in payload


@pytest.mark.anyio
async def test_network_error_after_instrument_validation_keeps_tradable_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)
    requested_paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_paths.append(request.url.path)
        if request.method == "GET":
            return httpx2.Response(200, json=read_body(request), request=request)
        raise httpx2.ConnectError("synthetic precheck connect failure", request=request)

    install_transport(monkeypatch, handler)
    payload = await _call_precheck()
    assert payload["status"] == "network_error"
    assert payload["failure_stage"] == "precheck"
    assert payload["instrument_tradable"] is True
    assert payload["precheck_endpoint_called"] is True
    assert payload["order_placement_endpoint_called"] is False
    assert not any(path.endswith("/trade/v2/orders") for path in requested_paths)


@pytest.mark.anyio
async def test_precheck_requires_http_200_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = read_body(request) if request.method == "GET" else {"PreCheckResult": "Ok"}
        status = 200 if request.method == "GET" else 201
        return httpx2.Response(status, json=body, request=request)

    install_transport(monkeypatch, handler)
    payload = await _call_precheck()
    assert payload["precheck_request_accepted"] is False

@pytest.mark.anyio
async def test_live_precheck_treats_parseable_server_error_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json=read_body(request), request=request)
        return httpx2.Response(
            503,
            json={"PreCheckResult": "Ok"},
            request=request,
        )

    install_transport(monkeypatch, handler)
    payload = await _call_precheck()
    assert payload["status"] == "precheck_unavailable"
    assert payload["precheck_request_accepted"] is False
