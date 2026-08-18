# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Candidate `6d66a3e4f679b814923ed9a31b4e721083248846` passed its focused, related,
full-suite, static, privacy, and exact Codex-only installation checks. Its one sealed proof attempt
stopped with child exit 1. Unlike the preceding candidate, this attempt retained an authenticated
stdlib-bootstrap envelope with a safe startup phase and reason. The installed producer did not emit
its phase envelope, so all unproved child activity and safety facts remain unknown. No retry ran.
All 54 proof profiles remain quarantined.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Source candidate | `6d66a3e4f679b814923ed9a31b4e721083248846` |
| Candidate tree | `883aef881db9e4ef634b31a1f7a5103f76a709eb` |
| Base candidate | `b9c097b9eb232f4cd298de75bab45d9207a33ac6` |
| Harness policy | `codex_native_v1` |
| Dependency lock fingerprint | `eb4584bf6f712062fac281b6ff1d2ba8205d91f026e115abae7fae201baa3482` |
| Project version | `0.1.0` |
| Tool catalog | 60 tools, including 21 analytics tools |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The candidate starts the stdlib-only bootstrap directly with an absolute Python interpreter and
isolated flags. The bootstrap durably writes its owner-only entry receipt before invoking the
absolute offline installed-project launcher. Missing or broken launchers therefore cannot bypass
entry evidence. Raw stderr and untyped stdout are not published. The historical `dual_v1` command,
types, prompts, and evidence remain unchanged.

## Current candidate checks

All pytest runs used `scripts/run-pytest` with `TMPDIR`, `TMP`, and `TEMP` set to the required
external temp root.

| Gate | Result for `6d66a3e` |
| --- | --- |
| Direct-bootstrap and failure-envelope tests | 40 passed twice after final source changes |
| Related proof, install, runtime, and policy tests | 122 passed |
| Related auth, session, and privacy tests | 243 passed |
| Guarded exact-candidate full suite | 2,700 passed; 0 failures, errors, or skips; exit 0 |
| Ruff and Ruff format | passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Plugin validator | passed |
| Nine-skill static gate | 9 skills, 0 findings, 0 errors |
| Generated catalogs | 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Evaluation manifest | 34 cases, 60 tools, 9 skills, 0 errors |
| Bounded changed-file scan | 0 findings, 0 scan errors |
| System disk guard | 92 GiB free after the sealed attempt; minimum is 50 GiB |

The retained full-suite receipt binds the candidate commit and tree, external temp root, exit,
2,700-test count, zero failure/error/skip counts, JUnit digest, command-output digests, clean
worktree before and after, and its own digest. The owner-only JUnit and receipt are mode 0600. Raw
suite output was not retained.

## Exact Codex-only installation

The one clean installation and its independent readback are bound to `6d66a3e` and passed.

| Check | Result |
| --- | --- |
| Execution mode | `codex_installed_verification` |
| Installed surface | 60 tools, 9 skills, 1 MCP server |
| Startup readback | 60 tools from source, installed cache, and registered listing |
| Byte inventory | 611 files compared, exact match, no mismatches |
| Caller Codex state | unchanged |
| Owner-only storage | passed |
| Installation privacy scan | passed |
| Process cleanup | complete, 0 remaining process IDs and process groups |
| Report errors | 0 |

The install uses no second-client state, command, account, process, or evaluation receipt. Shared
packaging metadata in the byte inventory does not prove or start another client.

## SIM preflight before the sealed command

The network-free status receipt proved requested and effective environment `SIM`, LIVE reads and
writes disabled, a fresh readable refresh-capable SIM cache, and no auth blockers. One required
session-capability call then passed with `network_call_made=true` and
`session_capabilities_proven=true`. It was an auth/session read, not a trade or broker mutation.

This external authorization gate does not substitute for the child-local preflight receipt. The
failed child did not emit that receipt, so its own preflight and network state remain unknown.

## One sealed proof attempt

Exactly one Codex-native proof command ran against the verified install and exited 1. The strict
outer publication passed round-trip and digest verification and binds all 54 analysis kinds and 54
evidence receipt IDs. Its authenticated result contains:

| Field | Value |
| --- | --- |
| `result_kind` | `verified_child_failure` |
| outer `status` | `refused` |
| outer `reason` | `proof_child_failure_envelope_missing` |
| producer failure evidence | missing; producer not authenticated |
| bootstrap evidence | authenticated |
| bootstrap state | `failed` |
| completed bootstrap phases | entry, producer binding, producer handoff |
| current bootstrap phase | `producer_execution` |
| bootstrap reason | `proof_bootstrap_producer_nonzero` |
| child exit | `1` |
| child SIM preflight and network | `unknown` |
| model, MCP, and Saxo event counts | `unknown` |
| execution, write, mutation, purchase, and disclaimer facts | `unknown` |
| child cleanup | `unknown` |
| outer runtime cleanup | complete; 0 remaining processes and process groups |
| redacted publication | `true` |

The bootstrap proves that the fixed Python entry boundary ran, the exact producer bytes were bound,
and the installed-runtime handoff returned nonzero. It does not prove that producer import,
child-local SIM preflight, model work, offline proof, or the SIM matrix did or did not occur. The
missing producer envelope therefore remains unknown rather than false or zero.

No second sealed command or diagnostic producer run occurred. The publication privacy scan passed
with 0 findings and 0 scan errors.

## Safety and local state after the attempt

Post-run checks prove outer cleanup, zero remaining attempt processes/groups, a clean Git worktree,
owner-only evidence, and a still-fresh local SIM cache with LIVE reads and writes off. They do not
prove attempt-bound account equality or zero child Saxo/model/mutation/purchase/disclaimer activity.
No before-account fingerprint was available, so no after-only broker read ran.

No Claude command, model, evaluation, or delegation was invoked by this Task 24 execution.

## Broker proof and activation

No validated receipt was produced for the native hard workflow, 54 per-analysis numerical proofs,
Saxo reconciliation, the fresh 60-tool SIM matrix, controlled lifecycle cleanup, or account-state
equality.

| Gate | State |
| --- | --- |
| Real installed native hard workflow | not proved |
| Full suite on exact candidate | passed, 2,700 tests |
| 54 source-bound analysis receipts | 0 validated |
| Saxo reconciliation | not proved |
| Fresh 60-tool SIM matrix | not proved |
| Attempt-bound account equality | unavailable |
| Proof profiles activated | 0 of 54 |
| Separate native review | pending |

The historical controlled SIM run keeps its own matrix, cleanup, and unchanged-account evidence.
It is not evidence for `6d66a3e` and is not relabeled here.

## Privacy and next gate

Private reports remain owner-only in the ignored evidence area. Public documentation contains only
safe counts, commit digests, result labels, and evidence-backed safety state.

Do not rerun this candidate. The pre-launch startup gap is closed, but the installed producer still
exited before emitting its authenticated phase envelope. Diagnose that pre-tracker/import boundary
locally in a new candidate before requesting another sealed attempt. Activation remains forbidden
until all 54 receipts and every remaining Task 24 gate pass.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.
