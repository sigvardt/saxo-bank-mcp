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

## Proof execution

The verified Codex cache starts a sealed child process. The child copies only the minimum Codex
file-backed authentication and SIM files into a temporary owner-only runtime. It never prepares,
copies, promotes, or executes Claude state.

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

The native path fails closed for an absent or unsafe Codex auth file, missing or expired SIM
material, a dirty or mismatched candidate, an inexact installed cache, a non-Codex evaluation
record, a skipped model call, incomplete tool coverage, failed proof measurement, Saxo mismatch,
cleanup residue, state change, privacy finding, LIVE event, broker write, purchase, or disclaimer
response.

No browser is opened. No LIVE endpoint is allowed. Missing Saxo inputs never receive substitute
market or account data.

## Test plan

Tests first prove that the current code fails because it requires Claude auth and accepts only a
dual report. New tests then require:

- the exact `codex_native_v1` policy;
- Codex-only runtime preparation with no Claude files or environment entries;
- rejection of non-Codex or no-model evaluation records;
- a trusted Codex-only installed cache with exact bytes and 60 tools;
- unchanged numerical, reconciliation, cleanup, privacy, and safety gates;
- refusal without activation when SIM authentication is unavailable.

Focused tests run twice through `scripts/run-pytest`. Source changes also require Ruff and
BasedPyright. One stable candidate receives the guarded full suite and remaining expensive gates.
