from __future__ import annotations

import json
from pathlib import Path

from saxo_bank_mcp import inventory
from saxo_bank_mcp.endpoint_registry import (
    EXPECTED_OPERATION_COUNT,
    EXPECTED_SERVICE_GROUP_COUNTS,
    find_registered_endpoint,
    find_registered_operation,
    load_inventory,
    path_rejection_reason,
    validate_inventory,
)


def test_checked_in_inventory_matches_official_service_group_counts() -> None:
    loaded = load_inventory()

    assert loaded.source_url == "https://www.developer.saxo/openapi/referencedocs"
    assert loaded.retrieved_at == "2026-07-01"
    assert loaded.operation_count == EXPECTED_OPERATION_COUNT
    assert loaded.service_group_counts == EXPECTED_SERVICE_GROUP_COUNTS
    assert len(loaded.operations) == loaded.operation_count


def test_inventory_validation_has_no_undecided_or_unclassified_operations() -> None:
    report = validate_inventory(load_inventory())

    assert report["status"] == "passed"
    assert report["operation_count"] == EXPECTED_OPERATION_COUNT
    assert report["unclassified_count"] == 0
    assert report["undecided_count"] == 0
    implemented_count = report["implemented_count"]
    refused_count = report["refused_count"]
    assert isinstance(implemented_count, int)
    assert isinstance(refused_count, int)
    assert implemented_count > 0
    assert refused_count > 0


def test_inventory_cli_writes_validation_report(tmp_path: Path) -> None:
    out = tmp_path / "inventory.json"

    result = inventory.main(["validate", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["service_group_counts"] == EXPECTED_SERVICE_GROUP_COUNTS
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_registry_finds_only_registered_relative_saxo_paths() -> None:
    diagnostics = find_registered_operation("GET", "/root/v1/diagnostics/get")
    unregistered = find_registered_operation("GET", "/not-a-registered-saxo-path")
    arbitrary_host = find_registered_operation(
        "GET", "https://evil.example/root/v1/diagnostics/get"
    )

    assert diagnostics is not None
    assert diagnostics.service_group == "Root Services"
    assert diagnostics.status == "implemented"
    assert diagnostics.auth_requirement == "none"
    assert unregistered is None
    assert arbitrary_host is None


def test_registry_resolves_documented_path_templates() -> None:
    registered = find_registered_endpoint("GET", "/hist/v3/accountvalues/client-123")

    assert registered is not None
    assert registered.operation.operation_id == "get.hist.v3.accountvalues.clientkey"
    assert registered.resolved_path == "/hist/v3/accountvalues/client-123"


def test_registry_prefers_exact_paths_over_placeholder_matches() -> None:
    account = find_registered_endpoint("GET", "/port/v1/accounts/me")
    position = find_registered_endpoint("GET", "/port/v1/positions/me")

    assert account is not None
    assert account.operation.operation_id == "get.port.v1.accounts.me"
    assert position is not None
    assert position.operation.operation_id == "get.port.v1.positions.me"


def test_registry_rejects_unsafe_path_template_values() -> None:
    assert find_registered_endpoint("GET", "/hist/v3/accountvalues/..") is None
    assert find_registered_endpoint("GET", "/hist/v3/accountvalues/%2Fsecret") is None


def test_registry_names_rejected_path_kind() -> None:
    assert path_rejection_reason("https://evil.example/root/v1/diagnostics/get") == (
        "absolute_url_rejected"
    )
    assert path_rejection_reason("//evil.example/root/v1/diagnostics/get") == (
        "absolute_url_rejected"
    )
    assert path_rejection_reason("root/v1/diagnostics/get") == "relative_path_required"
    assert path_rejection_reason("/root/v1/diagnostics/get") is None
