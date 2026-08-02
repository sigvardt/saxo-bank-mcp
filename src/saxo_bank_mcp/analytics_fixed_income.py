from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import DatasetId, InstrumentHandle

type FixedIncomeEntitlement = Literal["available", "partial", "denied"]

_SOURCE_SCOPE: Final = "saxo_openapi"
_ROOT_ITERATIONS: Final = 256
_ROOT_PRICE_TOLERANCE: Final = 1e-13
_MAX_YIELD_BOUND: Final = 1_000_000.0


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class FixedIncomeCashFlow(_StrictModel):
    years_from_settlement: float = Field(gt=0, allow_inf_nan=False)
    amount: float = Field(gt=0, allow_inf_nan=False)


class FixedIncomeDataset(_StrictModel):
    """Authoritative Saxo fields required for bounded bond measures."""

    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    as_of: datetime
    entitlement_state: FixedIncomeEntitlement
    dirty_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cash_flows: tuple[FixedIncomeCashFlow, ...]
    compounding_frequency: int | None = Field(default=None, ge=1)
    day_count_basis: str | None = Field(default=None, min_length=1, max_length=40)
    settlement_at: datetime | None = None
    horizon_coupon_cashflows: float | None = Field(default=None, allow_inf_nan=False)
    accrued_interest_change: float | None = Field(default=None, allow_inf_nan=False)
    financing_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    same_curve_shorter_maturity_price: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
    )
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_dataset(self) -> Self:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() != timedelta(0):
            raise ValueError("fixed-income as-of timestamp must use UTC")
        if self.settlement_at is not None and (
            self.settlement_at.tzinfo is None or self.settlement_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("fixed-income settlement timestamp must use UTC")
        times = tuple(cash_flow.years_from_settlement for cash_flow in self.cash_flows)
        if times != tuple(sorted(times)):
            raise ValueError("fixed-income cash flows must be time ordered")
        return self


class FixedIncomeResearch(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["fixed_income_measures"] = "fixed_income_measures"
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    as_of: datetime
    dirty_price: float = Field(gt=0, allow_inf_nan=False)
    yield_to_maturity: float = Field(allow_inf_nan=False)
    modified_duration: float = Field(gt=0, allow_inf_nan=False)
    convexity: float = Field(gt=0, allow_inf_nan=False)
    carry: float | None = Field(default=None, allow_inf_nan=False)
    roll_down: float | None = Field(default=None, allow_inf_nan=False)
    day_count_basis: str
    compounding_frequency: int = Field(ge=1)
    missing_fields: tuple[str, ...]
    warnings: tuple[str, ...]


def _validated_cash_flows(
    cash_flows: Sequence[FixedIncomeCashFlow],
) -> tuple[FixedIncomeCashFlow, ...]:
    values = tuple(cash_flows)
    if not values:
        raise ValueError("at least one positive fixed-income cash flow is required")
    if any(
        not math.isfinite(item.years_from_settlement)
        or item.years_from_settlement <= 0.0
        or not math.isfinite(item.amount)
        or item.amount <= 0.0
        for item in values
    ):
        raise ValueError("fixed-income cash flows must be positive and finite")
    return values


def _frequency(value: int) -> int:
    if isinstance(value, bool) or value < 1:
        raise ValueError("compounding frequency must be a positive integer")
    return value


def price_from_yield(
    cash_flows: Sequence[FixedIncomeCashFlow],
    annual_yield: float,
    compounding_frequency: int,
) -> float:
    """Discount authoritative cash flows under declared periodic compounding."""
    values = _validated_cash_flows(cash_flows)
    frequency = _frequency(compounding_frequency)
    if not math.isfinite(annual_yield):
        raise ValueError("yield must be finite")
    periodic_base = 1.0 + annual_yield / frequency
    if periodic_base <= 0.0:
        raise ValueError("yield is outside the periodic-compounding domain")
    try:
        price = math.fsum(
            item.amount / math.pow(periodic_base, frequency * item.years_from_settlement)
            for item in values
        )
    except OverflowError as error:
        raise ValueError("discounted fixed-income price is not finite") from error
    if not math.isfinite(price) or price <= 0.0:
        raise ValueError("discounted fixed-income price must be positive and finite")
    return price


def yield_to_maturity(
    cash_flows: Sequence[FixedIncomeCashFlow],
    *,
    dirty_price: float,
    compounding_frequency: int,
) -> float:
    """Solve the unique periodic yield for positive authoritative cash flows."""
    values = _validated_cash_flows(cash_flows)
    frequency = _frequency(compounding_frequency)
    if not math.isfinite(dirty_price) or dirty_price <= 0.0:
        raise ValueError("dirty price must be positive and finite")

    lower = -float(frequency) + max(1e-9, frequency * 1e-9)
    upper = 1.0
    while price_from_yield(values, upper, frequency) > dirty_price:
        upper = upper * 2.0 + 1.0
        if upper > _MAX_YIELD_BOUND:
            raise ValueError("yield root could not be bounded")
    for _ in range(_ROOT_ITERATIONS):
        midpoint = (lower + upper) / 2.0
        midpoint_price = price_from_yield(values, midpoint, frequency)
        if abs(midpoint_price - dirty_price) <= _ROOT_PRICE_TOLERANCE:
            return midpoint
        if midpoint_price > dirty_price:
            lower = midpoint
        else:
            upper = midpoint
    result = (lower + upper) / 2.0
    if not math.isfinite(result):
        raise ValueError("yield root is not finite")
    return result


def modified_duration(
    cash_flows: Sequence[FixedIncomeCashFlow],
    annual_yield: float,
    compounding_frequency: int,
    *,
    dirty_price: float,
) -> float:
    """Return modified duration for the supplied dirty-price cash-flow basis."""
    values = _validated_cash_flows(cash_flows)
    frequency = _frequency(compounding_frequency)
    if not math.isfinite(dirty_price) or dirty_price <= 0.0:
        raise ValueError("dirty price must be positive and finite")
    periodic_base = 1.0 + annual_yield / frequency
    if periodic_base <= 0.0:
        raise ValueError("yield is outside the periodic-compounding domain")
    macaulay = (
        math.fsum(
            item.years_from_settlement
            * item.amount
            / math.pow(periodic_base, frequency * item.years_from_settlement)
            for item in values
        )
        / dirty_price
    )
    result = macaulay / periodic_base
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError("modified duration is not positive and finite")
    return result


def convexity(
    cash_flows: Sequence[FixedIncomeCashFlow],
    annual_yield: float,
    compounding_frequency: int,
    *,
    dirty_price: float,
) -> float:
    """Return modified convexity under the declared periodic compounding."""
    values = _validated_cash_flows(cash_flows)
    frequency = _frequency(compounding_frequency)
    if not math.isfinite(dirty_price) or dirty_price <= 0.0:
        raise ValueError("dirty price must be positive and finite")
    periodic_base = 1.0 + annual_yield / frequency
    if periodic_base <= 0.0:
        raise ValueError("yield is outside the periodic-compounding domain")
    result = (
        math.fsum(
            item.amount
            * item.years_from_settlement
            * (item.years_from_settlement + 1.0 / frequency)
            / math.pow(periodic_base, frequency * item.years_from_settlement + 2.0)
            for item in values
        )
        / dirty_price
    )
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError("convexity is not positive and finite")
    return result


def _core_missing_fields(dataset: FixedIncomeDataset) -> tuple[str, ...]:
    missing: list[str] = []
    if dataset.dirty_price is None:
        missing.append("dirty_price")
    if not dataset.cash_flows:
        missing.append("cash_flows")
    if dataset.compounding_frequency is None:
        missing.append("compounding_frequency")
    if dataset.day_count_basis is None:
        missing.append("day_count_basis")
    if dataset.settlement_at is None:
        missing.append("settlement_at")
    return tuple(sorted(missing))


def _dataset_refusal(
    dataset: FixedIncomeDataset,
    *,
    reason_code: str,
    reason: str,
    missing_fields: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="fixed_income_measures",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=(dataset.instrument_handle,),
        missing_fields=tuple(sorted(set(missing_fields))),
        warnings=dataset.warnings,
    )


def analyze_fixed_income(
    dataset: FixedIncomeDataset,
) -> FixedIncomeResearch | ResearchRefusal:
    """Compute bond measures only when authoritative Saxo fields are sufficient."""
    if dataset.entitlement_state != "available":
        return _dataset_refusal(
            dataset,
            reason_code="fixed_income_entitlement_insufficient",
            reason="Saxo entitlement is insufficient for fixed-income measures",
        )
    missing_core = _core_missing_fields(dataset)
    if missing_core:
        return _dataset_refusal(
            dataset,
            reason_code="fixed_income_fields_insufficient",
            reason="authoritative Saxo fixed-income fields are incomplete",
            missing_fields=missing_core,
        )
    dirty_price = dataset.dirty_price
    frequency = dataset.compounding_frequency
    day_count_basis = dataset.day_count_basis
    if dirty_price is None or frequency is None or day_count_basis is None:
        raise AssertionError("fixed-income core-field guard did not narrow values")
    try:
        solved_yield = yield_to_maturity(
            dataset.cash_flows,
            dirty_price=dirty_price,
            compounding_frequency=frequency,
        )
        duration_value = modified_duration(
            dataset.cash_flows,
            solved_yield,
            frequency,
            dirty_price=dirty_price,
        )
        convexity_value = convexity(
            dataset.cash_flows,
            solved_yield,
            frequency,
            dirty_price=dirty_price,
        )
    except ValueError:
        return _dataset_refusal(
            dataset,
            reason_code="fixed_income_measure_undefined",
            reason="the authoritative Saxo fields do not define stable bond measures",
        )

    optional_fields = {
        "horizon_coupon_cashflows": dataset.horizon_coupon_cashflows,
        "accrued_interest_change": dataset.accrued_interest_change,
        "financing_cost": dataset.financing_cost,
        "same_curve_shorter_maturity_price": dataset.same_curve_shorter_maturity_price,
    }
    missing_optional = tuple(
        sorted(name for name, value in optional_fields.items() if value is None)
    )
    carry: float | None = None
    if (
        dataset.horizon_coupon_cashflows is not None
        and dataset.accrued_interest_change is not None
        and dataset.financing_cost is not None
    ):
        carry = (
            dataset.horizon_coupon_cashflows
            + dataset.accrued_interest_change
            - dataset.financing_cost
        )
    roll_down = (
        dataset.same_curve_shorter_maturity_price - dirty_price
        if dataset.same_curve_shorter_maturity_price is not None
        else None
    )
    warnings = set(dataset.warnings)
    if missing_optional:
        warnings.add("optional_fixed_income_fields_missing")
    return FixedIncomeResearch(
        status=(
            ResearchStatus.REDUCED if missing_optional or warnings else ResearchStatus.COMPLETE
        ),
        dataset_id=dataset.dataset_id,
        instrument_handle=dataset.instrument_handle,
        as_of=dataset.as_of,
        dirty_price=dirty_price,
        yield_to_maturity=solved_yield,
        modified_duration=duration_value,
        convexity=convexity_value,
        carry=carry,
        roll_down=roll_down,
        day_count_basis=day_count_basis,
        compounding_frequency=frequency,
        missing_fields=missing_optional,
        warnings=tuple(sorted(warnings)),
    )


__all__ = (
    "FixedIncomeCashFlow",
    "FixedIncomeDataset",
    "FixedIncomeResearch",
    "ResearchRefusal",
    "ResearchStatus",
    "analyze_fixed_income",
    "convexity",
    "modified_duration",
    "price_from_yield",
    "yield_to_maturity",
)
