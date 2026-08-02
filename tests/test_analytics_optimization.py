from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_optimization import (
    AssetClassConstraint,
    CovariancePerturbation,
    CurrencyConstraint,
    OptimizationAsset,
    OptimizationDataset,
    OptimizationRequest,
    PortfolioOptimizationResult,
    PrivateOptimizationValues,
    SolverSettings,
    optimize_portfolio,
)
from saxo_bank_mcp.analytics_optimizer_reference import (
    reference_portfolio_variance,
    reference_two_asset_minimum_variance,
    reference_two_asset_risk_parity,
)
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "analytics" / "golden_optimizations.json"
_ALIAS = "aa_00000000000040008000000000000061"
_DATASET = "ds_00000000000040008000000000000061"
_SNAPSHOT = "ps_00000000000040008000000000000061"
_HANDLES = tuple(f"ih_0000000000004000800000000000006{index}" for index in range(1, 6))
_START = datetime(2025, 1, 1, tzinfo=UTC)
_END = datetime(2026, 1, 1, tzinfo=UTC)
_AS_OF = datetime(2026, 1, 2, tzinfo=UTC)
_TOLERANCE = Decimal("0.000001")


class _GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    covariance: tuple[tuple[Decimal, ...], ...]
    current_weights: tuple[Decimal, ...] | None = None
    minimum_variance_weights: tuple[Decimal, ...]
    minimum_variance_objective: Decimal
    minimum_variance_turnover: Decimal | None = None
    risk_parity_weights: tuple[Decimal, ...] | None = None
    risk_parity_contributions: tuple[Decimal, ...] | None = None


class _GoldenFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    diagonal_two_asset: _GoldenCase
    correlated_two_asset: _GoldenCase


def _golden() -> _GoldenFixture:
    return _GoldenFixture.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


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
        source_revision="fixture:t16",
        capture_fingerprint_sha256={
            "chart_v3": "1",
            "positions_v1": "2",
            "exposure_instruments_v1": "3",
            "balances_v1": "4",
            "costs_v1": "5",
        }[contract_id]
        * 64,
        quality_state=quality,
        entitlement_state=entitlement,
    )


def _sources() -> tuple[SaxoSourceBinding, ...]:
    return tuple(
        _source(contract_id)
        for contract_id in (
            "chart_v3",
            "positions_v1",
            "exposure_instruments_v1",
            "balances_v1",
            "costs_v1",
        )
    )


def _asset(  # noqa: PLR0913
    index: int,
    current_weight: Decimal,
    *,
    asset_class: str = "equity",
    currency: str = "USD",
    lower_bound: Decimal = Decimal(0),
    upper_bound: Decimal = Decimal(1),
    cost_rate: Decimal = Decimal("0.01"),
    margin_rate: Decimal = Decimal("0.10"),
    minimum_trade: Decimal = Decimal(0),
    excluded: bool = False,
) -> OptimizationAsset:
    return OptimizationAsset(
        account_alias=_ALIAS,
        instrument_handle=_HANDLES[index],
        asset_class=asset_class,
        currency=currency,
        current_weight=current_weight,
        expected_return=Decimal("0.05"),
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        transaction_cost_rate=cost_rate,
        margin_requirement_rate=margin_rate,
        minimum_trade_weight=minimum_trade,
        excluded=excluded,
    )


def _dataset(  # noqa: PLR0913
    covariance: tuple[tuple[Decimal, ...], ...],
    current_weights: tuple[Decimal, ...],
    *,
    assets: tuple[OptimizationAsset, ...] | None = None,
    source_bindings: tuple[SaxoSourceBinding, ...] | None = None,
    quality_state: QualityState = QualityState.COMPLETE,
    sample_count: int = 60,
) -> OptimizationDataset:
    asset_values = assets or tuple(
        _asset(index, weight) for index, weight in enumerate(current_weights)
    )
    return OptimizationDataset(
        dataset_id=_DATASET,
        snapshot_id=_SNAPSHOT,
        account_alias=_ALIAS,
        estimation_start_at=_START,
        estimation_end_at=_END,
        as_of=_AS_OF,
        reporting_currency="USD",
        assets=asset_values,
        covariance_matrix=covariance,
        sample_count=sample_count,
        return_model="historical_arithmetic",
        covariance_model="sample_covariance",
        source_bindings=_sources() if source_bindings is None else source_bindings,
        quality_state=quality_state,
        missing_fields=("returns.before_start",) if quality_state is QualityState.PARTIAL else (),
        warnings=(),
    )


def _settings() -> SolverSettings:
    return SolverSettings(
        method="SLSQP",
        maximum_iterations=1000,
        objective_tolerance=Decimal("0.00000001"),
        feasibility_tolerance=Decimal("0.00000001"),
        kkt_tolerance=Decimal("0.00001"),
    )


def _request(  # noqa: PLR0913
    dataset: OptimizationDataset,
    *,
    objective: Literal["minimum_variance", "risk_parity"] = "minimum_variance",
    short_policy: Literal["long_only", "bounded_short"] = "long_only",
    asset_class_constraints: tuple[AssetClassConstraint, ...] = (),
    currency_constraints: tuple[CurrencyConstraint, ...] = (),
    maximum_turnover: Decimal = Decimal(2),
    maximum_cost: Decimal = Decimal(2),
    maximum_margin: Decimal = Decimal(2),
    perturbations: tuple[CovariancePerturbation, ...] | None = None,
    objective_confirmed: bool = True,
    constraints_confirmed: bool = True,
    concentration_threshold: Decimal = Decimal("0.95"),
    stability_warning_threshold: Decimal = Decimal("0.05"),
    stability_refusal_threshold: Decimal = Decimal("0.25"),
) -> OptimizationRequest:
    perturbation_values = perturbations or (
        CovariancePerturbation(
            perturbation_id="identity_check",
            covariance_matrix=dataset.covariance_matrix,
        ),
    )
    return OptimizationRequest(
        dataset=dataset,
        objective=objective,
        objective_confirmed_by_caller=objective_confirmed,
        constraints_confirmed_by_caller=constraints_confirmed,
        short_policy=short_policy,
        asset_class_constraints=asset_class_constraints,
        currency_constraints=currency_constraints,
        maximum_turnover=maximum_turnover,
        maximum_transaction_cost_ratio=maximum_cost,
        maximum_margin_ratio=maximum_margin,
        perturbations=perturbation_values,
        solver_settings=_settings(),
        lexicographic_tie_break_rule="asset_order_within_objective_tolerance",
        concentration_warning_threshold=concentration_threshold,
        condition_number_warning_threshold=Decimal(1000000),
        stability_warning_threshold=stability_warning_threshold,
        stability_refusal_threshold=stability_refusal_threshold,
    )


def _private(
    request: OptimizationRequest,
) -> tuple[PortfolioOptimizationResult, PrivateOptimizationValues]:
    result = optimize_portfolio(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    assert not isinstance(result, ResearchRefusal), (
        result.reason_code,
        result.reason,
    )
    assert result.private_values is not None
    return result, result.private_values


def _weights(values: PrivateOptimizationValues) -> tuple[Decimal, ...]:
    return tuple(item.target_weight for item in values.targets)


def _assert_close(actual: Decimal, expected: Decimal, tolerance: Decimal = _TOLERANCE) -> None:
    assert abs(actual - expected) <= tolerance


def test_minimum_variance_known_answer_reference_and_kkt_residuals() -> None:
    case = _golden().diagonal_two_asset
    assert case.current_weights is not None
    result, values = _private(
        _request(_dataset(case.covariance, case.current_weights)),
    )
    reference = reference_two_asset_minimum_variance(case.covariance)

    assert result.status is ResearchStatus.COMPLETE
    for actual, expected, independent in zip(
        _weights(values),
        case.minimum_variance_weights,
        reference,
        strict=True,
    ):
        _assert_close(actual, expected)
        _assert_close(actual, independent)
    _assert_close(values.objective_value, case.minimum_variance_objective)
    assert case.minimum_variance_turnover is not None
    _assert_close(values.turnover, case.minimum_variance_turnover)
    assert values.diagnostics.feasibility_residual <= Decimal("0.00000001")
    assert values.diagnostics.kkt_residual <= Decimal("0.00001")
    assert abs(sum(_weights(values), Decimal(0)) - Decimal(1)) <= Decimal("0.00000001")
    for target in values.targets:
        assert target.current_weight + target.current_to_target_delta == target.target_weight


def test_risk_parity_known_answer_and_normalized_contributions() -> None:
    case = _golden().diagonal_two_asset
    assert case.current_weights is not None
    assert case.risk_parity_weights is not None
    assert case.risk_parity_contributions is not None
    result, values = _private(
        _request(
            _dataset(case.covariance, case.current_weights),
            objective="risk_parity",
        ),
    )
    reference = reference_two_asset_risk_parity(case.covariance)

    assert result.status is ResearchStatus.COMPLETE
    for target, expected_weight, expected_contribution, independent in zip(
        values.targets,
        case.risk_parity_weights,
        case.risk_parity_contributions,
        reference,
        strict=True,
    ):
        _assert_close(target.target_weight, expected_weight)
        _assert_close(target.target_weight, independent)
        assert target.normalized_risk_contribution is not None
        _assert_close(target.normalized_risk_contribution, expected_contribution)
    _assert_close(
        sum(
            (item.normalized_risk_contribution or Decimal(0) for item in values.targets),
            Decimal(0),
        ),
        Decimal(1),
    )
    assert values.diagnostics.kkt_residual <= Decimal("0.00001")


def test_correlated_golden_case_matches_independent_reference() -> None:
    case = _golden().correlated_two_asset
    current = (Decimal("0.5"), Decimal("0.5"))
    _, values = _private(_request(_dataset(case.covariance, current)))
    independent = reference_two_asset_minimum_variance(case.covariance)

    for actual, expected, reference in zip(
        _weights(values),
        case.minimum_variance_weights,
        independent,
        strict=True,
    ):
        _assert_close(actual, expected)
        _assert_close(actual, reference)
    _assert_close(values.objective_value, case.minimum_variance_objective)
    _assert_close(
        reference_portfolio_variance(case.covariance, independent),
        case.minimum_variance_objective,
    )


def test_all_typed_constraints_are_feasible_and_reconciled() -> None:
    assets = (
        _asset(0, Decimal("0.25"), upper_bound=Decimal("0.25"), minimum_trade=Decimal("0.05")),
        _asset(
            1,
            Decimal("0.25"),
            currency="EUR",
            lower_bound=Decimal("0.35"),
            upper_bound=Decimal("0.35"),
            minimum_trade=Decimal("0.05"),
        ),
        _asset(
            2,
            Decimal("0.25"),
            asset_class="bond",
            lower_bound=Decimal("0.40"),
            upper_bound=Decimal("0.40"),
            minimum_trade=Decimal("0.05"),
        ),
        _asset(
            3,
            Decimal("0.25"),
            asset_class="cash",
            upper_bound=Decimal(0),
            minimum_trade=Decimal("0.05"),
            excluded=True,
        ),
    )
    covariance = (
        (Decimal("0.04"), Decimal(0), Decimal(0), Decimal(0)),
        (Decimal(0), Decimal("0.05"), Decimal(0), Decimal(0)),
        (Decimal(0), Decimal(0), Decimal("0.01"), Decimal(0)),
        (Decimal(0), Decimal(0), Decimal(0), Decimal("0.02")),
    )
    _, values = _private(
        _request(
            _dataset(covariance, tuple(asset.current_weight for asset in assets), assets=assets),
            asset_class_constraints=(
                AssetClassConstraint(
                    asset_class="equity",
                    aggregation="net_weight",
                    minimum_weight=Decimal("0.60"),
                    maximum_weight=Decimal("0.60"),
                ),
            ),
            currency_constraints=(
                CurrencyConstraint(
                    currency="EUR",
                    aggregation="net_weight",
                    minimum_weight=Decimal("0.35"),
                    maximum_weight=Decimal("0.35"),
                ),
            ),
            maximum_turnover=Decimal("0.25"),
            maximum_cost=Decimal("0.005"),
            maximum_margin=Decimal("0.15"),
        ),
    )

    assert _weights(values) == (
        Decimal("0.25"),
        Decimal("0.35"),
        Decimal("0.40"),
        Decimal(0),
    )
    assert values.turnover == Decimal("0.250")
    assert values.estimated_transaction_cost_ratio == Decimal("0.00500")
    assert values.margin_ratio == Decimal("0.1000")
    assert values.diagnostics.feasibility_residual <= Decimal("0.00000001")
    assert all(
        target.current_to_target_delta == 0
        or abs(target.current_to_target_delta) >= assets[index].minimum_trade_weight
        for index, target in enumerate(values.targets)
    )


def test_bounded_short_and_long_only_produce_their_known_boundary_solutions() -> None:
    covariance = (
        (Decimal("0.04"), Decimal("0.015")),
        (Decimal("0.015"), Decimal("0.01")),
    )
    current = (Decimal("0.5"), Decimal("0.5"))
    bounded_assets = (
        _asset(0, current[0], lower_bound=Decimal("-0.5"), upper_bound=Decimal("1.5")),
        _asset(1, current[1], lower_bound=Decimal("-0.5"), upper_bound=Decimal("1.5")),
    )
    _, bounded = _private(
        _request(
            _dataset(covariance, current, assets=bounded_assets),
            short_policy="bounded_short",
        ),
    )
    long_assets = (
        _asset(0, current[0]),
        _asset(1, current[1]),
    )
    _, long_only = _private(_request(_dataset(covariance, current, assets=long_assets)))

    _assert_close(bounded.targets[0].target_weight, Decimal("-0.25"))
    _assert_close(bounded.targets[1].target_weight, Decimal("1.25"))
    _assert_close(long_only.targets[0].target_weight, Decimal(0))
    _assert_close(long_only.targets[1].target_weight, Decimal(1))


def test_more_restrictive_position_bound_cannot_improve_minimum_variance() -> None:
    case = _golden().diagonal_two_asset
    assert case.current_weights is not None
    _, unconstrained = _private(_request(_dataset(case.covariance, case.current_weights)))
    constrained_assets = (
        _asset(0, Decimal("0.5"), lower_bound=Decimal("0.40")),
        _asset(1, Decimal("0.5"), upper_bound=Decimal("0.60")),
    )
    _, constrained = _private(
        _request(
            _dataset(case.covariance, case.current_weights, assets=constrained_assets),
        ),
    )

    assert constrained.objective_value >= unconstrained.objective_value
    _assert_close(constrained.targets[0].target_weight, Decimal("0.40"))
    _assert_close(constrained.targets[1].target_weight, Decimal("0.60"))


def test_minimum_trade_disjunction_is_solved_without_rounding() -> None:
    case = _golden().diagonal_two_asset
    current = (Decimal("0.5"), Decimal("0.5"))
    assets = (
        _asset(0, current[0], minimum_trade=Decimal("0.4")),
        _asset(1, current[1], minimum_trade=Decimal("0.4")),
    )
    _, values = _private(_request(_dataset(case.covariance, current, assets=assets)))

    _assert_close(values.targets[0].target_weight, Decimal("0.1"))
    _assert_close(values.targets[1].target_weight, Decimal("0.9"))
    assert all(abs(target.current_to_target_delta) == Decimal("0.4") for target in values.targets)


def test_explicit_perturbations_produce_reproducible_stability_identity() -> None:
    case = _golden().diagonal_two_asset
    assert case.current_weights is not None
    perturbed = (
        (Decimal("0.041"), Decimal(0)),
        (Decimal(0), Decimal("0.009")),
    )
    request = _request(
        _dataset(case.covariance, case.current_weights),
        perturbations=(
            CovariancePerturbation(
                perturbation_id="diagonal_shift",
                covariance_matrix=perturbed,
            ),
        ),
    )
    first_result, first = _private(request)
    second_result, second = _private(request)
    _, perturbed_values = _private(
        _request(_dataset(perturbed, case.current_weights)),
    )
    expected_stability = max(
        abs(base.target_weight - changed.target_weight)
        for base, changed in zip(first.targets, perturbed_values.targets, strict=True)
    )

    assert first_result.model_dump_json() == second_result.model_dump_json()
    assert first == second
    _assert_close(first.diagnostics.maximum_perturbation_weight_change, expected_stability)
    assert first.diagnostics.maximum_perturbation_weight_change > 0


def test_concentrated_covariance_reduces_with_diagnostic_warning() -> None:
    covariance = (
        (Decimal("0.0001"), Decimal(0)),
        (Decimal(0), Decimal(1)),
    )
    result, values = _private(
        _request(
            _dataset(covariance, (Decimal("0.5"), Decimal("0.5"))),
            concentration_threshold=Decimal("0.95"),
            stability_refusal_threshold=Decimal(1),
        ),
    )

    assert result.status is ResearchStatus.REDUCED
    assert "optimizer_concentrated_weights" in result.warnings
    assert values.diagnostics.maximum_absolute_target_weight > Decimal("0.99")


def test_singular_covariance_and_infeasible_constraints_refuse() -> None:
    singular = (
        (Decimal("0.04"), Decimal("0.02")),
        (Decimal("0.02"), Decimal("0.01")),
    )
    singular_result = optimize_portfolio(
        _request(_dataset(singular, (Decimal("0.5"), Decimal("0.5")))),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    bounded_assets = (
        _asset(0, Decimal("0.5"), upper_bound=Decimal("0.4")),
        _asset(1, Decimal("0.5"), upper_bound=Decimal("0.4")),
    )
    infeasible = optimize_portfolio(
        _request(
            _dataset(
                _golden().diagonal_two_asset.covariance,
                (Decimal("0.5"), Decimal("0.5")),
                assets=bounded_assets,
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(singular_result, ResearchRefusal)
    assert singular_result.reason_code == "optimizer_covariance_singular"
    assert isinstance(infeasible, ResearchRefusal)
    assert infeasible.reason_code == "optimizer_constraints_infeasible"


def test_short_enabled_risk_parity_and_unconfirmed_choices_refuse() -> None:
    case = _golden().diagonal_two_asset
    assert case.current_weights is not None
    short_assets = (
        _asset(0, case.current_weights[0], lower_bound=Decimal("-0.5")),
        _asset(1, case.current_weights[1], lower_bound=Decimal("-0.5")),
    )
    unsupported = optimize_portfolio(
        _request(
            _dataset(case.covariance, case.current_weights, assets=short_assets),
            objective="risk_parity",
            short_policy="bounded_short",
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    unconfirmed = optimize_portfolio(
        _request(
            _dataset(case.covariance, case.current_weights),
            objective_confirmed=False,
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(unsupported, ResearchRefusal)
    assert unsupported.reason_code == "optimizer_risk_parity_shorting_unsupported"
    assert isinstance(unconfirmed, ResearchRefusal)
    assert unconfirmed.reason_code == "optimizer_caller_selection_required"


def test_source_quality_privacy_and_no_order_authority_are_fail_closed() -> None:
    case = _golden().diagonal_two_asset
    assert case.current_weights is not None
    partial = optimize_portfolio(
        _request(
            _dataset(case.covariance, case.current_weights, quality_state=QualityState.PARTIAL),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    denied_sources = list(_sources())
    denied_sources[0] = _source("chart_v3", entitlement="denied")
    denied = optimize_portfolio(
        _request(
            _dataset(
                case.covariance,
                case.current_weights,
                source_bindings=tuple(denied_sources),
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    public = optimize_portfolio(
        _request(_dataset(case.covariance, case.current_weights)),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert isinstance(partial, ResearchRefusal)
    assert partial.reason_code == "optimizer_calibration_incomplete"
    assert isinstance(denied, ResearchRefusal)
    assert denied.reason_code == "source_entitlement_insufficient"
    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    assert public.evidence.private_values_redacted is True
    rendered = json.dumps(public.model_dump(mode="json"), sort_keys=True)
    assert "0.2" not in rendered
    assert "0.8" not in rendered
    assert public.proposal_only is True
    assert public.objective_selected_by_engine is False
    assert public.risk_tolerance_selected_by_engine is False
    assert public.order_creation_authority is False
    assert public.approval_authority is False
    assert public.execution_authority is False
    assert not hasattr(public, "order")
    assert not hasattr(public, "quantity")


def test_optimizer_inputs_reject_code_paths_network_and_private_fixture_material() -> None:
    case = _golden().diagonal_two_asset
    assert case.current_weights is not None
    request_data = _request(_dataset(case.covariance, case.current_weights)).model_dump()
    for field, value in (
        ("sql", "select * from positions"),
        ("python", "lambda x: x"),
        ("expression", "min(weights @ covariance @ weights)"),
        ("path", "relative/input.json"),
        ("network_callback", "https://example.invalid"),
    ):
        with pytest.raises(ValidationError):
            OptimizationRequest.model_validate({**request_data, field: value})
    fixture_text = _FIXTURE_PATH.read_text(encoding="utf-8").lower()
    for forbidden in (
        "accountkey",
        "clientkey",
        "token",
        "balance",
        "holding",
        "orderid",
    ):
        assert forbidden not in fixture_text


def test_optimizer_rejects_excessive_minimum_trade_branch_space() -> None:
    count = 5
    weight = Decimal(1) / Decimal(count)
    covariance = tuple(
        tuple(Decimal("0.01") if row == column else Decimal(0) for column in range(count))
        for row in range(count)
    )
    assets = tuple(
        _asset(index, weight, minimum_trade=Decimal("0.01")) for index in range(count)
    )
    result = optimize_portfolio(
        _request(
            _dataset(covariance, tuple(asset.current_weight for asset in assets), assets=assets),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "optimizer_minimum_trade_branch_limit"
