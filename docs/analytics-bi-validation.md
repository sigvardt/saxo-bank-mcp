# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Candidate `5853302f598a436179e15e75f22fdd91144e5354`, tree
`a8bf6c1c489e63bb03c63149b271e7c08d9fffdb`, passed its exact Codex-only
installation, quiet-window install readback, guarded 2,739-test full suite, and the required
static, type, catalog, evaluation-manifest, and public-privacy gates. One sealed proof then exited
1 with an authenticated producer failure in `agent_evaluation`. All 11 hard cases failed because
none of their required logical tools were invoked. The failure occurred before offline numerical
proof, the fresh SIM matrix, Saxo reconciliation, and activation. No retry ran. All 54 proof
profiles remain quarantined.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, transcripts, model arguments, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Source candidate | `5853302f598a436179e15e75f22fdd91144e5354` |
| Candidate tree | `a8bf6c1c489e63bb03c63149b271e7c08d9fffdb` |
| Harness policy | `codex_native_v1` |
| Project version | `0.1.0` |
| Installed surface | 60 tools, including 21 analytics tools; 9 skills; 1 MCP server |
| Saxo operation catalog | 294 operations: 182 implemented and 112 refused |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The historical `dual_v1` path and its evidence remain unchanged. The current native policy uses
only the Codex harness. Shared package-inventory metadata does not constitute another client,
account, process, or evaluation.

## Exact installation and full suite

The single retained-runtime installation passed with 611 exact compared files, no inventory
mismatch, unchanged caller state during the install window, owner-only evidence, and zero residual
install processes or process groups. Its proof interpreter was bound to the exact candidate, tree,
installed inventory, dependency lock, producer module, interpreter identity, and install receipt.

The first post-install verify-only attempt correctly refused because the caller plugin-cache digest
changed during its verification window. Read-only inspection found a bulk caller-cache metadata
refresh outside the finished installer. That failed receipt was preserved. After two stable
read-only samples, the separately authorized quiet-window verify-only attempt passed against the
same unchanged install. No second installation ran.

The signed exact-candidate full-suite receipt reports:

- 2,739 collected, executed, and passed tests;
- zero failures, errors, or skips;
- exact candidate and tree before and after;
- clean exact source worktree before and after;
- owner-only JUnit and command receipt; and
- external test temporary storage with the system disk always above the 50 GiB guard.

All pytest execution used `scripts/run-pytest` with `TMPDIR`, `TMP`, and `TEMP` set to the required
external temporary root.

## Post-suite local gates

| Gate | Result |
| --- | --- |
| Ruff check | passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Plugin validator | passed |
| Nine-skill static gate | 9 skills, 0 findings, 0 errors |
| Generated catalogs | 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Evaluation manifest | 34 cases, 60 tools, 9 skills, 0 errors |
| Public privacy scan | 0 findings and 0 scan errors across 6 public paths |
| Post-suite structural install readback | exact candidate, 611-file inventory, binding, modes, and retained runtime verified |
| System disk guard | approximately 82 GiB free after the sealed attempt; minimum is 50 GiB |

A separate whole-repository `ruff format --check .` observation reported 95 historical files that
would be reformatted. Those files were not changed. This observation is not reported as a passed
format gate and did not alter the exact candidate.

## SIM authorization gate

Before the sealed command, local status proved requested and effective environment `SIM`, LIVE
reads disabled, LIVE writes disabled, and a fresh readable refresh-capable owner cache. One
read-only session-capability call then passed with `network_call_made=true`, no login fallback, no
token refresh, and no order or subscription creation.

The installed child repeated and authenticated its own SIM preflight before model work. Its typed
failure receipt proves that child preflight passed and that a network request was made. Neither
receipt authorizes or proves a broker write.

## One sealed proof attempt

Exactly one sealed `codex_native_v1` command ran against the retained install and exited 1. Its
strict outer publication passed schema, binding, and digest verification and retained an
authenticated bootstrap envelope, producer failure envelope, and privacy-safe per-case summary.

| Field | Authenticated value |
| --- | --- |
| Result kind | `verified_child_failure` |
| Completed producer phases | `sim_preflight` |
| Current producer phase | `agent_evaluation` |
| Child exit | 1 |
| Reason | `installed_agent_evaluation_command_failed` |
| Child SIM preflight | passed |
| Network provenance | `network_call_made=true` |
| Execution performed | true |
| Model events | 11 |
| MCP events | 17 |
| Saxo event count | unknown |
| Broker write, LIVE mutation, purchase, disclaimer response | unknown |
| Evaluation cases | 11 failed of 11 |
| Required logical tools invoked | 0 in every case |
| Outer cleanup | complete; 0 remaining processes and 0 remaining process groups |

The 11 failed case IDs were `artifact-delivery`, `backtest-limitations`, `cost-xray`, `deletion`,
`market-comparison`, `optimization`, `options`, `portfolio-briefing`, `research-to-precheck`,
`scenario`, and `codex-native-safety-boundary`. Every case reported
`required_tool_missing`. Grants passed for all cases. The assertion gate passed for nine cases and
failed for `research-to-precheck` and `scenario`. No case invoked a required logical tool.

The publication's counts of 54 analysis kinds and 54 expected evidence receipt IDs bind the proof
contract; they are not completed numerical receipts. The producer stopped before offline proof, so
the count of validated per-analysis numerical receipts is zero.

Saxo activity and all broker safety outcomes remain unknown where the authenticated child receipt
does not prove them. No false zero, unchanged-account, no-purchase, no-disclaimer, or no-write claim
is made for the sealed child. The separate preflight proves only its own read-only SIM call.

## Cleanup, broker proof, and activation

The one-shot runtime consumption intent and cleanup receipt authenticate that the bound proof
runtime was removed durably after the attempt. The proof temporary directory is absent, no exact
candidate process remains, and the source worktree is clean. These local facts do not establish
broker account equality.

No attempt-bound before/after account fingerprint exists because the proof stopped during agent
evaluation. No fresh 60-tool SIM matrix, controlled lifecycle, numerical proof, Saxo reconciliation,
artifact parity proof, or account-state equality receipt ran for this candidate.

| Gate | State |
| --- | --- |
| Real installed native hard workflow | failed: 11/11 required-tool invocation checks |
| Exact-candidate full suite | passed: 2,739 tests |
| Validated numerical receipts | 0 of 54 |
| Saxo reconciliation | not run |
| Fresh 60-tool SIM matrix | not run |
| Controlled SIM lifecycle | not run |
| Attempt-bound account equality | unavailable |
| Proof profiles activated | 0 of 54 |
| Separate native review | pending |

The historical controlled SIM matrix keeps its own candidate binding and is not relabeled as
evidence for `5853302`. No second sealed command, diagnostic model run, after-only account read, or
activation occurred. The branch remains local and unpushed.

The next candidate must correct the native hard-evaluation tool-invocation failure with local TDD
and pass all exact-candidate gates before any separately authorized sealed attempt. Activation
remains forbidden until all 54 numerical receipts and every Task 24 gate pass.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.
