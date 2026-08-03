from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.audit import AuditPathError, append_audit_event
from saxo_bank_mcp.live_approval import live_approval_statement
from saxo_bank_mcp.live_instrument_refs import LiveInstrumentIdentity
from saxo_bank_mcp.safety_audit import (
    audit_mode,
    is_inside_repo,
    request_fingerprint,
    token_fingerprint,
    try_audit_denial,
)
from saxo_bank_mcp.safety_checks import current_safety_reasons, preview_denial_reasons
from saxo_bank_mcp.safety_models import (
    PREVIEW_TTL_SECONDS,
    SAFETY_TOOL_DOES_NOT_VERIFY,
    SAFETY_TOOL_VERIFIES,
    TEST_APPROVAL_FACTOR,
    AccountCurrencyRisk,
    PreviewResult,
    SafetyConfig,
    StoredPreview,
    WritePreviewRequest,
)
from saxo_bank_mcp.safety_state import (
    committed_fingerprint_count,
    discard_preview,
    get_preview,
    is_committed,
    is_preview_token_committed,
    mark_committed,
    pending_preview_count,
    rate_limit_reason,
    reset_safety_state,
    store_preview,
)
from saxo_bank_mcp.streaming import local_subscription_count

__all__ = (
    "TEST_APPROVAL_FACTOR",
    "AccountCurrencyRisk",
    "PreviewResult",
    "SafetyConfig",
    "SafetyKernel",
    "WritePreviewRequest",
    "discard_preview",
    "get_preview",
    "pending_preview_count",
    "request_fingerprint",
    "reset_safety_state",
    "token_fingerprint",
)


class SafetyKernel:
    def __init__(self, config: SafetyConfig | None = None) -> None:  # noqa: D107
        self.config = SafetyConfig.from_env() if config is None else config

    def status(self) -> dict[str, JsonValue]:
        return {
            "status": "passed",
            "tool_name": "saxo_safety_status",
            "environment": self.config.environment,
            "live_writes_enabled": self.config.live_writes_enabled,
            "global_kill_switch": self.config.global_kill_switch,
            "account_allowlist_count": len(self.config.account_allowlist),
            "instrument_allowlist_count": len(self.config.instrument_allowlist),
            "max_quantity": self.config.max_quantity,
            "max_notional": self.config.max_notional,
            "pending_preview_count": pending_preview_count(),
            "committed_fingerprint_count": committed_fingerprint_count(),
            "local_subscription_count": local_subscription_count(),
            "verifies": ["local safety configuration and in-memory preview state"],
            "does_not_verify": list(SAFETY_TOOL_DOES_NOT_VERIFY),
        }

    def create_preview(
        self,
        request: WritePreviewRequest,
        *,
        instrument_identity: LiveInstrumentIdentity | None = None,
    ) -> PreviewResult:
        fingerprint = request_fingerprint(request)
        try:
            denial_reasons = preview_denial_reasons(self.config, request, fingerprint)
        except AuditPathError:
            denial_reasons = ["audit_path_refused"]
        except OSError:
            denial_reasons = ["audit_write_failed"]
        if denial_reasons:
            return self._deny_preview(request, fingerprint, denial_reasons)

        token = secrets.token_urlsafe(32)
        preview_token_fingerprint = token_fingerprint(token)
        expires_at = datetime.now(UTC) + timedelta(seconds=PREVIEW_TTL_SECONDS)
        approval_prompt: str | None = None
        if self.config.environment == "LIVE":
            approval_prompt = live_approval_statement(
                _order_approval_action(request, instrument_identity),
                f"{fingerprint}:{preview_token_fingerprint}",
            )
        try:
            audit_path = append_audit_event(
                self.config.audit_dir,
                {
                    "event": "preview_created",
                    "environment": self.config.environment,
                    "operation_id": request.operation_id,
                    "account_key": request.account_key,
                    "instrument_uic": request.instrument_uic,
                    "request_fingerprint": fingerprint,
                    "preview_token_fingerprint": preview_token_fingerprint,
                },
            )
        except (AuditPathError, OSError):
            return self._deny_preview(request, fingerprint, ["audit_write_failed"])
        store_preview(
            token,
            StoredPreview(
                request,
                fingerprint,
                expires_at,
                self.config.environment,
                expected_approval_statement=approval_prompt,
            ),
        )
        result: PreviewResult = {
            "status": "preview_created",
            "tool_name": "saxo_create_write_preview",
            "environment": self.config.environment,
            "request_fingerprint": fingerprint,
            "preview_token": token,
            "preview_token_fingerprint": preview_token_fingerprint,
            "preview_token_sensitivity": "sensitive local commit token; do not log or expose",
            "preview_token_expires_at": expires_at.isoformat(),
            "approval_factor_mode": (
                "one_exact_action_chat_approval"
                if self.config.environment == "LIVE"
                else "autonomous_sim"
            ),
            "audit_path": str(audit_path),
            "audit_path_inside_repo": is_inside_repo(audit_path),
            "audit_mode": audit_mode(audit_path),
            "saxo_endpoint_called": False,
            "execution_performed": False,
            "simulation_only": self.config.environment == "SIM",
            "order_placed": False,
            "verifies": list(SAFETY_TOOL_VERIFIES),
            "does_not_verify": list(SAFETY_TOOL_DOES_NOT_VERIFY),
            "next_action": (
                "ask the human to send approval_prompt in agent chat, then pass it unchanged "
                "to saxo_commit_write_preview"
                if self.config.environment == "LIVE"
                else "call saxo_commit_write_preview; SIM needs no human approval"
            ),
        }
        if approval_prompt is not None:
            result["approval_prompt"] = approval_prompt
            summary: dict[str, JsonValue] = {
                "account_key_redacted": True,
                "estimated_notional": request.estimated_notional,
                "instrument_uic": request.instrument_uic,
                "operation_id": request.operation_id,
                "quantity": request.quantity,
                "request_body": redact_json(request.request_body),
            }
            if instrument_identity is not None:
                summary["instrument_description"] = instrument_identity.description
                summary["instrument_symbol"] = instrument_identity.symbol
                summary["instrument_identity_source"] = instrument_identity.source
            result["approval_summary"] = summary
        return result

    def commit_preview(
        self,
        preview_token: str,
        *,
        approval_factor: str | None,
    ) -> PreviewResult:
        stored, denial_reason = self._commit_denial_reason(preview_token, approval_factor)
        if denial_reason is not None:
            return self._deny_commit(stored, denial_reason)
        if stored is None:
            return self._deny_commit(None, "preview_token_invalid")

        try:
            audit_path = append_audit_event(
                self.config.audit_dir,
                {
                    "event": "commit_approved",
                    "environment": self.config.environment,
                    "operation_id": stored.request.operation_id,
                    "request_fingerprint": stored.request_fingerprint,
                    "preview_token_fingerprint": token_fingerprint(preview_token),
                    "execution_performed": False,
                    "saxo_endpoint_called": False,
                },
            )
        except (AuditPathError, OSError):
            return self._deny_commit(stored, "audit_write_failed")
        mark_committed(
            stored.request_fingerprint,
            token_fingerprint(preview_token),
        )
        live = self.config.environment == "LIVE"
        return {
            "status": "approved_for_execution" if live else "approved_for_simulation",
            "tool_name": "saxo_commit_write_preview",
            "environment": self.config.environment,
            "request_fingerprint": stored.request_fingerprint,
            "preview_token_fingerprint": token_fingerprint(preview_token),
            "approval_factor_mode": (
                "one_exact_action_chat_approval" if live else "autonomous_sim"
            ),
            "audit_path": str(audit_path),
            "audit_path_inside_repo": is_inside_repo(audit_path),
            "audit_mode": audit_mode(audit_path),
            "saxo_endpoint_called": False,
            "execution_performed": False,
            "simulation_only": not live,
            "order_placed": False,
            "verifies": list(SAFETY_TOOL_VERIFIES),
            "does_not_verify": list(SAFETY_TOOL_DOES_NOT_VERIFY),
            "next_action": (
                "exact LIVE request approved once; execution has not started"
                if live
                else "simulation approved; no order was placed"
            ),
        }

    def _commit_denial_reason(
        self,
        preview_token: str,
        approval_factor: str | None,
    ) -> tuple[StoredPreview | None, str | None]:
        if not preview_token.strip():
            return None, "preview_token_missing"
        stored = get_preview(preview_token)
        if stored is None:
            return None, "preview_token_invalid"
        reasons = self._stored_commit_denial_reasons(
            stored,
            preview_token,
            approval_factor,
        )
        return stored, reasons[0] if reasons else None

    def _stored_commit_denial_reasons(
        self,
        stored: StoredPreview,
        preview_token: str,
        approval_factor: str | None,
    ) -> list[str]:
        reasons: list[str] = []
        if stored.expires_at <= datetime.now(UTC):
            reasons.append("preview_token_expired")
        if stored.environment != self.config.environment:
            reasons.append("preview_environment_changed")
        reasons.extend(current_safety_reasons(self.config, stored.request))
        if self.config.environment == "LIVE":
            expected = stored.expected_approval_statement
            if approval_factor is None or not approval_factor.strip():
                reasons.append("chat_approval_missing")
            elif expected is None or not secrets.compare_digest(approval_factor, expected):
                reasons.append("chat_approval_mismatch")
        if is_preview_token_committed(token_fingerprint(preview_token)) or is_committed(
            stored.request_fingerprint,
        ):
            reasons.append("duplicate_request")
        limited = rate_limit_reason()
        if limited is not None:
            reasons.append(limited)
        return reasons

    def _deny_preview(
        self,
        request: WritePreviewRequest,
        fingerprint: str,
        reasons: list[str],
    ) -> PreviewResult:
        audit_path = try_audit_denial(
            self.config.audit_dir,
            {
                "event": "preview_denied",
                "environment": self.config.environment,
                "operation_id": request.operation_id,
                "request_fingerprint": fingerprint,
                "denial_reasons": reasons,
            },
        )
        return {
            "status": "denied",
            "tool_name": "saxo_create_write_preview",
            "environment": self.config.environment,
            "request_fingerprint": fingerprint,
            "denial_reasons": reasons,
            "saxo_endpoint_called": False,
            "execution_performed": False,
            "audit_path": "" if audit_path is None else str(audit_path),
            "audit_path_inside_repo": False if audit_path is None else is_inside_repo(audit_path),
            "audit_mode": None if audit_path is None else audit_mode(audit_path),
            "simulation_only": True,
            "order_placed": False,
            "verifies": ["deterministic local write safety gates denied the request"],
            "does_not_verify": list(SAFETY_TOOL_DOES_NOT_VERIFY),
            "next_action": f"fix safety condition: {reasons[0]}",
        }

    def _deny_commit(self, stored: StoredPreview | None, reason: str) -> PreviewResult:
        event: dict[str, JsonValue] = {
            "event": "commit_denied",
            "environment": self.config.environment,
            "denial_reason": reason,
        }
        if stored is not None:
            event["operation_id"] = stored.request.operation_id
            event["request_fingerprint"] = stored.request_fingerprint
        audit_path = try_audit_denial(self.config.audit_dir, event)
        return {
            "status": "denied",
            "tool_name": "saxo_commit_write_preview",
            "environment": self.config.environment,
            "request_fingerprint": "" if stored is None else stored.request_fingerprint,
            "denial_reason": reason,
            "saxo_endpoint_called": False,
            "execution_performed": False,
            "audit_path": "" if audit_path is None else str(audit_path),
            "audit_path_inside_repo": False if audit_path is None else is_inside_repo(audit_path),
            "audit_mode": None if audit_path is None else audit_mode(audit_path),
            "simulation_only": True,
            "order_placed": False,
            "verifies": ["deterministic local commit gate denied the request"],
            "does_not_verify": list(SAFETY_TOOL_DOES_NOT_VERIFY),
            "next_action": f"fix safety condition: {reason}",
        }


def _order_approval_action(
    request: WritePreviewRequest,
    instrument_identity: LiveInstrumentIdentity | None = None,
) -> str:
    body = request.request_body
    side_value = body.get("BuySell")
    side = side_value.upper() if isinstance(side_value, str) else ""
    match request.operation_id:
        case operation if operation.startswith("post.") or operation.endswith(".place"):
            action = f"PLACE a {side} order" if side else "PLACE an order"
        case operation if operation.startswith("patch.") or operation.endswith(".modify"):
            action = f"MODIFY a {side} order" if side else "MODIFY an order"
        case operation if operation.startswith("delete.") or operation.endswith(".cancel"):
            action = "CANCEL an order"
        case _:
            action = "EXECUTE an order action"
    quantity = f"{request.quantity:g}"
    if instrument_identity is not None:
        unit = _quantity_unit(request.quantity, instrument_identity.asset_type)
        instrument = (
            f"{instrument_identity.description} ({instrument_identity.symbol}, "
            f"UIC {request.instrument_uic})"
        )
        summary = f"{action} for {quantity} {unit} of {instrument}"
        price_currency = instrument_identity.price_currency
    else:
        asset_value = body.get("AssetType")
        asset = asset_value if isinstance(asset_value, str) else "instrument"
        unit = "unit" if request.quantity == 1 else "units"
        summary = f"{action} for {quantity} {asset} {unit} (UIC {request.instrument_uic})"
        price_currency = None
    order_type = body.get("OrderType")
    order_price = body.get("OrderPrice")
    stop_price = body.get("StopLimitPrice")
    if stop_price is None:
        stop_price = body.get("StopPrice")
    if isinstance(order_type, str):
        summary += f" as a {order_type} order"
        summary += _order_price_phrase(order_type, order_price, stop_price, price_currency)
    duration = body.get("OrderDuration")
    duration_type = duration.get("DurationType") if isinstance(duration, dict) else None
    if isinstance(duration_type, str):
        summary += f" valid for {duration_type}"
    return (
        f"{summary}, with estimated account impact {request.estimated_notional:g} "
        f"{request.account_currency}, on the allowlisted account"
    )


def _quantity_unit(quantity: float, asset_type: str) -> str:
    if asset_type == "Stock":
        return "share" if quantity == 1 else "shares"
    return "unit" if quantity == 1 else "units"


def _order_price_phrase(
    order_type: str,
    order_price: object,
    stop_price: object,
    price_currency: str | None,
) -> str:
    parts: list[str] = []
    if order_type in {"Limit", "StopLimit"} and isinstance(order_price, int | float):
        parts.append(f"at price {_format_money(order_price, price_currency)}")
    if order_type in {"Stop", "StopIfTraded", "StopLimit"} and isinstance(
        stop_price,
        int | float,
    ):
        parts.append(f"with stop price {_format_money(stop_price, price_currency)}")
    if not parts:
        return ""
    return " " + " and ".join(parts)


def _format_money(value: float, currency: str | None) -> str:
    amount = f"{value:g}"
    if currency is None:
        return f"{amount} in the instrument currency"
    return f"{amount} {currency}"
