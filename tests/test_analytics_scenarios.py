from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_scenarios import (
    CurrencyShock,
    PortfolioScenarioRequest,
    ScenarioComponent,
    ScenarioShock,
    run_portfolio_scenario,
    scenario_shock_map_fingerprint,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS = "aa_00000000000040008000000000000052"
_OTHER_ALIAS = "aa_00000000000040008000000000000053"
_DATASET = "ds_00000000000040008000000000000052"
_SNAPSHOT = "ps_00000000000040008000000000000052"
_STOCK = "ih_00000000000040008000000000000052"
_FOREIGN = "ih_00000000000040008000000000000053"
_MODEL = "an_00000000000040008000000000000052"
_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
_HISTORY_START = datetime(2020, 2, 19, tzinfo=UTC)
_HISTORY_END = datetime(2020, 3, 23, tzinfo=UTC)


def _source(
    contract_id: str,
    *,
    quality: QualityState = QualityState.COMPLETE,
    entitlement: Literal["available", "partial", "denied"] = "available",
) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t15",
        capture_fingerprint_sha256={
            "exposure_instruments_v1": "c",
            "balances_v1": "d",
            "positions_v1": "a",
            "chart_v3": "e",
        }[contract_id]
        * 64,
        quality_state=quality,
        entitlement_state=entitlement,
    )


def _component(  # noqa: PLR0913
    handle: str = _STOCK,
    *,
    value: Decimal = Decimal(100),
    currency: str = "USD",
    branch: Literal["linear", "option", "fixed_income"] = "linear",
    margin: Decimal = Decimal(20),
    account_alias: str = _ALIAS,
) -> ScenarioComponent:
    return ScenarioComponent(
        account_alias=account_alias,
        instrument_handle=handle,
        branch_id=branch,
        current_value=value,
        currency=currency,
        current_margin_requirement=margin,
        model_analysis_id=_MODEL if branch != "linear" else None,
    )


def _shock(  # noqa: PLR0913
    handle: str = _STOCK,
    *,
    price: Decimal = Decimal(0),
    volatility_points: Decimal = Decimal(0),
    rate_basis_points: Decimal = Decimal(0),
    cash_flow: Decimal = Decimal(0),
    repriced_value: Decimal | None = None,
    stressed_margin: Decimal = Decimal(20),
) -> ScenarioShock:
    return ScenarioShock(
        instrument_handle=handle,
        price_shock_ratio=price,
        volatility_shock_points=volatility_points,
        rate_shock_basis_points=rate_basis_points,
        cash_flow_shock=cash_flow,
        repriced_value_at_base_fx=repriced_value,
        stressed_margin_requirement=stressed_margin,
    )


def _request(  # noqa: PLR0913
    *,
    scenario_type: Literal[
        "historical",
        "equity",
        "currency",
        "volatility",
        "rate",
        "margin",
        "combined",
    ] = "equity",
    components: tuple[ScenarioComponent, ...] | None = None,
    component_shocks: tuple[ScenarioShock, ...] | None = None,
    currency_shocks: tuple[CurrencyShock, ...] | None = None,
    current_margin_headroom: Decimal = Decimal(100),
    input_mode: Literal["numeric", "narrative_proposal"] = "numeric",
    narrative_fingerprint: str | None = None,
    echoed: bool = False,
    accepted: bool = False,
    source_bindings: tuple[SaxoSourceBinding, ...] | None = None,
    quality_state: QualityState = QualityState.COMPLETE,
) -> PortfolioScenarioRequest:
    component_values = components or (_component(),)
    shock_values = component_shocks or (_shock(),)
    currencies = tuple(dict.fromkeys(component.currency for component in component_values))
    currency_values = currency_shocks or tuple(
        CurrencyShock(currency=currency, shock_ratio=Decimal(0)) for currency in currencies
    )
    shock_map_sha256 = scenario_shock_map_fingerprint(shock_values, currency_values)
    bindings = source_bindings
    if bindings is None:
        base = [
            _source("balances_v1"),
            _source("positions_v1"),
            _source("exposure_instruments_v1"),
        ]
        if scenario_type == "historical":
            base.append(_source("chart_v3"))
        bindings = tuple(base)
    return PortfolioScenarioRequest(
        dataset_id=_DATASET,
        snapshot_id=_SNAPSHOT,
        account_alias=_ALIAS,
        as_of=_AT,
        reporting_currency="USD",
        scenario_type=scenario_type,
        input_mode=input_mode,
        narrative_fingerprint_sha256=narrative_fingerprint,
        numeric_shocks_echoed_by_caller=echoed,
        caller_accepted_numeric_shocks=accepted,
        echoed_shock_map_sha256=shock_map_sha256 if echoed else None,
        accepted_shock_map_sha256=shock_map_sha256 if accepted else None,
        historical_start_at=_HISTORY_START if scenario_type == "historical" else None,
        historical_end_at=_HISTORY_END if scenario_type == "historical" else None,
        components=component_values,
        component_shocks=shock_values,
        currency_shocks=currency_values,
        current_margin_headroom=current_margin_headroom,
        source_bindings=bindings,
        quality_state=quality_state,
        missing_fields=("history.before_start",) if quality_state is QualityState.PARTIAL else (),
        warnings=(),
    )


def _private(request: PortfolioScenarioRequest):  # noqa: ANN202
    result = run_portfolio_scenario(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    assert not isinstance(result, ResearchRefusal)
    assert result.private_values is not None
    return result, result.private_values


@pytest.mark.parametrize(
    ("scenario_request", "expected_effect", "expected_margin_effect"),
    [
        (
            _request(
                scenario_type="historical",
                component_shocks=(_shock(price=Decimal("-0.20")),),
            ),
            Decimal(-20),
            Decimal(0),
        ),
        (
            _request(
                scenario_type="equity",
                component_shocks=(_shock(price=Decimal("-0.10")),),
            ),
            Decimal(-10),
            Decimal(0),
        ),
        (
            _request(
                scenario_type="currency",
                components=(_component(currency="EUR"),),
                currency_shocks=(CurrencyShock(currency="EUR", shock_ratio=Decimal("-0.10")),),
            ),
            Decimal(-10),
            Decimal(0),
        ),
        (
            _request(
                scenario_type="volatility",
                components=(_component(branch="option"),),
                component_shocks=(
                    _shock(
                        volatility_points=Decimal(5),
                        repriced_value=Decimal(110),
                    ),
                ),
            ),
            Decimal(10),
            Decimal(0),
        ),
        (
            _request(
                scenario_type="rate",
                components=(_component(branch="fixed_income"),),
                component_shocks=(
                    _shock(
                        rate_basis_points=Decimal(100),
                        repriced_value=Decimal(95),
                    ),
                ),
            ),
            Decimal(-5),
            Decimal(0),
        ),
        (
            _request(
                scenario_type="margin",
                component_shocks=(_shock(stressed_margin=Decimal(30)),),
            ),
            Decimal(0),
            Decimal(-10),
        ),
    ],
)
def test_each_typed_scenario_map_has_an_explicit_numeric_effect(
    scenario_request: PortfolioScenarioRequest,
    expected_effect: Decimal,
    expected_margin_effect: Decimal,
) -> None:
    result, values = _private(scenario_request)

    if scenario_request.scenario_type in {"volatility", "rate"}:
        assert result.status is ResearchStatus.REDUCED
        assert "explicit_model_repricing_input" in result.warnings
    else:
        assert result.status is ResearchStatus.COMPLETE
    assert result.is_not_forecast is True
    assert values.total_effect == expected_effect
    assert values.margin_stress_effect == expected_margin_effect


def test_zero_shock_is_identity_and_preserves_margin_headroom() -> None:
    _, values = _private(_request())

    assert values.total_effect == Decimal(0)
    assert values.contributions[0].current_value == Decimal(100)
    assert values.contributions[0].stressed_value == Decimal(100)
    assert values.contributions[0].effect == Decimal(0)
    assert values.current_margin_headroom == Decimal(100)
    assert values.stressed_margin_headroom == Decimal(100)
    assert values.margin_stress_effect == Decimal(0)


def test_combined_shocks_apply_simultaneously_and_reconcile_contributions() -> None:
    components = (
        _component(_STOCK, value=Decimal(100), currency="USD", margin=Decimal(20)),
        _component(_FOREIGN, value=Decimal(200), currency="EUR", margin=Decimal(40)),
    )
    shocks = (
        _shock(
            _STOCK,
            price=Decimal("-0.10"),
            cash_flow=Decimal(2),
            stressed_margin=Decimal(25),
        ),
        _shock(
            _FOREIGN,
            price=Decimal("0.05"),
            stressed_margin=Decimal(55),
        ),
    )
    _, values = _private(
        _request(
            scenario_type="combined",
            components=components,
            component_shocks=shocks,
            currency_shocks=(
                CurrencyShock(currency="USD", shock_ratio=Decimal(0)),
                CurrencyShock(currency="EUR", shock_ratio=Decimal("-0.10")),
            ),
        ),
    )

    assert tuple(item.effect for item in values.contributions) == (
        Decimal(-8),
        Decimal(-11),
    )
    assert values.total_effect == Decimal(-19)
    assert sum(item.effect for item in values.contributions) == values.total_effect
    assert values.stressed_margin_headroom == Decimal(80)
    assert values.margin_stress_effect == Decimal(-20)


def test_narrative_scenario_cannot_run_until_numbers_are_echoed_and_accepted() -> None:
    proposed = _request(
        input_mode="narrative_proposal",
        narrative_fingerprint="f" * 64,
        component_shocks=(_shock(price=Decimal("-0.12")),),
    )
    refused = run_portfolio_scenario(
        proposed,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    _, accepted = _private(
        _request(
            input_mode="narrative_proposal",
            narrative_fingerprint="f" * 64,
            echoed=True,
            accepted=True,
            component_shocks=(_shock(price=Decimal("-0.12")),),
        ),
    )

    assert isinstance(refused, ResearchRefusal)
    assert refused.reason_code == "scenario_numeric_confirmation_required"
    assert accepted.total_effect == Decimal(-12)


def test_scenario_refuses_incomplete_maps_and_unsupported_repricing() -> None:
    missing_component = run_portfolio_scenario(
        _request(component_shocks=(_shock(_FOREIGN),)),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    missing_repricing = run_portfolio_scenario(
        _request(
            scenario_type="volatility",
            components=(_component(branch="option"),),
            component_shocks=(_shock(volatility_points=Decimal(5)),),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(missing_component, ResearchRefusal)
    assert missing_component.reason_code == "scenario_shock_map_mismatch"
    assert isinstance(missing_repricing, ResearchRefusal)
    assert missing_repricing.reason_code == "scenario_repricing_required"


def test_scenario_requires_exact_saxo_sources_and_refuses_stale_data() -> None:
    missing_history = run_portfolio_scenario(
        _request(
            scenario_type="historical",
            source_bindings=(
                _source("balances_v1"),
                _source("positions_v1"),
                _source("exposure_instruments_v1"),
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    denied = run_portfolio_scenario(
        _request(
            source_bindings=(
                _source("exposure_instruments_v1", entitlement="denied"),
                _source("balances_v1"),
                _source("positions_v1"),
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    stale = run_portfolio_scenario(
        _request(quality_state=QualityState.STALE),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(missing_history, ResearchRefusal)
    assert missing_history.reason_code == "source_contract_missing"
    assert isinstance(denied, ResearchRefusal)
    assert denied.reason_code == "source_entitlement_insufficient"
    assert isinstance(stale, ResearchRefusal)
    assert stale.reason_code == "scenario_data_unusable"


def test_scenario_privacy_alias_isolation_and_typed_input_only() -> None:
    public = run_portfolio_scenario(
        _request(
            components=(_component(value=Decimal(123456)),),
            component_shocks=(_shock(price=Decimal("-0.25")),),
        ),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    assert public.evidence.private_values_redacted is True
    assert "123456" not in json.dumps(public.model_dump(mode="json"), sort_keys=True)
    with pytest.raises(ValidationError, match="account alias"):
        _request(components=(_component(account_alias=_OTHER_ALIAS),))
    request_data = _request().model_dump()
    shock_data = request_data["component_shocks"][0]
    del shock_data["price_shock_ratio"]
    with pytest.raises(ValidationError):
        PortfolioScenarioRequest.model_validate(request_data)
    with pytest.raises(ValidationError):
        PortfolioScenarioRequest.model_validate(
            {**_request().model_dump(), "sql": "select * from positions"},
        )
