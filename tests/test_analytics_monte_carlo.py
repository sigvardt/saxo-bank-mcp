from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_monte_carlo import (
    BootstrapGoalRequest,
    CashFlowPeriod,
    ReturnCalibrationDataset,
    ReturnObservation,
    run_bootstrap_goal_model,
)
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS = "aa_00000000000040008000000000000054"
_DATASET = "ds_00000000000040008000000000000054"
_START = datetime(2026, 1, 1, tzinfo=UTC)
_AS_OF = datetime(2026, 2, 1, tzinfo=UTC)


def _source(
    *,
    quality: QualityState = QualityState.COMPLETE,
    entitlement: Literal["available", "partial", "denied"] = "available",
) -> SaxoSourceBinding:
    contract = source_contracts_by_id()["performance_timeseries_v4"]
    return SaxoSourceBinding(
        contract_id=contract.contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t15",
        capture_fingerprint_sha256="f" * 64,
        quality_state=quality,
        entitlement_state=entitlement,
    )


def _observations(
    pattern: tuple[Decimal, ...] = (
        Decimal("-0.03"),
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.04"),
        Decimal("-0.01"),
    ),
) -> tuple[ReturnObservation, ...]:
    return tuple(
        ReturnObservation(
            at=_START + timedelta(days=index),
            return_ratio=pattern[index % len(pattern)],
        )
        for index in range(30)
    )


def _dataset(
    *,
    observations: tuple[ReturnObservation, ...] | None = None,
    quality_state: QualityState = QualityState.COMPLETE,
    source_bindings: tuple[SaxoSourceBinding, ...] | None = None,
) -> ReturnCalibrationDataset:
    return ReturnCalibrationDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        as_of=_AS_OF,
        reporting_currency="USD",
        observations=observations or _observations(),
        source_bindings=(_source(),) if source_bindings is None else source_bindings,
        quality_state=quality_state,
        missing_fields=("TimeWeightedReturn",) if quality_state is QualityState.PARTIAL else (),
        warnings=(),
    )


def _schedule(
    horizon: int,
    *,
    contribution: Decimal = Decimal(0),
    withdrawal: Decimal = Decimal(0),
) -> tuple[CashFlowPeriod, ...]:
    return tuple(
        CashFlowPeriod(
            period_index=index,
            contribution=contribution,
            withdrawal=withdrawal,
        )
        for index in range(1, horizon + 1)
    )


def _request(  # noqa: PLR0913
    *,
    dataset: ReturnCalibrationDataset | None = None,
    starting_value: Decimal = Decimal(100),
    goal_value: Decimal = Decimal(110),
    ruin_threshold: Decimal = Decimal(20),
    horizon_periods: int = 12,
    path_count: int = 400,
    block_length: int = 3,
    seed: int = 2026080201,
    cash_flows: tuple[CashFlowPeriod, ...] | None = None,
    include_inflation_adjustment: bool = False,
    annual_inflation_assumption: Decimal | None = None,
    horizon_years: Decimal = Decimal(1),
) -> BootstrapGoalRequest:
    return BootstrapGoalRequest(
        calibration=dataset or _dataset(),
        starting_value=starting_value,
        explicit_goal=goal_value,
        explicit_ruin_threshold=ruin_threshold,
        horizon_periods=horizon_periods,
        horizon_years=horizon_years,
        path_count=path_count,
        block_length=block_length,
        random_seed=seed,
        cash_flow_timing="start_of_period",
        cash_flows=cash_flows or _schedule(horizon_periods),
        include_inflation_adjustment=include_inflation_adjustment,
        annual_inflation_assumption=annual_inflation_assumption,
    )


def _private(request: BootstrapGoalRequest):  # noqa: ANN202
    result = run_bootstrap_goal_model(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    assert not isinstance(result, ResearchRefusal)
    assert result.private_values is not None
    return result, result.private_values


def test_zero_return_limit_has_exact_goal_ruin_and_sequence_results() -> None:
    request = _request(
        dataset=_dataset(observations=_observations((Decimal(0),))),
        starting_value=Decimal(100),
        goal_value=Decimal(100),
        ruin_threshold=Decimal(99),
        horizon_periods=6,
        path_count=40,
        block_length=1,
        cash_flows=_schedule(6),
    )
    result, values = _private(request)

    assert result.status is ResearchStatus.COMPLETE
    assert values.ending_values.minimum == Decimal(100)
    assert values.ending_values.lower == Decimal(100)
    assert values.ending_values.median == Decimal(100)
    assert values.ending_values.upper == Decimal(100)
    assert values.ending_values.maximum == Decimal(100)
    assert values.goal_probability == Decimal(1)
    assert values.ruin_probability == Decimal(0)
    assert values.ordered_failure_probability == Decimal(0)
    assert values.order_neutral_failure_probability == Decimal(0)
    assert values.sequence_of_returns_risk == Decimal(0)


def test_same_seed_is_byte_stable_and_probability_outputs_are_not_predictions() -> None:
    request = _request()
    first, first_values = _private(request)
    second, second_values = _private(request)
    _, changed_seed_values = _private(_request(seed=2026080202))

    assert first.model_dump_json() == second.model_dump_json()
    assert first_values == second_values
    assert first_values != changed_seed_values
    assert first.is_not_forecast is True
    assert first.model_distribution_only is True
    assert first.probability_label == "seeded_block_bootstrap_model_distribution_not_prediction"
    assert first.prediction_claim is False
    assert first.does_not_verify == ("future_goal_attainment", "future_returns")


def test_positive_scale_changes_values_only_and_higher_goal_is_never_easier() -> None:
    base_request = _request(
        cash_flows=_schedule(12, contribution=Decimal(3), withdrawal=Decimal(1)),
    )
    _, base = _private(base_request)
    _, scaled = _private(
        _request(
            starting_value=Decimal(1000),
            goal_value=Decimal(1100),
            ruin_threshold=Decimal(200),
            cash_flows=_schedule(12, contribution=Decimal(30), withdrawal=Decimal(10)),
        ),
    )
    _, higher_goal = _private(
        _request(
            goal_value=Decimal(130),
            cash_flows=_schedule(12, contribution=Decimal(3), withdrawal=Decimal(1)),
        ),
    )

    assert scaled.ending_values.minimum == base.ending_values.minimum * 10
    assert scaled.ending_values.lower == base.ending_values.lower * 10
    assert scaled.ending_values.median == base.ending_values.median * 10
    assert scaled.ending_values.upper == base.ending_values.upper * 10
    assert scaled.ending_values.maximum == base.ending_values.maximum * 10
    assert scaled.goal_probability == base.goal_probability
    assert scaled.ruin_probability == base.ruin_probability
    assert scaled.sequence_of_returns_risk == base.sequence_of_returns_risk
    assert higher_goal.goal_probability <= base.goal_probability


def test_sequence_model_detects_order_effect_with_flows_and_zero_without_flows() -> None:
    returns = _observations(
        (
            Decimal("-0.40"),
            Decimal("0.05"),
            Decimal("0.05"),
            Decimal("0.05"),
            Decimal("0.05"),
        ),
    )
    withdrawals = tuple(
        CashFlowPeriod(
            period_index=index,
            contribution=Decimal(0),
            withdrawal=Decimal("0.5"),
        )
        for index in range(1, 31)
    )
    calibration = _dataset(observations=returns)
    _, with_flows = _private(
        _request(
            dataset=calibration,
            starting_value=Decimal(100),
            goal_value=Decimal(8),
            ruin_threshold=Decimal(1),
            horizon_periods=30,
            path_count=500,
            block_length=30,
            cash_flows=withdrawals,
        ),
    )
    _, without_flows = _private(
        _request(
            dataset=calibration,
            starting_value=Decimal(100),
            goal_value=Decimal(8),
            ruin_threshold=Decimal(1),
            horizon_periods=30,
            path_count=500,
            block_length=30,
            cash_flows=_schedule(30),
        ),
    )

    assert with_flows.sequence_of_returns_risk != Decimal(0)
    assert (
        with_flows.sequence_of_returns_risk
        == with_flows.ordered_failure_probability
        - with_flows.order_neutral_failure_probability
    )
    assert without_flows.sequence_of_returns_risk == Decimal(0)


def test_inflation_adjustment_requires_an_explicit_number_and_is_monotone() -> None:
    missing = run_bootstrap_goal_model(
        _request(include_inflation_adjustment=True),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    _, zero = _private(
        _request(
            include_inflation_adjustment=True,
            annual_inflation_assumption=Decimal(0),
        ),
    )
    _, positive = _private(
        _request(
            include_inflation_adjustment=True,
            annual_inflation_assumption=Decimal("0.05"),
        ),
    )

    assert isinstance(missing, ResearchRefusal)
    assert missing.reason_code == "inflation_assumption_required"
    assert zero.inflation_adjusted_ending_values == zero.ending_values
    assert positive.inflation_adjusted_ending_values is not None
    assert positive.inflation_adjusted_ending_values.median < positive.ending_values.median


def test_model_refuses_incomplete_or_unusable_calibration_and_undefined_arithmetic() -> None:
    partial = run_bootstrap_goal_model(
        _request(dataset=_dataset(quality_state=QualityState.PARTIAL)),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    denied = run_bootstrap_goal_model(
        _request(
            dataset=_dataset(source_bindings=(_source(entitlement="denied"),)),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    missing_source = run_bootstrap_goal_model(
        _request(dataset=_dataset(source_bindings=())),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    extreme = run_bootstrap_goal_model(
        _request(
            dataset=_dataset(observations=_observations((Decimal("0.9"),))),
            starting_value=Decimal("9e999999"),
            goal_value=Decimal("1e999999"),
            ruin_threshold=Decimal(0),
            horizon_periods=2,
            path_count=2,
            block_length=1,
            cash_flows=_schedule(2),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(partial, ResearchRefusal)
    assert partial.reason_code == "model_calibration_incomplete"
    assert isinstance(denied, ResearchRefusal)
    assert denied.reason_code == "source_entitlement_insufficient"
    assert isinstance(missing_source, ResearchRefusal)
    assert missing_source.reason_code == "source_contract_missing"
    assert isinstance(extreme, ResearchRefusal)
    assert extreme.reason_code == "model_output_undefined"


def test_goal_model_public_evidence_is_value_free_and_inputs_are_bounded() -> None:
    public = run_bootstrap_goal_model(
        _request(starting_value=Decimal(123456), goal_value=Decimal(654321)),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    assert public.evidence.private_values_redacted is True
    rendered = json.dumps(public.model_dump(mode="json"), sort_keys=True)
    assert "123456" not in rendered
    assert "654321" not in rendered
    assert "goal_probability" not in rendered
    base = _request().model_dump()
    for field, value in (
        ("sql", "select * from returns"),
        ("python", "lambda x: x"),
        ("expression", "goal / value"),
        ("path", "relative/input.csv"),
        ("network_callback", "https://example.invalid"),
    ):
        with pytest.raises(ValidationError):
            BootstrapGoalRequest.model_validate({**base, field: value})


@pytest.mark.parametrize("seed", [True, -1, 2**64])
def test_seed_must_be_an_explicit_unsigned_64_bit_integer(seed: object) -> None:
    with pytest.raises(ValidationError):
        BootstrapGoalRequest.model_validate(
            {**_request().model_dump(), "random_seed": seed},
        )


def test_calibration_order_sample_count_returns_and_cash_flow_schedule_are_exact() -> None:
    with pytest.raises(ValidationError, match="at least 30"):
        _dataset(observations=_observations()[:29])
    unordered = list(_observations())
    unordered[0], unordered[1] = unordered[1], unordered[0]
    with pytest.raises(ValidationError, match="strictly ordered"):
        _dataset(observations=tuple(unordered))
    with pytest.raises(ValidationError):
        ReturnObservation(at=_START, return_ratio=Decimal("-1.01"))
    with pytest.raises(ValidationError, match="complete"):
        _request(cash_flows=_schedule(11))
