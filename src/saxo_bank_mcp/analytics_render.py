# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
# ruff: noqa: E501, SLF001
from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import math
import re
import textwrap
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files
from io import BytesIO
from typing import Final, Literal, Self, cast
from uuid import uuid4

import matplotlib as mpl
import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.text import Text
from matplotlib.ticker import MaxNLocator
from matplotlib.transforms import Bbox
from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_chart_semantics import (
    PRIVACY_FOOTER,
    ArtifactStamps,
    ChartSemantics,
    ChartSeries,
    ChartTemplateId,
    chart_kind_for,
    chart_semantics_sha256,
)
from saxo_bank_mcp.analytics_chart_semantics import (
    _bound_visible_stamp_lines as visible_stamp_lines,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_models import (
    AnalysisId,
    AnalysisResult,
    ArtifactId,
    ArtifactSummary,
    HandleKind,
    Sha256Fingerprint,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_proof_profiles import ProofRegistry
from saxo_bank_mcp.analytics_provenance import AnalysisReplayRefused, replay_analysis
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreError

mpl.use("Agg", force=True)

type ArtifactExtension = Literal["bin", "csv", "html", "json", "parquet", "pdf", "png", "txt"]
type RenderFormat = Literal["png", "plotly_html"]
type DeliveryReason = Literal[
    "artifact_return_limit",
    "inline_private_not_enabled",
    "local_resource_link_requested",
]
_DPI: Final = 100
_MIN_WIDTH: Final = 320
_MAX_WIDTH: Final = 2560
_MIN_HEIGHT: Final = 240
_MAX_HEIGHT: Final = 1600
_SCATTER_SERIES_COUNT: Final = 2
_MAX_DISPLAY_TICKS: Final = 8
_MIN_READABLE_FONT_PX: Final = 12
_INK_THRESHOLD: Final = 248
_CANVAS_TOLERANCE: Final = 0.5
_MAX_STAMP_WARNINGS: Final = 16
_PLOTLY_RESOURCE: Final = "package_data/plotly.min.js"
_NETWORK_RUNTIME_PATTERNS: Final = (
    re.compile(rb"https?://", re.IGNORECASE),
    re.compile(rb"\bfetch\s*\(", re.IGNORECASE),
    re.compile(rb"XMLHttpRequest|WebSocket|EventSource", re.IGNORECASE),
)
_EXTERNAL_HTML_PATTERNS: Final = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"<script[^>]+\bsrc\s*=", re.IGNORECASE),
    re.compile(r"\bfetch\s*\(", re.IGNORECASE),
    re.compile(r"XMLHttpRequest|WebSocket|EventSource", re.IGNORECASE),
)
_PLOTLY_NAMESPACE_URLS: Final = (
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/XML/1998/namespace",
    "http://www.w3.org/2000/xmlns/",
)
_COLORS: Final = (
    "#2f6bff",
    "#ef8354",
    "#38a169",
    "#805ad5",
    "#d69e2e",
    "#319795",
    "#c53030",
    "#4a5568",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class VisualQa(_StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    non_background_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    footer_ink_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    edge_ink_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    clipped_text_count: int = Field(ge=0)
    overlapping_label_count: int = Field(ge=0)


class HtmlVisualQa(_StrictModel):
    viewport_width: int = Field(ge=_MIN_WIDTH, le=_MAX_WIDTH)
    readable: bool
    horizontal_overflow: bool
    minimum_font_size_px: int = Field(ge=0)
    external_url_count: int = Field(ge=0)
    layout_issue_count: int = Field(default=0, ge=0)


class ArtifactPayload(_StrictModel):
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    extension: ArtifactExtension
    content: bytes
    byte_count: int = Field(ge=0)
    sha256: Sha256Fingerprint
    semantics_sha256: Sha256Fingerprint
    stamps: ArtifactStamps
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    visual_qa: VisualQa | None = None

    @model_validator(mode="after")
    def validate_payload_integrity(self) -> Self:
        if self.byte_count != len(self.content):
            raise ValueError("artifact byte count does not match its content")
        if self.sha256 != hashlib.sha256(self.content).hexdigest():
            raise ValueError("artifact fingerprint does not match its content")
        if (self.width is None) != (self.height is None):
            raise ValueError("artifact dimensions must be supplied together")
        if self.visual_qa is not None and (
            self.width != self.visual_qa.width or self.height != self.visual_qa.height
        ):
            raise ValueError("artifact dimensions and visual QA dimensions must match")
        return self


class ArtifactRefusal(_StrictModel):
    status: Literal["refused"] = "refused"
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    reason: str = Field(min_length=1, max_length=500)
    next_action: str = Field(min_length=1, max_length=500)


class InlineArtifact(_StrictModel):
    status: Literal["inline"] = "inline"
    artifact_id: ArtifactId
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    content: bytes
    byte_count: int = Field(ge=0)
    sha256: Sha256Fingerprint
    semantics_sha256: Sha256Fingerprint
    visible_stamps: tuple[str, ...]

    @model_validator(mode="after")
    def validate_inline_integrity(self) -> Self:
        if self.byte_count != len(self.content):
            raise ValueError("inline artifact byte count does not match")
        if self.sha256 != hashlib.sha256(self.content).hexdigest():
            raise ValueError("inline artifact fingerprint does not match")
        return self


class ArtifactResourceLink(_StrictModel):
    status: Literal["resource_link"] = "resource_link"
    artifact_id: ArtifactId
    resource_uri: str = Field(
        pattern=r"^saxo-analytics://artifacts/ar_[0-9a-f]{32}$",
    )
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    byte_count: int = Field(ge=0)
    sha256: Sha256Fingerprint
    semantics_sha256: Sha256Fingerprint
    owner_only: Literal[True] = True
    reason_code: DeliveryReason
    visible_stamps: tuple[str, ...]


type ArtifactDelivery = InlineArtifact | ArtifactResourceLink | ArtifactRefusal


class RenderRequest(_StrictModel):
    semantics: ChartSemantics
    output_format: RenderFormat
    width: int = Field(ge=_MIN_WIDTH, le=_MAX_WIDTH)
    height: int = Field(ge=_MIN_HEIGHT, le=_MAX_HEIGHT)


class StoredRenderRequest(_StrictModel):
    """Bound chart request containing no caller-provided values or provenance."""

    binding_id: str = Field(pattern=r"^ab_[0-9a-f]{32}$")
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    output_format: RenderFormat
    width: int = Field(ge=_MIN_WIDTH, le=_MAX_WIDTH)
    height: int = Field(ge=_MIN_HEIGHT, le=_MAX_HEIGHT)


class ArtifactBindingReceipt(_StrictModel):
    """Value-free receipt for one proof-replayed, in-process analysis binding."""

    binding_id: str = Field(pattern=r"^ab_[0-9a-f]{32}$")
    analysis_id: AnalysisId


class ArtifactBindingRegistry:
    """Hold proof-replayed results behind server-issued opaque handles."""

    def __init__(
        self,
        *,
        config: AnalyticsConfig,
        proof_registry: ProofRegistry,
        environment: Literal["SIM", "LIVE"],
    ) -> None:
        """Bind runtime-owned configuration, proof registry, and environment."""
        self._config = config
        self._proof_registry = proof_registry
        self._environment: Literal["SIM", "LIVE"] = environment
        self._results: dict[str, AnalysisResult] = {}
        self._template_ids: dict[str, frozenset[str]] = {}

    def issue(self, analysis_id: str) -> ArtifactBindingReceipt:
        """Replay exact persisted proof material before issuing a random binding."""
        result = replay_analysis(
            analysis_id,
            config=self._config,
            registry=self._proof_registry,
            at=_utc_now(),
        )
        profile = self._proof_registry.profile(result.analysis_kind)
        if profile is None:
            raise ValueError("stored analysis has no artifact proof profile")
        binding_id = f"ab_{uuid4().hex}"
        self._results[binding_id] = result
        self._template_ids[binding_id] = frozenset(profile.artifact_template_ids)
        return ArtifactBindingReceipt(binding_id=binding_id, analysis_id=result.analysis_id)

    def _result_for(self, binding_id: str) -> AnalysisResult:
        result = self._results.get(binding_id)
        if result is None:
            raise ValueError("artifact binding is unknown or stale")
        try:
            current = replay_analysis(
                result.analysis_id,
                config=self._config,
                registry=self._proof_registry,
                at=_utc_now(),
            )
        except AnalysisReplayRefused as error:
            raise ValueError("artifact binding no longer replays as verified") from error
        if current != result:
            raise ValueError("artifact binding no longer identifies the issued result")
        return current

    def _allows_template(self, binding_id: str, template_id: str) -> bool:
        self._result_for(binding_id)
        return template_id in self._template_ids[binding_id]

    def _base_visibility(self, binding_id: str) -> VisibilityMode:
        visibility = self._result_for(binding_id).visibility
        if visibility in {VisibilityMode.PRIVATE_USER_RESULT, VisibilityMode.INLINE_PRIVATE}:
            return VisibilityMode.INLINE_PRIVATE
        if visibility is VisibilityMode.LOCAL_RESOURCE_LINK:
            return VisibilityMode.LOCAL_RESOURCE_LINK
        raise ValueError("stored analysis visibility cannot authorize owner value delivery")

    def _stamps_for(
        self,
        binding_id: str,
        *,
        visibility: VisibilityMode,
    ) -> ArtifactStamps:
        result = self._result_for(binding_id)
        base_visibility = self._base_visibility(binding_id)
        if visibility not in {base_visibility, VisibilityMode.LOCAL_RESOURCE_LINK}:
            raise ValueError("artifact visibility does not match its stored analysis")
        warning_codes = {
            warning.code for warning in (*result.warnings, *result.data_quality.warnings)
        }
        warning_codes.add("artifact_market_metadata_unavailable")
        if len(warning_codes) > _MAX_STAMP_WARNINGS:
            raise ValueError("stored analysis warnings exceed the artifact contract")
        return ArtifactStamps(
            environment=self._environment,
            data_cutoff=result.as_of,
            quote_delay="not_available",
            price_type="not_available",
            currency=result.request.parameters.reporting_currency,
            adjustment_status="not_available",
            warnings=tuple(sorted(warning_codes)),
            analysis_id=result.analysis_id,
            source_scope=result.provenance.source_scope,
            source_revision=result.provenance.source_revision,
            visibility=visibility,
        )


def render_png(
    semantics: ChartSemantics,
    *,
    width: int = 1200,
    height: int = 675,
) -> ArtifactRefusal:
    """Refuse caller-composed chart evidence at the public rendering boundary."""
    del semantics, width, height
    return _unbound_analysis_refusal()


def _render_png_payload(  # pyright: ignore[reportUnusedFunction]
    semantics: ChartSemantics,
    *,
    width: int = 1200,
    height: int = 675,
) -> ArtifactPayload | ArtifactRefusal:
    """Render exact chart semantics through the forced headless Matplotlib backend."""
    if not _has_renderable_values(semantics):
        return _blank_refusal()
    if not (_MIN_WIDTH <= width <= _MAX_WIDTH and _MIN_HEIGHT <= height <= _MAX_HEIGHT):
        return _dimension_refusal()
    figure, canvas, bounded_texts, label_texts = _build_figure(
        semantics,
        width=width,
        height=height,
    )
    try:
        canvas.draw()
        pixels = np.asarray(canvas.buffer_rgba()).copy()
        visual_qa = _pixel_and_text_qa(
            pixels,
            canvas=canvas,
            bounded_texts=bounded_texts,
            label_texts=label_texts,
        )
        if visual_qa.clipped_text_count or visual_qa.overlapping_label_count:
            return _visual_qa_refusal()
        buffer = BytesIO()
        canvas.print_png(
            buffer,
            metadata={
                "Software": "saxo-analytics-matplotlib-1",
                "Title": semantics.title,
                "Description": semantics.template_id,
                "semantics_sha256": chart_semantics_sha256(semantics),
                "artifact_stamps": "\n".join(visible_stamp_lines(semantics.stamps)),
            },
        )
        return build_artifact_payload(
            media_type="image/png",
            extension="png",
            content=buffer.getvalue(),
            semantics_sha256=chart_semantics_sha256(semantics),
            stamps=semantics.stamps,
            width=width,
            height=height,
            visual_qa=visual_qa,
        )
    finally:
        figure.clear()


def render_plotly_html(
    semantics: ChartSemantics,
    *,
    viewport_width: int = 1280,
    height: int = 720,
) -> ArtifactRefusal:
    """Refuse caller-composed HTML evidence at the public rendering boundary."""
    del semantics, viewport_width, height
    return _unbound_analysis_refusal()


def _render_plotly_html_payload(  # pyright: ignore[reportUnusedFunction]
    semantics: ChartSemantics,
    *,
    viewport_width: int = 1280,
    height: int = 720,
) -> ArtifactPayload | ArtifactRefusal:
    """Return a self-contained Plotly artifact with all network paths disabled."""
    if not _has_renderable_values(semantics):
        return _blank_refusal()
    if not (_MIN_WIDTH <= viewport_width <= _MAX_WIDTH and _MIN_HEIGHT <= height <= _MAX_HEIGHT):
        return _dimension_refusal()
    semantics_sha256 = chart_semantics_sha256(semantics)
    runtime = _compressed_sanitized_plotly_runtime()
    semantics_json = _script_safe_json(semantics.model_dump(mode="json"))
    figure_json = _script_safe_json(_plotly_figure(semantics))
    stamp_lines = visible_stamp_lines(semantics.stamps)
    compact_stamps = "".join(f"<span>{html.escape(line)}</span>" for line in stamp_lines[:-1])
    fallback = _semantic_fallback_table(semantics)
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; connect-src 'none'">
<title>{html.escape(semantics.title)}</title>
<style>
:root{{--artifact-width:{viewport_width}px;--ink:#162033;--muted:#4a5568;--line:#d9e2ef}}
*{{box-sizing:border-box}}html,body{{margin:0;padding:0;background:#fff;color:var(--ink);font-family:Arial,sans-serif;font-size:14px;overflow-x:hidden}}
main{{width:min(100%,var(--artifact-width));max-width:100%;margin:0 auto;padding:16px}}
h1{{font-size:22px;line-height:1.25;margin:0 0 4px;overflow-wrap:anywhere}}p{{font-size:14px;line-height:1.4;margin:0 0 8px;color:var(--muted)}}
#chart{{width:100%;max-width:100%;height:{height}px;min-height:320px;border:1px solid var(--line)}}
.stamps{{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;padding-top:8px;border-top:1px solid var(--line);font-size:12px;line-height:1.35}}
.stamps span{{overflow-wrap:anywhere}}.privacy{{font-size:12px;font-weight:700;margin-top:8px;color:#27364d}}
.semantic-fallback{{margin-top:12px;max-width:100%;overflow:hidden}}table{{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}}
th,td{{padding:5px;border:1px solid var(--line);text-align:right;overflow-wrap:anywhere}}th:first-child,td:first-child{{text-align:left}}
@media(max-width:600px){{main{{padding:8px}}h1{{font-size:18px}}#chart{{height:{max(320, min(height, 480))}px}}.stamps{{font-size:12px}}
.semantic-fallback table,.semantic-fallback tbody,.semantic-fallback tr,.semantic-fallback td{{display:block;width:100%}}.semantic-fallback thead{{position:absolute;clip:rect(0 0 0 0);width:1px;height:1px;overflow:hidden}}
.semantic-fallback tr{{margin-bottom:8px;border:1px solid var(--line)}}.semantic-fallback td{{display:grid;grid-template-columns:minmax(88px,40%) 1fr;border:0;border-bottom:1px solid var(--line);font-size:12px;text-align:right;overflow-wrap:anywhere}}
.semantic-fallback td::before{{content:attr(data-field-label);font-weight:700;text-align:left}}}}
</style></head>
<body data-viewport-width="{viewport_width}" data-semantics-sha256="{semantics_sha256}"><main>
<h1>{html.escape(semantics.title)}</h1><p>{html.escape(semantics.subtitle)}</p>
<div id="chart" role="img" aria-label="{html.escape(semantics.title)}"></div>
<div class="stamps">{compact_stamps}</div><div class="privacy">{PRIVACY_FOOTER}</div>
{fallback}
<script id="plotly-runtime-gzip" type="application/octet-stream">{runtime}</script>
<script id="chart-semantics" type="application/json">{semantics_json}</script>
<script id="plotly-figure" type="application/json">{figure_json}</script>
<script>
"use strict";
(async()=>{{
 const packed=document.getElementById("plotly-runtime-gzip").textContent.trim();
 const bytes=Uint8Array.from(atob(packed),character=>character.charCodeAt(0));
 const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
 const runtime=await new Response(stream).text();
 (0,eval)(runtime);
 const figure=JSON.parse(document.getElementById("plotly-figure").textContent);
 await window.Plotly.newPlot("chart",figure.data,figure.layout,{{responsive:true,displaylogo:false,scrollZoom:false}});
}})().catch(()=>{{document.getElementById("chart").setAttribute("data-render-state","refused")}});
</script></main></body></html>"""
    for pattern in _EXTERNAL_HTML_PATTERNS:
        if pattern.search(document) is not None:
            return ArtifactRefusal(
                reason_code="artifact_html_sanitization_failed",
                reason="interactive HTML retained a forbidden external capability",
                next_action="request a deterministic PNG artifact",
            )
    content = document.encode()
    qa = inspect_html_artifact(content, viewport_width=viewport_width)
    if (
        not qa.readable
        or qa.horizontal_overflow
        or qa.minimum_font_size_px < _MIN_READABLE_FONT_PX
        or qa.external_url_count
        or qa.layout_issue_count
    ):
        return _visual_qa_refusal()
    return build_artifact_payload(
        media_type="text/html",
        extension="html",
        content=content,
        semantics_sha256=semantics_sha256,
        stamps=semantics.stamps,
        width=viewport_width,
        height=height,
    )


def inspect_html_artifact(content: bytes, *, viewport_width: int) -> HtmlVisualQa:
    """Perform deterministic desktop/mobile layout checks without a browser process."""
    try:
        document = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return HtmlVisualQa(
            viewport_width=viewport_width,
            readable=False,
            horizontal_overflow=True,
            minimum_font_size_px=0,
            external_url_count=0,
            layout_issue_count=1,
        )
    external_count = sum(len(pattern.findall(document)) for pattern in _EXTERNAL_HTML_PATTERNS)
    font_sizes = tuple(int(value) for value in re.findall(r"font-size:(\d+)px", document))
    declared_width = re.search(r'data-viewport-width="(\d+)"', document)
    required_markers = (
        "data-semantics-sha256=",
        'id="chart"',
        "semantic-fallback",
        PRIVACY_FOOTER,
    )
    layout_markers = (
        'data-mobile-layout="stacked"',
        "data-field-label=",
        ".semantic-fallback td::before",
        "overflow-wrap:anywhere",
    )
    layout_issues = sum(marker not in document for marker in layout_markers)
    horizontal_overflow = (
        "overflow-x:hidden" not in document or "max-width:100%" not in document or layout_issues > 0
    )
    readable = (
        all(marker in document for marker in required_markers)
        and declared_width is not None
        and int(declared_width.group(1)) == viewport_width
        and min(font_sizes, default=0) >= _MIN_READABLE_FONT_PX
        and external_count == 0
        and not horizontal_overflow
    )
    return HtmlVisualQa(
        viewport_width=viewport_width,
        readable=readable,
        horizontal_overflow=horizontal_overflow,
        minimum_font_size_px=min(font_sizes, default=0),
        external_url_count=external_count,
        layout_issue_count=layout_issues,
    )


def build_artifact_payload(  # noqa: PLR0913
    *,
    media_type: str,
    extension: ArtifactExtension,
    content: bytes,
    semantics_sha256: str,
    stamps: ArtifactStamps,
    width: int | None = None,
    height: int | None = None,
    visual_qa: VisualQa | None = None,
) -> ArtifactPayload:
    """Build an integrity-checked internal artifact payload for bounded delivery."""
    return ArtifactPayload(
        media_type=media_type,
        extension=extension,
        content=content,
        byte_count=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        semantics_sha256=semantics_sha256,
        stamps=stamps,
        width=width,
        height=height,
        visual_qa=visual_qa,
    )


def deliver_artifact(
    payload: ArtifactPayload,
    *,
    config: AnalyticsConfig,
    store: AnalyticsStore,
) -> ArtifactRefusal:
    """Refuse caller-created payloads at the public persistence boundary."""
    del payload, config, store
    return _unbound_analysis_refusal()


def render_analysis(
    request: RenderRequest | StoredRenderRequest,
    *,
    config: AnalyticsConfig,
    store: AnalyticsStore | None = None,
    bindings: ArtifactBindingRegistry | None = None,
) -> ArtifactDelivery:
    """Render only values derived from a server-issued proof-replayed binding."""
    if isinstance(request, RenderRequest) or store is None or bindings is None:
        return _unbound_analysis_refusal()

    def produce(stamps: ArtifactStamps) -> ArtifactPayload | ArtifactRefusal:
        semantics = _bound_chart_semantics(
            request.binding_id,
            request.template_id,
            stamps=stamps,
            bindings=bindings,
        )
        if isinstance(semantics, ArtifactRefusal):
            return semantics
        if request.output_format == "png":
            return _render_png_payload(semantics, width=request.width, height=request.height)
        return _render_plotly_html_payload(
            semantics,
            viewport_width=request.width,
            height=request.height,
        )

    return _produce_and_deliver_bound(
        binding_id=request.binding_id,
        producer=produce,
        config=config,
        store=store,
        bindings=bindings,
    )


def _bound_chart_semantics(  # noqa: PLR0911
    binding_id: str,
    template_id: str,
    *,
    stamps: ArtifactStamps,
    bindings: ArtifactBindingRegistry,
) -> ChartSemantics | ArtifactRefusal:
    """Derive one truthful exact-value chart from the stored result metrics."""
    try:
        result = bindings._result_for(binding_id)
        allowed = bindings._allows_template(binding_id, template_id)
    except ValueError:
        return _binding_refusal()
    if not allowed:
        return ArtifactRefusal(
            reason_code="artifact_template_unbound",
            reason="the stored proof profile does not bind this artifact template",
            next_action="request a template registered for the stored analysis kind",
        )
    metric_units = {metric.unit for metric in result.metrics}
    kind = chart_kind_for(cast("ChartTemplateId", template_id))
    if kind in {"scatter", "heatmap", "surface", "waterfall", "composite"}:
        return ArtifactRefusal(
            reason_code="artifact_template_values_unavailable",
            reason="the stored result does not contain the exact shape required by this template",
            next_action="store a verified analysis result with the template's exact value shape",
        )
    if kind in {"dashboard", "card"} and len(metric_units) != 1:
        return ArtifactRefusal(
            reason_code="artifact_template_values_unavailable",
            reason="the stored metrics do not share the unit required by this template",
            next_action="request a verified template with one truthful unit envelope",
        )
    if kind == "card" and len(result.metrics) != 1:
        return ArtifactRefusal(
            reason_code="artifact_template_values_unavailable",
            reason="the stored result has more values than the single-value card can render",
            next_action="request a chart template that renders every stored metric",
        )
    if len(metric_units) != 1:
        return ArtifactRefusal(
            reason_code="artifact_template_values_unavailable",
            reason="the stored metrics require separate axes that this result does not bind",
            next_action="store exact per-axis chart semantics through a verified analysis adapter",
        )
    unit = next(iter(metric_units))
    try:
        return ChartSemantics(
            template_id=cast("ChartTemplateId", template_id),
            analysis_kind=result.analysis_kind,
            title=template_id.replace("_", " ").title(),
            subtitle="Stored verified Saxo analysis",
            x_axis_title="Analysis cutoff",
            y_axis_title=f"Value ({unit})",
            secondary_y_axis_title=None,
            labels=(result.as_of.isoformat().replace("+00:00", "Z"),),
            series=tuple(
                ChartSeries(
                    name=metric.metric_id.replace("_", " ").title(),
                    values=(metric.value,),
                    unit=metric.unit,
                    style="line",
                    axis="primary",
                )
                for metric in result.metrics
            ),
            stamps=stamps,
        )
    except (KeyError, ValueError):
        return ArtifactRefusal(
            reason_code="artifact_template_values_unavailable",
            reason="the stored result cannot satisfy the exact registered chart semantics",
            next_action="request a verified template compatible with every stored metric",
        )


def _produce_and_deliver_bound(
    *,
    binding_id: str,
    producer: Callable[[ArtifactStamps], ArtifactPayload | ArtifactRefusal],
    config: AnalyticsConfig,
    store: AnalyticsStore,
    bindings: ArtifactBindingRegistry,
) -> ArtifactDelivery:
    """Render with derived visibility, rerendering when size requires a link stamp."""
    try:
        visibility = bindings._base_visibility(binding_id)
        payload = producer(bindings._stamps_for(binding_id, visibility=visibility))
        if isinstance(payload, ArtifactRefusal):
            return payload
        if payload.byte_count > config.limits.artifact_bytes and (
            visibility is not VisibilityMode.LOCAL_RESOURCE_LINK
        ):
            payload = producer(
                bindings._stamps_for(
                    binding_id,
                    visibility=VisibilityMode.LOCAL_RESOURCE_LINK,
                ),
            )
            if isinstance(payload, ArtifactRefusal):
                return payload
        return _deliver_bound_payload(
            payload,
            binding_id=binding_id,
            config=config,
            store=store,
            bindings=bindings,
        )
    except (StoreError, ValueError):
        return _binding_refusal()


def _deliver_bound_payload(
    payload: ArtifactPayload,
    *,
    binding_id: str,
    config: AnalyticsConfig,
    store: AnalyticsStore,
    bindings: ArtifactBindingRegistry,
) -> ArtifactDelivery:
    """Persist and deliver only a payload matching the exact stored binding stamps."""
    try:
        result = bindings._result_for(binding_id)
        base_visibility = bindings._base_visibility(binding_id)
        use_link = (
            base_visibility is VisibilityMode.LOCAL_RESOURCE_LINK
            or payload.byte_count > config.limits.artifact_bytes
        )
        expected_visibility = (
            VisibilityMode.LOCAL_RESOURCE_LINK if use_link else VisibilityMode.INLINE_PRIVATE
        )
        if payload.stamps != bindings._stamps_for(
            binding_id,
            visibility=expected_visibility,
        ):
            return _binding_refusal()
        if use_link:
            stored = store.put_owned_artifact(
                analysis_id=result.analysis_id,
                media_type=payload.media_type,
                extension=payload.extension,
                content=payload.content,
                description="stored verified analytics artifact",
            )
            return ArtifactResourceLink(
                artifact_id=stored.artifact_id,
                resource_uri=f"saxo-analytics://artifacts/{stored.artifact_id}",
                media_type=payload.media_type,
                byte_count=payload.byte_count,
                sha256=payload.sha256,
                semantics_sha256=payload.semantics_sha256,
                reason_code=(
                    "artifact_return_limit"
                    if payload.byte_count > config.limits.artifact_bytes
                    else "local_resource_link_requested"
                ),
                visible_stamps=visible_stamp_lines(payload.stamps),
            )
        artifact_id = new_safe_handle(HandleKind.ARTIFACT_ID)
        store.put_artifact(
            ArtifactSummary(
                visibility=VisibilityMode.INLINE_PRIVATE,
                artifact_id=artifact_id,
                analysis_id=result.analysis_id,
                media_type=payload.media_type,
                byte_count=payload.byte_count,
                sha256=payload.sha256,
                created_at=_utc_now(),
                description="stored verified inline analytics artifact",
            ),
        )
        return InlineArtifact(
            artifact_id=artifact_id,
            media_type=payload.media_type,
            content=payload.content,
            byte_count=payload.byte_count,
            sha256=payload.sha256,
            semantics_sha256=payload.semantics_sha256,
            visible_stamps=visible_stamp_lines(payload.stamps),
        )
    except (StoreError, ValueError):
        return _binding_refusal()


def _build_figure(
    semantics: ChartSemantics,
    *,
    width: int,
    height: int,
) -> tuple[Figure, FigureCanvasAgg, tuple[Text, ...], tuple[Text, ...]]:
    figure = Figure(
        figsize=(width / _DPI, height / _DPI),
        dpi=_DPI,
        facecolor="white",
    )
    canvas = FigureCanvasAgg(figure)
    axis = figure.add_axes((0.09, 0.27, 0.84, 0.50))
    bounded_texts: list[Text] = [
        figure.text(
            0.04,
            0.955,
            _wrap_display_text(semantics.title, max(24, width // 11)),
            fontsize=16,
            fontweight="bold",
            va="top",
        ),
        figure.text(
            0.04,
            0.89,
            _wrap_display_text(semantics.subtitle, max(30, width // 8)),
            fontsize=9,
            color="#4a5568",
            va="top",
        ),
    ]
    secondary_axis = _draw_chart(axis, semantics)
    axis.set_xlabel(semantics.x_axis_title, fontsize=9, labelpad=7)
    axis.set_ylabel(semantics.y_axis_title, fontsize=9, labelpad=7)
    bounded_texts.extend((axis.xaxis.label, axis.yaxis.label))
    if secondary_axis is not None:
        secondary_title = semantics.secondary_y_axis_title
        if secondary_title is None:
            raise ValueError("secondary chart axis is missing its title")
        secondary_axis.set_ylabel(secondary_title, fontsize=9, labelpad=7)
        bounded_texts.append(secondary_axis.yaxis.label)
    axis.grid(visible=True, axis="y", linewidth=0.5, color="#d9e2ef", alpha=0.8)
    axis.tick_params(labelsize=8)
    kind = chart_kind_for(semantics.template_id)
    if kind not in {"scatter", "dashboard", "card"}:
        _bounded_ticks(axis, semantics.labels)
    else:
        axis.xaxis.set_major_locator(MaxNLocator(nbins=6, prune="both"))
    label_texts = (
        *axis.get_xticklabels(),
        *axis.get_yticklabels(),
        *(secondary_axis.get_yticklabels() if secondary_axis is not None else ()),
    )
    handles, legend_labels = axis.get_legend_handles_labels()
    if secondary_axis is not None:
        secondary_handles, secondary_labels = secondary_axis.get_legend_handles_labels()
        handles.extend(secondary_handles)
        legend_labels.extend(secondary_labels)
    if handles:
        drawn = axis.legend(
            handles,
            legend_labels,
            loc="upper left",
            frameon=False,
            fontsize=8,
            ncols=min(3, len(semantics.series)),
        )
        bounded_texts.extend(drawn.get_texts())
    compact_lines = _compact_stamp_lines(semantics.stamps)
    for index, line in enumerate(compact_lines):
        bounded_texts.append(
            figure.text(
                0.04,
                0.175 - index * 0.038,
                line,
                fontsize=7,
                color="#27364d",
                va="top",
            ),
        )
    return figure, canvas, tuple(bounded_texts), label_texts


def _draw_chart(axis: Axes, semantics: ChartSemantics) -> Axes | None:
    kind = chart_kind_for(semantics.template_id)
    positions = np.arange(len(semantics.labels), dtype=np.float64)
    if kind in {"heatmap", "surface"}:
        _draw_heatmap(axis, semantics)
    elif kind == "waterfall":
        _draw_waterfall(axis, semantics.series[0], positions)
    elif kind == "bar":
        _draw_bars(axis, semantics.series, positions)
    elif kind == "scatter":
        _draw_scatter(axis, semantics.series)
    elif kind in {"dashboard", "card"}:
        _draw_dashboard(axis, semantics.series)
    else:
        return _draw_lines(axis, semantics.series, positions, composite=kind == "composite")
    return None


def _draw_lines(
    axis: Axes,
    series_values: Sequence[ChartSeries],
    positions: np.ndarray[tuple[int], np.dtype[np.float64]],
    *,
    composite: bool,
) -> Axes | None:
    secondary: Axes | None = None
    for index, series in enumerate(series_values):
        values = _numeric_values(series.values)
        target = axis
        if series.axis == "secondary":
            secondary = secondary or axis.twinx()
            target = secondary
            target.tick_params(labelsize=8)
        label = _short_label(series.name, 34)
        color = _COLORS[index % len(_COLORS)]
        if series.style == "bar" and composite:
            target.bar(positions, values, width=0.65, alpha=0.35, color=color, label=label)
        elif series.style == "area":
            target.fill_between(positions, 0, values, alpha=0.22, color=color, label=label)
            target.plot(positions, values, linewidth=1.4, color=color)
        else:
            target.plot(
                positions,
                values,
                linewidth=1.8,
                marker="o",
                markersize=3.5,
                color=color,
                label=label,
            )
    return secondary


def _draw_bars(
    axis: Axes,
    series_values: Sequence[ChartSeries],
    positions: np.ndarray[tuple[int], np.dtype[np.float64]],
) -> None:
    width = 0.75 / len(series_values)
    offset_start = -(len(series_values) - 1) * width / 2
    for index, series in enumerate(series_values):
        axis.bar(
            positions + offset_start + index * width,
            _numeric_values(series.values),
            width=width,
            color=_COLORS[index % len(_COLORS)],
            label=_short_label(series.name, 34),
        )


def _draw_waterfall(
    axis: Axes,
    series: ChartSeries,
    positions: np.ndarray[tuple[int], np.dtype[np.float64]],
) -> None:
    values = _numeric_values(series.values)
    starts = np.concatenate((np.array([0.0]), np.cumsum(values[:-1])))
    colors = tuple("#38a169" if value >= 0 else "#c53030" for value in values)
    axis.bar(
        positions,
        values,
        bottom=starts,
        width=0.66,
        color=colors,
        label=_short_label(series.name, 34),
    )
    axis.plot(positions, np.cumsum(values), color="#4a5568", linewidth=0.8)


def _draw_heatmap(axis: Axes, semantics: ChartSemantics) -> None:
    matrix = np.array(
        [
            [np.nan if value is None else value for value in series.values]
            for series in semantics.series
        ],
        dtype=np.float64,
    )
    axis.imshow(matrix, aspect="auto", interpolation="nearest", cmap="RdYlBu")
    axis.set_yticks(np.arange(len(semantics.series)))
    axis.set_yticklabels(tuple(_short_label(series.name, 22) for series in semantics.series))


def _draw_scatter(
    axis: Axes,
    series_values: Sequence[ChartSeries],
) -> None:
    if len(series_values) != _SCATTER_SERIES_COUNT:
        raise ValueError("scatter chart cardinality was not validated")
    x_values = _numeric_values(series_values[0].values)
    y_values = _numeric_values(series_values[1].values)
    axis.scatter(
        x_values,
        y_values,
        color=_COLORS[0],
        s=28,
        label=" / ".join(
            (_short_label(series_values[0].name, 18), _short_label(series_values[1].name, 18)),
        ),
    )


def _draw_dashboard(
    axis: Axes,
    series_values: Sequence[ChartSeries],
) -> None:
    latest = np.array(
        [_required_latest(series.values) for series in series_values],
        dtype=np.float64,
    )
    dashboard_positions = np.arange(len(series_values), dtype=np.float64)
    axis.barh(
        dashboard_positions,
        latest,
        color=tuple(_COLORS[index % len(_COLORS)] for index in range(len(series_values))),
    )
    axis.set_yticks(dashboard_positions)
    axis.set_yticklabels(tuple(_short_label(series.name, 24) for series in series_values))


def _bounded_ticks(axis: Axes, labels: Sequence[str]) -> None:
    positions = np.arange(len(labels), dtype=np.float64)
    sampled = _sample_tick_positions(positions)
    axis.set_xticks(sampled)
    tick_labels = axis.set_xticklabels(
        tuple(_short_label(labels[int(position)], 18) for position in sampled),
        rotation=18,
        ha="center",
    )
    if tick_labels:
        tick_labels[0].set_horizontalalignment("left")
        tick_labels[-1].set_horizontalalignment("right")


def _sample_tick_positions(
    positions: np.ndarray[tuple[int], np.dtype[np.float64]],
) -> np.ndarray[tuple[int], np.dtype[np.float64]]:
    if len(positions) <= _MAX_DISPLAY_TICKS:
        return positions
    indices = np.linspace(
        0,
        len(positions) - 1,
        num=_MAX_DISPLAY_TICKS,
        dtype=np.int64,
    )
    return positions[indices]


def _pixel_and_text_qa(
    pixels: np.ndarray[tuple[int, int, int], np.dtype[np.uint8]],
    *,
    canvas: FigureCanvasAgg,
    bounded_texts: Sequence[Text],
    label_texts: Sequence[Text],
) -> VisualQa:
    height, width, _channels = pixels.shape
    ink = np.any(pixels[:, :, :3] < _INK_THRESHOLD, axis=2)
    footer_start = int(height * 0.80)
    border = np.concatenate(
        (
            ink[:2, :].ravel(),
            ink[-2:, :].ravel(),
            ink[:, :2].ravel(),
            ink[:, -2:].ravel(),
        ),
    )
    renderer = canvas.get_renderer()
    rendered_texts = tuple(canvas.figure.findobj(match=Text))
    qa_texts = tuple(
        {id(item): item for item in (*bounded_texts, *label_texts, *rendered_texts)}.values(),
    )
    clipped = 0
    visible_bounds: list[Bbox] = []
    for item in qa_texts:
        if not item.get_visible() or not item.get_text():
            continue
        bounds = item.get_window_extent(renderer=renderer)
        visible_bounds.append(bounds)
        if (
            bounds.x0 < -_CANVAS_TOLERANCE
            or bounds.y0 < -_CANVAS_TOLERANCE
            or bounds.x1 > width + _CANVAS_TOLERANCE
            or bounds.y1 > height + _CANVAS_TOLERANCE
        ):
            clipped += 1
    overlapping = sum(
        int(first.overlaps(second))
        for index, first in enumerate(visible_bounds)
        for second in visible_bounds[index + 1 :]
    )
    return VisualQa(
        width=width,
        height=height,
        non_background_fraction=float(np.mean(ink)),
        footer_ink_fraction=float(np.mean(ink[footer_start:, :])),
        edge_ink_fraction=float(np.mean(border)),
        clipped_text_count=clipped,
        overlapping_label_count=overlapping,
    )


def _compact_stamp_lines(stamps: ArtifactStamps) -> tuple[str, str, str]:
    lines = visible_stamp_lines(stamps)
    return (
        " | ".join(lines[:6]),
        " | ".join(lines[6:9]),
        " | ".join(lines[9:]),
    )


def _plotly_figure(semantics: ChartSemantics) -> dict[str, object]:
    kind = chart_kind_for(semantics.template_id)
    data: list[dict[str, object]]
    if kind in {"heatmap", "surface"}:
        data = [
            {
                "type": "surface" if kind == "surface" else "heatmap",
                "x": list(semantics.labels),
                "y": [series.name for series in semantics.series],
                "z": [list(series.values) for series in semantics.series],
                "colorscale": "RdYlBu",
                "hoverongaps": False,
            },
        ]
    elif kind == "waterfall":
        series = semantics.series[0]
        data = [
            {
                "type": "waterfall",
                "name": series.name,
                "x": list(semantics.labels),
                "y": list(series.values),
                "measure": ["relative"] * len(semantics.labels),
            },
        ]
    elif kind == "bar":
        data = [
            {
                "type": "bar",
                "name": series.name,
                "x": list(semantics.labels),
                "y": list(series.values),
            }
            for series in semantics.series
        ]
    elif kind == "scatter":
        data = [
            {
                "type": "scatter",
                "mode": "markers",
                "name": f"{semantics.series[0].name} / {semantics.series[1].name}",
                "text": list(semantics.labels),
                "x": list(semantics.series[0].values),
                "y": list(semantics.series[1].values),
            },
        ]
    elif kind in {"dashboard", "card"}:
        data = [
            {
                "type": "bar",
                "orientation": "h",
                "x": [_required_latest(series.values)],
                "y": [series.name],
                "name": series.name,
            }
            for series in semantics.series
        ]
    else:
        data = [
            {
                "type": "scatter" if series.style != "bar" else "bar",
                "mode": "lines+markers",
                "fill": "tozeroy" if series.style == "area" else "none",
                "connectgaps": False,
                "name": series.name,
                "x": list(semantics.labels),
                "y": list(series.values),
                "yaxis": "y2" if series.axis == "secondary" else "y",
            }
            for series in semantics.series
        ]
    warning_text = ", ".join(semantics.stamps.warnings) or "none"
    layout: dict[str, object] = {
        "autosize": True,
        "barmode": "group",
        "font": {"family": "Arial, sans-serif", "size": 12, "color": "#162033"},
        "hovermode": "closest",
        "margin": {"b": 80, "l": 70, "r": 60, "t": 40},
        "paper_bgcolor": "#ffffff",
        "plot_bgcolor": "#ffffff",
        "showlegend": True,
        "template": None,
        "xaxis": {"title": semantics.x_axis_title, "automargin": True},
        "yaxis": {"title": semantics.y_axis_title, "automargin": True, "zeroline": True},
        "yaxis2": {
            "title": semantics.secondary_y_axis_title or "",
            "overlaying": "y",
            "side": "right",
            "automargin": True,
        },
        "meta": {
            "analysis_id": semantics.stamps.analysis_id,
            "semantics_sha256": chart_semantics_sha256(semantics),
            "environment": semantics.stamps.environment,
            "data_cutoff": semantics.stamps.data_cutoff.isoformat().replace("+00:00", "Z"),
            "quote_delay": semantics.stamps.quote_delay,
            "price_type": semantics.stamps.price_type,
            "currency": semantics.stamps.currency,
            "adjustment_status": semantics.stamps.adjustment_status,
            "warnings": warning_text,
            "source_scope": semantics.stamps.source_scope,
            "source_revision": semantics.stamps.source_revision,
            "visibility": semantics.stamps.visibility.value,
            "privacy_footer": PRIVACY_FOOTER,
        },
    }
    if kind == "surface":
        layout["scene"] = {
            "xaxis": {"title": semantics.x_axis_title},
            "yaxis": {"title": "Series"},
            "zaxis": {"title": semantics.y_axis_title},
        }
    return {"data": data, "layout": layout}


def _semantic_fallback_table(semantics: ChartSemantics) -> str:
    header = "".join(f"<th>{html.escape(series.name)}</th>" for series in semantics.series)
    rows: list[str] = []
    for index, label in enumerate(semantics.labels):
        cells = "".join(
            f'<td data-field-label="{html.escape(series.name)}" data-canonical-value="{html.escape(_canonical_value(series.values[index]))}">{html.escape(_canonical_value(series.values[index]))}</td>'
            for series in semantics.series
        )
        rows.append(
            f'<tr><td data-field-label="{html.escape(semantics.x_axis_title)}">{html.escape(label)}</td>{cells}</tr>',
        )
    return (
        '<section class="semantic-fallback" data-mobile-layout="stacked" aria-label="Exact chart values">'
        f"<table><thead><tr><th>{html.escape(semantics.x_axis_title)}</th>{header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _script_safe_json(value: object) -> str:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


@cache
def _compressed_sanitized_plotly_runtime() -> str:
    resource = files("plotly").joinpath(_PLOTLY_RESOURCE)
    runtime = resource.read_bytes()
    placeholders: dict[bytes, bytes] = {}
    for index, namespace in enumerate(_PLOTLY_NAMESPACE_URLS):
        placeholder = f"__SAXO_NAMESPACE_{index}__".encode()
        placeholders[placeholder] = namespace.encode()
        runtime = runtime.replace(namespace.encode(), placeholder)
    runtime = runtime.replace(b"https://", b"blocked:").replace(b"http://", b"blocked:")
    runtime = re.sub(rb"\bfetch\s*\(", b"networkDisabled(", runtime, flags=re.IGNORECASE)
    runtime = re.sub(
        rb"XMLHttpRequest|WebSocket|EventSource",
        b"NetworkCapabilityDisabled",
        runtime,
        flags=re.IGNORECASE,
    )
    for placeholder, namespace in placeholders.items():
        expression = b'["http","://' + namespace.split(b"://", maxsplit=1)[1] + b'"].join("")'
        runtime = runtime.replace(b'"' + placeholder + b'"', expression)
        runtime = runtime.replace(b"'" + placeholder + b"'", expression)
        runtime = runtime.replace(placeholder, b"blocked:namespace")
    if any(pattern.search(runtime) is not None for pattern in _NETWORK_RUNTIME_PATTERNS):
        raise RuntimeError("sanitized Plotly runtime retains a network capability")
    compressed = gzip.compress(runtime, compresslevel=9, mtime=0)
    return base64.b64encode(compressed).decode("ascii")


def _numeric_values(values: Sequence[float | None]) -> np.ndarray[tuple[int], np.dtype[np.float64]]:
    return np.array(
        [np.nan if value is None else value for value in values],
        dtype=np.float64,
    )


def _required_latest(values: Sequence[float | None]) -> float:
    latest = values[-1]
    if latest is None or not math.isfinite(latest):
        raise ValueError("latest chart value was not validated")
    return latest


def _canonical_value(value: float | None) -> str:
    return "missing" if value is None else repr(value)


def _short_label(value: str, width: int) -> str:
    return textwrap.shorten(value, width=width, placeholder="…")


def _wrap_display_text(value: str, width: int) -> str:
    return "\n".join(
        textwrap.wrap(
            value,
            width=width,
            break_long_words=True,
            break_on_hyphens=True,
        ),
    )


def _has_renderable_values(semantics: ChartSemantics) -> bool:
    return any(value is not None for series in semantics.series for value in series.values)


def _blank_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_has_no_renderable_values",
        reason="the structured result contains no finite values to render",
        next_action="request a source result with usable observations",
    )


def _dimension_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_dimensions_unsupported",
        reason="artifact dimensions are outside the bounded renderer range",
        next_action="request a width from 320 to 2560 and height from 240 to 1600",
    )


def _visual_qa_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_visual_qa_failed",
        reason="artifact QA detected clipping, overlap, overflow, or unreadable layout",
        next_action="shorten valid labels or request a larger bounded artifact",
    )


def _unbound_analysis_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_analysis_unbound",
        reason="caller-composed values cannot establish stored Saxo analysis provenance",
        next_action="render from a server-issued stored analysis binding",
    )


def _binding_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_analysis_binding_invalid",
        reason="the server-issued stored analysis binding is missing, stale, or inconsistent",
        next_action="replay the current verified analysis and request a new opaque binding",
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
