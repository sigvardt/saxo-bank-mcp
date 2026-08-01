from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx2
import pytest

from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_resolver import (
    InstrumentResolver,
    ResolutionError,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation


class _Executor:
    def __init__(self, responses: Sequence[Sequence[Mapping[str, object]]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        self.calls.append((operation.operation_id, request_target, dict(params)))
        rows = self._responses.pop(0)
        return httpx2.Response(
            200,
            content=json.dumps({"Data": rows}).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )


def _provider(
    responses: Sequence[Sequence[Mapping[str, object]]],
) -> SaxoAnalyticsProvider:
    return SaxoAnalyticsProvider(request_executor=_Executor(responses))


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )


def _instrument(
    identifier: int,
    *,
    symbol: str,
    exchange: str | None,
    asset_type: str = "Stock",
    description: str = "Fixture instrument",
) -> dict[str, object]:
    return {
        "Identifier": identifier,
        "AssetType": asset_type,
        "Description": description,
        "Symbol": symbol,
        "ExchangeId": exchange,
    }


@pytest.mark.anyio
async def test_ambiguous_ticker_keeps_all_multiple_listings(tmp_path: Path) -> None:
    executor = _Executor(
        [
            [
                _instrument(101, symbol="DUAL", exchange="XNYS"),
                _instrument(202, symbol="DUAL", exchange="XNAS"),
            ],
        ],
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)
    resolver = InstrumentResolver(provider, _config(tmp_path))

    result = await resolver.resolve_instruments("DUAL", (), ())

    assert result.status == "ambiguous"
    assert {match.exchange for match in result.matches} == {"XNYS", "XNAS"}
    assert {issue.code for issue in result.issues} == {"multiple_listings"}
    assert executor.calls == [
        (
            "get.ref.v1.instruments",
            "/ref/v1/instruments",
            {"$top": "100", "IncludeNonTradable": "true", "Keywords": "DUAL"},
        ),
    ]


@pytest.mark.anyio
async def test_asset_type_collision_is_not_silently_selected(tmp_path: Path) -> None:
    provider = _provider(
        [
            [
                _instrument(101, symbol="DUAL", exchange="XNAS"),
                _instrument(
                    101,
                    symbol="DUAL",
                    exchange="XNAS",
                    asset_type="CfdOnStock",
                ),
            ],
        ],
    )
    resolver = InstrumentResolver(provider, _config(tmp_path))

    result = await resolver.resolve_instruments("DUAL", (), ())

    assert result.status == "ambiguous"
    assert {match.asset_type for match in result.matches} == {"Stock", "CfdOnStock"}
    assert {issue.code for issue in result.issues} == {"asset_type_collision"}


@pytest.mark.anyio
async def test_exchange_filter_selects_one_listing_explicitly(tmp_path: Path) -> None:
    provider = _provider(
        [
            [
                _instrument(101, symbol="DUAL", exchange="XNYS"),
                _instrument(202, symbol="DUAL", exchange="XNAS"),
            ],
        ],
    )
    resolver = InstrumentResolver(provider, _config(tmp_path))

    result = await resolver.resolve_instruments("DUAL", ("Stock",), ("XNAS",))

    assert result.status == "resolved"
    assert len(result.matches) == 1
    assert result.matches[0].exchange == "XNAS"


@pytest.mark.anyio
async def test_exchange_filter_ignores_other_listing_saved_by_prior_search(
    tmp_path: Path,
) -> None:
    rows = [
        _instrument(101, symbol="DUAL", exchange="XNYS"),
        _instrument(202, symbol="DUAL", exchange="XNAS"),
    ]
    provider = _provider([rows, rows])
    resolver = InstrumentResolver(provider, _config(tmp_path))

    first = await resolver.resolve_instruments("DUAL", (), ())
    second = await resolver.resolve_instruments("DUAL", ("Stock",), ("XNAS",))

    assert first.status == "ambiguous"
    assert second.status == "resolved"
    assert len(second.matches) == 1
    assert second.matches[0].exchange == "XNAS"


def test_resolver_rejects_an_arbitrary_provider(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="SaxoAnalyticsProvider"):
        InstrumentResolver(object(), _config(tmp_path))  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_missing_exchange_remains_ambiguous(tmp_path: Path) -> None:
    provider = _provider(
        [[_instrument(101, symbol="NOEX", exchange=None)]],
    )
    resolver = InstrumentResolver(provider, _config(tmp_path))

    result = await resolver.resolve_instruments("NOEX", (), ())

    assert result.status == "ambiguous"
    assert result.matches[0].exchange is None
    assert {issue.code for issue in result.issues} == {"missing_exchange"}


@pytest.mark.anyio
async def test_delisted_instrument_becomes_unavailable_without_replacement(
    tmp_path: Path,
) -> None:
    provider = _provider(
        [
            [_instrument(101, symbol="GONE", exchange="XNAS")],
            [],
        ],
    )
    resolver = InstrumentResolver(provider, _config(tmp_path))
    first = await resolver.resolve_instruments("GONE", (), ())

    second = await resolver.resolve_instruments("GONE", (), ())

    assert first.status == "resolved"
    assert second.status == "unavailable"
    assert second.matches[0].instrument_handle == first.matches[0].instrument_handle
    assert second.matches[0].state == "unavailable"
    assert {issue.code for issue in second.issues} == {
        "previously_resolved_unavailable",
    }


@pytest.mark.anyio
async def test_renamed_instrument_preserves_its_safe_handle(tmp_path: Path) -> None:
    provider = _provider(
        [
            [_instrument(101, symbol="OLD", exchange="XNAS")],
            [_instrument(101, symbol="NEW", exchange="XNAS")],
        ],
    )
    resolver = InstrumentResolver(provider, _config(tmp_path))
    first = await resolver.resolve_instruments("OLD", (), ())

    second = await resolver.resolve_instruments("NEW", (), ())

    assert second.status == "resolved"
    assert second.matches[0].instrument_handle == first.matches[0].instrument_handle
    assert second.matches[0].state == "renamed"
    assert second.matches[0].symbol == "NEW"


@pytest.mark.anyio
async def test_new_listing_never_replaces_a_previously_resolved_identity(
    tmp_path: Path,
) -> None:
    provider = _provider(
        [
            [_instrument(101, symbol="SAME", exchange="XNAS")],
            [_instrument(202, symbol="SAME", exchange="XNAS")],
        ],
    )
    resolver = InstrumentResolver(provider, _config(tmp_path))
    first = await resolver.resolve_instruments("SAME", (), ())

    second = await resolver.resolve_instruments("SAME", (), ())

    assert second.status == "ambiguous"
    assert len({match.instrument_handle for match in second.matches}) == len(second.matches)
    assert first.matches[0].instrument_handle in {
        match.instrument_handle for match in second.matches
    }
    assert {issue.code for issue in second.issues} == {
        "no_silent_replacement",
        "previously_resolved_unavailable",
    }


@pytest.mark.anyio
async def test_safe_display_label_never_contains_the_broker_identifier(
    tmp_path: Path,
) -> None:
    provider = _provider(
        [[_instrument(9876543, symbol="SAFE", exchange="XNAS")]],
    )
    resolver = InstrumentResolver(provider, _config(tmp_path))

    result = await resolver.resolve_instruments("SAFE", (), ())

    match = result.matches[0]
    assert "9876543" not in match.display_label
    assert match.instrument_handle.startswith("ih_")
    assert not hasattr(match, "identifier")


@pytest.mark.anyio
async def test_sparse_owner_artifact_refuses_instrument_write(tmp_path: Path) -> None:
    config = _config(tmp_path)
    provider = _provider(
        [[_instrument(101, symbol="FULL", exchange="XNAS")]],
    )
    resolver = InstrumentResolver(provider, config)
    artifact = config.paths.artifacts_dir / "quota.bin"
    with artifact.open("wb") as handle:
        handle.truncate(config.limits.store_quota_bytes)
    artifact.chmod(0o600)

    with pytest.raises(ResolutionError, match="quota"):
        await resolver.resolve_instruments("FULL", (), ())
