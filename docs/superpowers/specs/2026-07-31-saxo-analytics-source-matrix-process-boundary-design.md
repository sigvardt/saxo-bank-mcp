# Saxo analytics source matrix process boundary

Date: 2026-07-31

Status: approved architecture amendment for Task 6

Related documents:

- `docs/analytics-bi-vision.md`
- `docs/superpowers/plans/2026-07-30-saxo-analytics-bi-suite.md`
- `.superpowers/sdd/2026-07-30-saxo-analytics-bi-suite/task-6-report.md`
- `.superpowers/sdd/2026-07-30-saxo-analytics-bi-suite/area-b-fix-report.md`

## Decision

The official analytics source-matrix run will use a fresh installed coordinator and one separate Model Context Protocol (MCP) child process over standard input and standard output. The coordinator will start the child with the exact interpreter from the sealed candidate runtime. The child will register exactly six existing tools. The coordinator will open exactly one MCP session, perform readiness and the source-matrix run in that session, close the child, revalidate the static runtime, and only then publish evidence.

This replaces the current `Client(server)` path. Passing a live `FastMCP` object selects `FastMCPTransport` and keeps the server, client, imported modules, globals, classes, and mutable caches in one Python heap. Repeated projection of that heap cannot establish a finite execution boundary. A distinct operating-system process gives the coordinator a small interface that can be listed, counted, sequenced, and closed.

The change is limited to the Task 6 official source-matrix harness and its installed runtime. It does not change the analytics product design, the 18 source contracts, MCP tool input or output schemas, the unfiltered 39-tool server, providers, the analytics store, or database migrations.

## Security objective and trust boundary

The evidence must show that one sealed candidate made the expected SIM-only read sequence through one filtered MCP child, made no mutation call, observed unchanged account state, and published no private values.

The trusted boundary contains:

- the installed candidate manifest and its verified source, wheel, distribution, dependency, launcher, interpreter, standard-library, shared-library, and build-configuration closure;
- the coordinator launched from that candidate with exact isolated interpreter flags;
- operating-system process, pipe, file-descriptor, ownership, mode, identity, and atomic-link operations;
- the existing server-side tool filter, registered read tool, safe request ledger, and strict receipt validators;
- the held descriptor for the claimed guard directory.

The following inputs are not trusted and must be checked or excluded:

- the repository checkout, current working directory, `PATH`, ambient Python import settings, ambient tool-filter settings, and arbitrary launcher arguments;
- a child tool list, MCP structured result, Saxo response, child exit status, stdout framing, or stderr text;
- raw response bodies, OAuth material, account and client keys, process identifiers, file descriptors, paths, exception strings, and environment dumps;
- any Python object graph already present in the coordinator.

The child server heap is outside the coordinator heap. The coordinator never imports or receives a `FastMCP` server object. It accepts only framed MCP messages from the child's stdout and writes only framed MCP messages to the child's stdin.

This design detects changes to the sealed files and recorded runtime metadata before launch and after child exit. It does not claim to defeat a hostile process already able to read or write the coordinator or child memory, `ptrace` either process, or falsify operating-system results. Kernel, firmware, and hardware compromise are also out of scope. Saxo service availability and the business truth of returned market or account data remain external assertions, not properties proven by this harness.

## Components

### Installed launcher and coordinator

The existing `saxo-bank-analytics-source-matrix` entry point remains the user-facing command. It must `exec` the exact installed interpreter with `-I`, `-B`, `-S`, one fixed `pycache_prefix`, and a sealed bootstrap. It must not search `PATH`, import the checkout, accept a server object, or accept an alternate child command.

`saxo_bank_mcp.qa_analytics_source_matrix` remains the coordinator and receipt owner. Its official entry point performs local validation, owns the one-shot process session, runs the matrix protocol, closes the child, repeats local validation, scans the final evidence, and publishes through the claimed directory descriptor.

The official function may retain the existing optional source fixtures. It must not expose injection parameters for a `FastMCP` instance, transport, process command, process factory, state output path, identity, claim callback, runtime validator, or publication callback. Unit seams belong below the official entry point and must use narrow scripted session or process events, never a live server object.

### One-shot process session

A small process-session component owns the direct child process handle, child PID, stdin pipe, stdout pipe, stderr drain, and one MCP `ClientSession`. It is a state machine with these states:

`new -> spawned -> initialized -> listed -> running -> closing -> exited`

Every transition is one way. A second spawn, second initialize, second session, reconnect, or restart raises a value-free local error. The component must use a direct process primitive that exposes the actual process handle and PID. It must not use a general client transport that can reconnect, keep a server alive, or start a replacement process.

Closing the session closes stdin and waits a bounded time for normal child exit. Timeout or nonzero exit is a failure. Cleanup may terminate and reap the same child so it cannot be orphaned, but cleanup must never start another child or repeat an MCP request.

### Filtered MCP child

The child uses the existing `saxo_bank_mcp.server` construction and existing environment-driven tool filter. It is started directly as the exact interpreter with `--transport stdio`. A sealed bootstrap inserts only the candidate's installed site-packages directory before importing the server. The bootstrap validates the child's isolated flags, executable, import roots, cache prefix, and fixed arguments before server construction.

The child environment is built from a fixed allowlist, never by copying the ambient environment. It contains the five fixed SIM, LIVE-gate, and filter settings listed below; the effective existing SIM inputs `SAXO_MCP_SIM_APP_KEY` or `SAXO_MCP_SIM_CLIENT_ID`, `SAXO_MCP_SIM_CREDENTIAL_FILE`, `SAXO_MCP_SIM_REDIRECT_URI`, `SAXO_MCP_SIM_AUTH_URL`, `SAXO_MCP_SIM_TOKEN_URL`, and `SAXO_MCP_TOKEN_CACHE_PATH` when applicable; a coordinator-created `TMPDIR`; and `LC_ALL=C` plus `LANG=C`. An optional `SSL_CERT_FILE` or `SSL_CERT_DIR` is allowed only when its absolute no-follow target is part of the verified non-writable runtime closure. The coordinator resolves default credential and token paths before spawn, then passes the explicit validated result. It does not pass `PATH`, proxy settings, LIVE credentials, QA fixtures, analytics, store, audit, write-safety, or other `SAXO_MCP_*` settings. QA account and client fixtures remain in coordinator memory and appear only in the sealed registered-call arguments that require them.

All `PYTHON*` settings are removed. If the caller supplied either `SAXO_MCP_EVAL_TOOL_FILTER` or `SAXO_MCP_EVAL_ALLOWED_TOOLS`, the coordinator refuses before spawn instead of accepting or overwriting them. The coordinator then calls the existing `derive_eval_tool_filter_env` with the exact tool tuple and checks `resolve_eval_tool_filter` against the finished child environment.

The finished child environment must have:

- `SAXO_MCP_ENVIRONMENT=SIM`;
- `SAXO_MCP_ENABLE_LIVE_READS=0`;
- `SAXO_MCP_ENABLE_LIVE_WRITES` present with an empty value;
- `SAXO_MCP_EVAL_TOOL_FILTER=1`;
- `SAXO_MCP_EVAL_ALLOWED_TOOLS` equal to the exact comma-separated allowlist below.

Any different value, duplicate tool, wildcard, qualified name, unknown tool, missing tool, extra tool, HTTP transport argument, alternate executable, alternate site-packages path, or caller-selected bootstrap is a pre-spawn refusal.

### Static runtime verifier

Runtime preparation may use owner-writable directories while installing. Final sealing must remove every owner write bit before the runtime is accepted:

- candidate directories are owner-only mode `0500`;
- regular data and source files are owner-only mode `0400`;
- files that must be executed are owner-only mode `0500`;
- the only symlinks allowed are the named conventional interpreter aliases already covered by the manifest, and every alias must resolve to the recorded interpreter;
- the candidate tree contains no cache directory, bytecode, socket, device, FIFO, writable file, unlisted file, or unlisted link.

The coordinator cache, child cache, working directory, `TMPDIR`, stderr drain state, guard, and evidence live outside the sealed runtime in separate owner-only state directories. Mutable directories use `0700`; mutable files use `0600`. Both interpreters use `-B`. Each fixed cache prefix, working directory, and `TMPDIR` must start empty, remain outside the sealed runtime, and be empty again after child exit. An unexpected entry is a runtime-validation failure.

The verifier has two projections:

1. The portable candidate identity covers relative names, file types, modes, content hashes, source manifest, built wheel, installed distributions and metadata, locked dependencies, launchers, bootstrap bytes, interpreter identity, standard library, extension and shared libraries, startup configuration, and selected Python build configuration.
2. The per-run seal also records device, inode, owner, mode, size, modification time, change time, and canonical ancestor identity. It holds no-follow descriptors for the candidate root and required ancestors so the post-exit check can prove that the canonical path still names the same closure.

Before child launch, both projections and the coordinator's finite scalar state must validate. The scalar checks are limited to the exact executable, `sys.path`, `sys.prefix` values, isolated flags, bytecode flag, no-site flag, cache prefix, implementation/version, and fixed integer conversion limit. They do not traverse Python modules or arbitrary objects.

After the child has exited and been reaped, the same descriptor-relative file walk and scalar-independent runtime projection must produce the same portable identity and per-run seal. This check runs on every exit path, including preclaim readiness refusal and postclaim failure. Evidence serialization cannot begin before it succeeds. A current-user-writable interpreter or external runtime artifact is refused unless preparation copied and sealed it inside the candidate closure.

## Exact child tool surface

The filtered child must expose exactly this set and no other tool:

1. `saxo_auth_status`
2. `saxo_list_registered_endpoints`
3. `saxo_get_session_capabilities`
4. `saxo_get_entitlements`
5. `saxo_get_safe_request_ledger`
6. `saxo_call_registered_endpoint`

Immediately after MCP initialization, the coordinator calls `tools/list` once. It rejects duplicate names and compares the returned set with the six names above before making any readiness call. The tool-set digest is SHA-256 over compact canonical JSON containing the six names in lexical order.

The coordinator's call wrapper also has a fixed argument profile per tool. It refuses any tool name outside the list. `saxo_call_registered_endpoint` is limited to `GET`, the sealed 18 source-contract operations and the three state paths, and the approved response mode for that call. State calls use `fingerprint_only`. Source calls use `analytics_contract_receipt` with the matching sealed contract ID. No arbitrary URL, method, path, response mode, operation ID, or configuration value can pass from ambient input into a tool call.

The production server remains unfiltered by default and continues to expose all 39 tools. The six-tool registration applies only to this explicitly filtered SIM child.

## One-child, one-session protocol

One command invocation performs the following order. A failed step closes the same child and does not continue to a later step.

1. Prove local `SIM` selection, disabled LIVE gates, the 18-contract catalog, source plan, candidate identity, non-writable runtime, fixed launcher configuration, empty external cache directories, and exact derived child filter.
2. Spawn one child directly with the exact installed interpreter. Record the operating-system PID and stdin, stdout, and stderr pipe identities from the process handle. Do not use a shell or `PATH` lookup.
3. Create and initialize one MCP session over that child's stdin and stdout.
4. Call `tools/list` once and require the exact six-tool surface.
5. Call `saxo_auth_status` and require ready SIM authentication.
6. Call `saxo_list_registered_endpoints` with fixed service-group and pagination arguments until the sealed source and state operation set has been checked. Every matched operation must be registered `GET`, classified read-only, and have the existing read-only support policy.
7. Call `saxo_get_session_capabilities` and require the existing strict SIM read receipt.
8. Call `saxo_get_entitlements` and require the existing strict SIM entitlement receipt.
9. Call `saxo_get_safe_request_ledger` with `{"clear": true}` and require a complete current-session clear receipt.
10. Atomically claim the candidate with the descriptor-bound guard. No state or source call may occur before this step. If the candidate is already claimed, stop.
11. Read the before-state fingerprints in this order through `saxo_call_registered_endpoint`: `/port/v1/orders/me`, `/port/v1/positions/me`, `/port/v1/balances/me`. Orders and positions use the existing `raw_response_body` fingerprint scope. Balances use `account_money_state_fields`.
12. Execute the sealed source plan in its canonical contract order. Each included source makes one MCP call to `saxo_call_registered_endpoint` with `response_mode=analytics_contract_receipt`. Excluded sources receive the existing value-free unavailable receipt. Pagination, continuation, and any existing bounded provider retry occur inside that one server tool call and remain visible in its receipt.
13. Repeat the same three state fingerprint calls in the same order and require equality with the before-state digest.
14. Call `saxo_get_safe_request_ledger` without clearing. Require a complete, non-evicted, SIM-only ledger with zero LIVE gateway events and zero non-GET gateway requests.
15. Close the one MCP session, close child stdin, drain and discard stderr, wait for exit, and reap the same child. Require exit code zero. Do not reconnect, restart, or reissue a timed-out or failed MCP request.
16. Revalidate the complete static runtime and empty external caches after child exit.
17. Add the process-boundary receipt, validate the full source-matrix receipt, scan the exact JSON text for private values, and publish through the held claimed-directory descriptor.

The no-retry rule applies to the coordinator-to-child MCP boundary. It does not change the existing bounded read retry inside the registered endpoint provider. Registry pagination and source continuation are progress to a new sealed target, not replay of a failed MCP request.

## Process-boundary receipt

Every full claimed source-matrix receipt gains one internal `process_boundary` object with these exact fields:

- `transport`: literal `stdio`;
- `child_process_distinct`: literal `true`, derived from the child process handle PID being different from the coordinator PID;
- `process_identity_sha256`: SHA-256 over canonical JSON containing the candidate identity, coordinator PID, child PID, and verified child executable identity;
- `stdio_identity_sha256`: SHA-256 over canonical JSON containing the verified stdin and stdout pipe endpoint identities;
- `child_spawn_count`: literal `1`;
- `mcp_session_count`: literal `1`;
- `mcp_initialize_count`: literal `1`;
- `tool_list_count`: literal `1`;
- `tool_count`: literal `6`;
- `tool_ids_sha256`: the exact tool-set digest defined above;
- `reconnect_count`: literal `0`;
- `restart_count`: literal `0`;
- `child_exit_code`: literal `0`;
- `stdout_protocol_only`: literal `true`;
- `stderr_published`: literal `false`.

The coordinator obtains PID and pipe facts from operating-system handles, not from a child message. Raw PIDs, file descriptors, pipe inode values, and paths are never serialized. The two hashes bind those facts to the candidate without publishing the raw values. The full receipt is not final until the child exit code and post-exit runtime check are known.

## Claim and receipt rules

### Before claim

Preclaim outcomes are value-free in-memory refusal receipts used for control flow and tests. The official command returns nonzero and writes no guard and no `source-matrix.json`. It does not print child output or the refusal object. The exact coordinator-level preclaim reasons are:

- `environment_not_sim`
- `live_reads_enabled`
- `live_writes_enabled`
- `source_contract_count_mismatch`
- `source_plan_invalid`
- `candidate_static_runtime_invalid`
- `child_configuration_refused`
- `child_process_unavailable`
- `child_tool_allowlist_mismatch`
- `sim_auth_unavailable`
- `registered_operation_mismatch`
- `sim_session_unavailable`
- `sim_entitlements_unavailable`
- `request_ledger_unavailable`
- `candidate_already_claimed`

Invalid child command, derived child environment, filter, or startup configuration maps to `child_configuration_refused` before spawn. The earlier local SIM and LIVE-gate proof keeps its three exact environment reasons. Spawn, initialization, framing, stdout noise, early exit, timeout, nonzero exit, or shutdown failure before claim maps to `child_process_unavailable`. A listed-tool mismatch maps to `child_tool_allowlist_mismatch`. The post-exit runtime check still runs after every spawned-child outcome. A post-exit static mismatch has first precedence and replaces the prior reason with `candidate_static_runtime_invalid`; with an unchanged runtime, a process failure has precedence over a tool-list or readiness reason. Since no new guard was created, a later explicit invocation may try again after SIM authentication or external readiness has been repaired. There is no automatic retry within the invocation.

### After claim

The claimed directory descriptor, directory device and inode, guard device and inode, and exact guard payload stay open through child teardown, static revalidation, privacy scanning, and publication. Every read, temporary-file creation, write, `fsync`, hard link, final open, and cleanup uses names relative to that descriptor with no-follow checks. Before the final link, the coordinator reopens the canonical parent chain and proves that it still resolves to the held directory and guard. A swapped ancestor, replaced guard, changed owner or mode, existing evidence file, or mismatched final inode fails closed. There is no alternate output path.

A normal claimed run publishes the full receipt even when its validated matrix status is `failed`. Existing matrix reasons such as `state_fingerprint_unverified`, `state_fingerprint_mismatch`, `unsafe_request_ledger`, `live_events_detected`, and existing source-level refusal or reduction reasons keep their current meanings.

An exception that prevents a valid full receipt publishes only a two-key object. Its `status` is `failed`, and its `reason` is exactly one of these values:

- `candidate_static_runtime_changed`
- `child_process_failed`
- `matrix_execution_failed`
- `evidence_secret_scan_failed`

Reason precedence is fixed. A failed post-exit static check is `candidate_static_runtime_changed` even if a tool, protocol, or child-exit error happened first. With an unchanged runtime, any child framing, pipe, shutdown, timeout, or nonzero-exit error is `child_process_failed`. Other execution or validation exceptions are `matrix_execution_failed`. A private-value finding or scan error after all prior checks is `evidence_secret_scan_failed`.

Publication is best effort only through the verified descriptor. If immutable publication itself cannot be completed, the command returns nonzero and does not replace an existing file, follow a new path, or weaken the descriptor checks to publish a second receipt.

## Privacy

Child stdout is reserved for MCP framing. Any non-protocol bytes cause process failure. Stderr is drained so the child cannot block, but content is discarded and only a byte count may remain in memory. Stderr content, exception messages, stack traces, environment values, command paths, and raw MCP messages are never copied into evidence.

The existing strict tool receipts remain the only accepted data across the child boundary. Source results expose safe counts, status values, schema and response fingerprints, request fingerprints, revision and timestamp proofs, and fixed operation metadata. State calls expose fingerprints only. The safe ledger exposes its fixed safe fields only. Raw Saxo bodies, tokens, account keys, client keys, query values, and refresh material are not retained.

The coordinator serializes with non-finite JSON values forbidden, applies the existing secret scanner to the exact final bytes, and publishes only when findings and scan errors are both zero. Guard and evidence directories remain `0700`; files remain `0600`.

## Removal of live Python object projection

The implementation must remove the general live-object sealing path from the official harness, including `_stable_import_state`, `_module_execution_projection`, `_loaded_module_projection`, `_interpreter_execution_projection`, `_execution_root_projection_sha256`, `_stabilized_execution_root_projection_sha256`, and helpers used only by those functions.

The coordinator must no longer traverse `sys.modules`, module globals, function closures, code constants, classes, metaclasses, descriptors, import hook objects, caches, mapping-form slots, alias graphs, or other arbitrary heap values. It must not stabilize a digest by repeated heap scans. Finite startup scalar checks and static file, metadata, dependency, and launcher validation remain required.

The process boundary does not claim that the child heap is immutable. It makes the child heap disposable and inaccessible to the coordinator, limits interaction to six listed tools, records one process and one session, and validates the sealed code on both sides of the child lifetime.

## Compatibility and migration

- The public installed command names and the 18 source contracts stay unchanged.
- The official runner no longer accepts or constructs an in-process server. Internal matrix logic consumes a narrow already-open session interface.
- The unfiltered server still exposes 39 tools. Existing MCP tool names, descriptions, input schemas, output schemas, middleware, and registered endpoint definitions do not change.
- The existing tool filter remains the registration control. This design adds coordinator-side refusal and post-initialize list verification; it does not add a second server registry.
- Provider request behavior, bounded registered-GET retry, pagination, source receipt reduction rules, analytics store code, and database migrations do not change.
- Existing descriptor-bound guard and publication mechanics are retained. The harness build hash changes, so the new implementation is a new candidate identity and does not rewrite or reuse historical evidence.
- The persisted Task 6 evidence model adds `process_boundary` for new full receipts. Historical artifacts are not migrated. Minimal failure receipts keep the existing two-key shape.
- `candidate_execution_closure_changed` is retired from the new harness. Preclaim static failure uses `candidate_static_runtime_invalid`; postclaim static change uses `candidate_static_runtime_changed`. This is an evidence-reason migration only, not an MCP or database schema change.

## Test migration and exact RED cases

Tests that prove arbitrary Python heap projection must be removed, not widened again:

- replace `test_execution_closure_is_revalidated_after_claim_and_around_every_mcp_call` with process ordering, one-session, and post-exit seal tests;
- replace `test_interpreter_projection_seals_all_import_state_and_loaded_origins` with sealed runtime mode, direct child launch, and static pre/post validation tests;
- remove `test_execution_identity_binds_complete_cpython_state_and_loaded_behavior`;
- adapt `test_postclaim_closure_change_wins_over_tool_exception_and_publication` to assert post-exit static-change precedence;
- retain `test_evidence_publication_refuses_a_swapped_claimed_ancestor`;
- remove the round 7 originless-global, class, metaclass, and `json.encoder.INFINITY` projection tests;
- retain and adapt the fresh installed runtime, candidate identity, package closure, source receipt, provider, ledger, filter, guard, privacy, and schema-drift tests.

Before implementation, add the following tests with these exact names. They must fail against the current in-process implementation:

1. `test_official_matrix_has_no_fastmcp_transport_or_server_injection`: fails while the official path imports `FastMCPTransport`, calls `Client(server)`, or accepts a live server seam.
2. `test_process_session_spawns_one_distinct_child_over_stdio`: fails without one direct process handle, a distinct child PID, and verified stdin and stdout pipes.
3. `test_child_command_uses_exact_installed_interpreter_and_isolated_bootstrap`: fails for shell use, `PATH` lookup, alternate interpreter, mutable bootstrap, wrong site path, wrong flags, or non-stdio transport.
4. `test_child_lists_exact_six_tools_before_readiness`: fails for a missing, extra, duplicate, qualified, or unlisted tool, or if auth is called before list verification.
5. `test_ambient_filter_or_live_configuration_refuses_before_spawn`: fails unless caller filter variables, non-SIM selection, LIVE gates, and invalid allowlists stop before process creation.
6. `test_child_start_initialize_or_early_exit_never_claims_or_restarts`: fails unless every preclaim process error leaves no new guard or evidence and spawn count stays one.
7. `test_process_session_cannot_initialize_connect_or_spawn_twice`: fails unless a second session transition is refused and reconnect and restart counters stay zero.
8. `test_child_protocol_failure_after_claim_publishes_one_minimal_failure`: fails unless malformed or truncated stdout after claim maps to `child_process_failed` through the held descriptor with no request replay.
9. `test_child_nonzero_exit_after_matrix_is_not_success`: fails unless a nonzero exit blocks a full receipt and maps to `child_process_failed`.
10. `test_prelaunch_static_runtime_change_refuses_before_child`: fails unless content, type, mode, owner, link, cache, extra-file, interpreter, or ancestor changes stop before spawn.
11. `test_runtime_is_owner_only_nonwritable_and_uses_external_empty_caches`: fails while the installed runtime contains owner-writable entries or an internal cache prefix.
12. `test_postexit_static_change_overrides_tool_or_child_failure`: fails unless a change during the child lifetime maps to `candidate_static_runtime_changed` before evidence serialization.
13. `test_readiness_clear_claim_state_source_ledger_order_is_exact`: fails unless tool listing, auth, registry, session, entitlements, ledger clear, claim, before state, 18-source plan, after state, and ledger readback occur in that order in one session.
14. `test_registered_call_profile_refuses_unsealed_tool_arguments`: fails unless non-GET methods, unsealed paths, wrong response modes, arbitrary URLs, and mismatched contract IDs are refused locally.
15. `test_process_boundary_receipt_binds_pid_stdio_tools_and_zero_reconnects`: fails until all exact process receipt fields are present and derived from operating-system handles.
16. `test_process_receipt_and_failure_paths_publish_no_private_runtime_values`: fails if raw PIDs, descriptors, pipe identities, paths, stderr, exceptions, environment values, fixtures, tokens, or response bodies appear.
17. `test_descriptor_bound_evidence_refuses_swapped_claimed_ancestor`: fails if publication can escape the held directory or use a fallback path.
18. `test_live_object_projection_functions_are_absent`: fails while any retired heap projection function or its round 5 through 7 projection tests remain.

Pure matrix tests may use a scripted narrow session to cover structured result parsing and source reduction. Process-boundary tests must exercise a real subprocess and real pipes. They may replace Saxo calls with sealed deterministic child tool results, but they must not pass a `FastMCP` object into the coordinator or weaken the official command builder.

## Focused validation after implementation

Run the following offline checks before any official SIM attempt:

```text
uv run pytest \
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

uv run ruff check \
  src/saxo_bank_mcp/qa_analytics_source_matrix.py \
  src/saxo_bank_mcp/server_eval_tool_filter.py \
  scripts/prepare_analytics_source_matrix_runtime.py \
  tests/test_analytics_source_process_boundary.py \
  tests/test_qa_analytics_source_matrix.py

uv run basedpyright \
  src/saxo_bank_mcp/qa_analytics_source_matrix.py \
  src/saxo_bank_mcp/server_eval_tool_filter.py \
  tests/test_analytics_source_process_boundary.py
```

Also perform these focused artifact checks:

1. Build the wheel offline and prepare two fresh installed runtimes from the same inputs.
2. Verify identical portable candidate identities and exact non-writable modes in both runtimes.
3. Run the installed launcher from an unrelated owner-only working directory with a deterministic sealed child fixture and verify one distinct PID, one session, exact six tools, zero reconnects, zero restarts, clean exit, and identical prelaunch and post-exit seals.
4. Mutate one copied runtime in each RED category and verify the required preclaim or postclaim refusal without evidence leakage.
5. Search the official harness and migrated tests for `FastMCPTransport`, `Client(server)`, and every retired heap-projection name. The official source-matrix path must contain no match.
6. Run the relevant full offline Area B suite after the focused cases pass.

No official guard or evidence should be created by deterministic or failing tests. They use dedicated temporary state roots.

## Real SIM acceptance remains blocked

This design document and its offline implementation do not complete Task 6. The official source-matrix evidence still requires a fresh installed candidate and ready real SIM authentication. Current real SIM authentication is unavailable, so no official run, claim, guard, state read, source call, ledger proof, or evidence publication is authorized as part of this architecture change.

When SIM authentication is ready, run the sealed installed command once for that candidate. A preclaim auth refusal may be followed by a later explicit invocation because it creates no guard. Once the candidate is claimed, the existing one-shot guard remains final. Task 6 can close only after the real run produces descriptor-bound evidence with the exact six-tool process receipt, SIM-only ledger, unchanged state fingerprints, privacy scan success, zero mutation calls, and a clean post-exit runtime seal.
