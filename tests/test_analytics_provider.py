from __future__ import annotations

import json
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_pagination import (
    DuplicateSourcePageError,
    PaginationCycleError,
    PaginationLimitError,
    UnsafePaginationLinkError,
)
from saxo_bank_mcp.analytics_provider import (
    SaxoAnalyticsProvider,
    SourceAccessError,
    SourceEndpointError,
    SourceEntitlementError,
    SourcePayloadError,
    SourceRateLimitError,
    SourceRequestError,
    SourceSchemaDriftError,
    SourceStableKeyError,
    SourceTransportError,
)
from saxo_bank_mcp.analytics_source_contracts import (
    FrozenSourceJsonValue,
    SourceJsonValue,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation

_FIXTURE_ROOT = Path(__file__).parent / "fixtures/analytics/saxo_pages"
_UNAUTHORIZED_STATUS = 401
_ENTITLEMENT_DENIED_STATUS = 403
_EXPECTED_THREE_ATTEMPTS = 3
_EXPECTED_TWO_ATTEMPTS = 2
_SYNTHETIC_UIC = 1001
_SOURCE_OBJECT_ADAPTER = TypeAdapter(dict[str, SourceJsonValue])
_POSITION_CURSOR_PAYLOAD: Mapping[str, Any] = {
    "Data": [
        {
            "PositionBase": {"Amount": 1},
            "PositionId": "synthetic-position",
        }
    ]
}


def _fixture_bytes(name: str) -> bytes:
    return (_FIXTURE_ROOT / name).read_bytes()


def _json_response(
    status_code: int,
    payload: Mapping[str, Any] | list[Any] | str,
    *,
    headers: Mapping[str, str] | None = None,
) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        content=json.dumps(payload).encode(),
        headers=headers,
        request=httpx2.Request("GET", "https://unit.test/registered"),
    )


def _fixture_response(name: str, *, status_code: int = 200) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        content=_fixture_bytes(name),
        request=httpx2.Request("GET", "https://unit.test/registered"),
    )


class FakeExecutor:
    def __init__(
        self,
        outcomes: list[httpx2.Response | httpx2.RequestError | SourceAccessError],
    ) -> None:
        """Store deterministic outcomes for the injected transport boundary."""
        self._outcomes = outcomes
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        self.calls.append((operation.operation_id, request_target, dict(params)))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, httpx2.RequestError | SourceAccessError):
            raise outcome
        return outcome


async def _no_sleep(_delay: float) -> None:
    return None


def _provider(
    executor: FakeExecutor,
    *,
    page_limit: int | None = None,
    retry_attempts: int | None = None,
) -> SaxoAnalyticsProvider:
    return SaxoAnalyticsProvider(
        request_executor=executor,
        page_limit=page_limit,
        retry_attempts=retry_attempts,
        sleep=_no_sleep,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_provider_uses_only_the_frozen_registered_read_route() -> None:
    executor = FakeExecutor([_fixture_response("chart_page_2.json")])
    provider = _provider(executor)

    pages = [
        page
        async for page in provider.fetch(
            "chart_v3",
            {
                "AssetType": "Stock",
                "ChartSampleFieldSet": "Bid",
                "Count": 1,
                "Uic": 1001,
            },
        )
    ]

    assert len(pages) == 1
    assert executor.calls == [
        (
            "get.chart.v3.charts",
            "/chart/v3/charts",
            {
                "AssetType": "Stock",
                "ChartSampleFieldSet": "Bid",
                "Count": "1",
                "Uic": "1001",
            },
        )
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("routing_key", ["url", "path", "method", "operation_id", "__next"])
async def test_provider_rejects_raw_caller_routing_before_transport(
    routing_key: str,
) -> None:
    executor = FakeExecutor([])
    provider = _provider(executor)

    with pytest.raises(SourceRequestError, match="caller routing"):
        _ = [
            page
            async for page in provider.fetch(
                "chart_v3",
                {routing_key: "https://example.invalid/private-path"},
            )
        ]

    assert executor.calls == []


@pytest.mark.anyio
async def test_data_and_next_are_structural_and_returned_pagination_is_preserved() -> None:
    executor = FakeExecutor(
        [
            _fixture_response("chart_page_1.json"),
            _fixture_response("chart_page_2.json"),
        ]
    )
    provider = _provider(executor)

    pages = [
        page
        async for page in provider.fetch(
            "chart_v3",
            {"AssetType": "Stock", "Count": 2, "Uic": 1001},
        )
    ]

    assert [page.page_number for page in pages] == [1, 2]
    assert [page.row_count for page in pages] == [1, 1]
    assert pages[0].rows[0]["Time"] == "2026-07-29T08:00:00Z"
    assert pages[0].next_link == (
        "/chart/v3/charts?AssetType=Stock&Count=2&Uic=1001&$skiptoken=synthetic-page-2"
    )
    assert pages[1].next_link is None
    assert executor.calls[1] == (
        "get.chart.v3.charts",
        "/chart/v3/charts",
        {
            "$skiptoken": "synthetic-page-2",
            "AssetType": "Stock",
            "Count": "2",
            "Uic": "1001",
        },
    )


@pytest.mark.anyio
async def test_null_returned_next_link_completes_without_quarantine() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "CloseBid": 101.0,
                            "Time": "2026-07-29T08:00:00Z",
                        }
                    ],
                    "DataVersion": 7,
                    "__next": None,
                },
            )
        ]
    )
    provider = _provider(executor)

    pages = [page async for page in provider.fetch("chart_v3", {})]

    assert [page.page_number for page in pages] == [1]
    assert pages[0].next_link is None
    assert len(executor.calls) == 1
    assert provider.quarantined_analysis_kinds == ()


@pytest.mark.anyio
async def test_absolute_returned_pagination_link_is_never_followed() -> None:
    first = _json_response(
        200,
        {
            "Data": [{"Time": "2026-07-29T08:00:00Z", "CloseBid": 101.0}],
            "DataVersion": 7,
            "__next": "https://example.invalid/chart/v3/charts?token=private",
        },
    )
    executor = FakeExecutor([first])
    provider = _provider(executor)

    with pytest.raises(UnsafePaginationLinkError):
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert len(executor.calls) == 1
    assert "instrument_risk" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_returned_pagination_cannot_change_the_scoped_resource_path() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "BookingId": "synthetic-booking",
                            "BookingDate": "2026-07-29T08:00:00Z",
                        }
                    ],
                    "__next": ("/cs/v1/reports/bookings/client-b?$skiptoken=private-continuation"),
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceEndpointError, match="scoped source path"):
        _ = [
            page
            async for page in provider.fetch(
                "bookings_v1",
                {"ClientKey": "client-a"},
            )
        ]

    assert len(executor.calls) == 1
    assert "portfolio_performance" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_returned_pagination_cannot_change_an_explicit_query_scope() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "Time": "2026-07-29T08:00:00Z",
                            "CloseBid": 101.0,
                        }
                    ],
                    "DataVersion": 7,
                    "__next": (
                        "/chart/v3/charts?AssetType=Stock&Uic=2002&$skiptoken=private-continuation"
                    ),
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceEndpointError, match="scoped source query"):
        _ = [
            page
            async for page in provider.fetch(
                "chart_v3",
                {"AssetType": "Stock", "Uic": _SYNTHETIC_UIC},
            )
        ]

    assert len(executor.calls) == 1
    assert "instrument_risk" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
@pytest.mark.parametrize(
    "next_link",
    [
        "/chart/v3/charts?AssetType=Stock&$skiptoken=second",
        ("/chart/v3/charts?AccountKey=added&AssetType=Stock&Uic=1001&$skiptoken=second"),
        "/chart/v3/charts?AssetType=Stock&Uic=1001&Uic=1001&$skiptoken=second",
    ],
)
async def test_returned_pagination_rejects_removed_added_or_duplicate_query_scope(
    next_link: str,
) -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "Time": "2026-07-29T08:00:00Z",
                            "CloseBid": 101.0,
                        }
                    ],
                    "DataVersion": 7,
                    "__next": next_link,
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceEndpointError, match="scoped source query"):
        _ = [
            page
            async for page in provider.fetch(
                "chart_v3",
                {"AssetType": "Stock", "Uic": _SYNTHETIC_UIC},
            )
        ]

    assert len(executor.calls) == 1
    assert "instrument_risk" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_returned_pagination_cannot_remove_count_scope() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "Time": "2026-07-29T08:00:00Z",
                            "CloseBid": 101.0,
                        }
                    ],
                    "DataVersion": 7,
                    "__next": ("/chart/v3/charts?AssetType=Stock&Uic=1001&$skiptoken=second"),
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceEndpointError, match="scoped source query"):
        _ = [
            page
            async for page in provider.fetch(
                "chart_v3",
                {"AssetType": "Stock", "Count": 2, "Uic": _SYNTHETIC_UIC},
            )
        ]

    assert len(executor.calls) == 1
    assert "instrument_risk" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_returned_pagination_fragment_is_rejected_before_transport() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "Time": "2026-07-29T08:00:00Z",
                            "CloseBid": 101.0,
                        }
                    ],
                    "DataVersion": 7,
                    "__next": "/chart/v3/charts?$skiptoken=second#private-marker",
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(UnsafePaginationLinkError):
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert len(executor.calls) == 1
    assert "instrument_risk" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_returned_pagination_path_parameters_are_rejected_before_transport() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "Time": "2026-07-29T08:00:00Z",
                            "CloseBid": 101.0,
                        }
                    ],
                    "DataVersion": 7,
                    "__next": "/chart/v3/charts;private-marker?$skiptoken=second",
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(UnsafePaginationLinkError):
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert len(executor.calls) == 1
    assert "instrument_risk" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_path_scoped_continuation_is_rebuilt_before_transport() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "BookingId": "synthetic-booking-a",
                            "BookingDate": "2026-07-29T08:00:00Z",
                        }
                    ],
                    "__next": ("/cs/v1/reports/bookings/client-a?$skiptoken=second"),
                },
            ),
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "BookingId": "synthetic-booking-b",
                            "BookingDate": "2026-07-29T08:01:00Z",
                        }
                    ]
                },
            ),
        ]
    )
    provider = _provider(executor)

    pages = [
        page
        async for page in provider.fetch(
            "bookings_v1",
            {"ClientKey": "client-a"},
        )
    ]

    assert [page.page_number for page in pages] == [1, 2]
    assert executor.calls[1] == (
        "get.cs.v1.reports.bookings.clientkey",
        "/cs/v1/reports/bookings/client-a",
        {"$skiptoken": "second"},
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("contract_id", "request_values"),
    [
        ("positions_v1", {"$skip": -1}),
        ("positions_v1", {"$skip": 1.5}),
        ("positions_v1", {"$top": 1_001}),
        (
            "bookings_v1",
            {
                "$skip": 1,
                "$skiptoken": "opaque",
                "ClientKey": "client-a",
            },
        ),
        (
            "bookings_v1",
            {
                "$skiptoken": "",
                "ClientKey": "client-a",
            },
        ),
    ],
)
async def test_initial_caller_cursors_use_the_frozen_pagination_schema_before_transport(
    contract_id: str,
    request_values: Mapping[str, object],
) -> None:
    executor = FakeExecutor([_json_response(200, {"Data": []})])
    provider = _provider(executor)

    with pytest.raises(SourceRequestError) as caught:
        _ = [page async for page in provider.fetch(contract_id, request_values)]

    assert caught.value.code == "invalid_source_pagination_cursor"
    assert executor.calls == []
    assert provider.quarantined_analysis_kinds == ()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("contract_id", "next_link", "payload", "quarantined_kind"),
    [
        (
            "positions_v1",
            "/port/v1/positions?$top=100",
            _POSITION_CURSOR_PAYLOAD,
            "portfolio_exposure",
        ),
        (
            "positions_v1",
            "/port/v1/positions?$top=999999999",
            _POSITION_CURSOR_PAYLOAD,
            "portfolio_exposure",
        ),
        (
            "positions_v1",
            "/port/v1/positions?$top=" + ("9" * 5_000),
            _POSITION_CURSOR_PAYLOAD,
            "portfolio_exposure",
        ),
        (
            "chart_v3",
            "/chart/v3/charts?$skiptoken=",
            {
                "Data": [{"CloseBid": 101.0, "Time": "2026-07-29T08:00:00Z"}],
                "DataVersion": 7,
            },
            "instrument_risk",
        ),
        (
            "chart_v3",
            "/chart/v3/charts?$skiptoken=invalid%20token",
            {
                "Data": [{"CloseBid": 101.0, "Time": "2026-07-29T08:00:00Z"}],
                "DataVersion": 7,
            },
            "instrument_risk",
        ),
        (
            "positions_v1",
            "/port/v1/positions?$skip=-1",
            _POSITION_CURSOR_PAYLOAD,
            "portfolio_exposure",
        ),
        (
            "positions_v1",
            "/port/v1/positions?$skip=1.5",
            _POSITION_CURSOR_PAYLOAD,
            "portfolio_exposure",
        ),
        (
            "positions_v1",
            "/port/v1/positions?$cursor=opaque",
            _POSITION_CURSOR_PAYLOAD,
            "portfolio_exposure",
        ),
        (
            "positions_v1",
            "/port/v1/positions?$skip=1&$skip=2",
            _POSITION_CURSOR_PAYLOAD,
            "portfolio_exposure",
        ),
        (
            "positions_v1",
            "/port/v1/positions?$skip=1&$skiptoken=opaque",
            _POSITION_CURSOR_PAYLOAD,
            "portfolio_exposure",
        ),
    ],
)
async def test_returned_pagination_rejects_invalid_cursor_schema_before_transport(
    contract_id: str,
    next_link: str,
    payload: Mapping[str, Any],
    quarantined_kind: str,
) -> None:
    first_payload = dict(payload)
    first_payload["__next"] = next_link
    executor = FakeExecutor([_json_response(200, first_payload)])
    provider = _provider(executor)

    with pytest.raises(SourceEndpointError) as caught:
        _ = [page async for page in provider.fetch(contract_id, {})]

    assert caught.value.code == "source_pagination_cursor_invalid"
    assert len(executor.calls) == 1
    assert quarantined_kind in provider.quarantined_analysis_kinds


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("initial_skip", "returned_skip"),
    [
        (10, 10),
        (10, 9),
        (0, 0),
    ],
)
async def test_returned_offset_cursor_must_advance_from_the_initial_caller_cursor(
    initial_skip: int,
    returned_skip: int,
) -> None:
    first_payload = dict(_POSITION_CURSOR_PAYLOAD)
    first_payload["__next"] = f"/port/v1/positions?$skip={returned_skip}&$top=100"
    executor = FakeExecutor([_json_response(200, first_payload)])
    provider = _provider(executor)

    with pytest.raises(SourceEndpointError) as caught:
        _ = [
            page
            async for page in provider.fetch(
                "positions_v1",
                {"$skip": initial_skip, "$top": 100},
            )
        ]

    assert caught.value.code == "source_pagination_cursor_invalid"
    assert len(executor.calls) == 1
    assert "portfolio_exposure" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("contract_id", "initial_request", "first_link", "second_link", "payloads"),
    [
        (
            "positions_v1",
            {"$skip": 0, "$top": 100},
            "/port/v1/positions?$skip=1&$top=100",
            "/port/v1/positions?$top=100&$skip=1",
            (
                {
                    "Data": [
                        {
                            "PositionBase": {"Amount": 1},
                            "PositionId": "synthetic-position-a",
                        }
                    ]
                },
                {
                    "Data": [
                        {
                            "PositionBase": {"Amount": 2},
                            "PositionId": "synthetic-position-b",
                        }
                    ]
                },
            ),
        ),
        (
            "chart_v3",
            {},
            "/chart/v3/charts?$skiptoken=opaque",
            "/chart/v3/charts?$skiptoken=%6Fpaque",
            (
                {
                    "Data": [
                        {
                            "CloseBid": 101.0,
                            "Time": "2026-07-29T08:00:00Z",
                        }
                    ],
                    "DataVersion": 7,
                },
                {
                    "Data": [
                        {
                            "CloseBid": 102.0,
                            "Time": "2026-07-29T08:01:00Z",
                        }
                    ],
                    "DataVersion": 7,
                },
            ),
        ),
    ],
)
async def test_semantically_identical_returned_cursor_is_rejected_before_transport(
    contract_id: str,
    initial_request: Mapping[str, object],
    first_link: str,
    second_link: str,
    payloads: tuple[Mapping[str, Any], Mapping[str, Any]],
) -> None:
    first_payload = dict(payloads[0])
    first_payload["__next"] = first_link
    second_payload = dict(payloads[1])
    second_payload["__next"] = second_link
    executor = FakeExecutor(
        [
            _json_response(200, first_payload),
            _json_response(200, second_payload),
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceEndpointError) as caught:
        _ = [page async for page in provider.fetch(contract_id, initial_request)]

    assert caught.value.code == "source_pagination_cursor_invalid"
    assert len(executor.calls) == _EXPECTED_TWO_ATTEMPTS
    assert provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_chart_pagination_cannot_cross_data_version_revisions() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [{"Time": "2026-07-29T08:00:00Z", "CloseBid": 101.0}],
                    "DataVersion": 7,
                    "__next": "/chart/v3/charts?$skiptoken=second",
                },
            ),
            _json_response(
                200,
                {
                    "Data": [{"Time": "2026-07-29T08:01:00Z", "CloseBid": 102.0}],
                    "DataVersion": 8,
                },
            ),
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourcePayloadError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert caught.value.code == "source_revision_changed"
    assert "7" not in str(caught.value)
    assert "8" not in str(caught.value)
    assert len(executor.calls) == _EXPECTED_TWO_ATTEMPTS
    assert provider.quarantined_analysis_kinds == ()


@pytest.mark.anyio
async def test_repeated_next_link_stops_with_a_cycle_error() -> None:
    link = "/chart/v3/charts?$skiptoken=cycle"
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [{"Time": "2026-07-29T08:00:00Z", "CloseBid": 101.0}],
                    "DataVersion": 7,
                    "__next": link,
                },
            ),
            _json_response(
                200,
                {
                    "Data": [{"Time": "2026-07-29T08:01:00Z", "CloseBid": 102.0}],
                    "DataVersion": 7,
                    "__next": link,
                },
            ),
        ]
    )
    provider = _provider(executor)

    with pytest.raises(PaginationCycleError):
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert len(executor.calls) == _EXPECTED_TWO_ATTEMPTS
    assert "instrument_risk" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_duplicate_page_content_is_rejected_even_with_a_new_link() -> None:
    first_link = "/chart/v3/charts?$skiptoken=second"
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [{"Time": "2026-07-29T08:00:00Z", "CloseBid": 101.0}],
                    "DataVersion": 7,
                    "__next": first_link,
                },
            ),
            _json_response(
                200,
                {
                    "Data": [{"Time": "2026-07-29T08:00:00Z", "CloseBid": 101.0}],
                    "DataVersion": 7,
                },
            ),
        ]
    )
    provider = _provider(executor)

    with pytest.raises(DuplicateSourcePageError):
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert len(executor.calls) == _EXPECTED_TWO_ATTEMPTS
    assert "instrument_risk" in provider.quarantined_analysis_kinds


@pytest.mark.anyio
async def test_page_limit_stops_before_an_unbounded_followup() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [{"Time": "2026-07-29T08:00:00Z", "CloseBid": 101.0}],
                    "DataVersion": 7,
                    "__next": "/chart/v3/charts?$skiptoken=second",
                },
            )
        ]
    )
    provider = _provider(executor, page_limit=1)

    with pytest.raises(PaginationLimitError):
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert len(executor.calls) == 1
    assert provider.quarantined_analysis_kinds == ()


@pytest.mark.anyio
async def test_revised_stable_key_across_pages_is_quarantined() -> None:
    next_link = "/hist/v1/transactions?$skiptoken=revised"
    first_payload = _SOURCE_OBJECT_ADAPTER.validate_json(
        _fixture_bytes("transactions_out_of_order.json")
    )
    first_payload["__next"] = next_link
    executor = FakeExecutor(
        [
            _json_response(200, first_payload),
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "TransactionId": "synthetic-transaction-b",
                            "ExecutionTime": "2026-07-29T08:02:00Z",
                            "TransactionType": "Trade",
                            "Amount": 11.0,
                        }
                    ]
                },
            ),
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceStableKeyError):
        _ = [page async for page in provider.fetch("transactions_v1", {})]

    assert provider.quarantine_reason("portfolio_performance") == (
        "source_stable_key_invalid:transactions_v1"
    )


@pytest.mark.anyio
async def test_returned_pagination_may_advance_registered_offset_controls() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "TransactionId": "synthetic-transaction-a",
                            "ExecutionTime": "2026-07-29T08:00:00Z",
                            "TransactionType": "Trade",
                        }
                    ],
                    "__next": "/hist/v1/transactions?$skip=100&$top=100",
                },
            ),
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "TransactionId": "synthetic-transaction-b",
                            "ExecutionTime": "2026-07-29T08:01:00Z",
                            "TransactionType": "Trade",
                        }
                    ]
                },
            ),
        ]
    )
    provider = _provider(executor)

    pages = [
        page
        async for page in provider.fetch(
            "transactions_v1",
            {"$skip": 0, "$top": 100},
        )
    ]

    assert [page.page_number for page in pages] == [1, 2]
    assert len(executor.calls) == _EXPECTED_TWO_ATTEMPTS
    assert provider.quarantined_analysis_kinds == ()


@pytest.mark.anyio
async def test_additive_data_version_is_refused_without_a_revision_contract() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "TransactionId": "synthetic-transaction",
                            "ExecutionTime": "2026-07-29T08:00:00Z",
                            "TransactionType": "Trade",
                        }
                    ],
                    "DataVersion": 99,
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceSchemaDriftError) as caught:
        _ = [item async for item in provider.fetch("transactions_v1", {})]

    assert caught.value.additive_fields == ("DataVersion",)
    assert "portfolio_performance" in provider.quarantined_analysis_kinds
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_entitlement_failure_is_sanitized_and_never_retried() -> None:
    executor = FakeExecutor(
        [
            _fixture_response(
                "entitlement_denied.json",
                status_code=_ENTITLEMENT_DENIED_STATUS,
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceEntitlementError) as caught:
        _ = [
            page
            async for page in provider.fetch(
                "info_price_v1",
                {"AssetType": "Stock", "Uic": 1001},
            )
        ]

    assert caught.value.http_status == _ENTITLEMENT_DENIED_STATUS
    assert caught.value.error_code == "NoAccess"
    assert "synthetic entitlement unavailable" not in str(caught.value)
    assert len(executor.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("price_type_bid", "expected_state", "expected_entitlement", "expected_delayed"),
    [
        ("NoAccess", "limited", ("PriceTypeAsk", "PriceTypeBid"), ()),
        ("Delayed", "limited", (), ("PriceTypeAsk", "PriceTypeBid")),
        ("Realtime", "complete", (), ()),
    ],
)
async def test_quote_quality_tracks_price_type_limitations_without_values(
    price_type_bid: str,
    expected_state: str,
    expected_entitlement: tuple[str, ...],
    expected_delayed: tuple[str, ...],
) -> None:
    payload = {
        "AssetType": "Stock",
        "PriceTypeAsk": price_type_bid,
        "PriceTypeBid": price_type_bid,
        "Quote": {
            "Ask": 101.2,
            "Bid": 101.0,
            "DelayedByMinutes": 0,
            "Mid": 101.1,
            "PriceType": "Realtime",
        },
        "Uic": _SYNTHETIC_UIC,
    }
    provider = _provider(FakeExecutor([_json_response(200, payload)]))

    pages = [
        page
        async for page in provider.fetch(
            "info_price_v1",
            {"AssetType": "Stock", "Uic": _SYNTHETIC_UIC},
        )
    ]

    quality = getattr(pages[0], "source_quality", None)
    assert quality is not None
    assert quality.state == expected_state
    assert quality.entitlement_limited_fields == expected_entitlement
    assert quality.delayed_fields == expected_delayed
    assert quality.missing_fields == ()


@pytest.mark.anyio
@pytest.mark.parametrize("missing_value", [pytest.param("absent"), pytest.param(None)])
async def test_quote_quality_tracks_missing_or_null_optional_quote_fields(
    missing_value: str | None,
) -> None:
    payload: dict[str, Any] = {
        "AssetType": "Stock",
        "PriceTypeAsk": "Realtime",
        "Quote": {
            "Ask": 101.2,
            "Bid": 101.0,
            "DelayedByMinutes": 0,
            "Mid": 101.1,
            "PriceType": "Realtime",
        },
        "Uic": _SYNTHETIC_UIC,
    }
    if missing_value is None:
        payload["PriceTypeBid"] = None
    provider = _provider(FakeExecutor([_json_response(200, payload)]))

    pages = [
        page
        async for page in provider.fetch(
            "info_price_v1",
            {"AssetType": "Stock", "Uic": _SYNTHETIC_UIC},
        )
    ]

    quality = getattr(pages[0], "source_quality", None)
    assert quality is not None
    assert quality.state == "limited"
    assert quality.entitlement_limited_fields == ()
    assert quality.delayed_fields == ()
    assert quality.missing_fields == ("PriceTypeBid",)


@pytest.mark.anyio
async def test_quote_quality_metadata_rejects_internally_inconsistent_state() -> None:
    payload = {
        "AssetType": "Stock",
        "PriceTypeAsk": "Realtime",
        "PriceTypeBid": "Realtime",
        "Quote": {
            "Ask": 101.2,
            "Bid": 101.0,
            "DelayedByMinutes": 0,
            "Mid": 101.1,
            "PriceType": "Realtime",
        },
        "Uic": _SYNTHETIC_UIC,
    }
    provider = _provider(FakeExecutor([_json_response(200, payload)]))
    pages = [
        item
        async for item in provider.fetch(
            "info_price_v1",
            {"AssetType": "Stock", "Uic": _SYNTHETIC_UIC},
        )
    ]
    page = pages[0]
    page_payload = page.model_dump(mode="json")
    page_payload["source_quality"] = {
        "state": "limited",
        "entitlement_limited_fields": [],
        "delayed_fields": [],
        "missing_fields": [],
    }

    with pytest.raises(ValidationError):
        page.__class__.model_validate(page_payload, strict=True)


@pytest.mark.anyio
async def test_unauthorized_response_routes_to_sanitized_auth_recovery() -> None:
    marker = "private-auth-payload-marker"
    executor = FakeExecutor(
        [
            _json_response(
                _UNAUTHORIZED_STATUS,
                {"ErrorCode": "Unauthorized", "Message": marker},
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceAccessError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert caught.value.contract_id == "chart_v3"
    assert caught.value.reason == "authentication_required"
    assert marker not in str(caught.value)
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_access_gate_failure_is_rebound_to_the_frozen_contract() -> None:
    executor = FakeExecutor(
        [SourceAccessError("get.chart.v3.charts", "live_confirmation_required")]
    )
    provider = _provider(executor)

    with pytest.raises(SourceAccessError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert caught.value.contract_id == "chart_v3"
    assert caught.value.reason == "live_confirmation_required"
    assert "get.chart.v3.charts" not in str(caught.value)
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_rate_limit_retry_is_bounded_and_preserves_only_safe_reset_details() -> None:
    payload_marker = "private-rate-payload-marker"
    executor = FakeExecutor(
        [
            _json_response(
                429,
                {"ErrorCode": "RateLimitExceeded", "Message": payload_marker},
                headers={"Retry-After": "0", "X-RateLimit-Reset": "1722250000"},
            ),
            _json_response(
                429,
                {"ErrorCode": "RateLimitExceeded", "Message": payload_marker},
                headers={"Retry-After": "0", "X-RateLimit-Reset": "1722250000"},
            ),
        ]
    )
    provider = _provider(executor, retry_attempts=2)

    with pytest.raises(SourceRateLimitError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert caught.value.retry_after_seconds == 0.0
    assert caught.value.reset_hint == "1722250000"
    assert caught.value.attempts == _EXPECTED_TWO_ATTEMPTS
    assert payload_marker not in str(caught.value)
    assert len(executor.calls) == _EXPECTED_TWO_ATTEMPTS


@pytest.mark.anyio
async def test_rate_limit_without_a_usable_server_delay_is_not_retried() -> None:
    response = _json_response(
        429,
        {"ErrorCode": "RateLimitExceeded", "Message": "private-rate-marker"},
        headers={"X-RateLimit-Reset": "private account marker"},
    )
    executor = FakeExecutor([response, response, response])
    provider = _provider(executor, retry_attempts=3)

    with pytest.raises(SourceRateLimitError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert caught.value.retry_after_seconds is None
    assert caught.value.reset_hint is None
    assert caught.value.attempts == 1
    assert "private-rate-marker" not in str(caught.value)
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_transport_ambiguity_retries_only_the_proven_read_and_stays_sanitized() -> None:
    marker = "private-transport-marker"

    def failure() -> httpx2.ReadTimeout:
        return httpx2.ReadTimeout(
            marker,
            request=httpx2.Request("GET", "https://example.invalid/private"),
        )

    executor = FakeExecutor([failure(), failure(), failure()])
    provider = _provider(executor, retry_attempts=3)

    with pytest.raises(SourceTransportError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert caught.value.outcome == "unknown"
    assert caught.value.error_type == "ReadTimeout"
    assert caught.value.attempts == _EXPECTED_THREE_ATTEMPTS
    assert marker not in str(caught.value)
    assert "example.invalid" not in str(caught.value)
    formatted = "".join(traceback.format_exception(caught.value))
    assert marker not in formatted
    assert "example.invalid" not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(executor.calls) == _EXPECTED_THREE_ATTEMPTS


@pytest.mark.anyio
async def test_decoding_error_is_sanitized_without_retry_or_raw_context() -> None:
    marker = "private-decoding-marker"
    body_marker = "private-request-body"
    failure = httpx2.DecodingError(
        marker,
        request=httpx2.Request(
            "GET",
            "https://example.invalid/private-path",
            content=body_marker,
        ),
    )
    executor = FakeExecutor([failure])
    provider = _provider(executor, retry_attempts=3)

    with pytest.raises(SourceTransportError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    formatted = "".join(traceback.format_exception(caught.value))
    assert caught.value.error_type == "DecodingError"
    assert caught.value.attempts == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert marker not in formatted
    assert body_marker not in formatted
    assert "example.invalid" not in formatted
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_custom_request_error_type_name_is_not_retained() -> None:
    marker = "PrivateRequestMarker"
    private_request_error = type(marker, (httpx2.RequestError,), {})
    failure = private_request_error(
        "private-message",
        request=httpx2.Request("GET", "https://example.invalid/private-path"),
    )
    executor = FakeExecutor([failure])
    provider = _provider(executor, retry_attempts=3)

    with pytest.raises(SourceTransportError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    formatted = "".join(traceback.format_exception(caught.value))
    assert caught.value.error_type == "RequestError"
    assert marker not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_required_schema_drift_quarantines_dependent_analysis_kinds() -> None:
    private_marker = "private-payload-marker"
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [{"CloseBid": 101.0, "Unexpected": private_marker}],
                    "DataVersion": 7,
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceSchemaDriftError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert {"instrument_price_return", "instrument_risk"} <= set(
        provider.quarantined_analysis_kinds
    )
    assert caught.value.missing_required_fields == ("Time",)
    assert private_marker not in str(caught.value)
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_present_optional_wrong_type_quarantines_before_page_yield() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "Time": "2026-07-29T08:00:00Z",
                            "CloseBid": 101.0,
                            "OpenBid": {"wrong": "type"},
                        },
                    ],
                    "DataVersion": 7,
                },
            ),
        ],
    )
    provider = _provider(executor)

    with pytest.raises(SourceSchemaDriftError):
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert len(executor.calls) == 1
    assert {"instrument_price_return", "instrument_risk"} <= set(
        provider.quarantined_analysis_kinds,
    )


@pytest.mark.anyio
async def test_additive_field_is_refused_and_quarantines_dependent_analysis() -> None:
    marker = "private-additive-marker"
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "Data": [
                        {
                            "CloseBid": 101.0,
                            "NewChartField": marker,
                            "Time": "2026-07-29T08:00:00Z",
                        }
                    ],
                    "DataVersion": 7,
                },
            )
        ]
    )
    provider = _provider(executor)

    with pytest.raises(SourceSchemaDriftError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert caught.value.additive_fields == ("NewChartField",)
    assert marker not in str(caught.value)
    assert "instrument_risk" in provider.quarantined_analysis_kinds
    assert len(executor.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    (
        "contract_id",
        "source_request",
        "payload",
        "additive_path",
        "quarantined_kind",
    ),
    [
        (
            "chart_v3",
            {},
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "Time": "2026-07-29T08:00:00Z",
                    }
                ],
                "DataVersion": 7,
                "NewEnvelopeField": {"private": "marker"},
            },
            "NewEnvelopeField",
            "instrument_risk",
        ),
        (
            "positions_v1",
            {},
            {
                "Data": [
                    {
                        "PositionBase": {
                            "Amount": 1,
                            "AssetType": "Stock",
                            "NewNestedField": "private-marker",
                            "Uic": 1001,
                        },
                        "PositionId": "synthetic-position",
                    }
                ]
            },
            "PositionBase.NewNestedField",
            "portfolio_exposure",
        ),
        (
            "options_chain_reference_v1",
            {"OptionRootId": 17},
            {
                "ExpiryDates": ["2026-09-18"],
                "OptionRootId": 17,
                "SpecificOptions": [
                    {
                        "NewNestedField": "private-marker",
                        "PutCall": "Call",
                        "Strike": 100.0,
                        "Uic": 1001,
                    }
                ],
            },
            "SpecificOptions[].NewNestedField",
            "option_chain",
        ),
    ],
)
async def test_recursive_schema_drift_is_refused_before_source_page_yield(
    contract_id: str,
    source_request: Mapping[str, object],
    payload: Mapping[str, Any],
    additive_path: str,
    quarantined_kind: str,
) -> None:
    executor = FakeExecutor([_json_response(200, payload)])
    provider = _provider(executor)

    with pytest.raises(SourceSchemaDriftError) as caught:
        _ = [page async for page in provider.fetch(contract_id, source_request)]

    assert additive_path in caught.value.additive_fields
    assert quarantined_kind in provider.quarantined_analysis_kinds
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_unknown_enum_value_is_refused_and_quarantines_dependents() -> None:
    executor = FakeExecutor([_fixture_response("reference_unknown_enum.json")])
    provider = _provider(executor)

    with pytest.raises(SourceSchemaDriftError) as caught:
        _ = [
            page
            async for page in provider.fetch(
                "reference_instruments_v1",
                {"Keywords": "synthetic"},
            )
        ]

    assert caught.value.unknown_enum_fields == ("AssetType",)
    assert "instrument_resolution" in provider.quarantined_analysis_kinds
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_invalid_json_shape_fails_without_exposing_the_payload() -> None:
    private_marker = "private-invalid-payload"
    executor = FakeExecutor([_json_response(200, private_marker)])
    provider = _provider(executor)

    with pytest.raises(SourcePayloadError) as caught:
        _ = [page async for page in provider.fetch("chart_v3", {})]

    assert private_marker not in str(caught.value)
    assert len(executor.calls) == 1


@pytest.mark.anyio
async def test_object_contract_becomes_one_source_row() -> None:
    executor = FakeExecutor([_fixture_response("info_price_object.json")])
    provider = _provider(executor)

    pages = [
        page
        async for page in provider.fetch(
            "info_price_v1",
            {"AssetType": "Stock", "Uic": 1001},
        )
    ]

    assert len(pages) == 1
    assert pages[0].row_count == 1
    assert pages[0].rows[0]["AssetType"] == "Stock"
    assert pages[0].rows[0]["Uic"] == _SYNTHETIC_UIC


@pytest.mark.anyio
async def test_source_page_rows_and_provenance_are_deeply_immutable() -> None:
    executor = FakeExecutor(
        [
            _json_response(
                200,
                {
                    "ExpiryDates": ["2026-09-18"],
                    "OptionRootId": 17,
                    "SpecificOptions": [{"Uic": 1001}],
                },
            )
        ]
    )
    provider = _provider(executor)

    page = next(
        iter(
            [
                item
                async for item in provider.fetch(
                    "options_chain_reference_v1",
                    {"OptionRootId": 17},
                )
            ]
        )
    )

    serialized_before = page.model_dump_json()
    fingerprint_before = page.page_fingerprint_sha256
    with pytest.raises(TypeError):
        cast("dict[str, FrozenSourceJsonValue]", page.rows[0])["OptionRootId"] = 18
    specific_options = page.rows[0]["SpecificOptions"]
    assert isinstance(specific_options, tuple)
    nested = specific_options[0]
    assert isinstance(nested, Mapping)
    with pytest.raises(TypeError):
        cast("dict[str, SourceJsonValue]", nested)["Uic"] = 2002
    with pytest.raises(TypeError):
        cast(
            "dict[str, tuple[str, ...]]",
            page.schema_comparison.unknown_enum_values,
        )["AssetType"] = ("InjectedAssetType",)

    assert page.model_dump_json() == serialized_before
    assert page.page_fingerprint_sha256 == fingerprint_before
    serialized = json.loads(serialized_before)
    assert serialized["rows"][0]["SpecificOptions"] == [{"Uic": 1001}]
