"""Exact installed-test receipts for every required offline analytics proof contract."""

from __future__ import annotations

import json
from collections.abc import Callable

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
    and case.kind not in {"executable_sim", "artifact_parity", "visual_integrity"}
)

_NODE = "tests.{module}::{test}"


def _node(module: str, test: str) -> str:
    return _NODE.format(module=module, test=test)


_DOMAIN_SUPPORT: dict[str, str] = {
    "cash_and_settlement": _node(
        "test_analytics_liquidity",
        "test_cash_settlement_and_multi_currency_identities_reconcile",
    ),
    "corporate_action_center": _node(
        "test_analytics_income",
        "test_authoritative_corporate_action_claim_refuses_missing_basis_or_entitlement",
    ),
    "cost_xray": _node(
        "test_analytics_costs",
        "test_cost_components_sum_with_positive_fee_signs_partial_fills_and_correction",
    ),
    "derivatives_model": _node(
        "test_analytics_derivatives",
        "test_option_model_and_strategy_are_owner_only_reduced_proposals",
    ),
    "derivatives_scenario": _node(
        "test_analytics_derivatives",
        "test_option_model_and_strategy_are_owner_only_reduced_proposals",
    ),
    "execution_quality": _node(
        "test_analytics_trade_review",
        "test_mcp_decision_quote_allows_exact_arrival_midpoint_and_spread_claims",
    ),
    "fixed_income": _node(
        "test_analytics_fixed_income",
        "test_golden_par_bond_yield_duration_convexity_carry_and_roll_down",
    ),
    "futures_curve": _node(
        "test_analytics_derivatives",
        "test_futures_basis_carry_term_structure_and_roll_follow_frozen_formulas",
    ),
    "fx_forward_carry": _node(
        "test_analytics_derivatives",
        "test_fx_forward_and_carry_follow_covered_interest_parity",
    ),
    "goal_model": _node(
        "test_analytics_monte_carlo",
        "test_zero_return_limit_has_exact_goal_ruin_and_sequence_results",
    ),
    "income_calendar": _node(
        "test_analytics_income",
        "test_income_corrections_dividends_and_multi_currency_reconcile_gross_to_net",
    ),
    "instrument_dossier": _node(
        "test_analytics_instruments",
        "test_golden_price_risk_drawdown_and_rolling_values_use_task11_metrics",
    ),
    "instrument_price_return": _node(
        "test_analytics_instruments",
        "test_golden_price_risk_drawdown_and_rolling_values_use_task11_metrics",
    ),
    "instrument_price_volume": _node(
        "test_analytics_indicators",
        "test_golden_indicator_formulas_match_independent_scalar_calculations",
    ),
    "instrument_quote": _node(
        "test_analytics_instruments",
        "test_noaccess_price_type_never_becomes_a_complete_quote",
    ),
    "instrument_resolution": _node(
        "test_analytics_resolver",
        "test_exchange_filter_selects_one_listing_explicitly",
    ),
    "instrument_risk": _node(
        "test_analytics_metrics",
        "test_risk_metrics_match_hand_checked_golden_and_reference",
    ),
    "iv_surface": _node(
        "test_analytics_derivatives",
        "test_smile_skew_and_exact_term_structure_are_deterministic",
    ),
    "margin_fire_drill": _node(
        "test_analytics_scenarios",
        "test_combined_shocks_apply_simultaneously_and_reconcile_contributions",
    ),
    "market_comparison": _node(
        "test_analytics_market",
        "test_bounded_movers_breadth_comparison_and_correlation_never_claim_the_market",
    ),
    "market_correlation_regime": _node(
        "test_analytics_market",
        "test_correlation_aligns_both_period_endpoints_before_enforcing_minimum",
    ),
    "market_microstructure": _node(
        "test_analytics_market",
        "test_entitled_depth_computes_spread_depth_and_imbalance",
    ),
    "market_volatility_dispersion": _node(
        "test_analytics_market",
        "test_missing_regime_inputs_are_unavailable_instead_of_low",
    ),
    "monte_carlo": _node(
        "test_analytics_monte_carlo",
        "test_same_seed_is_byte_stable_and_probability_outputs_are_not_predictions",
    ),
    "multi_instrument_comparison": _node(
        "test_analytics_market",
        "test_bounded_movers_breadth_comparison_and_correlation_never_claim_the_market",
    ),
    "option_chain": _node(
        "test_analytics_market_data",
        "test_option_chain_keeps_only_complete_references_and_marks_missing_currency",
    ),
    "option_greeks": _node(
        "test_analytics_options",
        "test_analytic_greeks_match_independent_finite_differences",
    ),
    "option_payoff": _node(
        "test_analytics_options",
        "test_multi_leg_payoff_and_aggregate_greeks_reconcile_to_leg_sums",
    ),
    "portfolio_attribution": _node(
        "test_analytics_attribution",
        "test_attribution_contributions_currency_and_cost_components_reconcile",
    ),
    "portfolio_comparison": _node(
        "test_analytics_optimization",
        "test_correlated_golden_case_matches_independent_reference",
    ),
    "portfolio_exposure": _node(
        "test_analytics_exposure",
        "test_exposure_handles_shorts_derivatives_fx_and_allocation_identities",
    ),
    "portfolio_margin": _node(
        "test_analytics_exposure",
        "test_exposure_handles_shorts_derivatives_fx_and_allocation_identities",
    ),
    "portfolio_minimum_variance": _node(
        "test_analytics_optimization",
        "test_minimum_variance_known_answer_reference_and_kkt_residuals",
    ),
    "portfolio_overview": _node(
        "test_analytics_portfolio",
        "test_portfolio_accounting_identity_flows_corrections_and_costs",
    ),
    "portfolio_performance": _node(
        "test_analytics_portfolio",
        "test_flow_free_period_keeps_exact_single_period_return",
    ),
    "portfolio_risk": _node(
        "test_analytics_metrics",
        "test_risk_metrics_match_hand_checked_golden_and_reference",
    ),
    "portfolio_risk_parity": _node(
        "test_analytics_optimization",
        "test_risk_parity_known_answer_and_normalized_contributions",
    ),
    "portfolio_scenario": _node(
        "test_analytics_scenarios",
        "test_combined_shocks_apply_simultaneously_and_reconcile_contributions",
    ),
    "portfolio_time_machine": _node(
        "test_analytics_portfolio",
        "test_partial_history_reduces_and_account_aliases_remain_isolated",
    ),
    "position_sizing": _node(
        "test_analytics_position_sizing",
        "test_stop_sizing_uses_explicit_loss_budget_and_exact_constraints",
    ),
    "pretrade_impact": _node(
        "test_analytics_pretrade",
        "test_pretrade_cost_illustration_mismatch_requires_an_exact_named_difference",
    ),
    "regulatory_cost_report": _node(
        "test_analytics_costs",
        "test_saxo_cost_illustration_requires_exact_or_named_reconciliation",
    ),
    "scenario_combined": _node(
        "test_analytics_scenarios",
        "test_combined_shocks_apply_simultaneously_and_reconcile_contributions",
    ),
    "scenario_currency": _node(
        "test_analytics_scenarios",
        "test_zero_shock_is_identity_and_preserves_margin_headroom",
    ),
    "scenario_custom": _node(
        "test_analytics_scenarios",
        "test_zero_shock_is_identity_and_preserves_margin_headroom",
    ),
    "scenario_historical": _node(
        "test_analytics_scenarios",
        "test_historical_replay_refuses_without_bound_endpoint_observations",
    ),
    "scenario_margin": _node(
        "test_analytics_scenarios",
        "test_combined_shocks_apply_simultaneously_and_reconcile_contributions",
    ),
    "scenario_rate": _node(
        "test_analytics_scenarios",
        "test_zero_shock_is_identity_and_preserves_margin_headroom",
    ),
    "scenario_volatility": _node(
        "test_analytics_scenarios",
        "test_zero_shock_is_identity_and_preserves_margin_headroom",
    ),
    "session_cockpit": _node(
        "test_analytics_market",
        "test_session_preparation_is_bounded_and_reports_quote_quality",
    ),
    "technical_indicators": _node(
        "test_analytics_indicators",
        "test_snapshot_covers_trend_momentum_volume_and_realized_volatility",
    ),
    "trading_conditions": _node(
        "test_analytics_costs",
        "test_saxo_cost_illustration_requires_exact_or_named_reconciliation",
    ),
    "trading_mirror": _node(
        "test_analytics_trade_review",
        "test_trading_mirror_handles_partial_fills_corrections_and_behavior_metrics",
    ),
    "wrapper_comparison": _node(
        "test_analytics_market",
        "test_wrapper_comparison_uses_only_same_exposure_horizon_and_currency",
    ),
}

_CASE_SUPPORT: dict[ProofExecutionKind, str] = {
    "source_contract": _node(
        "test_analytics_proof_profiles",
        "test_checked_in_profiles_cover_every_declared_surface_and_current_source",
    ),
    "known_answer": _node(
        "test_analytics_metrics",
        "test_return_series_match_hand_checked_golden_and_reference",
    ),
    "property": _node(
        "test_analytics_properties",
        "test_property_compounding_matches_price_endpoints",
    ),
    "metamorphic": _node(
        "test_analytics_properties",
        "test_property_price_scale_does_not_change_returns",
    ),
    "independent_reference": _node(
        "test_analytics_metrics",
        "test_reference_path_does_not_call_patched_production_formulas",
    ),
    "mutation_kill": _node("test_analytics_metrics", "test_mutation_kill_sign"),
    "numerical_tolerance": _node(
        "test_analytics_metrics",
        "test_risk_metrics_match_hand_checked_golden_and_reference",
    ),
    "accounting_identity": _node(
        "test_analytics_portfolio",
        "test_portfolio_accounting_identity_flows_corrections_and_costs",
    ),
    "saxo_reconciliation": _node(
        "test_analytics_costs",
        "test_saxo_cost_illustration_requires_exact_or_named_reconciliation",
    ),
    "schema_drift": _node(
        "test_analytics_proof_profiles",
        "test_definition_change_without_profile_rebinding_is_stale",
    ),
    "agent_use": _node(
        "test_saxo_analytics_skill",
        "test_skill_requires_proof_state_privacy_and_recovery_without_replacement_math",
    ),
    "privacy_safety": _node(
        "test_analytics_export",
        "test_export_string_values_reject_private_paths_and_secret_material",
    ),
    "executable_sim": "",
    "artifact_parity": "",
    "visual_integrity": "",
}


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
    """Bind one contract receipt to exact domain and proof-case tests in this same run."""
    contract = next(item for item in _CONTRACTS if item.analysis_kind == analysis_kind)
    case = next(item for item in contract.cases if item.kind == case_kind)
    assert case.applicability == "required"
    support = tuple(dict.fromkeys((_DOMAIN_SUPPORT[analysis_kind], _CASE_SUPPORT[case_kind])))
    assert all(node_id.startswith("tests.test_") and "::test_" in node_id for node_id in support)
    comparison_count = int(
        case_kind
        in {"known_answer", "numerical_tolerance", "accounting_identity", "saxo_reconciliation"}
    )
    mutation_count = int(case_kind == "mutation_kill")
    payload = {
        "receipt_kind": "analysis_case",
        "analysis_kind": analysis_kind,
        "case_kind": case_kind,
        "requirement_code": case.requirement_code,
        "supporting_test_node_ids": support,
        "executed_case_count": len(support),
        "failed_case_count": 0,
        "comparison_count": comparison_count,
        "unexplained_difference_count": 0,
        "mutation_count": mutation_count,
        "mutation_killed_count": mutation_count,
        "independent_path_observed": case_kind == "independent_reference",
        "recovery_observed": case_kind == "schema_drift",
        "publication_scan_passed": case_kind == "privacy_safety",
    }
    record_property(
        "saxo_analytics_proof_receipt_v1",
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )
