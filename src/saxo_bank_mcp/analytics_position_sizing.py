from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_FLOOR, Decimal, DecimalException, localcontext
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    InstrumentHandle,
    IsoCurrencyCode,
    QualityState,
    SafeAccountScope,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_portfolio import (
    PortfolioPublicEvidence,
    SaxoSourceBinding,
    assess_source_bindings,
    build_public_evidence,
    require_delivery_boundary,
)

_SOURCE_SCOPE: Final = "saxo_openapi"
_SIZING_SOURCE_CONTRACTS: Final = ("balances_v1", "positions_v1", "costs_v1")
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)

type SizingMethod = Literal["stop_distance", "volatility"]
type SizingConstraint = Literal["maximum_loss", "concentration", "buying_power", "margin"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class PositionSizingRequest(_StrictModel):
    """Explicit caller risk and exact account constraints for one sizing proposal."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    reporting_currency: IsoCurrencyCode
    method: SizingMethod
    maximum_loss: Decimal | None = Field(default=None, allow_inf_nan=False)
    risk_budget_confirmed: bool
    entry_price: Decimal = Field(gt=0, allow_inf_nan=False)
    stop_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    volatility_measure: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    volatility_multiplier: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    value_per_price_unit: Decimal = Field(gt=0, allow_inf_nan=False)
    lot_size: Decimal = Field(gt=0, allow_inf_nan=False)
    portfolio_value: Decimal = Field(gt=0, allow_inf_nan=False)
    maximum_weight: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    buying_power: Decimal = Field(ge=0, allow_inf_nan=False)
    reserved_buffer: Decimal = Field(ge=0, allow_inf_nan=False)
    estimated_transaction_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_headroom: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_requirement_per_money_unit: Decimal = Field(gt=0, allow_inf_nan=False)
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_quality(self) -> Self:
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial sizing quality must match explicit missing fields")
        return self


class PrivatePositionSizingValues(_StrictModel):
    """Owner-only quantities and money limits for a non-authorizing proposal."""

    reporting_currency: IsoCurrencyCode
    method: SizingMethod
    maximum_loss: Decimal = Field(gt=0, allow_inf_nan=False)
    risk_budget_source: Literal["caller_supplied"] = "caller_supplied"
    entry_price: Decimal = Field(gt=0, allow_inf_nan=False)
    stop_distance: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    volatility_measure: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    volatility_multiplier: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    unit_loss: Decimal = Field(gt=0, allow_inf_nan=False)
    lot_size: Decimal = Field(gt=0, allow_inf_nan=False)
    risk_quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    concentration_limit: Decimal = Field(ge=0, allow_inf_nan=False)
    concentration_quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    buying_power_limit: Decimal = Field(ge=0, allow_inf_nan=False)
    buying_power_quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_constraint: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    estimated_loss_at_invalidation: Decimal = Field(ge=0, allow_inf_nan=False)
    limiting_constraints: tuple[SizingConstraint, ...]

    @model_validator(mode="after")
    def validate_sizing_identities(self) -> Self:
        if self.estimated_loss_at_invalidation != self.quantity * self.unit_loss:
            raise ValueError("estimated sizing loss must equal quantity times unit loss")
        if self.quantity % self.lot_size != 0:
            raise ValueError("position quantity must be lot aligned")
        return self


class PositionSizingResult(_StrictModel):
    """Data-complete sizing proposal without advice, approval, or execution authority."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["position_sizing"] = "position_sizing"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivatePositionSizingValues | None
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence
    proposal_only: Literal[True] = True
    risk_budget_chosen_by_engine: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("position-sizing values do not match their delivery visibility")
        return self


def size_position(
    request: PositionSizingRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> PositionSizingResult | ResearchRefusal:
    """Return the largest lot-aligned quantity under every explicit constraint."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    source_assessment = assess_source_bindings(
        request.source_bindings,
        required_contract_ids=_SIZING_SOURCE_CONTRACTS,
        analysis_kind="position_sizing",
        dataset_id=request.dataset_id,
        instrument_handles=(request.instrument_handle,),
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if request.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            request,
            "position_sizing_data_unusable",
            "position-sizing inputs are missing, invalid, or stale",
        )
    if (
        request.maximum_loss is None
        or request.maximum_loss == 0
        or not request.risk_budget_confirmed
    ):
        return _refusal(
            request,
            "caller_risk_budget_required",
            "a non-zero caller-supplied and confirmed risk budget is required",
            missing_fields=("maximum_loss",),
        )
    try:
        values = _calculate_values(request)
    except _SizingInputError as exc:
        return _refusal(request, exc.reason_code, exc.reason, missing_fields=exc.missing_fields)
    except (ArithmeticError, DecimalException):
        return _refusal(
            request,
            "position_sizing_measure_undefined",
            "position sizing is undefined for the supplied finite inputs",
        )
    warnings = set(request.warnings)
    warnings.update(source_assessment)
    if request.quality_state is QualityState.PARTIAL:
        warnings.add("partial_position_sizing_inputs")
    if values.quantity == 0:
        warnings.add("position_size_zero")
    return PositionSizingResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=request.dataset_id,
        account_alias=request.account_alias,
        instrument_handle=request.instrument_handle,
        visibility=visibility,
        private_values=values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind="position_sizing",
            dataset_ids=(request.dataset_id,),
            account_aliases=(request.account_alias,),
            source_bindings=request.source_bindings,
            material=request,
        ),
    )


class _SizingInputError(ValueError):
    def __init__(
        self,
        reason_code: str,
        reason: str,
        *,
        missing_fields: Sequence[str] = (),
    ) -> None:
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason
        self.missing_fields = tuple(missing_fields)


def _calculate_values(request: PositionSizingRequest) -> PrivatePositionSizingValues:
    maximum_loss = abs(request.maximum_loss or Decimal(0))
    stop_distance: Decimal | None = None
    volatility_measure: Decimal | None = None
    volatility_multiplier: Decimal | None = None
    with localcontext() as context:
        context.prec = 50
        if request.method == "stop_distance":
            if request.stop_price is None:
                raise _SizingInputError(
                    "stop_price_required",
                    "stop-distance sizing requires an explicit stop or invalidation price",
                    missing_fields=("stop_price",),
                )
            stop_distance = abs(request.entry_price - request.stop_price)
            unit_loss = stop_distance * request.value_per_price_unit
        else:
            if request.volatility_measure is None or request.volatility_multiplier is None:
                raise _SizingInputError(
                    "volatility_sizing_inputs_required",
                    "volatility sizing requires an explicit measure and multiplier",
                    missing_fields=("volatility_measure", "volatility_multiplier"),
                )
            volatility_measure = request.volatility_measure
            volatility_multiplier = request.volatility_multiplier
            unit_loss = (
                volatility_measure * volatility_multiplier * request.value_per_price_unit
            )
        if unit_loss <= 0:
            raise _SizingInputError(
                "position_unit_loss_undefined",
                "the supplied sizing method produces no positive per-unit loss",
            )
        risk_quantity = maximum_loss / unit_loss
        concentration_limit = request.portfolio_value * request.maximum_weight
        concentration_quantity = concentration_limit / request.entry_price
        buying_power_limit = max(
            Decimal(0),
            request.buying_power
            - request.reserved_buffer
            - request.estimated_transaction_cost,
        )
        buying_power_quantity = buying_power_limit / request.entry_price
        margin_constraint = (
            max(Decimal(0), request.margin_headroom)
            / request.margin_requirement_per_money_unit
        )
        margin_quantity = margin_constraint / request.entry_price
        candidates: tuple[tuple[SizingConstraint, Decimal], ...] = (
            ("maximum_loss", risk_quantity),
            ("concentration", concentration_quantity),
            ("buying_power", buying_power_quantity),
            ("margin", margin_quantity),
        )
        raw_quantity = min(value for _, value in candidates)
        quantity = (
            (raw_quantity / request.lot_size).to_integral_value(rounding=ROUND_FLOOR)
            * request.lot_size
        )
        estimated_loss = quantity * unit_loss
    computed = (
        maximum_loss,
        unit_loss,
        risk_quantity,
        concentration_limit,
        concentration_quantity,
        buying_power_limit,
        buying_power_quantity,
        margin_constraint,
        margin_quantity,
        quantity,
        estimated_loss,
    )
    if not all(value.is_finite() for value in computed):
        raise ArithmeticError
    return PrivatePositionSizingValues(
        reporting_currency=request.reporting_currency,
        method=request.method,
        maximum_loss=maximum_loss,
        entry_price=request.entry_price,
        stop_distance=stop_distance,
        volatility_measure=volatility_measure,
        volatility_multiplier=volatility_multiplier,
        unit_loss=unit_loss,
        lot_size=request.lot_size,
        risk_quantity=risk_quantity,
        concentration_limit=concentration_limit,
        concentration_quantity=concentration_quantity,
        buying_power_limit=buying_power_limit,
        buying_power_quantity=buying_power_quantity,
        margin_constraint=margin_constraint,
        margin_quantity=margin_quantity,
        quantity=quantity,
        estimated_loss_at_invalidation=estimated_loss,
        limiting_constraints=tuple(
            name for name, value in candidates if value == raw_quantity
        ),
    )


def _refusal(
    request: PositionSizingRequest,
    reason_code: str,
    reason: str,
    *,
    missing_fields: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="position_sizing",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(request.dataset_id,),
        instrument_handles=(request.instrument_handle,),
        missing_fields=tuple(sorted(set(missing_fields))),
        source_scope=None,
    )


__all__ = (
    "PositionSizingRequest",
    "PositionSizingResult",
    "PrivatePositionSizingValues",
    "size_position",
)
