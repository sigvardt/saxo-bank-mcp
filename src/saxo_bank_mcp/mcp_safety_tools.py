from __future__ import annotations

from typing import Annotated, Final

from fastmcp.tools import ToolResult
from pydantic import Field

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.nontrade_policy import (
    nontrade_refusal_reason,
    nontrade_safety_class,
    nontrade_write_operation_for_id,
)
from saxo_bank_mcp.safety import (
    AccountCurrencyRisk,
    PreviewResult,
    SafetyKernel,
    WritePreviewRequest,
)
from saxo_bank_mcp.tool_metadata import tool_metadata

SAFETY_STATUS_TOOL_DESCRIPTION: Final = (
    "Reports local write-safety configuration and preview state. Does not call Saxo or prove "
    "order placement, account access, trading readiness, or live-write readiness."
)
PREVIEW_TOOL_DESCRIPTION: Final = (
    "Creates a local write preview token after deterministic safety checks. Does not call Saxo, "
    "place orders, or change account state. The returned preview token is sensitive and only "
    "authorizes a later local simulation commit."
)
COMMIT_TOOL_DESCRIPTION: Final = (
    "Simulates local approval only when the preview token and a separate out-of-band approval "
    "factor are present. Agents must not derive or expose that factor. Does not call Saxo, place "
    "orders, change account state, or prove live-write readiness."
)


def saxo_safety_status() -> ToolResult:
    status = SafetyKernel().status()
    status["tool_metadata"] = tool_metadata()
    return ToolResult(structured_content=status)


def saxo_create_write_preview(  # noqa: PLR0913
    operation_id: Annotated[
        str,
        Field(description="Registered write operation identifier, for example trade.order.place"),
    ],
    account_key: Annotated[
        str | None,
        Field(description="Target account key; never echoed raw"),
    ] = None,
    instrument_uic: Annotated[
        int | None,
        Field(description="Target Saxo instrument UIC"),
    ] = None,
    quantity: Annotated[
        float | None,
        Field(gt=0, description="Requested order quantity"),
    ] = None,
    estimated_notional: Annotated[
        float | None,
        Field(ge=0, description="Estimated account-currency notional value"),
    ] = None,
    account_currency: Annotated[
        str | None,
        Field(description="Account currency for risk checks"),
    ] = None,
    risk: Annotated[
        AccountCurrencyRisk | None,
        Field(description="Account-currency risk values"),
    ] = None,
    request_body: Annotated[
        dict[str, JsonValue] | None,
        Field(description="Exact request body to bind into the preview fingerprint"),
    ] = None,
) -> PreviewResult | ToolResult:
    operation = nontrade_write_operation_for_id(operation_id)
    if operation is not None:
        payload: dict[str, JsonValue] = {
            "status": "denied",
            "tool_name": "saxo_create_write_preview",
            "operation_id": operation.operation_id,
            "service_group": operation.service_group,
            "safety_class": nontrade_safety_class(operation),
            "refusal_reason": nontrade_refusal_reason(operation),
            "preview_created": False,
            "approval_requested": False,
            "network_call_made": False,
            "order_or_subscription_created": False,
        }
        return ToolResult(structured_content=payload, is_error=True)
    if not (
        account_key is not None
        and instrument_uic is not None
        and quantity is not None
        and estimated_notional is not None
        and account_currency is not None
        and risk is not None
        and request_body is not None
    ):
        return ToolResult(
            structured_content=_missing_trade_preview_payload(
                operation_id,
                account_key=account_key,
                instrument_uic=instrument_uic,
                quantity=quantity,
                estimated_notional=estimated_notional,
                account_currency=account_currency,
                risk=risk,
                request_body=request_body,
            ),
            is_error=True,
        )
    request = WritePreviewRequest(
        operation_id=operation_id,
        account_key=account_key,
        instrument_uic=instrument_uic,
        quantity=quantity,
        estimated_notional=estimated_notional,
        account_currency=account_currency,
        risk=risk,
        request_body=request_body,
    )
    return SafetyKernel().create_preview(request)


def _missing_trade_preview_payload(  # noqa: PLR0913
    operation_id: str,
    *,
    account_key: str | None,
    instrument_uic: int | None,
    quantity: float | None,
    estimated_notional: float | None,
    account_currency: str | None,
    risk: AccountCurrencyRisk | None,
    request_body: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    missing: list[str] = []
    if account_key is None:
        missing.append("account_key")
    if instrument_uic is None:
        missing.append("instrument_uic")
    if quantity is None:
        missing.append("quantity")
    if estimated_notional is None:
        missing.append("estimated_notional")
    if account_currency is None:
        missing.append("account_currency")
    if risk is None:
        missing.append("risk")
    if request_body is None:
        missing.append("request_body")
    return {
        "status": "denied",
        "tool_name": "saxo_create_write_preview",
        "operation_id": operation_id,
        "denial_reasons": [f"missing_{name}" for name in missing],
        "preview_created": False,
        "approval_requested": False,
        "network_call_made": False,
        "order_or_subscription_created": False,
    }


def saxo_commit_write_preview(
    preview_token: Annotated[
        str,
        Field(description="Sensitive preview token returned by saxo_create_write_preview"),
    ],
    approval_factor: Annotated[
        str | None,
        Field(description="Separate approval factor; SIM tests use a test-only factor"),
    ] = None,
) -> PreviewResult:
    return SafetyKernel().commit_preview(preview_token, approval_factor=approval_factor)
