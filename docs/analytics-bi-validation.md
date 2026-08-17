# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-17
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Candidate `d23d24fb8d6ed023aca5cb6a52828f9de67f3e80` passed its focused, related,
static, privacy, and exact Codex-only installation checks. Its one sealed proof attempt stopped with
`proof_producer_command_failed`. The public receipt lost the child phase and network provenance.
No retry ran. All 54 proof profiles remain quarantined.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Source candidate | `d23d24fb8d6ed023aca5cb6a52828f9de67f3e80` |
| Candidate tree | `62bc675a43bbd545a718a0f539face5f91143b79` |
| Base candidate | `9e958817d20b5467e2c4db99ef5eded64723a031` |
| Harness policy | `codex_native_v1` |
| Dependency lock fingerprint | `eb4584bf6f712062fac281b6ff1d2ba8205d91f026e115abae7fae201baa3482` |
| Project version | `0.1.0` |
| Tool catalog | 60 tools, including 21 analytics tools |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The candidate changes only the native evaluation path. It binds installed Codex to the contained
plugin copy and gives analytics hard cases schema-valid synthetic handles. The historical
`dual_v1` prompts, types, and evidence remain unchanged.

## Current candidate checks

All pytest runs used `scripts/run-pytest` with `TMPDIR`, `TMP`, and `TEMP` set to the required
external temp root.

| Gate | Result for `d23d24f` |
| --- | --- |
| Focused native tests | 2 passed twice after final formatting |
| Related evaluation, install, proof, and catalog tests | 207 passed |
| Public redaction and privacy tests | 222 passed |
| Ruff | passed |
| Ruff format | passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Plugin validator | passed |
| Nine-skill static gate | 9 skills, 0 findings, 0 errors |
| Generated catalogs | 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Evaluation manifest | 34 cases, 60 tools, 9 skills, 0 errors |
| Bounded changed-file scan | 0 findings, 0 scan errors |
| System disk guard | 96 GiB free after the sealed attempt; minimum is 50 GiB |

The guarded full suite was not rerun for `d23d24f`. The exact 2,645-test suite passed with zero
failures and zero skips at base candidate `9e95881`, before the native registration and fixture
binding correction. That older result is supporting history only and is not relabeled as an exact
`d23d24f` full-suite pass.

## Exact Codex-only installation

The one clean installation is bound to `d23d24f` and passed.

| Check | Result |
| --- | --- |
| Execution mode | `codex_installed_verification` |
| Installed surface | 60 tools, 9 skills, 1 MCP server |
| Byte inventory | 598 files compared, exact match, no mismatches |
| Caller Codex state | unchanged |
| Owner-only storage | passed |
| Installation privacy scan | passed |
| Process cleanup | complete, 0 remaining process IDs and process groups |
| Report errors | 0 |

The install report uses no second-client state, command, account, process, or evaluation receipt.
Shared packaging metadata in the byte inventory does not prove or start another client.

## One sealed proof attempt

Exactly one Codex-native proof command ran against the verified install. It returned exit code 1
after about two minutes with no command output. The owner-only public receipt contains:

| Field | Value |
| --- | --- |
| `status` | `refused` |
| `reason` | `proof_producer_command_failed` |
| `execution_performed` | `false` |
| `broker_write_made` | `false` |
| `live_mutation_calls` | `0` |
| `redacted_publication` | `true` |

The receipt does not contain `sim_preflight`, `network_call_made`, a child exit receipt, a child
error reason, model event counts, `live_events`, `purchase_occurred`, or
`disclaimer_response_made`. No retained child log exists. The temporary proof, model-evaluation,
and offline-suite directories were removed as designed.

This is a correctness defect in the proof boundary. The outer command converted an unknown child
failure into a generic refusal and dropped phase and provenance data. Its
`execution_performed=false` field cannot prove that the child performed no partial work. Therefore
this report does not claim that the SIM preflight passed, that a model tool call occurred, or that
the offline proof and SIM matrix started.

No second prompt, model run, Saxo proof, or proof retry ran.

## Safety and local state after the attempt

A local, network-free auth status read after the attempt passed and reported:

- requested environment `SIM`;
- effective read environment `SIM`;
- LIVE reads `false`;
- LIVE writes `false`;
- a present, readable, unexpired SIM cache with refresh support;
- no pending local login.

That local status explicitly does not verify the Saxo server session, account access, session
capabilities, or order readiness.

The owner-only 60-second session keeper remains loaded as a one-shot job. Its latest local result
was `SIM`, `fresh`, and `network_call_made=false`; its last exit code was 0 and its error log was
empty. No keeper schedule or runtime changed in this task.

Post-run cleanup checks found 0 proof or evaluation processes, 0 matching temporary directories,
and 0 open handles under the candidate evidence directory. The exact install remained intact and
the Git worktree remained clean.

The sealed command was configured for SIM with LIVE reads and writes disabled. It granted no
purchase or disclaimer-response path. The outer receipt records no broker write and 0 LIVE
mutations. Because the receipt omitted the remaining event fields, this candidate does not claim a
receipt-proven `live_events=0`, `purchase_occurred=false`, or
`disclaimer_response_made=false` for the failed child.

SIM needs no human approval.

## Broker proof and activation

No validated receipt was produced for the native hard workflow, 54 per-analysis numerical proofs,
Saxo reconciliation, the fresh 60-tool SIM matrix, controlled lifecycle cleanup, or account-state
equality. A fresh account read after failure would not prove equality because no attempt-bound
before fingerprint exists, so no extra broker read was made.

| Gate | State |
| --- | --- |
| Real installed native hard workflow | not proved |
| Full suite on exact candidate | not run |
| 54 source-bound analysis receipts | 0 validated |
| Saxo reconciliation | not proved |
| Fresh 60-tool SIM matrix | not proved |
| Attempt-bound account equality | unavailable |
| Proof profiles activated | 0 of 54 |
| Separate native review | pending |

The historical controlled SIM run keeps its own 60-tool, cleanup, and unchanged-account evidence.
It is not evidence for `d23d24f` and is not relabeled here.

## Privacy and storage

Private reports remain owner-only in the ignored evidence area. The current public documents contain
only safe counts, commit digests, result labels, and safety booleans. No Claude command, model,
evaluation, or process ran in this task.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.

## Next gate

Do not rerun the proof from this candidate. First correct the child-failure envelope so a failed run
retains the SIM preflight, truthful network provenance, exact safe phase, child exit, and partial
execution state. That correction creates a new candidate and requires TDD, focused checks twice,
related checks, Ruff, BasedPyright, privacy, exact installation, and one newly authorized sealed
attempt. Activation remains forbidden until all 54 receipts and every remaining Task 24 gate pass.
