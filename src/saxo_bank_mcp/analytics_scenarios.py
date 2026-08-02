from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal, DecimalException, localcontext
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    AnalysisId,
    ContractName,
    DatasetId,
    InstrumentHandle,
    IsoCurrencyCode,
    PortfolioSnapshotId,
    QualityState,
    SafeAccountScope,
    Sha256Fingerprint,
    UtcDateTime,
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
_BASE_SOURCE_CONTRACTS: Final = (
    "balances_v1",
    "positions_v1",
    "exposure_instruments_v1",
)
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)

type ScenarioType = Literal[
    "historical",
    "equity",
    "currency",
    "volatility",
    "rate",
    "margin",
    "combined",
]
type ScenarioInputMode = Literal["numeric", "narrative_proposal"]
type ScenarioBranch = Literal["linear", "option", "fixed_income"]
type ScenarioAnalysisKind = Literal[
    "scenario_historical",
    "scenario_custom",
    "scenario_currency",
    "scenario_volatility",
    "scenario_rate",
    "scenario_margin",
    "scenario_combined",
]
_SCENARIO_ANALYSIS_KIND: Final[dict[ScenarioType, ScenarioAnalysisKind]] = {
    "historical": "scenario_historical",
    "equity": "scenario_custom",
    "currency": "scenario_currency",
    "volatility": "scenario_volatility",
    "rate": "scenario_rate",
    "margin": "scenario_margin",
    "combined": "scenario_combined",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ScenarioComponent(_StrictModel):
    """One persisted reporting-currency component in an exact portfolio snapshot."""

    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    branch_id: ScenarioBranch
    current_value: Decimal = Field(allow_inf_nan=False)
    currency: IsoCurrencyCode
    current_margin_requirement: Decimal = Field(ge=0, allow_inf_nan=False)
    model_analysis_id: AnalysisId | None

    @model_validator(mode="after")
    def validate_branch_binding(self) -> Self:
        if (self.branch_id == "linear") != (self.model_analysis_id is None):
            raise ValueError("only model-repriced components require a model analysis handle")
        return self


class ScenarioShock(_StrictModel):
    """Complete explicit numeric shock row for one persisted component."""

    instrument_handle: InstrumentHandle
    price_shock_ratio: Decimal = Field(ge=-1, allow_inf_nan=False)
    volatility_shock_points: Decimal = Field(allow_inf_nan=False)
    rate_shock_basis_points: Decimal = Field(allow_inf_nan=False)
    cash_flow_shock: Decimal = Field(allow_inf_nan=False)
    repriced_value_at_base_fx: Decimal | None = Field(default=None, allow_inf_nan=False)
    stressed_margin_requirement: Decimal = Field(ge=0, allow_inf_nan=False)


class CurrencyShock(_StrictModel):
    """One explicit reporting-rate shock for a complete persisted currency exposure."""

    currency: IsoCurrencyCode
    shock_ratio: Decimal = Field(gt=-1, allow_inf_nan=False)


class PortfolioScenarioRequest(_StrictModel):
    """A total typed shock map bound to one exact account snapshot."""

    dataset_id: DatasetId
    snapshot_id: PortfolioSnapshotId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    reporting_currency: IsoCurrencyCode
    scenario_type: ScenarioType
    input_mode: ScenarioInputMode
    narrative_fingerprint_sha256: Sha256Fingerprint | None
    numeric_shocks_echoed_by_caller: bool
    caller_accepted_numeric_shocks: bool
    echoed_shock_map_sha256: Sha256Fingerprint | None
    accepted_shock_map_sha256: Sha256Fingerprint | None
    historical_start_at: UtcDateTime | None
    historical_end_at: UtcDateTime | None
    components: tuple[ScenarioComponent, ...] = Field(min_length=1)
    component_shocks: tuple[ScenarioShock, ...] = Field(min_length=1)
    currency_shocks: tuple[CurrencyShock, ...] = Field(min_length=1)
    current_margin_headroom: Decimal = Field(ge=0, allow_inf_nan=False)
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_request(self) -> Self:  # noqa: C901
        if any(component.account_alias != self.account_alias for component in self.components):
            raise ValueError(
                "scenario component account alias must match the request account alias",
            )
        handles = tuple(component.instrument_handle for component in self.components)
        if len(handles) != len(set(handles)):
            raise ValueError("scenario components must use unique instrument handles")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial scenario quality must match explicit missing fields")
        if self.scenario_type == "historical":
            if self.historical_start_at is None or self.historical_end_at is None:
                raise ValueError("historical scenarios require exact start and end timestamps")
            if (
                self.historical_end_at < self.historical_start_at
                or self.historical_end_at > self.as_of
            ):
                raise ValueError("historical scenario timestamps must be ordered before cutoff")
        elif self.historical_start_at is not None or self.historical_end_at is not None:
            raise ValueError("only historical scenarios may carry historical timestamps")
        confirmation_fields = (
            self.numeric_shocks_echoed_by_caller,
            self.caller_accepted_numeric_shocks,
            self.echoed_shock_map_sha256 is not None,
            self.accepted_shock_map_sha256 is not None,
        )
        if self.input_mode == "numeric":
            if self.narrative_fingerprint_sha256 is not None or any(confirmation_fields):
                raise ValueError("numeric scenarios cannot carry narrative confirmation state")
        elif self.narrative_fingerprint_sha256 is None:
            raise ValueError("narrative proposals require a value-free narrative fingerprint")
        return self


class ScenarioContribution(_StrictModel):
    """Owner-only component value change under one simultaneous shock map."""

    instrument_handle: InstrumentHandle
    branch_id: ScenarioBranch
    current_value: Decimal = Field(allow_inf_nan=False)
    stressed_value: Decimal = Field(allow_inf_nan=False)
    effect: Decimal = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_effect(self) -> Self:
        if self.effect != self.stressed_value - self.current_value:
            raise ValueError("scenario contribution must equal stressed minus current value")
        return self


class PrivateScenarioValues(_StrictModel):
    """Owner-only scenario money values and reconciliation identities."""

    reporting_currency: IsoCurrencyCode
    scenario_type: ScenarioType
    contributions: tuple[ScenarioContribution, ...]
    total_effect: Decimal = Field(allow_inf_nan=False)
    current_margin_headroom: Decimal = Field(ge=0, allow_inf_nan=False)
    stressed_margin_headroom: Decimal = Field(allow_inf_nan=False)
    margin_stress_effect: Decimal = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        if sum((item.effect for item in self.contributions), Decimal(0)) != self.total_effect:
            raise ValueError("scenario contributions must sum to total effect")
        if (
            self.stressed_margin_headroom - self.current_margin_headroom
            != self.margin_stress_effect
        ):
            raise ValueError("margin stress must equal stressed minus current headroom")
        return self


class PortfolioScenarioResult(_StrictModel):
    """Typed scenario hypothesis with private values and value-free public evidence."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: ScenarioAnalysisKind
    dataset_id: DatasetId
    snapshot_id: PortfolioSnapshotId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateScenarioValues | None
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence
    is_not_forecast: Literal[True] = True

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("scenario values do not match their delivery visibility")
        return self


def scenario_shock_map_fingerprint(
    component_shocks: Sequence[ScenarioShock],
    currency_shocks: Sequence[CurrencyShock],
) -> str:
    """Return a deterministic fingerprint for exactly the echoed numeric shock rows."""
    payload = {
        "component_shocks": [item.model_dump(mode="json") for item in component_shocks],
        "currency_shocks": [item.model_dump(mode="json") for item in currency_shocks],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()


def run_portfolio_scenario(  # noqa: C901, PLR0911
    request: PortfolioScenarioRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> PortfolioScenarioResult | ResearchRefusal:
    """Apply a complete explicit shock map without treating its hypothesis as a forecast."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    if request.input_mode == "narrative_proposal":
        expected = scenario_shock_map_fingerprint(
            request.component_shocks,
            request.currency_shocks,
        )
        if (
            not request.numeric_shocks_echoed_by_caller
            or not request.caller_accepted_numeric_shocks
            or request.echoed_shock_map_sha256 != expected
            or request.accepted_shock_map_sha256 != expected
        ):
            return _refusal(
                request,
                "scenario_numeric_confirmation_required",
                "narrative-proposed numbers must be echoed and accepted as the exact shock map",
            )
    required_contracts: list[str] = list(_BASE_SOURCE_CONTRACTS)
    if request.scenario_type == "historical":
        required_contracts.append("chart_v3")
    handles = tuple(component.instrument_handle for component in request.components)
    source_assessment = assess_source_bindings(
        request.source_bindings,
        required_contract_ids=required_contracts,
        analysis_kind=_analysis_kind(request.scenario_type),
        dataset_id=request.dataset_id,
        instrument_handles=handles,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if request.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            request,
            "scenario_data_unusable",
            "scenario inputs are missing, invalid, or stale",
        )
    map_error = _validate_total_maps(request)
    if map_error is not None:
        return map_error
    dimension_error = _validate_scenario_dimensions(request)
    if dimension_error is not None:
        return dimension_error
    try:
        values = _calculate_values(request)
    except (ArithmeticError, DecimalException):
        return _refusal(
            request,
            "scenario_measure_undefined",
            "the scenario value is undefined for the supplied finite shock map",
        )
    warnings = set(request.warnings)
    warnings.update(source_assessment)
    if request.quality_state is QualityState.PARTIAL:
        warnings.add("partial_scenario_inputs")
    if any(component.branch_id != "linear" for component in request.components):
        warnings.add("explicit_model_repricing_input")
    return PortfolioScenarioResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        analysis_kind=_analysis_kind(request.scenario_type),
        dataset_id=request.dataset_id,
        snapshot_id=request.snapshot_id,
        account_alias=request.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind=_analysis_kind(request.scenario_type),
            dataset_ids=(request.dataset_id,),
            account_aliases=(request.account_alias,),
            source_bindings=request.source_bindings,
            material=request,
        ),
    )


def _validate_total_maps(request: PortfolioScenarioRequest) -> ResearchRefusal | None:
    component_handles = tuple(component.instrument_handle for component in request.components)
    shock_handles = tuple(shock.instrument_handle for shock in request.component_shocks)
    currencies = tuple(dict.fromkeys(component.currency for component in request.components))
    shock_currencies = tuple(shock.currency for shock in request.currency_shocks)
    if component_handles != shock_handles or currencies != shock_currencies:
        return _refusal(
            request,
            "scenario_shock_map_mismatch",
            "shock-map keys and order must exactly equal the persisted snapshot components",
        )
    return None


def _validate_scenario_dimensions(
    request: PortfolioScenarioRequest,
) -> ResearchRefusal | None:
    zero_currency = all(shock.shock_ratio == 0 for shock in request.currency_shocks)
    unchanged_margin = all(
        shock.stressed_margin_requirement == component.current_margin_requirement
        for component, shock in zip(request.components, request.component_shocks, strict=True)
    )
    no_price_model_cash = all(
        shock.price_shock_ratio == 0
        and shock.volatility_shock_points == 0
        and shock.rate_shock_basis_points == 0
        and shock.cash_flow_shock == 0
        and shock.repriced_value_at_base_fx is None
        for shock in request.component_shocks
    )
    if request.scenario_type in {"historical", "equity"}:
        valid = zero_currency and unchanged_margin and all(
            component.branch_id == "linear"
            and shock.volatility_shock_points == 0
            and shock.rate_shock_basis_points == 0
            and shock.repriced_value_at_base_fx is None
            for component, shock in zip(
                request.components,
                request.component_shocks,
                strict=True,
            )
        )
    elif request.scenario_type == "currency":
        valid = unchanged_margin and no_price_model_cash
    elif request.scenario_type == "margin":
        valid = zero_currency and no_price_model_cash
    elif request.scenario_type == "volatility":
        valid = zero_currency and unchanged_margin and all(
            component.branch_id == "option"
            and shock.price_shock_ratio == 0
            and shock.rate_shock_basis_points == 0
            and shock.cash_flow_shock == 0
            for component, shock in zip(
                request.components,
                request.component_shocks,
                strict=True,
            )
        )
    elif request.scenario_type == "rate":
        valid = zero_currency and unchanged_margin and all(
            component.branch_id in {"option", "fixed_income"}
            and shock.price_shock_ratio == 0
            and shock.volatility_shock_points == 0
            and shock.cash_flow_shock == 0
            for component, shock in zip(
                request.components,
                request.component_shocks,
                strict=True,
            )
        )
    else:
        valid = all(
            component.branch_id != "linear"
            or (
                shock.volatility_shock_points == 0
                and shock.rate_shock_basis_points == 0
                and shock.repriced_value_at_base_fx is None
            )
            for component, shock in zip(
                request.components,
                request.component_shocks,
                strict=True,
            )
        )
    if not valid:
        return _refusal(
            request,
            "scenario_shock_type_mismatch",
            "the explicit shock dimensions do not match the selected scenario type",
        )
    if any(
        component.branch_id != "linear" and shock.repriced_value_at_base_fx is None
        for component, shock in zip(request.components, request.component_shocks, strict=True)
    ):
        return _refusal(
            request,
            "scenario_repricing_required",
            "supported option and fixed-income shocks require an explicit model-repriced value",
            missing_fields=("repriced_value_at_base_fx",),
        )
    return None


def _calculate_values(request: PortfolioScenarioRequest) -> PrivateScenarioValues:
    currency_by_code = {shock.currency: shock for shock in request.currency_shocks}
    contributions: list[ScenarioContribution] = []
    with localcontext() as context:
        context.prec = 50
        for component, shock in zip(
            request.components,
            request.component_shocks,
            strict=True,
        ):
            if component.branch_id == "linear":
                repriced = component.current_value * (Decimal(1) + shock.price_shock_ratio)
            else:
                if shock.repriced_value_at_base_fx is None:
                    raise ArithmeticError
                repriced = shock.repriced_value_at_base_fx
            fx_multiplier = Decimal(1) + currency_by_code[component.currency].shock_ratio
            stressed_value = repriced * fx_multiplier + shock.cash_flow_shock
            contributions.append(
                ScenarioContribution(
                    instrument_handle=component.instrument_handle,
                    branch_id=component.branch_id,
                    current_value=component.current_value,
                    stressed_value=stressed_value,
                    effect=stressed_value - component.current_value,
                ),
            )
        current_requirement = sum(
            (component.current_margin_requirement for component in request.components),
            Decimal(0),
        )
        stressed_requirement = sum(
            (shock.stressed_margin_requirement for shock in request.component_shocks),
            Decimal(0),
        )
        stressed_headroom = (
            request.current_margin_headroom - (stressed_requirement - current_requirement)
        )
        margin_effect = stressed_headroom - request.current_margin_headroom
        total_effect = sum((item.effect for item in contributions), Decimal(0))
    computed = tuple(
        value
        for item in contributions
        for value in (item.current_value, item.stressed_value, item.effect)
    )
    computed = (*computed, stressed_headroom, margin_effect, total_effect)
    if not all(value.is_finite() for value in computed):
        raise ArithmeticError
    return PrivateScenarioValues(
        reporting_currency=request.reporting_currency,
        scenario_type=request.scenario_type,
        contributions=tuple(contributions),
        total_effect=total_effect,
        current_margin_headroom=request.current_margin_headroom,
        stressed_margin_headroom=stressed_headroom,
        margin_stress_effect=margin_effect,
    )


def _analysis_kind(scenario_type: ScenarioType) -> ScenarioAnalysisKind:
    return _SCENARIO_ANALYSIS_KIND[scenario_type]


def _refusal(
    request: PortfolioScenarioRequest,
    reason_code: str,
    reason: str,
    *,
    missing_fields: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind=_analysis_kind(request.scenario_type),
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(request.dataset_id,),
        instrument_handles=tuple(
            component.instrument_handle for component in request.components
        ),
        missing_fields=tuple(sorted(set(missing_fields))),
        source_scope=None,
    )


__all__ = (
    "CurrencyShock",
    "PortfolioScenarioRequest",
    "PortfolioScenarioResult",
    "PrivateScenarioValues",
    "ScenarioComponent",
    "ScenarioContribution",
    "ScenarioShock",
    "run_portfolio_scenario",
    "scenario_shock_map_fingerprint",
)
