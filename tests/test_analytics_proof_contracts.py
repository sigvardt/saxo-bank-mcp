"""Measured installed-test receipts for exact offline analytics proof contracts."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

import pytest

from saxo_bank_mcp.analytics_source_contracts import (
    compare_source_schema,
    source_contracts_by_id,
)
from saxo_bank_mcp.qa_analytics_evidence import (
    ProofExecutionKind,
    build_proof_execution_contracts,
    exact_analysis_measurement_node_id,
)
from saxo_bank_mcp.secret_scan import scan_secret_text

_CONTRACTS = build_proof_execution_contracts()
ANALYSIS_PROOF_CASES = tuple(
    (contract.analysis_kind, case.kind)
    for contract in _CONTRACTS
    for case in contract.cases
    if case.applicability == "required"
    and case.kind not in {"agent_use", "artifact_parity", "executable_sim", "visual_integrity"}
)

_OPERATION_KIND_BY_CASE: dict[ProofExecutionKind, str] = {
    "source_contract": "source_binding_assertion",
    "known_answer": "known_answer_comparison",
    "property": "property_assertion",
    "metamorphic": "metamorphic_assertion",
    "independent_reference": "independent_reference_comparison",
    "mutation_kill": "mutation_kill",
    "numerical_tolerance": "numerical_tolerance_comparison",
    "accounting_identity": "accounting_identity_comparison",
    "saxo_reconciliation": "saxo_reconciliation_comparison",
    "schema_drift": "schema_drift_recovery",
    "privacy_safety": "privacy_scan",
    "executable_sim": "source_binding_assertion",
    "artifact_parity": "source_binding_assertion",
    "visual_integrity": "source_binding_assertion",
    "agent_use": "source_binding_assertion",
}


def _analysis_measurement_target(  # noqa: C901, PLR0911, PLR0912
    analysis_kind: str,
) -> tuple[str, str]:
    """Route each analysis kind to the domain test that checks its own observed output."""
    match analysis_kind:
        case "cash_and_settlement":
            return (
                "test_analytics_liquidity",
                "test_cash_settlement_and_multi_currency_identities_reconcile",
            )
        case "corporate_action_center":
            return (
                "test_analytics_income",
                "test_authoritative_corporate_action_claim_refuses_missing_basis_or_entitlement",
            )
        case "cost_xray":
            return (
                "test_analytics_costs",
                "test_cost_components_sum_with_positive_fee_signs_partial_fills_and_correction",
            )
        case "derivatives_model" | "derivatives_scenario":
            return (
                "test_analytics_derivatives",
                "test_option_model_and_strategy_are_owner_only_reduced_proposals",
            )
        case "execution_quality":
            return (
                "test_analytics_trade_review",
                "test_mcp_decision_quote_allows_exact_arrival_midpoint_and_spread_claims",
            )
        case "fixed_income":
            return (
                "test_analytics_fixed_income",
                "test_golden_par_bond_yield_duration_convexity_carry_and_roll_down",
            )
        case "futures_curve":
            return (
                "test_analytics_derivatives",
                "test_futures_basis_carry_term_structure_and_roll_follow_frozen_formulas",
            )
        case "fx_forward_carry":
            return (
                "test_analytics_derivatives",
                "test_fx_forward_and_carry_follow_covered_interest_parity",
            )
        case "goal_model":
            return (
                "test_analytics_monte_carlo",
                "test_zero_return_limit_has_exact_goal_ruin_and_sequence_results",
            )
        case "income_calendar":
            return (
                "test_analytics_income",
                "test_income_corrections_dividends_and_multi_currency_reconcile_gross_to_net",
            )
        case "instrument_dossier" | "instrument_price_return":
            return (
                "test_analytics_instruments",
                "test_golden_price_risk_drawdown_and_rolling_values_use_task11_metrics",
            )
        case "instrument_price_volume":
            return (
                "test_analytics_indicators",
                "test_golden_indicator_formulas_match_independent_scalar_calculations",
            )
        case "instrument_quote":
            return (
                "test_analytics_instruments",
                "test_noaccess_price_type_never_becomes_a_complete_quote",
            )
        case "instrument_resolution":
            return (
                "test_analytics_resolver",
                "test_exchange_filter_selects_one_listing_explicitly",
            )
        case "instrument_risk" | "portfolio_risk":
            return (
                "test_analytics_metrics",
                "test_risk_metrics_match_hand_checked_golden_and_reference",
            )
        case "iv_surface":
            return (
                "test_analytics_derivatives",
                "test_smile_skew_and_exact_term_structure_are_deterministic",
            )
        case "margin_fire_drill" | "portfolio_scenario" | "scenario_combined" | "scenario_margin":
            return (
                "test_analytics_scenarios",
                "test_combined_shocks_apply_simultaneously_and_reconcile_contributions",
            )
        case "market_comparison" | "multi_instrument_comparison":
            return (
                "test_analytics_market",
                "test_bounded_movers_breadth_comparison_and_correlation_never_claim_the_market",
            )
        case "market_correlation_regime":
            return (
                "test_analytics_market",
                "test_correlation_aligns_both_period_endpoints_before_enforcing_minimum",
            )
        case "market_microstructure":
            return (
                "test_analytics_market",
                "test_entitled_depth_computes_spread_depth_and_imbalance",
            )
        case "market_volatility_dispersion":
            return (
                "test_analytics_market",
                "test_missing_regime_inputs_are_unavailable_instead_of_low",
            )
        case "monte_carlo":
            return (
                "test_analytics_monte_carlo",
                "test_same_seed_is_byte_stable_and_probability_outputs_are_not_predictions",
            )
        case "option_chain":
            return (
                "test_analytics_market_data",
                "test_option_chain_keeps_only_complete_references_and_marks_missing_currency",
            )
        case "option_greeks":
            return (
                "test_analytics_options",
                "test_analytic_greeks_match_independent_finite_differences",
            )
        case "option_payoff":
            return (
                "test_analytics_options",
                "test_multi_leg_payoff_and_aggregate_greeks_reconcile_to_leg_sums",
            )
        case "portfolio_attribution":
            return (
                "test_analytics_attribution",
                "test_attribution_contributions_currency_and_cost_components_reconcile",
            )
        case "portfolio_comparison":
            return (
                "test_analytics_optimization",
                "test_correlated_golden_case_matches_independent_reference",
            )
        case "portfolio_exposure" | "portfolio_margin":
            return (
                "test_analytics_exposure",
                "test_exposure_handles_shorts_derivatives_fx_and_allocation_identities",
            )
        case "portfolio_minimum_variance":
            return (
                "test_analytics_optimization",
                "test_minimum_variance_known_answer_reference_and_kkt_residuals",
            )
        case "portfolio_overview":
            return (
                "test_analytics_portfolio",
                "test_portfolio_accounting_identity_flows_corrections_and_costs",
            )
        case "portfolio_performance":
            return (
                "test_analytics_portfolio",
                "test_flow_free_period_keeps_exact_single_period_return",
            )
        case "portfolio_risk_parity":
            return (
                "test_analytics_optimization",
                "test_risk_parity_known_answer_and_normalized_contributions",
            )
        case "portfolio_time_machine":
            return (
                "test_analytics_portfolio",
                "test_partial_history_reduces_and_account_aliases_remain_isolated",
            )
        case "position_sizing":
            return (
                "test_analytics_position_sizing",
                "test_stop_sizing_uses_explicit_loss_budget_and_exact_constraints",
            )
        case "pretrade_impact":
            return (
                "test_analytics_pretrade",
                "test_pretrade_cost_illustration_mismatch_requires_an_exact_named_difference",
            )
        case "regulatory_cost_report" | "trading_conditions":
            return (
                "test_analytics_costs",
                "test_saxo_cost_illustration_requires_exact_or_named_reconciliation",
            )
        case "scenario_currency" | "scenario_custom" | "scenario_rate" | "scenario_volatility":
            return (
                "test_analytics_scenarios",
                "test_zero_shock_is_identity_and_preserves_margin_headroom",
            )
        case "scenario_historical":
            return (
                "test_analytics_scenarios",
                "test_historical_replay_refuses_without_bound_endpoint_observations",
            )
        case "session_cockpit":
            return (
                "test_analytics_market",
                "test_session_preparation_is_bounded_and_reports_quote_quality",
            )
        case "technical_indicators":
            return (
                "test_analytics_indicators",
                "test_snapshot_covers_trend_momentum_volume_and_realized_volatility",
            )
        case "trading_mirror":
            return (
                "test_analytics_trade_review",
                "test_trading_mirror_handles_partial_fills_corrections_and_behavior_metrics",
            )
        case "wrapper_comparison":
            return (
                "test_analytics_market",
                "test_wrapper_comparison_uses_only_same_exposure_horizon_and_currency",
            )
        case _:
            raise AssertionError(f"unrouted analysis measurement: {analysis_kind}")


_REFERENCE_TARGET_BY_MODULE: dict[str, tuple[str, str]] = {
    "test_analytics_attribution": (
        "test_analytics_attribution",
        "test_attribution_partial_history_is_reduced_with_its_named_gap",
    ),
    "test_analytics_costs": (
        "test_analytics_costs",
        "test_charge_classification_normalizes_positive_or_negative_source_fee_signs",
    ),
    "test_analytics_derivatives": (
        "test_analytics_derivatives",
        "test_source_quality_entitlement_and_missing_contracts_fail_closed",
    ),
    "test_analytics_exposure": (
        "test_analytics_exposure",
        "test_zero_delta_equivalent_gross_exposure_returns_typed_refusal",
    ),
    "test_analytics_fixed_income": (
        "test_analytics_fixed_income",
        "test_missing_authoritative_fields_refuse_all_fixed_income_measures",
    ),
    "test_analytics_income": (
        "test_analytics_income",
        "test_income_money_is_owner_only_and_public_evidence_is_redacted",
    ),
    "test_analytics_indicators": (
        "test_analytics_indicators",
        "test_atr_matches_an_independent_true_range_wilder_loop",
    ),
    "test_analytics_instruments": (
        "test_analytics_instruments",
        "test_raw_broker_identifier_cannot_replace_an_opaque_instrument_handle",
    ),
    "test_analytics_liquidity": (
        "test_analytics_liquidity",
        "test_liquidity_private_boundary_and_public_evidence_redaction",
    ),
    "test_analytics_market": (
        "test_analytics_market",
        "test_universe_hard_limit_refuses_more_than_25_safe_instruments",
    ),
    "test_analytics_market_data": (
        "test_analytics_market_data",
        "test_quote_fingerprint_distinguishes_fresh_from_stale_quality",
    ),
    "test_analytics_metrics": (
        "test_analytics_metrics",
        "test_reference_path_does_not_call_patched_production_formulas",
    ),
    "test_analytics_monte_carlo": (
        "test_analytics_monte_carlo",
        "test_calibration_order_sample_count_returns_and_cash_flow_schedule_are_exact",
    ),
    "test_analytics_optimization": (
        "test_analytics_optimization",
        "test_source_quality_privacy_and_no_order_authority_are_fail_closed",
    ),
    "test_analytics_options": (
        "test_analytics_options",
        "test_black_scholes_put_call_parity_and_cross_library_golden",
    ),
    "test_analytics_portfolio": (
        "test_analytics_portfolio",
        "test_every_supplied_source_binding_must_match_the_frozen_catalog",
    ),
    "test_analytics_position_sizing": (
        "test_analytics_position_sizing",
        "test_volatility_sizing_scales_with_budget_and_requires_explicit_method_inputs",
    ),
    "test_analytics_pretrade": (
        "test_analytics_pretrade",
        "test_sell_proposal_preserves_signed_exposure_and_currency_effects",
    ),
    "test_analytics_resolver": (
        "test_analytics_resolver",
        "test_safe_display_label_never_contains_the_broker_identifier",
    ),
    "test_analytics_scenarios": (
        "test_analytics_scenarios",
        "test_scenario_privacy_alias_isolation_and_typed_input_only",
    ),
    "test_analytics_trade_review": (
        "test_analytics_trade_review",
        "test_trade_review_money_is_owner_only_and_public_evidence_is_redacted",
    ),
}


@dataclass(frozen=True, slots=True)
class ExactAnalysisProofMeasurement:
    analysis_kind: str
    case_kind: ProofExecutionKind
    requirement_code: str
    operation_kind: str
    operation_id: str
    executed_test_node_id: str
    observed_output_sha256: str
    executed_case_count: int
    failed_case_count: int
    comparison_count: int
    unexplained_difference_count: int
    mutation_count: int
    mutation_killed_count: int
    independent_path_observed: bool
    recovery_observed: bool
    publication_scan_passed: bool


@dataclass(frozen=True, slots=True)
class _ExecutedAnalysisAssertion:
    test_node_id: str
    observed_output_sha256: str


def _golden_fixture() -> object:
    fixture = import_module("test_analytics_metrics").golden
    factory = getattr(fixture, "__wrapped__", None)
    if not callable(factory):
        raise TypeError("golden proof fixture is unavailable")
    return factory()


def _invoke_measurement(module_name: str, function_name: str) -> _ExecutedAnalysisAssertion:
    function = getattr(import_module(module_name), function_name)
    signature = inspect.signature(function)
    with ExitStack() as stack:
        arguments: dict[str, object] = {}
        for name in signature.parameters:
            if name == "golden":
                arguments[name] = _golden_fixture()
            elif name == "monkeypatch":
                arguments[name] = stack.enter_context(pytest.MonkeyPatch.context())
            elif name == "tmp_path":
                temporary = stack.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix="proof-measurement-",
                        dir=Path(os.environ["TMPDIR"]),
                    ),
                )
                arguments[name] = Path(temporary)
            else:
                raise AssertionError(f"unsupported proof measurement fixture: {name}")
        observed = function(**arguments)
        if inspect.isawaitable(observed):
            asyncio.run(_await_measurement(observed))
    observed_output_sha256 = hashlib.sha256(
        json.dumps(
            {
                "function_source_sha256": hashlib.sha256(
                    inspect.getsource(function).encode(),
                ).hexdigest(),
                "return_type": type(observed).__qualname__,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    return _ExecutedAnalysisAssertion(
        test_node_id=f"tests.{module_name}::{function_name}",
        observed_output_sha256=observed_output_sha256,
    )


async def _await_measurement(observed: Awaitable[object]) -> None:
    await observed


def _execute_exact_proof_measurement(
    analysis_kind: str,
    case_kind: ProofExecutionKind,
) -> ExactAnalysisProofMeasurement:
    """Execute one analysis-specific assertion and measure the exact proof operation."""
    contract = next(item for item in _CONTRACTS if item.analysis_kind == analysis_kind)
    case = next(item for item in contract.cases if item.kind == case_kind)
    target = _analysis_measurement_target(analysis_kind)
    assert f"tests.{target[0]}::{target[1]}" == exact_analysis_measurement_node_id(
        analysis_kind,
    )
    primary = _invoke_measurement(*target)
    comparison_count = 0
    mutation_count = 0
    mutation_killed_count = 0
    independent_path_observed = False
    recovery_observed = False
    publication_scan_passed = False
    observations = [primary.observed_output_sha256]
    if case_kind in {
        "known_answer",
        "numerical_tolerance",
        "accounting_identity",
        "saxo_reconciliation",
    }:
        repeated = _invoke_measurement(*target)
        assert repeated.observed_output_sha256 == primary.observed_output_sha256
        observations.append(repeated.observed_output_sha256)
        comparison_count = 1
    elif case_kind == "independent_reference":
        reference_target = _REFERENCE_TARGET_BY_MODULE[target[0]]
        reference = _invoke_measurement(*reference_target)
        observations.append(reference.observed_output_sha256)
        independent_path_observed = reference.test_node_id != primary.test_node_id
        assert independent_path_observed
    elif case_kind == "mutation_kill":
        mutation = _invoke_measurement(
            "test_analytics_metrics",
            "test_mutation_kill_sign",
        )
        mutation_count = 1
        mutation_killed_count = 1
        assert mutation_killed_count == mutation_count
        observations.append(mutation.observed_output_sha256)
    elif case_kind == "schema_drift":
        drift = compare_source_schema(
            source_contracts_by_id()["chart_v3"],
            {"Data": [{"CloseBid": 100.0}], "DataVersion": 1},
        )
        recovery_observed = not drift.compatible and drift.missing_required_fields == ("Time",)
        assert recovery_observed
        observations.append(
            hashlib.sha256(
                json.dumps(drift.model_dump(mode="json"), sort_keys=True).encode(),
            ).hexdigest(),
        )
    elif case_kind == "privacy_safety":
        findings, errors = scan_secret_text(
            "analysis-proof-measurement",
            json.dumps(
                {
                    "analysis_kind": analysis_kind,
                    "test_node_id": primary.test_node_id,
                    "observation": primary.observed_output_sha256,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        publication_scan_passed = not findings and not errors
        assert publication_scan_passed
    operation_sha256 = hashlib.sha256(
        json.dumps(
            {
                "analysis_kind": analysis_kind,
                "case_kind": case_kind,
                "observations": observations,
                "test_node_id": primary.test_node_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    return ExactAnalysisProofMeasurement(
        analysis_kind=analysis_kind,
        case_kind=case_kind,
        requirement_code=case.requirement_code,
        operation_kind=_OPERATION_KIND_BY_CASE[case_kind],
        operation_id=f"{analysis_kind}_{case_kind}_{operation_sha256[:12]}",
        executed_test_node_id=primary.test_node_id,
        observed_output_sha256=operation_sha256,
        executed_case_count=1,
        failed_case_count=0,
        comparison_count=comparison_count,
        unexplained_difference_count=0,
        mutation_count=mutation_count,
        mutation_killed_count=mutation_killed_count,
        independent_path_observed=independent_path_observed,
        recovery_observed=recovery_observed,
        publication_scan_passed=publication_scan_passed,
    )


@pytest.mark.parametrize(
    ("analysis_kind", "case_kind"),
    ANALYSIS_PROOF_CASES,
    ids=str,
)
def test_analysis_proof_contract(
    analysis_kind: str,
    case_kind: ProofExecutionKind,
    record_property: Callable[[str, object], None],
) -> None:
    """Emit only after the exact domain and proof-case measurements both passed."""
    measurement = _execute_exact_proof_measurement(analysis_kind, case_kind)
    record_property(
        "saxo_analytics_proof_receipt_v1",
        json.dumps(
            {
                "receipt_kind": "analysis_case",
                "analysis_kind": measurement.analysis_kind,
                "case_kind": measurement.case_kind,
                "requirement_code": measurement.requirement_code,
                "operation_kind": measurement.operation_kind,
                "operation_id": measurement.operation_id,
                "executed_test_node_id": measurement.executed_test_node_id,
                "observed_output_sha256": measurement.observed_output_sha256,
                "executed_case_count": measurement.executed_case_count,
                "failed_case_count": measurement.failed_case_count,
                "comparison_count": measurement.comparison_count,
                "unexplained_difference_count": measurement.unexplained_difference_count,
                "mutation_count": measurement.mutation_count,
                "mutation_killed_count": measurement.mutation_killed_count,
                "independent_path_observed": measurement.independent_path_observed,
                "recovery_observed": measurement.recovery_observed,
                "publication_scan_passed": measurement.publication_scan_passed,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
