from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime

import anyio
import httpx2
import pytest

from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.request_ledger import (
    RequestLedgerAlreadyActiveError,
    capture_request_ledger,
    safe_query_names,
)


@pytest.mark.anyio
async def test_capture_records_completed_gateway_and_oauth_requests_without_secrets() -> None:
    path_account = "account-secret-123"
    query_token = "query-token-secret"  # noqa: S105
    header_token = "header-token-secret"  # noqa: S105
    body_account = "body-account-secret"
    oauth_field = "refresh_token"
    oauth_secret = "mocked-refresh-token"  # noqa: S105
    statuses = iter((201, 401))

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(next(statuses))

    transport = httpx2.MockTransport(handler)

    with capture_request_ledger() as ledger:
        async with create_async_client(transport=transport) as client:
            await client.post(
                f"https://gateway.saxobank.com/openapi/port/v1/accounts/{path_account}/orders",
                params={"access_token": query_token},
                headers={"Authorization": f"Bearer {header_token}"},
                content=f'{{"AccountKey":"{body_account}"}}'.encode(),
            )
            await client.post(
                "https://live.logonvalidation.net/token",
                content=f"{oauth_field}={oauth_secret}".encode(),
            )

        copied_events = ledger.events
        copied_events.clear()

    events = ledger.events
    serialized = json.dumps([asdict(event) for event in events], sort_keys=True)

    event_summaries = [
        (event.phase, event.host_role, event.method, event.path, event.status) for event in events
    ]
    expected_events = [
        (
            "attempted",
            "gateway",
            "POST",
            "/openapi/port/v1/accounts/{redacted}/orders",
            None,
        ),
        (
            "completed",
            "gateway",
            "POST",
            "/openapi/port/v1/accounts/{redacted}/orders",
            201,
        ),
        ("attempted", "oauth", "POST", "/token", None),
        ("completed", "oauth", "POST", "/token", 401),
    ]
    assert event_summaries == expected_events
    assert set(asdict(events[0])) == {
        "timestamp",
        "phase",
        "host_role",
        "method",
        "path",
        "query_names",
        "query_present",
        "status",
    }
    assert events[0].query_present is True
    assert events[0].query_names == ("{redacted}",)
    assert events[2].query_present is False
    assert events[2].query_names == ()
    assert all(datetime.fromisoformat(event.timestamp) for event in events)
    assert len(ledger.events) == len(expected_events)
    assert "?" not in serialized
    for secret in (path_account, query_token, header_token, body_account, oauth_secret):
        assert secret not in serialized


def test_query_name_capture_uses_an_allowlist() -> None:
    marker = "account_identifier_embedded_in_key"
    request = httpx2.Request(
        "GET",
        "https://gateway.saxobank.com/openapi/port/v1/orders",
        params={marker: "hidden", "AccountKey": "hidden"},
    )

    assert safe_query_names(request) == ("AccountKey", "{redacted}")
    assert marker not in str(safe_query_names(request))


@pytest.mark.anyio
async def test_root_debug_keeps_project_logs_without_dependency_request_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bearer_marker = "bearer-value-must-not-reach-logs"
    query_marker = "query-value-must-not-reach-logs"
    project_marker = "project-runtime-log-remains-visible"

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200)

    caplog.set_level(logging.DEBUG)
    logging.getLogger("saxo_bank_mcp.runtime_probe").debug(project_marker)
    for logger_name in (
        "httpx2",
        "httpcore2.http2",
        "hpack.hpack",
        "h2.connection",
        "websockets.client",
        "websockets.server",
    ):
        logging.getLogger(logger_name).debug(
            "request headers=%r url=%s :path=%s",
            [(b"authorization", f"Bearer {bearer_marker}".encode())],
            f"https://gateway.saxobank.com/openapi/port/v1/orders?ClientKey={query_marker}",
            f"/openapi/port/v1/orders?ClientKey={query_marker}",
        )
    async with create_async_client(transport=httpx2.MockTransport(handler)) as client:
        await client.get(
            "https://gateway.saxobank.com/openapi/port/v1/orders",
            params={"ClientKey": query_marker},
            headers={"Authorization": f"Bearer {bearer_marker}"},
        )

    assert project_marker in caplog.text
    assert bearer_marker not in caplog.text
    assert query_marker not in caplog.text
    assert "https://gateway.saxobank.com/openapi/port/v1/orders" not in caplog.text
    assert "HTTP Request:" not in caplog.text


@pytest.mark.anyio
async def test_capture_records_attempt_only_when_transport_raises() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("network unavailable", request=request)

    with capture_request_ledger() as ledger:
        async with create_async_client(transport=httpx2.MockTransport(handler)) as client:
            with pytest.raises(httpx2.ConnectError, match="network unavailable"):
                await client.get(
                    "https://gateway.saxobank.com/openapi/root/v1/sessions/capabilities"
                )

    assert [
        (event.phase, event.host_role, event.path, event.status) for event in ledger.events
    ] == [("attempted", "gateway", "/openapi/root/v1/sessions/capabilities", None)]


@pytest.mark.anyio
async def test_capture_records_unknown_host_without_exposing_host_or_path() -> None:
    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200)

    with capture_request_ledger() as ledger:
        async with create_async_client(transport=httpx2.MockTransport(handler)) as client:
            await client.get("https://private.example.test/customer/secret-value?token=hidden")

    assert [
        (event.phase, event.host_role, event.path, event.status) for event in ledger.events
    ] == [
        ("attempted", "other", "/{redacted}/{redacted}", None),
        ("completed", "other", "/{redacted}/{redacted}", 200),
    ]


@pytest.mark.anyio
async def test_capture_includes_task_created_before_ledger_activation() -> None:
    ready = anyio.Event()
    release = anyio.Event()
    completed = anyio.Event()

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200)

    async def background_request() -> None:
        ready.set()
        await release.wait()
        async with create_async_client(transport=httpx2.MockTransport(handler)) as client:
            await client.get("https://gateway.saxobank.com/openapi/port/v1/balances/me")
        completed.set()

    captured_phases: list[str] = []
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(background_request)
        await ready.wait()
        with capture_request_ledger() as ledger:
            release.set()
            await completed.wait()
        captured_phases = [event.phase for event in ledger.events]

    assert captured_phases == ["attempted", "completed"]


def test_capture_refuses_nested_ledgers() -> None:
    with (
        capture_request_ledger(),
        pytest.raises(RequestLedgerAlreadyActiveError),
        capture_request_ledger(),
    ):
        pytest.fail("nested ledger started")


@pytest.mark.anyio
async def test_capture_refuses_concurrent_ledger_from_another_task() -> None:
    release = anyio.Event()

    async def start_competing_ledger() -> None:
        await release.wait()
        with pytest.raises(RequestLedgerAlreadyActiveError), capture_request_ledger():
            pytest.fail("concurrent ledger started")

    with capture_request_ledger():
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(start_competing_ledger)
            release.set()


def _replace_transport_with_retry_spy(
    monkeypatch: pytest.MonkeyPatch,
    observed_retries: list[int],
) -> None:
    mock_transport = httpx2.MockTransport

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200)

    def transport_factory(
        *,
        http2: bool,
        retries: int,
        limits: httpx2.Limits,
        socket_options: list[tuple[int, int, int]],
    ) -> httpx2.AsyncBaseTransport:
        del limits
        assert http2 is True
        assert socket_options
        observed_retries.append(retries)
        return mock_transport(handler)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport_factory)


@pytest.mark.anyio
async def test_client_uses_three_retries_without_active_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_retries: list[int] = []
    _replace_transport_with_retry_spy(monkeypatch, observed_retries)

    async with create_async_client():
        pass

    assert observed_retries == [3]


@pytest.mark.anyio
async def test_client_disables_retries_with_active_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_retries: list[int] = []
    _replace_transport_with_retry_spy(monkeypatch, observed_retries)

    with capture_request_ledger():
        async with create_async_client():
            pass

    assert observed_retries == [0]
