from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from typing import Final, Literal, Protocol, Self, cast

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.optimize import (  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]
    Bounds,
    LinearConstraint,
    NonlinearConstraint,
    lsq_linear,  # pyright: ignore[reportUnknownVariableType]
    minimize,  # pyright: ignore[reportUnknownVariableType]
)

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    InstrumentHandle,
    IsoCurrencyCode,
    PortfolioSnapshotId,
    QualityState,
    SafeAccountScope,
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
_SOURCE_CONTRACTS: Final = (
    "chart_v3",
    "positions_v1",
    "exposure_instruments_v1",
    "balances_v1",
    "costs_v1",
)
_MINIMUM_SAMPLE_COUNT: Final = 30
_MAXIMUM_ASSETS: Final = 25
_MAXIMUM_PERTURBATIONS: Final = 8
_MAXIMUM_MINIMUM_TRADE_ASSETS: Final = 4
_EIGENVALUE_RELATIVE_FLOOR: Final = 1e-12
_VARIANCE_FLOOR: Final = 1e-18
_BUDGET_REPAIR_TOLERANCE: Final = 1e-12
_CAPACITY_TOLERANCE: Final = 1e-15
_BUDGET_REPAIR_FINAL_TOLERANCE: Final = 1e-10

type FloatVector = NDArray[np.float64]
type FloatMatrix = NDArray[np.float64]
type OptimizationObjective = Literal["minimum_variance", "risk_parity"]
type ShortPolicy = Literal["long_only", "bounded_short"]
type AnalysisKind = Literal["portfolio_minimum_variance", "portfolio_risk_parity"]
type TradeBranch = Literal[-1, 0, 1]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class OptimizationAsset(_StrictModel):
    """One exact ordered target asset and all explicit per-target constraints."""

    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    asset_class: ContractName
    currency: IsoCurrencyCode
    current_weight: Decimal = Field(allow_inf_nan=False)
    expected_return: Decimal = Field(allow_inf_nan=False)
    lower_bound: Decimal = Field(allow_inf_nan=False)
    upper_bound: Decimal = Field(allow_inf_nan=False)
    transaction_cost_rate: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_requirement_rate: Decimal = Field(ge=0, allow_inf_nan=False)
    minimum_trade_weight: Decimal = Field(ge=0, allow_inf_nan=False)
    excluded: bool

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.lower_bound > self.upper_bound:
            raise ValueError("optimizer position lower bound must not exceed upper bound")
        if self.excluded and not (self.lower_bound <= 0 <= self.upper_bound):
            raise ValueError("an excluded optimizer asset must permit a zero target weight")
        return self


class AssetClassConstraint(_StrictModel):
    """Explicit net-weight bound for one present asset class."""

    asset_class: ContractName
    aggregation: Literal["net_weight"]
    minimum_weight: Decimal = Field(allow_inf_nan=False)
    maximum_weight: Decimal = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.minimum_weight > self.maximum_weight:
            raise ValueError("asset-class minimum must not exceed maximum")
        return self


class CurrencyConstraint(_StrictModel):
    """Explicit net-weight bound for one present portfolio currency."""

    currency: IsoCurrencyCode
    aggregation: Literal["net_weight"]
    minimum_weight: Decimal = Field(allow_inf_nan=False)
    maximum_weight: Decimal = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.minimum_weight > self.maximum_weight:
            raise ValueError("currency minimum must not exceed maximum")
        return self


class CovariancePerturbation(_StrictModel):
    """One complete persisted covariance perturbation used for stability proof."""

    perturbation_id: ContractName
    covariance_matrix: tuple[tuple[Decimal, ...], ...]


class OptimizationDataset(_StrictModel):
    """Complete Saxo-bound current portfolio and one frozen estimation window."""

    dataset_id: DatasetId
    snapshot_id: PortfolioSnapshotId
    account_alias: SafeAccountScope
    estimation_start_at: UtcDateTime
    estimation_end_at: UtcDateTime
    as_of: UtcDateTime
    reporting_currency: IsoCurrencyCode
    assets: tuple[OptimizationAsset, ...] = Field(min_length=2, max_length=_MAXIMUM_ASSETS)
    covariance_matrix: tuple[tuple[Decimal, ...], ...]
    sample_count: int = Field(ge=_MINIMUM_SAMPLE_COUNT)
    return_model: Literal["historical_arithmetic"]
    covariance_model: Literal["sample_covariance"]
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if not (
            self.estimation_start_at < self.estimation_end_at <= self.as_of
        ):
            raise ValueError("optimizer estimation window must end at or before cutoff")
        if any(asset.account_alias != self.account_alias for asset in self.assets):
            raise ValueError("optimizer asset account alias must match the dataset account alias")
        handles = tuple(asset.instrument_handle for asset in self.assets)
        if len(handles) != len(set(handles)):
            raise ValueError("optimizer assets must use unique instrument handles")
        if sum((asset.current_weight for asset in self.assets), Decimal(0)) != Decimal(1):
            raise ValueError("current optimizer weights must sum exactly to one")
        _validate_covariance_shape(self.covariance_matrix, len(self.assets))
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial optimizer quality must match explicit missing fields")
        return self


class SolverSettings(_StrictModel):
    """Bounded deterministic SciPy adapter settings and persisted tolerances."""

    method: Literal["SLSQP"]
    maximum_iterations: int = Field(ge=50, le=5000)
    objective_tolerance: Decimal = Field(gt=0, allow_inf_nan=False)
    feasibility_tolerance: Decimal = Field(gt=0, allow_inf_nan=False)
    kkt_tolerance: Decimal = Field(gt=0, allow_inf_nan=False)


class OptimizationRequest(_StrictModel):
    """Caller-selected objective and complete explicit constrained optimization problem."""

    dataset: OptimizationDataset
    objective: OptimizationObjective
    objective_confirmed_by_caller: bool
    constraints_confirmed_by_caller: bool
    short_policy: ShortPolicy
    asset_class_constraints: tuple[AssetClassConstraint, ...]
    currency_constraints: tuple[CurrencyConstraint, ...]
    maximum_turnover: Decimal = Field(ge=0, allow_inf_nan=False)
    maximum_transaction_cost_ratio: Decimal = Field(ge=0, allow_inf_nan=False)
    maximum_margin_ratio: Decimal = Field(ge=0, allow_inf_nan=False)
    perturbations: tuple[CovariancePerturbation, ...] = Field(
        min_length=1,
        max_length=_MAXIMUM_PERTURBATIONS,
    )
    solver_settings: SolverSettings
    lexicographic_tie_break_rule: Literal["asset_order_within_objective_tolerance"]
    concentration_warning_threshold: Decimal = Field(gt=0, allow_inf_nan=False)
    condition_number_warning_threshold: Decimal = Field(gt=1, allow_inf_nan=False)
    stability_warning_threshold: Decimal = Field(ge=0, allow_inf_nan=False)
    stability_refusal_threshold: Decimal = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_problem(self) -> Self:
        if self.stability_warning_threshold > self.stability_refusal_threshold:
            raise ValueError("stability warning threshold must not exceed refusal threshold")
        asset_classes = {asset.asset_class for asset in self.dataset.assets}
        currencies = {asset.currency for asset in self.dataset.assets}
        class_keys = tuple(item.asset_class for item in self.asset_class_constraints)
        currency_keys = tuple(item.currency for item in self.currency_constraints)
        if len(class_keys) != len(set(class_keys)) or not set(class_keys) <= asset_classes:
            raise ValueError("asset-class constraints must be unique and match present assets")
        if len(currency_keys) != len(set(currency_keys)) or not set(currency_keys) <= currencies:
            raise ValueError("currency constraints must be unique and match present assets")
        for perturbation in self.perturbations:
            _validate_covariance_shape(
                perturbation.covariance_matrix,
                len(self.dataset.assets),
            )
        perturbation_ids = tuple(item.perturbation_id for item in self.perturbations)
        if len(perturbation_ids) != len(set(perturbation_ids)):
            raise ValueError("optimizer perturbation identifiers must be unique")
        return self


class OptimizationTarget(_StrictModel):
    """Owner-only target weight and mathematical delta, never an order instruction."""

    instrument_handle: InstrumentHandle
    current_weight: Decimal = Field(allow_inf_nan=False)
    target_weight: Decimal = Field(allow_inf_nan=False)
    current_to_target_delta: Decimal = Field(allow_inf_nan=False)
    normalized_risk_contribution: Decimal | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_delta(self) -> Self:
        if self.current_to_target_delta != self.target_weight - self.current_weight:
            raise ValueError("optimizer delta must equal target minus current weight")
        return self


class OptimizationDiagnostics(_StrictModel):
    """Private solver, feasibility, KKT, concentration, and perturbation diagnostics."""

    feasibility_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    kkt_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    complementarity_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    budget_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    bound_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    group_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    turnover_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    cost_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    minimum_trade_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    exclusion_residual: Decimal = Field(ge=0, allow_inf_nan=False)
    condition_number: Decimal = Field(ge=1, allow_inf_nan=False)
    maximum_absolute_target_weight: Decimal = Field(ge=0, allow_inf_nan=False)
    maximum_perturbation_weight_change: Decimal = Field(ge=0, allow_inf_nan=False)
    perturbation_count: int = Field(ge=1)
    solver_iterations: int = Field(ge=1)
    maximum_risk_contribution_deviation: Decimal | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )


class PrivateOptimizationValues(_StrictModel):
    """Owner-only mathematical target, deltas, and diagnostics."""

    reporting_currency: IsoCurrencyCode
    objective: OptimizationObjective
    targets: tuple[OptimizationTarget, ...]
    objective_value: Decimal = Field(ge=0, allow_inf_nan=False)
    turnover: Decimal = Field(ge=0, allow_inf_nan=False)
    estimated_transaction_cost_ratio: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_ratio: Decimal = Field(ge=0, allow_inf_nan=False)
    diagnostics: OptimizationDiagnostics

    @model_validator(mode="after")
    def validate_turnover(self) -> Self:
        expected = Decimal("0.5") * sum(
            (abs(item.current_to_target_delta) for item in self.targets),
            Decimal(0),
        )
        if self.turnover != expected:
            raise ValueError("optimizer turnover must equal half the absolute delta sum")
        return self


class PortfolioOptimizationResult(_StrictModel):
    """Private target-weight proposal with value-free public evidence and no authority."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: AnalysisKind
    dataset_id: DatasetId
    snapshot_id: PortfolioSnapshotId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateOptimizationValues | None
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence
    proposal_only: Literal[True] = True
    is_not_advice: Literal[True] = True
    is_not_forecast: Literal[True] = True
    model_distribution_only: Literal[False] = False
    objective_selected_by_engine: Literal[False] = False
    risk_tolerance_selected_by_engine: Literal[False] = False
    order_creation_authority: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("optimizer values do not match their delivery visibility")
        return self


@dataclass(frozen=True)
class _ConstraintMetrics:
    budget: float
    bounds: float
    groups: float
    turnover: float
    cost: float
    margin: float
    minimum_trade: float
    exclusion: float

    @property
    def maximum(self) -> float:
        return max(
            self.budget,
            self.bounds,
            self.groups,
            self.turnover,
            self.cost,
            self.margin,
            self.minimum_trade,
            self.exclusion,
        )


@dataclass(frozen=True)
class _AdapterSolution:
    weights: FloatVector
    objective_value: float
    effective_lower: FloatVector
    effective_upper: FloatVector
    iterations: int
    feasibility: _ConstraintMetrics
    kkt_residual: float
    complementarity_residual: float


class _OptimizerAdapter(Protocol):
    def solve(self, request: OptimizationRequest, covariance: FloatMatrix) -> _AdapterSolution:
        """Solve one exact persisted covariance problem or raise a bounded failure."""
        raise NotImplementedError


class _ScipyMinimizeResult(Protocol):
    success: bool
    x: FloatVector
    nit: int


class _ScipyLeastSquaresResult(Protocol):
    x: FloatVector


class _OptimizationFailureError(ValueError):
    def __init__(self, reason_code: str, reason: str) -> None:
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason


class ScipyOptimizerAdapter:
    """Typed deterministic adapter around SciPy's bounded SLSQP solver."""

    def solve(self, request: OptimizationRequest, covariance: FloatMatrix) -> _AdapterSolution:
        assets = request.dataset.assets
        minimum_trade_indexes = tuple(
            index
            for index, asset in enumerate(assets)
            if asset.minimum_trade_weight > 0 and not asset.excluded
        )
        if len(minimum_trade_indexes) > _MAXIMUM_MINIMUM_TRADE_ASSETS:
            raise _OptimizationFailureError(
                "optimizer_minimum_trade_branch_limit",
                "the exact minimum-trade branch space exceeds the bounded solver limit",
            )
        branches: Sequence[tuple[TradeBranch, ...]] = tuple(
            itertools.product((-1, 0, 1), repeat=len(minimum_trade_indexes)),
        )
        candidates: list[_AdapterSolution] = []
        for branch in branches:
            bounds = _branch_bounds(request, minimum_trade_indexes, branch)
            if bounds is None:
                continue
            lower, upper = bounds
            candidate = self._solve_continuous(request, covariance, lower, upper)
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            raise _OptimizationFailureError(
                "optimizer_constraints_infeasible",
                "the exact typed optimizer constraints have no supported feasible solution",
            )
        best_value = min(candidate.objective_value for candidate in candidates)
        objective_tolerance = float(request.solver_settings.objective_tolerance)
        eligible = tuple(
            candidate
            for candidate in candidates
            if candidate.objective_value <= best_value + objective_tolerance
        )
        return min(eligible, key=lambda candidate: tuple(candidate.weights.tolist()))

    def _solve_continuous(
        self,
        request: OptimizationRequest,
        covariance: FloatMatrix,
        lower: FloatVector,
        upper: FloatVector,
    ) -> _AdapterSolution | None:
        if float(np.sum(lower)) > 1.0 or float(np.sum(upper)) < 1.0:
            return None
        objective, gradient = _objective_functions(request, covariance)
        constraints = _scipy_constraints(request)
        best: _AdapterSolution | None = None
        for start in _candidate_starts(request, lower, upper):
            result = cast(
                "_ScipyMinimizeResult",
                minimize(
                    objective,
                    start,
                    method=request.solver_settings.method,
                    jac=gradient,
                    bounds=Bounds(
                        lower,  # pyright: ignore[reportArgumentType]
                        upper,  # pyright: ignore[reportArgumentType]
                    ),
                    constraints=constraints,
                    options={
                        "maxiter": request.solver_settings.maximum_iterations,
                        "ftol": min(
                            float(request.solver_settings.objective_tolerance),
                            _BUDGET_REPAIR_TOLERANCE,
                        ),
                        "disp": False,
                    },
                ),
            )
            if not bool(result.success):
                continue
            weights = _canonicalize_weights(
                np.asarray(result.x, dtype=np.float64),
                request,
                lower,
                upper,
            )
            feasibility = _constraint_metrics(request, weights, lower, upper)
            if feasibility.maximum > float(request.solver_settings.feasibility_tolerance):
                continue
            kkt, complementarity = _kkt_diagnostics(
                request,
                weights,
                lower,
                upper,
                gradient,
            )
            if kkt > float(request.solver_settings.kkt_tolerance):
                continue
            candidate = _AdapterSolution(
                weights=weights,
                objective_value=float(objective(weights)),
                effective_lower=lower,
                effective_upper=upper,
                iterations=max(1, int(getattr(result, "nit", 1))),
                feasibility=feasibility,
                kkt_residual=kkt,
                complementarity_residual=complementarity,
            )
            if best is None or (
                candidate.objective_value < best.objective_value
                or (
                    abs(candidate.objective_value - best.objective_value)
                    <= float(request.solver_settings.objective_tolerance)
                    and tuple(candidate.weights.tolist()) < tuple(best.weights.tolist())
                )
            ):
                best = candidate
        return best


_SCIPY_ADAPTER: Final[_OptimizerAdapter] = ScipyOptimizerAdapter()


def optimize_portfolio(  # noqa: C901, PLR0911
    request: OptimizationRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> PortfolioOptimizationResult | ResearchRefusal:
    """Solve the caller-selected typed portfolio problem without creating trade authority."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    if not request.objective_confirmed_by_caller or not request.constraints_confirmed_by_caller:
        return _refusal(
            request,
            "optimizer_caller_selection_required",
            "the caller must select the objective and confirm every numeric constraint",
        )
    source_assessment = assess_source_bindings(
        request.dataset.source_bindings,
        required_contract_ids=_SOURCE_CONTRACTS,
        analysis_kind=_analysis_kind(request.objective),
        dataset_id=request.dataset.dataset_id,
        instrument_handles=tuple(asset.instrument_handle for asset in request.dataset.assets),
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if request.dataset.quality_state is not QualityState.COMPLETE or source_assessment:
        return _refusal(
            request,
            "optimizer_calibration_incomplete",
            "optimization requires complete source coverage and entitlement",
        )
    if request.short_policy == "long_only" and any(
        asset.lower_bound < 0 for asset in request.dataset.assets
    ):
        return _refusal(
            request,
            "optimizer_long_only_bounds_invalid",
            "long-only optimization cannot carry a negative target bound",
        )
    if request.objective == "risk_parity" and request.short_policy == "bounded_short":
        return _refusal(
            request,
            "optimizer_risk_parity_shorting_unsupported",
            "the frozen equal-risk definition does not define signed short contributions",
        )
    try:
        covariance, condition_number = _validated_covariance(request.dataset.covariance_matrix)
        solution = _SCIPY_ADAPTER.solve(request, covariance)
        perturbation_solutions = tuple(
            _solve_perturbation(request, perturbation) for perturbation in request.perturbations
        )
        stability = max(
            float(np.max(np.abs(item.weights - solution.weights)))
            for item in perturbation_solutions
        )
        if stability > float(request.stability_refusal_threshold):
            return _refusal(
                request,
                "optimizer_solution_unstable",
                "the target changes beyond the explicit stability refusal threshold",
            )
        values = _private_values(request, solution, condition_number, stability)
    except _OptimizationFailureError as exc:
        return _refusal(request, exc.reason_code, exc.reason)
    except (ArithmeticError, DecimalException, FloatingPointError, ValueError):
        return _refusal(
            request,
            "optimizer_measure_undefined",
            "the constrained optimizer is undefined for the supplied finite inputs",
        )
    warnings = set(request.dataset.warnings)
    if values.diagnostics.maximum_absolute_target_weight > request.concentration_warning_threshold:
        warnings.add("optimizer_concentrated_weights")
    if values.diagnostics.condition_number > request.condition_number_warning_threshold:
        warnings.add("optimizer_condition_number_high")
    if (
        values.diagnostics.maximum_perturbation_weight_change
        > request.stability_warning_threshold
    ):
        warnings.add("optimizer_stability_warning")
    return PortfolioOptimizationResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        analysis_kind=_analysis_kind(request.objective),
        dataset_id=request.dataset.dataset_id,
        snapshot_id=request.dataset.snapshot_id,
        account_alias=request.dataset.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind=_analysis_kind(request.objective),
            dataset_ids=(request.dataset.dataset_id,),
            account_aliases=(request.dataset.account_alias,),
            source_bindings=request.dataset.source_bindings,
            material=request,
        ),
    )


def _validate_covariance_shape(
    covariance: Sequence[Sequence[Decimal]],
    size: int,
) -> None:
    if len(covariance) != size or any(len(row) != size for row in covariance):
        raise ValueError("optimizer covariance must be square and match the asset order")
    for row in range(size):
        for column in range(size):
            value = covariance[row][column]
            if not value.is_finite():
                raise ValueError("optimizer covariance must be finite")
            if value != covariance[column][row]:
                raise ValueError("optimizer covariance must be exactly symmetric")


def _validated_covariance(
    covariance: Sequence[Sequence[Decimal]],
) -> tuple[FloatMatrix, float]:
    matrix = np.asarray(covariance, dtype=np.float64)
    if not np.all(np.isfinite(matrix)):
        raise _OptimizationFailureError(
            "optimizer_covariance_invalid",
            "the covariance matrix must be finite",
        )
    eigenvalues = np.linalg.eigvalsh(matrix)
    maximum = float(np.max(eigenvalues))
    minimum = float(np.min(eigenvalues))
    if maximum <= 0 or minimum < -max(1.0, maximum) * _EIGENVALUE_RELATIVE_FLOOR:
        raise _OptimizationFailureError(
            "optimizer_covariance_invalid",
            "the covariance matrix must be positive semidefinite",
        )
    if minimum <= maximum * _EIGENVALUE_RELATIVE_FLOOR:
        raise _OptimizationFailureError(
            "optimizer_covariance_singular",
            "singular covariance is unsupported by the frozen optimizer policy",
        )
    condition_number = maximum / minimum
    if not np.isfinite(condition_number):
        raise _OptimizationFailureError(
            "optimizer_covariance_singular",
            "singular covariance is unsupported by the frozen optimizer policy",
        )
    return matrix, condition_number


def _branch_bounds(
    request: OptimizationRequest,
    minimum_trade_indexes: Sequence[int],
    branch: Sequence[TradeBranch],
) -> tuple[FloatVector, FloatVector] | None:
    lower = np.asarray(
        [float(asset.lower_bound) for asset in request.dataset.assets],
        dtype=np.float64,
    )
    upper = np.asarray(
        [float(asset.upper_bound) for asset in request.dataset.assets],
        dtype=np.float64,
    )
    for index, asset in enumerate(request.dataset.assets):
        if asset.excluded:
            lower[index] = 0.0
            upper[index] = 0.0
    for index, state in zip(minimum_trade_indexes, branch, strict=True):
        asset = request.dataset.assets[index]
        current = float(asset.current_weight)
        minimum = float(asset.minimum_trade_weight)
        if state == 0:
            lower[index] = max(lower[index], current)
            upper[index] = min(upper[index], current)
        elif state < 0:
            upper[index] = min(upper[index], current - minimum)
        else:
            lower[index] = max(lower[index], current + minimum)
    if np.any(lower > upper):
        return None
    return lower, upper


def _candidate_starts(
    request: OptimizationRequest,
    lower: FloatVector,
    upper: FloatVector,
) -> tuple[FloatVector, ...]:
    current = np.asarray(
        [float(asset.current_weight) for asset in request.dataset.assets],
        dtype=np.float64,
    )
    equal = np.full(len(current), 1.0 / len(current), dtype=np.float64)
    midpoint = (lower + upper) / 2.0
    starts: list[FloatVector] = []
    for candidate in (current, equal, midpoint, current[::-1].copy()):
        repaired = _repair_budget(np.clip(candidate, lower, upper), lower, upper)
        if repaired is not None and not any(np.array_equal(repaired, item) for item in starts):
            starts.append(repaired)
    return tuple(starts)


def _repair_budget(
    values: FloatVector,
    lower: FloatVector,
    upper: FloatVector,
) -> FloatVector | None:
    repaired = values.copy()
    for _ in range(len(repaired) * 2 + 2):
        difference = 1.0 - float(np.sum(repaired))
        if abs(difference) <= _BUDGET_REPAIR_TOLERANCE:
            return repaired
        capacity = upper - repaired if difference > 0 else repaired - lower
        eligible = capacity > _CAPACITY_TOLERANCE
        if not bool(np.any(eligible)):
            return None
        allocation = min(abs(difference), float(np.sum(capacity[eligible])))
        share = capacity / float(np.sum(capacity[eligible]))
        repaired += np.where(eligible, np.sign(difference) * allocation * share, 0.0)
        repaired = np.clip(repaired, lower, upper)
    return (
        repaired
        if abs(float(np.sum(repaired)) - 1.0) <= _BUDGET_REPAIR_FINAL_TOLERANCE
        else None
    )


def _objective_functions(
    request: OptimizationRequest,
    covariance: FloatMatrix,
) -> tuple[Callable[[FloatVector], float], Callable[[FloatVector], FloatVector]]:
    if request.objective == "minimum_variance":

        def minimum_variance(weights: FloatVector) -> float:
            return float(weights @ covariance @ weights)

        def minimum_variance_gradient(weights: FloatVector) -> FloatVector:
            return np.asarray(2.0 * covariance @ weights, dtype=np.float64)

        return minimum_variance, minimum_variance_gradient
    eligible_indexes = tuple(
        index
        for index, asset in enumerate(request.dataset.assets)
        if not asset.excluded
    )
    target = 1.0 / len(eligible_indexes)

    def risk_parity(weights: FloatVector) -> float:
        marginal = covariance @ weights
        component = weights * marginal
        variance = float(weights @ marginal)
        if variance <= _VARIANCE_FLOOR:
            return float("inf")
        contributions = component / variance
        deviations = np.asarray(
            [contributions[index] - target for index in eligible_indexes],
            dtype=np.float64,
        )
        return float(deviations @ deviations)

    def risk_parity_gradient(weights: FloatVector) -> FloatVector:
        marginal = covariance @ weights
        component = weights * marginal
        variance = float(weights @ marginal)
        if variance <= _VARIANCE_FLOOR:
            return np.full(len(weights), np.inf, dtype=np.float64)
        contributions = component / variance
        gradient = np.zeros(len(weights), dtype=np.float64)
        for column in range(len(weights)):
            component_derivative = np.zeros(len(weights), dtype=np.float64)
            component_derivative[column] = marginal[column]
            component_derivative += weights * covariance[:, column]
            variance_derivative = 2.0 * marginal[column]
            contribution_derivative = (
                component_derivative * variance
                - component * variance_derivative
            ) / (variance * variance)
            gradient[column] = 2.0 * sum(
                float(contributions[index] - target)
                * float(contribution_derivative[index])
                for index in eligible_indexes
            )
        return gradient

    return risk_parity, risk_parity_gradient


def _scipy_constraints(
    request: OptimizationRequest,
) -> tuple[LinearConstraint | NonlinearConstraint, ...]:
    size = len(request.dataset.assets)
    rows: list[FloatVector] = [np.ones(size, dtype=np.float64)]
    lower_values: list[float] = [1.0]
    upper_values: list[float] = [1.0]
    for constraint in request.asset_class_constraints:
        rows.append(
            np.asarray(
                [
                    1.0 if asset.asset_class == constraint.asset_class else 0.0
                    for asset in request.dataset.assets
                ],
                dtype=np.float64,
            ),
        )
        lower_values.append(float(constraint.minimum_weight))
        upper_values.append(float(constraint.maximum_weight))
    for constraint in request.currency_constraints:
        rows.append(
            np.asarray(
                [
                    1.0 if asset.currency == constraint.currency else 0.0
                    for asset in request.dataset.assets
                ],
                dtype=np.float64,
            ),
        )
        lower_values.append(float(constraint.minimum_weight))
        upper_values.append(float(constraint.maximum_weight))
    linear = LinearConstraint(
        np.asarray(rows, dtype=np.float64),
        np.asarray(  # pyright: ignore[reportArgumentType]
            lower_values,
            dtype=np.float64,
        ),
        np.asarray(  # pyright: ignore[reportArgumentType]
            upper_values,
            dtype=np.float64,
        ),
    )
    current = np.asarray(
        [float(asset.current_weight) for asset in request.dataset.assets],
        dtype=np.float64,
    )
    cost_rates = np.asarray(
        [float(asset.transaction_cost_rate) for asset in request.dataset.assets],
        dtype=np.float64,
    )
    margin_rates = np.asarray(
        [float(asset.margin_requirement_rate) for asset in request.dataset.assets],
        dtype=np.float64,
    )

    def turnover(weights: FloatVector) -> float:
        return 0.5 * float(np.sum(np.abs(weights - current)))

    def cost(weights: FloatVector) -> float:
        return float(np.sum(np.abs(weights - current) * cost_rates))

    def margin(weights: FloatVector) -> float:
        return float(np.sum(np.abs(weights) * margin_rates))

    return (
        linear,
        NonlinearConstraint(turnover, -np.inf, float(request.maximum_turnover)),
        NonlinearConstraint(cost, -np.inf, float(request.maximum_transaction_cost_ratio)),
        NonlinearConstraint(margin, -np.inf, float(request.maximum_margin_ratio)),
    )


def _canonicalize_weights(
    weights: FloatVector,
    request: OptimizationRequest,
    lower: FloatVector,
    upper: FloatVector,
) -> FloatVector:
    result = weights.copy()
    tolerance = float(request.solver_settings.feasibility_tolerance)
    current = np.asarray(
        [float(asset.current_weight) for asset in request.dataset.assets],
        dtype=np.float64,
    )
    for index in range(len(result)):
        for candidate in (lower[index], upper[index], current[index], 0.0):
            if abs(result[index] - candidate) <= tolerance:
                result[index] = candidate
                break
    return result


def _constraint_metrics(
    request: OptimizationRequest,
    weights: FloatVector,
    lower: FloatVector,
    upper: FloatVector,
) -> _ConstraintMetrics:
    assets = request.dataset.assets
    current = np.asarray([float(asset.current_weight) for asset in assets], dtype=np.float64)
    delta = weights - current
    budget = abs(float(np.sum(weights)) - 1.0)
    bounds = max(
        0.0,
        float(np.max(lower - weights)),
        float(np.max(weights - upper)),
    )
    group_residuals = [0.0]
    for constraint in request.asset_class_constraints:
        value = sum(
            weights[index]
            for index, asset in enumerate(assets)
            if asset.asset_class == constraint.asset_class
        )
        group_residuals.extend(
            (
                float(constraint.minimum_weight) - value,
                value - float(constraint.maximum_weight),
            ),
        )
    for constraint in request.currency_constraints:
        value = sum(
            weights[index]
            for index, asset in enumerate(assets)
            if asset.currency == constraint.currency
        )
        group_residuals.extend(
            (
                float(constraint.minimum_weight) - value,
                value - float(constraint.maximum_weight),
            ),
        )
    turnover_value = 0.5 * float(np.sum(np.abs(delta)))
    cost_value = float(
        np.sum(
            np.abs(delta)
            * np.asarray([float(asset.transaction_cost_rate) for asset in assets]),
        ),
    )
    margin_value = float(
        np.sum(
            np.abs(weights)
            * np.asarray([float(asset.margin_requirement_rate) for asset in assets]),
        ),
    )
    tolerance = float(request.solver_settings.feasibility_tolerance)
    minimum_trade = 0.0
    for value, asset in zip(delta, assets, strict=True):
        absolute = abs(float(value))
        minimum = float(asset.minimum_trade_weight)
        if tolerance < absolute < minimum:
            minimum_trade = max(minimum_trade, minimum - absolute)
    exclusion = max(
        (abs(float(weights[index])) for index, asset in enumerate(assets) if asset.excluded),
        default=0.0,
    )
    return _ConstraintMetrics(
        budget=budget,
        bounds=bounds,
        groups=max(0.0, *group_residuals),
        turnover=max(0.0, turnover_value - float(request.maximum_turnover)),
        cost=max(0.0, cost_value - float(request.maximum_transaction_cost_ratio)),
        margin=max(0.0, margin_value - float(request.maximum_margin_ratio)),
        minimum_trade=minimum_trade,
        exclusion=exclusion,
    )


def _kkt_diagnostics(  # noqa: C901, PLR0912, PLR0915
    request: OptimizationRequest,
    weights: FloatVector,
    lower: FloatVector,
    upper: FloatVector,
    gradient_function: Callable[[FloatVector], FloatVector],
) -> tuple[float, float]:
    size = len(weights)
    gradient = gradient_function(weights)
    if not np.all(np.isfinite(gradient)):
        return float("inf"), float("inf")
    equality_gradients: list[FloatVector] = [np.ones(size, dtype=np.float64)]
    inequality_gradients: list[FloatVector] = []
    inequality_slacks: list[float] = []
    active_tolerance = max(
        float(request.solver_settings.feasibility_tolerance) * 10.0,
        1e-7,
    )
    for index in range(size):
        basis = np.zeros(size, dtype=np.float64)
        basis[index] = 1.0
        if abs(upper[index] - lower[index]) <= active_tolerance:
            equality_gradients.append(basis)
            continue
        lower_slack = weights[index] - lower[index]
        upper_slack = upper[index] - weights[index]
        if lower_slack <= active_tolerance:
            inequality_gradients.append(basis)
            inequality_slacks.append(max(0.0, lower_slack))
        if upper_slack <= active_tolerance:
            inequality_gradients.append(-basis)
            inequality_slacks.append(max(0.0, upper_slack))
    for indexes, minimum, maximum in _group_rows(request):
        row = np.zeros(size, dtype=np.float64)
        row[list(indexes)] = 1.0
        value = float(row @ weights)
        if abs(maximum - minimum) <= active_tolerance:
            equality_gradients.append(row)
            continue
        if value - minimum <= active_tolerance:
            inequality_gradients.append(row)
            inequality_slacks.append(max(0.0, value - minimum))
        if maximum - value <= active_tolerance:
            inequality_gradients.append(-row)
            inequality_slacks.append(max(0.0, maximum - value))
    assets = request.dataset.assets
    current = np.asarray([float(asset.current_weight) for asset in assets], dtype=np.float64)
    delta = weights - current
    sign_delta = np.sign(delta)
    turnover_slack = float(request.maximum_turnover) - 0.5 * float(
        np.sum(np.abs(delta)),
    )
    if turnover_slack <= active_tolerance:
        inequality_gradients.append(-0.5 * sign_delta)
        inequality_slacks.append(max(0.0, turnover_slack))
    cost_rates = np.asarray(
        [float(asset.transaction_cost_rate) for asset in assets],
        dtype=np.float64,
    )
    cost_slack = float(request.maximum_transaction_cost_ratio) - float(
        np.sum(np.abs(delta) * cost_rates),
    )
    if cost_slack <= active_tolerance:
        inequality_gradients.append(-sign_delta * cost_rates)
        inequality_slacks.append(max(0.0, cost_slack))
    margin_rates = np.asarray(
        [float(asset.margin_requirement_rate) for asset in assets],
        dtype=np.float64,
    )
    margin_slack = float(request.maximum_margin_ratio) - float(
        np.sum(np.abs(weights) * margin_rates),
    )
    if margin_slack <= active_tolerance:
        inequality_gradients.append(-np.sign(weights) * margin_rates)
        inequality_slacks.append(max(0.0, margin_slack))
    equality = np.asarray(equality_gradients, dtype=np.float64)
    inequality = np.asarray(inequality_gradients, dtype=np.float64)
    _, singular_values, right_vectors = np.linalg.svd(equality, full_matrices=True)
    rank_tolerance = (
        max(equality.shape)
        * np.finfo(np.float64).eps
        * float(singular_values[0])
    )
    rank = int(np.count_nonzero(singular_values > rank_tolerance))
    null_space = np.asarray(right_vectors[rank:].T, dtype=np.float64)
    multipliers = np.zeros(len(inequality_gradients), dtype=np.float64)
    if null_space.shape[1] == 0:
        stationarity = 0.0
    elif inequality_gradients:
        projected_inequality = null_space.T @ inequality.T
        projected_gradient = null_space.T @ gradient
        multiplier_result = cast(
            "_ScipyLeastSquaresResult",
            lsq_linear(
                projected_inequality,
                projected_gradient,
                bounds=(np.zeros(len(inequality_gradients)), np.inf),
                tol=_BUDGET_REPAIR_TOLERANCE,
                lsmr_tol=_BUDGET_REPAIR_TOLERANCE,
                max_iter=1000,
            ),
        )
        multipliers = np.asarray(multiplier_result.x, dtype=np.float64)
        stationarity = float(
            np.max(
                np.abs(
                    projected_gradient - projected_inequality @ multipliers,
                ),
            ),
        )
    else:
        stationarity = float(np.max(np.abs(null_space.T @ gradient)))
    complementarity = 0.0
    if inequality_gradients:
        complementarity = max(
            (abs(float(multiplier * slack)) for multiplier, slack in zip(
                multipliers,
                inequality_slacks,
                strict=True,
            )),
            default=0.0,
        )
    return max(stationarity, complementarity), complementarity


def _group_rows(
    request: OptimizationRequest,
) -> tuple[tuple[tuple[int, ...], float, float], ...]:
    rows = [
        (
            tuple(
                index
                for index, asset in enumerate(request.dataset.assets)
                if asset.asset_class == constraint.asset_class
            ),
            float(constraint.minimum_weight),
            float(constraint.maximum_weight),
        )
        for constraint in request.asset_class_constraints
    ]
    rows.extend(
        (
            tuple(
                index
                for index, asset in enumerate(request.dataset.assets)
                if asset.currency == constraint.currency
            ),
            float(constraint.minimum_weight),
            float(constraint.maximum_weight),
        )
        for constraint in request.currency_constraints
    )
    return tuple(rows)


def _solve_perturbation(
    request: OptimizationRequest,
    perturbation: CovariancePerturbation,
) -> _AdapterSolution:
    try:
        covariance, _ = _validated_covariance(perturbation.covariance_matrix)
        return _SCIPY_ADAPTER.solve(request, covariance)
    except _OptimizationFailureError as exc:
        raise _OptimizationFailureError(
            "optimizer_stability_undefined",
            "an explicit covariance perturbation has no stable supported solution",
        ) from exc


def _private_values(
    request: OptimizationRequest,
    solution: _AdapterSolution,
    condition_number: float,
    stability: float,
) -> PrivateOptimizationValues:
    target_weights = tuple(_decimal(value) for value in solution.weights)
    current_weights = tuple(asset.current_weight for asset in request.dataset.assets)
    deltas = tuple(
        target - current for target, current in zip(target_weights, current_weights, strict=True)
    )
    risk_contributions = _risk_contributions(request, solution.weights)
    targets = tuple(
        OptimizationTarget(
            instrument_handle=asset.instrument_handle,
            current_weight=current,
            target_weight=target,
            current_to_target_delta=delta,
            normalized_risk_contribution=(
                None
                if risk_contributions is None
                else _decimal(risk_contributions[index])
            ),
        )
        for index, (asset, current, target, delta) in enumerate(
            zip(
                request.dataset.assets,
                current_weights,
                target_weights,
                deltas,
                strict=True,
            ),
        )
    )
    turnover = Decimal("0.5") * sum((abs(delta) for delta in deltas), Decimal(0))
    estimated_cost = sum(
        (
            abs(delta) * asset.transaction_cost_rate
            for delta, asset in zip(deltas, request.dataset.assets, strict=True)
        ),
        Decimal(0),
    )
    margin = sum(
        (
            abs(target) * asset.margin_requirement_rate
            for target, asset in zip(target_weights, request.dataset.assets, strict=True)
        ),
        Decimal(0),
    )
    maximum_contribution_deviation: Decimal | None = None
    if risk_contributions is not None:
        eligible_count = sum(not asset.excluded for asset in request.dataset.assets)
        target_contribution = Decimal(1) / Decimal(eligible_count)
        maximum_contribution_deviation = max(
            abs(_decimal(value) - target_contribution)
            for value, asset in zip(risk_contributions, request.dataset.assets, strict=True)
            if not asset.excluded
        )
    feasibility = solution.feasibility
    return PrivateOptimizationValues(
        reporting_currency=request.dataset.reporting_currency,
        objective=request.objective,
        targets=targets,
        objective_value=(
            _decimal_variance(request.dataset.covariance_matrix, target_weights)
            if request.objective == "minimum_variance"
            else _decimal(solution.objective_value)
        ),
        turnover=turnover,
        estimated_transaction_cost_ratio=estimated_cost,
        margin_ratio=margin,
        diagnostics=OptimizationDiagnostics(
            feasibility_residual=_decimal(feasibility.maximum),
            kkt_residual=_decimal(solution.kkt_residual),
            complementarity_residual=_decimal(solution.complementarity_residual),
            budget_residual=_decimal(feasibility.budget),
            bound_residual=_decimal(feasibility.bounds),
            group_residual=_decimal(feasibility.groups),
            turnover_residual=_decimal(feasibility.turnover),
            cost_residual=_decimal(feasibility.cost),
            margin_residual=_decimal(feasibility.margin),
            minimum_trade_residual=_decimal(feasibility.minimum_trade),
            exclusion_residual=_decimal(feasibility.exclusion),
            condition_number=_decimal(condition_number),
            maximum_absolute_target_weight=max(abs(value) for value in target_weights),
            maximum_perturbation_weight_change=_decimal(stability),
            perturbation_count=len(request.perturbations),
            solver_iterations=solution.iterations,
            maximum_risk_contribution_deviation=maximum_contribution_deviation,
        ),
    )


def _risk_contributions(
    request: OptimizationRequest,
    weights: FloatVector,
) -> FloatVector | None:
    if request.objective != "risk_parity":
        return None
    covariance = np.asarray(request.dataset.covariance_matrix, dtype=np.float64)
    marginal = covariance @ weights
    variance = float(weights @ marginal)
    if variance <= _VARIANCE_FLOOR:
        raise ArithmeticError
    values = weights * marginal / variance
    if not np.all(np.isfinite(values)):
        raise ArithmeticError
    return np.asarray(values, dtype=np.float64)


def _decimal_variance(
    covariance: Sequence[Sequence[Decimal]],
    weights: Sequence[Decimal],
) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        value = sum(
            (
                weights[row]
                * covariance[row][column]
                * weights[column]
                for row in range(len(weights))
                for column in range(len(weights))
            ),
            Decimal(0),
        )
    if not value.is_finite() or value < 0:
        raise ArithmeticError
    return value


def _decimal(value: float | np.float64) -> Decimal:
    numeric = float(value)
    if not np.isfinite(numeric):
        raise ArithmeticError
    return Decimal(format(numeric, ".15g"))


def _analysis_kind(objective: OptimizationObjective) -> AnalysisKind:
    if objective == "minimum_variance":
        return "portfolio_minimum_variance"
    return "portfolio_risk_parity"


def _refusal(
    request: OptimizationRequest,
    reason_code: str,
    reason: str,
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind=_analysis_kind(request.objective),
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(request.dataset.dataset_id,),
        instrument_handles=tuple(
            asset.instrument_handle for asset in request.dataset.assets
        ),
        source_scope=None,
    )


__all__ = (
    "AssetClassConstraint",
    "CovariancePerturbation",
    "CurrencyConstraint",
    "OptimizationAsset",
    "OptimizationDataset",
    "OptimizationDiagnostics",
    "OptimizationRequest",
    "OptimizationTarget",
    "PortfolioOptimizationResult",
    "PrivateOptimizationValues",
    "ScipyOptimizerAdapter",
    "SolverSettings",
    "optimize_portfolio",
)
