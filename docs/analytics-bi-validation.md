# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Candidate `06fb8e1731af158825d4c83a6f01cadb489dec83`, tree
`db36b068962bdb266d5f7b49da4d0e9b2234b738`, passed its exact Codex-only retained-runtime
installation, signed 2,810-test full suite, required local gates, and current SIM safety preflight.
Its one sealed proof command then returned an authenticated `verified_child_failure`. Ten of eleven
native hard cases passed. The `scenario` case failed `transcript_assertion_failed` despite a passed
grant check and exact invocation of its two required logical tools. The outer failure reason is
`proof_child_cleanup_failed`.

No retry ran. The numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation, account-equality
readback, and activation gates did not run. All 54 proof profiles remain quarantined and 0 are
active.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, transcripts, model arguments, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Sealed source | `06fb8e1731af158825d4c83a6f01cadb489dec83` |
| Sealed tree | `db36b068962bdb266d5f7b49da4d0e9b2234b738` |
| Harness policy | `codex_native_v1` |
| Installed surface | 60 tools, including 21 analytics tools; 9 skills; 1 MCP server |
| Installed inventory | 616 exact compared files |
| Saxo operation catalog | 294 operations: 182 implemented and 112 refused |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The historical `dual_v1` policy and its evidence remain unchanged. The native policy uses only
the Codex harness. Shared package inventory metadata does not constitute another client, account,
process, or evaluation.

## Exact candidate gates

The retained-runtime installation passed with exact candidate and tree binding, 60 tools, 9 skills,
1 MCP server, 616 exact compared files, no inventory mismatch, unchanged caller state during the
install window, owner-only evidence, and complete install-process cleanup. Verify-only readback
passed before the retained runtime was consumed.

The signed exact-candidate full-suite receipt reports:

- 2,810 tests passed;
- zero failures, errors, or skips;
- the exact candidate and tree before and after;
- a clean source worktree before and after;
- owner-only JUnit and command receipts; and
- no retained raw test output.

All pytest execution used `scripts/run-pytest` with `TMPDIR`, `TMP`, and `TEMP` set to the required
external temporary root. Candidate-attributed temporary storage returned to its baseline, no
candidate-owned system-temporary pytest directory appeared, and the system disk stayed above the
50 GiB guard.

| Local gate | Result |
| --- | --- |
| Ruff check | passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Plugin validator | passed |
| Nine-skill static gate | 9 skills, 0 findings, 0 errors |
| Generated catalogs | 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Evaluation manifest | 34 cases, 60 tools, 9 skills, 0 errors |
| Public privacy scan | passed with 0 findings and 0 scan errors |
| Structural install readback | exact candidate, inventory, binding, modes, and unconsumed runtime verified |

## SIM authorization gate

Before the sealed command, local status proved requested and effective environment `SIM`, with
LIVE reads disabled and LIVE writes disabled. One separate read-only session-capability call passed
and truthfully recorded `network_call_made=true`.

The sealed producer then performed its own required SIM preflight before model work. The
authenticated child receipt records requested and effective `SIM`, LIVE reads false, LIVE writes
false, session capabilities passed, and `network_call_made=true`. These receipts prove only their
own read-only capability requests. They do not prove account equality or the later unknown broker
facts.

## One sealed proof attempt

Exactly one sealed `codex_native_v1` command ran for `06fb8e1` and exited 1. The authenticated outer
publication has the complete 54-analysis and 54-receipt contract counts and result kind
`verified_child_failure`.

| Field | Authenticated value |
| --- | --- |
| Status | refused |
| Reason | `proof_child_cleanup_failed` |
| Completed phase | SIM preflight |
| Current phase | agent evaluation |
| Child exit | 1 |
| Execution performed | true |
| SIM preflight | passed |
| Network call made | true |
| Hard-case summary | 10 passed, 1 failed |
| Failing case | `scenario`: `transcript_assertion_failed` |
| Failing-case grant | passed |
| Failing-case required tools | exact two logical tools invoked |
| Child cleanup status | not started |
| Outer retained-runtime cleanup | complete |
| Candidate-runner cleanup | complete |

The strict privacy-safe per-case summary contains only case identifiers, assertion and grant
outcomes, required and invoked logical tool identifiers and counts, safe event counts, and bound
digests. It contains no transcript, arguments, handles, broker payload, account data, raw output,
or private path.

The overall receipt deliberately leaves aggregate model, MCP, and Saxo event counts, broker-write
state, LIVE mutation count, purchase state, and disclaimer-response state unknown. The per-case
summary is not used to invent those aggregate or broker facts. The failure receipt recorded two
remaining child processes and zero process groups at capture. After the command terminated, an
independent local readback found no exact-candidate process, no proof run root, and no retained
proof runtime.

No second proof, diagnostic model run, numerical proof, fresh SIM matrix, account read, or
activation ran.

## Cleanup and activation

Authenticated consumption intent and cleanup receipts bind the exact candidate, tree, policy,
runtime binding, and durable removal of the one-shot proof runtime. The candidate-runner receipt
binds the authenticated child-result digest, child exit 1, and completed runner cleanup. Retained
receipts are owner-only. The candidate worktree remained clean and no exact-candidate process was
present at final local readback.

Those local cleanup facts do not prove broker account equality, no mutation, no purchase, or no
disclaimer response. No attempt-bound before/after account fingerprint exists, so account state is
reported as unproved rather than unchanged.

All 54 profiles remain quarantined and 0 are active. The branch remains local and unpushed. A new
candidate or proof attempt requires separate authorization after the independent review identifies
whether the remaining blocker is the scenario assertion, child cleanup evidence, or both.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.
