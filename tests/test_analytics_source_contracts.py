from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_source_contracts import (
    SourceContract,
    SourceContractCatalog,
    SourceValueType,
    compare_source_schema,
    load_source_contract_catalog,
    source_contracts_by_id,
)
from saxo_bank_mcp.endpoint_registry import find_registered_endpoint

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CONTRACTS_PATH = _REPOSITORY_ROOT / "data/analytics/source_contracts.json"
_FIXTURE_ROOT = Path(__file__).parent / "fixtures/analytics/saxo_pages"
_REQUIRED_SOURCE_KINDS = {
    "balances",
    "bookings",
    "chart",
    "closed_positions",
    "corporate_actions",
    "costs",
    "exposure",
    "info_prices",
    "options_chain",
    "orders",
    "performance",
    "positions",
    "reference_instruments",
    "transactions",
}
_MAX_CONTRACT_RETRY_ATTEMPTS = 3
_OBJECT_ADAPTER = TypeAdapter(dict[str, object])


def _fixture(name: str) -> dict[str, Any]:
    return _OBJECT_ADAPTER.validate_json(
        (_FIXTURE_ROOT / name).read_text(encoding="utf-8"),
    )


def test_checked_in_contract_catalog_round_trips_exactly() -> None:
    checked_in = SourceContractCatalog.model_validate_json(
        _CONTRACTS_PATH.read_text(encoding="utf-8"),
        strict=True,
    )
    loaded = load_source_contract_catalog()

    assert loaded == checked_in
    assert loaded.schema_version == "1"
    assert len({contract.contract_id for contract in loaded.contracts}) == len(loaded.contracts)


def test_clean_wheel_loads_contracts_and_registry_outside_the_repository(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel"
    uv_path = shutil.which("uv")
    assert uv_path is not None
    build = subprocess.run(
        [
            uv_path,
            "build",
            "--offline",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        installed_files = set(archive.namelist())
    assert "saxo_bank_mcp/_analytics_source_contracts/source_contracts.json" in installed_files
    assert "saxo_bank_mcp/_endpoint_registry/openapi_inventory.json" in installed_files

    script = """
from saxo_bank_mcp.analytics_source_contracts import load_source_contract_catalog

catalog = load_source_contract_catalog()
print(f"{catalog.schema_version}:{len(catalog.contracts)}")
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(wheel)
    loaded = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == "1:18"


def test_contracts_cover_every_required_source_family_with_registered_reads() -> None:
    contracts = load_source_contract_catalog().contracts

    assert {contract.source_kind for contract in contracts} >= _REQUIRED_SOURCE_KINDS
    for contract in contracts:
        registered = find_registered_endpoint(contract.method, contract.path_template)
        assert registered is not None
        assert registered.operation.operation_id == contract.operation_id
        assert registered.operation.method == "GET"
        assert registered.operation.status == "implemented"
        assert registered.operation.read_write_class == "read"
        assert not contract.path_template.startswith(("http://", "https://", "//"))
        assert contract.retry_attempts <= _MAX_CONTRACT_RETRY_ATTEMPTS


def test_pagination_cursor_schemas_are_frozen_per_source_contract() -> None:
    contracts = source_contracts_by_id()
    chart_pagination = contracts["chart_v3"].pagination
    positions_pagination = contracts["positions_v1"].pagination

    assert chart_pagination is not None
    assert [
        (field.name, field.value_type.value, field.minimum, field.maximum)
        for field in chart_pagination.cursor_fields
    ] == [("$skiptoken", "token", None, None)]
    assert chart_pagination.valid_combinations == (("$skiptoken",),)

    assert positions_pagination is not None
    assert [
        (field.name, field.value_type.value, field.minimum, field.maximum)
        for field in positions_pagination.cursor_fields
    ] == [
        ("$skip", "integer", 0, 1_000_000),
        ("$top", "integer", 1, 1_000),
    ]
    assert positions_pagination.valid_combinations == (
        ("$skip",),
        ("$top",),
        ("$skip", "$top"),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "POST"),
        ("path_template", "https://example.invalid/chart/v3/charts"),
        ("operation_id", "post.chart.v3.charts.subscriptions"),
    ],
)
def test_contract_model_rejects_non_read_or_unregistered_routes(
    field: str,
    value: str,
) -> None:
    chart = source_contracts_by_id()["chart_v3"]
    payload = chart.model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError):
        SourceContract.model_validate(payload, strict=True)


def test_additive_fields_quarantine_dependent_analytics() -> None:
    chart = source_contracts_by_id()["chart_v3"]

    comparison = compare_source_schema(
        chart,
        {
            "Data": [
                {
                    "CloseBid": 101.0,
                    "NewChartField": "additive-field",
                    "Time": "2026-07-29T08:00:00Z",
                }
            ],
            "DataVersion": 7,
        },
    )

    assert comparison.compatible is False
    assert comparison.quarantined_analysis_kinds == chart.dependent_analysis_kinds
    assert "NewChartField" in comparison.additive_fields
    assert "AskVolume" in comparison.missing_optional_fields


def test_response_contract_closes_envelope_objects_and_list_items() -> None:
    contracts = source_contracts_by_id()
    positions = contracts["positions_v1"]
    options = contracts["options_chain_reference_v1"]

    assert positions.response_envelope.row_location.value == "data"
    position_base = next(field for field in positions.fields if field.name == "PositionBase")
    assert position_base.object_mode is not None
    assert position_base.object_mode.value == "closed"
    assert {field.name for field in position_base.properties} >= {
        "Amount",
        "AssetType",
        "Uic",
    }

    specific_options = next(field for field in options.fields if field.name == "SpecificOptions")
    assert specific_options.items is not None
    assert specific_options.items.value_type is SourceValueType.OBJECT
    assert {field.name for field in specific_options.items.properties} >= {
        "PutCall",
        "Strike",
        "Uic",
    }


@pytest.mark.parametrize(
    ("contract_id", "payload", "additive_path"),
    [
        (
            "chart_v3",
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "Time": "2026-07-29T08:00:00Z",
                    }
                ],
                "DataVersion": 7,
                "NewEnvelopeField": {"private": "marker"},
            },
            "NewEnvelopeField",
        ),
        (
            "positions_v1",
            {
                "Data": [
                    {
                        "PositionBase": {
                            "Amount": 1,
                            "AssetType": "Stock",
                            "NewNestedField": "private-marker",
                            "Uic": 1001,
                        },
                        "PositionId": "synthetic-position",
                    }
                ]
            },
            "PositionBase.NewNestedField",
        ),
        (
            "options_chain_reference_v1",
            {
                "ExpiryDates": ["2026-09-18"],
                "OptionRootId": 17,
                "SpecificOptions": [
                    {
                        "NewNestedField": "private-marker",
                        "PutCall": "Call",
                        "Strike": 100.0,
                        "Uic": 1001,
                    }
                ],
            },
            "SpecificOptions[].NewNestedField",
        ),
    ],
)
def test_additive_envelope_and_recursive_fields_are_incompatible(
    contract_id: str,
    payload: dict[str, Any],
    additive_path: str,
) -> None:
    contract = source_contracts_by_id()[contract_id]

    comparison = compare_source_schema(contract, payload)

    assert comparison.compatible is False
    assert additive_path in comparison.additive_fields
    assert comparison.quarantined_analysis_kinds == contract.dependent_analysis_kinds


def test_unknown_enum_values_quarantine_dependent_analytics() -> None:
    instruments = source_contracts_by_id()["reference_instruments_v1"]

    comparison = compare_source_schema(
        instruments,
        _fixture("reference_unknown_enum.json"),
    )

    assert comparison.compatible is False
    assert comparison.unknown_enum_values == {
        "AssetType": ("FutureSaxoAssetType",),
    }
    assert comparison.quarantined_analysis_kinds == instruments.dependent_analysis_kinds


def test_unknown_enum_provenance_is_immutable_and_serializes_deterministically() -> None:
    instruments = source_contracts_by_id()["reference_instruments_v1"]
    comparison = compare_source_schema(
        instruments,
        _fixture("reference_unknown_enum.json"),
    )

    serialized_before = comparison.model_dump_json()
    with pytest.raises(TypeError):
        cast(
            "dict[str, tuple[str, ...]]",
            comparison.unknown_enum_values,
        )["AssetType"] = ("InjectedAssetType",)

    assert comparison.model_dump_json() == serialized_before
    assert json.loads(serialized_before)["unknown_enum_values"] == {
        "AssetType": ["FutureSaxoAssetType"],
    }


def test_omitted_optional_performance_fields_remain_compatible() -> None:
    performance = source_contracts_by_id()["performance_timeseries_v4"]

    comparison = compare_source_schema(
        performance,
        _fixture("performance_optional_omitted.json"),
    )

    assert comparison.compatible is True
    assert next(field for field in performance.fields if field.name == "Date").value_type is (
        SourceValueType.DATE
    )
    assert "Benchmark" in comparison.missing_optional_fields
    assert comparison.structural_errors == ()


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        ({"Data": [{"CloseBid": 101.0}], "DataVersion": 7}, "Time"),
        ({"Data": [{"Time": None, "CloseBid": 101.0}], "DataVersion": 7}, "Time"),
        ({"Data": [{"Time": 123, "CloseBid": 101.0}], "DataVersion": 7}, "Time"),
    ],
)
def test_required_task_field_drift_quarantines_dependent_analysis(
    payload: dict[str, Any],
    expected_field: str,
) -> None:
    chart = source_contracts_by_id()["chart_v3"]

    comparison = compare_source_schema(chart, payload)

    assert comparison.compatible is False
    assert expected_field in (
        comparison.missing_required_fields
        + comparison.null_required_fields
        + comparison.required_type_mismatches
    )
    assert comparison.quarantined_analysis_kinds == chart.dependent_analysis_kinds


def test_missing_required_top_level_revision_field_quarantines_chart_analytics() -> None:
    chart = source_contracts_by_id()["chart_v3"]
    payload = {
        "Data": [
            {
                "Time": "2026-07-29T08:00:00Z",
                "CloseBid": 101.0,
            }
        ]
    }

    comparison = compare_source_schema(chart, payload)

    assert comparison.compatible is False
    assert comparison.missing_required_fields == ("DataVersion",)
    assert comparison.quarantined_analysis_kinds == chart.dependent_analysis_kinds


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "2026-07-29",
        "2026-07-29T08:00:00",
    ],
)
def test_required_timestamp_needs_a_time_and_timezone(
    invalid_timestamp: str,
) -> None:
    chart = source_contracts_by_id()["chart_v3"]

    comparison = compare_source_schema(
        chart,
        {
            "Data": [{"Time": invalid_timestamp, "CloseBid": 101.0}],
            "DataVersion": 7,
        },
    )

    assert comparison.compatible is False
    assert comparison.required_type_mismatches == ("Time",)
    assert comparison.quarantined_analysis_kinds == chart.dependent_analysis_kinds


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"DataVersion": 7}, "data_field_missing"),
        ({"Data": {}, "DataVersion": 7}, "data_field_not_array"),
        ({"Data": ["not-an-object"], "DataVersion": 7}, "data_row_not_object"),
        ({"Data": [], "DataVersion": 7, "__next": 42}, "next_link_not_string"),
    ],
)
def test_structural_response_drift_is_fail_closed(
    payload: dict[str, Any],
    expected_error: str,
) -> None:
    chart = source_contracts_by_id()["chart_v3"]

    comparison = compare_source_schema(chart, payload)

    assert comparison.compatible is False
    assert expected_error in comparison.structural_errors
    assert comparison.quarantined_analysis_kinds == chart.dependent_analysis_kinds


def test_optional_null_and_type_variation_does_not_hide_required_data() -> None:
    chart = source_contracts_by_id()["chart_v3"]
    payload = {
        "DataVersion": 7,
        "Data": [
            {
                "Time": "2026-07-29T08:00:00Z",
                "CloseBid": 101.0,
                "Volume": None,
                "PriceType": 123,
            }
        ],
    }

    comparison = compare_source_schema(chart, payload)

    assert comparison.compatible is True
    assert comparison.null_optional_fields == ("Volume",)
    assert comparison.optional_type_mismatches == ("PriceType",)
