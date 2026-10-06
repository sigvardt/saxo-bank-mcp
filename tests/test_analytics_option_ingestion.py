"""Documented option spaces join ordinary stored quotes by exact Saxo identity."""

# ruff: noqa: PLR2004
# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest
from test_analytics_sync import _CAPTURED_AT, _config, _PayloadExecutor, _resolved_handle

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_market_data import MarketDataValidationError, normalize_option_chain
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_reference_data import capture_instrument_details
from saxo_bank_mcp.analytics_resolver import InstrumentResolver
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.analytics_sync import (
    OptionReferenceDatasetRow,
    QuoteDatasetRow,
    SyncError,
    SyncValidationError,
    capture_option_chain,
    capture_quote,
    get_dataset,
)

_EXPIRY = date(2026, 9, 18)


def _space(*, underlying_uic: int = 1001) -> dict[str, object]:
    return {
        "AssetType": "StockOption",
        "CurrencyCode": "USD",
        "UnderlyingAssetType": "Stock",
        "OptionRootId": 800,
        "OptionSpace": [
            {"Expiry": "2026-12-18T00:00:00Z"},
            {
                "Expiry": "2026-09-18T00:00:00",
                "SpecificOptions": [
                    {
                        "PutCall": "Call",
                        "StrikePrice": 100,
                        "Uic": 2001,
                        "UnderlyingUic": underlying_uic,
                    },
                    {
                        "PutCall": "Put",
                        "StrikePrice": 100,
                        "Uic": 2002,
                        "UnderlyingUic": underlying_uic,
                    },
                ],
            },
        ],
    }


@pytest.mark.parametrize(
    ("asset_type", "underlying_type"),
    [("StockOption", "Stock"), ("FuturesOption", "Futures"), ("StockIndexOption", "StockIndex")],
)
def test_documented_option_space_uses_requested_expiry_exact_type_currency_and_underlying(
    asset_type: str,
    underlying_type: str,
) -> None:
    row = _space()
    row["AssetType"] = asset_type
    row["UnderlyingAssetType"] = underlying_type
    chain = normalize_option_chain(row=row, expected_root_id=800, expiry=_EXPIRY)
    assert len(chain.options) == 2
    assert chain.warnings == ()
    assert {option.expiry for option in chain.options} == {_EXPIRY}
    assert {option.asset_type for option in chain.options} == {asset_type}
    assert {option.currency for option in chain.options} == {"USD"}
    assert {option.underlying_identifier for option in chain.options} == {1001}
    assert {option.underlying_asset_type for option in chain.options} == {underlying_type}
    assert {option.strike_value for option in chain.options} == {100.0}
    changed = _space(underlying_uic=1002)
    changed["AssetType"] = asset_type
    changed["UnderlyingAssetType"] = underlying_type
    assert (
        normalize_option_chain(row=changed, expected_root_id=800, expiry=_EXPIRY).fingerprint_sha256
        != chain.fingerprint_sha256
    )


def test_documented_expiry_is_not_inferred_from_another_entry() -> None:
    with pytest.raises(MarketDataValidationError, match="expiry"):
        normalize_option_chain(row=_space(), expected_root_id=800, expiry=date(2027, 1, 1))
    row = _space()
    row["UnderlyingAssetType"] = None
    incomplete = normalize_option_chain(row=row, expected_root_id=800, expiry=_EXPIRY)
    assert incomplete.options == ()
    assert incomplete.warnings == ("option_reference_incomplete",)


@pytest.mark.anyio
async def test_actual_root_capture_reuses_saxo_identity_and_joins_normal_quote_and_details(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    underlying = await _resolved_handle(config)
    await capture_instrument_details(
        underlying,
        config=config,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (
                    {
                        "Data": [
                            {
                                "Uic": 1001,
                                "AssetType": "Stock",
                                "CurrencyCode": "USD",
                                "RelatedOptionRootsEnhanced": [
                                    {
                                        "AssetType": "StockOption",
                                        "OptionRootId": 800,
                                        "OptionType": "Default",
                                    }
                                ],
                            }
                        ],
                    },
                )
            )
        ),
    )
    executor = _PayloadExecutor((_space(), _space()))
    provider = SaxoAnalyticsProvider(request_executor=executor)
    first = await capture_option_chain(
        underlying,
        (_EXPIRY,),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    assert first.status == "complete"
    rows = get_dataset(first.datasets[0].dataset_id, 1, 500, config=config).rows
    assert all(isinstance(row, OptionReferenceDatasetRow) for row in rows)
    expected = {instrument_handle_for_saxo_identity("StockOption", uic) for uic in (2001, 2002)}
    assert {row.instrument_handle for row in rows} == expected
    assert {
        row.underlying_handle for row in rows if isinstance(row, OptionReferenceDatasetRow)
    } == {underlying}
    assert {row.currency for row in rows if isinstance(row, OptionReferenceDatasetRow)} == {"USD"}
    assert executor.calls[0][1].endswith("/contractoptionspaces/800")
    assert executor.calls[0][2]["OptionSpaceSegment"] == "SpecificDates"
    option = instrument_handle_for_saxo_identity("StockOption", 2001)
    quote_executor = _PayloadExecutor(
        (
            {
                "Uic": 2001,
                "AssetType": "StockOption",
                "Quote": {
                    "Bid": 4.0,
                    "Ask": 4.2,
                    "Mid": 4.1,
                    "DelayedByMinutes": 0,
                    "PriceType": "RealTime",
                },
            },
        )
    )
    quoted = await capture_quote(
        option,
        max_age=timedelta(minutes=5),
        config=config,
        provider=SaxoAnalyticsProvider(request_executor=quote_executor),
        clock=lambda: _CAPTURED_AT,
    )
    quote_row = get_dataset(quoted.datasets[0].dataset_id, 1, 500, config=config).rows[0]
    assert isinstance(quote_row, QuoteDatasetRow)
    assert quote_row.instrument_handle == option
    assert quote_row.mid_value == 4.1
    assert quote_executor.calls[0][2]["AssetType"] == "StockOption"
    assert quote_executor.calls[0][2]["Uic"] == "2001"
    await capture_instrument_details(
        option,
        config=config,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (
                    {
                        "Data": [
                            {
                                "Uic": 2001,
                                "AssetType": "StockOption",
                                "CurrencyCode": "USD",
                                "ContractSize": 100.0,
                            }
                        ],
                    },
                )
            )
        ),
    )
    resolved_option = await InstrumentResolver(
        SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (
                    {
                        "Data": [
                            {
                                "Identifier": 2001,
                                "AssetType": "StockOption",
                                "Description": "Actual option contract",
                                "Symbol": "OPT-C100",
                                "ExchangeId": "XCBO",
                            }
                        ]
                    },
                )
            )
        ),
        config,
    ).resolve_instruments("OPT-C100", ("StockOption",), ())
    assert resolved_option.matches[0].instrument_handle == option
    repeat = await capture_option_chain(
        underlying,
        (_EXPIRY,),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT + timedelta(minutes=1),
    )
    assert {
        row.instrument_handle
        for row in get_dataset(repeat.datasets[0].dataset_id, 1, 500, config=config).rows
    } == expected
    assert {
        row.instrument_handle
        for row in get_dataset(first.datasets[0].dataset_id, 1, 500, config=config).rows
    } == expected
    store = AnalyticsStore.open(config)
    try:
        assert store.find_authenticated_source_materials(
            contract_name="reference_instrument_details_v1", instrument_handle=option
        )
        material = store.get_authenticated_dataset_material(first.datasets[0].dataset_id)
        assert {page.contract_name for page in material.pages} == {
            "options_chain_reference_v1",
            "reference_instrument_details_v1",
        }
    finally:
        store.close()


@pytest.mark.anyio
async def test_legacy_flat_records_survive_new_typed_capture_with_same_option_uic(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config, related_root=800)
    executor = _PayloadExecutor(
        (
            {
                "OptionRootId": 800,
                "ExpiryDates": [_EXPIRY.isoformat()],
                "SpecificOptions": [{"Uic": 2001, "PutCall": "Call", "Strike": 100.0}],
            },
            _space(),
        )
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)
    legacy = await capture_option_chain(
        handle, (_EXPIRY,), provider=provider, config=config, clock=lambda: _CAPTURED_AT
    )
    legacy_rows = get_dataset(legacy.datasets[0].dataset_id, 1, 500, config=config).rows
    assert legacy.status == "degraded"
    assert len(legacy_rows) == 1
    typed = await capture_option_chain(
        handle,
        (_EXPIRY,),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT + timedelta(minutes=1),
    )
    typed_rows = get_dataset(typed.datasets[0].dataset_id, 1, 500, config=config).rows
    assert typed.status == "complete"
    assert legacy_rows[0].instrument_handle != instrument_handle_for_saxo_identity(
        "StockOption", 2001
    )
    assert instrument_handle_for_saxo_identity("StockOption", 2001) in {
        row.instrument_handle for row in typed_rows
    }
    assert get_dataset(legacy.datasets[0].dataset_id, 1, 500, config=config).rows == legacy_rows


@pytest.mark.anyio
@pytest.mark.parametrize("roots", [None, [800, 801]])
async def test_missing_or_ambiguous_root_refuses_before_source_access(
    tmp_path: Path,
    roots: list[int] | None,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    if roots is not None:
        await capture_instrument_details(
            handle,
            config=config,
            provider=SaxoAnalyticsProvider(
                request_executor=_PayloadExecutor(
                    (
                        {
                            "Data": [
                                {"Uic": 1001, "AssetType": "Stock", "RelatedOptionRoots": roots}
                            ],
                        },
                    )
                )
            ),
        )
    executor = _PayloadExecutor(())
    with pytest.raises(SyncValidationError, match="root"):
        await capture_option_chain(
            handle,
            (_EXPIRY,),
            config=config,
            provider=SaxoAnalyticsProvider(request_executor=executor),
        )
    assert executor.calls == []


@pytest.mark.anyio
async def test_related_option_handle_uses_explicit_authenticated_underlying_identity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    underlying = await _resolved_handle(config)
    resolved = await InstrumentResolver(
        SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (
                    {
                        "Data": [
                            {
                                "Identifier": 77,
                                "AssetType": "StockOption",
                                "Description": "Related option root",
                                "Symbol": "OPT",
                                "ExchangeId": "XCBO",
                            }
                        ],
                    },
                )
            )
        ),
        config,
    ).resolve_instruments("OPT", ("StockOption",), ())
    related = resolved.matches[0].instrument_handle
    await capture_instrument_details(
        related,
        config=config,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (
                    {
                        "Data": [
                            {
                                "Uic": 77,
                                "AssetType": "StockOption",
                                "UnderlyingAssetType": "Stock",
                                "RelatedInstruments": [{"Uic": 1001, "AssetType": "Stock"}],
                                "RelatedOptionRootsEnhanced": [
                                    {"OptionRootId": 800, "AssetType": "StockOption"}
                                ],
                            }
                        ],
                    },
                )
            )
        ),
    )
    executor = _PayloadExecutor((_space(),))
    captured = await capture_option_chain(
        related,
        (_EXPIRY,),
        config=config,
        provider=SaxoAnalyticsProvider(request_executor=executor),
    )
    rows = get_dataset(captured.datasets[0].dataset_id, 1, 500, config=config).rows
    assert executor.calls[0][1].endswith("/contractoptionspaces/800")
    assert {
        row.underlying_handle for row in rows if isinstance(row, OptionReferenceDatasetRow)
    } == {underlying}
    assert {row.instrument_handle for row in rows} == {
        instrument_handle_for_saxo_identity("StockOption", uic) for uic in (2001, 2002)
    }


@pytest.mark.anyio
async def test_response_option_type_must_match_authenticated_related_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    underlying = await _resolved_handle(config)
    await capture_instrument_details(
        underlying,
        config=config,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (
                    {
                        "Data": [
                            {
                                "Uic": 1001,
                                "AssetType": "Stock",
                                "RelatedOptionRootsEnhanced": [
                                    {"OptionRootId": 800, "AssetType": "StockOption"}
                                ],
                            }
                        ],
                    },
                )
            )
        ),
    )
    mismatched = _space()
    mismatched["AssetType"] = "FuturesOption"
    before = _owned_option_counts(config)
    with pytest.raises(SyncError, match="root asset type"):
        await capture_option_chain(
            underlying,
            (_EXPIRY,),
            config=config,
            provider=SaxoAnalyticsProvider(request_executor=_PayloadExecutor((mismatched,))),
        )
    assert _owned_option_counts(config) == before


@pytest.mark.anyio
async def test_mismatched_actual_underlying_rolls_back_capture_and_typed_identity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config, related_root=800)
    before = _owned_option_counts(config)
    with pytest.raises(SyncError, match="underlying identity"):
        await capture_option_chain(
            handle,
            (_EXPIRY,),
            config=config,
            provider=SaxoAnalyticsProvider(
                request_executor=_PayloadExecutor((_space(underlying_uic=1002),))
            ),
        )
    assert _owned_option_counts(config) == before


def _owned_option_counts(config: AnalyticsConfig) -> tuple[int, int, int, int]:
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        row = connection.execute(
            """SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM option_snapshots),
                (SELECT count(*) FROM safe_instruments),
                (SELECT count(*) FROM datasets)
            """
        ).fetchone()
        assert row is not None
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])
    finally:
        connection.close()
