from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp.client.client import CallToolResult
from pydantic import ValidationError
from test_live_precheck_proof_support import (
    JSON_OBJECT_ADAPTER,
    JSON_ROWS_ADAPTER,
    MULTIPLE_OAUTH_POSTS,
    TransportScenario,
    run_proof,
)

from saxo_bank_mcp.live_precheck_ledger_models import SafeLedgerReport
from saxo_bank_mcp.live_precheck_proof import accepted_exposed_ledger
from saxo_bank_mcp.live_precheck_proof_audit import ledger_allows_proof
from saxo_bank_mcp.request_ledger import RequestLedgerEvent
from saxo_bank_mcp.strict_json import StrictJsonError
from saxo_bank_mcp.transport_boundary import (
    TransportBoundaryEvent,
    parse_transport_boundary_events,
)


def test_error_flagged_exposed_ledger_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, report, _ = run_proof(tmp_path, monkeypatch)
    structured = JSON_OBJECT_ADAPTER.validate_python(report["mcp_request_ledger"])
    result = CallToolResult(
        content=[],
        structured_content=structured,
        meta=None,
        is_error=True,
    )

    accepted = accepted_exposed_ledger(result)

    assert accepted is None


def test_safe_ledger_models_reject_coercible_scalars() -> None:
    with pytest.raises(ValidationError):
        SafeLedgerReport.model_validate(
            {
                "status": "passed",
                "scope": "current_mcp_session",
                "safe_fields_only": True,
                "ledger_complete": "true",
                "events_evicted": 0,
                "negative_proof_available": True,
                "only_precheck_gateway_non_get": True,
                "unsafe_gateway_request_detected": False,
                "order_placement_endpoint_called": False,
                "events": [],
            },
        )


def test_transport_boundary_model_rejects_coercible_scalars() -> None:
    with pytest.raises(ValidationError):
        TransportBoundaryEvent.model_validate(
            {
                "timestamp": "2026-07-20T10:00:00Z",
                "phase": "completed",
                "host_role": "gateway",
                "method": "GET",
                "path": "/openapi/port/v1/orders",
                "query_names": [],
                "query_present": False,
                "status": "200",
            },
        )


def test_transport_boundary_parser_rejects_duplicate_members() -> None:
    raw = (
        b'[{"timestamp":"first","timestamp":"second","phase":"completed",'
        b'"host_role":"gateway","method":"GET","path":"/openapi/port/v1/orders",'
        b'"query_names":[],"query_present":false,"status":200}]'
    )

    with pytest.raises(StrictJsonError):
        parse_transport_boundary_events(raw)


def test_extra_gateway_post_aborts_completed_mcp_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, requests = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(inject_extra_post=True),
    )

    assert result == 1
    assert report["status"] == "aborted"
    assert ("POST", "/openapi/trade/v2/orders") in requests
    assert requests.count(("POST", "/openapi/trade/v2/orders/precheck")) == 1


def test_unknown_host_request_aborts_completed_mcp_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, requests = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(inject_other_host_get=True),
    )

    assert result == 1
    assert report["status"] == "aborted"
    assert ("GET", "/health") in requests
    ledger = JSON_ROWS_ADAPTER.validate_python(report["request_ledger"])
    assert any(event["host_role"] == "other" for event in ledger)


def test_multiple_oauth_refresh_posts_abort_completed_mcp_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, requests = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(inject_oauth_posts=MULTIPLE_OAUTH_POSTS),
    )

    assert result == 1
    assert report["status"] == "aborted"
    assert requests.count(("POST", "/token")) == MULTIPLE_OAUTH_POSTS


def test_query_presence_rejects_otherwise_allowlisted_request_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, requests = run_proof(tmp_path, monkeypatch)
    events = ledger_events_for_requests(requests)

    assert ledger_allows_proof(events) is True
    events[0] = replace(events[0], query_present=True)
    assert ledger_allows_proof(events) is False


def ledger_events_for_requests(
    requests: list[tuple[str, str]],
) -> list[RequestLedgerEvent]:
    events: list[RequestLedgerEvent] = []
    for method, path in requests:
        safe_path = (
            "/openapi/ref/v1/instruments/details/{redacted}/{redacted}"
            if path.startswith("/openapi/ref/v1/instruments/details/")
            else path
        )
        query_names = query_names_for_path(path)
        events.append(
            RequestLedgerEvent(
                timestamp="2026-07-15T00:00:00+00:00",
                phase="attempted",
                host_role="gateway",
                method=method,
                path=safe_path,
                query_names=query_names,
                query_present=False,
                status=None,
            ),
        )
        events.append(
            RequestLedgerEvent(
                timestamp="2026-07-15T00:00:00+00:00",
                phase="completed",
                host_role="gateway",
                method=method,
                path=safe_path,
                query_names=query_names,
                query_present=False,
                status=200,
            ),
        )
    return events


def query_names_for_path(path: str) -> tuple[str, ...]:
    del path
    return ()
