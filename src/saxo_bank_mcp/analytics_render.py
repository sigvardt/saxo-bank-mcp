# pyright: reportUnknownMemberType=false
# ruff: noqa: E501
from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import math
import os
import re
import stat
import textwrap
from collections.abc import Iterable, Sequence
from functools import cache
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from typing import Final, Literal, Self
from uuid import UUID

import matplotlib as mpl
import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.text import Text
from matplotlib.ticker import MaxNLocator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_chart_semantics import (
    PRIVACY_FOOTER,
    ArtifactStamps,
    ChartSemantics,
    ChartSeries,
    chart_kind_for,
    chart_semantics_sha256,
    visible_stamp_lines,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_models import ArtifactId, Sha256Fingerprint, VisibilityMode

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
_INK_THRESHOLD: Final = 248
_CANVAS_TOLERANCE: Final = 0.5
_OWNER_FILE_MODE: Final = 0o600
_OWNER_DIRECTORY_MODE: Final = 0o700
_OPAQUE_UUID_VERSION: Final = 4
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
    trusted_local_host: bool


def render_png(
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
@media(max-width:600px){{main{{padding:8px}}h1{{font-size:18px}}#chart{{height:{max(320, min(height, 480))}px}}.stamps{{font-size:12px}}table{{font-size:12px}}}}
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
        )
    external_count = sum(len(pattern.findall(document)) for pattern in _EXTERNAL_HTML_PATTERNS)
    font_sizes = tuple(int(value) for value in re.findall(r"font-size:(\d+)px", document))
    declared_width = re.search(r'data-viewport-width="(\d+)"', document)
    readable = (
        all(
            marker in document
            for marker in (
                "data-semantics-sha256=",
                'id="chart"',
                "semantic-fallback",
                PRIVACY_FOOTER,
            )
        )
        and declared_width is not None
        and int(declared_width.group(1)) == viewport_width
    )
    return HtmlVisualQa(
        viewport_width=viewport_width,
        readable=readable,
        horizontal_overflow="overflow-x:hidden" not in document or "max-width:100%" not in document,
        minimum_font_size_px=min(font_sizes, default=0),
        external_url_count=external_count,
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
    trusted_local_host: bool,
) -> ArtifactDelivery:
    """Enforce visibility and 25 MiB delivery before returning bytes or a safe link."""
    artifact_id = _artifact_id(payload.sha256)
    visible_stamps = visible_stamp_lines(payload.stamps)
    reason: DeliveryReason | None = None
    if payload.stamps.visibility is VisibilityMode.LOCAL_RESOURCE_LINK:
        reason = "local_resource_link_requested"
    elif payload.stamps.visibility is VisibilityMode.INLINE_PRIVATE:
        if payload.stamps.environment == "LIVE" and not trusted_local_host:
            reason = "inline_private_not_enabled"
        elif payload.byte_count > config.limits.artifact_bytes:
            reason = "artifact_return_limit"
    else:
        return ArtifactRefusal(
            reason_code="artifact_visibility_unsupported",
            reason="numeric artifacts require inline_private or local_resource_link visibility",
            next_action="request one of the supported owner-only artifact delivery modes",
        )
    if reason is None:
        return InlineArtifact(
            artifact_id=artifact_id,
            media_type=payload.media_type,
            content=payload.content,
            byte_count=payload.byte_count,
            sha256=payload.sha256,
            semantics_sha256=payload.semantics_sha256,
            visible_stamps=visible_stamps,
        )
    persisted = _persist_owner_artifact(payload, artifact_id=artifact_id, config=config)
    if isinstance(persisted, ArtifactRefusal):
        return persisted
    return ArtifactResourceLink(
        artifact_id=artifact_id,
        resource_uri=f"saxo-analytics://artifacts/{artifact_id}",
        media_type=payload.media_type,
        byte_count=payload.byte_count,
        sha256=payload.sha256,
        semantics_sha256=payload.semantics_sha256,
        reason_code=reason,
        visible_stamps=visible_stamps,
    )


def render_analysis(request: RenderRequest, *, config: AnalyticsConfig) -> ArtifactDelivery:
    """Render one exact semantic chart and apply the artifact delivery boundary."""
    if request.output_format == "png":
        payload = render_png(
            request.semantics,
            width=request.width,
            height=request.height,
        )
    else:
        payload = render_plotly_html(
            request.semantics,
            viewport_width=request.width,
            height=request.height,
        )
    if isinstance(payload, ArtifactRefusal):
        return payload
    return deliver_artifact(
        payload,
        config=config,
        trusted_local_host=request.trusted_local_host,
    )


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
    axis = figure.add_axes((0.09, 0.27, 0.84, 0.58))
    bounded_texts: list[Text] = [
        figure.text(0.04, 0.955, semantics.title, fontsize=16, fontweight="bold", va="top"),
        figure.text(0.04, 0.91, semantics.subtitle, fontsize=9, color="#4a5568", va="top"),
    ]
    _draw_chart(axis, semantics)
    axis.set_xlabel(semantics.x_axis_title, fontsize=9, labelpad=7)
    axis.set_ylabel(semantics.y_axis_title, fontsize=9, labelpad=7)
    axis.grid(visible=True, axis="y", linewidth=0.5, color="#d9e2ef", alpha=0.8)
    axis.tick_params(labelsize=8)
    kind = chart_kind_for(semantics.template_id)
    if kind not in {"scatter", "dashboard", "card"}:
        _bounded_ticks(axis, semantics.labels)
    else:
        axis.xaxis.set_major_locator(MaxNLocator(nbins=6, prune="both"))
    label_texts = tuple(axis.get_xticklabels())
    legend = axis.get_legend_handles_labels()
    if legend[0]:
        drawn = axis.legend(
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


def _draw_chart(axis: Axes, semantics: ChartSemantics) -> None:
    kind = chart_kind_for(semantics.template_id)
    positions = np.arange(len(semantics.labels), dtype=np.float64)
    if kind in {"heatmap", "surface"}:
        _draw_heatmap(axis, semantics)
    elif kind == "waterfall":
        _draw_waterfall(axis, semantics.series[0], positions)
    elif kind == "bar":
        _draw_bars(axis, semantics.series, positions)
    elif kind == "scatter":
        _draw_scatter(axis, semantics.series, positions)
    elif kind in {"dashboard", "card"}:
        _draw_dashboard(axis, semantics.series)
    else:
        _draw_lines(axis, semantics.series, positions, composite=kind == "composite")


def _draw_lines(
    axis: Axes,
    series_values: Sequence[ChartSeries],
    positions: np.ndarray[tuple[int], np.dtype[np.float64]],
    *,
    composite: bool,
) -> None:
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
    values = np.nan_to_num(_numeric_values(series.values), nan=0.0)
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
    positions: np.ndarray[tuple[int], np.dtype[np.float64]],
) -> None:
    if len(series_values) >= _SCATTER_SERIES_COUNT:
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
        return
    axis.scatter(
        positions,
        _numeric_values(series_values[0].values),
        color=_COLORS[0],
        s=28,
        label=_short_label(series_values[0].name, 34),
    )


def _draw_dashboard(
    axis: Axes,
    series_values: Sequence[ChartSeries],
) -> None:
    latest = np.array(
        [_last_finite(series.values) for series in series_values],
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
    axis.set_xticklabels(
        tuple(_short_label(labels[int(position)], 18) for position in sampled),
        rotation=18,
        ha="right",
    )


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
    clipped = 0
    for item in (*bounded_texts, *label_texts):
        if not item.get_visible() or not item.get_text():
            continue
        bounds = item.get_window_extent(renderer=renderer)
        if (
            bounds.x0 < -_CANVAS_TOLERANCE
            or bounds.y0 < -_CANVAS_TOLERANCE
            or bounds.x1 > width + _CANVAS_TOLERANCE
            or bounds.y1 > height + _CANVAS_TOLERANCE
        ):
            clipped += 1
    visible_labels = tuple(
        label.get_window_extent(renderer=renderer)
        for label in label_texts
        if label.get_visible() and label.get_text()
    )
    overlapping = sum(
        int(first.overlaps(second))
        for index, first in enumerate(visible_labels)
        for second in visible_labels[index + 1 :]
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
    elif kind == "scatter" and len(semantics.series) >= _SCATTER_SERIES_COUNT:
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
                "x": [_last_finite(series.values)],
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
            "title": semantics.y_axis_title,
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
            f'<td data-canonical-value="{html.escape(_canonical_value(series.values[index]))}">{html.escape(_canonical_value(series.values[index]))}</td>'
            for series in semantics.series
        )
        rows.append(f"<tr><td>{html.escape(label)}</td>{cells}</tr>")
    return (
        '<section class="semantic-fallback" aria-label="Exact chart values">'
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


def _persist_owner_artifact(  # noqa: PLR0911
    payload: ArtifactPayload,
    *,
    artifact_id: ArtifactId,
    config: AnalyticsConfig,
) -> Path | ArtifactRefusal:
    artifacts_dir = config.paths.artifacts_dir
    try:
        resolved_root = config.paths.state_root.resolve(strict=True)
        resolved_dir = artifacts_dir.resolve(strict=True)
        mode = stat.S_IMODE(resolved_dir.stat().st_mode)
    except OSError:
        return _storage_refusal()
    if (
        artifacts_dir.is_symlink()
        or not resolved_dir.is_relative_to(resolved_root)
        or mode != _OWNER_DIRECTORY_MODE
    ):
        return _storage_refusal()
    destination = resolved_dir / f"{artifact_id}.{payload.extension}"
    if destination.exists() or destination.is_symlink():
        try:
            if (
                destination.is_symlink()
                or not destination.is_file()
                or hashlib.sha256(destination.read_bytes()).hexdigest() != payload.sha256
            ):
                return _storage_refusal()
            destination.chmod(_OWNER_FILE_MODE)
            return destination  # noqa: TRY300
        except OSError:
            return _storage_refusal()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(destination, flags, _OWNER_FILE_MODE)
        _write_all(descriptor, payload.content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        destination.chmod(_OWNER_FILE_MODE)
        if (
            stat.S_IMODE(destination.stat().st_mode) != _OWNER_FILE_MODE
            or hashlib.sha256(destination.read_bytes()).hexdigest() != payload.sha256
        ):
            return _storage_refusal()
        return destination  # noqa: TRY300
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        return _storage_refusal()


def _artifact_id(sha256: str) -> str:
    raw = bytearray(bytes.fromhex(sha256)[:16])
    raw[6] = (raw[6] & 0x0F) | (_OPAQUE_UUID_VERSION << 4)
    raw[8] = (raw[8] & 0x3F) | 0x80
    return f"ar_{UUID(bytes=bytes(raw)).hex}"


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("artifact write made no progress")
        view = view[written:]


def _numeric_values(values: Sequence[float | None]) -> np.ndarray[tuple[int], np.dtype[np.float64]]:
    return np.array(
        [np.nan if value is None else value for value in values],
        dtype=np.float64,
    )


def _last_finite(values: Iterable[float | None]) -> float:
    finite = tuple(value for value in values if value is not None and math.isfinite(value))
    return 0.0 if not finite else finite[-1]


def _canonical_value(value: float | None) -> str:
    return "missing" if value is None else repr(value)


def _short_label(value: str, width: int) -> str:
    return textwrap.shorten(value, width=width, placeholder="…")


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


def _storage_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_owner_storage_unavailable",
        reason="the owner-only artifact directory could not be proven or written safely",
        next_action="restore the configured owner-only analytics artifact directory",
    )
