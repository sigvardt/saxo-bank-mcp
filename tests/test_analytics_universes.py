from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_models import HandleKind, new_safe_handle
from saxo_bank_mcp.analytics_resolver import (
    InstrumentResolver,
    InstrumentSource,
    InstrumentSourcePage,
)
from saxo_bank_mcp.analytics_universes import (
    ResearchUniverseStore,
    UniverseConflictError,
    UniverseNotFoundError,
    UniverseValidationError,
)

_SOURCE_AT = datetime(2026, 7, 30, 8, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Page:
    rows: tuple[Mapping[str, object], ...]
    source_revision: str = "fetch:fixture"
    source_timestamp: datetime = _SOURCE_AT


class _Provider:
    def __init__(self, identifier: int, symbol: str) -> None:
        self._identifier = identifier
        self._symbol = symbol

    async def fetch(
        self,
        contract_id: str,
        request: Mapping[str, object],
    ) -> AsyncIterator[InstrumentSourcePage]:
        del contract_id, request
        yield cast(
            "InstrumentSourcePage",
            _Page(
                rows=(
                    {
                        "Identifier": self._identifier,
                        "AssetType": "Stock",
                        "Description": "Fixture instrument",
                        "Symbol": self._symbol,
                        "ExchangeId": "XNAS",
                    },
                ),
            ),
        )


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )


async def _handle(config: AnalyticsConfig, identifier: int, symbol: str) -> str:
    resolver = InstrumentResolver(
        cast("InstrumentSource", _Provider(identifier, symbol)),
        config,
    )
    result = await resolver.resolve_instruments(symbol, (), ())
    return result.matches[0].instrument_handle


@pytest.mark.anyio
async def test_saved_universe_is_owner_local_and_persists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    handle = await _handle(config, 101, "ONE")
    first = ResearchUniverseStore(config)

    created = first.create_universe("Core", (handle,))
    reopened = ResearchUniverseStore(config)

    assert reopened.list_universes() == (created,)
    assert created.handles == (handle,)
    assert created.instruments[0].display_label.startswith("ONE")


@pytest.mark.anyio
async def test_update_uses_optimistic_concurrency(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_handle = await _handle(config, 101, "ONE")
    second_handle = await _handle(config, 202, "TWO")
    universes = ResearchUniverseStore(config)
    created = universes.create_universe("Core", (first_handle,))
    updated = universes.update_universe(
        created.universe_id,
        additions=(second_handle,),
        removals=(),
        expected_revision=created.revision,
    )

    with pytest.raises(UniverseConflictError, match="revision changed"):
        universes.update_universe(
            created.universe_id,
            additions=(),
            removals=(first_handle,),
            expected_revision=created.revision,
        )

    assert updated.revision != created.revision
    assert updated.handles == (first_handle, second_handle)


@pytest.mark.anyio
async def test_update_never_silently_replaces_existing_handle(tmp_path: Path) -> None:
    config = _config(tmp_path)
    old_listing = await _handle(config, 101, "SAME")
    new_listing = await _handle(config, 202, "SAME")
    universes = ResearchUniverseStore(config)
    created = universes.create_universe("Listings", (old_listing,))

    updated = universes.update_universe(
        created.universe_id,
        additions=(new_listing,),
        removals=(),
        expected_revision=created.revision,
    )

    assert updated.handles == (old_listing, new_listing)


@pytest.mark.anyio
async def test_unknown_instrument_handle_is_refused(tmp_path: Path) -> None:
    universes = ResearchUniverseStore(_config(tmp_path))
    missing = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)

    with pytest.raises(UniverseValidationError, match="instrument does not exist"):
        universes.create_universe("Invalid", (missing,))


@pytest.mark.anyio
async def test_delete_is_revision_guarded(tmp_path: Path) -> None:
    config = _config(tmp_path)
    handle = await _handle(config, 101, "ONE")
    universes = ResearchUniverseStore(config)
    created = universes.create_universe("Core", (handle,))

    with pytest.raises(UniverseConflictError, match="revision changed"):
        universes.delete_universe(created.universe_id, "f" * 64)

    universes.delete_universe(created.universe_id, created.revision)

    assert universes.list_universes() == ()
    with pytest.raises(UniverseNotFoundError):
        universes.delete_universe(created.universe_id, created.revision)
