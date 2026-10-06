"""Exact stored observations remain usable through ordinary chart/report delivery."""

# pyright: reportPrivateUsage=false
# ruff: noqa: SLF001, PLR2004

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from fastmcp import Client
from mcp.types import BlobResourceContents
from pydantic import ValidationError
from test_analytics_production_runtime import _checked_release
from test_analytics_provenance import _analysis_result
from test_analytics_render import _chart
from test_mcp_analytics_tools import _state_env, _synced_chart_fixture

import saxo_bank_mcp.mcp_analytics_tools as analytics_tools
from saxo_bank_mcp.analytics_chart_semantics import ChartTemplateId
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_export import (
    _analysis_table_export,
    _html_bytes,
    _stored_report_table,
    _table_report_pdf,
)
from saxo_bank_mcp.analytics_models import (
    AnalysisCell,
    AnalysisResult,
    AnalysisRow,
    AnalysisTable,
    AnalysisWarning,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_release import load_production_registry
from saxo_bank_mcp.analytics_render import (
    ArtifactBindingRegistry,
    ArtifactPayload,
    StoredChartSelection,
    _build_figure,
    _compressed_sanitized_plotly_runtime,
    _render_png_payload,
    _stored_table_semantics,
    read_artifact,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreError
from saxo_bank_mcp.server import create_mcp_server


def _result(*, rows: int = 3) -> AnalysisResult:
    result = _analysis_result()
    return result.model_copy(
        update={
            "tables": (
                AnalysisTable(
                    table_id="observations",
                    title="Exact observed prices and returns",
                    rows=tuple(
                        AnalysisRow(
                            label="Stored observation",
                            at=result.as_of + timedelta(minutes=index),
                            cells=(
                                AnalysisCell(field="close", value=100.0 + index, currency="EUR"),
                                AnalysisCell(
                                    field="price_return", value=0.01 * index, unit="ratio"
                                ),
                                AnalysisCell(field="peer_return", value=0.02 * index, unit="ratio"),
                                AnalysisCell(
                                    field="missing_return",
                                    value=None if index == 1 else 0.01 * index,
                                    unit="ratio",
                                ),
                            ),
                        )
                        for index in range(rows)
                    ),
                ),
            ),
        }
    )


@pytest.mark.parametrize(
    ("template", "fields", "secondary"),
    [
        ("stored_table_line", ("price_return", "peer_return"), ()),
        ("stored_table_bar", ("price_return",), ()),
        ("stored_table_scatter", ("close", "price_return"), ()),
        ("stored_table_heatmap", ("price_return", "peer_return"), ()),
        ("stored_table_composite", ("close", "price_return"), ("price_return",)),
    ],
)
def test_all_registered_stored_shapes_preserve_exact_values_and_units(
    template: ChartTemplateId, fields: tuple[str, ...], secondary: tuple[str, ...]
) -> None:
    result = _result()
    semantics = _stored_table_semantics(
        result,
        StoredChartSelection(table_id="observations", fields=fields, secondary_fields=secondary),
        template,
        _chart().stamps,
    )
    expected = {"close": (100.0, 101.0, 102.0), "price_return": (0.0, 0.01, 0.02)}
    for field, series in zip(fields, semantics.series, strict=True):
        if field in expected:
            assert series.values == expected[field]
        assert series.unit == ("currency_eur" if field == "close" else "ratio")
    if template == "stored_table_scatter":
        assert "currency eur" in semantics.x_axis_title
        assert "ratio" in semantics.y_axis_title
    if secondary:
        assert "currency eur" in semantics.y_axis_title
        assert semantics.secondary_y_axis_title == "Value (ratio)"
    png = _render_png_payload(semantics, width=1200, height=675)
    assert isinstance(png, ArtifactPayload), png


def test_missing_observations_remain_missing_without_row_drops() -> None:
    semantics = _stored_table_semantics(
        _result(),
        StoredChartSelection(table_id="observations", fields=("missing_return",)),
        "stored_table_line",
        _chart().stamps,
    )
    assert semantics.series[0].values == (0.0, None, 0.02)
    assert len(semantics.labels) == 3


def test_visible_dates_and_instrument_handles_identify_exact_stored_rows() -> None:
    result = _result()
    selection = StoredChartSelection(table_id="observations", fields=("price_return",))
    semantics = _stored_table_semantics(result, selection, "stored_table_line", _chart().stamps)
    figure, canvas, _, _ = _build_figure(semantics, width=1200, height=675)
    canvas.draw()
    visible = tuple(label.get_text() for label in figure.axes[0].get_xticklabels())
    for row, label in zip(result.tables[0].rows, visible, strict=True):
        assert row.at is not None
        assert label == f"{row.at.date().isoformat()}\n{row.at.strftime('%H:%M:%S')}"
    assert len(set(visible)) == len(visible)
    figure.clear()

    rows = tuple(
        row.model_copy(update={"at": None, "instrument_handle": f"ih_{index:032x}"})
        for index, row in enumerate(result.tables[0].rows)
    )
    result = result.model_copy(
        update={"tables": (result.tables[0].model_copy(update={"rows": rows}),)}
    )
    semantics = _stored_table_semantics(result, selection, "stored_table_line", _chart().stamps)
    assert semantics.labels == tuple(row.instrument_handle for row in rows)
    figure, canvas, _, _ = _build_figure(semantics, width=1200, height=675)
    canvas.draw()
    visible = tuple(
        label.get_text().replace("\n", "") for label in figure.axes[0].get_xticklabels()
    )
    assert visible == semantics.labels
    assert figure.axes[0].get_xlabel() == "Stored instrument handle"
    figure.clear()


def test_heatmap_key_and_cell_values_use_exact_stored_units_and_values() -> None:
    semantics = _stored_table_semantics(
        _result(),
        StoredChartSelection(table_id="observations", fields=("price_return", "peer_return")),
        "stored_table_heatmap",
        _chart().stamps,
    )
    figure, canvas, _, _ = _build_figure(semantics, width=1200, height=675)
    canvas.draw()
    assert figure.axes[0].get_ylabel() == "Stored field"
    assert figure.axes[1].get_ylabel() == "Value (ratio)"
    assert tuple(text.get_text() for text in figure.axes[0].texts) == (
        "0.0",
        "0.01",
        "0.02",
        "0.0",
        "0.02",
        "0.04",
    )
    figure.clear()


def test_overlapping_composite_paths_remain_visibly_distinguishable() -> None:
    semantics = _stored_table_semantics(
        _result(),
        StoredChartSelection(
            table_id="observations",
            fields=("close", "price_return"),
            secondary_fields=("price_return",),
        ),
        "stored_table_composite",
        _chart().stamps,
    )
    figure, canvas, _, _ = _build_figure(semantics, width=1200, height=675)
    canvas.draw()
    primary, secondary = figure.axes[0].lines[0], figure.axes[1].lines[0]
    np.testing.assert_array_equal(primary.get_ydata(), (100.0, 101.0, 102.0))
    np.testing.assert_array_equal(secondary.get_ydata(), (0.0, 0.01, 0.02))
    assert primary.get_linestyle() != secondary.get_linestyle()
    assert primary.get_marker() != secondary.get_marker()
    assert secondary.get_markerfacecolor() == "none"
    figure.clear()


def test_sanitized_interactive_runtime_is_valid_javascript() -> None:
    """Nested namespace strings must remain executable after network sanitization."""
    node = shutil.which("node")
    assert node is not None, "Node is part of the project CI verification toolchain"
    runtime = gzip.decompress(base64.b64decode(_compressed_sanitized_plotly_runtime()))
    parsed = subprocess.run(
        (node, "--check"), input=runtime.decode(), text=True, capture_output=True, check=False
    )
    assert parsed.returncode == 0, parsed.stderr[-600:]


@pytest.mark.parametrize(
    "cells",
    [
        (AnalysisCell(field="price_return", value=0.1),),
        (AnalysisCell(field="price_return", value=True, unit="ratio"),),
        (AnalysisCell(field="price_return", value="missing", unit="ratio"),),
        (
            AnalysisCell(field="price_return", value=0.1, unit="ratio"),
            AnalysisCell(field="price_return", value=0.2, unit="ratio"),
        ),
    ],
)
def test_chart_refuses_unknown_units_text_booleans_and_duplicate_fields(
    cells: tuple[AnalysisCell, ...],
) -> None:
    # A canonical metric with the same field ID cannot fill absent table metadata.
    result = _result().model_copy(
        update={
            "tables": (
                AnalysisTable(
                    table_id="observations",
                    title="Ambiguous observations",
                    rows=(AnalysisRow(label="Observed", cells=cells),),
                ),
            )
        }
    )
    with pytest.raises(ValueError, match=r"unit|numeric|ambiguous"):
        _stored_table_semantics(
            result,
            StoredChartSelection(table_id="observations", fields=("price_return",)),
            "stored_table_line",
            _chart().stamps,
        )


def test_chart_refuses_mixed_units_and_large_exact_observation_arrays() -> None:
    with pytest.raises(ValueError, match="primary-axis"):
        _stored_table_semantics(
            _result(),
            StoredChartSelection(table_id="observations", fields=("close", "price_return")),
            "stored_table_line",
            _chart().stamps,
        )
    with pytest.raises(ValueError, match="500"):
        _stored_table_semantics(
            _result(rows=501),
            StoredChartSelection(table_id="observations", fields=("price_return",)),
            "stored_table_line",
            _chart().stamps,
        )
    with pytest.raises(ValidationError):
        StoredChartSelection(table_id="observations", fields=("price_return", "price_return"))


def test_complete_report_retains_tables_exact_nulls_scope_and_warning_detail() -> None:
    result = _result().model_copy(
        update={
            "metrics": (),
            "unavailable_fields": ("future_returns",),
            "warnings": (
                AnalysisWarning(
                    code="coverage_gap", message="One observed gap", next_action="Inspect coverage"
                ),
            ),
        }
    )
    table = _stored_report_table(result)
    cells = {(row.label, cell.field): cell for row in table.rows for cell in row.cells}
    assert cells[("Warning", "warning_message")].value == "One observed gap"
    assert cells[("Warning", "warning_next_action")].value == "Inspect coverage"
    assert cells[("Result scope", "model_distribution_only")].value is False
    exported = _analysis_table_export(table, result.analysis_kind, _chart().stamps)
    document = _html_bytes(exported).decode()
    assert "future_returns" in document
    assert "missing" in document
    assert "EUR" in document
    assert "ratio" in document
    assert "data-field-label=" in document
    assert "td::before" in document
    pdf = _table_report_pdf(exported)
    assert isinstance(pdf, ArtifactPayload), pdf
    assert pdf.content.startswith(b"%PDF-")
    assert pdf.stamps == _chart().stamps
    assert pdf == _table_report_pdf(exported)


@pytest.mark.parametrize("kind", ["instrument_price_return", "market_comparison"])
def test_normal_live_mcp_stored_charts_and_complete_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = analytics_tools._analytics_config()
    handle, dataset_id, _, executor = asyncio.run(_synced_chart_fixture(config))
    _checked_release(monkeypatch)

    async def call() -> None:
        async with Client(create_mcp_server()) as client:
            request: dict[str, object] = {"analysis_kind": kind, "dataset_ids": [dataset_id]}
            if kind == "instrument_price_return":
                request.update({"instrument_handles": [handle], "rolling_window": 2})
            calculated = await client.call_tool(
                "saxo_analyze_instruments"
                if kind.startswith("instrument")
                else "saxo_analyze_market",
                {"request": request},
            )
            assert calculated.structured_content is not None
            response = calculated.structured_content
            assert response["status"] in {"verified", "degraded"}, response
            result = AnalysisResult.model_validate_json(json.dumps(response["result"]))
            if kind == "market_comparison":
                assert not result.metrics
            selected = next(
                table
                for table in result.tables
                if table.table_id
                == ("price_periods" if kind.startswith("instrument") else "market_comparisons")
            )
            field = "price_return"
            assert any(cell.field == field for cell in selected.rows[0].cells)
            for template in ("stored_table_line", "stored_table_bar", "stored_table_heatmap"):
                rendered = await client.call_tool(
                    "saxo_render_analysis",
                    {
                        "analysis_id": result.analysis_id,
                        "template_id": template,
                        "selection": {"table_id": selected.table_id, "fields": [field]},
                    },
                )
                assert rendered.structured_content is not None
                assert rendered.structured_content["status"] == "inline", (
                    rendered.structured_content
                )
                assert rendered.content[0].type == "image"
                assert base64.b64decode(rendered.content[0].data).startswith(b"\x89PNG")
            for output_format in ("html", "pdf"):
                exported = await client.call_tool(
                    "saxo_export_analysis",
                    {
                        "analysis_id": result.analysis_id,
                        "export_kind": "report",
                        "output_format": output_format,
                    },
                )
                assert exported.structured_content is not None
                assert exported.structured_content["status"] == "inline", (
                    exported.structured_content
                )
                assert exported.content[0].type == "resource"
                resource = exported.content[0].resource
                assert isinstance(resource, BlobResourceContents)
                content = base64.b64decode(resource.blob)
                assert content.startswith(
                    b"<!doctype html>" if output_format == "html" else b"%PDF-"
                )

            resource_uri = _assert_owned_resource_integrity(result, config)
            contents = await client.read_resource(resource_uri)
            assert len(contents) == 1
            assert isinstance(contents[0], BlobResourceContents)
            assert contents[0].mimeType == "application/pdf"
            assert base64.b64decode(contents[0].blob).startswith(b"%PDF-")

    asyncio.run(call())
    assert executor.call_count == 1


def _assert_owned_resource_integrity(result: AnalysisResult, config: AnalyticsConfig) -> str:
    registry = load_production_registry(config)
    bindings = ArtifactBindingRegistry(config=config, proof_registry=registry)
    issued = bindings.issue(result.analysis_id)
    stamps = bindings._stamps_for(issued.binding_id, visibility=VisibilityMode.LOCAL_RESOURCE_LINK)
    payload = _table_report_pdf(
        _analysis_table_export(_stored_report_table(result), result.analysis_kind, stamps)
    )
    assert isinstance(payload, ArtifactPayload), payload
    store = AnalyticsStore.open(config)
    try:
        artifact = store.put_owned_artifact(
            analysis_id=result.analysis_id,
            media_type=payload.media_type,
            extension=payload.extension,
            content=payload.content,
            description="Exact stored report resource",
            analysis_binding=bindings._analysis_binding_for(issued.binding_id),
        )
        content, mime = read_artifact(
            artifact.artifact_id, config=config, store=store, proof_registry=registry
        )
        assert mime == "application/pdf"
        assert content == payload.content
        assert hashlib.sha256(content).hexdigest() == artifact.sha256
        path = config.paths.artifacts_dir / f"{artifact.artifact_id}.pdf"
        original = path.read_bytes()
        try:
            path.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
            with pytest.raises(StoreError, match="integrity"):
                read_artifact(
                    artifact.artifact_id, config=config, store=store, proof_registry=registry
                )
        finally:
            path.write_bytes(original)
        with pytest.raises(StoreError, match="handle"):
            read_artifact("../private-file", config=config, store=store, proof_registry=registry)
        return f"saxo-analytics://artifacts/{artifact.artifact_id}"
    finally:
        store.close()
