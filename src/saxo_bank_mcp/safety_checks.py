from __future__ import annotations

from saxo_bank_mcp.audit import append_audit_event
from saxo_bank_mcp.safety_models import SafetyConfig, WritePreviewRequest
from saxo_bank_mcp.safety_state import is_committed


def preview_denial_reasons(
    config: SafetyConfig,
    request: WritePreviewRequest,
    fingerprint: str,
) -> list[str]:
    append_audit_event(
        config.audit_dir,
        {
            "event": "preview_checked",
            "environment": config.environment,
            "request_fingerprint": fingerprint,
        },
    )
    reasons = [
        *_environment_reasons(config),
        *_allowlist_reasons(config, request),
        *_limit_reasons(config, request),
        *_risk_reasons(request),
    ]
    if is_committed(fingerprint):
        reasons.append("duplicate_request")
    return reasons


def _environment_reasons(config: SafetyConfig) -> list[str]:
    reasons: list[str] = []
    if config.environment == "LIVE":
        reasons.append("live_environment_not_allowed")
    if config.live_writes_enabled:
        reasons.append("live_write_execution_disabled")
    if config.global_kill_switch:
        reasons.append("global_kill_switch_active")
    return reasons


def _allowlist_reasons(config: SafetyConfig, request: WritePreviewRequest) -> list[str]:
    reasons: list[str] = []
    if not config.account_allowlist:
        reasons.append("account_allowlist_missing")
    elif request.account_key not in config.account_allowlist:
        reasons.append("account_not_allowlisted")
    if not config.instrument_allowlist:
        reasons.append("instrument_allowlist_missing")
    elif request.instrument_uic not in config.instrument_allowlist:
        reasons.append("instrument_not_allowlisted")
    return reasons


def _limit_reasons(config: SafetyConfig, request: WritePreviewRequest) -> list[str]:
    reasons: list[str] = []
    if request.quantity > config.max_quantity:
        reasons.append("quantity_limit_exceeded")
    if request.estimated_notional > config.max_notional:
        reasons.append("notional_limit_exceeded")
    return reasons


def _risk_reasons(request: WritePreviewRequest) -> list[str]:
    reasons: list[str] = []
    if not request.risk.conversion_known:
        reasons.append("account_currency_conversion_unknown")
    if request.risk.margin_impact is None:
        reasons.append("margin_impact_unknown")
    if request.risk.contract_multiplier is None:
        reasons.append("contract_multiplier_unknown")
    return reasons
