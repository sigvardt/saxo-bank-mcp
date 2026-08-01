from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_resolver import (
    InstrumentResolver,
    InstrumentSource,
    InstrumentSourcePage,
)

_SOURCE_AT = datetime(2026, 7, 30, 8, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Page:
    rows: tuple[Mapping[str, object], ...]
    source_revision: str = "fetch:fixture"
    source_timestamp: datetime = _SOURCE_AT


class _Provider:
    def __init__(self, responses: Sequence[Sequence[Mapping[str, object]]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def fetch(
        self,
        contract_id: str,
        request: Mapping[str, object],
    ) -> AsyncIterator[InstrumentSourcePage]:
        self.calls.append((contract_id, dict(request)))
        rows = self._responses.pop(0)
        yield cast(
            "InstrumentSourcePage",
            _Page(rows=tuple(rows)),
        )


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
    provider = _Provider(
        [
            [
                _instrument(101, symbol="DUAL", exchange="XNYS"),
                _instrument(202, symbol="DUAL", exchange="XNAS"),
            ],
        ],
    )
    resolver = InstrumentResolver(cast("InstrumentSource", provider), _config(tmp_path))

    result = await resolver.resolve_instruments("DUAL", (), ())

    assert result.status == "ambiguous"
    assert {match.exchange for match in result.matches} == {"XNYS", "XNAS"}
    assert {issue.code for issue in result.issues} == {"multiple_listings"}
    assert provider.calls == [
        (
            "reference_instruments_v1",
            {"$top": 100, "IncludeNonTradable": True, "Keywords": "DUAL"},
        ),
    ]


@pytest.mark.anyio
async def test_asset_type_collision_is_not_silently_selected(tmp_path: Path) -> None:
    provider = _Provider(
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
    resolver = InstrumentResolver(cast("InstrumentSource", provider), _config(tmp_path))

    result = await resolver.resolve_instruments("DUAL", (), ())

    assert result.status == "ambiguous"
    assert {match.asset_type for match in result.matches} == {"Stock", "CfdOnStock"}
    assert {issue.code for issue in result.issues} == {"asset_type_collision"}


@pytest.mark.anyio
async def test_exchange_filter_selects_one_listing_explicitly(tmp_path: Path) -> None:
    provider = _Provider(
        [
            [
                _instrument(101, symbol="DUAL", exchange="XNYS"),
                _instrument(202, symbol="DUAL", exchange="XNAS"),
            ],
        ],
    )
    resolver = InstrumentResolver(cast("InstrumentSource", provider), _config(tmp_path))

    result = await resolver.resolve_instruments("DUAL", ("Stock",), ("XNAS",))

    assert result.status == "resolved"
    assert len(result.matches) == 1
    assert result.matches[0].exchange == "XNAS"


@pytest.mark.anyio
async def test_missing_exchange_remains_ambiguous(tmp_path: Path) -> None:
    provider = _Provider(
        [[_instrument(101, symbol="NOEX", exchange=None)]],
    )
    resolver = InstrumentResolver(cast("InstrumentSource", provider), _config(tmp_path))

    result = await resolver.resolve_instruments("NOEX", (), ())

    assert result.status == "ambiguous"
    assert result.matches[0].exchange is None
    assert {issue.code for issue in result.issues} == {"missing_exchange"}


@pytest.mark.anyio
async def test_delisted_instrument_becomes_unavailable_without_replacement(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        [
            [_instrument(101, symbol="GONE", exchange="XNAS")],
            [],
        ],
    )
    resolver = InstrumentResolver(cast("InstrumentSource", provider), _config(tmp_path))
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
    provider = _Provider(
        [
            [_instrument(101, symbol="OLD", exchange="XNAS")],
            [_instrument(101, symbol="NEW", exchange="XNAS")],
        ],
    )
    resolver = InstrumentResolver(cast("InstrumentSource", provider), _config(tmp_path))
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
    provider = _Provider(
        [
            [_instrument(101, symbol="SAME", exchange="XNAS")],
            [_instrument(202, symbol="SAME", exchange="XNAS")],
        ],
    )
    resolver = InstrumentResolver(cast("InstrumentSource", provider), _config(tmp_path))
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
    provider = _Provider(
        [[_instrument(9876543, symbol="SAFE", exchange="XNAS")]],
    )
    resolver = InstrumentResolver(cast("InstrumentSource", provider), _config(tmp_path))

    result = await resolver.resolve_instruments("SAFE", (), ())

    match = result.matches[0]
    assert "9876543" not in match.display_label
    assert match.instrument_handle.startswith("ih_")
    assert not hasattr(match, "identifier")
