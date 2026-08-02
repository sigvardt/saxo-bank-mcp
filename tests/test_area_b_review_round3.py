from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx2
import pytest
from analytics_source_matrix_support import REPOSITORY_COPY_IGNORE, ScriptedMatrixSession

import saxo_bank_mcp.analytics_source_receipt as receipt_module
import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_source_contracts import (
    SourceCaptureEnvelope,
    SourcePage,
    build_source_capture_context,
    build_source_capture_envelope,
    source_contract_fingerprint,
    source_contracts_by_id,
    source_page_fingerprint,
)
from saxo_bank_mcp.analytics_source_process import MatrixCallPolicy, RegisteredCallProfile
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    StoreConflictError,
)
from saxo_bank_mcp.auth_status import AuthStatusInputs, build_auth_status
from saxo_bank_mcp.endpoint_registry import EndpointOperation, find_registered_endpoint
from saxo_bank_mcp.read_tool_execution import ReadExecutionContext
from saxo_bank_mcp.registered_read_execution import RegisteredReadResponse

_ROOT = Path(__file__).parents[1]
_FIXTURE_ROOT = Path(__file__).parent / "fixtures/analytics/saxo_pages"
_CAPTURED_AT = datetime(2026, 7, 30, 12, tzinfo=UTC)
_SECOND_ATTEMPT = 2
_EXPECTED_ATTEMPTS = 3
_EXPECTED_CONTINUATION_ATTEMPTS = 2
_RATE_LIMIT_STATUS = 429
_SOURCE_CANDIDATE_FILES_HELPER = "_source_candidate_files"
_source_candidate_files = cast(
    "Callable[[Path], dict[str, str]]",
    getattr(
        importlib.import_module("saxo_bank_mcp.analytics_source_runtime"),
        _SOURCE_CANDIDATE_FILES_HELPER,
    ),
)
_IDENTITY_COMMAND = (
    "from saxo_bank_mcp.qa_analytics_source_matrix import "
    "source_matrix_candidate_identity;"
    "print(source_matrix_candidate_identity().candidate_identity_sha256)"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()


def _identity() -> matrix_module.SourceMatrixCandidateIdentity:
    return matrix_module.SourceMatrixCandidateIdentity(
        source_contract_catalog_sha256="a" * 64,
        harness_build_sha256="b" * 64,
        candidate_identity_sha256="c" * 64,
    )


def _mutated_wheel(source: Path, target: Path) -> None:
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(target, "w") as output:
        for item in archive.infolist():
            value = archive.read(item.filename)
            if item.filename == "saxo_bank_mcp/server.py":
                value += b"\nROUND3_FOREIGN_WHEEL = True\n"
            output.writestr(item, value)


def _copy_source_candidate(target: Path) -> None:
    shutil.copytree(
        _ROOT / "src",
        target / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
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


def test_installed_identity_seals_runtime_dependencies_and_source_wheel_projection(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    uv_path = Path(uv).resolve(strict=True)
    build_root = tmp_path / "build-source"
    shutil.copytree(
        _ROOT,
        build_root,
        ignore=REPOSITORY_COPY_IGNORE,
    )
    wheel_dir = tmp_path / "wheel"
    build = subprocess.run(
        (str(uv_path), "build", "--offline", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=build_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl")).resolve(strict=True)
    runtime = tmp_path / "runtime"
    prepared = subprocess.run(
        (
            sys.executable,
            str(build_root / "scripts/prepare_analytics_source_matrix_runtime.py"),
            "--runtime",
            str(runtime),
            "--wheel",
            str(wheel),
            "--uv",
            str(uv_path),
        ),
        cwd=build_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert prepared.returncode == 0, prepared.stderr
    generated = tmp_path / "candidate.json"
    generation = subprocess.run(
        (
            str(runtime / "bin/saxo-bank-analytics-source-matrix-generate"),
            "--repository-root",
            str(build_root),
            "--wheel",
            str(wheel),
            "--out",
            str(generated),
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert generation.returncode == 0, generation.stderr
    manifest = json.loads(generated.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "5"
    assert manifest["runtime_identity"]["implementation"] == sys.implementation.name
    assert manifest["runtime_identity"]["executable_sha256"]
    assert "pydantic" in manifest["dependency_distributions"]
    assert "annotated-types" in manifest["dependency_distributions"]
    assert manifest["dependency_distributions"]["pydantic"]["files"]
    assert manifest["source_wheel_projection_sha256"]
    assert manifest["portable_runtime_tree_sha256"]
    assert manifest["portable_runtime_entry_count"] > 1
    assert manifest["child_bootstrap_sha256"]
    assert {
        "scripts/generate_analytics_source_matrix_candidate.py",
        "scripts/run_analytics_source_matrix.py",
    } <= set(manifest["source_files"])

    foreign_wheel = tmp_path / "foreign.whl"
    _mutated_wheel(wheel, foreign_wheel)
    foreign = subprocess.run(
        (
            str(runtime / "bin/saxo-bank-analytics-source-matrix-generate"),
            "--repository-root",
            str(build_root),
            "--wheel",
            str(foreign_wheel),
            "--out",
            str(tmp_path / "foreign.json"),
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert foreign.returncode != 0

    source_copy = tmp_path / "source-copy"
    _copy_source_candidate(source_copy)
    source_files = _source_candidate_files(source_copy)
    for name in (
        "generate_analytics_source_matrix_candidate.py",
        "prepare_analytics_source_matrix_runtime.py",
        "run_analytics_source_matrix.py",
    ):
        target = source_copy / "scripts" / name
        original = target.read_bytes()
        target.write_bytes(original + b"\nROUND3_SOURCE_MUTATION = True\n")
        refused = _source_candidate_files(source_copy)
        target.write_bytes(original)
        assert refused != source_files, name

    for path in sorted(
        runtime.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
    runtime.chmod(0o700)


def test_guard_walk_is_symlink_owner_mode_and_parent_swap_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure_claim = getattr(matrix_module, "_claim_candidate_guard_at_owner", None)
    assert callable(secure_claim)

    symlink_owner = tmp_path / "symlink-owner"
    symlink_owner.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (symlink_owner / ".local").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="directory"):
        secure_claim(symlink_owner, _identity())
    assert not list(outside.rglob("claimed.json"))

    wrong_mode_owner = tmp_path / "wrong-mode-owner"
    protected = wrong_mode_owner / ".local" / "state" / "saxo-bank-mcp"
    protected.mkdir(parents=True, mode=0o755)
    protected.chmod(0o755)
    with pytest.raises(ValueError, match="mode"):
        secure_claim(wrong_mode_owner, _identity())

    wrong_owner = tmp_path / "wrong-owner"
    wrong_owner.mkdir(mode=0o700)
    actual_uid = os.getuid()
    monkeypatch.setattr(matrix_module.os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(ValueError, match="owner"):
        secure_claim(wrong_owner, _identity())
    monkeypatch.setattr(matrix_module.os, "getuid", lambda: actual_uid)

    swap_owner = tmp_path / "swap-owner"
    swap_owner.mkdir(mode=0o700)
    original_open = matrix_module.os.open
    swapped = False

    def swapping_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "claimed.json" and dir_fd is not None and not swapped:
            identity_dir = (
                swap_owner
                / ".local"
                / "state"
                / "saxo-bank-mcp"
                / "qa"
                / "analytics-source-matrix"
                / _identity().candidate_identity_sha256
            )
            moved = identity_dir.with_name(identity_dir.name + ".moved")
            identity_dir.rename(moved)
            identity_dir.mkdir(mode=0o700)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(matrix_module.os, "open", swapping_open)
    with pytest.raises(ValueError, match="changed"):
        secure_claim(swap_owner, _identity())
    assert not list(swap_owner.rglob("claimed.json"))


def _valid_auth_receipt() -> dict[str, JsonValue]:
    return cast(
        "dict[str, JsonValue]",
        build_auth_status(
            AuthStatusInputs(
                requested_environment="SIM",
                effective_read_environment="SIM",
                live_reads_enabled=False,
                sim_credentials_present=True,
                sim_credential_source="env",
                live_credentials_present=False,
                sim_redirect_uri_present=False,
                pending_pkce_authorization_present=False,
                token_cache_path_refused=False,
                token_cache_present=True,
                token_cache_readable=True,
                token_cache_expired=False,
                token_cache_refresh_supported=False,
                token_cache_environment="SIM",  # noqa: S106
            ),
        ),
    )


def _valid_network_read_receipt(tool_name: str) -> dict[str, JsonValue]:
    common: dict[str, JsonValue] = {
        "status": "passed",
        "tool_name": tool_name,
        "call_class": "sim_read_succeeded",
        "environment": "SIM",
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }
    if tool_name == "saxo_get_session_capabilities":
        return {
            **common,
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
            "capabilities": {
                "AuthenticationLevel": "Strong",
                "DataLevel": "Full",
                "TradeLevel": "None",
            },
            "next_action": "use current capability fields only",
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
    return {
        **common,
        "endpoint_path": "/port/v1/users/me/entitlements",
        "entitlement_field_set": "Default",
        "token_refreshed": False,
        "entitlement_summary": {
            "exchange_count": 0,
            "max_rows": 1000,
            "response_count": 0,
            "has_next_page": False,
            "possibly_truncated": False,
        },
        "exchange_ids": [],
        "entitlement_bucket_counts": {
            "DelayedFullBook": 0,
            "DelayedGreeks": 0,
            "Greeks": 0,
            "RealTimeFullBook": 0,
            "RealTimeTopOfBook": 0,
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


def test_readiness_receipts_require_exact_production_schema_and_safe_flags() -> None:
    auth = _valid_auth_receipt()
    assert (
        matrix_module._auth_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            auth,
        )
        == "ready"
    )
    for field, value in (
        ("mutation_possible", False),
        ("live_access", False),
        ("write", False),
        ("subscription", False),
    ):
        augmented = {**auth, field: value}
        assert (
            matrix_module._auth_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                augmented,
            )
            == "unavailable"
        )

    for tool_name in (
        "saxo_get_session_capabilities",
        "saxo_get_entitlements",
    ):
        valid = _valid_network_read_receipt(tool_name)
        assert (
            matrix_module._network_read_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                valid,
                tool_name,
            )
            == "passed"
        )
        mutations = (
            {**valid, "mutation_possible": False},
            {**valid, "network_call_made": 1},
            {**valid, "live_access": True},
            {**valid, "tool_name": "saxo_auth_status"},
        )
        for contradictory in mutations:
            assert (
                matrix_module._network_read_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    contradictory,
                    tool_name,
                )
                == "unverified"
            )


def _source_request_fingerprint() -> str:
    return _digest(
        {
            "contract_id": "chart_v3",
            "request": {"AssetType": "Stock", "Count": "2", "Uic": "1001"},
        },
    )


def _source_success_payload(*, page_count: int = 1) -> dict[str, JsonValue]:
    contract = source_contracts_by_id()["chart_v3"]
    quality: dict[str, JsonValue] = {
        "state": "not_applicable",
        "entitlement_limited_fields": [],
        "delayed_fields": [],
        "missing_fields": [],
    }
    pages: list[dict[str, JsonValue]] = [
        {
            "page_number": page_number,
            "row_count": 1,
            "page_fingerprint_sha256": f"{page_number:x}" * 64,
            "source_revision_sha256": "2" * 64,
            "schema_fingerprint_sha256": "3" * 64,
            "timestamp_value_count": 1,
            "timestamp_fingerprint_sha256": "4" * 64,
            "source_quality": dict(quality),
        }
        for page_number in range(1, page_count + 1)
    ]
    return {
        "status": "passed",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "sim_read_succeeded",
        "operation_id": contract.operation_id,
        "service_group": "Chart",
        "method": "GET",
        "path": contract.path_template,
        "environment": "SIM",
        "network_call_made": True,
        "network_call_count": page_count,
        "attempt_count": page_count,
        "initial_attempt_count": 1,
        "continuation_attempt_count": page_count - 1,
        "retry_count": 0,
        "distinct_target_count": page_count,
        "successful_page_count": page_count,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": True,
        "trading_ready": False,
        "http_status": 200,
        "response": None,
        "response_visibility": "analytics_contract_receipt",
        "response_fingerprint": _digest(pages),
        "response_fingerprint_scope": "analytics_contract_receipt",
        "analytics_contract_id": contract.contract_id,
        "analytics_contract_sha256": source_contract_fingerprint(contract),
        "page_count": page_count,
        "row_count": page_count,
        "continuation_call_count": page_count - 1,
        "page_receipts": cast("list[JsonValue]", pages),
        "source_quality": quality,
        "source_revision_fingerprint_sha256": _digest(["2" * 64] * page_count),
        "timestamp_value_count": page_count,
        "timestamp_fingerprint_sha256": _digest(["4" * 64] * page_count),
        "request_fingerprint_sha256": _source_request_fingerprint(),
    }


def _source_failure_payload() -> dict[str, JsonValue]:
    contract = source_contracts_by_id()["chart_v3"]
    return {
        "status": "http_error",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "sim_read_http_error",
        "operation_id": contract.operation_id,
        "service_group": "Chart",
        "method": "GET",
        "path": contract.path_template,
        "environment": "SIM",
        "network_call_made": True,
        "network_call_count": 1,
        "attempt_count": 1,
        "initial_attempt_count": 1,
        "continuation_attempt_count": 0,
        "retry_count": 0,
        "distinct_target_count": 1,
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
        "continuation_call_count": 0,
        "reason": "source_http_error",
        "http_status": 500,
        "request_fingerprint_sha256": _source_request_fingerprint(),
    }


async def _source_receipt(payload: dict[str, JsonValue]) -> matrix_module.SourceContractReceipt:
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
async def test_source_receipts_are_status_specific_strict_and_fail_closed() -> None:
    success = _source_success_payload()
    assert (await _source_receipt(success)).source_status == "observed"
    for contradictory in (
        {**success, "live_access": True},
        {**success, "arbitrary_url_allowed": True},
        {**success, "mutation_possible": False},
        {**success, "call_class": "live_read_succeeded"},
    ):
        assert (await _source_receipt(contradictory)).source_status == "refused"

    failure = _source_failure_payload()
    assert (await _source_receipt(failure)).source_status == "reduced"
    for contradictory in (
        {**failure, "http_status": 200},
        {**failure, "call_class": "sim_read_succeeded"},
        {**failure, "mutation_possible": False},
        {**failure, "environment": "LIVE"},
    ):
        assert (await _source_receipt(contradictory)).source_status == "refused"


@pytest.mark.anyio
async def test_attempt_roles_cannot_be_redistributed_between_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impossible = _source_success_payload(page_count=2)
    impossible["initial_attempt_count"] = 2
    impossible["continuation_attempt_count"] = 0
    assert (await _source_receipt(impossible)).source_status == "refused"

    registered = find_registered_endpoint("GET", "/chart/v3/charts")
    assert registered is not None
    first = json.loads((_FIXTURE_ROOT / "chart_page_1.json").read_text(encoding="utf-8"))
    second = json.loads((_FIXTURE_ROOT / "chart_page_2.json").read_text(encoding="utf-8"))
    attempts = 0

    async def execute(
        _operation: object,
        _request_target: str,
        _params: Mapping[str, str],
        **_kwargs: object,
    ) -> RegisteredReadResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            payload = first
            status = 200
        elif attempts == _SECOND_ATTEMPT:
            payload = {"ErrorCode": "TooManyRequests"}
            status = _RATE_LIMIT_STATUS
        else:
            payload = second
            status = 200
        response = httpx2.Response(
            status,
            json=payload,
            headers={"Retry-After": "0"} if status == _RATE_LIMIT_STATUS else None,
            request=httpx2.Request("GET", "https://registered.invalid"),
        )
        return RegisteredReadResponse(
            context=ReadExecutionContext(
                environment="SIM",
                rest_base_url="https://registered.invalid",
                token=None,
            ),
            response=response,
        )

    monkeypatch.setattr(receipt_module, "execute_registered_get", execute)
    receipt = await receipt_module.analytics_contract_receipt(
        registered,
        contract_id="chart_v3",
        params={"AssetType": "Stock", "Count": "2", "Uic": "1001"},
    )
    assert receipt["attempt_count"] == _EXPECTED_ATTEMPTS
    assert receipt["initial_attempt_count"] == 1
    assert receipt["continuation_attempt_count"] == _EXPECTED_CONTINUATION_ATTEMPTS
    assert receipt["retry_count"] == 1
    assert receipt["distinct_target_count"] == _SECOND_ATTEMPT
    assert receipt["successful_page_count"] == _SECOND_ATTEMPT
    assert receipt["continuation_call_count"] == 1


class _ProviderExecutor:
    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = payloads

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        del operation, request_target, params
        return httpx2.Response(
            200,
            content=self.payloads.pop(0),
            request=httpx2.Request("GET", "https://registered.invalid"),
        )


async def _chart_envelope() -> SourceCaptureEnvelope:
    request = {"AssetType": "Stock", "Count": 2, "Uic": 1001}
    capture = build_source_capture_context(
        {"chart_v3": request},
        captured_at=_CAPTURED_AT,
    )
    provider = SaxoAnalyticsProvider(
        request_executor=_ProviderExecutor(
            [
                (_FIXTURE_ROOT / "chart_page_1.json").read_bytes(),
                (_FIXTURE_ROOT / "chart_page_2.json").read_bytes(),
            ],
        ),
    )
    pages = tuple(
        [
            page
            async for page in provider.fetch(
                "chart_v3",
                request,
                capture=capture,
            )
        ],
    )
    return build_source_capture_envelope(capture, pages)


def _revised_envelope(
    envelope: SourceCaptureEnvelope,
    *,
    material_change: bool,
    native_revision: str | None = None,
) -> SourceCaptureEnvelope:
    pages: list[SourcePage] = []
    for index, page in enumerate(envelope.pages):
        values = page.model_dump(mode="python")
        if material_change and index == 0:
            rows = [dict(row) for row in cast("tuple[dict[str, JsonValue], ...]", values["rows"])]
            rows[0] = {**rows[0], "CloseBid": 987.654321}
            values["rows"] = rows
            values["page_fingerprint_sha256"] = source_page_fingerprint(rows)
        if native_revision is not None:
            values["source_revision"] = native_revision
            values["data_version"] = native_revision.removeprefix("data_version:")
        pages.append(SourcePage.model_validate(values, strict=True))
    return build_source_capture_envelope(envelope.capture, pages)


@pytest.mark.anyio
async def test_native_revision_is_logical_identity_not_material_identity(
    tmp_path: Path,
) -> None:
    envelope = await _chart_envelope()
    config = load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )
    store = AnalyticsStore.open(config)
    try:
        first = store.ingest_source_capture(envelope)
        replay = store.ingest_source_capture(envelope)
        assert [page.page_id for page in replay.pages] == [page.page_id for page in first.pages]

        conflicting = _revised_envelope(envelope, material_change=True)
        with pytest.raises(StoreConflictError, match="different material"):
            store.ingest_source_capture(conflicting)

        revised = _revised_envelope(
            envelope,
            material_change=False,
            native_revision="data_version:999",
        )
        accepted = store.ingest_source_capture(revised)
        assert [page.page_id for page in accepted.pages] != [page.page_id for page in first.pages]
        assert {page.source_native_revision for page in accepted.pages} == {
            "data_version:999",
        }
    finally:
        store.close()
