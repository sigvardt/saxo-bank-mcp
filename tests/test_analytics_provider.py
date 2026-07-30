from __future__ import annotations

import json
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from pydantic import TypeAdapter

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
        outcomes: list[httpx2.Response | httpx2.TransportError | SourceAccessError],
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
        if isinstance(outcome, httpx2.TransportError | SourceAccessError):
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
        "/chart/v3/charts?AssetType=Stock&Uic=1001&$skiptoken=synthetic-page-2"
    )
    assert pages[1].next_link is None
    assert executor.calls[1] == (
        "get.chart.v3.charts",
        "/chart/v3/charts?AssetType=Stock&Uic=1001&$skiptoken=synthetic-page-2",
        {},
    )


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
async def test_out_of_order_and_revised_rows_are_preserved_without_collapse() -> None:
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

    pages = [page async for page in provider.fetch("transactions_v1", {})]

    assert [row["TransactionId"] for row in pages[0].rows] == [
        "synthetic-transaction-b",
        "synthetic-transaction-a",
    ]
    assert pages[1].rows[0]["TransactionId"] == "synthetic-transaction-b"
    assert pages[0].page_fingerprint_sha256 != pages[1].page_fingerprint_sha256
    assert pages[0].source_revision == pages[1].source_revision


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
async def test_additive_data_version_cannot_override_a_contract_revision_policy() -> None:
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

    page = next(iter([item async for item in provider.fetch("transactions_v1", {})]))

    assert page.data_version is None
    assert page.source_revision.startswith("fetch:")


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
    assert len(executor.calls) == _EXPECTED_THREE_ATTEMPTS


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
async def test_unknown_enum_value_is_yielded_and_does_not_quarantine() -> None:
    executor = FakeExecutor([_fixture_response("reference_unknown_enum.json")])
    provider = _provider(executor)

    pages = [
        page
        async for page in provider.fetch(
            "reference_instruments_v1",
            {"Keywords": "synthetic"},
        )
    ]

    assert pages[0].schema_comparison.unknown_enum_values == {
        "AssetType": ("FutureSaxoAssetType",),
    }
    assert provider.quarantined_analysis_kinds == ()


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
async def test_source_page_rows_are_deeply_immutable_after_fingerprinting() -> None:
    payload = _SOURCE_OBJECT_ADAPTER.validate_json(_fixture_bytes("info_price_object.json"))
    payload["NewArray"] = [{"Value": 1}]
    executor = FakeExecutor([_json_response(200, payload)])
    provider = _provider(executor)

    page = next(
        iter(
            [
                item
                async for item in provider.fetch(
                    "info_price_v1",
                    {"AssetType": "Stock", "Uic": 1001},
                )
            ]
        )
    )

    with pytest.raises(TypeError):
        cast("dict[str, FrozenSourceJsonValue]", page.rows[0])["Uic"] = 2002
    quote = page.rows[0]["Quote"]
    assert isinstance(quote, Mapping)
    with pytest.raises(TypeError):
        cast("dict[str, SourceJsonValue]", quote)["Bid"] = 99.0
    new_array = page.rows[0]["NewArray"]
    assert isinstance(new_array, tuple)
    nested = new_array[0]
    assert isinstance(nested, Mapping)
    with pytest.raises(TypeError):
        cast("dict[str, SourceJsonValue]", nested)["Value"] = 2
    serialized = json.loads(page.model_dump_json())
    assert serialized["rows"][0]["NewArray"] == [{"Value": 1}]
