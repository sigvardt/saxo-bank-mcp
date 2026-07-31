# Saxo Analytics Source Matrix Process Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Task 6 in-process analytics source-matrix server with one sealed installed coordinator, one distinct six-tool MCP child over stdio, and descriptor-bound evidence that is finalized only after child exit and static runtime revalidation.

**Architecture:** The coordinator validates a portable candidate identity and a descriptor-held per-run seal, derives a fresh SIM-only child environment, and drives one direct operating-system child through one `mcp.client.session.ClientSession`. Matrix logic consumes a narrow session protocol, while process lifecycle, static runtime validation, guard ownership, evidence publication, and privacy scanning remain separate finite boundaries.

**Tech Stack:** Python 3.12, AnyIO 4.14.1, MCP Python SDK 1.28.1, FastMCP 3.4.2 in the child only, Pydantic 2, pytest, Ruff, BasedPyright, POSIX file descriptors, and the existing Saxo SIM receipt, filter, ledger, guard, and secret-scan code.

## Global Constraints

- Limit changes to the Task 6 official analytics source-matrix harness and its installed runtime. Do not change the 18 source contracts, provider behavior, analytics store, database migrations, registered endpoint schemas, or existing MCP tool input and output schemas.
- Keep the production server unfiltered by default with all 39 existing tools. The six-tool surface applies only to the explicitly filtered source-matrix child.
- The official coordinator must not import `FastMCP`, `Client`, `FastMCPTransport`, or `saxo_bank_mcp.server`, construct a live server object, or call `Client(server)`.
- Start exactly one child with the exact installed interpreter, no shell, no `PATH` lookup, exactly one MCP session, one initialize, one `tools/list`, zero reconnects, and zero restarts.
- The child tool tuple is exactly `saxo_auth_status`, `saxo_list_registered_endpoints`, `saxo_get_session_capabilities`, `saxo_get_entitlements`, `saxo_get_safe_request_ledger`, and `saxo_call_registered_endpoint`.
- Build the child environment from an empty mapping. Set `SAXO_MCP_ENVIRONMENT=SIM`, `SAXO_MCP_ENABLE_LIVE_READS=0`, `SAXO_MCP_ENABLE_LIVE_WRITES=` with the key present, `SAXO_MCP_EVAL_TOOL_FILTER=1`, and the exact comma-separated six-tool allowlist.
- Pass only effective SIM authentication inputs, a coordinator-created `TMPDIR`, `LC_ALL=C`, `LANG=C`, and an optional verified runtime-local `SSL_CERT_FILE` or `SSL_CERT_DIR`. Pass no `PATH`, proxy, LIVE, QA fixture, analytics, store, audit, write-safety, unrelated `SAXO_MCP_*`, or `PYTHON*` setting.
- Refuse before spawn if the caller supplied `SAXO_MCP_EVAL_TOOL_FILTER` or `SAXO_MCP_EVAL_ALLOWED_TOOLS`, even with an empty value, or if SIM and LIVE-gate values are not exact.
- Launch coordinator and child with `-I -B -S` and one fixed external `pycache_prefix`. The child bootstrap must validate executable, prefixes, flags, cache, working directory, `TMPDIR`, site-packages, and fixed stdio arguments before importing the server.
- Seal candidate directories `0500`, regular source and data files `0400`, and executable files `0500`. Allow only `bin/python` and `bin/python3` symlinks resolving to the recorded regular `bin/python3.12` interpreter.
- Keep coordinator and child cache, working, and temporary directories outside the candidate. Create mutable directories `0700`, mutable files `0600`, and require every cache, working directory, and `TMPDIR` to be empty before launch and after child exit.
- Compute a portable identity from normalized relative runtime entries and a per-run seal from device, inode, uid, mode, size, `mtime_ns`, `ctime_ns`, and canonical ancestor identities. Walk the runtime with held no-follow descriptors before launch and after the reaped child exits.
- Restrict coordinator scalar checks to exact executable, `sys.path`, `sys.prefix`, `sys.base_prefix`, isolated flags, bytecode flag, no-site flag, cache prefix, implementation, version, and integer conversion limit.
- Remove all arbitrary live-object projection and stabilization code. Do not traverse `sys.modules`, globals, closures, code constants, classes, metaclasses, descriptors, import hooks, caches, aliases, or other heap graphs.
- Preserve exact protocol order: local proof, spawn, initialize, list tools, auth, registry, session, entitlements, ledger clear, claim, before-state reads, 18-source canonical plan, after-state reads, ledger readback, close and reap, static revalidation, process receipt, privacy scan, publication.
- Treat this as a new candidate identity. Add `process_boundary` only to new full receipts; do not migrate, rewrite, reuse, or reinterpret historical Task 6 artifacts. Keep historical two-key failure artifacts unchanged.
- Use the exact preclaim reasons and postclaim two-key failure reasons from the approved design. A post-exit static mismatch has first precedence, a child/process failure has second precedence, matrix execution has third precedence, and privacy failure has last precedence.
- Never serialize raw PIDs, descriptors, pipe metadata, paths, stderr, exceptions, environment values, fixtures, tokens, raw MCP messages, or response bodies. Hash operating-system identities with canonical compact JSON and discard stderr bytes after counting them in memory.
- Add every named RED test before its owning production-code step. Process tests must use a real subprocess and real pipes; pure matrix tests must use a narrow scripted session and never a `FastMCP` object.
- Do not perform an official SIM run, authentication action, Saxo request, state read, source call, ledger clear, candidate claim, guard write, or evidence publication during this implementation. All tests use temporary state roots and deterministic local fixtures.
- The real SIM acceptance remains blocked until ready SIM authentication exists. A successful offline implementation does not complete Task 6.

---

## File Map

### New focused modules

- `src/saxo_bank_mcp/analytics_source_process.py`: owns the exact child tool contract, fixed child environment and command, registered-call profiles, direct pipes, stdio framing, one MCP session, process facts, and irreversible lifecycle state.
- `src/saxo_bank_mcp/analytics_source_runtime.py`: owns schema-5 candidate models, portable runtime projection, finite coordinator scalar proof, descriptor-held per-run seal, external run-directory validation, and prelaunch/post-exit comparison.
- `tests/test_analytics_source_process_boundary.py`: owns the 18 exact process-boundary acceptance tests from the approved design.
- `tests/analytics_source_matrix_support.py`: owns `ScriptedMatrixSession`, safe deterministic receipt builders, event recording, and value-free process-event test doubles used by matrix tests.
- `tests/fixtures/analytics/source_matrix_stdio_child.py`: a stdlib-only JSON-RPC child used to prove real process, pipe, initialize, list, malformed-frame, early-exit, and nonzero-exit behavior without Saxo access.
- `tests/fixtures/analytics/source_matrix_fixture_server.py`: a fixed six-tool raw MCP server used only in a temporary, separately sealed fixture candidate for the installed-launcher artifact check.

### Existing source and packaging files

- `src/saxo_bank_mcp/qa_analytics_source_matrix.py`: remove the in-process transport and heap projection; consume `MatrixSession`; enforce protocol order; own matrix outcomes, process receipt, claim state, privacy, failure precedence, and descriptor-bound publication.
- `src/saxo_bank_mcp/generate_analytics_source_matrix_candidate.py`: import finite runtime helpers from `analytics_source_runtime`, emit schema 5, and bind the normalized runtime tree and child bootstrap.
- `src/saxo_bank_mcp/server_eval_tool_filter.py`: retain behavior; use its existing `derive_eval_tool_filter_env` and `resolve_eval_tool_filter` as the single child registration control.
- `scripts/prepare_analytics_source_matrix_runtime.py`: copy the Python base into the candidate, create the venv from that copy, install offline, remove caches, normalize interpreter aliases, and apply final `0400` and `0500` modes.
- `scripts/saxo-bank-analytics-source-matrix`: derive a unique external run root, launch the coordinator with fixed isolated arguments, and clean empty run directories after return.
- `scripts/saxo-bank-analytics-source-matrix-generate`: use the same external-cache bootstrap for candidate generation.
- `scripts/run_analytics_source_matrix.py`: keep the development wrapper explicit, isolated, and external-cache based without claiming to be an official candidate.
- `scripts/generate_analytics_source_matrix_candidate.py`: keep the development generator wrapper explicit, isolated, and external-cache based.
- `pyproject.toml`: declare direct dependencies on the AnyIO and MCP APIs imported by the new process component.
- `uv.lock`: record the unchanged resolved AnyIO 4.14.1 and MCP SDK 1.28.1 packages as direct project dependencies.
- `data/analytics/source_matrix_candidate.json`: regenerate schema-5 source, installed, runtime, bootstrap, and candidate identities after all source changes settle.
- `docs/analytics-bi-vision.md`: replace the in-process execution-closure wording and installed-runtime commands with the approved process-boundary workflow and blocked real-SIM handoff.

### Existing tests to migrate

- `tests/test_qa_analytics_source_matrix.py`: replace its in-process `FastMCP` fixture with `ScriptedMatrixSession`; retain environment, source reduction, state, ledger, privacy, and one-shot guard coverage.
- `tests/test_area_b_review_fixes.py`: update installed identity and launcher assertions for schema 5, a separate child, and external caches.
- `tests/test_area_b_review_round2.py`: retain candidate mutation, guard, receipt, retry-role, stable-key, and replay proofs; update candidate builders for schema 5.
- `tests/test_area_b_review_round3.py`: retain installed package, guard-walk, strict readiness, source receipt, attempt-role, and revision tests; update portable entry and mode expectations.
- `tests/test_area_b_review_round4.py`: retain installed closure, cache refusal, registry, HTTP, and strict receipt tests; replace import-heap assumptions with static runtime metadata.
- `tests/test_area_b_review_round5.py`: remove the live interpreter projection test and replace the launcher test with two owner-nonwritable builds and external empty caches.
- `tests/test_area_b_review_round6.py`: remove the complete-heap identity test, adapt failure precedence to post-exit static change, and retain the swapped-ancestor publication proof.
- `tests/test_area_b_review_round7.py`: delete; both tests exist only to widen arbitrary live-object projection.
- `tests/test_agent_skill_eval_tool_filter.py`: retain the existing production-unfiltered and explicit-filter checks; add no second registry or alternate filter.
- `tests/test_analytics_source_receipt.py`, `tests/test_registered_call_tool.py`, `tests/test_registered_call_tool_failures.py`, and `tests/test_safe_request_ledger_tool.py`: retain server-side strict receipt, registered GET, and safe-ledger behavior unchanged.

## Shared Interfaces and Fixed Values

Use these names and signatures in every task:

```python
SOURCE_MATRIX_CHILD_TOOLS: Final[tuple[str, ...]] = (
    "saxo_auth_status",
    "saxo_list_registered_endpoints",
    "saxo_get_session_capabilities",
    "saxo_get_entitlements",
    "saxo_get_safe_request_ledger",
    "saxo_call_registered_endpoint",
)

type ProcessState = Literal[
    "new", "spawned", "initialized", "listed", "running", "closing", "exited"
]

class MatrixSession(Protocol):
    async def list_tools_once(self) -> tuple[str, ...]:
        raise NotImplementedError

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        raise NotImplementedError

@dataclass(frozen=True, slots=True)
class ChildBootstrapPaths:
    runtime_root: Path = field(repr=False)
    executable: Path = field(repr=False)
    site_packages: Path = field(repr=False)
    pycache_prefix: Path = field(repr=False)
    workdir: Path = field(repr=False)
    tmpdir: Path = field(repr=False)

@dataclass(frozen=True, slots=True)
class ChildLaunchConfig:
    command: tuple[str, ...] = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    cwd: Path = field(repr=False)
    executable_identity_sha256: str

def build_child_environment(
    caller_env: Mapping[str, str],
    *,
    tmpdir: Path,
    ssl_runtime_entry: tuple[Literal["SSL_CERT_FILE", "SSL_CERT_DIR"], Path] | None = None,
) -> dict[str, str]:
    raise NotImplementedError

def build_child_launch_config(
    paths: ChildBootstrapPaths,
    caller_env: Mapping[str, str],
    *,
    ssl_runtime_entry: tuple[Literal["SSL_CERT_FILE", "SSL_CERT_DIR"], Path] | None = None,
) -> ChildLaunchConfig:
    raise NotImplementedError

@dataclass(frozen=True, slots=True)
class RegisteredCallProfile:
    path: str
    params: Mapping[str, str]
    response_mode: Literal["fingerprint_only", "analytics_contract_receipt"]
    analytics_contract_id: str | None

@dataclass(slots=True)
class MatrixCallPolicy:
    registry_page_offsets: Mapping[str, tuple[int, ...]]
    registered_calls: tuple[RegisteredCallProfile, ...]
    _registry_positions: dict[str, int] = field(init=False, repr=False)
    _ledger_phase: Literal["clear", "readback", "complete"] = field(
        init=False,
        repr=False,
        default="clear",
    )

    def validate(self, name: str, arguments: dict[str, JsonValue]) -> None:
        raise NotImplementedError

    def observe(
        self,
        name: str,
        arguments: dict[str, JsonValue],
        payload: Mapping[str, JsonValue],
    ) -> None:
        raise NotImplementedError

@dataclass(frozen=True, slots=True)
class ProcessSessionFacts:
    coordinator_pid: int = field(repr=False)
    child_pid: int = field(repr=False)
    executable_identity_sha256: str
    stdin_identity: PipeIdentity = field(repr=False)
    stdout_identity: PipeIdentity = field(repr=False)
    stderr_byte_count: int = field(repr=False)
    listed_tool_names: tuple[str, ...] = field(repr=False)
    child_spawn_count: Literal[1]
    mcp_session_count: Literal[1]
    mcp_initialize_count: Literal[1]
    tool_list_count: Literal[1]
    reconnect_count: Literal[0]
    restart_count: Literal[0]
    child_exit_code: int
    stdout_protocol_only: bool

class ProcessMatrixSession(MatrixSession, Protocol):
    async def spawn(self) -> None:
        raise NotImplementedError

    async def initialize(self) -> None:
        raise NotImplementedError

    async def list_tools_once(self) -> tuple[str, ...]:
        raise NotImplementedError

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        raise NotImplementedError

    async def close(self) -> ProcessSessionFacts:
        raise NotImplementedError

    async def abort(self) -> ProcessSessionFacts:
        raise NotImplementedError

class OneShotProcessSession(ProcessMatrixSession):
    """The sole production implementation of ProcessMatrixSession."""
```

The canonical contract order is fixed to the order checked into `data/analytics/source_contracts.json`:

```python
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
```

### Task 1: Define the finite child configuration and call policy

**Files:**

- Create: `src/saxo_bank_mcp/analytics_source_process.py`
- Create: `tests/test_analytics_source_process_boundary.py`
- Modify: `pyproject.toml:7-18`
- Modify: `uv.lock`
- Verify unchanged: `src/saxo_bank_mcp/server_eval_tool_filter.py`
- Verify unchanged: `tests/test_agent_skill_eval_tool_filter.py`

**Interfaces:**

- Consumes: `JsonValue` from `saxo_bank_mcp._evidence`; `derive_eval_tool_filter_env` and `resolve_eval_tool_filter` from `server_eval_tool_filter`; `resolve_sim_auth_settings` from `config`.
- Produces: `SOURCE_MATRIX_CHILD_TOOLS`, `CHILD_TOOL_IDS_SHA256`, `MatrixSession`, `ChildBootstrapPaths`, `ChildLaunchConfig`, `RegisteredCallProfile`, `MatrixCallPolicy`, `build_child_environment(...)`, and `build_child_launch_config(...)` with the signatures fixed below.

- [ ] **Step 1: Add direct dependency declarations without changing resolved versions**

  Add these entries to `[project].dependencies` in lexical order and run the lock check:

  ```toml
  "anyio==4.14.1",
  "mcp==1.28.1",
  ```

  Run: `uv lock --check`

  Expected: PASS; `uv.lock` still resolves AnyIO 4.14.1, MCP 1.28.1, and FastMCP 3.4.2.

- [ ] **Step 2: Write the three owning RED tests**

  First add these exact local fixtures. The runtime tree is synthetic and never used as an official identity:

  ```python
  @pytest.fixture
  def sealed_child_paths(tmp_path: Path) -> ChildBootstrapPaths:
      runtime = tmp_path / "runtime"
      executable = runtime / "bin/python3.12"
      site_packages = runtime / "lib/python3.12/site-packages"
      executable.parent.mkdir(parents=True, mode=0o700)
      site_packages.mkdir(parents=True, mode=0o700)
      shutil.copy2(sys.executable, executable)
      executable.chmod(0o500)
      for directory in (site_packages, site_packages.parent, site_packages.parent.parent, runtime / "bin", runtime):
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
  ```

  Then add these exact test names before creating the production definitions:

  ```python
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
  ```

- [ ] **Step 3: Run the RED tests and record the expected boundary failure**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_child_command_uses_exact_installed_interpreter_and_isolated_bootstrap \
    tests/test_analytics_source_process_boundary.py::test_ambient_filter_or_live_configuration_refuses_before_spawn \
    tests/test_analytics_source_process_boundary.py::test_registered_call_profile_refuses_unsealed_tool_arguments -v
  ```

  Expected: FAIL during collection because `saxo_bank_mcp.analytics_source_process` does not exist.

- [ ] **Step 4: Add exact constants, value-free errors, and narrow types**

  Create the module with these public definitions. Keep secret-bearing fields out of `repr` and do not implement process spawning in this task.

  ```python
  SOURCE_MATRIX_CHILD_TOOLS: Final[tuple[str, ...]] = (
      "saxo_auth_status",
      "saxo_list_registered_endpoints",
      "saxo_get_session_capabilities",
      "saxo_get_entitlements",
      "saxo_get_safe_request_ledger",
      "saxo_call_registered_endpoint",
  )
  CHILD_TOOL_IDS_SHA256: Final = _digest(tuple(sorted(SOURCE_MATRIX_CHILD_TOOLS)))

  type ChildConfigurationReason = Literal[
      "ambient_filter_configuration",
      "environment_not_sim",
      "live_reads_enabled",
      "live_writes_enabled",
      "child_environment_invalid",
      "child_path_invalid",
      "registered_call_profile_invalid",
      "tool_call_profile_invalid",
  ]

  @dataclass(frozen=True, slots=True)
  class ChildConfigurationError(ValueError):
      reason: ChildConfigurationReason

      def __str__(self) -> str:
          return self.reason
  ```

  `_digest` must use `json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)` and UTF-8 before SHA-256.

- [ ] **Step 5: Implement the empty-base child environment derivation**

  Use this signature and exact key sets:

  ```python
  def build_child_environment(
      caller_env: Mapping[str, str],
      *,
      tmpdir: Path,
      ssl_runtime_entry: tuple[Literal["SSL_CERT_FILE", "SSL_CERT_DIR"], Path] | None = None,
  ) -> dict[str, str]:
      """Return the complete child environment or refuse without retaining values."""
  ```

  The implementation sequence is fixed:

  1. Reject presence of either filter key in `caller_env`.
  2. Require exact `SIM`, `0`, and empty LIVE-write values.
  3. Call `resolve_sim_auth_settings(caller_env)` to validate the selected credential source, trusted SIM auth and token URLs, redirect URI, and token cache.
  4. Preserve exactly one of `SAXO_MCP_SIM_APP_KEY`, `SAXO_MCP_SIM_CLIENT_ID`, or the resolved absolute `SAXO_MCP_SIM_CREDENTIAL_FILE`, then set explicit redirect, auth URL, token URL, and token cache path.
  5. Start from the fixed SIM, LIVE-gate, `TMPDIR`, `LC_ALL`, and `LANG` mapping. Do not copy `caller_env`.
  6. Add the optional SSL key only after a no-follow runtime-seal check supplied by Task 3.
  7. Call `derive_eval_tool_filter_env(base, SOURCE_MATRIX_CHILD_TOOLS)`.
  8. Require `resolve_eval_tool_filter(child) == frozenset(SOURCE_MATRIX_CHILD_TOOLS)` and require every final key to be in `_CHILD_ENV_KEYS`.

  Catch SIM-setting, path, and filter exceptions at this boundary and raise the matching `ChildConfigurationError` with `from None`; do not copy the source exception or its arguments. Define `_CHILD_ENV_KEYS` as the five fixed control keys, seven listed SIM auth keys, `TMPDIR`, `LC_ALL`, `LANG`, `SSL_CERT_FILE`, and `SSL_CERT_DIR`. Assert no key starts with `PYTHON`, no key contains `PROXY`, and no key starts with `SAXO_MCP_` unless explicitly listed.

- [ ] **Step 6: Implement the immutable bootstrap and exact command builder**

  Store one literal `CHILD_BOOTSTRAP` in the module. It must perform checks before its only package import and end with this fixed server invocation:

  ```python
  sys.path.insert(0, site_packages)
  sys.argv[:] = ["saxo-bank-mcp", "--transport", "stdio"]
  runpy.run_module("saxo_bank_mcp.server", run_name="__main__", alter_sys=True)
  ```

  Before those lines, validate all of the following and raise `SystemExit("child_bootstrap_refused")` for any mismatch:

  ```python
  required_flags = (
      sys.flags.isolated == 1,
      sys.flags.dont_write_bytecode == 1,
      sys.flags.no_site == 1,
      sys.flags.ignore_environment == 1,
      sys.flags.safe_path is True,
  )
  required_paths = (
      os.path.samefile(sys.executable, executable),
      Path(sys.prefix).is_relative_to(runtime_root),
      Path(sys.base_prefix).is_relative_to(runtime_root),
      Path(site_packages).is_relative_to(runtime_root),
      Path(sys.pycache_prefix or "") == pycache_prefix,
      Path.cwd() == workdir,
      Path(os.environ.get("TMPDIR", "")) == tmpdir,
  )
  ```

  `build_child_launch_config(paths, caller_env)` must reject nonabsolute or symlinked executable, site, cache, work, or temp paths; require executable and site inside `runtime_root`; require cache, work, and temp outside it; hash the regular executable through a no-follow descriptor; and return this command:

  ```python
  command = (
      str(paths.executable),
      "-I",
      "-B",
      "-S",
      "-X",
      f"pycache_prefix={paths.pycache_prefix}",
      "-c",
      CHILD_BOOTSTRAP,
      str(paths.runtime_root),
      str(paths.executable),
      str(paths.site_packages),
      str(paths.pycache_prefix),
      str(paths.workdir),
      str(paths.tmpdir),
  )
  ```

  The bootstrap itself installs `--transport stdio`; the caller cannot append transport or server arguments.

- [ ] **Step 7: Implement the stateful local call policy**

  `MatrixCallPolicy.validate(name, arguments)` must accept only:

  - `{}` for auth, session capabilities, and entitlements;
  - `{"clear": True}` once, followed by `{}` once, for the ledger;
  - registry calls with a sealed service group, `limit=100`, and the next member of that group's precomputed offset tuple;
  - a `saxo_call_registered_endpoint` mapping equal to one precomputed `RegisteredCallProfile`, including exact method `GET`, path, string parameters, response mode, and matching or absent contract ID.

  Build each group's offset tuple as `tuple(range(0, registered_group_row_count, 100)) or (0,)` from the sealed local endpoint registry. Require returned `next_offset` to equal the next tuple member or be absent after its final member. Reject a duplicate ledger phase, skipped or replayed registry offset, unknown tool, extra key, qualified name, wildcard, URL, unsealed path, non-GET method, wrong response mode, wrong contract ID, or parameter difference with `ChildConfigurationError("tool_call_profile_invalid")`.

- [ ] **Step 8: Run the focused GREEN tests and unchanged filter tests**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_child_command_uses_exact_installed_interpreter_and_isolated_bootstrap \
    tests/test_analytics_source_process_boundary.py::test_ambient_filter_or_live_configuration_refuses_before_spawn \
    tests/test_analytics_source_process_boundary.py::test_registered_call_profile_refuses_unsealed_tool_arguments \
    tests/test_agent_skill_eval_tool_filter.py -v
  uv run ruff check src/saxo_bank_mcp/analytics_source_process.py tests/test_analytics_source_process_boundary.py
  uv run basedpyright src/saxo_bank_mcp/analytics_source_process.py tests/test_analytics_source_process_boundary.py
  ```

  Expected: all selected tests pass; production remains unfiltered at 39 tools and the explicit child filter resolves to exactly six.

- [ ] **Step 9: Commit the finite child contract**

  ```bash
  git add pyproject.toml uv.lock \
    src/saxo_bank_mcp/analytics_source_process.py \
    tests/test_analytics_source_process_boundary.py
  git commit -m "feat: define analytics child boundary"
  ```

### Task 2: Implement the one-shot direct stdio process session

**Files:**

- Modify: `src/saxo_bank_mcp/analytics_source_process.py`
- Modify: `tests/test_analytics_source_process_boundary.py`
- Create: `tests/fixtures/analytics/source_matrix_stdio_child.py`

**Interfaces:**

- Consumes: `ChildLaunchConfig`, `MatrixSession`, `SOURCE_MATRIX_CHILD_TOOLS`, `CHILD_TOOL_IDS_SHA256` from Task 1; `ClientSession` and `SessionMessage` from MCP SDK 1.28.1; `anyio.open_process` from AnyIO 4.14.1.
- Produces: `PipeIdentity`, `ProcessSessionFacts`, `ProcessSessionError`, `ProcessMatrixSession`, and the complete `OneShotProcessSession` interface declared in Shared Interfaces and Fixed Values.

- [ ] **Step 1: Add a stdlib-only deterministic JSON-RPC fixture child**

  The fixture reads one JSON object per UTF-8 line from stdin and writes one compact JSON object per line to stdout. It must implement these methods without importing project code:

  ```python
  if method == "initialize":
      result = {
          "protocolVersion": params["protocolVersion"],
          "capabilities": {"tools": {}},
          "serverInfo": {"name": "sealed-source-matrix-fixture", "version": "1"},
      }
  elif method == "tools/list":
      result = {
          "tools": [
              {
                  "name": name,
                  "description": "sealed fixture",
                  "inputSchema": {"type": "object", "additionalProperties": True},
              }
              for name in SOURCE_MATRIX_CHILD_TOOLS_LITERAL
          ],
      }
  elif method == "tools/call":
      result = {"content": [], "structuredContent": {"status": "passed"}, "isError": False}
  ```

  Ignore the `notifications/initialized` notification. Select fixture behavior only from a fixed positional scenario literal in `{"normal", "early_exit", "malformed", "truncated", "nonzero"}`. Never read environment values or files.

- [ ] **Step 2: Write the two process-session RED tests**

  Define the real-process fixture without using the official command builder:

  ```python
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
  ```

  This constructor is a unit seam only; the official coordinator obtains its config exclusively from `build_child_launch_config`. Add these exact names with a real fixture subprocess:

  ```python
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
  ```

- [ ] **Step 3: Run the RED process tests**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_process_session_spawns_one_distinct_child_over_stdio \
    tests/test_analytics_source_process_boundary.py::test_process_session_cannot_initialize_connect_or_spawn_twice -v
  ```

  Expected: FAIL because `OneShotProcessSession` has no process implementation.

- [ ] **Step 4: Add process state, raw pipe identity, and value-free errors**

  Use these exact finite records:

  ```python
  @dataclass(frozen=True, slots=True)
  class PipeIdentity:
      endpoint_kind: Literal["fifo"]
      parent_device: int = field(repr=False)
      parent_inode: int = field(repr=False)
      child_device: int = field(repr=False)
      child_inode: int = field(repr=False)

      def canonical_private_material(self) -> dict[str, int]:
          return {
              "child_device": self.child_device,
              "child_inode": self.child_inode,
              "parent_device": self.parent_device,
              "parent_inode": self.parent_inode,
          }

  type ProcessFailureReason = Literal[
      "invalid_transition",
      "spawn_failed",
      "initialize_failed",
      "tool_list_failed",
      "protocol_failed",
      "call_failed",
      "shutdown_failed",
      "nonzero_exit",
  ]

  @dataclass(frozen=True, slots=True)
  class ProcessSessionError(RuntimeError):
      reason: ProcessFailureReason

      def __str__(self) -> str:
          return self.reason
  ```

  Do not attach the originating exception, command, PID, path, stderr, frame, or environment to `ProcessSessionError`.

- [ ] **Step 5: Create three direct pipe pairs and start one process**

  `spawn()` must:

  1. Require state `new`, then increment `child_spawn_count` before the only `anyio.open_process` call.
  2. Create stdin, stdout, and stderr pairs with `os.pipe2(os.O_CLOEXEC)`.
  3. `fstat` both ends, require FIFO mode and matching device/inode within each pair, and require stdin, stdout, and stderr pairs to be distinct.
  4. Pass child read stdin, child write stdout, and child write stderr as integer descriptors to `anyio.open_process(config.command, env=config.environment, cwd=config.cwd, start_new_session=True)`.
  5. Close child-side descriptors in the coordinator immediately after spawn and make parent endpoints nonblocking.
  6. Record the actual `process.pid` and require it differs from `os.getpid()`.
  7. Create zero-buffer memory streams and exactly one `ClientSession`; do not call `mcp.client.stdio.stdio_client` or any FastMCP transport.

  On any failure, close every created descriptor, terminate and reap only the same child if it exists, set state `exited`, and raise `ProcessSessionError("spawn_failed")`.

- [ ] **Step 6: Implement strict stdout framing, stdin writing, and stderr discard**

  The stdout pump must buffer at most `_MAX_PROTOCOL_LINE_BYTES = 1_048_576`, require strict UTF-8, reject blank or malformed lines, parse each complete line with `types.JSONRPCMessage.model_validate_json`, and send `SessionMessage(message)` to the MCP memory stream. EOF with a partial line is `protocol_failed`.

  The stdin pump must serialize only `session_message.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"` and write all bytes after `anyio.wait_writable(fd)`. It closes the parent stdin descriptor exactly once.

  The stderr pump must repeatedly `os.read` after `anyio.wait_readable`, add only byte lengths to a saturating integer counter, retain no chunks or decoded text, and close at EOF.

- [ ] **Step 7: Implement one MCP lifecycle and tool result conversion**

  Use the exact transitions and counts:

  ```python
  async def initialize(self) -> None:
      self._require_state("spawned")
      self._mcp_session_count = 1
      self._mcp_initialize_count = 1
      await self._client_session.__aenter__()
      await self._client_session.initialize()
      self._state = "initialized"

  async def list_tools_once(self) -> tuple[str, ...]:
      self._require_state("initialized")
      self._tool_list_count = 1
      result = await self._client_session.list_tools()
      names = tuple(tool.name for tool in result.tools)
      self._listed_tool_names = names
      self._state = "listed"
      return names
  ```

  `call_tool` must require `listed` or `running`, reject names outside `SOURCE_MATRIX_CHILD_TOOLS`, set state `running`, call once with `raise_on_error` unavailable at the SDK layer, require `isError is not True`, and validate `structuredContent` as a JSON object. Never replay a failed call.

- [ ] **Step 8: Implement bounded close and same-child cleanup**

  `close()` must set `closing`, close the MCP session and parent stdin, wait at most 2.0 seconds for normal exit, drain stdout and stderr, and require zero exit. A timeout may terminate, then kill and reap the same child, but still raises `shutdown_failed`; nonzero raises `nonzero_exit`.

  `abort()` accepts any spawned non-exited state, closes stdin, sends terminate, waits at most 2.0 seconds, sends kill only after that timeout, reaps the same process, closes every stream and descriptor, and returns facts or raises one value-free process error. Neither method can spawn, initialize, reconnect, or call a tool.

- [ ] **Step 9: Run process tests, lint, and types**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_process_session_spawns_one_distinct_child_over_stdio \
    tests/test_analytics_source_process_boundary.py::test_process_session_cannot_initialize_connect_or_spawn_twice -v
  uv run ruff check \
    src/saxo_bank_mcp/analytics_source_process.py \
    tests/fixtures/analytics/source_matrix_stdio_child.py \
    tests/test_analytics_source_process_boundary.py
  uv run basedpyright \
    src/saxo_bank_mcp/analytics_source_process.py \
    tests/test_analytics_source_process_boundary.py
  ```

  Expected: both tests pass with one real child PID, real FIFO identities, one initialize/list, a zero exit, and zero reconnect/restart counts.

- [ ] **Step 10: Commit the direct process session**

  ```bash
  git add src/saxo_bank_mcp/analytics_source_process.py \
    tests/test_analytics_source_process_boundary.py \
    tests/fixtures/analytics/source_matrix_stdio_child.py
  git commit -m "feat: add one-shot analytics MCP process"
  ```

### Task 3: Replace heap projection with a sealed static runtime

**Files:**

- Create: `src/saxo_bank_mcp/analytics_source_runtime.py`
- Modify: `src/saxo_bank_mcp/qa_analytics_source_matrix.py:1-70, 294-385, 2448-4857`
- Modify: `src/saxo_bank_mcp/generate_analytics_source_matrix_candidate.py:1-210`
- Modify: `scripts/prepare_analytics_source_matrix_runtime.py:1-148`
- Modify: `scripts/saxo-bank-analytics-source-matrix`
- Modify: `scripts/saxo-bank-analytics-source-matrix-generate`
- Modify: `scripts/run_analytics_source_matrix.py`
- Modify: `scripts/generate_analytics_source_matrix_candidate.py`
- Modify: `tests/test_analytics_source_process_boundary.py`
- Modify: `tests/test_area_b_review_fixes.py:27-69`
- Modify: `tests/test_area_b_review_round2.py:93-208`
- Modify: `tests/test_area_b_review_round3.py:84-439`
- Modify: `tests/test_area_b_review_round4.py:84-534`
- Modify: `tests/test_area_b_review_round5.py:1-380`
- Modify: `tests/test_area_b_review_round6.py:1-151`
- Delete: `tests/test_area_b_review_round7.py`
- Defer regeneration: `data/analytics/source_matrix_candidate.json`

**Interfaces:**

- Consumes: `CHILD_BOOTSTRAP` and `ChildBootstrapPaths` from Task 1; the current catalog, dependency, installed-file, wheel, launcher, guard, and manifest projections from `qa_analytics_source_matrix.py` and `generate_analytics_source_matrix_candidate.py`.
- Produces: `SourceMatrixCandidateIdentity`, `ExternalRunLayout`, `CandidateRuntimeSeal`, `source_matrix_candidate_identity()`, `open_candidate_runtime_seal()`, `revalidate_candidate_runtime(seal)`, `close_candidate_runtime_seal(seal)`, `prepare_child_run_paths(seal, layout)`, and schema-5 manifest helpers.

  Use these exact public signatures:

  ```python
  class SourceMatrixCandidateIdentity(BaseModel):
      model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

      source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
      harness_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
      candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

  @dataclass(frozen=True, slots=True)
  class ExternalRunLayout:
      root: Path = field(repr=False)
      coordinator_cache: Path = field(repr=False)
      coordinator_work: Path = field(repr=False)
      coordinator_tmp: Path = field(repr=False)
      child_cache: Path = field(repr=False)
      child_work: Path = field(repr=False)
      child_tmp: Path = field(repr=False)

  type RuntimeEntryKind = Literal["directory", "file", "symlink"]

  @dataclass(frozen=True, slots=True)
  class RuntimeEntrySnapshot:
      relative_path: str
      entry_kind: RuntimeEntryKind
      device: int = field(repr=False)
      inode: int = field(repr=False)
      uid: int = field(repr=False)
      mode: int
      size: int
      mtime_ns: int = field(repr=False)
      ctime_ns: int = field(repr=False)
      content_sha256: str | None
      link_target: str | None

  @dataclass(frozen=True, slots=True)
  class PortableRuntimeProjection:
      sha256: str
      entry_count: int

  type CandidateRuntimeReason = Literal[
      "runtime_path_invalid",
      "runtime_entry_invalid",
      "runtime_identity_mismatch",
      "runtime_scalar_mismatch",
      "runtime_ancestor_mismatch",
      "external_layout_invalid",
  ]

  @dataclass(frozen=True, slots=True)
  class CandidateRuntimeError(ValueError):
      reason: CandidateRuntimeReason

      def __str__(self) -> str:
          return self.reason

  @dataclass(frozen=True, slots=True)
  class CandidateRuntimeSeal:
      identity: SourceMatrixCandidateIdentity
      portable_identity_sha256: str
      instance_identity_sha256: str
      runtime_root: Path = field(repr=False)
      executable: Path = field(repr=False)
      site_packages: Path = field(repr=False)
      root_descriptor: int = field(repr=False)
      ancestor_descriptors: tuple[int, ...] = field(repr=False)
      entry_snapshot: tuple[RuntimeEntrySnapshot, ...] = field(repr=False)

  def source_matrix_candidate_identity() -> SourceMatrixCandidateIdentity:
      raise NotImplementedError

  def portable_runtime_projection() -> PortableRuntimeProjection:
      raise NotImplementedError

  def open_candidate_runtime_seal() -> tuple[CandidateRuntimeSeal, ExternalRunLayout]:
      raise NotImplementedError

  def revalidate_candidate_runtime(
      seal: CandidateRuntimeSeal,
      layout: ExternalRunLayout,
  ) -> None:
      raise NotImplementedError

  def close_candidate_runtime_seal(seal: CandidateRuntimeSeal) -> None:
      raise NotImplementedError

  def prepare_child_run_paths(
      seal: CandidateRuntimeSeal,
      layout: ExternalRunLayout,
  ) -> ChildBootstrapPaths:
      raise NotImplementedError

  def _open_candidate_runtime_seal_for_test(
      runtime_root: Path,
      manifest_text: str,
      layout: ExternalRunLayout,
  ) -> CandidateRuntimeSeal:
      raise NotImplementedError
  ```

- [ ] **Step 1: Write the three static-boundary RED tests**

  Define `SealedRuntimeFixture` in the test module with `root`, schema-5 `manifest_text`, `ExternalRunLayout`, and these exact mutations: flip one recorded file byte for `content`; replace one file with a directory for `type`; chmod one file `0600` for `mode`; monkeypatch `_expected_owner_uid` for `owner`; replace one file with a symlink for `link`; create `__pycache__` for `cache`; create `unlisted.py` for `extra_file`; replace `bin/python3.12` for `interpreter`; and replace one candidate ancestor with a symlink for `ancestor`. Its `open_seal()` calls only `_open_candidate_runtime_seal_for_test(root, manifest_text, layout)`.

  Define `PreparedRuntimeFixture(root: Path, portable_identity: str)` as a frozen test dataclass. Define `two_prepared_runtimes` by building one wheel offline, invoking `prepare_analytics_source_matrix_runtime.py` twice with separate new paths and the same absolute `uv` and wheel paths, and generating each runtime's manifest into a separate temporary file. Parse `candidate_identity_sha256` into `portable_identity`. This fixture retains both roots until pytest cleanup. Set `QA_MATRIX_PATH = Path("src/saxo_bank_mcp/qa_analytics_source_matrix.py")` and `ROUND7_PATH = Path("tests/test_area_b_review_round7.py")`. Then add the exact test names:

  ```python
  @pytest.mark.parametrize(
      "mutation",
      (
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
      sealed_runtime.apply(mutation, monkeypatch)
      spawn = Mock()
      def open_then_spawn() -> None:
          sealed_runtime.open_seal()
          spawn()
      with pytest.raises(CandidateRuntimeError):
          open_then_spawn()
      spawn.assert_not_called()

  def test_runtime_is_owner_only_nonwritable_and_uses_external_empty_caches(
      two_prepared_runtimes: tuple[PreparedRuntimeFixture, PreparedRuntimeFixture],
  ) -> None:
      first, second = two_prepared_runtimes
      assert first.portable_identity == second.portable_identity
      for prepared in (first, second):
          runtime = prepared.root
          for entry in (runtime, *runtime.rglob("*")):
              metadata = entry.lstat()
              if entry.is_symlink():
                  assert entry.relative_to(runtime).as_posix() in {
                      "bin/python",
                      "bin/python3",
                  }
              elif entry.is_dir():
                  assert stat.S_IMODE(metadata.st_mode) == 0o500
              elif stat.S_IMODE(metadata.st_mode) & 0o111:
                  assert stat.S_IMODE(metadata.st_mode) == 0o500
              else:
                  assert stat.S_IMODE(metadata.st_mode) == 0o400
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
  ```

  For the owner case, make the test-only `_expected_owner_uid()` return a different integer; do not require privileged `chown`.

- [ ] **Step 2: Run the RED static tests**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_prelaunch_static_runtime_change_refuses_before_child \
    tests/test_analytics_source_process_boundary.py::test_runtime_is_owner_only_nonwritable_and_uses_external_empty_caches \
    tests/test_analytics_source_process_boundary.py::test_live_object_projection_functions_are_absent -v
  ```

  Expected: FAIL because the current runtime is owner-writable, uses an internal cache, references an external base interpreter, and still exposes every retired heap projection.

- [ ] **Step 3: Define schema-5 finite candidate records in the runtime module**

  Move finite Pydantic candidate models out of `qa_analytics_source_matrix.py`. Keep `SourceMatrixCandidateIdentity` re-exported from that module for import compatibility. Define the manifest changes exactly:

  ```python
  class _SourceMatrixCandidateManifest(BaseModel):
      model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

      schema_version: Literal["5"]
      source_contract_catalog_sha256: str
      source_files: Mapping[str, str]
      installed_files: Mapping[str, str]
      dependency_distributions: Mapping[str, _DependencyDistribution]
      runtime_identity: _RuntimeIdentity
      installed_metadata_projection: Mapping[str, str]
      console_scripts: Mapping[str, str]
      source_wheel_projection_sha256: str
      portable_runtime_tree_sha256: str
      portable_runtime_entry_count: int = Field(ge=1)
      child_bootstrap_sha256: str
      source_exclusions: tuple[str, ...]
      installed_exclusions: tuple[str, ...]
      source_build_sha256: str
      installed_build_sha256: str
      harness_build_sha256: str
      candidate_identity_sha256: str
  ```

  Apply the existing SHA-256 pattern to every digest field. Include `portable_runtime_tree_sha256`, `portable_runtime_entry_count`, and `child_bootstrap_sha256` in the harness digest. Include the complete schema-5 harness digest and catalog digest in `candidate_identity_sha256`.

  The checked manifest is expected to be stale after the first source edit and until Task 6. Do not temporarily accept schema 4, skip a digest, or run an official candidate during Tasks 3 through 5.

- [ ] **Step 4: Implement the portable descriptor-relative runtime projection**

  Open `/` and each canonical ancestor through the candidate root using `O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`. For every directory descriptor:

  - enumerate names with `os.listdir(descriptor)` and sort by encoded name;
  - reject empty, dot, slash-containing, or non-round-tripping names;
  - call `os.stat(name, dir_fd=descriptor, follow_symlinks=False)`;
  - require current uid, directory `0500`, regular file `0400` or `0500`, and no group or other bits;
  - open regular files with `O_RDONLY | O_NOFOLLOW | O_CLOEXEC`, compare pre-read and post-read `fstat`, and hash bytes;
  - reject sockets, devices, FIFOs, hard-linked regular files, caches, bytecode, unlisted entries, and every symlink except `bin/python` and `bin/python3` resolving relative to `python3.12`;
  - require `bin/python3.12` to be a regular `0500` file and the exact `sys.executable` in official mode.

  Build each portable entry as one of these canonical mappings:

  ```python
  {"mode": "0500", "path": relative, "type": "directory"}
  {"mode": mode, "path": relative, "sha256": content_sha256, "type": "file"}
  {"mode": "0777", "path": relative, "target": "python3.12", "type": "symlink"}
  ```

  Normalize the shebang of installed console entry points to `bin/python3.12` and normalize `home`, `executable`, and `command` values in `pyvenv.cfg` to candidate-relative paths before hashing. Exclude only the manifest resource itself and the one installed `RECORD` file from content hashing; still require their exact names, types, owners, and final modes.

- [ ] **Step 5: Implement the per-run seal and finite scalar projection**

  `RuntimeEntrySnapshot` must contain relative path, type, device, inode, uid, mode, size, `mtime_ns`, `ctime_ns`, content hash for files, and relative target for allowed symlinks. Hash the ordered snapshots plus root and ancestor metadata into `instance_identity_sha256`, but never serialize those snapshots.

  Validate this exact scalar material before opening the seal:

  ```python
  scalar_material = {
      "cache_prefix": normalized_external_path(sys.pycache_prefix),
      "dont_write_bytecode": sys.flags.dont_write_bytecode,
      "executable": normalized_runtime_path(sys.executable),
      "implementation": sys.implementation.name,
      "int_max_str_digits": sys.get_int_max_str_digits(),
      "isolated": sys.flags.isolated,
      "no_site": sys.flags.no_site,
      "prefix": normalized_runtime_path(sys.prefix),
      "base_prefix": normalized_runtime_path(sys.base_prefix),
      "safe_path": sys.flags.safe_path,
      "sys_path": tuple(normalized_runtime_path(item) for item in sys.path),
      "version": platform.python_version(),
  }
  ```

  Require CPython 3.12, `int_max_str_digits == 4300`, `isolated == 1`, `dont_write_bytecode == 1`, `no_site == 1`, `ignore_environment == 1`, `safe_path is True`, no candidate-external Python path, and the manifest's recorded scalar digest. Do not inspect a module, callable, class, descriptor, closure, cache object, or importer.

- [ ] **Step 6: Derive and validate the external run layout**

  The installed launcher bootstrap creates one sibling directory named `.saxo-source-matrix-run.<pid>` with mode `0700`, then exact children `coordinator-cache`, `coordinator-work`, and `coordinator-tmp`, each `0700`. The coordinator derives the root only from `sys.pycache_prefix`, `Path.cwd()`, and `TMPDIR`, requires those three paths share the one root, and creates exact empty `child-cache`, `child-work`, and `child-tmp` children with `0700`.

  `open_candidate_runtime_seal()` must require all six mutable directories outside the runtime and empty before child spawn. `revalidate_candidate_runtime()` must repeat the descriptor-relative portable and instance projections after the child is reaped and require all six directories empty. It must compare root and ancestor device/inode/uid/mode metadata through the held descriptors and by reopening the canonical path.

  `close_candidate_runtime_seal()` closes descriptors only after revalidation and publication completes. It does not delete a nonempty directory or follow a changed path.

- [ ] **Step 7: Copy the complete Python base into each prepared candidate**

  Replace `uv venv --python 3.12` with this build sequence in `prepare_analytics_source_matrix_runtime.py`:

  1. Require a new absolute runtime target and an absolute regular `--uv` executable.
  2. Copy `sys.base_prefix` to `runtime/_python` with symlinks dereferenced; reject sockets, devices, FIFOs, caches, bytecode, and paths escaping the copy root.
  3. Run `runtime/_python/bin/python3.12 -I -B -S -m venv --copies --without-pip runtime`.
  4. Require the created venv interpreter and its `_base_executable`, stdlib, platform stdlib, extension roots, shared library, and build configuration to resolve within `runtime`.
  5. Copy every locked dependency file after checking version and content against the bootstrap manifest.
  6. Install the wheel offline with the absolute `uv` executable and `--no-deps` into `runtime/bin/python3.12`.
  7. Delete every `__pycache__`, `.pyc`, and `.pyo` created during installation.
  8. Make `bin/python3.12` regular, replace `bin/python` and `bin/python3` with relative aliases to it, and reject every other symlink.
  9. Record which regular files need execute permission, then chmod all directories `0500`, those files `0500`, and all remaining regular files `0400`.
  10. Run the same no-follow portable walk used by the coordinator and print only the absolute launcher path.

  Preparation is allowed to write before Step 9. No final candidate entry may retain an owner write bit.

- [ ] **Step 8: Replace both installed launchers with an external-run bootstrap**

  Each shell launcher must resolve its own directory without `PATH`, derive `runtime_root` and exact `bin/python3.12`, set `run_root` to the candidate sibling `.saxo-source-matrix-run.$$`, and execute:

  ```sh
  exec "$python" -I -B -S -X "pycache_prefix=$run_root/coordinator-cache" -c "$bootstrap" \
    "$runtime_root" "$site_packages" "$run_root" "$module" "$@"
  ```

  The sealed Python bootstrap, stored literally in each recorded launcher, must create the root and three coordinator directories with `os.mkdir(..., 0o700)`, reject preexistence, set `TMPDIR`, change to `coordinator-work`, insert only installed site-packages, and run the fixed coordinator or generator module. Its `finally` block must require the three directories empty, remove only those exact empty names and root, and turn any residue into a nonzero exit without recursive deletion.

  Update both development wrappers to use `tempfile.mkdtemp` outside `sys.prefix`, explicit `-I -B -S`, an external cache, explicit repository `src` and resolved site-packages, and a `finally` cleanup that removes only known empty directories. They retain `SAXO_BANK_MCP_OFFICIAL_ISOLATED_LAUNCHER=dev` and can never satisfy official runtime validation.

- [ ] **Step 9: Remove the arbitrary heap projection path**

  Delete the exact named functions from the approved design and every helper used only by them. This includes the `_stable_import_state` block, execution reference and slot projection, regex-cache projection, function-global dependency scan, module/class/metaclass projection, loaded-module and loaded-origin scans, import-hook normalization, live import execution closure, stabilization limit, and their dedicated imports.

  Retain static file, wheel, distribution, launcher, dependency, and runtime projections by moving them into `analytics_source_runtime.py` and rewriting path walks to use the descriptor functions from Steps 4 and 5. Replace `_validate_official_interpreter_state()` with `open_candidate_runtime_seal()` plus the scalar proof. Remove `_ExecutionClosureChangedError` and `execution_closure_checkpoints` from the receipt model; Task 5 adds the process receipt in its place.

- [ ] **Step 10: Migrate review tests that encoded the abandoned heap design**

  Apply these exact changes:

  - delete `test_interpreter_projection_seals_all_import_state_and_loaded_origins` from round 5;
  - rewrite the round-5 installed launcher test to prepare two candidate roots and assert byte-identical portable identities, exact modes, internal copied Python, external empty caches, and no unsupported entry;
  - delete `test_execution_identity_binds_complete_cpython_state_and_loaded_behavior` from round 6;
  - leave `test_postclaim_closure_change_wins_over_tool_exception_and_publication` for Task 5, where it becomes a post-exit static precedence test;
  - delete `tests/test_area_b_review_round7.py` completely;
  - update schema literals and fixture manifests in rounds 2 through 4 to `"5"` and add the three new manifest fields;
  - replace assertions about an internal `.saxo-bank-mcp-pycache` with assertions for six external empty run directories;
  - retain installed package closure, dependency, source wheel, entry point, mutation, cache refusal, guard, strict readiness, source receipt, retry, and schema-drift assertions.

- [ ] **Step 11: Run the static GREEN slice and negative source scan**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_prelaunch_static_runtime_change_refuses_before_child \
    tests/test_analytics_source_process_boundary.py::test_runtime_is_owner_only_nonwritable_and_uses_external_empty_caches \
    tests/test_analytics_source_process_boundary.py::test_live_object_projection_functions_are_absent \
    tests/test_area_b_review_round2.py::test_candidate_identity_rejects_mutation_anywhere_in_installed_executable \
    tests/test_area_b_review_round3.py::test_installed_identity_seals_runtime_dependencies_and_source_wheel_projection \
    tests/test_area_b_review_round4.py::test_candidate_identity_refuses_unsealed_python_cache_and_search_symlink -v
  if rg -n \
    'FastMCPTransport|Client\(server\)|_stable_import_state|_module_execution_projection|_loaded_module_projection|_interpreter_execution_projection|_execution_root_projection_sha256|_stabilized_execution_root_projection_sha256' \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py tests/test_area_b_review_round5.py tests/test_area_b_review_round6.py; then
    exit 1
  fi
  test ! -e tests/test_area_b_review_round7.py
  uv run ruff check \
    src/saxo_bank_mcp/analytics_source_runtime.py \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/generate_analytics_source_matrix_candidate.py \
    scripts/prepare_analytics_source_matrix_runtime.py
  uv run basedpyright \
    src/saxo_bank_mcp/analytics_source_runtime.py \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py
  ```

  Expected: all selected tests pass; the negative search returns no match and round 7 is absent.

- [ ] **Step 12: Commit the static runtime boundary**

  ```bash
  git add src/saxo_bank_mcp/analytics_source_runtime.py \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/generate_analytics_source_matrix_candidate.py \
    scripts/prepare_analytics_source_matrix_runtime.py \
    scripts/saxo-bank-analytics-source-matrix \
    scripts/saxo-bank-analytics-source-matrix-generate \
    scripts/run_analytics_source_matrix.py \
    scripts/generate_analytics_source_matrix_candidate.py \
    tests/test_analytics_source_process_boundary.py \
    tests/test_area_b_review_fixes.py \
    tests/test_area_b_review_round2.py \
    tests/test_area_b_review_round3.py \
    tests/test_area_b_review_round4.py \
    tests/test_area_b_review_round5.py \
    tests/test_area_b_review_round6.py \
    tests/test_area_b_review_round7.py
  git commit -m "fix: seal analytics source runtime statically"
  ```

### Task 4: Run the matrix protocol through the narrow session

**Files:**

- Modify: `src/saxo_bank_mcp/qa_analytics_source_matrix.py:281-2427`
- Modify: `src/saxo_bank_mcp/analytics_source_process.py`
- Create: `tests/analytics_source_matrix_support.py`
- Modify: `tests/test_qa_analytics_source_matrix.py:1-769`
- Modify: `tests/test_analytics_source_process_boundary.py`
- Modify: `tests/test_area_b_review_round2.py:209-520`
- Modify: `tests/test_area_b_review_round3.py:440-810`
- Modify: `tests/test_area_b_review_round4.py:535-760`

**Interfaces:**

- Consumes: `MatrixSession`, `MatrixCallPolicy`, `RegisteredCallProfile`, `SOURCE_MATRIX_CHILD_TOOLS`, and `CHILD_TOOL_IDS_SHA256` from Task 1; the existing strict receipt models and source reduction helpers; `SourceMatrixCandidateIdentity` from Task 3.
- Produces: `PreclaimReason`, `PreclaimRefusal`, `PreparedMatrix`, `ClaimedMatrixDraft`, `MatrixProtocolOutcome`, `prepare_analytics_source_matrix(...)`, and the new session-based `run_analytics_source_matrix(...)`.

  Define the outcome boundary exactly:

  ```python
  type PreclaimReason = Literal[
      "environment_not_sim",
      "live_reads_enabled",
      "live_writes_enabled",
      "source_contract_count_mismatch",
      "source_plan_invalid",
      "candidate_static_runtime_invalid",
      "child_configuration_refused",
      "child_process_unavailable",
      "child_tool_allowlist_mismatch",
      "sim_auth_unavailable",
      "registered_operation_mismatch",
      "sim_session_unavailable",
      "sim_entitlements_unavailable",
      "request_ledger_unavailable",
      "candidate_already_claimed",
  ]

  class PreclaimRefusal(BaseModel):
      model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

      status: Literal["refused"] = "refused"
      reason: PreclaimReason
      source_execution_claimed: Literal[False] = False

  class ClaimedMatrixDraft(BaseModel):
      model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

      status: MatrixStatus
      reason: str
      environment: Literal["SIM"] = "SIM"
      source_contract_catalog_sha256: str
      harness_build_sha256: str
      candidate_identity_sha256: str
      captured_at: datetime
      source_plan_sha256: str
      source_plan_exclusions: tuple[SourcePlanExclusion, ...]
      environment_proof: EnvironmentProof
      auth_status: str
      session_status: str
      entitlement_status: str
      source_receipts: tuple[SourceContractReceipt, ...]
      history_state: HistoryState
      source_execution_claimed: Literal[True] = True
      cleanup: CleanupReceipt
      ledger: LedgerReceipt
      live_events: int = Field(ge=0)
      live_mutation_calls: Literal[0] = 0
      errors: tuple[str, ...]

  type MatrixProtocolOutcome = PreclaimRefusal | ClaimedMatrixDraft

  @dataclass(frozen=True, slots=True)
  class MatrixExecutionError(RuntimeError):
      reason: Literal["structured_result_invalid", "matrix_receipt_invalid"]

      def __str__(self) -> str:
          return self.reason

  @dataclass(frozen=True, slots=True)
  class PreparedMatrix:
      candidate_identity: SourceMatrixCandidateIdentity
      captured_at: datetime
      environment_proof: EnvironmentProof
      contracts: tuple[SourceContract, ...]
      requests: Mapping[str, Mapping[str, object] | None] = field(repr=False)
      source_plan: _SourceExecutionPlan
      call_policy: MatrixCallPolicy = field(repr=False)

  def prepare_analytics_source_matrix(
      *,
      env: Mapping[str, str],
      fixtures: SourceMatrixFixtures,
      candidate_identity: SourceMatrixCandidateIdentity,
      captured_at: datetime | None = None,
  ) -> PreclaimRefusal | PreparedMatrix:
      raise NotImplementedError

  async def run_analytics_source_matrix(
      session: MatrixSession,
      *,
      prepared: PreparedMatrix,
      claim_source_execution: Callable[[], bool],
  ) -> MatrixProtocolOutcome:
      raise NotImplementedError
  ```

- [ ] **Step 1: Create the narrow scripted matrix support**

  Move deterministic payload builders from `tests/test_qa_analytics_source_matrix.py` into `tests/analytics_source_matrix_support.py` and define this session without importing FastMCP:

  ```python
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
  ```

  Add `canonical_digest(value: object) -> str` using independent compact sorted `json.dumps(..., allow_nan=False)` plus SHA-256. Add `matrix_payloads(prepared: PreparedMatrix) -> dict[str, deque[dict[str, JsonValue]]]` by moving the current strict auth, registry, capability, entitlement, source, state-fingerprint, and ledger dictionaries without changing a field. Add `expected_matrix_events(prepared: PreparedMatrix) -> list[tuple[str, dict[str, JsonValue]]]` that independently derives sorted registry pages from `endpoint_registry`, then appends readiness, claim, the three state profiles, included source profiles in `_SOURCE_CONTRACT_ORDER`, the same three state profiles, and ledger readback. Keep private values only in fixture input fields and expected registered-call arguments.

  Define these fixtures in `tests/test_analytics_source_process_boundary.py`:

  ```python
  @pytest.fixture
  def prepared_matrix(exact_sim_env: dict[str, str]) -> PreparedMatrix:
      identity = SourceMatrixCandidateIdentity(
          source_contract_catalog_sha256=source_contract_catalog_sha256(),
          harness_build_sha256="a" * 64,
          candidate_identity_sha256="b" * 64,
      )
      result = prepare_analytics_source_matrix(
          env=exact_sim_env,
          fixtures=SourceMatrixFixtures(),
          candidate_identity=identity,
          captured_at=datetime(2026, 7, 31, tzinfo=UTC),
      )
      assert isinstance(result, PreparedMatrix)
      return result

  @pytest.fixture
  def scripted_matrix_session(prepared_matrix: PreparedMatrix) -> ScriptedMatrixSession:
      return ScriptedMatrixSession(payloads=matrix_payloads(prepared_matrix))
  ```

- [ ] **Step 2: Write the three protocol RED tests**

  Add these exact names:

  ```python
  def test_official_matrix_has_no_fastmcp_transport_or_server_injection() -> None:
      tree = ast.parse(QA_MATRIX_PATH.read_text(encoding="utf-8"))
      imported = {
          alias.name
          for node in ast.walk(tree)
          if isinstance(node, ast.Import)
          for alias in node.names
      } | {
          node.module
          for node in ast.walk(tree)
          if isinstance(node, ast.ImportFrom) and node.module is not None
      }
      source = QA_MATRIX_PATH.read_text(encoding="utf-8")
      signature = inspect.signature(execute_analytics_source_matrix_once)
      assert not any(name.startswith("fastmcp") for name in imported)
      assert "saxo_bank_mcp.server" not in imported
      assert "Client(server)" not in source
      assert tuple(signature.parameters) == ("fixtures",)

  @pytest.mark.parametrize(
      "tool_names",
      (
          SOURCE_MATRIX_CHILD_TOOLS[:-1],
          (*SOURCE_MATRIX_CHILD_TOOLS, "saxo_auth_status"),
          (*SOURCE_MATRIX_CHILD_TOOLS, "saxo_get_orders"),
          tuple(f"server:{name}" for name in SOURCE_MATRIX_CHILD_TOOLS),
      ),
  )
  async def test_child_lists_exact_six_tools_before_readiness(
      tool_names: tuple[str, ...],
      scripted_matrix_session: ScriptedMatrixSession,
      prepared_matrix: PreparedMatrix,
  ) -> None:
      scripted_matrix_session.tool_names = tool_names
      outcome = await run_analytics_source_matrix(
          scripted_matrix_session,
          prepared=prepared_matrix,
          claim_source_execution=lambda: True,
      )
      assert outcome == PreclaimRefusal(reason="child_tool_allowlist_mismatch")
      assert scripted_matrix_session.events == [("tools/list", {})]

  async def test_readiness_clear_claim_state_source_ledger_order_is_exact(
      scripted_matrix_session: ScriptedMatrixSession,
      prepared_matrix: PreparedMatrix,
  ) -> None:
      def claim() -> bool:
          scripted_matrix_session.events.append(("claim", {}))
          return True

      outcome = await run_analytics_source_matrix(
          scripted_matrix_session,
          prepared=prepared_matrix,
          claim_source_execution=claim,
      )
      assert isinstance(outcome, ClaimedMatrixDraft)
      assert scripted_matrix_session.events == expected_matrix_events(prepared_matrix)

  ```

- [ ] **Step 3: Run the protocol RED slice that has an owning implementation in this task**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_official_matrix_has_no_fastmcp_transport_or_server_injection \
    tests/test_analytics_source_process_boundary.py::test_child_lists_exact_six_tools_before_readiness \
    tests/test_analytics_source_process_boundary.py::test_readiness_clear_claim_state_source_ledger_order_is_exact -v
  ```

  Expected: FAIL because the coordinator imports FastMCP and accepts a live server.

- [ ] **Step 4: Remove the in-process client and change every matrix helper to `MatrixSession`**

  Delete `MatrixClient`, `_call_tool_with_checkpoint`, `validate_source_execution`, every closure checkpoint, `async with Client(server)`, and `server=mcp`. Change `_call_tool`, `_registered_operation_receipts`, `_run_provider_source`, and `_state_fingerprint` to accept `MatrixSession` plus `MatrixCallPolicy`.

  Use one call wrapper:

  ```python
  async def _call_tool(
      session: MatrixSession,
      policy: MatrixCallPolicy,
      name: str,
      arguments: dict[str, JsonValue],
  ) -> dict[str, JsonValue]:
      policy.validate(name, arguments)
      payload = await session.call_tool(name, arguments)
      policy.observe(name, arguments, payload)
      return _JSON_OBJECT.validate_python(payload)
  ```

  Let `ProcessSessionError` escape for Task 5 to classify. Convert Pydantic structured-result failures to `MatrixExecutionError("structured_result_invalid")`, another value-free internal error defined in this module.

- [ ] **Step 5: Build the exact local preparation and registered-call profile before readiness**

  `prepare_analytics_source_matrix()` must prove the exact SIM and disabled LIVE gates, timezone-aware capture time, 18-item catalog with keys equal to `_SOURCE_CONTRACT_ORDER`, source plan, exclusions, resolved paths, and string query parameters before any process exists. Return the first exact `PreclaimRefusal` on failure. Add three state profiles in this exact order:

  ```python
  (
      RegisteredCallProfile(
          path="/port/v1/orders/me",
          params={},
          response_mode="fingerprint_only",
          analytics_contract_id=None,
      ),
      RegisteredCallProfile(
          path="/port/v1/positions/me",
          params={},
          response_mode="fingerprint_only",
          analytics_contract_id=None,
      ),
      RegisteredCallProfile(
          path="/port/v1/balances/me",
          params={},
          response_mode="fingerprint_only",
          analytics_contract_id=None,
      ),
  )
  ```

  For every included source, create one profile with the resolved registered path, exact rendered params, `analytics_contract_receipt`, and matching contract ID. Excluded source contracts receive no profile and no MCP call. Require catalog keys to equal `_SOURCE_CONTRACT_ORDER` before creating the plan.

- [ ] **Step 6: Enforce list, readiness, registry, and claim order**

  The first session action must be `list_tools_once()`. Reject if length is not six, names are not unique, any name is qualified, or the set differs. Then call exactly:

  1. `saxo_auth_status` with `{}`;
  2. `saxo_list_registered_endpoints` by lexically sorted sealed service group, `limit=100`, starting at offset zero and following only a strictly increasing `next_offset`;
  3. `saxo_get_session_capabilities` with `{}`;
  4. `saxo_get_entitlements` with `{}`;
  5. `saxo_get_safe_request_ledger` with `{"clear": True}`;
  6. `claim_source_execution()` once.

  Registry readiness must cover every operation behind all 18 contracts and all three state paths, each with exact operation ID, `GET`, path template, `read`, and `read_only_definition_registered`. Map failures to the exact preclaim reason; make no later call after a failure.

- [ ] **Step 7: Enforce claimed state, source, and ledger order**

  After claim, read orders, positions, and balances. Require `response_visibility=fingerprint_only`; require `raw_response_body` scope for orders and positions and `account_money_state_fields` scope for balances. Iterate `_SOURCE_CONTRACT_ORDER`, making exactly one registered call for each included source and constructing the existing value-free unavailable receipt for each sealed exclusion. Read the same three state paths again with the same scopes, then read the ledger with `{}`.

  Preserve existing status and reason semantics for `state_fingerprint_unverified`, `state_fingerprint_mismatch`, `unsafe_request_ledger`, `live_events_detected`, source refusal, and source reduction. Source pagination, continuation, and bounded provider retry remain inside that source's single server tool call and remain visible in its strict receipt; the coordinator never retries a failed MCP request. Return `ClaimedMatrixDraft`; do not perform privacy scan, process receipt construction, child close, or publication inside the matrix function.

- [ ] **Step 8: Migrate pure matrix and review tests to the narrow session**

  Replace the 500-line FastMCP fake in `tests/test_qa_analytics_source_matrix.py` with imports from `analytics_source_matrix_support`. Delete `test_execution_closure_is_revalidated_after_claim_and_around_every_mcp_call`; the exact process-order test replaces it. Update assertions to distinguish `PreclaimRefusal` from `ClaimedMatrixDraft`.

  In rounds 2 through 4, replace fake `.structured_content` result objects and `Client[FastMCPTransport]` annotations with direct dictionaries returned by `ScriptedMatrixSession`. Preserve strict source, state, ledger, retry-role, native-revision, schema, and HTTP semantics.

- [ ] **Step 9: Run the protocol GREEN slice and matrix regressions**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_official_matrix_has_no_fastmcp_transport_or_server_injection \
    tests/test_analytics_source_process_boundary.py::test_child_lists_exact_six_tools_before_readiness \
    tests/test_analytics_source_process_boundary.py::test_readiness_clear_claim_state_source_ledger_order_is_exact \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_area_b_review_round2.py \
    tests/test_area_b_review_round3.py \
    tests/test_area_b_review_round4.py -v
  uv run ruff check \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/analytics_source_process.py \
    tests/analytics_source_matrix_support.py \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_analytics_source_process_boundary.py
  uv run basedpyright \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/analytics_source_process.py \
    tests/analytics_source_matrix_support.py \
    tests/test_qa_analytics_source_matrix.py
  ```

  Expected: the selected tests pass; every matrix event is in the exact order and no official source imports or accepts a live server.

- [ ] **Step 10: Commit the narrow matrix protocol**

  ```bash
  git add src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/analytics_source_process.py \
    tests/analytics_source_matrix_support.py \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_analytics_source_process_boundary.py \
    tests/test_area_b_review_round2.py \
    tests/test_area_b_review_round3.py \
    tests/test_area_b_review_round4.py
  git commit -m "refactor: run source matrix through narrow session"
  ```

### Task 5: Finalize process-bound evidence after exit and static revalidation

**Files:**

- Modify: `src/saxo_bank_mcp/qa_analytics_source_matrix.py:294-770, 1049-1258, 4862-5358`
- Modify: `src/saxo_bank_mcp/analytics_source_process.py`
- Modify: `src/saxo_bank_mcp/analytics_source_runtime.py`
- Modify: `tests/analytics_source_matrix_support.py`
- Modify: `tests/test_analytics_source_process_boundary.py`
- Modify: `tests/test_qa_analytics_source_matrix.py:770-910`
- Modify: `tests/test_area_b_review_round6.py:152-340`

**Interfaces:**

- Consumes: `PreparedMatrix`, `PreclaimRefusal`, `ClaimedMatrixDraft`, and `run_analytics_source_matrix` from Task 4; `OneShotProcessSession` and `ProcessSessionFacts` from Task 2; `CandidateRuntimeSeal` and `ExternalRunLayout` from Task 3; existing `_ClaimedGuardDirectory`, `_claim_candidate_guard_directory`, `_publish_claimed_text`, and secret scanning.
- Produces: `ProcessBoundaryReceipt`, the final `AnalyticsSourceMatrixReceipt`, exact postclaim failure publication, and the official `execute_analytics_source_matrix_once(*, fixtures=None) -> int` with no process, server, path, identity, or callback injection.

  Define the final model exactly:

  ```python
  class ProcessBoundaryReceipt(BaseModel):
      model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

      transport: Literal["stdio"] = "stdio"
      child_process_distinct: Literal[True] = True
      process_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
      stdio_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
      child_spawn_count: Literal[1] = 1
      mcp_session_count: Literal[1] = 1
      mcp_initialize_count: Literal[1] = 1
      tool_list_count: Literal[1] = 1
      tool_count: Literal[6] = 6
      tool_ids_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
      reconnect_count: Literal[0] = 0
      restart_count: Literal[0] = 0
      child_exit_code: Literal[0] = 0
      stdout_protocol_only: Literal[True] = True
      stderr_published: Literal[False] = False

  type PostclaimFailureReason = Literal[
      "candidate_static_runtime_changed",
      "child_process_failed",
      "matrix_execution_failed",
      "evidence_secret_scan_failed",
  ]

  class AnalyticsSourceMatrixReceipt(ClaimedMatrixDraft):
      process_boundary: ProcessBoundaryReceipt
      privacy: PrivacyReceipt
  ```

  `AnalyticsSourceMatrixReceipt` is only constructible after zero child exit and successful post-exit static revalidation. Minimal failures remain exactly `{"status":"failed","reason":<PostclaimFailureReason>}`.

  Define the test-only wrapper in `tests/analytics_source_matrix_support.py`:

  ```python
  @dataclass(frozen=True, slots=True)
  class BoundaryTestResult:
      command_exit_code: int
      child_exit_code: int | None
      claimed: bool
      spawn_count: int
      restart_count: int
      requests_replayed: int
      publication_count: int
      guard_path: Path
      evidence_path: Path
      published_text: str
      redirected_evidence: Path
      original_evidence: Path

  @dataclass(slots=True)
  class BoundaryEventFixture:
      root: Path
      forbidden_scalars: frozenset[str | int]

      async def run(
          self,
          *,
          child_scenario: str,
          postexit_runtime_mutation: str | None = None,
      ) -> BoundaryTestResult:
          raise NotImplementedError

      def swap_parent_then_publish(self) -> BoundaryTestResult:
          raise NotImplementedError

  def recursive_scalar_values(value: JsonValue) -> frozenset[str | int | float | bool | None]:
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
  ```

  Its pytest fixture constructor creates one synthetic schema-5 runtime, one temporary `0700` state root, one `ScriptedProcessSession` implementing the same spawn/initialize/list/call/close/abort interface, and fixed private sentinel strings and large integer identities. `run()` calls only `_execute_source_matrix_with_events`; `swap_parent_then_publish()` calls the retained descriptor publication helper after renaming the claimed parent. The wrapper records counts from scripted events and reads only its temporary evidence path.

- [ ] **Step 1: Write the seven orchestration and evidence RED tests**

  Add these exact tests:

  ```python
  @pytest.mark.parametrize("process_failure", ("spawn", "initialize", "early_exit"))
  async def test_child_start_initialize_or_early_exit_never_claims_or_restarts(
      boundary_fixture: BoundaryEventFixture,
      process_failure: Literal["spawn", "initialize", "early_exit"],
  ) -> None:
      result = await boundary_fixture.run(child_scenario=process_failure)
      assert result.command_exit_code != 0
      assert result.claimed is False
      assert result.spawn_count <= 1
      assert result.restart_count == 0
      assert not result.guard_path.exists()
      assert not result.evidence_path.exists()

  async def test_child_protocol_failure_after_claim_publishes_one_minimal_failure(
      boundary_fixture: BoundaryEventFixture,
  ) -> None:
      result = await boundary_fixture.run(child_scenario="malformed_after_claim")
      assert result.requests_replayed == 0
      assert json.loads(result.published_text) == {
          "status": "failed",
          "reason": "child_process_failed",
      }
      assert result.publication_count == 1

  async def test_child_nonzero_exit_after_matrix_is_not_success(
      boundary_fixture: BoundaryEventFixture,
  ) -> None:
      result = await boundary_fixture.run(child_scenario="nonzero_after_matrix")
      assert result.child_exit_code != 0
      assert json.loads(result.published_text) == {
          "status": "failed",
          "reason": "child_process_failed",
      }

  async def test_postexit_static_change_overrides_tool_or_child_failure(
      boundary_fixture: BoundaryEventFixture,
  ) -> None:
      result = await boundary_fixture.run(
          child_scenario="malformed_after_claim",
          postexit_runtime_mutation="content",
      )
      assert json.loads(result.published_text) == {
          "status": "failed",
          "reason": "candidate_static_runtime_changed",
      }

  def test_process_boundary_receipt_binds_pid_stdio_tools_and_zero_reconnects() -> None:
      identity = SourceMatrixCandidateIdentity(
          source_contract_catalog_sha256="a" * 64,
          harness_build_sha256="b" * 64,
          candidate_identity_sha256="c" * 64,
      )
      stdin = PipeIdentity("fifo", 101, 201, 101, 201)
      stdout = PipeIdentity("fifo", 102, 202, 102, 202)
      facts = ProcessSessionFacts(
          coordinator_pid=30_001,
          child_pid=30_002,
          executable_identity_sha256="d" * 64,
          stdin_identity=stdin,
          stdout_identity=stdout,
          stderr_byte_count=17,
          listed_tool_names=SOURCE_MATRIX_CHILD_TOOLS,
          child_spawn_count=1,
          mcp_session_count=1,
          mcp_initialize_count=1,
          tool_list_count=1,
          reconnect_count=0,
          restart_count=0,
          child_exit_code=0,
          stdout_protocol_only=True,
      )
      receipt = process_boundary_receipt(facts, identity)
      assert receipt.process_identity_sha256 == canonical_digest(
          {
              "candidate_identity_sha256": "c" * 64,
              "child_executable_identity_sha256": "d" * 64,
              "child_pid": 30_002,
              "coordinator_pid": 30_001,
          },
      )
      assert receipt.stdio_identity_sha256 == canonical_digest(
          {
              "stdin": stdin.canonical_private_material(),
              "stdout": stdout.canonical_private_material(),
          },
      )
      assert receipt.tool_ids_sha256 == CHILD_TOOL_IDS_SHA256
      assert receipt.reconnect_count == receipt.restart_count == 0

  @pytest.mark.parametrize("scenario", ("success", "preclaim", "postclaim", "stderr"))
  async def test_process_receipt_and_failure_paths_publish_no_private_runtime_values(
      boundary_fixture: BoundaryEventFixture,
      scenario: str,
  ) -> None:
      result = await boundary_fixture.run(child_scenario=scenario)
      if scenario == "preclaim":
          assert result.published_text == ""
          assert not result.evidence_path.exists()
          return
      document = json.loads(result.published_text)
      scalars = recursive_scalar_values(document)
      assert boundary_fixture.forbidden_scalars.isdisjoint(scalars)
      assert not any(
          key in result.published_text
          for key in ("child_pid", "coordinator_pid", "descriptor", "inode", "stderr")
      )

  def test_descriptor_bound_evidence_refuses_swapped_claimed_ancestor(
      boundary_fixture: BoundaryEventFixture,
  ) -> None:
      result = boundary_fixture.swap_parent_then_publish()
      assert result.command_exit_code != 0
      assert not result.redirected_evidence.exists()
      assert not result.original_evidence.exists()
  ```

  Complete `test_child_start_initialize_or_early_exit_never_claims_or_restarts` from Task 4. Keep the existing round-6 swapped-ancestor test as a second regression against the same descriptor functions.

- [ ] **Step 2: Run the RED orchestration tests**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_child_start_initialize_or_early_exit_never_claims_or_restarts \
    tests/test_analytics_source_process_boundary.py::test_child_protocol_failure_after_claim_publishes_one_minimal_failure \
    tests/test_analytics_source_process_boundary.py::test_child_nonzero_exit_after_matrix_is_not_success \
    tests/test_analytics_source_process_boundary.py::test_postexit_static_change_overrides_tool_or_child_failure \
    tests/test_analytics_source_process_boundary.py::test_process_boundary_receipt_binds_pid_stdio_tools_and_zero_reconnects \
    tests/test_analytics_source_process_boundary.py::test_process_receipt_and_failure_paths_publish_no_private_runtime_values \
    tests/test_analytics_source_process_boundary.py::test_descriptor_bound_evidence_refuses_swapped_claimed_ancestor -v
  ```

  Expected: FAIL because the current official runner has no process receipt, no post-exit boundary, and no process-aware reason precedence.

- [ ] **Step 3: Build the process receipt only from successful raw facts**

  Add:

  ```python
  def process_boundary_receipt(
      facts: ProcessSessionFacts,
      candidate_identity: SourceMatrixCandidateIdentity,
  ) -> ProcessBoundaryReceipt:
      raise NotImplementedError
  ```

  Refuse unless the child PID differs, every literal count is exact, listed names are six unique exact names, exit is zero, and stdout was protocol-only. Compute:

  ```python
  process_identity_sha256 = _digest(
      {
          "candidate_identity_sha256": candidate_identity.candidate_identity_sha256,
          "child_executable_identity_sha256": facts.executable_identity_sha256,
          "child_pid": facts.child_pid,
          "coordinator_pid": facts.coordinator_pid,
      },
  )
  stdio_identity_sha256 = _digest(
      {
          "stdin": facts.stdin_identity.canonical_private_material(),
          "stdout": facts.stdout_identity.canonical_private_material(),
      },
  )
  ```

  Set `tool_ids_sha256` by digesting the actual listed names in lexical order and require it equals `CHILD_TOOL_IDS_SHA256`. Do not include stderr identity or count in evidence.

- [ ] **Step 4: Separate the official function from the internal test seam**

  The public function remains:

  ```python
  def execute_analytics_source_matrix_once(
      *,
      fixtures: SourceMatrixFixtures | None = None,
  ) -> int:
      return anyio.run(_execute_official_source_matrix, fixtures or SourceMatrixFixtures())
  ```

  `_execute_official_source_matrix` obtains `os.environ`, the official runtime seal, fixed state root, current capture time, child launch config, direct `OneShotProcessSession`, guard, and publisher internally. It accepts only `fixtures`.

  Tests may call this private seam below the official function:

  ```python
  async def _execute_source_matrix_with_events(
      *,
      fixtures: SourceMatrixFixtures,
      env: Mapping[str, str],
      seal: CandidateRuntimeSeal,
      layout: ExternalRunLayout,
      session: ProcessMatrixSession,
      state_root: Path,
      captured_at: datetime,
      postexit_revalidate: Callable[[], None],
  ) -> int:
      raise NotImplementedError
  ```

  The seam accepts finite scripted lifecycle events, a narrow process session, a synthetic already-open seal, and a temporary state root. It must not contain a `FastMCP`, transport, process command, process factory, official output override, or publication callback. The public official function supplies its own direct session, state root, seal, clock, and static revalidator and exposes none of these parameters.

- [ ] **Step 5: Implement pre-spawn refusal and one-child orchestration**

  Execute this exact outer sequence:

  1. `open_candidate_runtime_seal()` and derive the empty external layout.
  2. `source_matrix_candidate_identity()` and require it equals the seal identity.
  3. `prepare_analytics_source_matrix(...)`; return nonzero without spawn for a `PreclaimRefusal`.
  4. `prepare_child_run_paths(...)`, `build_child_launch_config(...)`, and require exact filter resolution.
  5. Construct one `OneShotProcessSession`, call `spawn()`, then `initialize()`.
  6. Call `run_analytics_source_matrix(...)` with a closure that claims exactly once through `_claim_candidate_guard_directory` and retains its descriptor.
  7. Call `close()` once, require successful facts, then `revalidate_candidate_runtime(...)`.
  8. If claimed, build process receipt, finalize, scan, and publish. If preclaim, publish nothing.
  9. Close the seal and remove only verified empty child and coordinator run directories.

  Map child configuration errors before spawn to `child_configuration_refused`. Map spawn, initialize, framing, early exit, timeout, nonzero exit, or shutdown failure before claim to `child_process_unavailable`. A tool mismatch retains `child_tool_allowlist_mismatch` only if close and exit succeed.

- [ ] **Step 6: Make teardown and static revalidation unconditional after spawn**

  Track only finite control state:

  ```python
  claimed: _ClaimedGuardDirectory | None = None
  draft: ClaimedMatrixDraft | None = None
  preclaim: PreclaimRefusal | None = None
  process_failure = False
  matrix_failure = False
  static_failure = False
  facts: ProcessSessionFacts | None = None
  ```

  In `finally`, abort and reap the same session if it did not exit, then call `revalidate_candidate_runtime` on every spawned-child path, including readiness refusal and exceptions. Do not construct JSON before that call.

  Preclaim precedence is:

  1. post-exit static mismatch becomes `candidate_static_runtime_invalid`;
  2. any process lifecycle failure becomes `child_process_unavailable`;
  3. otherwise retain the tool-list or readiness refusal.

  No preclaim path writes a guard or evidence. A later explicit command may retry only because no claim exists; this invocation never retries.

- [ ] **Step 7: Apply exact postclaim failure precedence through the held descriptor**

  Once `claimed` is non-`None`, keep the claim directory descriptor, directory metadata, guard descriptor metadata, and guard payload open through teardown, revalidation, scanning, and publication. Select exactly one reason:

  ```python
  if static_failure:
      reason = "candidate_static_runtime_changed"
  elif process_failure:
      reason = "child_process_failed"
  elif matrix_failure:
      reason = "matrix_execution_failed"
  elif privacy_failure:
      reason = "evidence_secret_scan_failed"
  ```

  Serialize a minimal failure with canonical compact JSON and publish once through `_publish_claimed_text`. Do not construct a full receipt when facts are missing, exit is nonzero, stdout failed, or static revalidation failed. Do not replay a request or try an alternate path if publication fails.

- [ ] **Step 8: Finalize and scan the exact successful bytes**

  Construct `AnalyticsSourceMatrixReceipt` from `ClaimedMatrixDraft`, `ProcessBoundaryReceipt`, and `PrivacyReceipt(findings=0, scan_errors=0)`. Serialize once with:

  ```python
  text = json.dumps(
      receipt.model_dump(mode="json"),
      allow_nan=False,
      separators=(",", ":"),
      sort_keys=True,
  )
  ```

  Scan that exact text with `scan_secret_text("source-matrix.json", text)`. Also reject any nonempty fixture, SIM auth, child environment, command/path, stderr, exception, or raw-response string that appears verbatim. Parse the final object and prove raw PID, descriptor, and pipe metadata integers are absent as scalar values and their field names are absent; do not use short numeric substring matching. Store only counts in `PrivacyReceipt`; publish the same already-scanned `text` without reserialization.

- [ ] **Step 9: Retain and harden descriptor-bound immutable publication**

  Keep every state-root and claimed-directory operation relative to the held descriptor with no-follow metadata checks. Before the final hard link, reopen the canonical ancestor chain and require it reaches the held directory and unchanged guard. Require `0700` directories, `0600` guard/temp/final files, current uid, no preexisting final name, matching final inode after link, and `fsync` on file and directory.

  A swapped ancestor, changed owner or mode, replaced guard, existing evidence, or mismatched inode returns nonzero. It cannot redirect publication, reopen by an unverified path, replace evidence, or fall back to another state root.

- [ ] **Step 10: Update official CLI behavior without widening arguments**

  Keep existing source-fixture flags, `--identity`, `--preflight`, and `--help`. `--identity` prints only the portable candidate identity after static verification. `--preflight` validates local environment, candidate, run layout, and child configuration without spawn, claim, or publication. A normal command prints no child output, refusal object, stderr, stack trace, or environment and returns zero only after a full immutable receipt is published.

- [ ] **Step 11: Adapt existing claim, privacy, and round-6 tests**

  In `tests/test_qa_analytics_source_matrix.py`, keep environment, state, ledger, source, privacy, guard, readiness-no-write, and frozen-once assertions. Replace expected closure checkpoint fields with exact `process_boundary` literals and hashes.

  Rename round 6's `test_postclaim_closure_change_wins_over_tool_exception_and_publication` to `test_postexit_static_change_wins_over_tool_exception_and_publication`; make it mutate one sealed file after claim, inject one tool exception, and assert the sole minimal reason is `candidate_static_runtime_changed`. Retain `test_evidence_publication_refuses_a_swapped_claimed_ancestor` unchanged apart from schema-5 identity fixtures.

- [ ] **Step 12: Run the orchestration GREEN slice and descriptor regressions**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_child_start_initialize_or_early_exit_never_claims_or_restarts \
    tests/test_analytics_source_process_boundary.py::test_child_protocol_failure_after_claim_publishes_one_minimal_failure \
    tests/test_analytics_source_process_boundary.py::test_child_nonzero_exit_after_matrix_is_not_success \
    tests/test_analytics_source_process_boundary.py::test_postexit_static_change_overrides_tool_or_child_failure \
    tests/test_analytics_source_process_boundary.py::test_process_boundary_receipt_binds_pid_stdio_tools_and_zero_reconnects \
    tests/test_analytics_source_process_boundary.py::test_process_receipt_and_failure_paths_publish_no_private_runtime_values \
    tests/test_analytics_source_process_boundary.py::test_descriptor_bound_evidence_refuses_swapped_claimed_ancestor \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_area_b_review_round6.py -v
  uv run ruff check \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/analytics_source_process.py \
    src/saxo_bank_mcp/analytics_source_runtime.py \
    tests/test_analytics_source_process_boundary.py \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_area_b_review_round6.py
  uv run basedpyright \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/analytics_source_process.py \
    src/saxo_bank_mcp/analytics_source_runtime.py \
    tests/test_analytics_source_process_boundary.py
  ```

  Expected: every selected test passes; failures publish at most one fixed two-key object through the held descriptor and successful receipts contain all exact process fields without raw process data.

- [ ] **Step 13: Commit the process-bound publication path**

  ```bash
  git add src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/analytics_source_process.py \
    src/saxo_bank_mcp/analytics_source_runtime.py \
    tests/analytics_source_matrix_support.py \
    tests/test_analytics_source_process_boundary.py \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_area_b_review_round6.py
  git commit -m "fix: publish process-bound analytics evidence"
  ```

### Task 6: Freeze the schema-5 candidate and run all offline gates

**Files:**

- Modify: `src/saxo_bank_mcp/generate_analytics_source_matrix_candidate.py`
- Modify: `data/analytics/source_matrix_candidate.json`
- Modify: `docs/analytics-bi-vision.md:1779-1830, 2290-2304`
- Modify: `tests/analytics_source_matrix_support.py`
- Modify: `tests/test_analytics_source_process_boundary.py`
- Create: `tests/fixtures/analytics/source_matrix_fixture_server.py`
- Verify: `docs/superpowers/specs/2026-07-31-saxo-analytics-source-matrix-process-boundary-design.md`
- Verify: `tests/test_qa_analytics_source_matrix.py`
- Verify: `tests/test_area_b_review_fixes.py`
- Verify: `tests/test_area_b_review_round2.py`
- Verify: `tests/test_area_b_review_round3.py`
- Verify: `tests/test_area_b_review_round4.py`
- Verify: `tests/test_area_b_review_round5.py`
- Verify: `tests/test_area_b_review_round6.py`
- Verify absent: `tests/test_area_b_review_round7.py`

**Interfaces:**

- Consumes: the complete source, process, runtime, coordinator, test, and packaging work from Tasks 1 through 5.
- Produces: one checked schema-5 candidate manifest, two byte-identical portable runtime proofs from separate final builds, an installed-launcher deterministic boundary proof, updated operator documentation, and a clean offline validation record. It does not produce real SIM evidence.

- [ ] **Step 1: Add the sealed deterministic fixture server for the artifact test**

  Implement a stdlib-only line-framed MCP server with no socket or Saxo import. On startup it must call `resolve_eval_tool_filter(os.environ)` and exit unless the result equals `frozenset(SOURCE_MATRIX_CHILD_TOOLS)`. Return exactly six tools from `tools/list` and dispatch `tools/call` by name and exact arguments.

  The fixed result sequence is:

  ```python
  FIXED_CALL_SEQUENCE = (
      "saxo_auth_status",
      "saxo_list_registered_endpoints",
      "saxo_get_session_capabilities",
      "saxo_get_entitlements",
      "saxo_get_safe_request_ledger:clear",
      "saxo_call_registered_endpoint:before:orders",
      "saxo_call_registered_endpoint:before:positions",
      "saxo_call_registered_endpoint:before:balances",
      "saxo_call_registered_endpoint:sources",
      "saxo_call_registered_endpoint:after:orders",
      "saxo_call_registered_endpoint:after:positions",
      "saxo_call_registered_endpoint:after:balances",
      "saxo_get_safe_request_ledger:readback",
  )
  ```

  Reuse the exact strict safe dictionaries already centralized in `tests/analytics_source_matrix_support.py` by copying their literal JSON values into this standalone fixture. State fingerprints are 64-character fixed hex values and repeat before and after. Registry rows cover the sealed 18 source operations and three state operations. Each included source response uses its matching contract ID and catalog fingerprint; excluded sources receive no call. The ledger clear and readback are complete, non-evicted, SIM-only, zero-LIVE, and zero-non-GET. The fixture must reject any sequence or argument mismatch before writing a result.

- [ ] **Step 2: Add the installed-launcher fixture-candidate RED test**

  Add `test_installed_launcher_runs_sealed_deterministic_child_boundary` using a not-yet-defined `build_fixture_candidate(...)` helper from `tests/analytics_source_matrix_support.py`. The test must:

  1. Copy the repository into a temporary owner-only source root.
  2. Replace only the copied `src/saxo_bank_mcp/server.py` with `source_matrix_fixture_server.py`.
  3. Bind the copied coordinator's state-root helper to a fixed temporary owner-only directory at build time, not through a command argument or environment variable.
  4. Build a bootstrap wheel offline, prepare a bootstrap runtime, generate that fixture candidate's own schema-5 manifest, rebuild, and prepare a final fixture runtime.
  5. Run its installed `saxo-bank-analytics-source-matrix` from an unrelated `0700` working directory with fixed nonprivate SIM fixture auth inputs.
  6. Assert one distinct child, one session, one initialize, one list, exact six-tool digest, zero reconnects, zero restarts, zero exit, protocol-only stdout, discarded stderr, equal state, safe ledger, and equal prelaunch/post-exit seals.
  7. Assert the temporary guard and evidence live only below the compiled temporary state root and delete neither with production cleanup code. Let the pytest temporary directory own cleanup.

  This fixture candidate is never copied into `data/analytics/source_matrix_candidate.json` and is never treated as real SIM proof.

- [ ] **Step 3: Run the installed fixture RED test, then add only the artifact helper**

  Run:

  ```bash
  uv run pytest \
    tests/test_analytics_source_process_boundary.py::test_installed_launcher_runs_sealed_deterministic_child_boundary -v
  ```

  Expected RED: FAIL during collection because `build_fixture_candidate` is not defined.

  Then add `build_fixture_candidate(...)` to `tests/analytics_source_matrix_support.py`. It accepts a pytest temporary root and absolute `uv` path; it accepts no server command, runtime environment override, official output path, or arbitrary source patch. Use this fixed structure:

  ```python
  @dataclass(frozen=True, slots=True)
  class SealedFixtureCandidate:
      identity: str
      receipt: dict[str, JsonValue]
      runtime: Path
      state_root: Path

  def build_fixture_candidate(root: Path, uv_path: Path) -> SealedFixtureCandidate:
      source = root / "source"
      compiled_home = root / "compiled-home"
      compiled_home.mkdir(mode=0o700)
      state_root = compiled_home / ".local/state/saxo-bank-mcp"
      shutil.copytree(
          REPOSITORY_ROOT,
          source,
          symlinks=False,
          ignore=shutil.ignore_patterns(
              ".git",
              ".venv",
              ".superpowers",
              "dist",
              "__pycache__",
              "*.pyc",
              "*.pyo",
          ),
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
      _run_checked((uv_path, "build", "--offline", "--wheel", "--out-dir", bootstrap_wheels), source)
      _run_checked(
          (
              uv_path,
              "run",
              "python",
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
      _run_checked((uv_path, "build", "--offline", "--wheel", "--out-dir", final_wheels), source)
      _run_checked(
          (
              uv_path,
              "run",
              "python",
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
          "SAXO_MCP_SIM_APP_KEY": "sealed-fixture-app-key",
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
          receipt=json.loads(evidence.read_text(encoding="utf-8")),
          runtime=final_runtime,
          state_root=state_root,
      )
  ```

  `_run_checked` converts every `Path` argument to `str`, uses `subprocess.run(..., shell=False, check=True, capture_output=True, text=True)`, and never includes captured stdout or stderr in an assertion message or published artifact. Run the same node again.

  Expected GREEN: PASS with the one sealed temporary fixture candidate and no production state-root access.

- [ ] **Step 4: Finish schema-5 generator output and byte stability**

  In `generate_analytics_source_matrix_candidate.py`, import only finite helpers from `analytics_source_runtime`. Remove `_normalize_official_import_machinery`, `_validate_official_interpreter_state`, and all QA-module private imports. Set `portable_runtime = portable_runtime_projection()` after isolated-launcher validation, then emit compactly derived values in the pretty sorted manifest:

  ```python
  payload = {
      "candidate_identity_sha256": candidate_identity_sha256,
      "child_bootstrap_sha256": hashlib.sha256(CHILD_BOOTSTRAP.encode()).hexdigest(),
      "console_scripts": console_scripts,
      "dependency_distributions": dependency_distributions,
      "harness_build_sha256": harness_build_sha256,
      "installed_build_sha256": installed_build_sha256,
      "installed_exclusions": installed_exclusions,
      "installed_files": installed_files,
      "installed_metadata_projection": installed_metadata_projection,
      "portable_runtime_entry_count": portable_runtime.entry_count,
      "portable_runtime_tree_sha256": portable_runtime.sha256,
      "runtime_identity": runtime_identity,
      "schema_version": "5",
      "source_build_sha256": source_build_sha256,
      "source_contract_catalog_sha256": catalog_sha256,
      "source_exclusions": source_exclusions,
      "source_files": source_files,
      "source_wheel_projection_sha256": source_wheel_projection_sha256,
  }
  ```

  Write with `json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"`, an owner-only temporary file, `fsync`, and atomic replace only when `--out` is outside the sealed runtime. Print only `candidate_identity_sha256`.

- [ ] **Step 5: Build the bootstrap candidate and regenerate the checked manifest**

  Use fresh explicit paths and the absolute build tool. Do not reuse a prior runtime:

  ```bash
  ARTIFACT_ROOT=$(mktemp -d)
  UV_BIN=$(command -v uv)
  case "$UV_BIN" in
    /*) ;;
    *) exit 1 ;;
  esac
  BOOTSTRAP_WHEELS="$ARTIFACT_ROOT/bootstrap-wheel"
  BOOTSTRAP_RUNTIME="$ARTIFACT_ROOT/bootstrap-runtime"
  mkdir -m 700 "$BOOTSTRAP_WHEELS"
  "$UV_BIN" build --offline --wheel --out-dir "$BOOTSTRAP_WHEELS"
  "$UV_BIN" run python scripts/prepare_analytics_source_matrix_runtime.py \
    --uv "$UV_BIN" \
    --runtime "$BOOTSTRAP_RUNTIME" \
    --wheel "$BOOTSTRAP_WHEELS/saxo_bank_mcp-0.1.0-py3-none-any.whl"
  "$BOOTSTRAP_RUNTIME/bin/saxo-bank-analytics-source-matrix-generate" \
    --repository-root "$PWD" \
    --wheel "$BOOTSTRAP_WHEELS/saxo_bank_mcp-0.1.0-py3-none-any.whl" \
    --out data/analytics/source_matrix_candidate.json
  ```

  Expected: schema 5 is written once from a runtime containing the complete copied Python closure. No Saxo process, state root, guard, or evidence is touched.

- [ ] **Step 6: Build two independent final runtimes and compare portable output**

  Continue with the same explicit artifact root:

  ```bash
  FINAL_WHEELS="$ARTIFACT_ROOT/final-wheel"
  FINAL_RUNTIME_A="$ARTIFACT_ROOT/final-runtime-a"
  FINAL_RUNTIME_B="$ARTIFACT_ROOT/final-runtime-b"
  FINAL_MANIFEST_A="$ARTIFACT_ROOT/final-manifest-a.json"
  FINAL_MANIFEST_B="$ARTIFACT_ROOT/final-manifest-b.json"
  mkdir -m 700 "$FINAL_WHEELS"
  "$UV_BIN" build --offline --wheel --out-dir "$FINAL_WHEELS"
  "$UV_BIN" run python scripts/prepare_analytics_source_matrix_runtime.py \
    --uv "$UV_BIN" \
    --runtime "$FINAL_RUNTIME_A" \
    --wheel "$FINAL_WHEELS/saxo_bank_mcp-0.1.0-py3-none-any.whl"
  "$UV_BIN" run python scripts/prepare_analytics_source_matrix_runtime.py \
    --uv "$UV_BIN" \
    --runtime "$FINAL_RUNTIME_B" \
    --wheel "$FINAL_WHEELS/saxo_bank_mcp-0.1.0-py3-none-any.whl"
  "$FINAL_RUNTIME_A/bin/saxo-bank-analytics-source-matrix-generate" \
    --repository-root "$PWD" \
    --wheel "$FINAL_WHEELS/saxo_bank_mcp-0.1.0-py3-none-any.whl" \
    --out "$FINAL_MANIFEST_A"
  "$FINAL_RUNTIME_B/bin/saxo-bank-analytics-source-matrix-generate" \
    --repository-root "$PWD" \
    --wheel "$FINAL_WHEELS/saxo_bank_mcp-0.1.0-py3-none-any.whl" \
    --out "$FINAL_MANIFEST_B"
  cmp data/analytics/source_matrix_candidate.json "$FINAL_MANIFEST_A"
  cmp "$FINAL_MANIFEST_A" "$FINAL_MANIFEST_B"
  ```

  Expected: both `cmp` commands succeed. Keep `ARTIFACT_ROOT` available for inspection; do not recursively delete it as part of the proof.

- [ ] **Step 7: Verify installed identity, modes, and external-cache cleanup**

  Run only local modes from both launchers:

  ```bash
  "$FINAL_RUNTIME_A/bin/saxo-bank-analytics-source-matrix" --help
  IDENTITY_A=$(
    "$FINAL_RUNTIME_A/bin/saxo-bank-analytics-source-matrix" --identity
  )
  IDENTITY_B=$(
    "$FINAL_RUNTIME_B/bin/saxo-bank-analytics-source-matrix" --identity
  )
  test "$IDENTITY_A" = "$IDENTITY_B"
  /usr/bin/env -i \
    LANG=C \
    LC_ALL=C \
    SAXO_MCP_ENABLE_LIVE_READS=0 \
    SAXO_MCP_ENABLE_LIVE_WRITES= \
    SAXO_MCP_ENVIRONMENT=SIM \
    SAXO_MCP_SIM_APP_KEY=sealed-preflight-app-key \
    SAXO_MCP_SIM_REDIRECT_URI=http://localhost:8080/callback \
    SAXO_MCP_TOKEN_CACHE_PATH="$ARTIFACT_ROOT/preflight-token-cache.json" \
    "$FINAL_RUNTIME_A/bin/saxo-bank-analytics-source-matrix" --preflight
  "$UV_BIN" run pytest \
    tests/test_analytics_source_process_boundary.py::test_runtime_is_owner_only_nonwritable_and_uses_external_empty_caches \
    tests/test_analytics_source_process_boundary.py::test_installed_launcher_runs_sealed_deterministic_child_boundary -v
  ```

  Expected: identities match, local preflight succeeds without spawn or network access, every final mode is exact, copied interpreters and runtime artifacts are internal, and no run directory remains after a clean launcher return. The deterministic fixture test uses only its compiled temporary state root.

- [ ] **Step 8: Prove all 18 exact acceptance names exist once**

  Run this read-only assertion:

  ```bash
  "$UV_BIN" run python - <<'PY'
  import ast
  from pathlib import Path

  expected = {
      "test_official_matrix_has_no_fastmcp_transport_or_server_injection",
      "test_process_session_spawns_one_distinct_child_over_stdio",
      "test_child_command_uses_exact_installed_interpreter_and_isolated_bootstrap",
      "test_child_lists_exact_six_tools_before_readiness",
      "test_ambient_filter_or_live_configuration_refuses_before_spawn",
      "test_child_start_initialize_or_early_exit_never_claims_or_restarts",
      "test_process_session_cannot_initialize_connect_or_spawn_twice",
      "test_child_protocol_failure_after_claim_publishes_one_minimal_failure",
      "test_child_nonzero_exit_after_matrix_is_not_success",
      "test_prelaunch_static_runtime_change_refuses_before_child",
      "test_runtime_is_owner_only_nonwritable_and_uses_external_empty_caches",
      "test_postexit_static_change_overrides_tool_or_child_failure",
      "test_readiness_clear_claim_state_source_ledger_order_is_exact",
      "test_registered_call_profile_refuses_unsealed_tool_arguments",
      "test_process_boundary_receipt_binds_pid_stdio_tools_and_zero_reconnects",
      "test_process_receipt_and_failure_paths_publish_no_private_runtime_values",
      "test_descriptor_bound_evidence_refuses_swapped_claimed_ancestor",
      "test_live_object_projection_functions_are_absent",
  }
  tree = ast.parse(
      Path("tests/test_analytics_source_process_boundary.py").read_text(encoding="utf-8"),
  )
  names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
  assert {name for name in names if name in expected} == expected
  assert all(names.count(name) == 1 for name in expected)
  PY
  ```

  Expected: PASS with every approved test defined exactly once and no renamed substitute.

- [ ] **Step 9: Run the exact focused offline suite**

  Run the approved command, excluding deleted round 7:

  ```bash
  "$UV_BIN" run pytest \
    tests/test_analytics_source_process_boundary.py \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_area_b_review_fixes.py \
    tests/test_area_b_review_round2.py \
    tests/test_area_b_review_round3.py \
    tests/test_area_b_review_round4.py \
    tests/test_area_b_review_round5.py \
    tests/test_area_b_review_round6.py \
    tests/test_agent_skill_eval_tool_filter.py \
    tests/test_analytics_source_receipt.py \
    tests/test_registered_call_tool.py \
    tests/test_safe_request_ledger_tool.py
  ```

  Expected: PASS. No test contacts Saxo, authenticates, uses LIVE, or writes the official state root.

- [ ] **Step 10: Run lint and strict types on every changed executable file**

  Run:

  ```bash
  "$UV_BIN" run ruff check \
    src/saxo_bank_mcp/analytics_source_process.py \
    src/saxo_bank_mcp/analytics_source_runtime.py \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/generate_analytics_source_matrix_candidate.py \
    src/saxo_bank_mcp/server_eval_tool_filter.py \
    scripts/prepare_analytics_source_matrix_runtime.py \
    scripts/run_analytics_source_matrix.py \
    scripts/generate_analytics_source_matrix_candidate.py \
    tests/analytics_source_matrix_support.py \
    tests/fixtures/analytics/source_matrix_stdio_child.py \
    tests/fixtures/analytics/source_matrix_fixture_server.py \
    tests/test_analytics_source_process_boundary.py \
    tests/test_qa_analytics_source_matrix.py
  "$UV_BIN" run basedpyright \
    src/saxo_bank_mcp/analytics_source_process.py \
    src/saxo_bank_mcp/analytics_source_runtime.py \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/generate_analytics_source_matrix_candidate.py \
    src/saxo_bank_mcp/server_eval_tool_filter.py \
    tests/analytics_source_matrix_support.py \
    tests/test_analytics_source_process_boundary.py
  ```

  Expected: both commands pass with no ignored new process, runtime, or privacy diagnostic.

- [ ] **Step 11: Run negative boundary scans and the relevant full offline Area B suite**

  Run:

  ```bash
  if rg -n \
    'FastMCPTransport|Client\(server\)|from saxo_bank_mcp\.server import|_stable_import_state|_module_execution_projection|_loaded_module_projection|_interpreter_execution_projection|_execution_root_projection_sha256|_stabilized_execution_root_projection_sha256' \
    src/saxo_bank_mcp/qa_analytics_source_matrix.py \
    src/saxo_bank_mcp/analytics_source_process.py \
    src/saxo_bank_mcp/analytics_source_runtime.py \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_area_b_review_round5.py \
    tests/test_area_b_review_round6.py; then
    exit 1
  fi
  test ! -e tests/test_area_b_review_round7.py
  "$UV_BIN" run pytest \
    tests/test_analytics_config.py \
    tests/test_analytics_models.py \
    tests/test_analytics_provider.py \
    tests/test_analytics_store.py \
    tests/test_analytics_source_contracts.py \
    tests/test_analytics_source_receipt.py \
    tests/test_analytics_source_store_bridge.py \
    tests/test_analytics_source_process_boundary.py \
    tests/test_qa_analytics_source_matrix.py \
    tests/test_area_b_review_fixes.py \
    tests/test_area_b_review_round2.py \
    tests/test_area_b_review_round3.py \
    tests/test_area_b_review_round4.py \
    tests/test_area_b_review_round5.py \
    tests/test_area_b_review_round6.py \
    tests/test_agent_skill_eval_tool_filter.py \
    tests/test_registered_call_tool.py \
    tests/test_registered_call_tool_failures.py \
    tests/test_safe_request_ledger_tool.py
  ```

  Expected: the scan has no match and the complete selected Area B suite passes offline.

- [ ] **Step 12: Update operator documentation to the process boundary**

  In `docs/analytics-bi-vision.md`, replace the current runtime paragraph and command block with:

  - copied and sealed CPython base inside each candidate;
  - final directory, file, and executable modes `0500`, `0400`, and `0500`;
  - external per-run coordinator and child cache/work/temp directories;
  - one exact installed interpreter, one direct stdio child, one MCP session, exact six tools, and no reconnect or restart;
  - prelaunch and post-exit descriptor-held static verification;
  - process-boundary receipt fields and fixed reason precedence;
  - the two-build byte-identical portable manifest check from Steps 5 through 7;
  - explicit wording that fixture replay and offline process proof cannot close Task 6.

  Preserve the real acceptance instruction but mark it blocked: no normal installed invocation occurs until ready real SIM auth and separate authorization exist. State that the future one-shot run must prove SIM-only ledger, unchanged state, zero mutation calls, privacy success, exact process receipt, and clean post-exit seal.

- [ ] **Step 13: Inspect the final diff for prohibited scope changes**

  Run:

  ```bash
  git diff --check
  git diff --name-status 369629df19de255e951393a8ac0399e0be37222f..HEAD
  git diff -- \
    data/analytics/source_contracts.json \
    src/saxo_bank_mcp/analytics_provider.py \
    src/saxo_bank_mcp/analytics_store.py \
    src/saxo_bank_mcp/analytics_migrations.py \
    data/analytics/migrations
  ```

  Expected: `git diff --check` passes; the second command shows only files in this plan; the scoped diff is empty. Review the generated manifest rather than hand-editing any digest.

- [ ] **Step 14: Commit the final candidate and documentation**

  ```bash
  git add data/analytics/source_matrix_candidate.json \
    docs/analytics-bi-vision.md \
    src/saxo_bank_mcp/generate_analytics_source_matrix_candidate.py \
    tests/analytics_source_matrix_support.py \
    tests/test_analytics_source_process_boundary.py \
    tests/fixtures/analytics/source_matrix_fixture_server.py
  git commit -m "chore: freeze analytics process candidate"
  ```

- [ ] **Step 15: Record the blocked acceptance handoff without external action**

  Record these facts in the task report only:

  ```text
  offline_process_boundary=passed
  portable_candidate_builds=2
  portable_candidate_identities_equal=true
  official_sim_run=not_run
  official_guard_created=false
  official_evidence_created=false
  sim_acceptance=blocked_on_ready_auth_and_separate_authorization
  ```

  Stop here. Do not authenticate, open a browser, call Saxo, clear a real ledger, read brokerage state, claim the final candidate, or publish official evidence.

## Completion Criteria

- All six implementation commits exist and the worktree is clean.
- Every exact RED test exists once and passes.
- The coordinator has no FastMCP/server import or live-object projection path.
- The child uses one real process, real stdio pipes, one MCP session, exact six tools, and zero reconnects or restarts.
- The final candidate contains copied Python, exact nonwritable modes, no internal cache, no bytecode, and no unsupported entry.
- Two separately prepared final runtimes emit byte-identical schema-5 manifests and portable candidate identities.
- Claimed success is serialized only after zero child exit and unchanged post-exit static seal.
- Claimed failure uses one descriptor-bound two-key object with exact precedence and no private runtime value.
- Production remains unfiltered with 39 tools; provider, store, source contracts, registered call schemas, ledger behavior, and migrations remain unchanged.
- The implementation report states that real SIM acceptance is still blocked and that no official external or state-changing action occurred.
