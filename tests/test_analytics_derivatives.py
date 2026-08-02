from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_derivatives import (
    DerivativeAnalyticsResult,
    DerivativeDataset,
    FuturesContractPoint,
    FuturesCurveRequest,
    FuturesCurveValues,
    FxForwardRequest,
    FxForwardValues,
    IvSurfacePoint,
    IvSurfaceRequest,
    IvSurfaceValues,
    LifecyclePosition,
    LifecycleRadarRequest,
    LifecycleRadarValues,
    OptionAnalyticsValues,
    SaxoGreekSnapshot,
    StrategyAnalyticsValues,
    analyze_futures_curve,
    analyze_fx_forward,
    analyze_iv_surface,
    analyze_lifecycle_radar,
    analyze_option_model,
    analyze_option_strategy,
)
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_options import (
    OptionContract,
    OptionGreeks,
    OptionModelInput,
    OptionStrategyLeg,
    OptionStrategyRequest,
)
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS = "aa_00000000000040008000000000000071"
_DATASET = "ds_00000000000040008000000000000071"
_OPTION = "ih_00000000000040008000000000000071"
_OPTION_TWO = "ih_00000000000040008000000000000072"
_UNDERLYING = "ih_00000000000040008000000000000073"
_FUTURES_NEAR = "ih_00000000000040008000000000000074"
_FUTURES_LATER = "ih_00000000000040008000000000000075"
_AS_OF = datetime(2026, 1, 2, tzinfo=UTC)
_PAYOFF_POINT_COUNT = 3
_RADAR_EVENT_COUNT = 2
_FIRST_EXPIRY_DAYS = 10
_EXPECTED_BASIS = 2.0
_EXPECTED_ROLL = -41.0
_FX_SPOT = 7.0


def _source(
    contract_id: str,
    *,
    quality: QualityState = QualityState.COMPLETE,
    entitlement: Literal["available", "partial", "denied"] = "available",
) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    characters = {
        "options_chain_reference_v1": "1",
        "info_price_v1": "2",
        "reference_instruments_v1": "3",
        "positions_v1": "4",
        "corporate_action_events_v2": "5",
    }
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t17",
        capture_fingerprint_sha256=characters[contract_id] * 64,
        quality_state=quality,
        entitlement_state=entitlement,
    )


def _sources() -> tuple[SaxoSourceBinding, ...]:
    return tuple(
        _source(contract_id)
        for contract_id in (
            "options_chain_reference_v1",
            "info_price_v1",
            "reference_instruments_v1",
            "positions_v1",
            "corporate_action_events_v2",
        )
    )


def _dataset(
    *,
    source_bindings: tuple[SaxoSourceBinding, ...] | None = None,
    quality_state: QualityState = QualityState.COMPLETE,
) -> DerivativeDataset:
    return DerivativeDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        as_of=_AS_OF,
        instrument_handles=(
            _OPTION,
            _OPTION_TWO,
            _UNDERLYING,
            _FUTURES_NEAR,
            _FUTURES_LATER,
        ),
        source_bindings=_sources() if source_bindings is None else source_bindings,
        quality_state=quality_state,
        missing_fields=("quote.mid",) if quality_state is QualityState.PARTIAL else (),
        warnings=(),
    )


def _contract(
    *,
    option_handle: str = _OPTION,
    option_type: Literal["call", "put"] = "call",
    strike: float = 100.0,
) -> OptionContract:
    return OptionContract(
        dataset_id=_DATASET,
        option_handle=option_handle,
        underlying_handle=_UNDERLYING,
        contract_currency="USD",
        pricing_model="black_scholes",
        reference_kind="spot",
        option_type=option_type,
        exercise_style="european",
        payoff_style="vanilla",
        rate_model="constant",
        reference_price=100.0,
        strike=strike,
        time_to_expiry_years=1.0,
        risk_free_rate=0.05,
        dividend_yield=0.0,
        days_per_year=365.0,
    )


def _model_input() -> OptionModelInput:
    return OptionModelInput(contract=_contract(), volatility=0.2)


def _strategy() -> OptionStrategyRequest:
    return OptionStrategyRequest(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        strategy_id="vertical_fixture",
        legs=(
            OptionStrategyLeg(
                model_input=OptionModelInput(contract=_contract(strike=95.0), volatility=0.2),
                quantity=1.0,
                contract_multiplier=100.0,
            ),
            OptionStrategyLeg(
                model_input=OptionModelInput(
                    contract=_contract(option_handle=_OPTION_TWO, strike=105.0),
                    volatility=0.2,
                ),
                quantity=-1.0,
                contract_multiplier=100.0,
            ),
        ),
        net_premium=250.0,
        eligible_costs=10.0,
    )


def _private(result: DerivativeAnalyticsResult) -> object:
    assert result.private_values is not None
    return result.private_values


def test_option_model_and_strategy_are_owner_only_reduced_proposals() -> None:
    option_result = analyze_option_model(
        _dataset(),
        _model_input(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    strategy_result = analyze_option_strategy(
        _dataset(),
        _strategy(),
        expiry_reference_prices=(90.0, 100.0, 110.0),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(option_result, ResearchRefusal)
    assert not isinstance(strategy_result, ResearchRefusal)
    assert option_result.status is ResearchStatus.REDUCED
    assert strategy_result.status is ResearchStatus.REDUCED
    assert "saxo_greeks_not_compared" in option_result.warnings
    assert "saxo_greeks_not_compared" in strategy_result.warnings
    option_values = cast("OptionAnalyticsValues", _private(option_result))
    strategy_values = cast("StrategyAnalyticsValues", _private(strategy_result))
    assert option_values.model.value > 0
    assert len(strategy_values.payoff_points) == _PAYOFF_POINT_COUNT
    assert math.isfinite(strategy_values.aggregate_greeks.delta)
    assert option_result.recommendation_authority is False
    assert option_result.order_creation_authority is False
    assert option_result.approval_authority is False
    assert option_result.execution_authority is False
    assert option_result.is_not_forecast is True
    assert not hasattr(option_result, "order")


def test_public_derivative_evidence_redacts_model_and_strategy_values() -> None:
    result = analyze_option_strategy(
        _dataset(),
        _strategy(),
        expiry_reference_prices=(100.0,),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.private_values is None
    assert result.evidence.private_values_redacted is True
    rendered = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert "250.0" not in rendered
    assert "100.0" not in rendered


def test_source_quality_entitlement_and_missing_contracts_fail_closed() -> None:
    stale = analyze_option_model(
        _dataset(quality_state=QualityState.STALE),
        _model_input(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    denied_sources = list(_sources())
    denied_sources[0] = _source("options_chain_reference_v1", entitlement="denied")
    denied = analyze_option_model(
        _dataset(source_bindings=tuple(denied_sources)),
        _model_input(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    missing = analyze_option_model(
        _dataset(
            source_bindings=tuple(
                binding for binding in _sources() if binding.contract_id != "info_price_v1"
            ),
        ),
        _model_input(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(stale, ResearchRefusal)
    assert stale.reason_code == "derivative_source_incomplete"
    assert isinstance(denied, ResearchRefusal)
    assert denied.reason_code == "source_entitlement_insufficient"
    assert isinstance(missing, ResearchRefusal)
    assert missing.reason_code == "source_contract_missing"


def test_current_frozen_catalog_refuses_unbound_saxo_greek_claims() -> None:
    snapshot = SaxoGreekSnapshot(
        instrument_handle=_OPTION,
        observed_at=_AS_OF,
        source_binding=_source("options_chain_reference_v1"),
        greeks=OptionGreeks(
            delta=0.64,
            gamma=0.019,
            theta_per_day=-0.018,
            vega_per_volatility_point=0.38,
            rho_per_rate_point=0.53,
        ),
    )
    result = analyze_option_model(
        _dataset(),
        _model_input(),
        saxo_greeks=snapshot,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "saxo_greeks_source_contract_unbound"


def _surface_request(
    *,
    interpolation_rule: Literal["exact_match", "linear"] = "exact_match",
) -> IvSurfaceRequest:
    first_expiry = _AS_OF + timedelta(days=30)
    return IvSurfaceRequest(
        smile_axis="strike",
        smile_expiry_at=first_expiry,
        smile_points=(
            IvSurfacePoint(
                instrument_handle=_OPTION_TWO,
                expiry_at=first_expiry,
                time_to_expiry_years=30.0 / 365.0,
                strike=110.0,
                signed_delta=0.30,
                moneyness=1.10,
                implied_volatility=0.22,
            ),
            IvSurfacePoint(
                instrument_handle=_OPTION,
                expiry_at=first_expiry,
                time_to_expiry_years=30.0 / 365.0,
                strike=90.0,
                signed_delta=0.70,
                moneyness=0.90,
                implied_volatility=0.25,
            ),
        ),
        term_selector="moneyness",
        term_selector_value=1.0,
        term_interpolation_rule=interpolation_rule,
        term_points=(
            IvSurfacePoint(
                instrument_handle=_OPTION,
                expiry_at=_AS_OF + timedelta(days=90),
                time_to_expiry_years=90.0 / 365.0,
                strike=100.0,
                signed_delta=0.50,
                moneyness=1.0,
                implied_volatility=0.23,
            ),
            IvSurfacePoint(
                instrument_handle=_OPTION_TWO,
                expiry_at=_AS_OF + timedelta(days=30),
                time_to_expiry_years=30.0 / 365.0,
                strike=100.0,
                signed_delta=0.50,
                moneyness=1.0,
                implied_volatility=0.20,
            ),
        ),
    )


def test_smile_skew_and_exact_term_structure_are_deterministic() -> None:
    result = analyze_iv_surface(
        _dataset(),
        _surface_request(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    values = cast("IvSurfaceValues", _private(result))
    assert tuple(point.strike for point in values.smile_points) == (90.0, 110.0)
    assert values.skew == pytest.approx((0.22 - 0.25) / 20.0)
    assert tuple(point.time_to_expiry_years for point in values.term_points) == tuple(
        sorted(point.time_to_expiry_years for point in values.term_points)
    )


def test_surface_duplicate_axis_mixed_expiry_and_interpolation_refuse() -> None:
    request = _surface_request()
    duplicate = request.model_copy(
        update={
            "smile_points": (
                request.smile_points[0],
                request.smile_points[1].model_copy(update={"strike": 110.0}),
            ),
        },
    )
    mixed_expiry = request.model_copy(
        update={
            "smile_points": (
                request.smile_points[0],
                request.smile_points[1].model_copy(
                    update={"expiry_at": _AS_OF + timedelta(days=31)},
                ),
            ),
        },
    )
    results = (
        analyze_iv_surface(
            _dataset(),
            duplicate,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
        ),
        analyze_iv_surface(
            _dataset(),
            mixed_expiry,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
        ),
        analyze_iv_surface(
            _dataset(),
            _surface_request(interpolation_rule="linear"),
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
        ),
    )

    assert all(isinstance(result, ResearchRefusal) for result in results)
    assert tuple(cast("ResearchRefusal", result).reason_code for result in results) == (
        "iv_smile_axis_duplicate",
        "iv_smile_expiry_mismatch",
        "iv_interpolation_unsupported",
    )


def _radar_request() -> LifecycleRadarRequest:
    return LifecycleRadarRequest(
        horizon_days=45,
        positions=(
            LifecyclePosition(
                instrument_handle=_OPTION,
                option_type="call",
                exercise_style="european",
                quantity=2.0,
                strike=95.0,
                reference_price=100.0,
                expiry_at=_AS_OF + timedelta(days=10),
                assignment_state="none",
            ),
            LifecyclePosition(
                instrument_handle=_OPTION_TWO,
                option_type="put",
                exercise_style="european",
                quantity=-1.0,
                strike=105.0,
                reference_price=100.0,
                expiry_at=_AS_OF + timedelta(days=20),
                assignment_state="reported",
            ),
            LifecyclePosition(
                instrument_handle=_OPTION_TWO,
                option_type="call",
                exercise_style="european",
                quantity=1.0,
                strike=110.0,
                reference_price=100.0,
                expiry_at=_AS_OF + timedelta(days=90),
                assignment_state="none",
            ),
        ),
    )


def test_expiry_and_assignment_radar_is_bounded_and_owner_only() -> None:
    result = analyze_lifecycle_radar(
        _dataset(),
        _radar_request(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    values = cast("LifecycleRadarValues", _private(result))
    assert len(values.events) == _RADAR_EVENT_COUNT
    assert values.events[0].days_to_expiry == _FIRST_EXPIRY_DAYS
    assert values.events[1].assignment_label == "reported"
    assert result.is_not_forecast is True


def test_assignment_radar_refuses_american_and_missing_event_source() -> None:
    request = _radar_request()
    american = request.model_copy(
        update={
            "positions": (
                request.positions[0].model_copy(update={"exercise_style": "american"}),
            ),
        },
    )
    american_result = analyze_lifecycle_radar(
        _dataset(),
        american,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    missing_source = analyze_lifecycle_radar(
        _dataset(
            source_bindings=tuple(
                binding
                for binding in _sources()
                if binding.contract_id != "corporate_action_events_v2"
            ),
        ),
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(american_result, ResearchRefusal)
    assert american_result.reason_code == "option_american_unsupported"
    assert isinstance(missing_source, ResearchRefusal)
    assert missing_source.reason_code == "source_contract_missing"


def _futures_request(
    *,
    spot_price: float = 100.0,
    near_price: float = 102.0,
    later_price: float = 104.0,
    eligible_roll_costs: float = 1.0,
) -> FuturesCurveRequest:
    return FuturesCurveRequest(
        spot_instrument_handle=_UNDERLYING,
        spot_price=spot_price,
        near_contract=FuturesContractPoint(
            instrument_handle=_FUTURES_NEAR,
            expiry_at=_AS_OF + timedelta(days=182),
            time_to_expiry_years=0.5,
            price=near_price,
        ),
        later_contract=FuturesContractPoint(
            instrument_handle=_FUTURES_LATER,
            expiry_at=_AS_OF + timedelta(days=365),
            time_to_expiry_years=1.0,
            price=later_price,
        ),
        quantity=2.0,
        contract_multiplier=10.0,
        eligible_roll_costs=eligible_roll_costs,
        reporting_currency="USD",
    )


def test_futures_basis_carry_term_structure_and_roll_follow_frozen_formulas() -> None:
    result = analyze_futures_curve(
        _dataset(),
        _futures_request(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    values = cast("FuturesCurveValues", _private(result))
    assert values.basis == _EXPECTED_BASIS
    assert values.annualized_carry == pytest.approx(0.04)
    assert values.term_structure == pytest.approx(104.0 / 102.0 - 1.0)
    assert values.modeled_roll == _EXPECTED_ROLL


def test_futures_zero_basis_is_zero_and_invalid_ordering_refuses() -> None:
    zero_result = analyze_futures_curve(
        _dataset(),
        _futures_request(
            spot_price=100.0,
            near_price=100.0,
            later_price=100.0,
            eligible_roll_costs=0.0,
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    request = _futures_request()
    invalid = request.model_copy(
        update={
            "later_contract": request.later_contract.model_copy(
                update={"time_to_expiry_years": 0.25},
            ),
        },
    )
    invalid_result = analyze_futures_curve(
        _dataset(),
        invalid,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(zero_result, ResearchRefusal)
    zero = cast("FuturesCurveValues", _private(zero_result))
    assert (zero.basis, zero.annualized_carry, zero.term_structure, zero.modeled_roll) == (
        0.0,
        0.0,
        0.0,
        0.0,
    )
    assert isinstance(invalid_result, ResearchRefusal)
    assert invalid_result.reason_code == "futures_curve_order_invalid"


def _fx_request(
    *,
    base_rate: float = 0.02,
    quote_rate: float = 0.04,
    rate_model: Literal["simple", "complex"] = "simple",
) -> FxForwardRequest:
    return FxForwardRequest(
        spot_instrument_handle=_UNDERLYING,
        base_currency="USD",
        quote_currency="DKK",
        spot=_FX_SPOT,
        time_to_expiry_years=0.5,
        base_rate=base_rate,
        quote_rate=quote_rate,
        rate_model=rate_model,
    )


def test_fx_forward_and_carry_follow_covered_interest_parity() -> None:
    result = analyze_fx_forward(
        _dataset(),
        _fx_request(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    equal_rates = analyze_fx_forward(
        _dataset(),
        _fx_request(base_rate=0.03, quote_rate=0.03),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert not isinstance(equal_rates, ResearchRefusal)
    values = cast("FxForwardValues", _private(result))
    equal_values = cast("FxForwardValues", _private(equal_rates))
    expected = _FX_SPOT * (1.0 + 0.04 * 0.5) / (1.0 + 0.02 * 0.5)
    assert values.forward == pytest.approx(expected)
    assert values.annualized_carry == pytest.approx(
        (expected / _FX_SPOT - 1.0) / 0.5,
    )
    assert equal_values.forward == _FX_SPOT
    assert equal_values.annualized_carry == 0.0


def test_complex_rate_fx_forward_refuses_exact_capability() -> None:
    result = analyze_fx_forward(
        _dataset(),
        _fx_request(rate_model="complex"),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "fx_complex_rates_unsupported"


def test_derivative_requests_reject_code_paths_network_and_order_authority() -> None:
    request_data = _futures_request().model_dump()
    for field, value in (
        ("sql", "select futures"),
        ("python", "lambda curve: curve"),
        ("expression", "near - far"),
        ("path", "relative/curve.json"),
        ("network_callback", "https://example.invalid"),
        ("order", {"side": "Sell"}),
    ):
        with pytest.raises(ValidationError):
            FuturesCurveRequest.model_validate({**request_data, field: value})
