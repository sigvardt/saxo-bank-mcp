# pyright: reportPrivateUsage=false
# ruff: noqa: E501, SLF001
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
import re
import tempfile
from contextlib import suppress
from io import StringIO
from pathlib import Path
from typing import Annotated, Final, Literal, Self

import duckdb
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
    ArtifactStamps,
    ChartSemantics,
    validate_artifact_text,
)
from saxo_bank_mcp.analytics_chart_semantics import (
    _bound_visible_stamp_lines as visible_stamp_lines,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_models import ContractName
from saxo_bank_mcp.analytics_render import (
    ArtifactBindingRegistry,
    ArtifactDelivery,
    ArtifactPayload,
    ArtifactRefusal,
    _bound_chart_semantics,
    _produce_and_deliver_bound,
    build_artifact_payload,
)
from saxo_bank_mcp.analytics_reports import (
    AnalysisReport,
    _render_report_html_payload,
    _render_report_pdf_payload,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore

type ExportValue = str | int | float | bool | None
type ExportValueType = Literal["string", "number", "integer", "boolean", "decimal"]
type TableExportFormat = Literal["csv", "parquet", "json", "html"]
type ChartExportFormat = Literal["png", "html"]
type ReportExportFormat = Literal["html", "pdf"]

_MAX_EXPORT_ROWS: Final = 5_000_000
_DECIMAL_PATTERN: Final = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_RESERVED_STAMP_COLUMNS: Final = (
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
_FORBIDDEN_RAW_KEYS: Final = frozenset(
    {
        "accountid",
        "accountkey",
        "accountnumber",
        "accountgroupid",
        "accountgroupkey",
        "accountgroupname",
        "appid",
        "appkey",
        "clientid",
        "clientkey",
        "displayname",
        "instrumentid",
        "orderid",
        "positionid",
        "tradeid",
        "transactionid",
        "userid",
        "userkey",
        "accesstoken",
        "refreshtoken",
        "previewtoken",
        "path",
        "url",
    },
)
_IDENTIFIER_KEY_PATTERN: Final = re.compile(
    r"(?:account|client|order|position|user|trade|transaction|application|app|instrument)"
    r"[a-z0-9]*(?:id|identifier|key|number|name|ref|reference)[a-z0-9]*$",
)
_PARQUET_TYPES: Final = {
    "string": "VARCHAR",
    "number": "DOUBLE",
    "integer": "BIGINT",
    "boolean": "BOOLEAN",
    "decimal": "VARCHAR",
}


def _safe_export_label(value: str) -> str:
    return validate_artifact_text(value)


type ExportLabel = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=240),
    AfterValidator(_safe_export_label),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ExportColumn(_StrictModel):
    key: ContractName
    label: ExportLabel
    value_type: ExportValueType
    values: tuple[ExportValue, ...] = Field(min_length=1, max_length=_MAX_EXPORT_ROWS)

    @model_validator(mode="after")
    def validate_column(self) -> Self:
        normalized_key = re.sub(r"[^a-z0-9]", "", self.key.casefold())
        if (
            self.key in _RESERVED_STAMP_COLUMNS
            or normalized_key in _FORBIDDEN_RAW_KEYS
            or _IDENTIFIER_KEY_PATTERN.fullmatch(normalized_key) is not None
        ):
            raise ValueError("export column key is reserved or contains a raw broker field")
        for value in self.values:
            _validate_export_value(value, self.value_type)
        return self


class ExportTable(_StrictModel):
    schema_version: Literal["1"] = "1"
    title: ExportLabel
    analysis_kind: ContractName
    columns: tuple[ExportColumn, ...] = Field(min_length=1, max_length=100)
    stamps: ArtifactStamps

    @model_validator(mode="after")
    def validate_table(self) -> Self:
        keys = tuple(column.key for column in self.columns)
        if len(set(keys)) != len(keys):
            raise ValueError("export column keys must be unique")
        row_counts = {len(column.values) for column in self.columns}
        if len(row_counts) != 1:
            raise ValueError("export columns must contain the same row count")
        return self


class TableExportRequest(_StrictModel):
    request_kind: Literal["table"] = "table"
    table: ExportTable
    output_format: TableExportFormat


class ChartExportRequest(_StrictModel):
    request_kind: Literal["chart"] = "chart"
    semantics: ChartSemantics
    output_format: ChartExportFormat
    width: int = Field(default=1200, ge=320, le=2560)
    height: int = Field(default=675, ge=240, le=1600)


class ReportExportRequest(_StrictModel):
    request_kind: Literal["report"] = "report"
    report: AnalysisReport
    output_format: ReportExportFormat
    viewport_width: int = Field(default=1280, ge=320, le=2560)


class StoredTableExportRequest(_StrictModel):
    """Export request whose values are derived from a proof-replayed binding."""

    request_kind: Literal["stored_table"] = "stored_table"
    binding_id: str = Field(pattern=r"^ab_[0-9a-f]{32}$")
    output_format: TableExportFormat


class StoredReportExportRequest(_StrictModel):
    """Report request containing only a binding, registered template, and format."""

    request_kind: Literal["stored_report"] = "stored_report"
    binding_id: str = Field(pattern=r"^ab_[0-9a-f]{32}$")
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    output_format: ReportExportFormat
    viewport_width: int = Field(default=1280, ge=320, le=2560)


type ExportRequest = (
    TableExportRequest
    | ChartExportRequest
    | ReportExportRequest
    | StoredTableExportRequest
    | StoredReportExportRequest
)


def export_analysis(
    request: ExportRequest,
    *,
    config: AnalyticsConfig,
    store: AnalyticsStore | None = None,
    bindings: ArtifactBindingRegistry | None = None,
) -> ArtifactDelivery:
    """Export only exact values derived from a server-issued stored binding."""
    if (
        isinstance(request, (TableExportRequest, ChartExportRequest, ReportExportRequest))
        or store is None
        or bindings is None
    ):
        return _unbound_export_refusal()
    if isinstance(request, StoredTableExportRequest):

        def produce(stamps: ArtifactStamps) -> ArtifactPayload | ArtifactRefusal:
            try:
                result = bindings._result_for(request.binding_id)
                table = ExportTable(
                    title="Stored verified analytics metrics",
                    analysis_kind=result.analysis_kind,
                    columns=(
                        ExportColumn(
                            key="metric_id",
                            label="Metric",
                            value_type="string",
                            values=tuple(metric.metric_id for metric in result.metrics),
                        ),
                        ExportColumn(
                            key="metric_value",
                            label="Value",
                            value_type="number",
                            values=tuple(metric.value for metric in result.metrics),
                        ),
                        ExportColumn(
                            key="metric_unit",
                            label="Unit",
                            value_type="string",
                            values=tuple(metric.unit for metric in result.metrics),
                        ),
                        ExportColumn(
                            key="metric_currency",
                            label="Currency",
                            value_type="string",
                            values=tuple(metric.currency for metric in result.metrics),
                        ),
                    ),
                    stamps=stamps,
                )
            except ValueError:
                return _bound_export_refusal()
            return _export_table_payload(table, request.output_format, config=config)

        return _produce_and_deliver_bound(
            binding_id=request.binding_id,
            producer=produce,
            config=config,
            store=store,
            bindings=bindings,
        )

    def produce_report(stamps: ArtifactStamps) -> ArtifactPayload | ArtifactRefusal:
        semantics = _bound_chart_semantics(
            request.binding_id,
            request.template_id,
            stamps=stamps,
            bindings=bindings,
        )
        if isinstance(semantics, ArtifactRefusal):
            return semantics
        report = AnalysisReport(title="Stored verified analytics report", charts=(semantics,))
        if request.output_format == "html":
            return _render_report_html_payload(report, viewport_width=request.viewport_width)
        return _render_report_pdf_payload(report)

    return _produce_and_deliver_bound(
        binding_id=request.binding_id,
        producer=produce_report,
        config=config,
        store=store,
        bindings=bindings,
    )


def table_semantics_sha256(table: ExportTable) -> str:
    """Fingerprint exact typed table values and stamps in canonical order."""
    encoded = json.dumps(
        table.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _export_table_payload(  # pyright: ignore[reportUnusedFunction]
    table: ExportTable,
    output_format: TableExportFormat,
    *,
    config: AnalyticsConfig,
) -> ArtifactPayload | ArtifactRefusal:
    if output_format == "csv":
        content = _csv_bytes(table)
        media_type = "text/csv"
        extension = "csv"
    elif output_format == "json":
        content = _json_bytes(table)
        media_type = "application/json"
        extension = "json"
    elif output_format == "html":
        content = _html_bytes(table)
        media_type = "text/html"
        extension = "html"
    else:
        parquet = _parquet_bytes(table, config=config)
        if isinstance(parquet, ArtifactRefusal):
            return parquet
        content = parquet
        media_type = "application/vnd.apache.parquet"
        extension = "parquet"
    return build_artifact_payload(
        media_type=media_type,
        extension=extension,
        content=content,
        semantics_sha256=table_semantics_sha256(table),
        stamps=table.stamps,
    )


def _csv_bytes(table: ExportTable) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow((*_column_keys(table), *_RESERVED_STAMP_COLUMNS))
    stamp_values = _stamp_values(table.stamps)
    for row in _rows(table):
        writer.writerow((*(_csv_value(value) for value in row), *stamp_values))
    return output.getvalue().encode()


def _json_bytes(table: ExportTable) -> bytes:
    document = {
        "schema_version": "1",
        "title": table.title,
        "analysis_kind": table.analysis_kind,
        "columns": [
            {
                "key": column.key,
                "label": column.label,
                "value_type": column.value_type,
            }
            for column in table.columns
        ],
        "rows": [dict(zip(_column_keys(table), row, strict=True)) for row in _rows(table)],
        "stamps": table.stamps.model_dump(mode="json"),
        "privacy_footer": PRIVACY_FOOTER,
        "table_semantics_sha256": table_semantics_sha256(table),
    }
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _html_bytes(table: ExportTable) -> bytes:
    header = "".join(f"<th>{html.escape(column.label)}</th>" for column in table.columns)
    body_rows: list[str] = []
    for row in _rows(table):
        cells = "".join(
            f'<td data-canonical-value="{html.escape(_canonical_value(value))}">{html.escape(_canonical_value(value))}</td>'
            for value in row
        )
        body_rows.append(f"<tr>{cells}</tr>")
    stamps = "".join(
        f"<span>{html.escape(line)}</span>" for line in visible_stamp_lines(table.stamps)[:-1]
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; connect-src 'none'">
<title>{html.escape(table.title)}</title><style>
*{{box-sizing:border-box}}html,body{{margin:0;padding:0;background:#fff;color:#162033;font-family:Arial,sans-serif;font-size:14px;overflow-x:hidden}}
main{{max-width:100%;padding:16px}}h1{{font-size:22px;overflow-wrap:anywhere}}table{{width:100%;max-width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}}
th,td{{border:1px solid #d9e2ef;padding:5px;text-align:right;overflow-wrap:anywhere}}.stamps{{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;border-top:1px solid #d9e2ef;padding-top:8px;font-size:12px}}.privacy{{font-size:12px;font-weight:700;margin-top:8px}}
@media(max-width:600px){{main{{padding:8px}}table,.stamps,.privacy{{font-size:12px}}}}
</style></head><body><main data-table-semantics-sha256="{table_semantics_sha256(table)}"><h1>{html.escape(table.title)}</h1>
<table><thead><tr>{header}</tr></thead><tbody>{"".join(body_rows)}</tbody></table>
<footer><div class="stamps">{stamps}</div><div class="privacy">{PRIVACY_FOOTER}</div></footer></main></body></html>"""
    return document.encode()


def _parquet_bytes(
    table: ExportTable,
    *,
    config: AnalyticsConfig,
) -> bytes | ArtifactRefusal:
    descriptor: int | None = None
    temp_path = ""
    connection: duckdb.DuckDBPyConnection | None = None
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".task19-parquet-",
            suffix=".parquet",
            dir=config.paths.artifacts_dir,
        )
        os.close(descriptor)
        descriptor = None
        Path(temp_path).unlink()
        definitions = [
            f'"{column.key}" {_PARQUET_TYPES[column.value_type]}' for column in table.columns
        ]
        definitions.extend(f'"{key}" VARCHAR' for key in _RESERVED_STAMP_COLUMNS)
        connection = duckdb.connect(
            config={
                "allow_unsigned_extensions": "false",
                "autoinstall_known_extensions": "false",
            },
        )
        connection.execute(f"CREATE TABLE artifact_export ({', '.join(definitions)})")
        placeholders = ", ".join("?" for _ in definitions)
        stamp_values = _stamp_values(table.stamps)
        connection.executemany(
            f"INSERT INTO artifact_export VALUES ({placeholders})",  # noqa: S608
            [(*row, *stamp_values) for row in _rows(table)],
        )
        escaped_path = temp_path.replace("'", "''")
        connection.execute(
            f"COPY artifact_export TO '{escaped_path}' "
            "(FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE 122880)",
        )
        return Path(temp_path).read_bytes()
    except (duckdb.Error, OSError, ValueError):
        return ArtifactRefusal(
            reason_code="artifact_export_undefined",
            reason="the deterministic Parquet export could not be produced",
            next_action="request CSV or JSON, or restore owner-only artifact storage",
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if connection is not None:
            connection.close()
        if temp_path:
            with suppress(FileNotFoundError):
                Path(temp_path).unlink()


def _rows(table: ExportTable) -> tuple[tuple[ExportValue, ...], ...]:
    return tuple(zip(*(column.values for column in table.columns), strict=True))


def _column_keys(table: ExportTable) -> tuple[str, ...]:
    return tuple(column.key for column in table.columns)


def _stamp_values(stamps: ArtifactStamps) -> tuple[str, ...]:
    return (
        stamps.environment,
        stamps.data_cutoff.isoformat().replace("+00:00", "Z"),
        stamps.quote_delay,
        stamps.price_type,
        stamps.currency,
        stamps.adjustment_status,
        ",".join(stamps.warnings) if stamps.warnings else "none",
        stamps.analysis_id,
        stamps.source_scope,
        stamps.source_revision,
        stamps.visibility.value,
        PRIVACY_FOOTER,
    )


def _csv_value(value: ExportValue) -> str | int | float:
    if value is None:
        return ""
    if type(value) is bool:
        return "true" if value else "false"
    return value


def _canonical_value(value: ExportValue) -> str:
    if value is None:
        return "missing"
    if type(value) is bool:
        return "true" if value else "false"
    return str(value) if type(value) is not float else repr(value)


def _finite_float(value: float) -> bool:
    return math.isfinite(value)


def _validate_export_value(  # noqa: C901
    value: ExportValue,
    value_type: ExportValueType,
) -> None:
    if value is None:
        return
    if value_type == "string":
        if type(value) is not str:
            raise ValueError("string export columns require string values")
        validate_artifact_text(value)
    elif value_type == "number":
        if type(value) is not float or not _finite_float(value):
            raise ValueError("number export columns require finite float values")
    elif value_type == "integer":
        if type(value) is not int:
            raise ValueError("integer export columns require integer values")
    elif value_type == "boolean":
        if type(value) is not bool:
            raise ValueError("boolean export columns require boolean values")
    elif type(value) is not str or _DECIMAL_PATTERN.fullmatch(value) is None:
        raise ValueError("decimal export columns require canonical decimal strings")


def _unbound_export_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_analysis_unbound",
        reason="caller-composed export values cannot establish stored Saxo provenance",
        next_action="export from a server-issued stored analysis binding",
    )


def _bound_export_refusal() -> ArtifactRefusal:
    return ArtifactRefusal(
        reason_code="artifact_export_values_undefined",
        reason="the stored verified result cannot satisfy the exact typed export schema",
        next_action="request a supported export for the stored analysis metrics",
    )
