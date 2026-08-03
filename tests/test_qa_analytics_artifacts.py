# pyright: reportPrivateUsage=false
"""Measured, contract-keyed artifact receipts for the installed proof producer."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from saxo_bank_mcp.analytics_chart_semantics import (
    ChartSemantics,
    TemplateBinding,
    chart_semantics_sha256,
    core_template_bindings,
    structured_chart_semantics,
)
from saxo_bank_mcp.analytics_render import (
    ArtifactPayload,
    _render_plotly_html_payload,
    _render_png_payload,
    inspect_html_artifact,
)
from saxo_bank_mcp.qa_analytics_artifacts import (
    ArtifactParityReceipt,
    ArtifactVisualIntegrityReceipt,
)

ARTIFACT_TEMPLATE_IDS = tuple(item.template_id for item in core_template_bindings())
_GOLDEN = Path(__file__).parent / "fixtures/analytics/golden_artifacts/golden_analysis.json"


def _base_chart() -> ChartSemantics:
    document = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return ChartSemantics.model_validate_json(
        json.dumps(document["chart"], separators=(",", ":"), sort_keys=True),
    )


def _chart_for(binding: TemplateBinding) -> ChartSemantics:
    payload = _base_chart().model_dump(mode="json")
    series = cast("list[dict[str, object]]", payload["series"])
    payload.update(
        {
            "analysis_kind": binding.analysis_kind,
            "template_id": binding.template_id,
            "title": f"Synthetic {binding.template_id.replace('_', ' ')}",
        },
    )
    if binding.chart_kind == "scatter":
        payload.update(
            {
                "x_axis_title": "Index",
                "y_axis_title": "Percent",
                "secondary_y_axis_title": None,
                "series": series[:2],
            },
        )
    elif binding.chart_kind == "waterfall":
        payload.update({"secondary_y_axis_title": None, "series": series[:1]})
    elif binding.chart_kind in {"dashboard", "card"}:
        selected = [
            {**item, "values": [cast("list[float]", item["values"])[-1]], "axis": "primary"}
            for item in (series[0], series[2])
        ]
        payload.update(
            {
                "labels": [cast("list[str]", payload["labels"])[-1]],
                "y_axis_title": "Index",
                "secondary_y_axis_title": None,
                "series": selected[:1] if binding.chart_kind == "card" else selected,
            },
        )
    elif binding.chart_kind != "composite":
        payload.update({"secondary_y_axis_title": None, "series": [series[0], series[2]]})
    return ChartSemantics.model_validate_json(json.dumps(payload, separators=(",", ":")))


@pytest.mark.parametrize(
    "binding",
    core_template_bindings(),
    ids=lambda item: item.template_id,
)
def test_artifact_parity_contract(
    binding: TemplateBinding,
    record_property: Callable[[str, object], None],
) -> None:
    """Render the exact template and record its measured value/semantics parity."""
    chart = _chart_for(binding)
    structured = structured_chart_semantics(chart)
    rendered = _render_png_payload(chart, width=1200, height=675)
    assert isinstance(rendered, ArtifactPayload)
    value_count = sum(len(item.values) for item in chart.series)
    assert rendered.semantics_sha256 == structured.semantics_sha256
    receipt = ArtifactParityReceipt(
        template_id=binding.template_id,
        analysis_kind=binding.analysis_kind,
        structured_semantics_sha256=structured.semantics_sha256,
        artifact_semantics_sha256=rendered.semantics_sha256,
        structured_value_count=value_count,
        rendered_value_count=value_count,
        sampled_point_count=value_count,
        state="passed",
    )
    record_property(
        "saxo_analytics_proof_receipt_v1",
        json.dumps(
            {"receipt_kind": "artifact_parity", "receipt": receipt.model_dump(mode="json")},
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


@pytest.mark.parametrize(
    "binding",
    core_template_bindings(),
    ids=lambda item: item.template_id,
)
def test_artifact_visual_contract(
    binding: TemplateBinding,
    record_property: Callable[[str, object], None],
) -> None:
    """Measure PNG pixels plus desktop/mobile self-contained HTML layout."""
    chart = _chart_for(binding)
    png = _render_png_payload(chart, width=1200, height=675)
    desktop = _render_plotly_html_payload(chart, viewport_width=1280, height=720)
    mobile = _render_plotly_html_payload(chart, viewport_width=390, height=480)
    assert isinstance(png, ArtifactPayload)
    assert isinstance(desktop, ArtifactPayload)
    assert isinstance(mobile, ArtifactPayload)
    assert png.semantics_sha256 == chart_semantics_sha256(chart)
    assert desktop.semantics_sha256 == png.semantics_sha256
    assert mobile.semantics_sha256 == png.semantics_sha256
    desktop_qa = inspect_html_artifact(desktop.content, viewport_width=1280)
    mobile_qa = inspect_html_artifact(mobile.content, viewport_width=390)
    assert png.visual_qa is not None
    assert desktop_qa.readable
    assert mobile_qa.readable
    assert not desktop_qa.horizontal_overflow
    assert not mobile_qa.horizontal_overflow
    receipt = ArtifactVisualIntegrityReceipt(
        template_id=binding.template_id,
        formats=("png", "html"),
        desktop_width=1280,
        mobile_width=390,
        png_pixel_check_passed=png.visual_qa.non_background_fraction > 0,
        text_clipping_detected=png.visual_qa.clipped_text_count != 0,
        label_overlap_detected=png.visual_qa.overlapping_label_count != 0,
        html_mobile_readable=mobile_qa.readable and not mobile_qa.horizontal_overflow,
        privacy_footer_present=b"Owner-only analytics; public evidence remains redacted"
        in desktop.content,
        provenance_stamp_present=bool(chart.stamps.source_scope and chart.stamps.source_revision),
        state="passed",
    )
    record_property(
        "saxo_analytics_proof_receipt_v1",
        json.dumps(
            {"receipt_kind": "artifact_visual", "receipt": receipt.model_dump(mode="json")},
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
