# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

import saxo_bank_mcp.agent_skill_eval_runner as eval_runner
from saxo_bank_mcp import agent_skill_eval_native_preflight as preflight
from saxo_bank_mcp.agent_skill_eval_execution import HarnessRoots
from saxo_bank_mcp.agent_skill_eval_models import load_eval_cases
from saxo_bank_mcp.agent_skill_eval_native_preflight import CodexNativePreflightError
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager
from saxo_bank_mcp.agent_skill_eval_runner import EvalRunOptions
from saxo_bank_mcp.agent_skill_matrix_env import MatrixIsolatedRuntime

ROOT: Final = Path(__file__).resolve().parents[1]
CASE_ROOT: Final = ROOT / "evals/saxo-bank"
GRANTS: Final = ("saxo_health", "saxo_list_registered_endpoints")


@dataclass(frozen=True, slots=True)
class _FailureScenario:
    plugin_enabled: bool
    visible: tuple[str, ...]
    reason: str
    mcp_started: bool


def _env(tmp_path: Path) -> dict[str, str]:
    codex_home = tmp_path / "codex-home"
    home = tmp_path / "home"
    probe = tmp_path / "probe"
    for path in (codex_home, home, probe):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "TMPDIR": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "UV_PROJECT_ENVIRONMENT": str(probe),
        "UV_OFFLINE": "1",
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }


def _plugin_root(env: dict[str, str]) -> Path:
    root = Path(env["CODEX_HOME"]) / "plugins/cache/sigvardt/saxo-bank-mcp/0.1.0"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_native_preflight_proves_enabled_plugin_and_exact_filtered_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(tmp_path)
    plugin_payload = {
        "installed": [
            {
                "name": "saxo-bank-mcp",
                "version": "0.1.0",
                "enabled": True,
                "installed": True,
            }
        ],
        "available": [],
    }

    def fake_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=json.dumps(plugin_payload))

    def fake_probe(*_args: object, **kwargs: object) -> SimpleNamespace:
        assert kwargs.get("offline") is True
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "tool_count": len(GRANTS),
                    "annotations_missing": [],
                    "tool_names": list(GRANTS),
                }
            )
        )

    monkeypatch.setattr(
        preflight,
        "run_command",
        fake_command,
    )
    monkeypatch.setattr(
        preflight,
        "probe_root_stdio",
        fake_probe,
    )

    receipt = preflight.preflight_codex_native_case(
        codex_home=Path(env["CODEX_HOME"]),
        plugin_root=_plugin_root(env),
        logical_grants=GRANTS,
        env=env,
        probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
    )

    assert receipt.plugin_enabled is True
    assert receipt.mcp_started is True
    assert receipt.visible_logical_tools == GRANTS


@pytest.mark.parametrize(
    "scenario",
    [
        _FailureScenario(
            plugin_enabled=False,
            visible=GRANTS,
            reason="codex_native_registration_invalid",
            mcp_started=False,
        ),
        _FailureScenario(
            plugin_enabled=True,
            visible=("saxo_health",),
            reason="codex_native_tool_visibility_mismatch",
            mcp_started=True,
        ),
    ],
)
def test_native_preflight_fails_typed_before_model_for_registration_or_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: _FailureScenario,
) -> None:
    env = _env(tmp_path)

    def fake_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "installed": [
                        {
                            "name": "saxo-bank-mcp",
                            "version": "0.1.0",
                            "enabled": scenario.plugin_enabled,
                            "installed": True,
                        }
                    ],
                    "available": [],
                }
            )
        )

    def fake_probe(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "tool_count": len(scenario.visible),
                    "annotations_missing": [],
                    "tool_names": list(scenario.visible),
                }
            )
        )

    monkeypatch.setattr(
        preflight,
        "run_command",
        fake_command,
    )
    monkeypatch.setattr(
        preflight,
        "probe_root_stdio",
        fake_probe,
    )

    with pytest.raises(preflight.CodexNativePreflightError) as exc_info:
        preflight.preflight_codex_native_case(
            codex_home=Path(env["CODEX_HOME"]),
            plugin_root=_plugin_root(env),
            logical_grants=GRANTS,
            env=env,
            probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
        )

    assert exc_info.value.reason == scenario.reason
    assert exc_info.value.mcp_started is scenario.mcp_started


def test_native_preflight_keeps_mcp_start_unknown_when_probe_process_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(tmp_path)

    def fake_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "installed": [
                        {
                            "name": "saxo-bank-mcp",
                            "version": "0.1.0",
                            "enabled": True,
                            "installed": True,
                        }
                    ],
                    "available": [],
                }
            )
        )

    monkeypatch.setattr(preflight, "run_command", fake_command)

    def fail_probe(*_args: object, **_kwargs: object) -> object:
        raise OSError("sanitized local probe failure")

    monkeypatch.setattr(preflight, "probe_root_stdio", fail_probe)

    with pytest.raises(preflight.CodexNativePreflightError) as exc_info:
        preflight.preflight_codex_native_case(
            codex_home=Path(env["CODEX_HOME"]),
            plugin_root=_plugin_root(env),
            logical_grants=GRANTS,
            env=env,
            probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
        )

    assert exc_info.value.reason == "codex_native_mcp_start_failed"
    assert exc_info.value.mcp_started is None


def test_runner_records_native_preflight_failure_without_starting_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(tmp_path)
    case = next(
        case for case in load_eval_cases(CASE_ROOT) if case.id == "codex-native-safety-boundary"
    )
    runtime = MatrixIsolatedRuntime(
        run_root=tmp_path,
        env=env,
        home=Path(env["HOME"]),
        codex_home=Path(env["CODEX_HOME"]),
        probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
        auth_dir=tmp_path / "auth",
        sim_credential_path=tmp_path / "auth" / "credentials",
        token_cache_path=tmp_path / "auth" / "token.json",
        sim_token_source=tmp_path / "source-token.json",
        sim_token_source_digest="d" * 64,
    )
    options = EvalRunOptions(
        harness="codex",
        case_id=case.id,
        tag=None,
        environment="LOCAL",
        case_root=CASE_ROOT,
        codex_plugin_root=ROOT,
        claude_plugin_root=ROOT,
        codex_home=None,
        claude_home=None,
        out=tmp_path / "eval.json",
        dry_run=False,
        nonzero_on_skip=True,
        credential_mode="ephemeral-owner-only-copy",
        harness_policy="codex_native_v1",
    )

    def fail_preflight(**_kwargs: object) -> object:
        raise CodexNativePreflightError(
            "codex_native_mcp_start_failed",
            mcp_started=None,
        )

    def reject_model(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("model work began after failed native preflight")

    monkeypatch.setattr(eval_runner, "preflight_codex_native_case", fail_preflight)
    monkeypatch.setattr(eval_runner, "execute_model_case", reject_model)

    records = eval_runner._execute_selected_cases(  # noqa: SLF001
        options,
        cases=(case,),
        roots=HarnessRoots(ROOT, ROOT, None, None),
        binding=None,
        runtime=runtime,
        process_manager=EvalProcessManager(),
    )

    assert len(records) == 1
    assert records[0].error == "codex_native_mcp_start_failed"
    assert records[0].no_model_call is True
    assert records[0].no_mcp_call is False
    assert records[0].no_saxo_call is True
