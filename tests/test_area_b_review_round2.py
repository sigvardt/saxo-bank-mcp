from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest

import saxo_bank_mcp.analytics_provider as provider_module
import saxo_bank_mcp.analytics_source_contracts as contracts_module
import saxo_bank_mcp.analytics_source_receipt as receipt_module
import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider, SourceProviderError
from saxo_bank_mcp.analytics_source_contracts import (
    SourceCaptureContext,
    SourcePage,
    build_source_capture_context,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreValidationError
from saxo_bank_mcp.endpoint_registry import EndpointOperation, find_registered_endpoint
from saxo_bank_mcp.read_tool_types import ReadExecutionContext
from saxo_bank_mcp.registered_read_execution import RegisteredReadResponse

_CAPTURED_AT = datetime(2026, 7, 30, 12, tzinfo=UTC)
_FIXTURE_ROOT = Path(__file__).parent / "fixtures/analytics/saxo_pages"
_EXPECTED_RETRY_ATTEMPTS = 2


def _identity() -> matrix_module.SourceMatrixCandidateIdentity:
    return matrix_module.SourceMatrixCandidateIdentity(
        source_contract_catalog_sha256="a" * 64,
        harness_build_sha256="b" * 64,
        candidate_identity_sha256="c" * 64,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


class _Executor:
    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = payloads

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        _ = operation, request_target, params
        return httpx2.Response(
            200,
            content=self.payloads.pop(0),
            request=httpx2.Request("GET", "https://registered.invalid"),
        )


async def _chart_pages(
    capture: SourceCaptureContext,
) -> tuple[SourcePage, ...]:
    provider = SaxoAnalyticsProvider(
        request_executor=_Executor(
            [
                (_FIXTURE_ROOT / "chart_page_1.json").read_bytes(),
                (_FIXTURE_ROOT / "chart_page_2.json").read_bytes(),
            ],
        ),
    )
    request = {"AssetType": "Stock", "Count": 2, "Uic": 1001}
    return tuple(
        [page async for page in provider.fetch("chart_v3", request, capture=capture)],
    )


def test_candidate_identity_rejects_mutation_anywhere_in_installed_executable(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel"
    uv = shutil.which("uv")
    assert uv is not None
    build = subprocess.run(
        (uv, "build", "--offline", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    installed = tmp_path / "installed"
    install = subprocess.run(
        (
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    command = (
        sys.executable,
        "-c",
        (
            "from saxo_bank_mcp.qa_analytics_source_matrix import "
            "source_matrix_candidate_identity;"
            "print(source_matrix_candidate_identity().candidate_identity_sha256)"
        ),
    )
    environment = {**os.environ, "PYTHONPATH": str(installed)}
    baseline = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert baseline.returncode == 0, baseline.stderr

    dist_info = next(installed.glob("*.dist-info"))
    targets = (
        installed / "saxo_bank_mcp" / "server.py",
        installed / "saxo_bank_mcp" / "endpoint_registry.py",
        installed / "saxo_bank_mcp" / "live_token_refresh.py",
        installed / "saxo_bank_mcp" / "read_tool_results.py",
        installed / "saxo_bank_mcp" / "secret_scan.py",
        installed / "saxo_bank_mcp" / "_analytics_source_contracts" / "source_contracts.json",
        dist_info / "entry_points.txt",
        dist_info / "METADATA",
        installed / "bin" / "saxo-bank-analytics-source-matrix",
    )
    for target in targets:
        original = target.read_bytes()
        target.write_bytes(original + b"\n# installed artifact mutation\n")
        tampered = subprocess.run(
            command,
            cwd=tmp_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        target.write_bytes(original)
        assert tampered.returncode != 0, target


def test_guard_root_ignores_process_environment_and_claim_is_durable_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_home = tmp_path / "owner-home"
    monkeypatch.setenv("HOME", str(tmp_path / "forged-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "forged-state"))
    root = matrix_module._source_matrix_state_root(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        owner_home=owner_home,
    )
    assert root.is_relative_to(owner_home)

    guard = matrix_module.candidate_guard_path(root, _identity().candidate_identity_sha256)

    def claim_once(_index: int) -> bool:
        return matrix_module._claim_candidate_guard(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            guard,
            _identity(),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = tuple(pool.map(claim_once, range(32)))
    assert sum(claims) == 1

    fsync_targets: list[bool] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        fsync_targets.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(matrix_module.os, "fsync", record_fsync)
    second_guard = matrix_module.candidate_guard_path(root, "d" * 64)
    assert (
        matrix_module._claim_candidate_guard(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            second_guard,
            _identity(),
        )
        is True
    )
    assert fsync_targets[-1] is True


def test_readiness_requires_exact_safe_sim_receipts() -> None:
    auth: dict[str, JsonValue] = {
        "status": "passed",
        "tool_name": "forged_tool",
        "requested_environment": "SIM",
        "effective_read_environment": "SIM",
        "live_reads": False,
        "live_writes": False,
        "token_cache_present": True,
        "token_cache_readable": True,
        "token_cache_expired": False,
        "token_cache_environment": "SIM",
        "blocking_reasons": [],
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }
    assert (
        matrix_module._auth_receipt_status(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            auth,
        )
        != "ready"
    )

    valid_network_receipt: dict[str, JsonValue] = {
        "status": "passed",
        "tool_name": "saxo_get_session_capabilities",
        "environment": "SIM",
        "call_class": "sim_read_succeeded",
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
    validator = getattr(matrix_module, "_network_read_receipt_status", None)
    assert validator is not None
    assert (
        validator(
            valid_network_receipt,
            "saxo_get_session_capabilities",
        )
        == "passed"
    )
    for update in (
        {"tool_name": "saxo_get_entitlements"},
        {"environment": "LIVE"},
        {"call_class": "live_read_succeeded"},
        {"network_call_made": False},
        {"live_write_called": True},
        {"order_or_subscription_created": True},
    ):
        assert (
            validator(
                {**valid_network_receipt, **update},
                "saxo_get_session_capabilities",
            )
            != "passed"
        )


@pytest.mark.anyio
async def test_source_and_ledger_receipts_recompute_all_declared_proof() -> None:
    contract = source_contracts_by_id()["chart_v3"]
    request = {"AssetType": "Stock", "Count": 2, "Uic": 1001}
    page_receipts: list[dict[str, JsonValue]] = [
        {
            "page_number": 1,
            "row_count": 1,
            "page_fingerprint_sha256": "1" * 64,
            "source_revision_sha256": "2" * 64,
            "schema_fingerprint_sha256": "3" * 64,
            "timestamp_value_count": 1,
            "timestamp_fingerprint_sha256": "4" * 64,
        },
    ]
    payload: dict[str, JsonValue] = {
        "status": "passed",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "sim_read_succeeded",
        "operation_id": contract.operation_id,
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
        "successful_page_count": 1,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "response": None,
        "response_visibility": "analytics_contract_receipt",
        "response_fingerprint": _sha256(page_receipts),
        "response_fingerprint_scope": "analytics_contract_receipt",
        "analytics_contract_id": contract.contract_id,
        "analytics_contract_sha256": source_contract_fingerprint(contract),
        "page_count": 1,
        "row_count": 99,
        "continuation_call_count": 0,
        "page_receipts": cast("list[JsonValue]", page_receipts),
        "source_revision_fingerprint_sha256": _sha256(["native-revision"]),
        "timestamp_value_count": 1,
        "timestamp_fingerprint_sha256": _sha256(["timestamp-proof"]),
        "request_fingerprint_sha256": "5" * 64,
        "http_status": 200,
    }

    class Client:
        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, JsonValue],
            *,
            raise_on_error: bool,
        ) -> SimpleNamespace:
            assert raise_on_error is False
            return SimpleNamespace(structured_content=payload)

    receipt = await matrix_module._run_provider_source(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        cast("matrix_module.MatrixClient", Client()),
        contract,
        request,
    )
    assert receipt.source_status == "refused"
    assert receipt.outcome_reason == "analytics_contract_receipt_invalid"

    ledger = matrix_module._ledger_receipt(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        {
            "status": "passed",
            "tool_name": "saxo_get_safe_request_ledger",
            "ledger_complete": True,
            "negative_proof_available": True,
            "events_evicted": 0,
            "request_count": 2,
            "non_get_request_count": 0,
            "events": [
                {
                    "phase": "attempted",
                    "host_role": "gateway",
                    "environment": "SIM",
                    "method": "GET",
                },
            ],
        },
    )
    assert ledger.sim_only is False


@pytest.mark.anyio
async def test_retry_attempts_are_not_counted_as_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def execute(
        _operation: object,
        _request_target: str,
        _params: Mapping[str, str],
        **_kwargs: object,
    ) -> RegisteredReadResponse:
        nonlocal attempts
        attempts += 1
        response = (
            httpx2.Response(
                429,
                content=b'{"ErrorCode":"TooManyRequests"}',
                headers={"Retry-After": "0"},
                request=httpx2.Request("GET", "https://registered.invalid"),
            )
            if attempts == 1
            else httpx2.Response(
                200,
                content=(_FIXTURE_ROOT / "chart_page_2.json").read_bytes(),
                request=httpx2.Request("GET", "https://registered.invalid"),
            )
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
    registered = find_registered_endpoint("GET", "/chart/v3/charts")
    assert registered is not None
    receipt = await receipt_module.analytics_contract_receipt(
        registered,
        contract_id="chart_v3",
        params={"AssetType": "Stock", "Count": "2", "Uic": "1001"},
    )

    assert receipt["attempt_count"] == _EXPECTED_RETRY_ATTEMPTS
    assert receipt["initial_attempt_count"] == _EXPECTED_RETRY_ATTEMPTS
    assert receipt["continuation_attempt_count"] == 0
    assert receipt["retry_count"] == 1
    assert receipt["distinct_target_count"] == 1
    assert receipt["successful_page_count"] == 1
    assert receipt["continuation_call_count"] == 0


@pytest.mark.anyio
async def test_stable_keys_are_nonempty_unique_across_raw_pages_and_explicit() -> None:
    first = json.loads((_FIXTURE_ROOT / "chart_page_1.json").read_text(encoding="utf-8"))
    second = json.loads((_FIXTURE_ROOT / "chart_page_2.json").read_text(encoding="utf-8"))
    second["Data"][0]["Time"] = first["Data"][0]["Time"]
    provider = SaxoAnalyticsProvider(
        request_executor=_Executor(
            [
                json.dumps(first).encode(),
                json.dumps(second).encode(),
            ],
        ),
    )
    error_type = getattr(
        provider_module,
        "SourceStableKeyError",
        SourceProviderError,
    )
    with pytest.raises(error_type) as caught:
        _ = [
            page
            async for page in provider.fetch(
                "chart_v3",
                {"AssetType": "Stock", "Count": 2, "Uic": 1001},
            )
        ]
    assert caught.value.code == "source_stable_key_invalid"
    assert provider.quarantine_reason("instrument_price_return") == (
        "source_stable_key_invalid:chart_v3"
    )
    assert all(
        contract.stable_key_policy in {"required_unique", "unkeyed_snapshot"}
        for contract in source_contracts_by_id().values()
    )


@pytest.mark.anyio
async def test_capture_envelope_authenticates_replay_and_native_revision(
    tmp_path: Path,
) -> None:
    request = {"AssetType": "Stock", "Count": 2, "Uic": 1001}
    capture = build_source_capture_context(
        {"chart_v3": request},
        captured_at=_CAPTURED_AT,
    )
    first_pages = await _chart_pages(capture)
    replay_pages = await _chart_pages(capture)
    assert [page.source_revision for page in first_pages] == [
        page.source_revision for page in replay_pages
    ]

    builder = getattr(contracts_module, "build_source_capture_envelope", None)
    assert builder is not None
    envelope = builder(capture, first_pages)
    tampered_page = first_pages[0].model_copy(
        update={"request_fingerprint_sha256": "f" * 64},
    )
    tampered = envelope.model_copy(
        update={"pages": (tampered_page, *first_pages[1:])},
    )
    config = load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )
    store = AnalyticsStore.open(config)
    try:
        with pytest.raises(StoreValidationError, match="capture envelope"):
            store.ingest_source_capture(tampered)
        first = store.ingest_source_capture(envelope)
        replay = store.ingest_source_capture(envelope)
    finally:
        store.close()
    assert first.dataset.dataset_id == replay.dataset.dataset_id
    assert [page.page_id for page in first.pages] == [page.page_id for page in replay.pages]
