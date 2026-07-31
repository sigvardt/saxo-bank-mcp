# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import anyio
import pytest

from saxo_bank_mcp import analytics_source_process
from saxo_bank_mcp._evidence import JsonValue
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

_NONZERO_FIXTURE_EXIT_CODE: Final = 17


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
