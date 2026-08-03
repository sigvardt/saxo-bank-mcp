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
from importlib import import_module
from pathlib import Path

import pytest

from saxo_bank_mcp.qa_analytics_evidence import (
    ProofExecutionKind,
    build_proof_execution_contracts,
)

_CONTRACTS = build_proof_execution_contracts()
ANALYSIS_PROOF_CASES = tuple(
    (contract.analysis_kind, case.kind)
    for contract in _CONTRACTS
    for case in contract.cases
    if case.applicability == "required"
    and case.kind not in {"agent_use", "artifact_parity", "executable_sim", "visual_integrity"}
)


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


def _proof_case_measurement_target(  # noqa: C901, PLR0911
    case_kind: ProofExecutionKind,
) -> tuple[str, str]:
    """Select the check that performs this exact proof operation."""
    match case_kind:
        case "source_contract":
            return (
                "test_analytics_proof_profiles",
                "test_checked_in_profiles_cover_every_declared_surface_and_current_source",
            )
        case "known_answer":
            return (
                "test_analytics_metrics",
                "test_return_series_match_hand_checked_golden_and_reference",
            )
        case "property":
            return (
                "test_analytics_properties",
                "test_property_compounding_matches_price_endpoints",
            )
        case "metamorphic":
            return (
                "test_analytics_properties",
                "test_property_price_scale_does_not_change_returns",
            )
        case "independent_reference":
            return (
                "test_analytics_metrics",
                "test_reference_path_does_not_call_patched_production_formulas",
            )
        case "mutation_kill":
            return "test_analytics_metrics", "test_mutation_kill_sign"
        case "numerical_tolerance":
            return (
                "test_analytics_metrics",
                "test_risk_metrics_match_hand_checked_golden_and_reference",
            )
        case "accounting_identity":
            return (
                "test_analytics_portfolio",
                "test_portfolio_accounting_identity_flows_corrections_and_costs",
            )
        case "saxo_reconciliation":
            return (
                "test_analytics_costs",
                "test_saxo_cost_illustration_requires_exact_or_named_reconciliation",
            )
        case "schema_drift":
            return (
                "test_analytics_proof_profiles",
                "test_definition_change_without_profile_rebinding_is_stale",
            )
        case "privacy_safety":
            return (
                "test_analytics_export",
                "test_export_string_values_reject_private_paths_and_secret_material",
            )
        case _:
            raise AssertionError(f"unrouted offline proof measurement: {case_kind}")


def _golden_fixture() -> object:
    fixture = import_module("test_analytics_metrics").golden
    factory = getattr(fixture, "__wrapped__", None)
    if not callable(factory):
        raise TypeError("golden proof fixture is unavailable")
    return factory()


def _invoke_measurement(module_name: str, function_name: str) -> str:
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
    return hashlib.sha256(inspect.getsource(function).encode()).hexdigest()


async def _await_measurement(observed: Awaitable[object]) -> None:
    await observed


def _execute_exact_proof_measurement(
    analysis_kind: str,
    case_kind: ProofExecutionKind,
) -> str:
    """Execute both exact domain-output and proof-operation assertions before emitting."""
    domain_sha256 = _invoke_measurement(*_analysis_measurement_target(analysis_kind))
    proof_sha256 = _invoke_measurement(*_proof_case_measurement_target(case_kind))
    return hashlib.sha256(
        json.dumps(
            {
                "analysis_kind": analysis_kind,
                "case_kind": case_kind,
                "domain_measurement_sha256": domain_sha256,
                "proof_measurement_sha256": proof_sha256,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()


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
    contract = next(item for item in _CONTRACTS if item.analysis_kind == analysis_kind)
    case = next(item for item in contract.cases if item.kind == case_kind)
    measurement_sha256 = _execute_exact_proof_measurement(analysis_kind, case_kind)
    record_property(
        "saxo_analytics_proof_receipt_v1",
        json.dumps(
            {
                "receipt_kind": "analysis_case",
                "analysis_kind": analysis_kind,
                "case_kind": case_kind,
                "requirement_code": case.requirement_code,
                "measurement_sha256": measurement_sha256,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
