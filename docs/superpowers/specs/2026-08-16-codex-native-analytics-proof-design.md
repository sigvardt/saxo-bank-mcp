# Codex-native analytics proof design

Date: 2026-08-16
Status: approved for implementation

## Purpose

Replace the unavailable dual-agent Task 24 execution method with one explicit Codex-native method.
Only the model quorum changes. The numerical, source, Saxo reconciliation, artifact, privacy,
cleanup, SIM, and no-write requirements stay unchanged.

## Evidence boundary

The new path uses policy ID `codex_native_v1`. It requires exactly the `codex` harness and rejects
reports that contain another harness. Each selected hard case must execute a real Codex model call,
pass its checked assertions, preserve warnings, publish no private value, and make no broker write.

The old dual-agent producer, report types, and receipts remain unchanged. They stay historical and
cannot satisfy `codex_native_v1`.

## Installed candidate

A new Codex-only install command creates an owner-only clone, marketplace export, Codex home, and
plugin cache. Its typed report binds the exact candidate commit to:

- one Codex plugin registration;
- 9 installed skills;
- one MCP server and 60 tools;
- byte equality between the clean clone and installed cache;
- clean startup metadata;
- unchanged caller Codex state;
- complete process cleanup;
- a clean privacy scan.

The report has no Claude runtime-state field, command, account, process, or receipt. Its exact byte
inventory may include `.claude-plugin/plugin.json` as shared packaging metadata; that path proves
installed-byte parity and is not Claude runtime state or execution. The proof producer independently
rechecks the candidate commit, clean source tree, installed inventory, cache digest, registration,
and owner-only modes before it trusts the installation.

## Candidate-bound invocation

An executed `codex_native_v1` outer command must supply `--candidate-source-root`. Before install
verification or proof-runtime consumption, the launcher reads only the owner-only install binding
and requires that root to be an absolute, non-symlink Git top level in detached-HEAD state. Its
commit and tree must exactly equal the install candidate and retained-runtime binding, its tracked
and untracked status must be clean, and its proof runner and producer must be regular files.

When orchestration starts from a later documentation commit, the launcher starts the exact
candidate root's runner with that root as its working directory and source import root. The
candidate runner revalidates its own script path, producer-module path, environment binding,
commit, tree, detached state, and cleanliness. The producer checks the same root again before it
can enter the one-shot runtime boundary and again after execution. A caller cannot use the hidden
handoff flag to keep running later-commit code. Missing, mismatched, attached, or dirty roots
publish a typed `not_started` boundary failure and cannot create a consumption intent.

The historical `dual_v1` path and non-executing plan-only behavior are unchanged.

## Candidate-result publication

The outer launcher reserves the final publication path for parent-owned output. It gives the exact
candidate runner a different owner-only result path and records only a schema digest for the
path-bearing command. Before trusting candidate output, the parent verifies the complete native
publication digest and exact candidate, policy, proof contract, analysis-kind count, and expected
receipt count. The result and its directory must also be owner-only regular objects with no link or
symlink ambiguity.

After complete runner cleanup, the parent publishes the verified candidate bytes to the final path
and verifies them again. If cleanup fails, the final publication is an overall boundary refusal;
the authenticated candidate result remains separate, and the refusal binds both its byte digest
and the candidate-runner cleanup receipt. A failed cleanup does not promote child facts into the
outer refusal. Missing, malformed, tampered, mismatched, or incorrectly permissioned candidate
output remains unknown and is not retained as trusted evidence. The historical `dual_v1` path is
unchanged.

## Proof execution

The verified Codex cache starts a sealed child process. The child copies only the minimum Codex
file-backed authentication and SIM files into a temporary owner-only runtime. It never prepares,
copies, promotes, or executes Claude state.

For every non-router `codex_native_v1` hard case, the disposable `CODEX_HOME` registers the exact
retained plugin through the same marketplace-add and plugin-add CLI flow that the isolated install
already proved. Before model work begins, a local-only preflight requires Codex to report that
plugin enabled, starts its Saxo MCP server offline, and requires `list_tools` to expose exactly the
case's server-side logical-tool grant set. Registration and tool-visibility failures are typed and
stop before the model. A failed MCP-start probe keeps start provenance unknown unless the boundary
can prove whether the server started.

The child runs the existing proof work without changing its meaning:

1. the installed offline proof suite emits measured receipts for every required proof case;
2. the hard-task command runs with `--harness codex` and must return only Codex records;
3. the installed SIM child proves `environment=SIM` before Saxo activity;
4. all 54 analysis kinds retain known-answer, property, independent reference, mutation,
   numerical tolerance, accounting, Saxo reconciliation, artifact, recovery, privacy, and agent-use
   checks;
5. the result fails unless cleanup completes, account state is unchanged, LIVE calls are zero,
   broker writes are zero, no purchase occurs, and no disclaimer response occurs.

The producer writes to a new evidence namespace. It does not edit or reinterpret prior evidence.

## Activation

Process-local activation remains temporary proof authority. Checked-in profiles may change from
quarantined to active only after all 54 receipts pass for the exact installed candidate. If SIM
authentication, Saxo data, or permissions are unavailable, the producer returns a refusal or
reduced result and leaves all checked-in profiles quarantined.

An activation commit is a new candidate. It must repeat the affected install, proof, SIM, cleanup,
privacy, and review gates before release claims can use it.

## Failure rules

Diagnostic evidence is fail closed. If the Codex event stream is malformed or only partly
observable, every parse-derived tool, call, and count field is unknown. This includes call-absence
booleans, model/tool/MCP/Saxo event counts, invoked logical tool identities and counts, runner
aggregates, assistant-message presence, hashes, and assertion vectors; zero, false, empty, and
negative values require a completely decoded observable surface. The authenticated per-case
failure summary and outer publication enforce the same observability state. Timeout, post-spawn
operating-system failure, and ordinary terminal cleanup use captured PID birth identities. A
process may receive TERM or KILL only while its current birth identity matches; historical process
groups are never raw signal targets. One terminal semantic snapshot supplies authenticated target
outcomes and remaining process and group counts. Identity reuse, absence, and non-executing zombies
are zero candidate survivors; incomplete or unknown observation coverage remains unknown and fails
closed. Verifiers use those authenticated semantic outcomes rather than a second raw liveness
query. Cleanup runs exactly once and persists its evidence before a terminal failure is raised. The
authenticated cleanup-evidence status is one of
`authenticated`, `no-target-observed`, `observation-unknown`, or `write-failed`. A receipt digest is
present for `authenticated` completion evidence and for the separate owner-only
`observation-unknown` diagnostic receipt. The unknown receipt binds a fixed reason code,
watcher-drain state, coverage state, target count, and null remaining process and group counts.
`no-target-observed` and `write-failed` carry no receipt digest. Strict child-failure,
candidate-runner, and outer-publication schemas bind and verify the status, digest, and reason
relationship. Missing, inconsistent, unavailable, or tampered cleanup evidence cannot prove
absence or successful cleanup and cannot promote event or broker-safety negatives.

Process-table coverage is explicit and strict at the cleanup-scope boundary. Both root-bound
admission snapshots and every watcher observation use checked snapshots, and any incomplete or
failed observation is accumulated under the watcher lock as sticky unknown coverage. Terminal
cleanup still operates only on previously admitted birth-bound identities, then emits null
remaining process and group counts with a typed unknown receipt. Nested evaluation preserves those
nulls through report aggregation; it never defaults `None` or an empty set of terminal snapshots to
zero or passed cleanup.

Nested model execution marks cleanup pending and counts the root process immediately after a
successful spawn, before fallible process-group or scope observation. Any later timeout, uncertain
or residual cleanup, output parse failure, malformed output, or post-spawn operating-system error
has unknown transcript grading, assistant evidence, invoked tools, and model/command/MCP/Saxo event
counts. The authenticated per-case summary, child failure, candidate runner, and outer publication
must preserve those nulls; no post-launch unobservable result may be represented as an observable
empty trace.

The native path fails closed for an absent or unsafe Codex auth file, missing or expired SIM
material, a dirty or mismatched candidate, an inexact installed cache, a non-Codex evaluation
record, a skipped model call, an absent or unknown MCP event identity, incomplete tool coverage,
failed proof measurement, Saxo mismatch, cleanup residue, state change, privacy finding, LIVE
event, broker write, purchase, or disclaimer response. Each native execution prompt also binds the
exact case ID; prose that merely names a tool cannot satisfy tool evidence. The native `scenario`
fixture contract explicitly requires the exact ordered final-receipt text
`explicit numeric shocks -0.10 0.05`; its existing transcript assertion remains unchanged.

No browser is opened. No LIVE endpoint is allowed. Missing Saxo inputs never receive substitute
market or account data.

## Test plan

Tests first prove that the current code fails because it requires Claude auth and accepts only a
dual report. New tests then require:

- the exact `codex_native_v1` policy;
- Codex-only runtime preparation with no Claude files or environment entries;
- exact marketplace registration in each disposable `CODEX_HOME`;
- a no-model preflight that proves the plugin enabled and exact filtered `list_tools` visibility;
- fail-closed parsing of missing or unknown Codex MCP event identities using captured Codex 0.147
  JSON shapes;
- rejection of non-Codex or no-model evaluation records;
- a trusted Codex-only installed cache with exact bytes and 60 tools;
- unchanged numerical, reconciliation, cleanup, privacy, and safety gates;
- refusal without activation when SIM authentication is unavailable.

Focused tests run twice through `scripts/run-pytest`. Source changes also require Ruff and
BasedPyright. One stable candidate receives the guarded full suite and remaining expensive gates.
