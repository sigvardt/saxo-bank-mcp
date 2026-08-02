from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_fx import FxNormalizationError, FxQuote, convert_amount
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    InstrumentHandle,
    IsoCurrencyCode,
    QualityState,
    SafeAccountScope,
    UtcDateTime,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_portfolio import (
    PortfolioPublicEvidence,
    ReconciliationSummary,
    SaxoSourceBinding,
    assess_source_bindings,
    build_public_evidence,
    require_delivery_boundary,
)

_SOURCE_SCOPE: Final = "saxo_openapi"
_EXPOSURE_TOLERANCE: Final = Decimal("0.01")
_EXPOSURE_SOURCE_CONTRACTS: Final = ("positions_v1", "exposure_instruments_v1")
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)

type ExposureInstrumentKind = Literal["cash", "option", "future", "cfd", "fx"]
type ExposureDifferenceReason = Literal[
    "contract_multiplier_alignment",
    "delta_snapshot_timing",
    "fx_translation",
    "valuation_timing",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ExposurePosition(_StrictModel):
    """One signed position with an explicit derivative-equivalent basis."""

    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    instrument_kind: ExposureInstrumentKind
    quantity: Decimal = Field(allow_inf_nan=False)
    reference_price: Decimal = Field(gt=0, allow_inf_nan=False)
    contract_multiplier: Decimal = Field(gt=0, allow_inf_nan=False)
    delta: Decimal | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    currency: IsoCurrencyCode
    broker_reported_exposure: Decimal | None = Field(default=None, allow_inf_nan=False)
    as_of: UtcDateTime

    @model_validator(mode="after")
    def validate_position(self) -> Self:
        if self.quantity == 0:
            raise ValueError("exposure quantity must be non-zero")
        if self.instrument_kind == "cash" and (
            self.delta != Decimal(1) or self.contract_multiplier != Decimal(1)
        ):
            raise ValueError("cash exposure requires unit delta and multiplier")
        return self


class NamedExposureDifference(_StrictModel):
    """Exact named money difference for one broker-reported instrument exposure."""

    instrument_handle: InstrumentHandle
    calculated_minus_saxo: Decimal = Field(allow_inf_nan=False)
    reason_code: ExposureDifferenceReason


class ExposureDataset(_StrictModel):
    """Bounded current exposures for exactly one safe account alias."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    reporting_currency: IsoCurrencyCode
    positions: tuple[ExposurePosition, ...] = Field(min_length=1)
    fx_quotes: tuple[FxQuote, ...]
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    named_differences: tuple[NamedExposureDifference, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if any(position.account_alias != self.account_alias for position in self.positions):
            raise ValueError("exposure position account alias must match the dataset account alias")
        if any(position.as_of != self.as_of for position in self.positions):
            raise ValueError("exposure positions must share the dataset cutoff")
        handles = tuple(position.instrument_handle for position in self.positions)
        if len(handles) != len(set(handles)):
            raise ValueError("exposure positions must use unique instrument handles")
        difference_handles = tuple(
            difference.instrument_handle for difference in self.named_differences
        )
        if len(difference_handles) != len(set(difference_handles)):
            raise ValueError("named exposure differences must be unique by instrument")
        if any(handle not in set(handles) for handle in difference_handles):
            raise ValueError("named exposure differences must match a dataset instrument")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial exposure quality must match explicit missing fields")
        return self


class PrivatePositionExposure(_StrictModel):
    """Owner-only reporting-currency exposure for one safe instrument handle."""

    instrument_handle: InstrumentHandle
    instrument_kind: ExposureInstrumentKind
    exposure: Decimal = Field(allow_inf_nan=False)
    gross_weight_percentage: Decimal = Field(allow_inf_nan=False)


class PrivateExposureValues(_StrictModel):
    """Owner-only long, short, gross, net, and allocation identities."""

    reporting_currency: IsoCurrencyCode
    positions: tuple[PrivatePositionExposure, ...]
    long_exposure: Decimal = Field(ge=0, allow_inf_nan=False)
    short_exposure: Decimal = Field(ge=0, allow_inf_nan=False)
    gross_exposure: Decimal = Field(gt=0, allow_inf_nan=False)
    net_exposure: Decimal = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        if self.long_exposure + self.short_exposure != self.gross_exposure:
            raise ValueError("long plus short exposure must equal gross exposure")
        if self.long_exposure - self.short_exposure != self.net_exposure:
            raise ValueError("long minus short exposure must equal net exposure")
        if sum((item.exposure for item in self.positions), Decimal(0)) != self.net_exposure:
            raise ValueError("position allocations must sum to net exposure")
        if sum(
            (abs(item.gross_weight_percentage) for item in self.positions),
            Decimal(0),
        ) != Decimal(100):
            raise ValueError("absolute gross position weights must sum to 100 percent")
        return self


class ExposureResult(_StrictModel):
    """Broker-reconciled exposure with owner-bound money values."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["portfolio_exposure"] = "portfolio_exposure"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateExposureValues | None
    reconciliation: ReconciliationSummary
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("exposure values do not match their delivery visibility")
        return self


def analyze_portfolio_exposure(  # noqa: PLR0911
    dataset: ExposureDataset,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> ExposureResult | ResearchRefusal:
    """Calculate signed delta-equivalent exposure and reconcile every broker total."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    handles = tuple(position.instrument_handle for position in dataset.positions)
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_EXPOSURE_SOURCE_CONTRACTS,
        analysis_kind="portfolio_exposure",
        dataset_id=dataset.dataset_id,
        instrument_handles=handles,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if dataset.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            dataset,
            "exposure_data_unusable",
            "exposure inputs are missing, invalid, or stale",
        )
    if any(
        position.instrument_kind in {"option", "future", "cfd", "fx"} and position.delta is None
        for position in dataset.positions
    ):
        return _refusal(
            dataset,
            "derivative_exposure_basis_missing",
            "a derivative position is missing its explicit delta-equivalent basis",
            missing_fields=("delta",),
        )
    try:
        exposures = tuple(_position_exposure(position, dataset) for position in dataset.positions)
        broker_exposures = tuple(
            _broker_exposure(position, dataset) for position in dataset.positions
        )
    except FxNormalizationError:
        return _refusal(
            dataset,
            "exposure_fx_basis_missing",
            "an eligible Saxo FX conversion is unavailable",
        )
    if any(value is None for value in broker_exposures):
        return _refusal(
            dataset,
            "saxo_exposure_total_missing",
            "a compatible broker exposure is missing",
            missing_fields=("broker_reported_exposure",),
        )
    reconciliation = _reconcile_exposures(
        dataset,
        exposures,
        tuple(value for value in broker_exposures if value is not None),
    )
    if isinstance(reconciliation, ResearchRefusal):
        return reconciliation
    values = _exposure_values(dataset.positions, exposures, dataset.reporting_currency)
    warnings = set(source_assessment)
    if dataset.quality_state is QualityState.PARTIAL:
        warnings.add("partial_history")
    if reconciliation.state == "named_difference":
        warnings.add("exposure_reconciled_with_named_difference")
    return ExposureResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        reconciliation=reconciliation,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind="portfolio_exposure",
            dataset_ids=(dataset.dataset_id,),
            account_aliases=(dataset.account_alias,),
            source_bindings=dataset.source_bindings,
            material=dataset,
        ),
    )


def _position_exposure(position: ExposurePosition, dataset: ExposureDataset) -> Decimal:
    if position.delta is None:
        raise ValueError("position delta must be present before exposure calculation")
    local = (
        position.quantity * position.reference_price * position.contract_multiplier * position.delta
    )
    return convert_amount(
        local,
        position.currency,
        dataset.reporting_currency,
        at=position.as_of,
        quotes=dataset.fx_quotes,
    )


def _broker_exposure(
    position: ExposurePosition,
    dataset: ExposureDataset,
) -> Decimal | None:
    if position.broker_reported_exposure is None:
        return None
    return convert_amount(
        position.broker_reported_exposure,
        position.currency,
        dataset.reporting_currency,
        at=position.as_of,
        quotes=dataset.fx_quotes,
    )


def _exposure_values(
    positions: tuple[ExposurePosition, ...],
    exposures: tuple[Decimal, ...],
    reporting_currency: str,
) -> PrivateExposureValues:
    long_exposure = sum((value for value in exposures if value > 0), Decimal(0))
    short_exposure = -sum((value for value in exposures if value < 0), Decimal(0))
    gross_exposure = long_exposure + short_exposure
    if gross_exposure <= 0:
        raise ValueError("gross exposure must be positive")
    values = tuple(
        PrivatePositionExposure(
            instrument_handle=position.instrument_handle,
            instrument_kind=position.instrument_kind,
            exposure=exposure,
            gross_weight_percentage=exposure / gross_exposure * Decimal(100),
        )
        for position, exposure in zip(positions, exposures, strict=True)
    )
    return PrivateExposureValues(
        reporting_currency=reporting_currency,
        positions=values,
        long_exposure=long_exposure,
        short_exposure=short_exposure,
        gross_exposure=gross_exposure,
        net_exposure=long_exposure - short_exposure,
    )


def _reconcile_exposures(
    dataset: ExposureDataset,
    calculated: tuple[Decimal, ...],
    broker: tuple[Decimal, ...],
) -> ReconciliationSummary | ResearchRefusal:
    observed = {
        position.instrument_handle: calculated_value - broker_value
        for position, calculated_value, broker_value in zip(
            dataset.positions,
            calculated,
            broker,
            strict=True,
        )
        if abs(calculated_value - broker_value) > _EXPOSURE_TOLERANCE
    }
    named = {difference.instrument_handle: difference for difference in dataset.named_differences}
    if set(named) != set(observed) or any(
        abs(named[handle].calculated_minus_saxo - difference) > _EXPOSURE_TOLERANCE
        for handle, difference in observed.items()
    ):
        return _refusal(
            dataset,
            "exposure_reconciliation_unexplained",
            "a compatible Saxo exposure difference is missing an exact named reason",
        )
    if not observed:
        return ReconciliationSummary(state="exact", reason_codes=())
    return ReconciliationSummary(
        state="named_difference",
        reason_codes=tuple(sorted(item.reason_code for item in named.values())),
    )


def _refusal(
    dataset: ExposureDataset,
    reason_code: str,
    reason: str,
    *,
    missing_fields: tuple[str, ...] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="portfolio_exposure",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=tuple(item.instrument_handle for item in dataset.positions),
        missing_fields=missing_fields,
        source_scope=None,
    )


__all__ = (
    "ExposureDataset",
    "ExposurePosition",
    "ExposureResult",
    "NamedExposureDifference",
    "PrivateExposureValues",
    "analyze_portfolio_exposure",
)
