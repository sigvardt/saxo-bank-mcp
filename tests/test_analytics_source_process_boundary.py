# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import Mock

import anyio
import pytest

from saxo_bank_mcp import analytics_source_process, qa_analytics_source_matrix
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import source_contract_catalog_sha256
from saxo_bank_mcp.analytics_source_process import (
    CHILD_BOOTSTRAP,
    SOURCE_MATRIX_CHILD_TOOLS,
    ChildBootstrapPaths,
    ChildConfigurationError,
    ChildLaunchConfig,
    MatrixCallPolicy,
    MatrixSession,
    OneShotProcessSession,
    ProcessSessionError,
    RegisteredCallProfile,
    RegisteredResponseMode,
    build_child_launch_config,
)

if TYPE_CHECKING:
    from saxo_bank_mcp.analytics_source_runtime import (
        CandidateRuntimeSeal,
        ExternalRunLayout,
    )

_NONZERO_FIXTURE_EXIT_CODE: Final = 17
_SEALED_DIRECTORY_MODE: Final = 0o500
_SEALED_FILE_MODE: Final = 0o400
_ROOT: Final = Path(__file__).parents[1]
QA_MATRIX_PATH: Final = Path("src/saxo_bank_mcp/qa_analytics_source_matrix.py")
ROUND7_PATH: Final = Path("tests/test_area_b_review_round7.py")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    ).hexdigest()


def _fixture_portable_entries(root: Path) -> list[dict[str, str]]:
    paths = (root, *sorted(root.rglob("*"), key=os.fsencode))
    entries: list[dict[str, str]] = []
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"mode": "0500", "path": relative, "type": "directory"})
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(
                {
                    "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                    "path": relative,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "type": "file",
                },
            )
        elif stat.S_ISLNK(metadata.st_mode):
            entries.append(
                {
                    "mode": "0777",
                    "path": relative,
                    "target": os.fspath(path.readlink()),
                    "type": "symlink",
                },
            )
        else:
            raise AssertionError("fixture runtime contains an unsupported entry")
    return entries


def _fixture_manifest_text(root: Path) -> str:
    executable_sha256 = hashlib.sha256((root / "bin/python3.12").read_bytes()).hexdigest()
    payload_sha256 = hashlib.sha256(
        (root / "lib/python3.12/site-packages/payload.py").read_bytes(),
    ).hexdigest()
    portable_entries = _fixture_portable_entries(root)
    portable_sha256 = _canonical_digest(portable_entries)
    source_files = {"src/saxo_bank_mcp/payload.py": payload_sha256}
    installed_files = {"payload.py": payload_sha256}
    dependency_distributions = {
        "fixture-dependency": {
            "files": {"payload.py": payload_sha256},
            "installer_metadata": {"INSTALLER": hashlib.sha256(b"uv").hexdigest()},
            "version": "1.0",
        },
    }
    runtime_identity = {
        "base_executable_sha256": executable_sha256,
        "build_config_sha256": "1" * 64,
        "cache_tag": "cpython-312",
        "executable_projection_sha256": executable_sha256,
        "executable_sha256": executable_sha256,
        "implementation": "cpython",
        "importable_suffixes": [".py"],
        "interpreter_policy_sha256": "2" * 64,
        "platstdlib_file_count": 1,
        "platstdlib_merkle_sha256": "3" * 64,
        "platform": "fixture",
        "python_build_sha256": "4" * 64,
        "python_version": "3.12.0",
        "shared_runtime_file_count": 0,
        "shared_runtime_sha256": "5" * 64,
        "startup_configuration_file_count": 0,
        "startup_configuration_sha256": "6" * 64,
        "stdlib_file_count": 1,
        "stdlib_merkle_sha256": "7" * 64,
    }
    source_build_sha256 = _canonical_digest(source_files)
    installed_build_sha256 = _canonical_digest(installed_files)
    installed_exclusions = (
        "saxo_bank_mcp/_analytics_source_matrix/source_matrix_candidate.json",
        "saxo_bank_mcp-0.1.0.dist-info/RECORD",
    )
    source_exclusions = ("data/analytics/source_matrix_candidate.json",)
    console_scripts = {
        "saxo-bank-analytics-source-matrix": "saxo_bank_mcp.qa_analytics_source_matrix:main",
        "saxo-bank-analytics-source-matrix-generate": (
            "saxo_bank_mcp.generate_analytics_source_matrix_candidate:main"
        ),
    }
    child_bootstrap_sha256 = hashlib.sha256(CHILD_BOOTSTRAP.encode("utf-8")).hexdigest()
    harness_build_sha256 = _canonical_digest(
        {
            "child_bootstrap_sha256": child_bootstrap_sha256,
            "console_scripts": console_scripts,
            "dependency_distributions": dependency_distributions,
            "installed_build_sha256": installed_build_sha256,
            "installed_exclusions": installed_exclusions,
            "installed_metadata_projection": {
                "INSTALLER": hashlib.sha256(b"uv").hexdigest(),
            },
            "portable_runtime_entry_count": len(portable_entries),
            "portable_runtime_tree_sha256": portable_sha256,
            "runtime_identity": runtime_identity,
            "schema_version": "5",
            "source_build_sha256": source_build_sha256,
            "source_exclusions": source_exclusions,
            "source_wheel_projection_sha256": "8" * 64,
        },
    )
    catalog_sha256 = source_contract_catalog_sha256()
    candidate_identity_sha256 = _canonical_digest(
        {
            "harness_build_sha256": harness_build_sha256,
            "source_contract_catalog_sha256": catalog_sha256,
        },
    )
    return (
        json.dumps(
            {
                "candidate_identity_sha256": candidate_identity_sha256,
                "child_bootstrap_sha256": child_bootstrap_sha256,
                "console_scripts": console_scripts,
                "dependency_distributions": dependency_distributions,
                "harness_build_sha256": harness_build_sha256,
                "installed_build_sha256": installed_build_sha256,
                "installed_exclusions": installed_exclusions,
                "installed_files": installed_files,
                "installed_metadata_projection": {
                    "INSTALLER": hashlib.sha256(b"uv").hexdigest(),
                },
                "portable_runtime_entry_count": len(portable_entries),
                "portable_runtime_tree_sha256": portable_sha256,
                "runtime_identity": runtime_identity,
                "schema_version": "5",
                "source_build_sha256": source_build_sha256,
                "source_contract_catalog_sha256": catalog_sha256,
                "source_exclusions": source_exclusions,
                "source_files": source_files,
                "source_wheel_projection_sha256": "8" * 64,
            },
            sort_keys=True,
        )
        + "\n"
    )


def _make_tree_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)


@dataclass(frozen=True, slots=True)
class SealedRuntimeFixture:
    root: Path
    manifest_text: str
    layout: ExternalRunLayout
    payload: Path
    ancestor: Path

    def open_seal(self) -> CandidateRuntimeSeal:
        runtime_module = importlib.import_module("saxo_bank_mcp.analytics_source_runtime")
        return runtime_module._open_candidate_runtime_seal_for_test(  # noqa: SLF001
            self.root,
            self.manifest_text,
            self.layout,
        )

    def apply(self, mutation: str, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime_module = importlib.import_module("saxo_bank_mcp.analytics_source_runtime")
        if mutation == "owner":
            monkeypatch.setattr(runtime_module, "_expected_owner_uid", lambda: os.getuid() + 1)
            return
        if mutation == "ancestor":
            replacement = self.ancestor.with_name(f"{self.ancestor.name}.real")
            self.ancestor.rename(replacement)
            self.ancestor.symlink_to(replacement.name, target_is_directory=True)
            return
        target = self.payload
        if mutation == "content":
            target.chmod(0o600)
            target.write_bytes(target.read_bytes() + b"mutation")
            target.chmod(0o400)
        elif mutation == "type":
            target.parent.chmod(0o700)
            target.unlink()
            target.mkdir(mode=0o500)
            target.parent.chmod(0o500)
        elif mutation == "mode":
            target.chmod(0o600)
        elif mutation == "link":
            target.parent.chmod(0o700)
            target.unlink()
            target.symlink_to("../../../../bin/python3.12")
            target.parent.chmod(0o500)
        elif mutation in {"cache", "extra_file"}:
            target.parent.chmod(0o700)
            created = target.parent / ("__pycache__" if mutation == "cache" else "unlisted.py")
            if mutation == "cache":
                created.mkdir(mode=0o500)
            else:
                created.write_text("UNLISTED = True\n", encoding="utf-8")
                created.chmod(0o400)
            target.parent.chmod(0o500)
        elif mutation == "interpreter":
            interpreter = self.root / "bin/python3.12"
            interpreter.parent.chmod(0o700)
            interpreter.chmod(0o600)
            interpreter.write_bytes(Path(sys.executable).read_bytes())
            interpreter.chmod(0o500)
            interpreter.parent.chmod(0o500)
        else:
            raise AssertionError(f"unsupported mutation: {mutation}")


@pytest.fixture
def sealed_runtime(tmp_path: Path) -> Iterator[SealedRuntimeFixture]:
    runtime_module = importlib.import_module("saxo_bank_mcp.analytics_source_runtime")
    ancestor = tmp_path / "sealed-parent"
    root = ancestor / "runtime"
    payload = root / "lib/python3.12/site-packages/payload.py"
    payload.parent.mkdir(parents=True)
    executable = root / "bin/python3.12"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture-interpreter\n")
    payload.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "bin/python").symlink_to("python3.12")
    (root / "bin/python3").symlink_to("python3.12")
    executable.chmod(0o500)
    payload.chmod(0o400)
    for directory in (
        payload.parent,
        payload.parent.parent,
        payload.parent.parent.parent,
        executable.parent,
        root,
    ):
        directory.chmod(0o500)
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    children = tuple(run_root / name for name in (
        "coordinator-cache",
        "coordinator-work",
        "coordinator-tmp",
        "child-cache",
        "child-work",
        "child-tmp",
    ))
    for child in children:
        child.mkdir(mode=0o700)
    layout = runtime_module.ExternalRunLayout(run_root, *children)
    fixture = SealedRuntimeFixture(
        root=root,
        manifest_text=_fixture_manifest_text(root),
        layout=layout,
        payload=payload,
        ancestor=ancestor,
    )
    try:
        yield fixture
    finally:
        for candidate in (ancestor, ancestor.with_name(f"{ancestor.name}.real")):
            _make_tree_writable(candidate)


@dataclass(frozen=True, slots=True)
class PreparedRuntimeFixture:
    root: Path
    portable_identity: str


@pytest.fixture(scope="module")
def two_prepared_runtimes(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[PreparedRuntimeFixture, PreparedRuntimeFixture]]:
    temp_root = tmp_path_factory.mktemp("two-sealed-runtimes")
    source_root = temp_root / "source"
    shutil.copytree(
        _ROOT,
        source_root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".ruff_cache",
            ".pytest_cache",
            ".basedpyright",
            "__pycache__",
            "*.pyc",
            "*.pyo",
        ),
    )
    uv = shutil.which("uv")
    assert uv is not None
    uv_path = Path(uv).resolve(strict=True)
    wheel_dir = temp_root / "wheel"
    build = subprocess.run(
        (str(uv_path), "build", "--offline", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=source_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl")).resolve(strict=True)
    prepared_items: list[PreparedRuntimeFixture] = []
    for index in range(2):
        runtime = temp_root / f"runtime-{index}"
        prepared = subprocess.run(
            (
                sys.executable,
                str(source_root / "scripts/prepare_analytics_source_matrix_runtime.py"),
                "--runtime",
                str(runtime),
                "--wheel",
                str(wheel),
                "--uv",
                str(uv_path),
            ),
            cwd=source_root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert prepared.returncode == 0, prepared.stderr
        manifest = temp_root / f"candidate-{index}.json"
        generated = subprocess.run(
            (
                str(runtime / "bin/saxo-bank-analytics-source-matrix-generate"),
                "--repository-root",
                str(source_root),
                "--wheel",
                str(wheel),
                "--out",
                str(manifest),
            ),
            cwd=temp_root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert generated.returncode == 0, generated.stderr
        identity = json.loads(manifest.read_text(encoding="utf-8"))[
            "candidate_identity_sha256"
        ]
        prepared_items.append(PreparedRuntimeFixture(runtime, identity))
    try:
        yield cast(
            "tuple[PreparedRuntimeFixture, PreparedRuntimeFixture]",
            tuple(prepared_items),
        )
    finally:
        for prepared in prepared_items:
            _make_tree_writable(prepared.root)


@pytest.fixture
def sealed_child_paths(tmp_path: Path) -> ChildBootstrapPaths:
    runtime = tmp_path / "runtime"
    executable = runtime / "bin/python3.12"
    site_packages = runtime / "lib/python3.12/site-packages"
    executable.parent.mkdir(parents=True, mode=0o700)
    site_packages.mkdir(parents=True, mode=0o700)
    shutil.copy2(sys.executable, executable)
    executable.chmod(0o500)
    for directory in (
        site_packages,
        site_packages.parent,
        site_packages.parent.parent,
        runtime / "bin",
        runtime,
    ):
        directory.chmod(0o500)
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    cache = run_root / "child-cache"
    work = run_root / "child-work"
    temp = run_root / "child-tmp"
    for directory in (cache, work, temp):
        directory.mkdir(mode=0o700)
    return ChildBootstrapPaths(runtime, executable, site_packages, cache, work, temp)


@pytest.fixture
def exact_sim_env(tmp_path: Path) -> dict[str, str]:
    return {
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
        "SAXO_MCP_SIM_APP_KEY": "fixture-app-key",
        "SAXO_MCP_SIM_REDIRECT_URI": "http://localhost:8080/callback",
        "SAXO_MCP_TOKEN_CACHE_PATH": str(tmp_path / "token-cache.json"),
    }


@pytest.fixture
def stdio_fixture_config(tmp_path: Path) -> ChildLaunchConfig:
    fixture = Path("tests/fixtures/analytics/source_matrix_stdio_child.py").resolve(strict=True)
    executable = Path(sys.executable).resolve(strict=True)
    return ChildLaunchConfig(
        command=(str(executable), "-I", "-B", "-S", str(fixture), "normal"),
        environment={"LANG": "C", "LC_ALL": "C"},
        cwd=tmp_path,
        executable_identity_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )


def _fixture_scenario(config: ChildLaunchConfig, scenario: str) -> ChildLaunchConfig:
    return replace(config, command=(*config.command[:-1], scenario))


async def _emergency_cleanup_cancelled_session(session: OneShotProcessSession) -> None:
    """Keep an intended RED cancellation failure from leaking its fixture child."""
    task_group = session._task_group  # noqa: SLF001
    if task_group is not None:
        task_group.cancel_scope.shield = True
    if session._client_session is not None and session._mcp_session_count == 1:  # noqa: SLF001
        session._client_session_entered = True  # noqa: SLF001
        with suppress(BaseException):
            await session._close_client_session()  # noqa: SLF001
    with suppress(BaseException):
        await session._close_outgoing_stream()  # noqa: SLF001
    session._close_parent_stdin()  # noqa: SLF001
    with suppress(BaseException):
        session._child_exit_code = await session._terminate_and_reap_same_child()  # noqa: SLF001
    with suppress(BaseException):
        await session._finalize_resources()  # noqa: SLF001
    session._state = "exited"  # noqa: SLF001


@pytest.mark.anyio
async def test_process_session_spawns_one_distinct_child_over_stdio(
    stdio_fixture_config: ChildLaunchConfig,
) -> None:
    session = OneShotProcessSession(stdio_fixture_config)
    await session.spawn()
    await session.initialize()
    assert await session.list_tools_once() == SOURCE_MATRIX_CHILD_TOOLS
    facts = await session.close()
    assert facts.child_pid != facts.coordinator_pid
    assert facts.stdin_identity.endpoint_kind == "fifo"
    assert facts.stdout_identity.endpoint_kind == "fifo"
    assert facts.stdin_identity != facts.stdout_identity
    assert facts.child_spawn_count == facts.mcp_session_count == 1
    assert facts.mcp_initialize_count == facts.tool_list_count == 1
    assert facts.child_exit_code == 0


@pytest.mark.anyio
async def test_process_session_cannot_initialize_connect_or_spawn_twice(
    stdio_fixture_config: ChildLaunchConfig,
) -> None:
    session = OneShotProcessSession(stdio_fixture_config)
    await session.spawn()
    with pytest.raises(ProcessSessionError):
        await session.spawn()
    await session.initialize()
    with pytest.raises(ProcessSessionError):
        await session.initialize()
    await session.list_tools_once()
    with pytest.raises(ProcessSessionError):
        await session.list_tools_once()
    facts = await session.close()
    assert facts.reconnect_count == 0
    assert facts.restart_count == 0


@pytest.mark.anyio
async def test_process_session_close_cancellation_reaps_child_and_exits(
    stdio_fixture_config: ChildLaunchConfig,
) -> None:
    session = OneShotProcessSession(stdio_fixture_config)
    cancelled = False
    state_after_cancellation = ""
    child_running_after_cancellation = True
    with anyio.CancelScope() as caller_scope:
        await session.spawn()
        await session.initialize()
        await session.list_tools_once()
        process = session._process  # noqa: SLF001
        assert process is not None
        caller_scope.cancel()
        try:
            await session.close()
        except anyio.get_cancelled_exc_class():
            cancelled = True
        finally:
            state_after_cancellation = session._state  # noqa: SLF001
            child_running_after_cancellation = process.returncode is None
            if state_after_cancellation != "exited" or child_running_after_cancellation:
                await _emergency_cleanup_cancelled_session(session)

    assert cancelled
    assert state_after_cancellation == "exited"
    assert not child_running_after_cancellation


@pytest.mark.anyio
async def test_process_session_abort_cancellation_reaps_child_and_exits(
    stdio_fixture_config: ChildLaunchConfig,
) -> None:
    session = OneShotProcessSession(stdio_fixture_config)
    cancelled = False
    state_after_cancellation = ""
    child_running_after_cancellation = True
    with anyio.CancelScope() as caller_scope:
        await session.spawn()
        process = session._process  # noqa: SLF001
        assert process is not None
        caller_scope.cancel()
        try:
            await session.abort()
        except anyio.get_cancelled_exc_class():
            cancelled = True
        finally:
            state_after_cancellation = session._state  # noqa: SLF001
            child_running_after_cancellation = process.returncode is None
            if state_after_cancellation != "exited" or child_running_after_cancellation:
                await _emergency_cleanup_cancelled_session(session)

    assert cancelled
    assert state_after_cancellation == "exited"
    assert not child_running_after_cancellation


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scenario", "initialize_reason", "shutdown_reason", "exit_code"),
    [
        ("early_exit", "initialize_failed", "shutdown_failed", 0),
        ("malformed", "protocol_failed", "protocol_failed", None),
        ("truncated", "protocol_failed", "protocol_failed", 0),
    ],
)
async def test_process_session_failure_fixtures_are_reaped(
    stdio_fixture_config: ChildLaunchConfig,
    scenario: str,
    initialize_reason: str,
    shutdown_reason: str,
    exit_code: int | None,
) -> None:
    session = OneShotProcessSession(_fixture_scenario(stdio_fixture_config, scenario))
    await session.spawn()
    process = session._process  # noqa: SLF001
    assert process is not None
    with pytest.raises(ProcessSessionError, match=initialize_reason):
        await session.initialize()
    with pytest.raises(ProcessSessionError, match=shutdown_reason):
        await session.abort()
    assert process.returncode is not None
    if exit_code is not None:
        assert process.returncode == exit_code
    assert session._state == "exited"  # noqa: SLF001


@pytest.mark.anyio
async def test_process_session_nonzero_fixture_is_reaped(
    stdio_fixture_config: ChildLaunchConfig,
) -> None:
    session = OneShotProcessSession(_fixture_scenario(stdio_fixture_config, "nonzero"))
    await session.spawn()
    await session.initialize()
    await session.list_tools_once()
    process = session._process  # noqa: SLF001
    assert process is not None
    with pytest.raises(ProcessSessionError, match="nonzero_exit"):
        await session.close()
    assert process.returncode == _NONZERO_FIXTURE_EXIT_CODE
    assert session._state == "exited"  # noqa: SLF001


def test_pipe_identity_rejects_mismatched_endpoints() -> None:
    first_read, first_write = os.pipe()
    second_read, second_write = os.pipe()
    try:
        with pytest.raises(OSError, match="pipe_identity_invalid"):
            analytics_source_process._pipe_identity(first_write, second_read)  # noqa: SLF001
    finally:
        for descriptor in (first_read, first_write, second_read, second_write):
            with suppress(OSError):
                os.close(descriptor)


def test_child_command_uses_exact_installed_interpreter_and_isolated_bootstrap(
    sealed_child_paths: ChildBootstrapPaths,
    exact_sim_env: dict[str, str],
) -> None:
    config = build_child_launch_config(sealed_child_paths, exact_sim_env)
    assert config.command[:7] == (
        str(sealed_child_paths.executable),
        "-I",
        "-B",
        "-S",
        "-X",
        f"pycache_prefix={sealed_child_paths.pycache_prefix}",
        "-c",
    )
    assert config.command[7] == CHILD_BOOTSTRAP
    assert config.cwd == sealed_child_paths.workdir
    assert "--transport" not in config.command[8:]
    assert "stdio" not in config.command[8:]


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("SAXO_MCP_EVAL_TOOL_FILTER", ""),
        ("SAXO_MCP_EVAL_ALLOWED_TOOLS", "saxo_auth_status"),
        ("SAXO_MCP_EVAL_ALLOWED_TOOLS", "saxo_auth_status,saxo_auth_status"),
        ("SAXO_MCP_EVAL_ALLOWED_TOOLS", "saxo_*"),
        ("SAXO_MCP_EVAL_ALLOWED_TOOLS", "server:saxo_auth_status"),
        ("SAXO_MCP_EVAL_ALLOWED_TOOLS", "saxo_unknown_tool"),
        ("SAXO_MCP_ENVIRONMENT", "LIVE"),
        ("SAXO_MCP_ENABLE_LIVE_READS", "1"),
        ("SAXO_MCP_ENABLE_LIVE_WRITES", "1"),
    ],
)
def test_ambient_filter_or_live_configuration_refuses_before_spawn(
    override: str,
    value: str,
    exact_sim_env: dict[str, str],
    sealed_child_paths: ChildBootstrapPaths,
) -> None:
    caller = {**exact_sim_env, override: value}
    with pytest.raises(ChildConfigurationError):
        build_child_launch_config(sealed_child_paths, caller)


def test_registered_call_profile_refuses_unsealed_tool_arguments() -> None:
    profile = RegisteredCallProfile(
        path="/port/v1/balances/me",
        params={},
        response_mode="fingerprint_only",
        analytics_contract_id=None,
    )
    policy = MatrixCallPolicy(
        registry_page_offsets={"Portfolio": (0,)},
        registered_calls=(profile,),
    )
    valid: dict[str, JsonValue] = {
        "method": "GET",
        "path": "/port/v1/balances/me",
        "response_mode": "fingerprint_only",
    }
    policy.validate("saxo_call_registered_endpoint", valid)
    invalid = (
        {**valid, "method": "POST"},
        {**valid, "path": "https://example.invalid/port/v1/balances/me"},
        {**valid, "path": "/trade/v2/orders"},
        {**valid, "response_mode": "raw"},
        {**valid, "analytics_contract_id": "chart_v3"},
    )
    for arguments in invalid:
        with pytest.raises(ChildConfigurationError):
            policy.validate("saxo_call_registered_endpoint", arguments)


def test_registry_result_requires_the_next_sealed_offset() -> None:
    policy = MatrixCallPolicy(
        registry_page_offsets={"Portfolio": (0, 100)},
        registered_calls=(),
    )
    arguments: dict[str, JsonValue] = {
        "service_group": "Portfolio",
        "limit": 100,
        "offset": 0,
    }
    policy.validate("saxo_list_registered_endpoints", arguments)
    policy.observe("saxo_list_registered_endpoints", arguments, {"next_offset": 100})
    policy.validate("saxo_list_registered_endpoints", {**arguments, "offset": 100})
    with pytest.raises(ChildConfigurationError):
        policy.observe(
            "saxo_list_registered_endpoints",
            {**arguments, "offset": 100},
            {"next_offset": 100},
        )


@pytest.mark.parametrize("escape", ["parent", "ancestor_symlink"])
def test_child_paths_refuse_parent_escape_and_symlinked_ancestor(
    escape: str,
    sealed_child_paths: ChildBootstrapPaths,
    exact_sim_env: dict[str, str],
    tmp_path: Path,
) -> None:
    if escape == "parent":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        executable = sealed_child_paths.runtime_root / ".." / "outside" / "python3.12"
        shutil.copy2(sys.executable, outside / "python3.12")
        paths = ChildBootstrapPaths(
            sealed_child_paths.runtime_root,
            executable,
            sealed_child_paths.site_packages,
            sealed_child_paths.pycache_prefix,
            sealed_child_paths.workdir,
            sealed_child_paths.tmpdir,
        )
    else:
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        shutil.copy2(sys.executable, outside / "python3.12")
        sealed_child_paths.runtime_root.chmod(0o700)
        linked_runtime = sealed_child_paths.runtime_root / "linked-bin"
        linked_runtime.symlink_to(outside, target_is_directory=True)
        sealed_child_paths.runtime_root.chmod(0o500)
        paths = ChildBootstrapPaths(
            sealed_child_paths.runtime_root,
            linked_runtime / "python3.12",
            sealed_child_paths.site_packages,
            sealed_child_paths.pycache_prefix,
            sealed_child_paths.workdir,
            sealed_child_paths.tmpdir,
        )

    with pytest.raises(ChildConfigurationError, match="child_path_invalid"):
        build_child_launch_config(paths, exact_sim_env)


def test_isolated_bootstrap_imports_from_the_installed_site_packages(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    package = site_packages / "saxo_bank_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "server.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    cache = tmp_path / "cache"
    work = tmp_path / "work"
    temp = tmp_path / "temp"
    for directory in (cache, work, temp):
        directory.mkdir()

    interpreter = str(Path(sys.executable).resolve())
    completed = subprocess.run(
        (
            interpreter,
            "-I",
            "-B",
            "-S",
            "-X",
            f"pycache_prefix={cache}",
            "-c",
            CHILD_BOOTSTRAP,
            "/",
            interpreter,
            str(site_packages),
            str(cache),
            str(work),
            str(temp),
        ),
        check=False,
        cwd=work,
        env={"TMPDIR": str(temp)},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_shared_process_interfaces_match_the_approved_plan(
    sealed_child_paths: ChildBootstrapPaths,
    exact_sim_env: dict[str, str],
) -> None:
    config = build_child_launch_config(sealed_child_paths, exact_sim_env, ssl_runtime_entry=None)
    assert config.executable_identity_sha256
    assert not hasattr(config, "executable_sha256")
    assert getattr(MatrixSession, "_is_protocol", False) is True

    with pytest.raises(ChildConfigurationError, match="registered_call_profile_invalid"):
        RegisteredCallProfile(
            path="/port/v1/balances/me",
            params={},
            response_mode=cast("RegisteredResponseMode", "redacted_body"),
            analytics_contract_id=None,
        )

    policy = MatrixCallPolicy(registry_page_offsets={"Portfolio": (0,)}, registered_calls=())
    assert callable(policy.observe)


def test_registry_final_page_accepts_explicit_null_next_offset() -> None:
    policy = MatrixCallPolicy(registry_page_offsets={"Portfolio": (0,)}, registered_calls=())
    arguments: dict[str, JsonValue] = {
        "service_group": "Portfolio",
        "limit": 100,
        "offset": 0,
    }
    policy.validate("saxo_list_registered_endpoints", arguments)
    policy.observe("saxo_list_registered_endpoints", arguments, {"next_offset": None})


@pytest.mark.parametrize(
    "mutation",
    (  # noqa: PT007
        "content",
        "type",
        "mode",
        "owner",
        "link",
        "cache",
        "extra_file",
        "interpreter",
        "ancestor",
    ),
)
def test_prelaunch_static_runtime_change_refuses_before_child(
    mutation: str,
    sealed_runtime: SealedRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("saxo_bank_mcp.analytics_source_runtime")
    baseline = sealed_runtime.open_seal()
    runtime_module.close_candidate_runtime_seal(baseline)
    sealed_runtime.apply(mutation, monkeypatch)
    spawn = Mock()

    def open_then_spawn() -> None:
        sealed_runtime.open_seal()
        spawn()

    with pytest.raises(runtime_module.CandidateRuntimeError):
        open_then_spawn()
    spawn.assert_not_called()


def test_runtime_is_owner_only_nonwritable_and_uses_external_empty_caches(
    two_prepared_runtimes: tuple[PreparedRuntimeFixture, PreparedRuntimeFixture],
) -> None:
    first, second = two_prepared_runtimes
    assert first.portable_identity == second.portable_identity
    for prepared in (first, second):
        runtime = prepared.root
        assert (runtime / "_python/bin/python3.12").is_file()
        assert (runtime / "bin/python3.12").read_bytes() == (
            runtime / "_python/bin/python3.12"
        ).read_bytes()
        for entry in (runtime, *runtime.rglob("*")):
            metadata = entry.lstat()
            if entry.is_symlink():
                assert entry.relative_to(runtime).as_posix() in {
                    "bin/python",
                    "bin/python3",
                }
            elif entry.is_dir() or stat.S_IMODE(metadata.st_mode) & 0o111:
                assert stat.S_IMODE(metadata.st_mode) == _SEALED_DIRECTORY_MODE
            else:
                assert stat.S_IMODE(metadata.st_mode) == _SEALED_FILE_MODE
        assert not any(
            path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
            for path in runtime.rglob("*")
        )
        assert not (runtime / ".saxo-bank-mcp-pycache").exists()


def test_live_object_projection_functions_are_absent() -> None:
    retired = {
        "_stable_import_state",
        "_module_execution_projection",
        "_loaded_module_projection",
        "_interpreter_execution_projection",
        "_execution_root_projection_sha256",
        "_stabilized_execution_root_projection_sha256",
    }
    source = QA_MATRIX_PATH.read_text(encoding="utf-8")
    assert retired.isdisjoint(vars(qa_analytics_source_matrix))
    assert not any(name in source for name in retired)
    assert not ROUND7_PATH.exists()
