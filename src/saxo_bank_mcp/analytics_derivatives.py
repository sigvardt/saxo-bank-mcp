from __future__ import annotations

import math
from collections.abc import Sequence
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
    UtcDateTime,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_options import (
    OptionGreekComparison,
    OptionGreeks,
    OptionModelInput,
    OptionModelValues,
    OptionStrategyRequest,
    StrategyGreeks,
    StrategyPayoffPoint,
    aggregate_strategy_greeks,
    compare_greek_vectors,
    model_option,
    strategy_payoff,
)
from saxo_bank_mcp.analytics_portfolio import (
    PortfolioPublicEvidence,
    SaxoSourceBinding,
    assess_source_bindings,
    build_public_evidence,
    require_delivery_boundary,
)
from saxo_bank_mcp.analytics_source_contracts import (
    SourceValueSchema,
    source_contracts_by_id,
)

type DerivativeAnalysisKind = Literal[
    "derivatives_model",
    "option_payoff",
    "iv_surface",
    "option_chain",
    "futures_curve",
    "fx_forward_carry",
]

_SOURCE_SCOPE: Final = "saxo_openapi"
_OPTION_SOURCE_CONTRACTS: Final = (
    "options_chain_reference_v1",
    "info_price_v1",
)
_FUTURES_SOURCE_CONTRACTS: Final = (
    "reference_instruments_v1",
    "info_price_v1",
)
_FX_SOURCE_CONTRACTS: Final = _FUTURES_SOURCE_CONTRACTS
_LIFECYCLE_SOURCE_CONTRACTS: Final = (
    "options_chain_reference_v1",
    "positions_v1",
    "corporate_action_events_v2",
)
_GREEK_FIELD_NAMES: Final = frozenset({"delta", "gamma", "theta", "vega", "rho"})
_GREEK_ABSOLUTE_TOLERANCE: Final = 1e-9
_GREEK_RELATIVE_TOLERANCE: Final = 1e-7
_MAXIMUM_SURFACE_POINTS: Final = 100
_MAXIMUM_LIFECYCLE_POSITIONS: Final = 200
_FUTURES_HANDLE_COUNT: Final = 3


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class DerivativeDataset(_StrictModel):
    """One complete Saxo-bound derivative input cutoff using only safe handles."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    instrument_handles: tuple[InstrumentHandle, ...] = Field(min_length=1)
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if len(self.instrument_handles) != len(set(self.instrument_handles)):
            raise ValueError("derivative dataset instrument handles must be unique")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial derivative quality must match explicit missing fields")
        return self


class SaxoGreekSnapshot(_StrictModel):
    """Optional broker Greek claim accepted only when its bound contract proves every field."""

    instrument_handle: InstrumentHandle
    observed_at: UtcDateTime
    source_binding: SaxoSourceBinding
    greeks: OptionGreeks


class OptionAnalyticsValues(_StrictModel):
    model: OptionModelValues
    saxo_greek_comparison: OptionGreekComparison | None


class StrategyAnalyticsValues(_StrictModel):
    payoff_points: tuple[StrategyPayoffPoint, ...]
    aggregate_greeks: StrategyGreeks


class IvSurfacePoint(_StrictModel):
    instrument_handle: InstrumentHandle
    expiry_at: UtcDateTime
    time_to_expiry_years: float = Field(gt=0, allow_inf_nan=False)
    strike: float = Field(gt=0, allow_inf_nan=False)
    signed_delta: float = Field(ge=-1, le=1, allow_inf_nan=False)
    moneyness: float = Field(gt=0, allow_inf_nan=False)
    implied_volatility: float = Field(gt=0, allow_inf_nan=False)


class IvSurfaceRequest(_StrictModel):
    """Exact smile points and exact-match term points; no hidden interpolation."""

    smile_axis: Literal["strike", "delta"]
    smile_expiry_at: UtcDateTime
    smile_points: tuple[IvSurfacePoint, ...] = Field(
        min_length=2,
        max_length=_MAXIMUM_SURFACE_POINTS,
    )
    term_selector: Literal["moneyness", "delta"]
    term_selector_value: float = Field(allow_inf_nan=False)
    term_interpolation_rule: Literal["exact_match", "linear"]
    term_points: tuple[IvSurfacePoint, ...] = Field(
        min_length=2,
        max_length=_MAXIMUM_SURFACE_POINTS,
    )


class IvSurfaceValues(_StrictModel):
    smile_axis: Literal["strike", "delta"]
    smile_points: tuple[IvSurfacePoint, ...]
    skew: float = Field(allow_inf_nan=False)
    term_selector: Literal["moneyness", "delta"]
    term_selector_value: float = Field(allow_inf_nan=False)
    term_points: tuple[IvSurfacePoint, ...]
    metric_class: Literal["model_output"] = "model_output"


class LifecyclePosition(_StrictModel):
    instrument_handle: InstrumentHandle
    option_type: Literal["call", "put"]
    exercise_style: Literal["european", "american"]
    quantity: float = Field(allow_inf_nan=False)
    strike: float = Field(gt=0, allow_inf_nan=False)
    reference_price: float = Field(gt=0, allow_inf_nan=False)
    expiry_at: UtcDateTime
    assignment_state: Literal["none", "reported"]

    @model_validator(mode="after")
    def validate_quantity(self) -> Self:
        if self.quantity == 0.0:
            raise ValueError("lifecycle position quantity must be non-zero")
        return self


class LifecycleRadarRequest(_StrictModel):
    horizon_days: int = Field(ge=0, le=366)
    positions: tuple[LifecyclePosition, ...] = Field(
        min_length=1,
        max_length=_MAXIMUM_LIFECYCLE_POSITIONS,
    )


class LifecycleRadarEvent(_StrictModel):
    instrument_handle: InstrumentHandle
    expiry_at: UtcDateTime
    days_to_expiry: int = Field(ge=0)
    quantity: float = Field(allow_inf_nan=False)
    moneyness_state: Literal["in_the_money", "at_the_money", "out_of_the_money"]
    assignment_label: Literal["none", "reported", "short_itm_expiry_exposure"]


class LifecycleRadarValues(_StrictModel):
    horizon_days: int = Field(ge=0)
    events: tuple[LifecycleRadarEvent, ...]


class FuturesContractPoint(_StrictModel):
    instrument_handle: InstrumentHandle
    expiry_at: UtcDateTime
    time_to_expiry_years: float = Field(gt=0, allow_inf_nan=False)
    price: float = Field(gt=0, allow_inf_nan=False)


class FuturesCurveRequest(_StrictModel):
    spot_instrument_handle: InstrumentHandle
    spot_price: float = Field(gt=0, allow_inf_nan=False)
    near_contract: FuturesContractPoint
    later_contract: FuturesContractPoint
    quantity: float = Field(allow_inf_nan=False)
    contract_multiplier: float = Field(gt=0, allow_inf_nan=False)
    eligible_roll_costs: float = Field(ge=0, allow_inf_nan=False)
    reporting_currency: IsoCurrencyCode

    @model_validator(mode="after")
    def validate_handles(self) -> Self:
        handles = {
            self.spot_instrument_handle,
            self.near_contract.instrument_handle,
            self.later_contract.instrument_handle,
        }
        if len(handles) != _FUTURES_HANDLE_COUNT:
            raise ValueError("spot, near, and later futures handles must be distinct")
        return self


class FuturesCurveValues(_StrictModel):
    basis: float = Field(allow_inf_nan=False)
    annualized_carry: float = Field(allow_inf_nan=False)
    term_structure: float = Field(allow_inf_nan=False)
    modeled_roll: float = Field(allow_inf_nan=False)
    reporting_currency: IsoCurrencyCode
    metric_class: Literal["model_output"] = "model_output"


class FxForwardRequest(_StrictModel):
    spot_instrument_handle: InstrumentHandle
    base_currency: IsoCurrencyCode
    quote_currency: IsoCurrencyCode
    spot: float = Field(gt=0, allow_inf_nan=False)
    time_to_expiry_years: float = Field(gt=0, allow_inf_nan=False)
    base_rate: float = Field(allow_inf_nan=False)
    quote_rate: float = Field(allow_inf_nan=False)
    rate_model: Literal["simple", "complex"]


class FxForwardValues(_StrictModel):
    base_currency: IsoCurrencyCode
    quote_currency: IsoCurrencyCode
    forward: float = Field(gt=0, allow_inf_nan=False)
    annualized_carry: float = Field(allow_inf_nan=False)
    metric_class: Literal["model_output"] = "model_output"


type DerivativePrivateValues = (
    OptionAnalyticsValues
    | StrategyAnalyticsValues
    | IvSurfaceValues
    | LifecycleRadarValues
    | FuturesCurveValues
    | FxForwardValues
)


class DerivativeAnalyticsResult(_StrictModel):
    """Owner-only derivative values with value-free public evidence and no authority."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: DerivativeAnalysisKind
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: DerivativePrivateValues | None
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence
    is_not_advice: Literal[True] = True
    is_not_forecast: Literal[True] = True
    model_distribution_only: Literal[False] = False
    recommendation_authority: Literal[False] = False
    order_creation_authority: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def validate_delivery_and_kind(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("derivative values do not match their delivery visibility")
        expected_types: dict[str, type[_StrictModel]] = {
            "derivatives_model": OptionAnalyticsValues,
            "option_payoff": StrategyAnalyticsValues,
            "iv_surface": IvSurfaceValues,
            "option_chain": LifecycleRadarValues,
            "futures_curve": FuturesCurveValues,
            "fx_forward_carry": FxForwardValues,
        }
        if self.private_values is not None and not isinstance(
            self.private_values,
            expected_types[self.analysis_kind],
        ):
            raise ValueError("derivative analysis kind does not match its private values")
        return self


class _StrategyMaterial(_StrictModel):
    strategy: OptionStrategyRequest
    expiry_reference_prices: tuple[float, ...]


def _refusal(  # noqa: PLR0913
    dataset: DerivativeDataset,
    *,
    analysis_kind: DerivativeAnalysisKind,
    reason_code: str,
    reason: str,
    instrument_handles: Sequence[str] | None = None,
    missing_fields: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind=analysis_kind,
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=(
            tuple(dataset.instrument_handles)
            if instrument_handles is None
            else tuple(instrument_handles)
        ),
        missing_fields=tuple(sorted(set(missing_fields))),
        warnings=tuple(sorted({*dataset.warnings, *warnings})),
        source_scope=None,
    )


def _assess_dataset(
    dataset: DerivativeDataset,
    *,
    analysis_kind: DerivativeAnalysisKind,
    required_contract_ids: Sequence[str],
    instrument_handles: Sequence[str],
) -> ResearchRefusal | None:
    if dataset.quality_state is not QualityState.COMPLETE:
        return _refusal(
            dataset,
            analysis_kind=analysis_kind,
            reason_code="derivative_source_incomplete",
            reason="derivative analysis requires complete, current source data",
            instrument_handles=instrument_handles,
            missing_fields=dataset.missing_fields,
        )
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=required_contract_ids,
        analysis_kind=analysis_kind,
        dataset_id=dataset.dataset_id,
        instrument_handles=instrument_handles,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if source_assessment:
        return _refusal(
            dataset,
            analysis_kind=analysis_kind,
            reason_code="derivative_source_incomplete",
            reason="derivative analysis requires complete entitlement and source coverage",
            instrument_handles=instrument_handles,
            warnings=source_assessment,
        )
    missing_handles = tuple(
        sorted(set(instrument_handles) - set(dataset.instrument_handles)),
    )
    if missing_handles:
        return _refusal(
            dataset,
            analysis_kind=analysis_kind,
            reason_code="derivative_handle_scope_mismatch",
            reason="a requested derivative handle is outside the bound dataset",
            instrument_handles=instrument_handles,
            missing_fields=missing_handles,
        )
    return None


def _result(  # noqa: PLR0913
    dataset: DerivativeDataset,
    *,
    analysis_kind: DerivativeAnalysisKind,
    private_values: DerivativePrivateValues,
    visibility: VisibilityMode,
    trusted_local_host: bool,
    warnings: Sequence[str],
    material: BaseModel | Sequence[BaseModel],
) -> DerivativeAnalyticsResult:
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    combined_warnings = tuple(sorted({*dataset.warnings, *warnings}))
    return DerivativeAnalyticsResult(
        status=(
            ResearchStatus.REDUCED if combined_warnings else ResearchStatus.COMPLETE
        ),
        analysis_kind=analysis_kind,
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        visibility=visibility,
        private_values=private_values if private_delivery else None,
        warnings=combined_warnings,
        evidence=build_public_evidence(
            analysis_kind=analysis_kind,
            dataset_ids=(dataset.dataset_id,),
            account_aliases=(dataset.account_alias,),
            source_bindings=dataset.source_bindings,
            material=material,
        ),
    )


def analyze_option_model(
    dataset: DerivativeDataset,
    model_input: OptionModelInput,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
    saxo_greeks: SaxoGreekSnapshot | None = None,
) -> DerivativeAnalyticsResult | ResearchRefusal:
    """Evaluate one supported option with complete source proof and optional Greek comparison."""
    contract = model_input.contract
    handles = (contract.option_handle, contract.underlying_handle)
    assessment = _assess_dataset(
        dataset,
        analysis_kind="derivatives_model",
        required_contract_ids=_OPTION_SOURCE_CONTRACTS,
        instrument_handles=handles,
    )
    if assessment is not None:
        return assessment
    if contract.dataset_id != dataset.dataset_id:
        return _refusal(
            dataset,
            analysis_kind="derivatives_model",
            reason_code="derivative_dataset_mismatch",
            reason="the option model input does not match the bound dataset",
            instrument_handles=handles,
        )
    modeled = model_option(model_input)
    if isinstance(modeled, ResearchRefusal):
        return modeled
    warnings = set(modeled.warnings)
    comparison: OptionGreekComparison | None = None
    material: tuple[BaseModel, ...] = (model_input,)
    if saxo_greeks is None:
        warnings.add("saxo_greeks_not_compared")
    else:
        material = (model_input, saxo_greeks)
        comparison_refusal = _validate_saxo_greek_snapshot(
            dataset,
            contract.option_handle,
            saxo_greeks,
        )
        if comparison_refusal is not None:
            return comparison_refusal
        if modeled.greeks is None:
            return _refusal(
                dataset,
                analysis_kind="derivatives_model",
                reason_code="saxo_greeks_comparison_undefined",
                reason="modeled Greeks are unavailable at expiry",
                instrument_handles=handles,
            )
        comparison = compare_greek_vectors(
            modeled.greeks,
            saxo_greeks.greeks,
            absolute_tolerance=_GREEK_ABSOLUTE_TOLERANCE,
            relative_tolerance=_GREEK_RELATIVE_TOLERANCE,
        )
        if comparison.state == "disagreement":
            warnings.add("saxo_greeks_disagreement")
    return _result(
        dataset,
        analysis_kind="derivatives_model",
        private_values=OptionAnalyticsValues(
            model=modeled,
            saxo_greek_comparison=comparison,
        ),
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        warnings=tuple(warnings),
        material=material,
    )


def _validate_saxo_greek_snapshot(
    dataset: DerivativeDataset,
    option_handle: str,
    snapshot: SaxoGreekSnapshot,
) -> ResearchRefusal | None:
    if snapshot.instrument_handle != option_handle or snapshot.observed_at > dataset.as_of:
        return _refusal(
            dataset,
            analysis_kind="derivatives_model",
            reason_code="saxo_greeks_snapshot_mismatch",
            reason="the Saxo Greek snapshot does not match the modeled option and cutoff",
            instrument_handles=(option_handle,),
        )
    if snapshot.source_binding not in dataset.source_bindings:
        return _refusal(
            dataset,
            analysis_kind="derivatives_model",
            reason_code="saxo_greeks_source_contract_unbound",
            reason="the Saxo Greek snapshot is not bound to the derivative dataset",
            instrument_handles=(option_handle,),
        )
    binding = snapshot.source_binding
    if (
        binding.entitlement_state != "available"
        or binding.quality_state is not QualityState.COMPLETE
    ):
        return _refusal(
            dataset,
            analysis_kind="derivatives_model",
            reason_code="saxo_greeks_source_insufficient",
            reason="the Saxo Greek snapshot is not complete and entitled",
            instrument_handles=(option_handle,),
        )
    contract = source_contracts_by_id().get(binding.contract_id)
    declared_fields: frozenset[str] = frozenset()
    if contract is not None:
        declared_fields = frozenset(
            {
                *(field.name.lower() for field in contract.fields),
                *(
                    field_name.lower()
                    for field in contract.fields
                    for field_name in _schema_field_names(field)
                ),
            },
        )
    if not declared_fields.issuperset(_GREEK_FIELD_NAMES):
        return _refusal(
            dataset,
            analysis_kind="derivatives_model",
            reason_code="saxo_greeks_source_contract_unbound",
            reason="no current bound Saxo source contract proves every supplied Greek field",
            instrument_handles=(option_handle,),
        )
    return None


def _schema_field_names(schema: SourceValueSchema) -> tuple[str, ...]:
    names: list[str] = []
    for property_schema in schema.properties:
        names.append(property_schema.name)
        names.extend(_schema_field_names(property_schema))
    if schema.items is not None:
        names.extend(_schema_field_names(schema.items))
    return tuple(names)


def analyze_option_strategy(
    dataset: DerivativeDataset,
    request: OptionStrategyRequest,
    *,
    expiry_reference_prices: Sequence[float],
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> DerivativeAnalyticsResult | ResearchRefusal:
    """Return bounded strategy payoff points and exact aggregate Greeks."""
    handles = tuple(
        dict.fromkeys(
            (
                *(leg.model_input.contract.option_handle for leg in request.legs),
                request.legs[0].model_input.contract.underlying_handle,
            ),
        ),
    )
    assessment = _assess_dataset(
        dataset,
        analysis_kind="option_payoff",
        required_contract_ids=_OPTION_SOURCE_CONTRACTS,
        instrument_handles=handles,
    )
    if assessment is not None:
        return assessment
    if request.dataset_id != dataset.dataset_id or request.account_alias != dataset.account_alias:
        return _refusal(
            dataset,
            analysis_kind="option_payoff",
            reason_code="derivative_dataset_mismatch",
            reason="the option strategy does not match the bound dataset and account alias",
            instrument_handles=handles,
        )
    price_values = tuple(expiry_reference_prices)
    if not price_values:
        return _refusal(
            dataset,
            analysis_kind="option_payoff",
            reason_code="option_payoff_grid_empty",
            reason="at least one explicit expiry reference price is required",
            instrument_handles=handles,
        )
    points: list[StrategyPayoffPoint] = []
    for price in price_values:
        point = strategy_payoff(request, expiry_reference_price=price)
        if isinstance(point, ResearchRefusal):
            return point
        points.append(point)
    aggregate = aggregate_strategy_greeks(request)
    if isinstance(aggregate, ResearchRefusal):
        return aggregate
    material = _StrategyMaterial(
        strategy=request,
        expiry_reference_prices=price_values,
    )
    return _result(
        dataset,
        analysis_kind="option_payoff",
        private_values=StrategyAnalyticsValues(
            payoff_points=tuple(points),
            aggregate_greeks=aggregate,
        ),
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        warnings=("saxo_greeks_not_compared",),
        material=material,
    )


def analyze_iv_surface(  # noqa: PLR0911
    dataset: DerivativeDataset,
    request: IvSurfaceRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> DerivativeAnalyticsResult | ResearchRefusal:
    """Build exact-axis smile, skew, and exact-match term structure."""
    handles = tuple(
        dict.fromkeys(
            point.instrument_handle
            for point in (*request.smile_points, *request.term_points)
        ),
    )
    assessment = _assess_dataset(
        dataset,
        analysis_kind="iv_surface",
        required_contract_ids=_OPTION_SOURCE_CONTRACTS,
        instrument_handles=handles,
    )
    if assessment is not None:
        return assessment
    if request.term_interpolation_rule != "exact_match":
        return _refusal(
            dataset,
            analysis_kind="iv_surface",
            reason_code="iv_interpolation_unsupported",
            reason="the bounded term structure supports exact matches and no interpolation",
            instrument_handles=handles,
        )
    if any(point.expiry_at != request.smile_expiry_at for point in request.smile_points):
        return _refusal(
            dataset,
            analysis_kind="iv_surface",
            reason_code="iv_smile_expiry_mismatch",
            reason="every smile point must use one exact expiry",
            instrument_handles=handles,
        )
    coordinate_name = "strike" if request.smile_axis == "strike" else "signed_delta"
    ordered_smile = tuple(
        sorted(request.smile_points, key=lambda point: getattr(point, coordinate_name)),
    )
    smile_coordinates = tuple(
        float(getattr(point, coordinate_name)) for point in ordered_smile
    )
    if len(smile_coordinates) != len(set(smile_coordinates)):
        return _refusal(
            dataset,
            analysis_kind="iv_surface",
            reason_code="iv_smile_axis_duplicate",
            reason="smile axis coordinates must be unique",
            instrument_handles=handles,
        )
    term_coordinate_name = (
        "moneyness" if request.term_selector == "moneyness" else "signed_delta"
    )
    if any(
        float(getattr(point, term_coordinate_name)) != request.term_selector_value
        for point in request.term_points
    ):
        return _refusal(
            dataset,
            analysis_kind="iv_surface",
            reason_code="iv_term_exact_match_required",
            reason="every term point must exactly match the declared selector",
            instrument_handles=handles,
        )
    ordered_term = tuple(
        sorted(request.term_points, key=lambda point: point.time_to_expiry_years),
    )
    expiries = tuple(point.expiry_at for point in ordered_term)
    times = tuple(point.time_to_expiry_years for point in ordered_term)
    if len(expiries) != len(set(expiries)) or len(times) != len(set(times)):
        return _refusal(
            dataset,
            analysis_kind="iv_surface",
            reason_code="iv_term_coordinate_duplicate",
            reason="term-structure expiries and times must be unique",
            instrument_handles=handles,
        )
    axis_difference = smile_coordinates[-1] - smile_coordinates[0]
    if axis_difference == 0.0:
        return _refusal(
            dataset,
            analysis_kind="iv_surface",
            reason_code="iv_smile_axis_duplicate",
            reason="smile skew requires distinct axis coordinates",
            instrument_handles=handles,
        )
    skew = (
        ordered_smile[-1].implied_volatility
        - ordered_smile[0].implied_volatility
    ) / axis_difference
    if not math.isfinite(skew):
        return _refusal(
            dataset,
            analysis_kind="iv_surface",
            reason_code="iv_surface_undefined",
            reason="the supplied points do not define a finite smile skew",
            instrument_handles=handles,
        )
    return _result(
        dataset,
        analysis_kind="iv_surface",
        private_values=IvSurfaceValues(
            smile_axis=request.smile_axis,
            smile_points=ordered_smile,
            skew=skew,
            term_selector=request.term_selector,
            term_selector_value=request.term_selector_value,
            term_points=ordered_term,
        ),
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        warnings=(),
        material=request,
    )


def analyze_lifecycle_radar(
    dataset: DerivativeDataset,
    request: LifecycleRadarRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> DerivativeAnalyticsResult | ResearchRefusal:
    """Return deterministic expiry and reported-assignment states within one horizon."""
    handles = tuple(dict.fromkeys(position.instrument_handle for position in request.positions))
    assessment = _assess_dataset(
        dataset,
        analysis_kind="option_chain",
        required_contract_ids=_LIFECYCLE_SOURCE_CONTRACTS,
        instrument_handles=handles,
    )
    if assessment is not None:
        return assessment
    if any(position.exercise_style == "american" for position in request.positions):
        return _refusal(
            dataset,
            analysis_kind="option_chain",
            reason_code="option_american_unsupported",
            reason="American exercise is outside the bounded lifecycle capability",
            instrument_handles=handles,
        )
    horizon_seconds = float(request.horizon_days * 86_400)
    events: list[LifecycleRadarEvent] = []
    for position in request.positions:
        seconds = (position.expiry_at - dataset.as_of).total_seconds()
        if seconds < 0.0 or seconds > horizon_seconds:
            continue
        if position.reference_price == position.strike:
            moneyness_state = "at_the_money"
        elif (
            position.option_type == "call"
            and position.reference_price > position.strike
        ) or (
            position.option_type == "put"
            and position.reference_price < position.strike
        ):
            moneyness_state = "in_the_money"
        else:
            moneyness_state = "out_of_the_money"
        if position.assignment_state == "reported":
            assignment_label = "reported"
        elif position.quantity < 0.0 and moneyness_state == "in_the_money":
            assignment_label = "short_itm_expiry_exposure"
        else:
            assignment_label = "none"
        events.append(
            LifecycleRadarEvent(
                instrument_handle=position.instrument_handle,
                expiry_at=position.expiry_at,
                days_to_expiry=math.ceil(seconds / 86_400.0),
                quantity=position.quantity,
                moneyness_state=moneyness_state,
                assignment_label=assignment_label,
            ),
        )
    events.sort(key=lambda event: (event.expiry_at, event.instrument_handle))
    return _result(
        dataset,
        analysis_kind="option_chain",
        private_values=LifecycleRadarValues(
            horizon_days=request.horizon_days,
            events=tuple(events),
        ),
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        warnings=(),
        material=request,
    )


def analyze_futures_curve(
    dataset: DerivativeDataset,
    request: FuturesCurveRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> DerivativeAnalyticsResult | ResearchRefusal:
    """Calculate frozen futures basis, carry, relative term structure, and roll."""
    handles = (
        request.spot_instrument_handle,
        request.near_contract.instrument_handle,
        request.later_contract.instrument_handle,
    )
    assessment = _assess_dataset(
        dataset,
        analysis_kind="futures_curve",
        required_contract_ids=_FUTURES_SOURCE_CONTRACTS,
        instrument_handles=handles,
    )
    if assessment is not None:
        return assessment
    if (
        request.later_contract.time_to_expiry_years
        <= request.near_contract.time_to_expiry_years
        or request.later_contract.expiry_at <= request.near_contract.expiry_at
    ):
        return _refusal(
            dataset,
            analysis_kind="futures_curve",
            reason_code="futures_curve_order_invalid",
            reason="later futures contract time and expiry must follow the near contract",
            instrument_handles=handles,
        )
    try:
        basis = request.near_contract.price - request.spot_price
        carry = (
            request.near_contract.price / request.spot_price - 1.0
        ) / request.near_contract.time_to_expiry_years
        term_structure = (
            request.later_contract.price / request.near_contract.price - 1.0
        )
        modeled_roll = (
            request.quantity
            * request.contract_multiplier
            * (request.near_contract.price - request.later_contract.price)
            - request.eligible_roll_costs
        )
    except (ArithmeticError, OverflowError, ZeroDivisionError):
        return _refusal(
            dataset,
            analysis_kind="futures_curve",
            reason_code="futures_curve_undefined",
            reason="the supplied finite inputs do not define stable futures measures",
            instrument_handles=handles,
        )
    if any(
        not math.isfinite(value)
        for value in (basis, carry, term_structure, modeled_roll)
    ):
        return _refusal(
            dataset,
            analysis_kind="futures_curve",
            reason_code="futures_curve_undefined",
            reason="the supplied finite inputs do not define stable futures measures",
            instrument_handles=handles,
        )
    return _result(
        dataset,
        analysis_kind="futures_curve",
        private_values=FuturesCurveValues(
            basis=basis,
            annualized_carry=carry,
            term_structure=term_structure,
            modeled_roll=modeled_roll,
            reporting_currency=request.reporting_currency,
        ),
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        warnings=(),
        material=request,
    )


def analyze_fx_forward(  # noqa: PLR0911
    dataset: DerivativeDataset,
    request: FxForwardRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> DerivativeAnalyticsResult | ResearchRefusal:
    """Calculate simple-rate covered-interest-parity FX forward and carry."""
    handles = (request.spot_instrument_handle,)
    assessment = _assess_dataset(
        dataset,
        analysis_kind="fx_forward_carry",
        required_contract_ids=_FX_SOURCE_CONTRACTS,
        instrument_handles=handles,
    )
    if assessment is not None:
        return assessment
    if request.rate_model == "complex":
        return _refusal(
            dataset,
            analysis_kind="fx_forward_carry",
            reason_code="fx_complex_rates_unsupported",
            reason="complex rate curves are outside the simple covered-interest capability",
            instrument_handles=handles,
        )
    if request.base_currency == request.quote_currency:
        return _refusal(
            dataset,
            analysis_kind="fx_forward_carry",
            reason_code="fx_pair_invalid",
            reason="FX forward base and quote currencies must differ",
            instrument_handles=handles,
        )
    numerator = 1.0 + request.quote_rate * request.time_to_expiry_years
    denominator = 1.0 + request.base_rate * request.time_to_expiry_years
    if numerator <= 0.0 or denominator <= 0.0:
        return _refusal(
            dataset,
            analysis_kind="fx_forward_carry",
            reason_code="fx_forward_undefined",
            reason="simple-rate compounding factors must remain positive",
            instrument_handles=handles,
        )
    try:
        forward = request.spot * numerator / denominator
        carry = (forward / request.spot - 1.0) / request.time_to_expiry_years
    except (ArithmeticError, OverflowError, ZeroDivisionError):
        return _refusal(
            dataset,
            analysis_kind="fx_forward_carry",
            reason_code="fx_forward_undefined",
            reason="the supplied finite inputs do not define stable FX forward measures",
            instrument_handles=handles,
        )
    if not math.isfinite(forward) or not math.isfinite(carry) or forward <= 0.0:
        return _refusal(
            dataset,
            analysis_kind="fx_forward_carry",
            reason_code="fx_forward_undefined",
            reason="the supplied finite inputs do not define stable FX forward measures",
            instrument_handles=handles,
        )
    return _result(
        dataset,
        analysis_kind="fx_forward_carry",
        private_values=FxForwardValues(
            base_currency=request.base_currency,
            quote_currency=request.quote_currency,
            forward=forward,
            annualized_carry=carry,
        ),
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        warnings=(),
        material=request,
    )


__all__ = (
    "DerivativeAnalyticsResult",
    "DerivativeDataset",
    "FuturesContractPoint",
    "FuturesCurveRequest",
    "FuturesCurveValues",
    "FxForwardRequest",
    "FxForwardValues",
    "IvSurfacePoint",
    "IvSurfaceRequest",
    "IvSurfaceValues",
    "LifecyclePosition",
    "LifecycleRadarEvent",
    "LifecycleRadarRequest",
    "LifecycleRadarValues",
    "OptionAnalyticsValues",
    "SaxoGreekSnapshot",
    "StrategyAnalyticsValues",
    "analyze_futures_curve",
    "analyze_fx_forward",
    "analyze_iv_surface",
    "analyze_lifecycle_radar",
    "analyze_option_model",
    "analyze_option_strategy",
)
