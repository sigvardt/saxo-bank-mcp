# pyright: reportPrivateUsage=false
from __future__ import annotations

import importlib.machinery
import json
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import anyio
import pytest
from test_qa_analytics_source_matrix import (
    CAPTURED_AT,
    FakeMatrixState,
    _fixtures,
    _identity,
    _matrix_server,
    _sim_env,
)

import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue

_CPYTHON_312_FLAGS = {
    "bytes_warning",
    "debug",
    "dev_mode",
    "dont_write_bytecode",
    "hash_randomization",
    "ignore_environment",
    "inspect",
    "interactive",
    "isolated",
    "int_max_str_digits",
    "no_site",
    "no_user_site",
    "optimize",
    "quiet",
    "safe_path",
    "utf8_mode",
    "verbose",
    "warn_default_encoding",
}
_OWNER_FILE_MODE = 0o600


def _execution_projection() -> dict[str, JsonValue]:
    return matrix_module._interpreter_execution_projection()  # noqa: SLF001


def test_execution_identity_binds_complete_cpython_state_and_loaded_behavior(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _execution_projection()
    assert set(cast("dict[str, JsonValue]", initial["flags"])) == _CPYTHON_312_FLAGS

    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "dont_write_bytecode", not sys.dont_write_bytecode)
        assert _execution_projection() != initial
    with monkeypatch.context() as scoped:
        replacement_prefix = str(tmp_path / "replacement-cache")
        scoped.setattr(sys, "pycache_prefix", replacement_prefix)
        assert _execution_projection() != initial

    original_limit = sys.get_int_max_str_digits()
    replacement_limit = 0 if original_limit != 0 else 640
    try:
        sys.set_int_max_str_digits(replacement_limit)
        assert _execution_projection() != initial
    finally:
        sys.set_int_max_str_digits(original_limit)

    originless = ModuleType("round6_originless")
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, originless.__name__, originless)
        with_originless = _execution_projection()
        assert with_originless != initial
        loaded_modules = cast(
            "list[dict[str, JsonValue]]",
            with_originless["loaded_modules"],
        )
        assert any(
            item["name_sha256"]
            == matrix_module.hashlib.sha256(originless.__name__.encode()).hexdigest()
            for item in loaded_modules
        )

    before_pathfinder_change = _execution_projection()

    def changed_find_spec(
        _cls: type[importlib.machinery.PathFinder],
        _fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> None:
        return None

    with monkeypatch.context() as scoped:
        scoped.setattr(
            importlib.machinery.PathFinder,
            "find_spec",
            classmethod(changed_find_spec),
        )
        assert _execution_projection() != before_pathfinder_change

    cache_prefix = tmp_path / ".saxo-bank-mcp-pycache"
    cache_prefix.mkdir(mode=0o700)
    cache_prefix.chmod(0o700)
    original_limit = sys.get_int_max_str_digits()
    with monkeypatch.context() as scoped:
        scoped.setenv(matrix_module._OFFICIAL_LAUNCHER_ENV, "1")  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        scoped.setattr(matrix_module, "_official_runtime_root", lambda: tmp_path)
        scoped.setattr(
            matrix_module,
            "_interpreter_flag_projection",
            matrix_module._official_interpreter_flags,  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        )
        scoped.setattr(
            matrix_module,
            "_interpreter_xoptions",
            lambda: {"pycache_prefix": str(cache_prefix)},
        )
        scoped.setattr(matrix_module, "_validate_official_import_machinery", lambda: None)
        scoped.setattr(matrix_module, "_validate_owner_only_runtime", lambda: None)
        scoped.setattr(sys, "path", [str(tmp_path)])
        scoped.setattr(sys, "pycache_prefix", str(cache_prefix))
        scoped.setattr(sys, "dont_write_bytecode", True)
        try:
            sys.set_int_max_str_digits(sys.int_info.default_max_str_digits)
            matrix_module._validate_official_interpreter_state()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

            scoped.setattr(sys, "dont_write_bytecode", False)
            with pytest.raises(ValueError, match="interpreter"):
                matrix_module._validate_official_interpreter_state()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            scoped.setattr(sys, "dont_write_bytecode", True)

            scoped.setattr(sys, "pycache_prefix", str(tmp_path / "other-cache"))
            with pytest.raises(ValueError, match="cache prefix"):
                matrix_module._validate_official_interpreter_state()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            scoped.setattr(sys, "pycache_prefix", str(cache_prefix))

            sys.set_int_max_str_digits(640)
            with pytest.raises(ValueError, match="integer"):
                matrix_module._validate_official_interpreter_state()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        finally:
            sys.set_int_max_str_digits(original_limit)


def test_postclaim_closure_change_wins_over_tool_exception_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = FakeMatrixState()
    closure_valid = True
    claimed = False
    original_call_tool = matrix_module._call_tool  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    async def fail_after_first_claimed_call(
        client: matrix_module.MatrixClient,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        nonlocal closure_valid
        payload = await original_call_tool(client, name, arguments)
        if claimed and name == "saxo_call_registered_endpoint":
            closure_valid = False
            raise RuntimeError("private tool failure")
        return payload

    def claim() -> bool:
        nonlocal claimed
        claimed = True
        return True

    async def exercise_tool_exception() -> matrix_module.AnalyticsSourceMatrixReceipt:
        return await matrix_module.run_analytics_source_matrix(
            _matrix_server(state),
            env=_sim_env(),
            fixtures=_fixtures(),
            candidate_identity=_identity(),
            captured_at=CAPTURED_AT,
            claim_source_execution=claim,
            validate_source_execution=lambda: closure_valid,
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(matrix_module, "_call_tool", fail_after_first_claimed_call)
        try:
            receipt = anyio.run(exercise_tool_exception)
        except RuntimeError:
            receipt = None

    assert receipt is not None
    assert receipt.status == "failed"
    assert receipt.reason == "candidate_execution_closure_changed"
    claimed_calls = [
        arguments for tool, arguments in state.calls if tool == "saxo_call_registered_endpoint"
    ]
    assert len(claimed_calls) == 1

    async def build_successful_receipt() -> matrix_module.AnalyticsSourceMatrixReceipt:
        return await matrix_module.run_analytics_source_matrix(
            _matrix_server(FakeMatrixState()),
            env=_sim_env(),
            fixtures=_fixtures(),
            candidate_identity=_identity(),
            captured_at=CAPTURED_AT,
            claim_source_execution=lambda: True,
        )

    successful = anyio.run(build_successful_receipt)
    publication_closure_valid = True

    async def return_success_after_claim(
        *_args: object,
        **kwargs: object,
    ) -> matrix_module.AnalyticsSourceMatrixReceipt:
        claim_callback = kwargs["claim_source_execution"]
        assert callable(claim_callback)
        assert claim_callback()
        return successful

    def projection() -> str:
        return "a" * 64 if publication_closure_valid else "b" * 64

    def mutate_during_privacy_scan(
        _name: str,
        _text: str,
    ) -> tuple[list[object], list[object]]:
        nonlocal publication_closure_valid
        publication_closure_valid = False
        return [], []

    with monkeypatch.context() as scoped:
        scoped.setattr(matrix_module, "source_matrix_candidate_identity", _identity)
        scoped.setattr(matrix_module, "_execution_root_projection_sha256", projection)
        scoped.setattr(matrix_module, "run_analytics_source_matrix", return_success_after_claim)
        scoped.setattr(matrix_module, "scan_secret_text", mutate_during_privacy_scan)
        state_root = tmp_path / "publication-state"
        result = matrix_module._execute_analytics_source_matrix_once(  # noqa: SLF001
            fixtures=_fixtures(),
            server=_matrix_server(FakeMatrixState()),
            env=_sim_env(),
            captured_at=CAPTURED_AT,
            state_root=state_root,
            candidate_identity=None,
        )

    evidence = matrix_module.candidate_evidence_path(
        state_root,
        _identity().candidate_identity_sha256,
    )
    assert result == 1
    assert json.loads(evidence.read_text(encoding="utf-8")) == {
        "reason": "candidate_execution_closure_changed",
        "status": "failed",
    }


def test_evidence_publication_refuses_a_swapped_claimed_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    identity = _identity()
    original_run = matrix_module.run_analytics_source_matrix
    redirected = tmp_path / "redirected"
    redirected.mkdir(mode=0o700)
    redirected.chmod(0o700)
    moved = state_root / "qa" / "analytics-source-matrix.claimed"

    async def swap_ancestor_after_claim(
        *args: object,
        **kwargs: object,
    ) -> matrix_module.AnalyticsSourceMatrixReceipt:
        receipt = await original_run(
            *args,  # pyright: ignore[reportArgumentType]
            **kwargs,
        )
        ancestor = state_root / "qa" / "analytics-source-matrix"
        ancestor.rename(moved)
        (redirected / identity.candidate_identity_sha256).mkdir(mode=0o700)
        ancestor.symlink_to(redirected, target_is_directory=True)
        return receipt

    monkeypatch.setattr(
        matrix_module,
        "run_analytics_source_matrix",
        swap_ancestor_after_claim,
    )
    result = matrix_module._execute_analytics_source_matrix_once(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        fixtures=_fixtures(),
        server=_matrix_server(FakeMatrixState()),
        env=_sim_env(),
        captured_at=CAPTURED_AT,
        state_root=state_root,
        candidate_identity=identity,
    )

    assert result == 1
    assert not (redirected / identity.candidate_identity_sha256 / "source-matrix.json").exists()
    original_parent = moved / identity.candidate_identity_sha256
    assert (original_parent / "claimed.json").is_file()
    assert stat.S_IMODE((original_parent / "claimed.json").stat().st_mode) == _OWNER_FILE_MODE
    assert not (original_parent / "source-matrix.json").exists()
