# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Candidate `f24fddfeef2fff73af880e85db2b8a4a64d08d2a` passed focused, related,
full-suite, static, privacy, and exact Codex-only installation checks. One sealed proof command
then exited 1 and published an authenticated outer boundary refusal. No authenticated producer or
bootstrap failure evidence survived, so every child phase, network, model, MCP, Saxo, execution,
write, purchase, disclaimer, and cleanup fact remains unknown. No retry ran. All 54 proof profiles
remain quarantined.

Commit `e217ae9c1ea12467d14c0b85c71831e761e4ca24`, tree
`23463a1bc236dcc03c6381eacb61b97c6e191e1a`, is the locally verified corrective source
candidate. It preserves authenticated phase, reason, command, and cleanup discriminators at the
native boundary and records a two-phase owner-only runtime-consumption receipt before deleting the
bound runtime. It has not yet received an exact retained-runtime install, full-suite receipt, SIM
preflight, or sealed proof. The `f24fddf` evidence below remains the latest sealed evidence and is
not relabeled.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Source candidate | `f24fddfeef2fff73af880e85db2b8a4a64d08d2a` |
| Candidate tree | `5ba8b0e840e524b00c5028362b7d02155587cdbe` |
| Harness policy | `codex_native_v1` |
| Dependency lock fingerprint | `eb4584bf6f712062fac281b6ff1d2ba8205d91f026e115abae7fae201baa3482` |
| Project version | `0.1.0` |
| Tool catalog | 60 tools, including 21 analytics tools |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The candidate retains one owner-only, install-probed Python runtime through the sealed proof
boundary. Both the install probe and the sealed child use Python's explicit no-bytecode flag. This
prevents imports from changing the exact installed source inventory. The stdlib bootstrap still
starts directly before any project launcher. The historical `dual_v1` path remains unchanged.

## Current candidate checks

All pytest runs used `scripts/run-pytest` with `TMPDIR`, `TMP`, and `TEMP` set to the required
external temp root.

| Gate | Result for `f24fddf` |
| --- | --- |
| Bytecode-write regressions | 2 expected RED failures, then 2 passed |
| Retained-runtime focused suite | 56 passed twice after final source changes |
| Related proof, install, and evaluation tests | 245 passed |
| Related auth, session, and privacy tests | 279 passed |
| Guarded exact-candidate full suite | 2,713 passed; 0 failures, errors, or skips; exit 0 |
| Ruff and Ruff format | passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Plugin validator | passed |
| Nine-skill static gate | 9 skills, 0 findings, 0 errors |
| Generated catalogs | 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Evaluation manifest | 34 cases, 60 tools, 9 skills, 0 errors |
| Bounded changed-file scan | 0 findings, 0 scan errors |
| System disk guard | 90.7 GiB free after the sealed attempt; minimum is 50 GiB |

The retained full-suite receipt binds the exact candidate and tree, approved external temp root,
exit, 2,713-test count, zero failure/error/skip counts, JUnit digest, clean worktree before and
after, and its own digest. The JUnit and receipt are owner-only mode 0600. Raw suite output was not
retained. External temp returned to its pre-suite baseline after completion.

## Local correction after the current evidence

The `e217ae9` correction began with failing real-boundary and durability regressions. One added
regression proved the pre-delete intent needed authenticated readback before runtime deletion.
After the correction:

- 15 focused boundary and runtime-consumption tests passed twice;
- 383 related proof, install, auth, session, and privacy tests passed;
- Ruff and Ruff format passed;
- BasedPyright reported 0 errors, 0 warnings, and 0 notes;
- plugin, nine-skill static, catalog, and evaluation-manifest checks passed;
- the bounded seven-file privacy scan reported 0 findings and 0 scan errors.

These are local source gates only. No model, Saxo, browser, or network activity occurred during the
corrective batch. They do not replace the exact install, signed full suite, SIM preflight, or sealed
proof required for a new evidence candidate.

## Exact Codex-only installation

The one clean retained-runtime installation and its built-in independent readback passed before the
sealed command.

| Check | Result |
| --- | --- |
| Execution mode | `codex_installed_verification` |
| Installed surface | 60 tools, 9 skills, 1 MCP server |
| Startup readback | 60 tools from source, installed cache, and registered listing |
| Byte inventory | 611 files compared, exact match, no mismatches |
| Source and cache bytecode directories | 0 |
| Caller Codex state | unchanged |
| Owner-only storage | report and binding mode 0600; retained runtime mode 0700 |
| Installation privacy scan | passed |
| Install process cleanup | complete, 0 remaining process IDs and process groups |
| Report errors | 0 |

The installed report contains no second-client state, command, account, process, or evaluation
receipt. Shared packaging metadata in the byte inventory does not prove or start another client.
The retained proof runtime was removed after the sealed command, as required.

## SIM preflight before the sealed command

The network-free status receipt proved requested and effective environment `SIM`, LIVE reads and
writes disabled, and a fresh readable refresh-capable SIM cache. One read-only session-capability
call then passed with `network_call_made=true` and `session_capabilities_proven=true`.

This external authorization gate does not substitute for child-local proof. The sealed result has
no authenticated child preflight receipt, so the child's own session and network state remain
unknown.

## One sealed proof attempt

Exactly one Codex-native proof command ran against the verified retained install and exited 1. The
strict outer publication passed digest and schema readback and binds all 54 analysis kinds and 54
evidence receipt IDs.

| Field | Value |
| --- | --- |
| `result_kind` | `boundary_failure` |
| outer `status` | `refused` |
| outer `reason` | `proof_producer_native_boundary_failed` |
| producer evidence | missing; producer not authenticated |
| bootstrap evidence | missing |
| child exit and phase | `unknown` |
| child SIM preflight and network | `unknown` |
| model, MCP, and Saxo event counts | `unknown` |
| execution, write, mutation, purchase, and disclaimer facts | `unknown` |
| child and outer runtime cleanup fields | `unknown` |
| redacted publication | `true` |

The receipt does not claim false zero values. A separate post-exit local process check found zero
candidate processes, zero native temp-runtime residues, and the retained proof runtime removed.
Those checks prove local cleanup after exit. They do not prove child execution or broker account
state.

No second sealed command, diagnostic producer run, or after-only account read occurred. The
publication privacy scan passed with 0 findings and 0 scan errors.

## Broker proof and activation

No validated receipt was produced for the native hard workflow, 54 per-analysis numerical proofs,
Saxo reconciliation, the fresh 60-tool SIM matrix, controlled lifecycle cleanup, or account-state
equality.

| Gate | State |
| --- | --- |
| Real installed native hard workflow | not proved |
| Full suite on exact candidate | passed, 2,713 tests |
| 54 source-bound analysis receipts | 0 validated |
| Saxo reconciliation | not proved |
| Fresh 60-tool SIM matrix | not proved |
| Attempt-bound account equality | unavailable |
| Proof profiles activated | 0 of 54 |
| Separate native review | pending |

The historical controlled SIM run keeps its own matrix, cleanup, and unchanged-account evidence.
It is not evidence for `f24fddf` and is not relabeled here.

## Privacy and next gate

Private receipts remain owner-only in the ignored evidence area. Public documentation contains only
safe counts, commit digests, result labels, and evidence-backed state.

Do not rerun `f24fddf`. The next gate is one exact retained-runtime installation and one signed full
suite for `e217ae9`, followed by static and privacy readback. Only if those gates pass may a newly
authorized sealed proof run. Activation remains forbidden until all 54 receipts and every remaining
Task 24 gate pass.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.
