from __future__ import annotations

import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from saxo_bank_mcp.analytics_models import (
    AnalysisId,
    ContractName,
    IsoCurrencyCode,
    SourceRevision,
    UtcDateTime,
    VisibilityMode,
)
from saxo_bank_mcp.secret_scan import scan_secret_text

type ChartTemplateId = Literal[
    "price_volume_indicator",
    "relative_performance",
    "equity_curve_drawdown",
    "monthly_return_heatmap",
    "portfolio_tearsheet",
    "portfolio_briefing",
    "allocation_exposure",
    "contribution_waterfall",
    "risk_contribution",
    "correlation_heatmap_cluster_map",
    "scenario_waterfall",
    "efficient_frontier",
    "options_payoff_greeks",
    "iv_smile_surface",
    "cost_waterfall",
    "trade_distribution_behavior_dashboard",
    "trading_mirror",
    "pretrade_impact_card",
    "execution_report_card",
    "session_cockpit",
    "stored_table_line",
    "stored_table_bar",
    "stored_table_scatter",
    "stored_table_heatmap",
    "stored_table_composite",
]
type ChartKind = Literal[
    "line",
    "bar",
    "heatmap",
    "waterfall",
    "scatter",
    "surface",
    "composite",
    "dashboard",
    "card",
]
type SeriesStyle = Literal["line", "bar", "area", "scatter"]
type SeriesAxis = Literal["primary", "secondary"]
type ArtifactEnvironment = Literal["SIM", "LIVE"]

PRIVACY_FOOTER: Final = "Owner-only analytics; public evidence remains redacted"
_SOURCE_SCOPE: Final = "saxo_openapi"
_MAX_LABELS: Final = 500
_MAX_SERIES: Final = 25
_SCATTER_SERIES_COUNT: Final = 2
_FORBIDDEN_DISPLAY_TEXT: Final = re.compile(
    r"(?:https?://|ftp://|file://|<\s*script\b|\bfetch\s*\(|"
    r"XMLHttpRequest|WebSocket|EventSource|(?:/Users/|/Volumes/|/private/)|"
    r"(?:^|[\s=])~/|(?:^|[\s=])[A-Za-z]:\\|\\\\[^\\]+\\|"
    r"\b(?:access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
    r"client[_ -]?key|account[_ -]?key|account[_ -]?id|order[_ -]?id|"
    r"position[_ -]?id|preview[_ -]?token|"
    r"(?:account|client|order|position|user|trade|transaction|application|app)"
    r"[_ -]?(?:id|key|number|name|ref))\b)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_TEXT: Final = re.compile(
    r"(?:^|[\s=(])/(?!/)[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+",
)
_CONTROL_TEXT: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _safe_display_text(value: str) -> str:
    material = value.strip()
    findings, scan_errors = scan_secret_text("artifact-field.txt", material)
    if (
        not material
        or _CONTROL_TEXT.search(material)
        or _FORBIDDEN_DISPLAY_TEXT.search(material)
        or _ABSOLUTE_PATH_TEXT.search(material)
        or findings
        or scan_errors
    ):
        raise ValueError("artifact display text contains forbidden material")
    return material


type DisplayText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=240),
    AfterValidator(_safe_display_text),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class TemplateBinding(_StrictModel):
    template_id: ChartTemplateId
    analysis_kind: ContractName
    chart_kind: ChartKind


_CORE_TEMPLATE_BINDINGS: Final = (
    TemplateBinding(
        template_id="price_volume_indicator",
        analysis_kind="instrument_price_volume",
        chart_kind="composite",
    ),
    TemplateBinding(
        template_id="relative_performance",
        analysis_kind="market_comparison",
        chart_kind="line",
    ),
    TemplateBinding(
        template_id="equity_curve_drawdown",
        analysis_kind="portfolio_performance",
        chart_kind="composite",
    ),
    TemplateBinding(
        template_id="monthly_return_heatmap",
        analysis_kind="portfolio_performance",
        chart_kind="heatmap",
    ),
    TemplateBinding(
        template_id="portfolio_tearsheet",
        analysis_kind="portfolio_performance",
        chart_kind="dashboard",
    ),
    TemplateBinding(
        template_id="portfolio_briefing",
        analysis_kind="portfolio_performance",
        chart_kind="dashboard",
    ),
    TemplateBinding(
        template_id="allocation_exposure",
        analysis_kind="portfolio_exposure",
        chart_kind="bar",
    ),
    TemplateBinding(
        template_id="contribution_waterfall",
        analysis_kind="portfolio_attribution",
        chart_kind="waterfall",
    ),
    TemplateBinding(
        template_id="risk_contribution",
        analysis_kind="portfolio_risk",
        chart_kind="bar",
    ),
    TemplateBinding(
        template_id="correlation_heatmap_cluster_map",
        analysis_kind="portfolio_risk",
        chart_kind="heatmap",
    ),
    TemplateBinding(
        template_id="scenario_waterfall",
        analysis_kind="portfolio_scenario",
        chart_kind="waterfall",
    ),
    TemplateBinding(
        template_id="efficient_frontier",
        analysis_kind="portfolio_minimum_variance",
        chart_kind="scatter",
    ),
    TemplateBinding(
        template_id="options_payoff_greeks",
        analysis_kind="option_payoff",
        chart_kind="line",
    ),
    TemplateBinding(
        template_id="iv_smile_surface",
        analysis_kind="iv_surface",
        chart_kind="surface",
    ),
    TemplateBinding(
        template_id="cost_waterfall",
        analysis_kind="cost_xray",
        chart_kind="waterfall",
    ),
    TemplateBinding(
        template_id="trade_distribution_behavior_dashboard",
        analysis_kind="trading_mirror",
        chart_kind="scatter",
    ),
    TemplateBinding(
        template_id="trading_mirror",
        analysis_kind="trading_mirror",
        chart_kind="dashboard",
    ),
    TemplateBinding(
        template_id="pretrade_impact_card",
        analysis_kind="pretrade_impact",
        chart_kind="card",
    ),
    TemplateBinding(
        template_id="execution_report_card",
        analysis_kind="execution_quality",
        chart_kind="card",
    ),
    TemplateBinding(
        template_id="session_cockpit",
        analysis_kind="session_cockpit",
        chart_kind="dashboard",
    ),
)
STORED_TABLE_TEMPLATES: Final = (
    "stored_table_line",
    "stored_table_bar",
    "stored_table_scatter",
    "stored_table_heatmap",
    "stored_table_composite",
)
_TABLE_TEMPLATE_KINDS: Final[dict[ChartTemplateId, ChartKind]] = {
    "stored_table_line": "line",
    "stored_table_bar": "bar",
    "stored_table_scatter": "scatter",
    "stored_table_heatmap": "heatmap",
    "stored_table_composite": "composite",
}
_BINDINGS_BY_TEMPLATE: Final = MappingProxyType(
    {binding.template_id: binding for binding in _CORE_TEMPLATE_BINDINGS}
    | {
        name: TemplateBinding(template_id=name, analysis_kind="stored_table", chart_kind=kind)
        for name, kind in _TABLE_TEMPLATE_KINDS.items()
    },
)


class ArtifactStamps(_StrictModel):
    environment: ArtifactEnvironment
    data_cutoff: UtcDateTime
    quote_delay: ContractName
    price_type: ContractName
    currency: IsoCurrencyCode
    adjustment_status: ContractName
    warnings: tuple[ContractName, ...] = Field(max_length=16)
    analysis_id: AnalysisId
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    source_revision: SourceRevision
    visibility: VisibilityMode

    @model_validator(mode="after")
    def validate_warning_order(self) -> Self:
        if len(set(self.warnings)) != len(self.warnings) or self.warnings != tuple(
            sorted(self.warnings),
        ):
            raise ValueError("artifact warnings must be unique and sorted")
        return self


class ChartSeries(_StrictModel):
    name: DisplayText
    values: tuple[float | None, ...] = Field(
        min_length=1,
        max_length=_MAX_LABELS,
    )
    unit: ContractName
    style: SeriesStyle
    axis: SeriesAxis

    @model_validator(mode="after")
    def validate_finite_values(self) -> Self:
        if any(value is not None and not _is_finite(value) for value in self.values):
            raise ValueError("chart series values must be finite or missing")
        return self


class ChartSemantics(_StrictModel):
    schema_version: Literal["1"] = "1"
    template_id: ChartTemplateId
    analysis_kind: ContractName
    title: DisplayText
    subtitle: DisplayText
    x_axis_title: DisplayText
    y_axis_title: DisplayText
    secondary_y_axis_title: DisplayText | None = None
    labels: tuple[DisplayText, ...] = Field(min_length=1, max_length=_MAX_LABELS)
    series: tuple[ChartSeries, ...] = Field(min_length=1, max_length=_MAX_SERIES)
    stamps: ArtifactStamps

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        binding = _BINDINGS_BY_TEMPLATE[self.template_id]
        if (
            self.template_id not in _TABLE_TEMPLATE_KINDS
            and self.analysis_kind != binding.analysis_kind
        ):
            raise ValueError("chart template is not bound to this analysis kind")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("chart labels must be unique")
        names = tuple(series.name for series in self.series)
        if len(set(names)) != len(names):
            raise ValueError("chart series names must be unique")
        if any(len(series.values) != len(self.labels) for series in self.series):
            raise ValueError("every chart series must align exactly to the chart labels")
        kind = binding.chart_kind
        _validate_template_values(kind, self.labels, self.series)
        self._validate_axis_contract(kind)
        return self

    def _validate_axis_contract(self, kind: ChartKind) -> None:  # noqa: C901, PLR0912
        if kind in {"dashboard", "card"}:
            if self.secondary_y_axis_title is not None:
                raise ValueError("dashboard and card charts cannot use a secondary axis")
            if any(series.axis != "primary" for series in self.series):
                raise ValueError("dashboard and card series must use the primary axis")
            units = {series.unit for series in self.series}
            if len(units) != 1:
                raise ValueError("dashboard and card series must share one unit")
            _require_axis_unit_title(self.y_axis_title, next(iter(units)), "primary")
            return
        primary = tuple(series for series in self.series if series.axis == "primary")
        secondary = tuple(series for series in self.series if series.axis == "secondary")
        if kind == "scatter":
            if self.secondary_y_axis_title is not None:
                raise ValueError("scatter charts use explicit x and y titles only")
            _require_axis_unit_title(self.x_axis_title, self.series[0].unit, "scatter x")
            _require_axis_unit_title(self.y_axis_title, self.series[1].unit, "scatter y")
            return
        if kind != "composite" and secondary:
            raise ValueError("only composite charts may use a secondary axis")
        if not primary:
            raise ValueError("charts require a primary-axis series")
        primary_units = {series.unit for series in primary}
        if len(primary_units) != 1:
            raise ValueError("primary-axis series must share one unit")
        _require_axis_unit_title(self.y_axis_title, next(iter(primary_units)), "primary")
        if secondary:
            secondary_units = {series.unit for series in secondary}
            if len(secondary_units) != 1:
                raise ValueError("secondary-axis series must share one unit")
            if self.secondary_y_axis_title is None:
                raise ValueError("secondary-axis series require a separate axis title")
            _require_axis_unit_title(
                self.secondary_y_axis_title,
                next(iter(secondary_units)),
                "secondary",
            )
        elif self.secondary_y_axis_title is not None:
            raise ValueError("secondary axis title requires secondary-axis series")


class StructuredChartSemantics(_StrictModel):
    schema_version: Literal["1"] = "1"
    template_id: ChartTemplateId
    analysis_kind: ContractName
    chart_kind: ChartKind
    title: DisplayText
    subtitle: DisplayText
    x_axis_title: DisplayText
    y_axis_title: DisplayText
    secondary_y_axis_title: DisplayText | None
    labels: tuple[DisplayText, ...]
    series: tuple[ChartSeries, ...]
    series_sha256s: tuple[str, ...]
    semantics_sha256: str
    visible_stamps: tuple[str, ...]


def core_template_bindings() -> tuple[TemplateBinding, ...]:
    """Return the exact frozen Task 19 template-owner inventory."""
    return _CORE_TEMPLATE_BINDINGS


def chart_kind_for(template_id: ChartTemplateId) -> ChartKind:
    """Return the bounded visual grammar for one frozen template."""
    return _BINDINGS_BY_TEMPLATE[template_id].chart_kind


def chart_semantics_sha256(semantics: ChartSemantics) -> str:
    """Fingerprint exact serialized values without recalculating a metric."""
    return _model_sha256(semantics)


def structured_chart_semantics(semantics: ChartSemantics) -> StructuredChartSemantics:
    """Expose exact chart values and fingerprints for agent explanation."""
    return StructuredChartSemantics(
        template_id=semantics.template_id,
        analysis_kind=semantics.analysis_kind,
        chart_kind=chart_kind_for(semantics.template_id),
        title=semantics.title,
        subtitle=semantics.subtitle,
        x_axis_title=semantics.x_axis_title,
        y_axis_title=semantics.y_axis_title,
        secondary_y_axis_title=semantics.secondary_y_axis_title,
        labels=semantics.labels,
        series=semantics.series,
        series_sha256s=tuple(_model_sha256(series) for series in semantics.series),
        semantics_sha256=chart_semantics_sha256(semantics),
        visible_stamps=visible_stamp_lines(semantics.stamps),
    )


def visible_stamp_lines(stamps: ArtifactStamps) -> tuple[str, ...]:
    """Label caller-composed semantics without asserting source provenance."""
    del stamps
    return (
        "Provenance: unverified caller-composed chart semantics",
        "Artifact delivery: refused until stored analysis binding",
        PRIVACY_FOOTER,
    )


def _bound_visible_stamp_lines(  # pyright: ignore[reportUnusedFunction]
    stamps: ArtifactStamps,
) -> tuple[str, ...]:
    """Build stamps only for an internal adapter that already proved binding."""
    warning_text = ", ".join(stamps.warnings) if stamps.warnings else "none"
    return (
        f"Environment: {stamps.environment}",
        f"Cutoff: {stamps.data_cutoff.isoformat().replace('+00:00', 'Z')}",
        f"Delay: {stamps.quote_delay}",
        f"Price type: {stamps.price_type}",
        f"Reporting currency: {stamps.currency}",
        f"Adjustment: {stamps.adjustment_status}",
        f"Warnings: {warning_text}",
        f"Analysis: {stamps.analysis_id}",
        f"Provenance: Saxo OpenAPI / {stamps.source_revision}",
        f"Visibility: {stamps.visibility.value}",
        PRIVACY_FOOTER,
    )


def validate_artifact_text(value: str) -> str:
    """Validate one artifact text cell against network, secret, ID, and path material."""
    return _safe_display_text(value)


def _model_sha256(model: BaseModel) -> str:
    encoded = json.dumps(
        model.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_finite(value: float) -> bool:
    return math.isfinite(value)


def _require_axis_unit_title(title: str, unit: str, axis: str) -> None:
    normalized_unit = unit.casefold().replace("_", " ")
    if normalized_unit not in title.casefold().replace("_", " "):
        raise ValueError(f"{axis} axis title must identify its series unit")


def _validate_template_values(  # noqa: C901
    kind: ChartKind,
    labels: tuple[str, ...],
    series: tuple[ChartSeries, ...],
) -> None:
    if kind == "waterfall":
        if len(series) != 1:
            raise ValueError("waterfall charts require exactly one series")
        if any(value is None for value in series[0].values):
            raise ValueError("waterfall charts cannot replace missing values")
    if kind == "scatter":
        if len(series) != _SCATTER_SERIES_COUNT:
            raise ValueError("scatter charts require exactly two series")
        if any(value is None for item in series for value in item.values):
            raise ValueError("scatter charts require complete paired values")
    if kind in {"dashboard", "card"}:
        if len(labels) != 1:
            raise ValueError("dashboard and card charts require exactly one observation")
        if any(item.values[0] is None for item in series):
            raise ValueError("dashboard and card values cannot be missing")
        if kind == "card" and len(series) != 1:
            raise ValueError("card charts require exactly one series")
