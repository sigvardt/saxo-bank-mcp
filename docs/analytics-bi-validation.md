# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Candidate `04241a5f243541ed9f7b88899ebad014ed38c3ee` passed its focused, related,
full-suite, static, privacy, and exact Codex-only installation checks. Its one sealed proof attempt
stopped with child exit 1 before an authenticated child envelope was available. The new outer
receipt correctly publishes every unproved activity and safety fact as unknown. No retry ran. All
54 proof profiles remain quarantined.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Source candidate | `04241a5f243541ed9f7b88899ebad014ed38c3ee` |
| Candidate tree | `7961b6cd6aad82ffa750cae5f9095461d9182d63` |
| Base candidate | `d23d24fb8d6ed023aca5cb6a52828f9de67f3e80` |
| Harness policy | `codex_native_v1` |
| Dependency lock fingerprint | `eb4584bf6f712062fac281b6ff1d2ba8205d91f026e115abae7fae201baa3482` |
| Project version | `0.1.0` |
| Tool catalog | 60 tools, including 21 analytics tools |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The candidate adds a strict, candidate-bound native child-failure envelope and a fail-closed outer
receipt. Raw child output remains ephemeral. The historical `dual_v1` prompts, types, and evidence
remain unchanged.

## Current candidate checks

All pytest runs used `scripts/run-pytest` with `TMPDIR`, `TMP`, and `TEMP` set to the required
external temp root.

| Gate | Result for `04241a5` |
| --- | --- |
| Failure-envelope tests | 21 passed twice after final source changes |
| Related proof, install, evaluation, and command tests | 175 passed |
| Related auth, session, and privacy tests | 257 passed |
| Guarded full suite | 2,667 collected and passed; exit 0 |
| Ruff | passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Plugin validator | passed |
| Nine-skill static gate | 9 skills, 0 findings, 0 errors |
| Generated catalogs | 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Evaluation manifest | 34 cases, 60 tools, 9 skills, 0 errors |
| Bounded public changed-file scan | 0 findings, 0 scan errors |
| System disk guard | 95 GiB free after the sealed attempt; minimum is 50 GiB |

The exact candidate full suite and clean install each ran once. The full suite reached 100% and
exited 0. Collection readback counted 2,667 tests.

## Exact Codex-only installation

The one clean installation is bound to `04241a5` and passed.

| Check | Result |
| --- | --- |
| Execution mode | `codex_installed_verification` |
| Installed surface | 60 tools, 9 skills, 1 MCP server |
| Byte inventory | 602 files compared, exact match, no mismatches |
| Caller Codex state | unchanged |
| Owner-only storage | passed |
| Installation privacy scan | passed |
| Process cleanup | complete, 0 remaining process IDs and process groups |
| Report errors | 0 |

The install report uses no second-client state, command, account, process, or evaluation receipt.
Shared packaging metadata in the byte inventory does not prove or start another client.

## One sealed proof attempt

Exactly one Codex-native proof command ran against the verified install. It returned child exit 1
with an empty stdout digest and a nonempty stderr digest. The owner-only redacted receipt contains:

| Field | Value |
| --- | --- |
| `status` | `refused` |
| `reason` | `proof_child_failure_envelope_missing` |
| `failure_evidence_status` | `missing` |
| `producer_authenticated` | `false` |
| `child_exit_code` | `1` |
| `sim_preflight_status` | `unknown` |
| `network_call_made` | `unknown` |
| model, MCP, and Saxo event counts | `unknown` |
| execution, write, mutation, purchase, and disclaimer facts | `unknown` |
| child cleanup | `unknown` |
| outer runtime cleanup | complete; 0 remaining processes and process groups |
| `redacted_publication` | `true` |

No authenticated child envelope or retained child log exists. The outer receipt therefore does not
invent a phase, SIM result, event count, or negative safety claim. This is the required behavior for
a missing, malformed, tampered, or crashed child envelope. It does not prove that SIM preflight,
model work, offline proof, or the SIM matrix did or did not start.

No second sealed command or proof retry ran. Whether the failed first child reached model or Saxo
work is unknown.

## Safety and local state after the attempt

Post-run checks prove only the outer process boundary: cleanup completed, no proof child or process
group remains, the exact install is intact, and the Git worktree stayed clean. Because the child
envelope is missing, this candidate does not claim a current SIM capability result,
`network_call_made=false`, `live_events=0`, `live_mutation_calls=0`,
`purchase_occurred=false`, or `disclaimer_response_made=false` for this attempt. No after-only
account read ran because it could not establish attempt-bound equality.

## Broker proof and activation

No validated receipt was produced for the native hard workflow, 54 per-analysis numerical proofs,
Saxo reconciliation, the fresh 60-tool SIM matrix, controlled lifecycle cleanup, or account-state
equality. A fresh account read after failure would not prove equality because no attempt-bound
before fingerprint exists, so no extra broker read was made.

| Gate | State |
| --- | --- |
| Real installed native hard workflow | not proved |
| Full suite on exact candidate | passed, 2,667 tests |
| 54 source-bound analysis receipts | 0 validated |
| Saxo reconciliation | not proved |
| Fresh 60-tool SIM matrix | not proved |
| Attempt-bound account equality | unavailable |
| Proof profiles activated | 0 of 54 |
| Separate native review | pending |

The historical controlled SIM run keeps its own 60-tool, cleanup, and unchanged-account evidence.
It is not evidence for `04241a5` and is not relabeled here.

## Privacy and storage

Private reports remain owner-only in the ignored evidence area. The current public documents contain
only safe counts, commit digests, result labels, and safety booleans. No Claude command, model,
evaluation, or process ran in this task.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.

## Next gate

Do not rerun the proof from this candidate. The outer boundary now treats a missing envelope
correctly, but the child startup failure has no authenticated safe cause or phase. Diagnose that
startup boundary locally and create a new candidate before requesting any newly authorized sealed
attempt. Activation remains forbidden until all 54 receipts and every remaining Task 24 gate pass.
