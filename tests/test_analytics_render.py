from __future__ import annotations

import json
import re
import stat
from io import BytesIO
from pathlib import Path
from typing import cast

import matplotlib as mpl
import pytest
from PIL import Image
from pydantic import ValidationError

from saxo_bank_mcp.analytics_chart_semantics import (
    ArtifactStamps,
    ChartSemantics,
    TemplateBinding,
    chart_semantics_sha256,
    core_template_bindings,
    structured_chart_semantics,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_render import (
    ArtifactPayload,
    ArtifactRefusal,
    ArtifactResourceLink,
    InlineArtifact,
    RenderRequest,
    build_artifact_payload,
    deliver_artifact,
    inspect_html_artifact,
    render_analysis,
    render_plotly_html,
    render_png,
)
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
_OWNER_FILE_MODE = 0o600
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
    payload.update(
        {
            "analysis_kind": binding.analysis_kind,
            "template_id": binding.template_id,
            "title": f"Synthetic {binding.template_id.replace('_', ' ')}",
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


@pytest.mark.parametrize("binding", core_template_bindings(), ids=lambda item: item.template_id)
def test_every_core_template_renders_a_bounded_nonblank_png(binding: TemplateBinding) -> None:
    chart = _chart_for(binding)
    payload = render_png(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)

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
    first = render_png(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)
    second = render_png(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)

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
        "Currency: DKK",
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
    payload = render_png(chart, width=_PNG_WIDTH, height=_PNG_HEIGHT)

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

    result = render_png(missing, width=_PNG_WIDTH, height=_PNG_HEIGHT)

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_has_no_renderable_values"


def test_plotly_html_is_self_contained_sanitized_deterministic_and_responsive() -> None:
    chart = _chart()
    desktop = render_plotly_html(chart, viewport_width=1280, height=720)
    mobile = render_plotly_html(chart, viewport_width=390, height=640)
    desktop_again = render_plotly_html(chart, viewport_width=1280, height=720)

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
        ("subtitle", "/Volumes/private/account.json"),
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


def test_artifact_delivery_enforces_exact_25_mib_limit_and_owner_only_fallback(
    tmp_path: Path,
) -> None:
    chart = _chart()
    at_limit = build_artifact_payload(
        media_type="application/octet-stream",
        extension="bin",
        content=b"x" * _ARTIFACT_LIMIT,
        semantics_sha256=chart_semantics_sha256(chart),
        stamps=chart.stamps,
    )
    over_limit = build_artifact_payload(
        media_type="application/octet-stream",
        extension="bin",
        content=b"x" * (_ARTIFACT_LIMIT + 1),
        semantics_sha256=chart_semantics_sha256(chart),
        stamps=chart.stamps,
    )
    config = _config(tmp_path)

    direct = deliver_artifact(
        at_limit,
        config=config,
        trusted_local_host=True,
    )
    linked = deliver_artifact(
        over_limit,
        config=config,
        trusted_local_host=True,
    )

    assert isinstance(direct, InlineArtifact)
    assert direct.byte_count == _ARTIFACT_LIMIT
    assert isinstance(linked, ArtifactResourceLink)
    assert linked.reason_code == "artifact_return_limit"
    assert linked.owner_only
    assert linked.resource_uri.startswith("saxo-analytics://artifacts/ar_")
    dumped = linked.model_dump(mode="json")
    assert "path" not in dumped
    files = tuple(config.paths.artifacts_dir.iterdir())
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == _OWNER_FILE_MODE
    assert files[0].stat().st_size == _ARTIFACT_LIMIT + 1


def test_live_inline_private_without_host_proof_falls_back_to_owner_resource(
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

    result = deliver_artifact(
        payload,
        config=_config(tmp_path),
        trusted_local_host=False,
    )

    assert isinstance(result, ArtifactResourceLink)
    assert result.reason_code == "inline_private_not_enabled"


def test_render_analysis_dispatches_png_without_mutating_semantics(tmp_path: Path) -> None:
    chart = _chart()
    serialized_before = chart.model_dump_json()

    result = render_analysis(
        RenderRequest(
            semantics=chart,
            output_format="png",
            width=_PNG_WIDTH,
            height=_PNG_HEIGHT,
            trusted_local_host=True,
        ),
        config=_config(tmp_path),
    )

    assert isinstance(result, InlineArtifact)
    assert result.media_type == "image/png"
    assert chart.model_dump_json() == serialized_before
