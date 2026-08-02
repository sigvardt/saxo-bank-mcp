# ruff: noqa: SLF001
# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import uuid4

import anyio
from anyio.lowlevel import checkpoint

from saxo_bank_mcp import qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import (
    SourceContract,
    source_contract_catalog_sha256,
    source_contract_fingerprint,
)
from saxo_bank_mcp.analytics_source_process import (
    SOURCE_MATRIX_CHILD_TOOLS,
    MatrixSession,
    PipeIdentity,
    ProcessMatrixSession,
    ProcessSessionError,
    ProcessSessionFacts,
)
from saxo_bank_mcp.analytics_source_runtime import (
    CandidateRuntimeError,
    CandidateRuntimeSeal,
    ExternalRunLayout,
    SourceMatrixCandidateIdentity,
)
from saxo_bank_mcp.endpoint_registry import find_registered_operation, load_inventory

if TYPE_CHECKING:
    from saxo_bank_mcp.analytics_source_process import RegisteredCallProfile
    from saxo_bank_mcp.qa_analytics_source_matrix import PreparedMatrix


BOUNDARY_COORDINATOR_PID: Final = 987_654_321
BOUNDARY_CHILD_PID: Final = 987_654_323
BOUNDARY_STDIN_IDENTITIES: Final = (987_654_331, 987_654_337, 987_654_341, 987_654_343)
BOUNDARY_STDOUT_IDENTITIES: Final = (987_654_349, 987_654_353, 987_654_359, 987_654_361)
BOUNDARY_PRIVATE_ACCOUNT: Final = "boundary-private-account-sentinel"
BOUNDARY_PRIVATE_CLIENT: Final = "boundary-private-client-sentinel"
BOUNDARY_FORBIDDEN_SCALARS: Final[frozenset[str | int]] = frozenset(
    {
        BOUNDARY_COORDINATOR_PID,
        BOUNDARY_CHILD_PID,
        *BOUNDARY_STDIN_IDENTITIES,
        *BOUNDARY_STDOUT_IDENTITIES,
        BOUNDARY_PRIVATE_ACCOUNT,
        BOUNDARY_PRIVATE_CLIENT,
    },
)
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
REPOSITORY_COPY_EXCLUDE_PATTERNS: Final[tuple[str, ...]] = (
    ".git",
    ".venv",
    "venv",
    "env",
    ".superpowers",
    ".pytest_cache",
    ".hypothesis",
    ".tox",
    ".nox",
    ".ruff_cache",
    ".mypy_cache",
    ".basedpyright",
    ".pyright",
    ".pytype",
    ".pyre",
    ".cache",
    "cache",
    "caches",
    "*-cache",
    "cache-*",
    "*-cache-*",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".coverage",
    ".coverage.*",
    "coverage.xml",
    "htmlcov",
    "build",
    "dist",
    "*.egg",
    "*.egg-info",
    "*.whl",
    "wheels",
    "wheelhouse",
    "*-wheel*",
    "wheel-*",
    "runtime",
    "runtimes",
    ".runtime",
    ".runtimes",
    "*-runtime*",
    "runtime-*",
    ".omo",
    "evidence",
    ".evidence",
    "artifacts",
    ".artifacts",
    "*-artifacts",
    "node_modules",
    ".eslintcache",
    ".local",
    ".state",
    "state",
    "logs",
    "audit",
)
REPOSITORY_COPY_IGNORE: Final = shutil.ignore_patterns(*REPOSITORY_COPY_EXCLUDE_PATTERNS)


_SOURCE_CONTRACT_ORDER: Final[tuple[str, ...]] = (
    "chart_v3",
    "reference_instruments_v1",
    "reference_instrument_details_v1",
    "options_chain_reference_v1",
    "info_price_v1",
    "info_prices_list_v1",
    "performance_summary_v4",
    "performance_timeseries_v4",
    "balances_v1",
    "positions_v1",
    "orders_v1",
    "transactions_v1",
    "bookings_v1",
    "closed_positions_history_v1",
    "exposure_instruments_v1",
    "costs_v1",
    "corporate_action_events_v2",
    "corporate_action_holdings_v2",
)
_STATE_PATHS: Final[tuple[str, ...]] = (
    "/port/v1/orders/me",
    "/port/v1/positions/me",
    "/port/v1/balances/me",
)


@dataclass(frozen=True, slots=True)
class SealedFixtureCandidate:
    identity: str
    receipt: dict[str, JsonValue]
    runtime: Path
    state_root: Path


def build_fixture_candidate(root: Path, uv_path: Path) -> SealedFixtureCandidate:
    if not root.is_absolute() or not uv_path.is_absolute():
        raise ValueError("fixture candidate paths must be absolute")
    source = root / "source"
    interpreter = Path(sys.executable).absolute()
    compiled_home = root / "compiled-home"
    compiled_home.mkdir(mode=0o700)
    state_root = compiled_home / ".local/state/saxo-bank-mcp"
    for directory in (
        compiled_home / ".local",
        compiled_home / ".local/state",
        state_root,
    ):
        directory.mkdir(mode=0o700)
    shutil.copytree(
        REPOSITORY_ROOT,
        source,
        symlinks=False,
        ignore=REPOSITORY_COPY_IGNORE,
    )
    shutil.copy2(
        REPOSITORY_ROOT / "tests/fixtures/analytics/source_matrix_fixture_server.py",
        source / "src/saxo_bank_mcp/server.py",
    )
    qa_path = source / "src/saxo_bank_mcp/qa_analytics_source_matrix.py"
    qa_text = qa_path.read_text(encoding="utf-8")
    original = (
        "selected_home = Path(pwd.getpwuid(os.getuid()).pw_dir) "
        "if owner_home is None else owner_home"
    )
    replacement = (
        f"selected_home = Path({str(compiled_home)!r}) "
        "if owner_home is None else owner_home"
    )
    assert qa_text.count(original) == 1
    qa_path.write_text(qa_text.replace(original, replacement), encoding="utf-8")

    bootstrap_wheels = root / "bootstrap-wheel"
    bootstrap_runtime = root / "bootstrap-runtime"
    final_wheels = root / "final-wheel"
    final_runtime = root / "final-runtime"
    bootstrap_wheels.mkdir(mode=0o700)
    final_wheels.mkdir(mode=0o700)
    wheel_name = "saxo_bank_mcp-0.1.0-py3-none-any.whl"
    _run_checked(
        (uv_path, "build", "--offline", "--wheel", "--out-dir", bootstrap_wheels),
        source,
    )
    _run_checked(
        (
            interpreter,
            "scripts/prepare_analytics_source_matrix_runtime.py",
            "--uv",
            uv_path,
            "--runtime",
            bootstrap_runtime,
            "--wheel",
            bootstrap_wheels / wheel_name,
        ),
        source,
    )
    _run_checked(
        (
            bootstrap_runtime / "bin/saxo-bank-analytics-source-matrix-generate",
            "--repository-root",
            source,
            "--wheel",
            bootstrap_wheels / wheel_name,
            "--out",
            source / "data/analytics/source_matrix_candidate.json",
        ),
        source,
    )
    _run_checked(
        (uv_path, "build", "--offline", "--wheel", "--out-dir", final_wheels),
        source,
    )
    _run_checked(
        (
            interpreter,
            "scripts/prepare_analytics_source_matrix_runtime.py",
            "--uv",
            uv_path,
            "--runtime",
            final_runtime,
            "--wheel",
            final_wheels / wheel_name,
        ),
        source,
    )
    token_cache = root / "fixture-token-cache.json"
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
        "SAXO_MCP_SIM_REDIRECT_URI": "http://localhost:8080/callback",
        "SAXO_MCP_TOKEN_CACHE_PATH": str(token_cache),
    }
    _run_checked(
        (final_runtime / "bin/saxo-bank-analytics-source-matrix",),
        root,
        env=environment,
    )
    identity = _run_checked(
        (final_runtime / "bin/saxo-bank-analytics-source-matrix", "--identity"),
        root,
        env=environment,
    ).stdout.strip()
    evidence = state_root / "qa/analytics-source-matrix" / identity / "source-matrix.json"
    return SealedFixtureCandidate(
        identity=identity,
        receipt=cast(
            "dict[str, JsonValue]",
            json.loads(evidence.read_text(encoding="utf-8")),
        ),
        runtime=final_runtime,
        state_root=state_root,
    )


def _run_checked(
    command: Sequence[str | os.PathLike[str]],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    subprocess_environment = None
    if env is not None:
        subprocess_environment = dict(env)
        for name in ("TMPDIR", "TMP", "TEMP"):
            if value := os.environ.get(name):
                subprocess_environment[name] = value
    return subprocess.run(
        tuple(os.fspath(argument) for argument in command),
        cwd=cwd,
        env=subprocess_environment,
        shell=False,
        check=True,
        capture_output=True,
        text=True,
    )


@dataclass(slots=True)
class ScriptedMatrixSession(MatrixSession):
    payloads: dict[str, deque[dict[str, JsonValue]]]
    tool_names: tuple[str, ...] = SOURCE_MATRIX_CHILD_TOOLS
    events: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)
    list_count: int = 0

    async def list_tools_once(self) -> tuple[str, ...]:
        self.list_count += 1
        if self.list_count != 1:
            raise ProcessSessionError("invalid_transition")
        self.events.append(("tools/list", {}))
        return self.tool_names

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        self.events.append((name, arguments))
        queue = self.payloads[name]
        if not queue:
            raise AssertionError(f"unexpected repeated tool call: {name}")
        return queue.popleft()


@dataclass(slots=True)
class ScriptedProcessSession(ProcessMatrixSession):
    matrix: ScriptedMatrixSession
    scenario: str
    spawn_count: int = 0
    initialize_count: int = 0
    close_count: int = 0
    abort_count: int = 0
    restart_count: int = 0
    requests_replayed: int = 0
    last_facts: ProcessSessionFacts | None = None
    cancel_scope: anyio.CancelScope | None = None
    _failed_request: tuple[str, str] | None = field(default=None, init=False)
    _state: Literal["new", "spawned", "initialized", "listed", "running", "exited"] = (
        field(default="new", init=False)
    )

    async def spawn(self) -> None:
        if self._state != "new":
            self.restart_count += 1
            raise ProcessSessionError("invalid_transition")
        self.spawn_count += 1
        if self.scenario == "spawn":
            self._state = "exited"
            raise ProcessSessionError("spawn_failed")
        self._state = "spawned"

    async def initialize(self) -> None:
        if self._state != "spawned":
            raise ProcessSessionError("invalid_transition")
        self.initialize_count += 1
        if self.scenario == "initialize":
            raise ProcessSessionError("initialize_failed")
        self._state = "initialized"

    async def list_tools_once(self) -> tuple[str, ...]:
        if self._state != "initialized":
            raise ProcessSessionError("invalid_transition")
        if self.scenario == "early_exit":
            self.matrix.list_count += 1
            raise ProcessSessionError("tool_list_failed")
        names = await self.matrix.list_tools_once()
        self._state = "listed"
        return names

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if self._state not in {"listed", "running"}:
            raise ProcessSessionError("invalid_transition")
        self._state = "running"
        if (
            self.scenario == "cancel_preclaim"
            and name == "saxo_auth_status"
        ):
            self._cancel_current_scope()
            await checkpoint()
        if (
            self.scenario in {"cancel_after_claim", "cancel_during_abort"}
            and name == "saxo_call_registered_endpoint"
        ):
            self.matrix.events.append((name, arguments))
            if self.scenario == "cancel_after_claim":
                self._cancel_current_scope()
                await checkpoint()
            raise ProcessSessionError("call_failed")
        if name == "saxo_call_registered_endpoint" and self.scenario in {
            "malformed_after_claim",
            "postclaim",
        }:
            failed_request = (name, canonical_digest(arguments))
            if self._failed_request == failed_request:
                self.requests_replayed += 1
            else:
                self._failed_request = failed_request
            self.matrix.events.append((name, arguments))
            if self.scenario == "malformed_after_claim":
                raise ProcessSessionError("protocol_failed")
            raise RuntimeError("scripted private tool failure")
        return await self.matrix.call_tool(name, arguments)

    async def close(self) -> ProcessSessionFacts:
        if self._state not in {"listed", "running"}:
            raise ProcessSessionError("invalid_transition")
        self.close_count += 1
        exit_code = (
            17
            if self.scenario in {"nonzero_after_matrix", "invalid_close_facts"}
            else 0
        )
        self._state = "exited"
        self.last_facts = self._facts(exit_code)
        if self.scenario == "nonzero_after_matrix":
            raise ProcessSessionError("nonzero_exit")
        return self.last_facts

    async def abort(self) -> ProcessSessionFacts:
        if self._state in {"new", "exited"}:
            raise ProcessSessionError("invalid_transition")
        self.abort_count += 1
        if self.scenario == "cancel_during_abort":
            self._cancel_current_scope()
            await checkpoint()
        self._state = "exited"
        self.last_facts = self._facts(0)
        if self.scenario == "malformed_after_claim":
            raise ProcessSessionError("protocol_failed")
        return self.last_facts

    def _cancel_current_scope(self) -> None:
        if self.cancel_scope is None:
            raise AssertionError("scripted cancellation scope is unavailable")
        self.cancel_scope.cancel()

    def _facts(self, exit_code: int) -> ProcessSessionFacts:
        stderr_count = 4099 if self.scenario == "stderr" else 0
        return ProcessSessionFacts(
            coordinator_pid=BOUNDARY_COORDINATOR_PID,
            child_pid=BOUNDARY_CHILD_PID,
            executable_identity_sha256="d" * 64,
            stdin_identity=PipeIdentity("fifo", *BOUNDARY_STDIN_IDENTITIES),
            stdout_identity=PipeIdentity("fifo", *BOUNDARY_STDOUT_IDENTITIES),
            stderr_byte_count=stderr_count,
            listed_tool_names=SOURCE_MATRIX_CHILD_TOOLS,
            child_spawn_count=1,
            mcp_session_count=1,
            mcp_initialize_count=1,
            tool_list_count=1,
            reconnect_count=0,
            restart_count=0,
            child_exit_code=exit_code,
            stdout_protocol_only=self.scenario != "malformed_after_claim",
        )


@dataclass(frozen=True, slots=True)
class BoundaryTestResult:
    command_exit_code: int
    child_exit_code: int | None
    cancellation_observed: bool
    cancellation_propagated: bool
    claimed: bool
    spawn_count: int
    close_count: int
    abort_count: int
    restart_count: int
    requests_replayed: int
    publication_count: int
    revalidation_count: int
    guard_path: Path
    evidence_path: Path
    published_text: str
    redirected_evidence: Path
    original_evidence: Path


@dataclass(slots=True)
class BoundaryEventFixture:
    root: Path
    forbidden_scalars: frozenset[str | int]

    async def run(  # noqa: PLR0915
        self,
        *,
        child_scenario: str,
        postexit_runtime_mutation: str | None = None,
    ) -> BoundaryTestResult:
        case_root = self.root / f"boundary-{uuid4().hex}"
        state_root = case_root / "state"
        runtime_root = case_root / "runtime"
        payload = runtime_root / "payload.py"
        run_root = case_root / "run"
        state_root.mkdir(parents=True, mode=0o700)
        runtime_root.mkdir(mode=0o700)
        payload.write_text("VALUE = 1\n", encoding="utf-8")
        payload.chmod(0o400)
        run_root.mkdir(mode=0o700)
        run_paths = tuple(
            run_root / name
            for name in (
                "coordinator-cache",
                "coordinator-work",
                "coordinator-tmp",
                "child-cache",
                "child-work",
                "child-tmp",
            )
        )
        for path in run_paths:
            path.mkdir(mode=0o700)
        identity = SourceMatrixCandidateIdentity(
            source_contract_catalog_sha256=source_contract_catalog_sha256(),
            harness_build_sha256="b" * 64,
            candidate_identity_sha256="c" * 64,
        )
        root_descriptor = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY)
        seal = CandidateRuntimeSeal(
            identity=identity,
            portable_identity_sha256="e" * 64,
            instance_identity_sha256="f" * 64,
            runtime_root=runtime_root,
            executable=payload,
            site_packages=runtime_root,
            root_descriptor=root_descriptor,
            ancestor_descriptors=(),
            entry_snapshot=(),
        )
        layout = ExternalRunLayout(run_root, *run_paths)
        captured_at = datetime(2026, 7, 31, 12, tzinfo=UTC)
        fixtures = matrix_module.SourceMatrixFixtures(
            account_key=BOUNDARY_PRIVATE_ACCOUNT,
            client_key=BOUNDARY_PRIVATE_CLIENT,
        )
        env = {
            "SAXO_MCP_ENVIRONMENT": "SIM",
            "SAXO_MCP_ENABLE_LIVE_READS": "0",
            "SAXO_MCP_ENABLE_LIVE_WRITES": "",
        }
        prepared = matrix_module.prepare_analytics_source_matrix(
            env=env,
            fixtures=fixtures,
            candidate_identity=identity,
            captured_at=captured_at,
        )
        assert isinstance(prepared, matrix_module.PreparedMatrix)
        payload_queues = matrix_payloads(prepared)
        if child_scenario == "preclaim":
            auth = payload_queues["saxo_auth_status"][0]
            auth["token_cache_expired"] = True
            auth["blocking_reasons"] = ["token_cache_expired"]
        session = ScriptedProcessSession(
            matrix=ScriptedMatrixSession(payloads=payload_queues),
            scenario=child_scenario,
        )
        baseline = hashlib.sha256(payload.read_bytes()).hexdigest()
        revalidation_count = 0

        def postexit_revalidate() -> None:
            nonlocal revalidation_count
            revalidation_count += 1
            if child_scenario == "cancel_during_revalidation":
                cancel_scope.cancel()
            if postexit_runtime_mutation == "content":
                payload.chmod(0o600)
                payload.write_bytes(payload.read_bytes() + b"MUTATION")
                payload.chmod(0o400)
            if hashlib.sha256(payload.read_bytes()).hexdigest() != baseline:
                raise CandidateRuntimeError("runtime_identity_mismatch")

        command_exit_code = 1
        completed_normally = False
        cancel_scope = anyio.CancelScope()
        session.cancel_scope = cancel_scope
        try:
            with cancel_scope:
                command_exit_code = await matrix_module._execute_source_matrix_with_events(
                    fixtures=fixtures,
                    env=env,
                    seal=seal,
                    layout=layout,
                    session=session,
                    state_root=state_root,
                    captured_at=captured_at,
                    postexit_revalidate=postexit_revalidate,
                )
                completed_normally = True
        finally:
            os.close(root_descriptor)
        guard_path = matrix_module.candidate_guard_path(
            state_root,
            identity.candidate_identity_sha256,
        )
        evidence_path = matrix_module.candidate_evidence_path(
            state_root,
            identity.candidate_identity_sha256,
        )
        published_text = (
            evidence_path.read_text(encoding="utf-8") if evidence_path.is_file() else ""
        )
        return BoundaryTestResult(
            command_exit_code=command_exit_code,
            child_exit_code=(
                session.last_facts.child_exit_code
                if session.last_facts is not None
                else None
            ),
            cancellation_observed=cancel_scope.cancel_called,
            cancellation_propagated=(
                cancel_scope.cancel_called and not completed_normally
            ),
            claimed=guard_path.is_file(),
            spawn_count=session.spawn_count,
            close_count=session.close_count,
            abort_count=session.abort_count,
            restart_count=session.restart_count,
            requests_replayed=session.requests_replayed,
            publication_count=int(evidence_path.is_file()),
            revalidation_count=revalidation_count,
            guard_path=guard_path,
            evidence_path=evidence_path,
            published_text=published_text,
            redirected_evidence=case_root / "unused-redirected-evidence",
            original_evidence=evidence_path,
        )

    def swap_parent_then_publish(self) -> BoundaryTestResult:
        case_root = self.root / f"swap-{uuid4().hex}"
        state_root = case_root / "state"
        state_root.mkdir(parents=True, mode=0o700)
        identity = SourceMatrixCandidateIdentity(
            source_contract_catalog_sha256=source_contract_catalog_sha256(),
            harness_build_sha256="b" * 64,
            candidate_identity_sha256="c" * 64,
        )
        claim = matrix_module._claim_candidate_guard_directory(
            lambda: matrix_module._open_state_guard_parent(
                state_root,
                identity.candidate_identity_sha256,
            ),
            identity,
        )
        assert claim is not None
        canonical_parent = state_root / "qa" / "analytics-source-matrix"
        moved_parent = state_root / "qa" / "analytics-source-matrix.claimed"
        redirected_parent = case_root / "redirected"
        redirected_candidate = redirected_parent / identity.candidate_identity_sha256
        redirected_candidate.mkdir(parents=True, mode=0o700)
        canonical_parent.rename(moved_parent)
        canonical_parent.symlink_to(redirected_parent, target_is_directory=True)
        original_evidence = (
            moved_parent / identity.candidate_identity_sha256 / "source-matrix.json"
        )
        redirected_evidence = redirected_candidate / "source-matrix.json"
        command_exit_code = 1
        try:
            try:
                matrix_module._publish_claimed_text(
                    claim,
                    '{"reason":"matrix_execution_failed","status":"failed"}',
                )
            except (OSError, ValueError):
                pass
            else:
                command_exit_code = 0
        finally:
            os.close(claim.descriptor)
        return BoundaryTestResult(
            command_exit_code=command_exit_code,
            child_exit_code=None,
            cancellation_observed=False,
            cancellation_propagated=False,
            claimed=True,
            spawn_count=0,
            close_count=0,
            abort_count=0,
            restart_count=0,
            requests_replayed=0,
            publication_count=int(original_evidence.exists())
            + int(redirected_evidence.exists()),
            revalidation_count=0,
            guard_path=moved_parent / identity.candidate_identity_sha256 / "claimed.json",
            evidence_path=original_evidence,
            published_text="",
            redirected_evidence=redirected_evidence,
            original_evidence=original_evidence,
        )


def recursive_scalar_values(
    value: JsonValue,
) -> frozenset[str | int | float | bool | None]:
    if isinstance(value, Mapping):
        return frozenset(
            scalar
            for child in value.values()
            for scalar in recursive_scalar_values(child)
        )
    if isinstance(value, Sequence) and not isinstance(value, str):
        return frozenset(
            scalar
            for child in value
            for scalar in recursive_scalar_values(child)
        )
    return frozenset({value})


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    ).hexdigest()


def matrix_payloads(
    prepared: PreparedMatrix,
) -> dict[str, deque[dict[str, JsonValue]]]:
    contracts = {contract.contract_id: contract for contract in prepared.contracts}
    source_profiles = _source_profiles(prepared)
    registered_payloads = [_state_payload(path) for path in _STATE_PATHS]
    registered_payloads.extend(
        _source_payload(
            contracts[profile.analytics_contract_id],
            profile,
        )
        for profile in source_profiles
        if profile.analytics_contract_id is not None
    )
    registered_payloads.extend(_state_payload(path) for path in _STATE_PATHS)
    ledger_events = [_ledger_event() for _payload in registered_payloads]
    return {
        "saxo_auth_status": deque([_auth_payload()]),
        "saxo_list_registered_endpoints": deque(_registry_payloads(prepared)),
        "saxo_get_session_capabilities": deque([_session_payload()]),
        "saxo_get_entitlements": deque([_entitlements_payload()]),
        "saxo_get_safe_request_ledger": deque(
            [_ledger_clear_payload(), _ledger_readback_payload(ledger_events)],
        ),
        "saxo_call_registered_endpoint": deque(registered_payloads),
    }


def expected_matrix_events(
    prepared: PreparedMatrix,
) -> list[tuple[str, dict[str, JsonValue]]]:
    events: list[tuple[str, dict[str, JsonValue]]] = [
        ("tools/list", {}),
        ("saxo_auth_status", {}),
    ]
    events.extend(
        ("saxo_list_registered_endpoints", arguments)
        for arguments in _registry_arguments(prepared)
    )
    events.extend(
        (
            ("saxo_get_session_capabilities", {}),
            ("saxo_get_entitlements", {}),
            ("saxo_get_safe_request_ledger", {"clear": True}),
            ("claim", {}),
        ),
    )
    state_arguments = [_registered_arguments(path=path) for path in _STATE_PATHS]
    events.extend(("saxo_call_registered_endpoint", arguments) for arguments in state_arguments)
    contracts = {contract.contract_id: contract for contract in prepared.contracts}
    for contract_id in _SOURCE_CONTRACT_ORDER:
        request = prepared.requests[contract_id]
        if request is None:
            continue
        contract = contracts[contract_id]
        path = _resolved_path(contract, request)
        params = {
            key: _render_query_value(value)
            for key, value in request.items()
            if key in contract.query_parameters
        }
        events.append(
            (
                "saxo_call_registered_endpoint",
                _registered_arguments(
                    path=path,
                    params=params,
                    analytics_contract_id=contract_id,
                ),
            ),
        )
    events.extend(("saxo_call_registered_endpoint", arguments) for arguments in state_arguments)
    events.append(("saxo_get_safe_request_ledger", {}))
    return events


def _source_profiles(prepared: PreparedMatrix) -> tuple[RegisteredCallProfile, ...]:
    return tuple(
        profile
        for profile in prepared.call_policy.registered_calls
        if profile.analytics_contract_id is not None
    )


def _registry_arguments(prepared: PreparedMatrix) -> list[dict[str, JsonValue]]:
    inventory = load_inventory()
    groups = sorted(
        {
            operation.service_group
            for contract in prepared.contracts
            if (operation := find_registered_operation("GET", contract.path_template)) is not None
        }
        | {
            operation.service_group
            for path in _STATE_PATHS
            if (operation := find_registered_operation("GET", path)) is not None
        },
    )
    return [
        {"service_group": group, "limit": 100, "offset": offset}
        for group in groups
        for offset in tuple(range(0, inventory.service_group_counts[group], 100)) or (0,)
    ]


def _registry_payloads(prepared: PreparedMatrix) -> list[dict[str, JsonValue]]:
    inventory = load_inventory()
    payloads: list[dict[str, JsonValue]] = []
    for arguments in _registry_arguments(prepared):
        group = cast("str", arguments["service_group"])
        offset = cast("int", arguments["offset"])
        selected = [
            operation
            for operation in inventory.operations
            if operation.service_group == group
        ]
        page = selected[offset : offset + 100]
        next_offset = offset + 100 if offset + 100 < len(selected) else None
        payloads.append(
            {
                "status": "metadata_only_not_ready_for_trading",
                "network_call_made": False,
                "operations": [
                    {
                        "operation_id": operation.operation_id,
                        "service_group": operation.service_group,
                        "method": operation.method,
                        "path_template": operation.path_template,
                        "read_write_class": operation.read_write_class,
                        "mcp_support_policy": (
                            "read_only_definition_registered"
                            if operation.status == "implemented" and operation.method == "GET"
                            else "refused"
                        ),
                        "refusal_reason": operation.refusal_reason,
                    }
                    for operation in page
                ],
                "next_offset": next_offset,
                "returned_count": len(page),
            },
        )
    return payloads


def _auth_payload() -> dict[str, JsonValue]:
    return {
        "status": "passed",
        "tool_name": "saxo_auth_status",
        "call_class": "local_status_succeeded",
        "requested_environment": "SIM",
        "effective_read_environment": "SIM",
        "live_reads": False,
        "live_writes": False,
        "sim_credentials_present": True,
        "sim_credential_source": "env",
        "live_credentials_present": False,
        "sim_redirect_uri_present": False,
        "pending_pkce_authorization_present": False,
        "token_cache_present": True,
        "token_cache_readable": True,
        "token_cache_expired": False,
        "token_cache_refresh_supported": False,
        "token_cache_environment": "SIM",
        "scope_used": False,
        "verifies": [],
        "does_not_verify": [],
        "blocking_reasons": [],
        "next_action": "test readiness action",
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }


def _session_payload() -> dict[str, JsonValue]:
    return {
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


def _entitlements_payload() -> dict[str, JsonValue]:
    return {
        "status": "passed",
        "tool_name": "saxo_get_entitlements",
        "environment": "SIM",
        "call_class": "sim_read_succeeded",
        "endpoint_path": "/port/v1/users/me/entitlements",
        "entitlement_field_set": "Default",
        "token_refreshed": False,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
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


def _state_payload(path: str) -> dict[str, JsonValue]:
    operation = find_registered_operation("GET", path)
    assert operation is not None
    return {
        "status": "passed",
        "operation_id": operation.operation_id,
        "method": "GET",
        "path": operation.path_template,
        "environment": "SIM",
        "network_call_made": True,
        "response_visibility": "fingerprint_only",
        "response": None,
        "response_fingerprint": "a" * 64,
        "response_fingerprint_scope": (
            "account_money_state_fields"
            if path == "/port/v1/balances/me"
            else "raw_response_body"
        ),
        "http_status": 200,
    }


def _source_payload(
    contract: SourceContract,
    profile: RegisteredCallProfile,
) -> dict[str, JsonValue]:
    operation = find_registered_operation("GET", profile.path)
    assert operation is not None
    page_count = 2 if contract.contract_id == "chart_v3" else 1
    quality: dict[str, JsonValue] = {
        "state": "complete" if contract.source_kind == "info_prices" else "not_applicable",
        "entitlement_limited_fields": [],
        "delayed_fields": [],
        "missing_fields": [],
    }
    pages: list[dict[str, JsonValue]] = [
        {
            "page_number": page_number,
            "row_count": 1 if page_number == 1 else 0,
            "page_fingerprint_sha256": f"{page_number:x}" * 64,
            "source_revision_sha256": "e" * 64,
            "schema_fingerprint_sha256": "f" * 64,
            "timestamp_value_count": 1 if page_number == 1 else 0,
            "timestamp_fingerprint_sha256": "9" * 64,
            "source_quality": dict(quality),
        }
        for page_number in range(1, page_count + 1)
    ]
    request_fingerprint = _request_fingerprint(contract, profile.path, dict(profile.params))
    return {
        "status": "passed",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "sim_read_succeeded",
        "operation_id": operation.operation_id,
        "service_group": operation.service_group,
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
        "auth_exercised": operation.auth_requirement != "none",
        "trading_ready": False,
        "response_visibility": "analytics_contract_receipt",
        "response": None,
        "response_fingerprint": canonical_digest(pages),
        "response_fingerprint_scope": "analytics_contract_receipt",
        "analytics_contract_id": contract.contract_id,
        "analytics_contract_sha256": source_contract_fingerprint(contract),
        "page_count": page_count,
        "row_count": 1,
        "continuation_call_count": page_count - 1,
        "page_receipts": cast("list[JsonValue]", pages),
        "source_quality": quality,
        "source_revision_fingerprint_sha256": canonical_digest(["e" * 64] * page_count),
        "timestamp_value_count": 1,
        "timestamp_fingerprint_sha256": canonical_digest(["9" * 64] * page_count),
        "request_fingerprint_sha256": request_fingerprint,
        "http_status": 200,
    }


def _request_fingerprint(
    contract: SourceContract,
    resolved_path: str,
    params: dict[str, str],
) -> str:
    request: dict[str, object] = dict(params)
    for template, resolved in zip(
        contract.path_template.strip("/").split("/"),
        resolved_path.strip("/").split("/"),
        strict=True,
    ):
        if template.startswith("{") and template.endswith("}"):
            request[template[1:-1]] = resolved
    return canonical_digest({"contract_id": contract.contract_id, "request": request})


def _ledger_clear_payload() -> dict[str, JsonValue]:
    return {
        "status": "cleared",
        "tool_name": "saxo_get_safe_request_ledger",
        "scope": "current_mcp_session",
        "safe_fields_only": True,
        "ledger_complete": True,
        "events_evicted": 0,
        "negative_proof_available": True,
        "request_count": 0,
        "non_get_request_count": 0,
        "unsafe_gateway_request_detected": False,
        "order_placement_endpoint_called": False,
        "events": [],
    }


def _ledger_event() -> dict[str, JsonValue]:
    return {
        "timestamp": "2026-07-31T12:00:00+00:00",
        "phase": "attempted",
        "host_role": "gateway",
        "environment": "SIM",
        "method": "GET",
        "path": "/sim/openapi/{redacted}",
        "query_names": [],
        "query_present": False,
        "status": None,
    }


def _ledger_readback_payload(
    events: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    return {
        "status": "passed",
        "tool_name": "saxo_get_safe_request_ledger",
        "scope": "current_mcp_session",
        "safe_fields_only": True,
        "ledger_complete": True,
        "events_evicted": 0,
        "negative_proof_available": True,
        "request_count": len(events),
        "non_get_request_count": 0,
        "unsafe_gateway_request_detected": False,
        "order_placement_endpoint_called": False,
        "events": cast("list[JsonValue]", events),
    }


def _registered_arguments(
    *,
    path: str,
    params: Mapping[str, str] | None = None,
    analytics_contract_id: str | None = None,
) -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {
        "method": "GET",
        "path": path,
        "response_mode": (
            "analytics_contract_receipt"
            if analytics_contract_id is not None
            else "fingerprint_only"
        ),
    }
    if params:
        arguments["params"] = dict(params)
    if analytics_contract_id is not None:
        arguments["analytics_contract_id"] = analytics_contract_id
    return arguments


def _resolved_path(contract: SourceContract, request: Mapping[str, object]) -> str:
    path = contract.path_template
    for parameter in contract.path_parameters:
        path = path.replace(f"{{{parameter}}}", str(request[parameter]))
    return path


def _render_query_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return ",".join(str(item) for item in items)
    raise TypeError("unsupported scripted query value")
