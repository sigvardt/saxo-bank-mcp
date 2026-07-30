from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest

import saxo_bank_mcp.analytics_source_receipt as receipt_module
import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.endpoint_registry import find_registered_endpoint
from saxo_bank_mcp.read_tool_execution import ReadExecutionContext
from saxo_bank_mcp.registered_read_execution import RegisteredReadResponse

_ROOT = Path(__file__).parents[1]
_EXPECTED_POST_ACCESS_ATTEMPTS = 2
_IDENTITY_COMMAND = (
    "from saxo_bank_mcp.qa_analytics_source_matrix import "
    "source_matrix_candidate_identity;"
    "print(source_matrix_candidate_identity().candidate_identity_sha256)"
)


def _run_identity(
    site: Path,
    cwd: Path,
    *,
    prefix: Path | None = None,
    command: str = _IDENTITY_COMMAND,
) -> subprocess.CompletedProcess[str]:
    clean_python_home = cwd.parent / "clean-python-home"
    if not clean_python_home.exists():
        shutil.copytree(
            Path(sys.base_prefix),
            clean_python_home,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
    dependency_site = Path(str(distribution("packaging").locate_file("")))
    search_paths = (site, dependency_site) if prefix is None else (prefix, site, dependency_site)
    python_path = os.pathsep.join(str(path) for path in search_paths)
    prepared_command = (
        "import sys, sysconfig\n"
        "from pathlib import Path as _RuntimePath\n"
        "sys.path[:] = [item for item in sys.path "
        "if _RuntimePath(item).name != 'lib-dynload']\n"
        "sys.path.append(str(sysconfig.get_config_var('DESTSHARED')))\n"
        f"{command}"
    )
    return subprocess.run(
        (sys.executable, "-c", prepared_command),
        cwd=cwd,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHOME": str(clean_python_home),
            "PYTHONPATH": python_path,
        },
        check=False,
        capture_output=True,
        text=True,
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
        "run_analytics_source_matrix.py",
    ):
        shutil.copyfile(_ROOT / "scripts" / name, target / "scripts" / name)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(_ROOT / name, target / name)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def test_candidate_identity_closes_startup_import_and_python_runtime(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    source_copy = tmp_path / "source-copy"
    _copy_source_candidate(source_copy)
    source_site = source_copy / "src"
    foreign_cwd = tmp_path / "foreign-cwd"
    foreign_cwd.mkdir()
    assert _run_identity(source_site, foreign_cwd).returncode == 0

    startup_shadow = tmp_path / "startup-shadow"
    startup_shadow.mkdir()
    (startup_shadow / "sitecustomize.py").write_text(
        "STARTUP_CUSTOMIZATION = True\n",
        encoding="utf-8",
    )
    assert _run_identity(source_site, foreign_cwd, prefix=startup_shadow).returncode != 0
    (startup_shadow / "sitecustomize.py").unlink()
    (startup_shadow / "unrecorded-path.pth").write_text(
        "import unrecorded_startup\n",
        encoding="utf-8",
    )
    assert _run_identity(source_site, foreign_cwd, prefix=startup_shadow).returncode != 0
    (startup_shadow / "unrecorded-path.pth").unlink()
    (startup_shadow / "python._pth").write_text(
        f"{source_site}\nimport site\n",
        encoding="utf-8",
    )
    assert _run_identity(source_site, foreign_cwd, prefix=startup_shadow).returncode != 0
    (startup_shadow / "python._pth").unlink()

    outside = tmp_path / "outside-shadow"
    outside.mkdir()
    (outside / "shadow.py").write_text("SHADOW = True\n", encoding="utf-8")
    source_symlink = source_site / "saxo_bank_mcp" / "symlink_shadow"
    source_symlink.symlink_to(outside, target_is_directory=True)
    assert _run_identity(source_site, foreign_cwd).returncode != 0
    source_symlink.unlink()

    namespace_shadow = tmp_path / "namespace-shadow"
    shadow_package = namespace_shadow / "saxo_bank_mcp"
    shadow_package.mkdir(parents=True)
    shadow_module = shadow_package / "injected_submodule.py"
    shadow_module.write_text("SHADOW = True\n", encoding="utf-8")
    assert _run_identity(source_site, foreign_cwd, prefix=namespace_shadow).returncode != 0
    injected_command = (
        "import importlib.util,sys;"
        f"spec=importlib.util.spec_from_file_location('saxo_bank_mcp.injected_submodule',"
        f"{str(shadow_module)!r});"
        "module=importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(module);"
        "sys.modules['saxo_bank_mcp.injected_submodule']=module;" + _IDENTITY_COMMAND
    )
    assert (
        _run_identity(
            source_site,
            foreign_cwd,
            prefix=namespace_shadow,
            command=injected_command,
        ).returncode
        != 0
    )

    uv = shutil.which("uv")
    assert uv is not None
    wheel_dir = tmp_path / "wheel"
    build = subprocess.run(
        (uv, "build", "--offline", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    site = tmp_path / "installed-site"
    install = subprocess.run(
        (
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(site),
            str(wheel),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    assert _run_identity(site, foreign_cwd).returncode == 0
    (startup_shadow / "sitecustomize.py").write_text(
        "STARTUP_CUSTOMIZATION = True\n",
        encoding="utf-8",
    )
    assert _run_identity(site, foreign_cwd, prefix=startup_shadow).returncode != 0
    (startup_shadow / "sitecustomize.py").unlink()
    installed_symlink = site / "saxo_bank_mcp" / "symlink_shadow"
    installed_symlink.symlink_to(outside, target_is_directory=True)
    assert _run_identity(site, foreign_cwd).returncode != 0
    installed_symlink.unlink()
    assert _run_identity(site, foreign_cwd, prefix=namespace_shadow).returncode != 0
    wrapper = site / "bin" / "saxo-bank-analytics-source-matrix"
    wrapper_mode = wrapper.stat().st_mode
    wrapper.chmod(wrapper_mode & ~0o111)
    assert _run_identity(site, foreign_cwd).returncode != 0
    wrapper.chmod(wrapper_mode)

    runtime_tree = tmp_path / "runtime-tree"
    runtime_tree.mkdir()
    runtime_module = runtime_tree / "module.py"
    runtime_module.write_text("VALUE = 1\n", encoding="utf-8")
    runtime_data = runtime_tree / "runtime-data.txt"
    runtime_data.write_text("version=1\n", encoding="utf-8")
    project_tree = getattr(matrix_module, "_runtime_tree_projection", None)
    assert callable(project_tree)
    initial_tree = project_tree(runtime_tree)
    runtime_data.write_text("version=2\n", encoding="utf-8")
    assert project_tree(runtime_tree) != initial_tree
    runtime_data.write_text("version=1\n", encoding="utf-8")
    runtime_module.write_text("VALUE = 2\n", encoding="utf-8")
    assert project_tree(runtime_tree) != initial_tree
    runtime_symlink = runtime_tree / "linked-runtime"
    runtime_symlink.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        project_tree(runtime_tree)

    runtime_binary = tmp_path / "python-runtime"
    runtime_binary.write_bytes(Path(sys.executable).resolve(strict=True).read_bytes())
    project_executable = getattr(matrix_module, "_runtime_file_projection", None)
    assert callable(project_executable)
    initial_binary = project_executable(runtime_binary)
    runtime_binary.write_bytes(runtime_binary.read_bytes() + b"\nmutation")
    assert project_executable(runtime_binary) != initial_binary

    project_roots = getattr(matrix_module, "_execution_root_projection_sha256", None)
    assert callable(project_roots)
    initial_roots = project_roots()
    new_import_root = tmp_path / "late-import-root"
    new_import_root.mkdir()
    sys.path.append(str(new_import_root))
    try:
        assert project_roots() != initial_roots
    finally:
        sys.path.pop()


def test_candidate_identity_refuses_unsealed_python_cache_and_search_symlink(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    source_copy = tmp_path / "source-copy"
    _copy_source_candidate(source_copy)
    source_site = source_copy / "src"
    foreign_cwd = tmp_path / "foreign-cwd"
    foreign_cwd.mkdir()
    assert _run_identity(source_site, foreign_cwd).returncode == 0

    source_cache = source_site / "saxo_bank_mcp" / "__pycache__"
    source_cache.mkdir()
    (source_cache / "unsealed.cpython-312.pyc").write_bytes(b"unsealed-cache")
    assert _run_identity(source_site, foreign_cwd).returncode != 0
    shutil.rmtree(source_cache)

    startup_search = tmp_path / "startup-search"
    startup_search.mkdir()
    assert _run_identity(source_site, foreign_cwd, prefix=startup_search).returncode == 0
    startup_cache = startup_search / "__pycache__"
    startup_cache.mkdir()
    (startup_cache / "unsealed.cpython-312.pyc").write_bytes(b"unsealed-cache")
    assert _run_identity(source_site, foreign_cwd, prefix=startup_search).returncode != 0
    shutil.rmtree(startup_cache)
    outside = tmp_path / "outside"
    outside.mkdir()
    startup_symlink = startup_search / "unsealed-search-link"
    startup_symlink.symlink_to(outside, target_is_directory=True)
    assert _run_identity(source_site, foreign_cwd, prefix=startup_search).returncode != 0
    startup_symlink.unlink()

    runtime_tree = tmp_path / "runtime-tree"
    runtime_tree.mkdir()
    (runtime_tree / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    project_tree = getattr(matrix_module, "_runtime_tree_projection", None)
    assert callable(project_tree)
    for relative in (
        Path("__pycache__/module.cpython-312.pyc"),
        Path("module.pyc"),
        Path("module.pyo"),
    ):
        cache_entry = runtime_tree / relative
        cache_entry.parent.mkdir(parents=True, exist_ok=True)
        cache_entry.write_bytes(b"unsealed-cache")
        with pytest.raises(ValueError, match="cache"):
            project_tree(runtime_tree)
        cache_entry.unlink()
        if cache_entry.parent != runtime_tree:
            cache_entry.parent.rmdir()

    uv = shutil.which("uv")
    assert uv is not None
    wheel_dir = tmp_path / "wheel"
    build = subprocess.run(
        (uv, "build", "--offline", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    installed_site = tmp_path / "installed-site"
    install = subprocess.run(
        (
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(installed_site),
            str(wheel),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    assert _run_identity(installed_site, foreign_cwd).returncode == 0

    package_cache = installed_site / "saxo_bank_mcp" / "__pycache__"
    package_cache.mkdir()
    (package_cache / "unsealed.cpython-312.pyc").write_bytes(b"unsealed-cache")
    assert _run_identity(installed_site, foreign_cwd).returncode != 0
    shutil.rmtree(package_cache)

    dependency_root = tmp_path / "dependency-root"
    dependency_root.mkdir()
    dependency_module = dependency_root / "dependency.py"
    dependency_module.write_text("VALUE = 1\n", encoding="utf-8")
    scan_import_root = getattr(matrix_module, "_scan_recorded_import_root", None)
    assert callable(scan_import_root)
    scan_import_root(
        dependency_root,
        allowed_files={dependency_module.resolve(strict=True)},
        allowed_unsealed_files=set(),
    )
    dependency_cache = dependency_root / "__pycache__"
    dependency_cache.mkdir()
    (dependency_cache / "dependency.cpython-312.pyc").write_bytes(b"unsealed-cache")
    with pytest.raises(ValueError, match="cache"):
        scan_import_root(
            dependency_root,
            allowed_files={dependency_module.resolve(strict=True)},
            allowed_unsealed_files=set(),
        )


def test_candidate_identity_seals_ordered_search_directories_during_verification(
    tmp_path: Path,
) -> None:
    source_copy = tmp_path / "source-copy"
    _copy_source_candidate(source_copy)
    source_site = source_copy / "src"
    foreign_cwd = tmp_path / "foreign-cwd"
    foreign_cwd.mkdir()
    assert _run_identity(source_site, foreign_cwd).returncode == 0

    mutations: list[tuple[str, Path, str]] = []
    for name in ("added", "removed", "reordered", "metadata"):
        search_root = tmp_path / f"{name}-search-root"
        search_root.mkdir()
        if name == "metadata":
            (search_root / "nested").mkdir()
        late_root = tmp_path / f"{name}-late-root"
        late_root.mkdir()
        setup = f"sys.path.insert(0, {str(late_root)!r})" if name == "reordered" else "pass"
        mutation = {
            "added": f"sys.path.append({str(late_root)!r})",
            "removed": f"sys.path.remove({str(search_root)!r})",
            "reordered": (
                f"first = sys.path.index({str(search_root)!r})\n"
                f"    second = sys.path.index({str(late_root)!r})\n"
                "    sys.path[first], sys.path[second] = sys.path[second], sys.path[first]"
            ),
            "metadata": (
                f"Path({str(search_root)!r}, 'nested', 'late.pyc').write_bytes(b'unsealed-cache')"
            ),
        }[name]
        command = (
            "import sys\n"
            "from pathlib import Path\n"
            "import saxo_bank_mcp.qa_analytics_source_matrix as matrix\n"
            f"{setup}\n"
            "original = matrix._validate_import_execution_closure\n"
            "def mutate(*args, **kwargs):\n"
            "    result = original(*args, **kwargs)\n"
            f"    {mutation}\n"
            "    return result\n"
            "matrix._validate_import_execution_closure = mutate\n"
            "print(matrix.source_matrix_candidate_identity().candidate_identity_sha256)\n"
        )
        mutations.append((name, search_root, command))

    for name, search_root, command in mutations:
        result = _run_identity(
            source_site,
            foreign_cwd,
            prefix=search_root,
            command=command,
        )
        assert result.returncode != 0, (name, result.stdout, result.stderr)


class _ReceiptClient:
    def __init__(self, payload: dict[str, JsonValue]) -> None:
        self.payload = payload

    async def call_tool(
        self,
        _name: str,
        _arguments: dict[str, JsonValue],
        *,
        raise_on_error: bool,
    ) -> SimpleNamespace:
        assert raise_on_error is False
        return SimpleNamespace(structured_content=self.payload)


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
    return await matrix_module._run_provider_source(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        cast("matrix_module.MatrixClient", _ReceiptClient(payload)),
        source_contracts_by_id()["chart_v3"],
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
