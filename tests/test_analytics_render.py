# pyright: reportPrivateUsage=false
# ruff: noqa: SLF001

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import cast

import matplotlib as mpl
import numpy as np
import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.text import Text
from PIL import Image
from pydantic import ValidationError

import saxo_bank_mcp.analytics_render as render_module
from saxo_bank_mcp.analytics_chart_semantics import (
    ArtifactStamps,
    ChartSemantics,
    TemplateBinding,
    chart_semantics_sha256,
    core_template_bindings,
    structured_chart_semantics,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_models import VisibilityMode
from saxo_bank_mcp.analytics_render import (
    ArtifactPayload,
    ArtifactRefusal,
    HtmlVisualQa,
    RenderRequest,
    _render_plotly_html_payload,
    _render_png_payload,
    build_artifact_payload,
    deliver_artifact,
    inspect_html_artifact,
    render_analysis,
    render_plotly_html,
    render_png,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.analytics_vision_requirements import (
    load_vision_coverage_requirements,
)

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "analytics" / "golden_artifacts"
_GOLDEN_PATH = _FIXTURE_ROOT / "golden_analysis.json"
_TEMPLATES_PATH = _FIXTURE_ROOT / "core_templates.json"
_PNG_WIDTH = 1200
_PNG_HEIGHT = 675
_ARTIFACT_LIMIT = 25 * 1024 * 1024
_CORE_TEMPLATE_COUNT = 20
_NONBLANK_FRACTION = 0.01
_MAX_EDGE_INK_FRACTION = 0.02
_MINIMUM_MOBILE_FONT_PX = 12
_COMPOSITE_AXIS_COUNT = 2
_FORBIDDEN_HTML = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"<script[^>]+\bsrc\s*=", re.IGNORECASE),
    re.compile(r"\bfetch\s*\(", re.IGNORECASE),
    re.compile(r"XMLHttpRequest|WebSocket|EventSource", re.IGNORECASE),
)


def _fixture_document() -> dict[str, object]:
    value = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _chart() -> ChartSemantics:
    payload = _fixture_document()["chart"]
    return ChartSemantics.model_validate_json(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def _chart_for(binding: TemplateBinding) -> ChartSemantics:
    payload = _chart().model_dump(mode="json")
    series = cast("list[dict[str, object]]", payload["series"])
    payload.update(
        {
            "analysis_kind": binding.analysis_kind,
            "template_id": binding.template_id,
            "title": f"Synthetic {binding.template_id.replace('_', ' ')}",
        },
    )
    if binding.chart_kind == "composite":
        pass
    elif binding.chart_kind == "scatter":
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
        dashboard_series = [
            {
                **item,
                "values": [cast("list[float]", item["values"])[-1]],
                "axis": "primary",
            }
            for item in (series[0], series[2])
        ]
        payload.update(
            {
                "labels": [cast("list[str]", payload["labels"])[-1]],
                "y_axis_title": "Index",
                "secondary_y_axis_title": None,
                "series": dashboard_series[:1]
                if binding.chart_kind == "card"
                else dashboard_series,
            },
        )
    else:
        payload.update(
            {
                "secondary_y_axis_title": None,
                "series": [series[0], series[2]],
            },
        )
    return ChartSemantics.model_validate_json(json.dumps(payload))


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})


def test_renderer_forces_headless_matplotlib_backend() -> None:
    assert mpl.get_backend().casefold() == "agg"


def test_core_template_inventory_matches_frozen_vision_and_golden_fixture() -> None:
    fixture = json.loads(_TEMPLATES_PATH.read_text(encoding="utf-8"))
    expected = tuple(
        (item["template_id"], item["analysis_kind"], item["chart_kind"])
        for item in fixture["templates"]
    )
    bindings = core_template_bindings()
    actual = tuple(
        (binding.template_id, binding.analysis_kind, binding.chart_kind) for binding in bindings
    )
    requirements = load_vision_coverage_requirements()

    assert len(bindings) == _CORE_TEMPLATE_COUNT
    assert actual == expected
    assert {(item.template_id, item.analysis_kind) for item in bindings} == {
        (item.template_id, item.analysis_kind) for item in requirements.required_artifact_owners
    }


def test_structured_semantics_preserve_exact_values_and_stable_fingerprints() -> None:
    chart = _chart()
    structured = structured_chart_semantics(chart)

    assert structured.template_id == chart.template_id
    assert structured.labels == chart.labels
    assert tuple(series.values for series in structured.series) == tuple(
        series.values for series in chart.series
    )
    assert structured.semantics_sha256 == chart_semantics_sha256(chart)
    assert structured == structured_chart_semantics(chart)
    assert len(set(structured.series_sha256s)) == len(chart.series)
    assert any("unverified caller-composed" in line for line in structured.visible_stamps)
    assert all("Saxo OpenAPI" not in line for line in structured.visible_stamps)


@pytest.mark.parametrize("binding", core_template_bindings(), ids=lambda item: item.template_id)
def test_every_core_template_renders_a_bounded_nonblank_png(binding: TemplateBinding) -> None:
    chart = _chart_for(binding)
    payload = _render_png_payload(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)

    assert isinstance(payload, ArtifactPayload)
    assert payload.media_type == "image/png"
    assert payload.width == _PNG_WIDTH
    assert payload.height == _PNG_HEIGHT
    assert payload.semantics_sha256 == chart_semantics_sha256(chart)
    assert payload.visual_qa is not None
    assert payload.visual_qa.non_background_fraction > _NONBLANK_FRACTION
    assert payload.visual_qa.footer_ink_fraction > 0
    assert payload.visual_qa.clipped_text_count == 0
    assert payload.visual_qa.overlapping_label_count == 0
    with Image.open(BytesIO(payload.content)) as image:
        assert image.size == (_PNG_WIDTH, _PNG_HEIGHT)
        assert image.convert("RGB").getextrema() != ((255, 255),) * 3


def test_png_is_deterministic_and_embeds_parity_and_required_stamps() -> None:
    chart = _chart()
    first = _render_png_payload(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)
    second = _render_png_payload(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)

    assert isinstance(first, ArtifactPayload)
    assert isinstance(second, ArtifactPayload)
    assert first.content == second.content
    assert first.sha256 == second.sha256
    with Image.open(BytesIO(first.content)) as image:
        metadata = image.info
        assert metadata["semantics_sha256"] == chart_semantics_sha256(chart)
        stamp_text = metadata["artifact_stamps"]
    for expected in (
        "Environment: SIM",
        "Cutoff: 2026-08-01T12:00:00Z",
        "Delay: delayed_15_minutes",
        "Price type: bar_close",
        "Reporting currency: DKK",
        "Adjustment: adjusted",
        "Warnings: synthetic_delayed_fixture",
        "Provenance: Saxo OpenAPI / fixture:t19",
        "Visibility: inline_private",
        "Owner-only analytics; public evidence remains redacted",
    ):
        assert expected in stamp_text


def test_long_labels_remain_inside_the_canvas_without_overlap() -> None:
    chart = _chart().model_copy(
        update={
            "labels": tuple(
                f"Synthetic deliberately long period label number {index} for clipping proof"
                for index in range(4)
            ),
            "series": tuple(
                series.model_copy(
                    update={
                        "name": f"Synthetic deliberately long series label {index}",
                    },
                )
                for index, series in enumerate(_chart().series)
            ),
        },
    )
    payload = _render_png_payload(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)

    assert isinstance(payload, ArtifactPayload)
    assert payload.visual_qa is not None
    assert payload.visual_qa.clipped_text_count == 0
    assert payload.visual_qa.overlapping_label_count == 0
    assert payload.visual_qa.edge_ink_fraction < _MAX_EDGE_INK_FRACTION


def test_all_missing_chart_refuses_instead_of_returning_a_blank_artifact() -> None:
    chart = _chart()
    missing = chart.model_copy(
        update={
            "series": tuple(
                series.model_copy(update={"values": (None,) * len(chart.labels)})
                for series in chart.series
            ),
        },
    )

    result = _render_png_payload(missing, width=_PNG_WIDTH, height=_PNG_HEIGHT)

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_has_no_renderable_values"


def test_plotly_html_is_self_contained_sanitized_deterministic_and_responsive() -> None:
    chart = _chart()
    desktop = _render_plotly_html_payload(chart, viewport_width=1280, height=720)
    mobile = _render_plotly_html_payload(chart, viewport_width=390, height=640)
    desktop_again = _render_plotly_html_payload(chart, viewport_width=1280, height=720)

    assert isinstance(desktop, ArtifactPayload)
    assert isinstance(mobile, ArtifactPayload)
    assert isinstance(desktop_again, ArtifactPayload)
    assert desktop.content == desktop_again.content
    desktop_text = desktop.content.decode("utf-8")
    mobile_text = mobile.content.decode("utf-8")
    for pattern in _FORBIDDEN_HTML:
        assert pattern.search(desktop_text) is None
        assert pattern.search(mobile_text) is None
    assert 'data-viewport-width="1280"' in desktop_text
    assert 'data-viewport-width="390"' in mobile_text
    assert "plotly-runtime-gzip" in desktop_text
    assert chart_semantics_sha256(chart) in desktop_text
    assert "-1.4634146341" in desktop_text
    assert "Owner-only analytics; public evidence remains redacted" in desktop_text

    desktop_check = inspect_html_artifact(desktop.content, viewport_width=1280)
    mobile_check = inspect_html_artifact(mobile.content, viewport_width=390)
    assert desktop_check.readable
    assert mobile_check.readable
    assert desktop_check.external_url_count == 0
    assert mobile_check.external_url_count == 0
    assert not desktop_check.horizontal_overflow
    assert not mobile_check.horizontal_overflow
    assert mobile_check.minimum_font_size_px >= _MINIMUM_MOBILE_FONT_PX


@pytest.mark.parametrize(
    ("field", "unsafe"),
    [
        ("title", "Unsafe https://example.invalid/chart"),
        ("subtitle", str(Path("/") / "Volumes" / "private" / "account.json")),
        ("x_axis_title", "<script>unsafe()</script>"),
        ("y_axis_title", "fetch('/private')"),
    ],
)
def test_chart_semantics_reject_network_script_and_private_path_text(
    field: str,
    unsafe: str,
) -> None:
    payload = _chart().model_dump(mode="json")
    payload[field] = unsafe

    with pytest.raises(ValidationError):
        ChartSemantics.model_validate_json(json.dumps(payload))


def test_chart_semantics_reject_raw_broker_identifier_fields() -> None:
    payload = _chart().model_dump(mode="json")
    account_field = "Account" + "Key"
    payload["series"][0]["name"] = f"{account_field}=synthetic-raw-broker-value"

    with pytest.raises(ValidationError):
        ChartSemantics.model_validate_json(json.dumps(payload))


def test_unbound_artifact_delivery_refuses_at_exact_25_mib_boundary(
    tmp_path: Path,
) -> None:
    chart = _chart()
    local_stamps = chart.stamps.model_copy(
        update={"visibility": VisibilityMode.LOCAL_RESOURCE_LINK},
    )
    at_limit = build_artifact_payload(
        media_type="application/octet-stream",
        extension="bin",
        content=b"x" * _ARTIFACT_LIMIT,
        semantics_sha256=chart_semantics_sha256(chart),
        stamps=local_stamps,
    )
    over_limit = build_artifact_payload(
        media_type="application/octet-stream",
        extension="bin",
        content=b"x" * (_ARTIFACT_LIMIT + 1),
        semantics_sha256=chart_semantics_sha256(chart),
        stamps=local_stamps,
    )
    config = _config(tmp_path)

    store = AnalyticsStore.open(config)
    try:
        at_limit_result = deliver_artifact(at_limit, config=config, store=store)
        over_limit_result = deliver_artifact(over_limit, config=config, store=store)
    finally:
        store.close()

    assert isinstance(at_limit_result, ArtifactRefusal)
    assert at_limit_result.reason_code == "artifact_analysis_unbound"
    assert isinstance(over_limit_result, ArtifactRefusal)
    assert over_limit_result.reason_code == "artifact_analysis_unbound"
    assert tuple(config.paths.artifacts_dir.iterdir()) == ()


def test_live_inline_private_without_stored_binding_is_refused(
    tmp_path: Path,
) -> None:
    chart = _chart()
    stamps = ArtifactStamps.model_validate_json(
        json.dumps({**chart.stamps.model_dump(mode="json"), "environment": "LIVE"}),
    )
    payload = build_artifact_payload(
        media_type="text/plain",
        extension="txt",
        content=b"synthetic private artifact",
        semantics_sha256=chart_semantics_sha256(chart),
        stamps=stamps,
    )

    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        result = deliver_artifact(payload, config=config, store=store)
    finally:
        store.close()

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_analysis_unbound"


def test_render_analysis_dispatches_png_without_mutating_semantics(tmp_path: Path) -> None:
    chart = _chart()
    serialized_before = chart.model_dump_json()

    result = render_analysis(
        RenderRequest(
            semantics=chart,
            output_format="png",
            width=_PNG_WIDTH,
            height=_PNG_HEIGHT,
        ),
        config=_config(tmp_path),
    )

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_analysis_unbound"
    assert chart.model_dump_json() == serialized_before


def test_unbound_caller_chart_cannot_claim_saxo_provenance_or_host_trust(
    tmp_path: Path,
) -> None:
    chart = _chart()
    unbound = chart.model_copy(
        update={
            "stamps": chart.stamps.model_copy(
                update={"source_revision": "caller:unbound"},
            ),
        },
    )
    request = RenderRequest(
        semantics=unbound,
        output_format="png",
        width=_PNG_WIDTH,
        height=_PNG_HEIGHT,
    )

    result = render_analysis(request, config=_config(tmp_path))

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_analysis_unbound"
    assert render_png(unbound).reason_code == "artifact_analysis_unbound"
    assert render_plotly_html(unbound).reason_code == "artifact_analysis_unbound"
    with pytest.raises(ValidationError):
        RenderRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "trusted_local_host": True,
            },
        )


def test_template_cardinality_and_missing_values_cannot_be_silently_changed() -> None:
    base = _chart().model_dump(mode="json")
    waterfall = {
        **base,
        "template_id": "contribution_waterfall",
        "analysis_kind": "portfolio_attribution",
    }
    with pytest.raises(ValidationError, match="waterfall"):
        ChartSemantics.model_validate_json(json.dumps(waterfall))

    scatter = {
        **base,
        "template_id": "efficient_frontier",
        "analysis_kind": "portfolio_minimum_variance",
    }
    with pytest.raises(ValidationError, match="scatter"):
        ChartSemantics.model_validate_json(json.dumps(scatter))

    missing_waterfall = {
        **waterfall,
        "series": [
            {
                **base["series"][0],
                "values": [100.0, None, 101.0, 104.0],
            },
        ],
    }
    with pytest.raises(ValidationError, match="missing"):
        ChartSemantics.model_validate_json(json.dumps(missing_waterfall))

    for template_id, analysis_kind in (
        ("portfolio_tearsheet", "portfolio_performance"),
        ("pretrade_impact_card", "pretrade_impact"),
    ):
        dashboard_or_card = {
            **base,
            "template_id": template_id,
            "analysis_kind": analysis_kind,
            "labels": [base["labels"][-1]],
            "y_axis_title": "Index",
            "secondary_y_axis_title": None,
            "series": [
                {
                    **base["series"][0],
                    "values": [None],
                    "axis": "primary",
                }
            ],
        }
        with pytest.raises(ValidationError, match="missing"):
            ChartSemantics.model_validate_json(json.dumps(dashboard_or_card))


def test_dashboard_and_card_render_every_accepted_value_with_one_truthful_unit() -> None:
    base = _chart().model_dump(mode="json")
    dashboard = {
        **base,
        "template_id": "portfolio_tearsheet",
        "analysis_kind": "portfolio_performance",
        "secondary_y_axis_title": None,
    }
    with pytest.raises(ValidationError, match="one observation"):
        ChartSemantics.model_validate_json(json.dumps(dashboard))

    latest_series = [
        {
            **series,
            "values": [series["values"][-1]],
            "axis": "primary",
        }
        for series in base["series"][:2]
    ]
    mixed_dashboard = {
        **dashboard,
        "labels": [base["labels"][-1]],
        "series": latest_series,
    }
    with pytest.raises(ValidationError, match="unit"):
        ChartSemantics.model_validate_json(json.dumps(mixed_dashboard))

    multi_series_card = {
        **mixed_dashboard,
        "template_id": "pretrade_impact_card",
        "analysis_kind": "pretrade_impact",
        "series": [{**series, "unit": "index"} for series in latest_series],
    }
    with pytest.raises(ValidationError, match="exactly one series"):
        ChartSemantics.model_validate_json(json.dumps(multi_series_card))


def test_axis_title_must_identify_currency_instead_of_percent() -> None:
    base = _chart().model_dump(mode="json")
    misleading = {
        **base,
        "template_id": "relative_performance",
        "analysis_kind": "market_comparison",
        "y_axis_title": "Percent",
        "secondary_y_axis_title": None,
        "series": [
            {
                **base["series"][0],
                "unit": "currency",
                "axis": "primary",
            },
        ],
    }

    with pytest.raises(ValidationError, match="unit"):
        ChartSemantics.model_validate_json(json.dumps(misleading))


def test_composite_axes_have_honest_units_titles_and_one_complete_legend() -> None:
    raw_payload = _fixture_document()["chart"]
    assert isinstance(raw_payload, dict)
    payload = cast("dict[str, object]", raw_payload)
    corrected: dict[str, object] = {
        **payload,
        "y_axis_title": "Index",
        "secondary_y_axis_title": "Percent",
    }
    chart = ChartSemantics.model_validate_json(json.dumps(corrected))

    figure, canvas, _, _ = render_module._build_figure(
        chart,
        width=_PNG_WIDTH,
        height=_PNG_HEIGHT,
    )
    try:
        canvas.draw()
        assert len(figure.axes) == _COMPOSITE_AXIS_COUNT
        assert figure.axes[0].get_ylabel() == "Index"
        assert figure.axes[1].get_ylabel() == "Percent"
        legend = figure.axes[0].get_legend()
        assert legend is not None
        assert {text.get_text() for text in legend.get_texts()} == {
            series.name for series in chart.series
        }
    finally:
        figure.clear()

    plotly = render_module._plotly_figure(chart)
    layout = cast("dict[str, object]", plotly["layout"])
    assert cast("dict[str, object]", layout["yaxis"])["title"] == {"text": "Index"}
    assert cast("dict[str, object]", layout["yaxis2"])["title"] == {"text": "Percent"}

    mixed: dict[str, object] = corrected.copy()
    raw_series = corrected["series"]
    assert isinstance(raw_series, list)
    corrected_series = cast("list[dict[str, object]]", raw_series)
    mixed["series"] = [
        *corrected_series,
        {
            **corrected_series[0],
            "name": "Synthetic mixed primary unit",
            "unit": "currency",
        },
    ]
    with pytest.raises(ValidationError, match="unit"):
        ChartSemantics.model_validate_json(json.dumps(mixed))


@pytest.mark.parametrize(
    "unsafe",
    [
        str(
            Path("/")
            / "Volumes"
            / "ssd_1"
            / "codex"
            / "tmp"
            / "saxo-bank-mcp-analytics"
            / "agent-implementation"
            / "synthetic"
            / "report.txt"
        ),
        "Account" + " Number=synthetic-value",
        "eyJ" + "a" * 24 + "." + "b" * 24 + "." + "c" * 24,
    ],
)
def test_chart_text_rejects_linux_paths_identifier_variants_and_token_shapes(
    unsafe: str,
) -> None:
    payload = _chart().model_dump(mode="json")
    payload["title"] = unsafe

    with pytest.raises(ValidationError):
        ChartSemantics.model_validate_json(json.dumps(payload))


def test_png_delivery_is_blocked_when_actual_visual_qa_detects_clipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = render_module._pixel_and_text_qa

    def clipped(
        pixels: np.ndarray[tuple[int, int, int], np.dtype[np.uint8]],
        *,
        canvas: FigureCanvasAgg,
        bounded_texts: Sequence[Text],
        label_texts: Sequence[Text],
    ) -> render_module.VisualQa:
        result = original(
            pixels,
            canvas=canvas,
            bounded_texts=bounded_texts,
            label_texts=label_texts,
        )
        return result.model_copy(update={"clipped_text_count": 1})

    monkeypatch.setattr(render_module, "_pixel_and_text_qa", clipped)
    result = _render_png_payload(_chart(), width=_PNG_WIDTH, height=_PNG_HEIGHT)

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_visual_qa_failed"


def test_naturally_clipped_valid_axis_title_blocks_png_delivery() -> None:
    payload = _chart().model_dump(mode="json")
    payload["y_axis_title"] = "Index " + "extended-axis-title-" * 11
    chart = ChartSemantics.model_validate_json(json.dumps(payload))

    result = _render_png_payload(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_visual_qa_failed"


def test_html_delivery_is_blocked_when_mobile_artifact_qa_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(_content: bytes, *, viewport_width: int) -> HtmlVisualQa:
        return HtmlVisualQa(
            viewport_width=viewport_width,
            readable=False,
            horizontal_overflow=True,
            minimum_font_size_px=8,
            external_url_count=0,
        )

    monkeypatch.setattr(render_module, "inspect_html_artifact", unreadable)
    result = _render_plotly_html_payload(_chart(), viewport_width=390, height=640)

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_visual_qa_failed"


def test_mobile_html_qa_requires_actual_stacked_fallback_layout() -> None:
    rendered = _render_plotly_html_payload(_chart(), viewport_width=390, height=640)
    assert isinstance(rendered, ArtifactPayload)
    tampered = rendered.content.replace(b'data-mobile-layout="stacked"', b"")

    qa = inspect_html_artifact(tampered, viewport_width=390)

    assert qa.readable is False
    assert qa.layout_issue_count > 0
