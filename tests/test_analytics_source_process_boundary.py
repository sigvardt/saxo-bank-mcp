from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from saxo_bank_mcp.analytics_source_process import (
    CHILD_BOOTSTRAP,
    ChildBootstrapPaths,
    ChildConfigurationError,
    MatrixCallPolicy,
    RegisteredCallProfile,
    build_child_launch_config,
)


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
    valid = {
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
    arguments = {"service_group": "Portfolio", "limit": 100, "offset": 0}
    policy.validate("saxo_list_registered_endpoints", arguments)
    policy.validate_result("saxo_list_registered_endpoints", {"next_offset": 100})
    policy.validate("saxo_list_registered_endpoints", {**arguments, "offset": 100})
    with pytest.raises(ChildConfigurationError):
        policy.validate_result("saxo_list_registered_endpoints", {"next_offset": 100})
