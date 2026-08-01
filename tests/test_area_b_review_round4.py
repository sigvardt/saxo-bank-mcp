from __future__ import annotations

import copy
import hashlib
import importlib
import json
import shutil
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx2
import pytest
from analytics_source_matrix_support import ScriptedMatrixSession

import saxo_bank_mcp.analytics_source_receipt as receipt_module
import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_source_process import MatrixCallPolicy, RegisteredCallProfile
from saxo_bank_mcp.endpoint_registry import find_registered_endpoint
from saxo_bank_mcp.read_tool_execution import ReadExecutionContext
from saxo_bank_mcp.registered_read_execution import RegisteredReadResponse

_ROOT = Path(__file__).parents[1]
_EXPECTED_POST_ACCESS_ATTEMPTS = 2
_SOURCE_CANDIDATE_FILES_HELPER = "_source_candidate_files"
_RUNTIME_TREE_PROJECTION_HELPER = "_runtime_tree_projection"
_VALIDATE_DIRECTORY_ENTRY_TREE_HELPER = "_validate_directory_entry_tree"
_source_runtime = importlib.import_module("saxo_bank_mcp.analytics_source_runtime")
_source_candidate_files = cast(
    "Callable[[Path], dict[str, str]]",
    getattr(_source_runtime, _SOURCE_CANDIDATE_FILES_HELPER),
)
_runtime_tree_projection = cast(
    "Callable[[Path], tuple[str, int]]",
    getattr(_source_runtime, _RUNTIME_TREE_PROJECTION_HELPER),
)
_validate_directory_entry_tree = cast(
    "Callable[..., None]",
    getattr(_source_runtime, _VALIDATE_DIRECTORY_ENTRY_TREE_HELPER),
)


def _copy_source_candidate(target: Path) -> None:
    ignore_python_cache = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    shutil.copytree(_ROOT / "src", target / "src", ignore=ignore_python_cache)
    shutil.copytree(_ROOT / "data" / "analytics", target / "data" / "analytics")
    (target / "data" / "saxo").mkdir(parents=True)
    shutil.copyfile(
        _ROOT / "data" / "saxo" / "openapi_inventory.json",
        target / "data" / "saxo" / "openapi_inventory.json",
    )
    (target / "scripts").mkdir()
    for name in (
        "generate_analytics_source_matrix_candidate.py",
        "prepare_analytics_source_matrix_runtime.py",
        "run_analytics_source_matrix.py",
        "saxo-bank-analytics-source-matrix",
        "saxo-bank-analytics-source-matrix-generate",
    ):
        shutil.copyfile(_ROOT / "scripts" / name, target / "scripts" / name)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(_ROOT / name, target / name)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def test_candidate_identity_closes_startup_import_and_python_runtime(
    tmp_path: Path,
) -> None:
    source_copy = tmp_path / "source-copy"
    _copy_source_candidate(source_copy)
    baseline = _source_candidate_files(source_copy)

    package = source_copy / "src" / "saxo_bank_mcp"
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = package / "linked-runtime"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _source_candidate_files(source_copy)
    linked.unlink()

    runtime_tree = tmp_path / "runtime-tree"
    runtime_tree.mkdir()
    runtime_module = runtime_tree / "module.py"
    runtime_module.write_text("VALUE = 1\n", encoding="utf-8")
    initial = _runtime_tree_projection(runtime_tree)
    runtime_module.write_text("VALUE = 2\n", encoding="utf-8")
    assert _runtime_tree_projection(runtime_tree) != initial
    assert _source_candidate_files(source_copy) == baseline


def test_candidate_identity_refuses_unsealed_python_cache_and_search_symlink(
    tmp_path: Path,
) -> None:
    source_copy = tmp_path / "source-copy"
    _copy_source_candidate(source_copy)
    source_cache = source_copy / "src" / "saxo_bank_mcp" / "__pycache__"
    source_cache.mkdir()
    (source_cache / "unsealed.cpython-312.pyc").write_bytes(b"unsealed-cache")
    with pytest.raises(ValueError, match="cache"):
        _source_candidate_files(source_copy)

    startup_search = tmp_path / "startup-search"
    startup_search.mkdir()
    _validate_directory_entry_tree(
        startup_search,
        scope="round4 startup search",
    )
    startup_cache = startup_search / "__pycache__"
    startup_cache.mkdir()
    (startup_cache / "unsealed.cpython-312.pyc").write_bytes(b"unsealed-cache")
    with pytest.raises(ValueError, match="cache"):
        _validate_directory_entry_tree(
            startup_search,
            scope="round4 startup search",
        )
    shutil.rmtree(startup_cache)

    outside = tmp_path / "outside"
    outside.mkdir()
    startup_symlink = startup_search / "unsealed-search-link"
    startup_symlink.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _validate_directory_entry_tree(
            startup_search,
            scope="round4 startup search",
        )

    runtime_tree = tmp_path / "runtime-tree"
    runtime_tree.mkdir()
    (runtime_tree / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    for relative in (
        Path("__pycache__/module.cpython-312.pyc"),
        Path("module.pyc"),
        Path("module.pyo"),
    ):
        cache_entry = runtime_tree / relative
        cache_entry.parent.mkdir(parents=True, exist_ok=True)
        cache_entry.write_bytes(b"unsealed-cache")
        with pytest.raises(ValueError, match="cache"):
            _runtime_tree_projection(runtime_tree)
        cache_entry.unlink()
        if cache_entry.parent != runtime_tree:
            cache_entry.parent.rmdir()


def test_candidate_identity_seals_ordered_search_directories_during_verification() -> None:
    runtime_source = (
        _ROOT / "src" / "saxo_bank_mcp" / "analytics_source_runtime.py"
    ).read_text(encoding="utf-8")
    assert "def _scalar_material(" in runtime_source
    assert "sys.modules" not in runtime_source
    assert "sys.path_importer_cache" not in runtime_source
    assert "sys.meta_path" not in runtime_source


def _source_failure_payload(  # noqa: PLR0913
    *,
    status: str = "http_error",
    reason: str = "source_http_error",
    http_status: int | None = 500,
    network_call_count: int = 1,
    attempt_count: int = 1,
    initial_attempt_count: int = 1,
    continuation_attempt_count: int = 0,
    retry_count: int = 0,
    distinct_target_count: int = 1,
    continuation_call_count: int = 0,
) -> dict[str, JsonValue]:
    contract = source_contracts_by_id()["chart_v3"]
    return {
        "status": status,
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": ("sim_read_http_error" if status == "http_error" else "sim_read_refused"),
        "operation_id": contract.operation_id,
        "service_group": "Chart",
        "method": "GET",
        "path": contract.path_template,
        "environment": "SIM",
        "network_call_made": network_call_count > 0,
        "network_call_count": network_call_count,
        "attempt_count": attempt_count,
        "initial_attempt_count": initial_attempt_count,
        "continuation_attempt_count": continuation_attempt_count,
        "retry_count": retry_count,
        "distinct_target_count": distinct_target_count,
        "successful_page_count": 0,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": True,
        "trading_ready": False,
        "response": None,
        "response_visibility": "analytics_contract_receipt",
        "response_fingerprint": None,
        "response_fingerprint_scope": "analytics_contract_receipt",
        "analytics_contract_id": contract.contract_id,
        "analytics_contract_sha256": source_contract_fingerprint(contract),
        "page_count": 0,
        "row_count": 0,
        "continuation_call_count": continuation_call_count,
        "reason": reason,
        "http_status": http_status,
        "request_fingerprint_sha256": _digest(
            {
                "contract_id": "chart_v3",
                "request": {"AssetType": "Stock", "Count": "2", "Uic": "1001"},
            },
        ),
    }


async def _source_receipt(
    payload: dict[str, JsonValue],
) -> matrix_module.SourceContractReceipt:
    contract = source_contracts_by_id()["chart_v3"]
    profile = RegisteredCallProfile(
        path=contract.path_template,
        params={"AssetType": "Stock", "Count": "2", "Uic": "1001"},
        response_mode="analytics_contract_receipt",
        analytics_contract_id=contract.contract_id,
    )
    session = ScriptedMatrixSession(
        payloads={"saxo_call_registered_endpoint": deque([payload])},
    )
    policy = MatrixCallPolicy.from_local_registry((profile,))
    return await matrix_module._run_provider_source(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        session,
        policy,
        contract,
        {"AssetType": "Stock", "Count": 2, "Uic": 1001},
    )


@pytest.mark.anyio
async def test_http_failure_requires_real_activity_and_reason_bound_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (await _source_receipt(_source_failure_payload())).source_status == "reduced"

    zero_activity = _source_failure_payload(
        network_call_count=0,
        attempt_count=0,
        initial_attempt_count=0,
        distinct_target_count=0,
    )
    assert (await _source_receipt(zero_activity)).source_status == "refused"

    for reason, status in (
        ("source_rate_limited", 500),
        ("source_entitlement_unavailable", 500),
        ("source_http_error", 429),
        ("source_http_error", 302),
    ):
        contradictory = _source_failure_payload(reason=reason, http_status=status)
        assert (await _source_receipt(contradictory)).source_status == "refused"

    pre_network_access = _source_failure_payload(
        status="refused",
        reason="source_access_unavailable",
        http_status=None,
        network_call_count=0,
    )
    assert (await _source_receipt(pre_network_access)).source_status == "reduced"
    access_after_network = _source_failure_payload(
        status="refused",
        reason="source_access_unavailable",
        http_status=None,
    )
    assert (await _source_receipt(access_after_network)).source_status == "refused"
    classified_access_after_network = _source_failure_payload(
        status="refused",
        reason="source_access_unavailable_after_network",
        http_status=None,
    )
    assert (await _source_receipt(classified_access_after_network)).source_status == "reduced"

    post_network_drift = _source_failure_payload(
        status="refused",
        reason="source_schema_drift",
        http_status=None,
    )
    assert (await _source_receipt(post_network_drift)).source_status == "reduced"

    registered = find_registered_endpoint("GET", "/chart/v3/charts")
    assert registered is not None
    first_page = (_ROOT / "tests/fixtures/analytics/saxo_pages/chart_page_1.json").read_bytes()
    attempts = 0

    async def execute(
        _operation: object,
        _request_target: str,
        _params: dict[str, str],
        **_kwargs: object,
    ) -> RegisteredReadResponse | dict[str, JsonValue]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return RegisteredReadResponse(
                context=ReadExecutionContext(
                    environment="SIM",
                    rest_base_url="https://registered.invalid",
                    token=None,
                ),
                response=httpx2.Response(
                    200,
                    content=first_page,
                    request=httpx2.Request("GET", "https://registered.invalid"),
                ),
            )
        return {
            "status": "auth_required",
            "environment": "SIM",
            "network_call_made": False,
            "reason": "token_expired",
        }

    monkeypatch.setattr(receipt_module, "execute_registered_get", execute)
    produced = await receipt_module.analytics_contract_receipt(
        registered,
        contract_id="chart_v3",
        params={"AssetType": "Stock", "Count": "2", "Uic": "1001"},
    )
    assert produced["reason"] == "source_access_unavailable_after_network"
    assert produced["network_call_count"] == 1
    assert produced["attempt_count"] == _EXPECTED_POST_ACCESS_ATTEMPTS
    assert produced["distinct_target_count"] == _EXPECTED_POST_ACCESS_ATTEMPTS
    assert (await _source_receipt(produced)).source_status == "reduced"


def _valid_session_receipt() -> dict[str, JsonValue]:
    return {
        "status": "passed",
        "tool_name": "saxo_get_session_capabilities",
        "call_class": "sim_read_succeeded",
        "environment": "SIM",
        "endpoint_path": "/root/v1/sessions/capabilities",
        "token_refreshed": False,
        "token": {
            "has_access_token": True,
            "has_refresh_token": False,
            "has_code_verifier": False,
            "environment": "SIM",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "is_expired": False,
        },
        "token_refresh_supported": False,
        "scope_used": False,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "capabilities": {
            "AuthenticationLevel": "Strong",
            "DataLevel": "Full",
            "TradeLevel": "None",
        },
        "next_action": "use these fields only as current session-capability proof",
        "verifies": [
            "cached SIM bearer token can read current session capability fields",
        ],
        "does_not_verify": [
            "order placement safety",
            "instrument/account suitability",
            "real-money approval",
            "live endpoint access",
        ],
    }


def _valid_entitlements_receipt() -> dict[str, JsonValue]:
    return {
        "status": "passed",
        "tool_name": "saxo_get_entitlements",
        "call_class": "sim_read_succeeded",
        "environment": "SIM",
        "endpoint_path": "/port/v1/users/me/entitlements",
        "entitlement_field_set": "Default",
        "token_refreshed": False,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "entitlement_summary": {
            "exchange_count": 2,
            "max_rows": 100,
            "response_count": 2,
            "has_next_page": False,
            "possibly_truncated": False,
        },
        "exchange_ids": ["XNAS", "XCSE"],
        "entitlement_bucket_counts": {
            "DelayedFullBook": 0,
            "DelayedGreeks": 0,
            "Greeks": 0,
            "RealTimeFullBook": 0,
            "RealTimeTopOfBook": 2,
        },
        "verifies": [
            "cached SIM bearer token can read current market-data entitlement summary",
        ],
        "does_not_verify": [
            "price availability for a specific instrument",
            "quote recency or real-time price delivery for any instrument",
            "order placement safety",
            "instrument/account suitability",
            "real-money approval",
            "live endpoint access",
        ],
    }


def test_readiness_nested_receipts_are_recursively_strict_and_coherent() -> None:
    session = _valid_session_receipt()
    entitlements = _valid_entitlements_receipt()
    assert (
        matrix_module._network_read_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            session,
            "saxo_get_session_capabilities",
        )
        == "passed"
    )
    assert (
        matrix_module._network_read_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            entitlements,
            "saxo_get_entitlements",
        )
        == "passed"
    )

    session_mutations: list[dict[str, JsonValue]] = []
    mutation = copy.deepcopy(session)
    cast("dict[str, JsonValue]", mutation["token"])["access_token"] = "hidden-token-value"  # noqa: S105
    session_mutations.append(mutation)
    mutation = copy.deepcopy(session)
    cast("dict[str, JsonValue]", mutation["token"])["has_refresh_token"] = True
    session_mutations.append(mutation)
    mutation = copy.deepcopy(session)
    cast("dict[str, JsonValue]", mutation["token"])["expires_at"] = "2000-01-01T00:00:00+00:00"
    session_mutations.append(mutation)
    mutation = copy.deepcopy(session)
    cast("dict[str, JsonValue]", mutation["capabilities"])["mutation_possible"] = True
    session_mutations.append(mutation)
    mutation = copy.deepcopy(session)
    cast("dict[str, JsonValue]", mutation["capabilities"])["DataLevel"] = cast(
        "JsonValue",
        ["Full"],
    )
    session_mutations.append(mutation)
    for contradictory in session_mutations:
        assert (
            matrix_module._network_read_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                contradictory,
                "saxo_get_session_capabilities",
            )
            == "unverified"
        )

    entitlement_mutations: list[dict[str, JsonValue]] = []
    mutation = copy.deepcopy(entitlements)
    cast("dict[str, JsonValue]", mutation["entitlement_summary"])["exchange_count"] = 1
    entitlement_mutations.append(mutation)
    mutation = copy.deepcopy(entitlements)
    cast("dict[str, JsonValue]", mutation["entitlement_summary"])["possibly_truncated"] = True
    entitlement_mutations.append(mutation)
    mutation = copy.deepcopy(entitlements)
    cast("dict[str, JsonValue]", mutation["entitlement_summary"])["response_count"] = 1
    entitlement_mutations.append(mutation)
    mutation = copy.deepcopy(entitlements)
    cast("dict[str, JsonValue]", mutation["entitlement_bucket_counts"]).pop(
        "DelayedGreeks",
    )
    entitlement_mutations.append(mutation)
    mutation = copy.deepcopy(entitlements)
    cast("dict[str, JsonValue]", mutation["entitlement_bucket_counts"])["UnsafeWriteBucket"] = 1
    entitlement_mutations.append(mutation)
    mutation = copy.deepcopy(entitlements)
    cast("dict[str, JsonValue]", mutation["entitlement_bucket_counts"])["RealTimeTopOfBook"] = True
    entitlement_mutations.append(mutation)
    mutation = copy.deepcopy(entitlements)
    cast("dict[str, JsonValue]", mutation["entitlement_summary"])["account_key"] = "private-account"
    entitlement_mutations.append(mutation)
    for contradictory in entitlement_mutations:
        assert (
            matrix_module._network_read_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                contradictory,
                "saxo_get_entitlements",
            )
            == "unverified"
        )
