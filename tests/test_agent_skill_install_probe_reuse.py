from __future__ import annotations

import json
from pathlib import Path

import pytest

from saxo_bank_mcp import agent_skill_install_probe as install_probe
from saxo_bank_mcp.agent_skill_command_runner import CommandResult
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_install_probe import reuse_probe
from saxo_bank_mcp.agent_skill_install_verify_live import startup_probe_errors_for_caches

EXPECTED_DISTINCT_ROOTS = 3


def _probe_result(name: str, root: Path) -> CommandResult:
    receipt = CommandReceipt(
        name=name,
        argv=("uv", "run"),
        cwd=str(root),
        exit_code=0,
        timed_out=False,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        pid=None,
        pgid=None,
        cleanup_attempted=True,
    )
    return CommandResult(
        receipt=receipt,
        stdout=json.dumps({"tool_count": 60, "annotations_missing": []}),
        stderr="",
    )


def _record_probe(
    calls: list[tuple[str, Path]],
) -> object:
    def _fake_probe(
        name: str,
        root: Path,
        *,
        env: dict[str, str],
        probe_env: Path,
    ) -> CommandResult:
        _ = env, probe_env
        calls.append((name, root.resolve()))
        return _probe_result(name, root)

    return _fake_probe


def test_reuse_probe_starts_each_distinct_root_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate claim about the same exact root reuses the first successful result."""
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_probe.probe_root_stdio",
        _record_probe(calls),
    )
    root = tmp_path / "cache"
    root.mkdir()
    probe_env = tmp_path / "probe-env"
    cache: dict[Path, CommandResult] = {}

    first = reuse_probe(cache, "cache_mcp_probe", root, env={}, probe_env=probe_env)
    second = reuse_probe(cache, "cache_list_tools_probe", root, env={}, probe_env=probe_env)

    assert second is first
    assert len(calls) == 1
    assert calls[0][0] == "cache_mcp_probe"


def test_probe_offline_mode_is_explicit_and_preserves_legacy_command_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    (root / ".mcp.json").write_text("{}\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        **_kwargs: object,
    ) -> CommandResult:
        commands.append(argv)
        return _probe_result(name, root)

    monkeypatch.setattr(install_probe, "run_command", fake_run_command)

    install_probe.probe_root_stdio(
        "legacy",
        root,
        env={},
        probe_env=tmp_path / "legacy-env",
    )
    install_probe.probe_root_stdio(
        "native",
        root,
        env={},
        probe_env=tmp_path / "native-env",
        offline=True,
    )

    assert commands[0][:3] == ("uv", "run", "--project")
    assert commands[1][:4] == ("uv", "run", "--offline", "--project")


def test_reuse_probe_keeps_distinct_roots_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent proof is preserved: every distinct root is started on its own."""
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_probe.probe_root_stdio",
        _record_probe(calls),
    )
    probe_env = tmp_path / "probe-env"
    cache: dict[Path, CommandResult] = {}
    roots: list[Path] = []
    for name in ("source-clone", "codex-cache", "claude-cache"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)
        reuse_probe(cache, f"{name}_probe", root, env={}, probe_env=probe_env)

    assert len(calls) == EXPECTED_DISTINCT_ROOTS
    assert {call[1] for call in calls} == {root.resolve() for root in roots}
    assert len(cache) == EXPECTED_DISTINCT_ROOTS


def test_verify_startup_probes_each_root_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification proves source, Codex cache, and Claude cache without restarting a root."""
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live.probe_root_stdio",
        _record_probe(calls),
    )
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_probe.probe_root_stdio",
        _record_probe(calls),
    )
    run_root = tmp_path / "run"
    clone = run_root / "source-clone"
    codex_cache = run_root / "codex-home" / "cache"
    claude_cache = run_root / "home" / "cache"
    for path in (clone, codex_cache, claude_cache):
        path.mkdir(parents=True)

    errors = startup_probe_errors_for_caches(
        run_root=run_root,
        clone=clone,
        codex_cache=codex_cache,
        claude_cache=claude_cache,
    )

    assert errors == []
    probed = [call[1] for call in calls]
    assert sorted(probed) == sorted(
        {clone.resolve(), codex_cache.resolve(), claude_cache.resolve()},
    )
    assert len(probed) == EXPECTED_DISTINCT_ROOTS
