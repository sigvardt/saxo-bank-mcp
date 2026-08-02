# pyright: reportPrivateUsage=false

from __future__ import annotations

import csv
import json
import re
from io import StringIO
from pathlib import Path
from typing import cast

import duckdb
import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_chart_semantics import ChartSemantics
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_export import (
    ChartExportRequest,
    ExportColumn,
    ExportTable,
    ReportExportRequest,
    TableExportFormat,
    TableExportRequest,
    _export_table_payload,
    export_analysis,
)
from saxo_bank_mcp.analytics_models import VisibilityMode
from saxo_bank_mcp.analytics_render import (
    ArtifactPayload,
    ArtifactRefusal,
    _render_plotly_html_payload,
    _render_png_payload,
)
from saxo_bank_mcp.analytics_reports import (
    AnalysisReport,
    _render_report_html_payload,
    _render_report_pdf_payload,
    render_report_html,
    render_report_pdf,
)

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "analytics" / "golden_artifacts"
_GOLDEN_PATH = _FIXTURE_ROOT / "golden_analysis.json"
_GOLDEN_CSV_PATH = _FIXTURE_ROOT / "golden_table.csv"
_FORBIDDEN_HTML = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"<script[^>]+\bsrc\s*=", re.IGNORECASE),
    re.compile(r"\bfetch\s*\(", re.IGNORECASE),
    re.compile(r"/Users/|/Volumes/|/private/|[A-Za-z]:\\", re.IGNORECASE),
)


def _fixture_document() -> dict[str, object]:
    value = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _chart() -> ChartSemantics:
    return ChartSemantics.model_validate_json(
        json.dumps(_fixture_document()["chart"], sort_keys=True, separators=(",", ":")),
    )


def _table() -> ExportTable:
    return ExportTable.model_validate_json(
        json.dumps(_fixture_document()["table"], sort_keys=True, separators=(",", ":")),
    )


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})


def _export_table(tmp_path: Path, output_format: TableExportFormat) -> ArtifactPayload:
    result = _export_table_payload(
        _table(),
        output_format,
        config=_config(tmp_path),
    )
    assert isinstance(result, ArtifactPayload)
    return result


def test_csv_export_matches_golden_and_repeats_every_required_stamp(tmp_path: Path) -> None:
    result = _export_table(tmp_path, "csv")
    text = result.content.decode("utf-8")

    assert result.media_type == "text/csv"
    assert text == _GOLDEN_CSV_PATH.read_text(encoding="utf-8")
    rows = tuple(csv.DictReader(StringIO(text)))
    assert [row["return_ratio"] for row in rows] == ["0.025", "-0.014634146341"]
    assert all(row["environment"] == "SIM" for row in rows)
    assert all(row["currency"] == "DKK" for row in rows)
    assert all(row["source_scope"] == "saxo_openapi" for row in rows)
    assert all(row["privacy_footer"].startswith("Owner-only analytics") for row in rows)


def test_json_export_preserves_typed_values_and_complete_stamp_envelope(tmp_path: Path) -> None:
    first = _export_table(tmp_path, "json")
    second = _export_table(tmp_path, "json")
    document = json.loads(first.content)

    assert first.content == second.content
    assert document["rows"] == [
        {
            "complete": True,
            "exact_fee": "1.25",
            "observation_count": 21,
            "period": "2026-01",
            "return_ratio": 0.025,
        },
        {
            "complete": False,
            "exact_fee": "0.00",
            "observation_count": 20,
            "period": "2026-02",
            "return_ratio": -0.014634146341,
        },
    ]
    assert document["stamps"]["environment"] == "SIM"
    assert document["stamps"]["data_cutoff"] == "2026-08-01T12:00:00Z"
    assert document["stamps"]["quote_delay"] == "delayed_15_minutes"
    assert document["stamps"]["price_type"] == "bar_close"
    assert document["stamps"]["currency"] == "DKK"
    assert document["stamps"]["adjustment_status"] == "adjusted"
    assert document["stamps"]["warnings"] == ["synthetic_delayed_fixture"]
    assert document["stamps"]["source_scope"] == "saxo_openapi"
    assert document["stamps"]["visibility"] == "inline_private"
    assert document["privacy_footer"].startswith("Owner-only analytics")


def test_html_table_export_is_sanitized_readable_and_value_exact(tmp_path: Path) -> None:
    result = _export_table(tmp_path, "html")
    text = result.content.decode("utf-8")

    for pattern in _FORBIDDEN_HTML:
        assert pattern.search(text) is None
    assert "<table" in text
    assert 'data-canonical-value="-0.014634146341"' in text
    assert "Environment: SIM" in text
    assert "Cutoff: 2026-08-01T12:00:00Z" in text
    assert "Delay: delayed_15_minutes" in text
    assert "Price type: bar_close" in text
    assert "Currency: DKK" in text
    assert "Adjustment: adjusted" in text
    assert "Warnings: synthetic_delayed_fixture" in text
    assert "Provenance: Saxo OpenAPI / fixture:t19" in text
    assert "Visibility: inline_private" in text
    assert "Owner-only analytics; public evidence remains redacted" in text


def test_parquet_export_is_deterministic_and_preserves_schema_values_and_stamps(
    tmp_path: Path,
) -> None:
    first = _export_table(tmp_path, "parquet")
    second = _export_table(tmp_path, "parquet")
    parquet_path = tmp_path / "golden.parquet"
    parquet_path.write_bytes(first.content)
    parquet_path.chmod(0o600)

    assert first.content == second.content
    assert first.content[:4] == b"PAR1"
    connection = duckdb.connect()
    try:
        description = connection.execute(
            "SELECT * FROM read_parquet(?)",
            (str(parquet_path),),
        ).description
        rows = connection.fetchall()
    finally:
        connection.close()
    assert tuple(column[0] for column in description) == (
        "period",
        "return_ratio",
        "observation_count",
        "complete",
        "exact_fee",
        "environment",
        "data_cutoff",
        "quote_delay",
        "price_type",
        "currency",
        "adjustment_status",
        "warnings",
        "analysis_id",
        "source_scope",
        "source_revision",
        "visibility",
        "privacy_footer",
    )
    assert rows[0][:5] == ("2026-01", 0.025, 21, True, "1.25")
    assert rows[1][:5] == ("2026-02", -0.014634146341, 20, False, "0.00")
    assert rows[0][5:11] == (
        "SIM",
        "2026-08-01T12:00:00Z",
        "delayed_15_minutes",
        "bar_close",
        "DKK",
        "adjusted",
    )


@pytest.mark.parametrize(
    ("export_request", "media_type", "magic"),
    [
        pytest.param(
            ChartExportRequest(
                semantics=_chart(),
                output_format="png",
                width=1200,
                height=675,
            ),
            "image/png",
            b"\x89PNG\r\n\x1a\n",
            id="png",
        ),
        pytest.param(
            ReportExportRequest(
                report=AnalysisReport(title="Synthetic report", charts=(_chart(),)),
                output_format="pdf",
                viewport_width=1200,
            ),
            "application/pdf",
            b"%PDF",
            id="pdf",
        ),
        pytest.param(
            ReportExportRequest(
                report=AnalysisReport(title="Synthetic report", charts=(_chart(),)),
                output_format="html",
                viewport_width=390,
            ),
            "text/html",
            b"<!doctype html>",
            id="report-html",
        ),
    ],
)
def test_png_html_and_pdf_export_dispatch_is_deterministic(
    tmp_path: Path,
    export_request: ChartExportRequest | ReportExportRequest,
    media_type: str,
    magic: bytes,
) -> None:
    del tmp_path
    if isinstance(export_request, ChartExportRequest):
        if export_request.output_format == "png":
            first = _render_png_payload(
                export_request.semantics,
                width=export_request.width,
                height=export_request.height,
            )
            second = _render_png_payload(
                export_request.semantics,
                width=export_request.width,
                height=export_request.height,
            )
        else:
            first = _render_plotly_html_payload(
                export_request.semantics,
                viewport_width=export_request.width,
                height=export_request.height,
            )
            second = _render_plotly_html_payload(
                export_request.semantics,
                viewport_width=export_request.width,
                height=export_request.height,
            )
    elif export_request.output_format == "html":
        first = _render_report_html_payload(
            export_request.report,
            viewport_width=export_request.viewport_width,
        )
        second = _render_report_html_payload(
            export_request.report,
            viewport_width=export_request.viewport_width,
        )
    else:
        first = _render_report_pdf_payload(export_request.report)
        second = _render_report_pdf_payload(export_request.report)

    assert isinstance(first, ArtifactPayload)
    assert isinstance(second, ArtifactPayload)
    assert first.media_type == media_type
    assert first.content.startswith(magic)
    assert first.content == second.content


def test_report_requires_one_source_linked_analysis_and_visibility() -> None:
    first = _chart()
    second_payload = first.model_dump(mode="json")
    second_payload["stamps"]["analysis_id"] = "an_00000000000040008000000000000092"
    second = ChartSemantics.model_validate_json(json.dumps(second_payload))

    with pytest.raises(ValidationError):
        AnalysisReport(title="Mismatched report", charts=(first, second))


def test_public_report_renderers_refuse_unbound_caller_charts() -> None:
    report = AnalysisReport(title="Synthetic report", charts=(_chart(),))

    assert render_report_html(report).reason_code == "artifact_analysis_unbound"
    assert render_report_pdf(report).reason_code == "artifact_analysis_unbound"


@pytest.mark.parametrize(
    ("stamp_field", "replacement"),
    [
        ("quote_delay", "real_time"),
        ("price_type", "midpoint"),
        ("currency", "EUR"),
        ("adjustment_status", "unadjusted"),
        ("warnings", ("different_warning",)),
    ],
)
def test_report_requires_complete_stamp_equality(
    stamp_field: str,
    replacement: object,
) -> None:
    first = _chart()
    second = first.model_copy(
        update={
            "stamps": first.stamps.model_copy(update={stamp_field: replacement}),
        },
    )

    with pytest.raises(ValidationError, match="stamp"):
        AnalysisReport(title="Mismatched report", charts=(first, second))


@pytest.mark.parametrize(
    ("field", "unsafe"),
    [
        ("label", str(Path("/") / "Users" / "private" / "report.csv")),
        ("label", "https://example.invalid/private"),
        ("key", "account_key"),
        ("key", "order_id"),
    ],
)
def test_export_schema_rejects_paths_network_requests_and_raw_broker_fields(
    field: str,
    unsafe: str,
) -> None:
    column = _table().columns[0]
    payload = column.model_dump(mode="json")
    payload[field] = unsafe

    with pytest.raises(ValidationError):
        ExportColumn.model_validate_json(json.dumps(payload))


def test_export_string_values_reject_private_paths_and_secret_material() -> None:
    column = _table().columns[0]
    payload = column.model_dump(mode="json")
    credential_name = "access" + "_token"
    payload["values"] = ["safe", f"{credential_name}=synthetic-secret-material"]

    with pytest.raises(ValidationError):
        ExportColumn.model_validate_json(json.dumps(payload))


def test_export_fields_reject_linux_paths_identifier_variants_and_token_shapes() -> None:
    column = _table().columns[0]
    unsafe_key = column.model_dump(mode="json")
    unsafe_key["key"] = "account_number"
    with pytest.raises(ValidationError):
        ExportColumn.model_validate_json(json.dumps(unsafe_key))

    unsafe_values = column.model_dump(mode="json")
    unsafe_values["values"] = [
        str(
            Path("/")
            / "Volumes"
            / "ssd_1"
            / "codex"
            / "tmp"
            / "saxo-bank-mcp-analytics"
            / "agent-implementation"
            / "synthetic"
            / "export.txt"
        ),
        "eyJ" + "d" * 24 + "." + "e" * 24 + "." + "f" * 24,
    ]
    with pytest.raises(ValidationError):
        ExportColumn.model_validate_json(json.dumps(unsafe_values))


@pytest.mark.parametrize(
    ("value_type", "values"),
    [
        ("string", ("synthetic",)),
        ("number", (1.0,)),
        ("integer", (1,)),
        ("boolean", (True,)),
        ("decimal", ("1",)),
    ],
)
def test_identifier_shaped_export_keys_are_refused_for_every_value_type(
    value_type: str,
    values: tuple[object, ...],
) -> None:
    payload = {
        "key": "client_identifier",
        "label": "Synthetic identifier",
        "value_type": value_type,
        "values": values,
    }

    with pytest.raises(ValidationError, match="raw broker field"):
        ExportColumn.model_validate_json(json.dumps(payload))


def test_unbound_caller_export_refuses_instead_of_claiming_saxo_provenance(
    tmp_path: Path,
) -> None:
    table = _table()
    unbound = table.model_copy(
        update={
            "stamps": table.stamps.model_copy(
                update={"source_revision": "caller:unbound"},
            ),
        },
    )

    result = export_analysis(
        TableExportRequest(table=unbound, output_format="json"),
        config=_config(tmp_path),
    )

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_analysis_unbound"


def test_export_columns_require_equal_rows_and_unique_keys() -> None:
    table = _table()
    short = table.columns[0].model_copy(update={"values": ("2026-01",)})

    with pytest.raises(ValidationError):
        ExportTable(
            title=table.title,
            analysis_kind=table.analysis_kind,
            columns=(short, *table.columns[1:]),
            stamps=table.stamps,
        )
    with pytest.raises(ValidationError):
        ExportTable(
            title=table.title,
            analysis_kind=table.analysis_kind,
            columns=(table.columns[0], table.columns[0]),
            stamps=table.stamps,
        )


def test_local_resource_link_export_still_refuses_unbound_caller_values(
    tmp_path: Path,
) -> None:
    table = _table()
    local_stamps = table.stamps.model_copy(
        update={"visibility": VisibilityMode.LOCAL_RESOURCE_LINK},
    )
    local_table = table.model_copy(update={"stamps": local_stamps})
    config = _config(tmp_path)

    result = export_analysis(
        TableExportRequest(table=local_table, output_format="json"),
        config=config,
    )

    assert isinstance(result, ArtifactRefusal)
    assert result.reason_code == "artifact_analysis_unbound"
    assert tuple(config.paths.artifacts_dir.iterdir()) == ()
