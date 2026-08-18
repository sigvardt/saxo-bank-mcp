# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from re import fullmatch
from types import SimpleNamespace
from typing import Final

import pytest

import saxo_bank_mcp.agent_skill_eval_runner as eval_runner
from saxo_bank_mcp import agent_skill_eval_native_preflight as preflight
from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError
from saxo_bank_mcp.agent_skill_eval_execution import HarnessRoots
from saxo_bank_mcp.agent_skill_eval_models import EvalRunRecord, load_eval_cases
from saxo_bank_mcp.agent_skill_eval_native_preflight import (
    CodexNativePreflightError,
    CodexNativePreflightReceipt,
)
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager
from saxo_bank_mcp.agent_skill_eval_runner import EvalRunOptions
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_install_probe import probe_root_stdio
from saxo_bank_mcp.agent_skill_matrix_env import MatrixIsolatedRuntime
from saxo_bank_mcp.server_eval_tool_filter import derive_eval_tool_filter_env

ROOT: Final = Path(__file__).resolve().parents[1]
CASE_ROOT: Final = ROOT / "evals/saxo-bank"
GRANTS: Final = ("saxo_health", "saxo_list_registered_endpoints")
CODEX_0147_PLUGIN_LIST: Final = ROOT / "tests/fixtures/codex-0.147-plugin-list.json"
MCP_PROBE_FAILURE_EXIT_CODE: Final = 23


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
        path.chmod(0o700)
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
    for path in (root, *root.parents):
        if path == Path(env["CODEX_HOME"]).parent:
            break
        path.chmod(0o700)
    return root


def _fake_codex(tmp_path: Path) -> Path:
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        'test "$1" = "plugin" || exit 64\n'
        'test "$2" = "list" || exit 64\n'
        'test "$3" = "--json" || exit 64\n'
        '/bin/cat "$PLUGIN_LIST_FIXTURE"\n'
        'exit "${PLUGIN_LIST_EXIT_CODE:-0}"\n',
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _use_real_plugin_list_subprocess(
    env: dict[str, str],
    *,
    executable: Path,
    fixture: Path,
    exit_code: int = 0,
) -> None:
    env["EVAL_CLI_CODEX_BIN"] = str(executable)
    env["PLUGIN_LIST_FIXTURE"] = str(fixture)
    env["PLUGIN_LIST_EXIT_CODE"] = str(exit_code)


def _write_plugin_list_fixture(tmp_path: Path, raw: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    fixture = tmp_path / "plugin-list.json"
    fixture.write_text(raw, encoding="utf-8")
    fixture.chmod(0o600)
    return fixture


def _write_plugin_identity(root: Path) -> None:
    marketplace = root / ".agents/plugins/marketplace.json"
    manifest = root / ".codex-plugin/plugin.json"
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    marketplace.write_text(
        json.dumps(
            {
                "name": "sigvardt",
                "plugins": [{"name": "saxo-bank-mcp", "version": "0.1.0"}],
            }
        ),
        encoding="utf-8",
    )
    manifest.write_text(
        json.dumps({"name": "saxo-bank-mcp", "version": "0.1.0"}),
        encoding="utf-8",
    )


def test_generic_offline_probe_fails_with_empty_disposable_environment(
    tmp_path: Path,
) -> None:
    """Catch replacement of the retained runtime with a fresh offline uv environment."""
    uv = shutil.which("uv")
    assert uv is not None
    empty_project_environment = tmp_path / "empty-project-environment"
    empty_cache = tmp_path / "empty-uv-cache"
    empty_python = tmp_path / "empty-uv-python"
    for path in (empty_project_environment, empty_cache, empty_python):
        path.mkdir(mode=0o700)
    env = _env(tmp_path)
    env.update(
        {
            "PATH": f"{Path(uv).parent}:/usr/bin:/bin",
            "UV_CACHE_DIR": str(empty_cache),
            "UV_PROJECT_ENVIRONMENT": str(empty_project_environment),
            "UV_PYTHON_INSTALL_DIR": str(empty_python),
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )

    with pytest.raises(CommandFailureError) as exc_info:
        probe_root_stdio(
            "codex_native_empty_offline_probe",
            ROOT,
            env=env,
            probe_env=empty_project_environment,
            offline=True,
        )

    assert exc_info.value.receipt.exit_code != 0
    assert exc_info.value.receipt.cleanup_attempted is True
    assert exc_info.value.remaining_process_count == 0
    assert exc_info.value.remaining_process_group_count == 0


def test_native_preflight_uses_retained_interpreter_when_disposable_uv_env_is_empty(
    tmp_path: Path,
) -> None:
    """The native MCP probe must not need uv or an empty nested project environment."""
    env = derive_eval_tool_filter_env(_env(tmp_path), GRANTS)
    plugin_root = _plugin_root(env)
    _write_plugin_identity(plugin_root)
    shutil.copy2(ROOT / ".mcp.json", plugin_root / ".mcp.json")
    _use_real_plugin_list_subprocess(
        env,
        executable=_fake_codex(tmp_path),
        fixture=CODEX_0147_PLUGIN_LIST,
    )
    empty_project_environment = Path(env["UV_PROJECT_ENVIRONMENT"])
    empty_cache = tmp_path / "empty-uv-cache"
    empty_cache.mkdir(mode=0o700)
    env["UV_CACHE_DIR"] = str(empty_cache)
    env["PATH"] = "/usr/bin:/bin"

    receipt = preflight.preflight_codex_native_case(
        codex_home=Path(env["CODEX_HOME"]),
        plugin_root=plugin_root,
        logical_grants=GRANTS,
        env=env,
        probe_env=empty_project_environment,
        retained_project_environment=Path(sys.prefix),
        retained_interpreter=Path(sys.executable),
    )

    assert receipt.mcp_started is True
    assert receipt.visible_logical_tools == GRANTS
    assert receipt.mcp_probe_stage == "complete"
    assert receipt.mcp_probe_exit_code == 0
    assert fullmatch(r"[0-9a-f]{64}", receipt.mcp_probe_stdout_schema_sha256)
    assert not any(empty_project_environment.iterdir())
    assert not any(empty_cache.iterdir())


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
                "marketplaceName": "sigvardt",
                "pluginId": "saxo-bank-mcp@sigvardt",
                "enabled": True,
                "installed": True,
            }
        ],
        "available": [],
    }

    def fake_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdout=json.dumps(plugin_payload),
            receipt=SimpleNamespace(exit_code=0),
        )

    def fake_probe(*_args: object, **kwargs: object) -> SimpleNamespace:
        assert kwargs.get("offline") is True
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "tool_count": len(GRANTS),
                    "annotations_missing": [],
                    "tool_names": list(GRANTS),
                }
            ),
            receipt=SimpleNamespace(exit_code=0),
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


def test_native_preflight_accepts_codex_0147_plugin_list_via_isolated_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(tmp_path)
    _use_real_plugin_list_subprocess(
        env,
        executable=_fake_codex(tmp_path),
        fixture=CODEX_0147_PLUGIN_LIST,
    )

    def fake_probe(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "tool_count": len(GRANTS),
                    "annotations_missing": [],
                    "tool_names": list(GRANTS),
                }
            ),
            receipt=SimpleNamespace(exit_code=0),
        )

    monkeypatch.setattr(preflight, "probe_root_stdio", fake_probe)

    receipt = preflight.preflight_codex_native_case(
        codex_home=Path(env["CODEX_HOME"]),
        plugin_root=_plugin_root(env),
        logical_grants=GRANTS,
        env=env,
        probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
    )

    assert receipt.plugin_list_exit_code == 0
    assert fullmatch(r"[0-9a-f]{64}", receipt.plugin_list_stdout_schema_sha256)
    assert "/" not in receipt.plugin_list_stdout_schema_sha256


def test_native_preflight_rejects_plugin_root_outside_disposable_home_before_command(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    outside = tmp_path / "outside-plugin"
    outside.mkdir(mode=0o700)

    with pytest.raises(preflight.CodexNativePreflightError) as exc_info:
        preflight.preflight_codex_native_case(
            codex_home=Path(env["CODEX_HOME"]),
            plugin_root=outside,
            logical_grants=GRANTS,
            env=env,
            probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
        )

    assert exc_info.value.reason == "codex_native_disposable_binding_invalid"
    assert exc_info.value.plugin_list_exit_code is None
    assert exc_info.value.plugin_list_stdout_schema_sha256 is None


@pytest.mark.parametrize(
    ("raw", "exit_code", "reason", "expected_exit"),
    [
        (
            CODEX_0147_PLUGIN_LIST.read_text(encoding="utf-8"),
            17,
            "codex_native_plugin_list_command_failed",
            17,
        ),
        ("{not-json", 0, "codex_native_plugin_list_schema_invalid", 0),
        (json.dumps({"available": []}), 0, "codex_native_plugin_list_schema_invalid", 0),
        (
            json.dumps({"installed": [], "available": []}),
            0,
            "codex_native_plugin_identity_cardinality_invalid",
            0,
        ),
        (
            json.dumps(
                {
                    "installed": [
                        {
                            "name": "saxo-bank-mcp",
                            "version": "0.1.0",
                            "marketplaceName": "sigvardt",
                            "pluginId": "saxo-bank-mcp@sigvardt",
                            "enabled": True,
                            "installed": True,
                        },
                        {
                            "name": "saxo-bank-mcp",
                            "version": "0.1.0",
                            "marketplaceName": "sigvardt",
                            "pluginId": "saxo-bank-mcp@sigvardt",
                            "enabled": True,
                            "installed": True,
                        },
                    ],
                    "available": [],
                }
            ),
            0,
            "codex_native_plugin_identity_cardinality_invalid",
            0,
        ),
        (
            json.dumps(
                {
                    "installed": [
                        {
                            "name": "saxo-bank-mcp",
                            "version": "0.1.0",
                            "marketplaceName": "sigvardt",
                            "pluginId": "saxo-bank-mcp@sigvardt",
                            "enabled": True,
                            "installed": False,
                        }
                    ],
                    "available": [],
                }
            ),
            0,
            "codex_native_plugin_not_installed",
            0,
        ),
        (
            json.dumps(
                {
                    "installed": [
                        {
                            "name": "saxo-bank-mcp",
                            "version": "0.1.0",
                            "marketplaceName": "sigvardt",
                            "pluginId": "saxo-bank-mcp@sigvardt",
                            "enabled": False,
                            "installed": True,
                        }
                    ],
                    "available": [],
                }
            ),
            0,
            "codex_native_plugin_not_enabled",
            0,
        ),
        (
            json.dumps(
                {
                    "installed": [
                        {
                            "name": "saxo-bank-mcp",
                            "version": "0.1.0",
                            "marketplaceName": "other",
                            "pluginId": "saxo-bank-mcp@other",
                            "enabled": True,
                            "installed": True,
                        }
                    ],
                    "available": [],
                }
            ),
            0,
            "codex_native_plugin_identity_cardinality_invalid",
            0,
        ),
    ],
)
def test_native_preflight_reports_strict_plugin_list_failure_reason_and_safe_command_evidence(
    tmp_path: Path,
    raw: str,
    exit_code: int,
    reason: str,
    expected_exit: int,
) -> None:
    env = _env(tmp_path)
    fixture = _write_plugin_list_fixture(tmp_path, raw)
    _use_real_plugin_list_subprocess(
        env,
        executable=_fake_codex(tmp_path),
        fixture=fixture,
        exit_code=exit_code,
    )

    with pytest.raises(preflight.CodexNativePreflightError) as exc_info:
        preflight.preflight_codex_native_case(
            codex_home=Path(env["CODEX_HOME"]),
            plugin_root=_plugin_root(env),
            logical_grants=GRANTS,
            env=env,
            probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
        )

    assert exc_info.value.reason == reason
    assert exc_info.value.plugin_list_exit_code == expected_exit
    assert fullmatch(r"[0-9a-f]{64}", exc_info.value.plugin_list_stdout_schema_sha256 or "")
    rendered = repr(exc_info.value)
    assert raw not in rendered
    assert str(fixture) not in rendered


def test_native_preflight_schema_digest_ignores_plugin_list_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(tmp_path)
    first = CODEX_0147_PLUGIN_LIST.read_text(encoding="utf-8")
    second = first.replace("fixture:marketplace", "private-value").replace(
        "fixture:plugin", "different-private-value"
    )
    digests: list[str] = []

    def fake_probe(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "tool_count": len(GRANTS),
                    "annotations_missing": [],
                    "tool_names": list(GRANTS),
                }
            ),
            receipt=SimpleNamespace(exit_code=0),
        )

    monkeypatch.setattr(preflight, "probe_root_stdio", fake_probe)
    executable = _fake_codex(tmp_path)
    for index, raw in enumerate((first, second)):
        fixture = _write_plugin_list_fixture(tmp_path / str(index), raw)
        _use_real_plugin_list_subprocess(env, executable=executable, fixture=fixture)
        receipt = preflight.preflight_codex_native_case(
            codex_home=Path(env["CODEX_HOME"]),
            plugin_root=_plugin_root(env),
            logical_grants=GRANTS,
            env=env,
            probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
        )
        digests.append(receipt.plugin_list_stdout_schema_sha256)

    assert digests[0] == digests[1]


def test_runner_maps_retained_manifest_identity_to_disposable_registered_plugin(
    tmp_path: Path,
) -> None:
    retained = tmp_path / "retained-cache"
    retained.mkdir(mode=0o700)
    _write_plugin_identity(retained)
    retained_auth_home = tmp_path / "retained-auth-home"
    retained_auth_home.mkdir(mode=0o700)
    disposable_home = tmp_path / "disposable-home"
    expected = disposable_home / "plugins/cache/sigvardt/saxo-bank-mcp/0.1.0"
    expected.mkdir(parents=True)

    resolved = eval_runner._disposable_codex_plugin_path(  # noqa: SLF001
        disposable_home,
        retained_codex_home=retained_auth_home,
        retained_plugin_root=retained,
    )

    assert resolved == expected.resolve()


@pytest.mark.parametrize(
    "scenario",
    [
        _FailureScenario(
            plugin_enabled=False,
            visible=GRANTS,
            reason="codex_native_plugin_not_enabled",
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
                            "marketplaceName": "sigvardt",
                            "pluginId": "saxo-bank-mcp@sigvardt",
                            "enabled": scenario.plugin_enabled,
                            "installed": True,
                        }
                    ],
                    "available": [],
                }
            ),
            receipt=SimpleNamespace(exit_code=0),
        )

    def fake_probe(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "tool_count": len(scenario.visible),
                    "annotations_missing": [],
                    "tool_names": list(scenario.visible),
                }
            ),
            receipt=SimpleNamespace(exit_code=0),
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
    assert exc_info.value.plugin_list_exit_code == 0
    assert fullmatch(r"[0-9a-f]{64}", exc_info.value.plugin_list_stdout_schema_sha256 or "")


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
                            "marketplaceName": "sigvardt",
                            "pluginId": "saxo-bank-mcp@sigvardt",
                            "enabled": True,
                            "installed": True,
                        }
                    ],
                    "available": [],
                }
            ),
            receipt=SimpleNamespace(exit_code=0),
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
    assert exc_info.value.plugin_list_exit_code == 0
    assert fullmatch(r"[0-9a-f]{64}", exc_info.value.plugin_list_stdout_schema_sha256 or "")
    assert exc_info.value.mcp_probe_stage == "command_start"
    assert exc_info.value.mcp_probe_exit_code is None
    assert exc_info.value.mcp_probe_stdout_schema_sha256 is None


def test_native_preflight_retains_safe_mcp_command_failure_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(tmp_path)

    def fake_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            stdout=CODEX_0147_PLUGIN_LIST.read_text(encoding="utf-8"),
            receipt=SimpleNamespace(exit_code=0),
        )

    stdout = '{"private_value":"DO_NOT_PUBLISH"}'
    stderr = "PRIVATE_STDERR_SENTINEL"

    def fail_probe(*_args: object, **_kwargs: object) -> object:
        raise CommandFailureError(
            CommandReceipt(
                name="codex_native_case_list_tools",
                argv=("private-command",),
                cwd=str(tmp_path),
                pid=17,
                pgid=17,
                exit_code=MCP_PROBE_FAILURE_EXIT_CODE,
                stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
                stderr_sha256=hashlib.sha256(stderr.encode()).hexdigest(),
                cleanup_attempted=True,
            ),
            stdout=stdout,
            stderr=stderr,
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    monkeypatch.setattr(preflight, "run_command", fake_command)
    monkeypatch.setattr(preflight, "probe_root_stdio", fail_probe)

    with pytest.raises(CodexNativePreflightError) as exc_info:
        preflight.preflight_codex_native_case(
            codex_home=Path(env["CODEX_HOME"]),
            plugin_root=_plugin_root(env),
            logical_grants=GRANTS,
            env=env,
            probe_env=Path(env["UV_PROJECT_ENVIRONMENT"]),
        )

    failure = exc_info.value
    assert failure.reason == "codex_native_mcp_start_failed"
    assert failure.mcp_started is None
    assert failure.mcp_probe_stage == "command_exit"
    assert failure.mcp_probe_exit_code == MCP_PROBE_FAILURE_EXIT_CODE
    assert fullmatch(r"[0-9a-f]{64}", failure.mcp_probe_stdout_schema_sha256 or "")
    rendered = repr(failure)
    assert "DO_NOT_PUBLISH" not in rendered
    assert "PRIVATE_STDERR_SENTINEL" not in rendered
    assert str(tmp_path) not in rendered


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


def test_runner_retains_plugin_list_command_evidence_in_failed_record() -> None:
    case = next(
        case for case in load_eval_cases(CASE_ROOT) if case.id == "codex-native-safety-boundary"
    )
    failure = CodexNativePreflightError(
        reason="codex_native_plugin_not_enabled",
        mcp_started=False,
        plugin_list_exit_code=0,
        plugin_list_stdout_schema_sha256="e" * 64,
        mcp_probe_stage="command_exit",
        mcp_probe_exit_code=MCP_PROBE_FAILURE_EXIT_CODE,
        mcp_probe_stdout_schema_sha256="f" * 64,
    )

    record = eval_runner._native_preflight_failed_record(  # noqa: SLF001
        case,
        ("mcp__saxo_bank_mcp__saxo_health",),
        failure,
    )

    assert record.plugin_list_exit_code == 0
    assert record.plugin_list_stdout_schema_sha256 == "e" * 64
    assert record.mcp_probe_stage == "command_exit"
    assert record.mcp_probe_exit_code == MCP_PROBE_FAILURE_EXIT_CODE
    assert record.mcp_probe_stdout_schema_sha256 == "f" * 64


def test_runner_retains_completed_mcp_probe_evidence_on_model_record(
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

    def passed_preflight(**_kwargs: object) -> CodexNativePreflightReceipt:
        return CodexNativePreflightReceipt(
            plugin_enabled=True,
            mcp_started=True,
            visible_logical_tools=case.exact_tool_grants["codex"],
            plugin_list_exit_code=0,
            plugin_list_stdout_schema_sha256="e" * 64,
            mcp_probe_stage="complete",
            mcp_probe_exit_code=0,
            mcp_probe_stdout_schema_sha256="f" * 64,
        )

    def passed_model(*_args: object, **_kwargs: object) -> EvalRunRecord:
        return EvalRunRecord(
            case_id=case.id,
            harness="codex",
            status="passed",
            execution_mode="model_execution",
            expected_skill=case.expected_skill,
            required_logical_tools=case.required_logical_tools,
            forbidden_logical_tools=case.forbidden_logical_tools,
            resolved_tool_grants=case.exact_tool_grants["codex"],
            transcript_assertions_passed=True,
            no_model_call=False,
            no_mcp_call=False,
            no_saxo_call=True,
            grant_status="passed",
            assertion_status="passed",
        )

    monkeypatch.setattr(eval_runner, "preflight_codex_native_case", passed_preflight)
    monkeypatch.setattr(eval_runner, "execute_model_case", passed_model)

    records = eval_runner._execute_selected_cases(  # noqa: SLF001
        options,
        cases=(case,),
        roots=HarnessRoots(ROOT, ROOT, None, None),
        binding=None,
        runtime=runtime,
        process_manager=EvalProcessManager(),
    )

    assert records[0].mcp_probe_stage == "complete"
    assert records[0].mcp_probe_exit_code == 0
    assert records[0].mcp_probe_stdout_schema_sha256 == "f" * 64
