# pyright: reportUnknownMemberType=false
# ruff: noqa: E501
from __future__ import annotations

import hashlib
import html
import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Annotated, Literal, Self

import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from PIL import Image
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from saxo_bank_mcp.analytics_chart_semantics import (
    PRIVACY_FOOTER,
    ChartSemantics,
    chart_semantics_sha256,
    validate_artifact_text,
    visible_stamp_lines,
)
from saxo_bank_mcp.analytics_render import (
    ArtifactPayload,
    ArtifactRefusal,
    build_artifact_payload,
    render_png,
)

type ReportFormat = Literal["html", "pdf"]
_MIN_REPORT_WIDTH = 320
_MAX_REPORT_WIDTH = 2560
type ReportTitle = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=240),
    AfterValidator(validate_artifact_text),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class AnalysisReport(_StrictModel):
    schema_version: Literal["1"] = "1"
    title: ReportTitle
    charts: tuple[ChartSemantics, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_source_linkage(self) -> Self:
        first = self.charts[0].stamps
        if any(
            chart.stamps.analysis_id != first.analysis_id
            or chart.stamps.environment != first.environment
            or chart.stamps.data_cutoff != first.data_cutoff
            or chart.stamps.source_scope != first.source_scope
            or chart.stamps.source_revision != first.source_revision
            or chart.stamps.visibility != first.visibility
            for chart in self.charts[1:]
        ):
            raise ValueError("report charts must share one source-linked analysis and visibility")
        return self


def report_semantics_sha256(report: AnalysisReport) -> str:
    """Fingerprint the exact report definition and chart semantics."""
    encoded = json.dumps(
        report.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def render_report_html(
    report: AnalysisReport,
    *,
    viewport_width: int = 1280,
) -> ArtifactPayload | ArtifactRefusal:
    """Render a deterministic, script-free HTML report from exact chart values."""
    if not _MIN_REPORT_WIDTH <= viewport_width <= _MAX_REPORT_WIDTH:
        return _report_dimension_refusal()
    stamps = report.charts[0].stamps
    sections = "".join(_chart_section(chart) for chart in report.charts)
    stamp_html = "".join(
        f"<span>{html.escape(line)}</span>" for line in visible_stamp_lines(stamps)[:-1]
    )
    report_sha256 = report_semantics_sha256(report)
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; connect-src 'none'">
<title>{html.escape(report.title)}</title><style>
:root{{--report-width:{viewport_width}px;--ink:#162033;--muted:#4a5568;--line:#d9e2ef}}
*{{box-sizing:border-box}}html,body{{margin:0;padding:0;background:#fff;color:var(--ink);font-family:Arial,sans-serif;font-size:14px;overflow-x:hidden}}
main{{width:min(100%,var(--report-width));max-width:100%;margin:auto;padding:16px}}h1{{font-size:24px;margin:0 0 14px;overflow-wrap:anywhere}}
section{{margin:0 0 18px;break-inside:avoid}}h2{{font-size:18px;margin:0 0 3px;overflow-wrap:anywhere}}p{{font-size:13px;color:var(--muted);margin:0 0 7px}}
table{{width:100%;max-width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}}th,td{{border:1px solid var(--line);padding:5px;text-align:right;overflow-wrap:anywhere}}th:first-child,td:first-child{{text-align:left}}
.stamps{{display:flex;flex-wrap:wrap;gap:6px 14px;border-top:1px solid var(--line);padding-top:8px;font-size:12px;line-height:1.35}}.privacy{{font-size:12px;font-weight:700;margin-top:8px}}
@media(max-width:600px){{main{{padding:8px}}h1{{font-size:20px}}h2{{font-size:16px}}table,.stamps,.privacy{{font-size:12px}}}}
</style></head><body data-viewport-width="{viewport_width}" data-report-sha256="{report_sha256}"><main>
<h1>{html.escape(report.title)}</h1>{sections}<footer><div class="stamps">{stamp_html}</div><div class="privacy">{PRIVACY_FOOTER}</div></footer>
</main></body></html>"""
    return build_artifact_payload(
        media_type="text/html",
        extension="html",
        content=document.encode(),
        semantics_sha256=report_sha256,
        stamps=stamps,
        width=viewport_width,
        height=max(480, 360 * len(report.charts)),
    )


def render_report_pdf(report: AnalysisReport) -> ArtifactPayload | ArtifactRefusal:
    """Render deterministic PDF pages from the same exact PNG semantics."""
    report_sha256 = report_semantics_sha256(report)
    rendered: list[ArtifactPayload] = []
    for chart in report.charts:
        payload = render_png(chart, width=1200, height=675)
        if isinstance(payload, ArtifactRefusal):
            return payload
        rendered.append(payload)
    buffer = BytesIO()
    fixed_at = datetime(1970, 1, 1, tzinfo=UTC)
    metadata = {
        "Title": report.title,
        "Author": "Saxo analytics",
        "Subject": report_sha256,
        "Keywords": "source-linked deterministic owner-only analytics",
        "Creator": "saxo-analytics-matplotlib-1",
        "Producer": "saxo-analytics-matplotlib-1",
        "CreationDate": fixed_at,
        "ModDate": fixed_at,
    }
    with PdfPages(buffer, metadata=metadata) as pdf:
        for payload in rendered:
            with Image.open(BytesIO(payload.content)) as image:
                pixels = np.asarray(image.convert("RGB")).copy()
            figure = Figure(figsize=(12, 6.75), dpi=100, facecolor="white")
            axis = figure.add_axes((0, 0, 1, 1))
            axis.imshow(pixels)
            axis.set_axis_off()
            pdf.savefig(figure, dpi=100, facecolor="white")
            figure.clear()
    return build_artifact_payload(
        media_type="application/pdf",
        extension="pdf",
        content=buffer.getvalue(),
        semantics_sha256=report_sha256,
        stamps=report.charts[0].stamps,
    )


def _chart_section(chart: ChartSemantics) -> str:
    header = "".join(f"<th>{html.escape(series.name)}</th>" for series in chart.series)
    rows: list[str] = []
    for index, label in enumerate(chart.labels):
        cells = "".join(
            f'<td data-canonical-value="{html.escape(_canonical_value(series.values[index]))}">{html.escape(_canonical_value(series.values[index]))}</td>'
            for series in chart.series
        )
        rows.append(f"<tr><td>{html.escape(label)}</td>{cells}</tr>")
    return (
        f'<section data-template-id="{chart.template_id}" data-semantics-sha256="{chart_semantics_sha256(chart)}">'
        f"<h2>{html.escape(chart.title)}</h2><p>{html.escape(chart.subtitle)}</p>"
        f"<table><thead><tr><th>{html.escape(chart.x_axis_title)}</th>{header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _canonical_value(value: float | None) -> str:
    return "missing" if value is None else repr(value)


def _report_dimension_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_dimensions_unsupported",
        reason="report width is outside the bounded renderer range",
        next_action="request a report width from 320 to 2560",
    )
