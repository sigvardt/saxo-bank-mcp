# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Latest sealed evidence candidate `119e93de3ed89bca6070d3fa5509f6c6680df658`, tree
`7ac20fdca8dcea0462d9f61d9cdfebe506970845`, passed its exact Codex-only install,
signed 2,779-test full suite, and required local gates. Its one sealed proof reached native model
evaluation: 3 of 11 cases passed, 7 failed `out_of_grant_tool`, and 1 failed
`transcript_assertion_failed`. No retry ran.

Local corrective candidate `fb2948325d6ebe9e2cd3d04607d62ce48cbf5e62`, tree
`e18b8cf048fff6378c702079cf3d437e56845643`, binds the exact per-case SIM-only MCP config to both
direct preflight and the contained Codex plugin, narrows native fixture prompts, and authenticates
allowlisted assertion outcomes. It has passed local tests and static checks only. It has not had
an exact install, signed full suite, SIM preflight, or sealed proof. All 54 proof profiles remain
quarantined.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, transcripts, model arguments, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Latest sealed source | `119e93de3ed89bca6070d3fa5509f6c6680df658` |
| Latest sealed tree | `7ac20fdca8dcea0462d9f61d9cdfebe506970845` |
| Local corrective source | `fb2948325d6ebe9e2cd3d04607d62ce48cbf5e62` |
| Local corrective tree | `e18b8cf048fff6378c702079cf3d437e56845643` |
| Harness policy | `codex_native_v1` |
| Installed surface | 60 tools, including 21 analytics tools; 9 skills; 1 MCP server |
| Saxo operation catalog | 294 operations: 182 implemented and 112 refused |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The historical `dual_v1` policy and its evidence remain unchanged. The native policy uses only
the Codex harness. Shared package inventory metadata does not constitute another client, account,
process, or evaluation.

## Exact sealed-candidate gates

The retained-runtime installation for `119e93d` passed with 60 tools, 9 skills, 1 MCP server, 615
exact compared files, no inventory mismatch, unchanged caller state, owner-only evidence, and zero
remaining install processes or process groups.

The signed exact-candidate full-suite receipt reports:

- 2,779 tests passed;
- zero failures, errors, or skips;
- the exact candidate and tree before and after;
- a clean source worktree before and after;
- owner-only JUnit and command receipts; and
- no retained raw test output.

All pytest execution used `scripts/run-pytest` with `TMPDIR`, `TMP`, and `TEMP` set to the required
external temporary root. The system disk stayed above the 50 GiB guard.

| Local gate | Result |
| --- | --- |
| Ruff check | passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Plugin validator | passed |
| Nine-skill static gate | 9 skills, 0 findings, 0 errors |
| Generated catalogs | 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Evaluation manifest | 34 cases, 60 tools, 9 skills, 0 errors |
| Public privacy scan | passed with 0 findings and 0 scan errors |
| Structural install readback | exact candidate, inventory, binding, modes, and retained runtime verified |

A separate whole-repository Ruff format observation found historical format drift. Those files
were not changed, and this is not reported as a passed format gate.

## SIM authorization gate

Before the sealed command, local status proved requested and effective environment `SIM`, with
LIVE reads disabled and LIVE writes disabled. One read-only session-capability call passed and
truthfully recorded `network_call_made=true`. The installed child repeated and authenticated its
own SIM preflight before registration checks.

These calls prove current SIM session capability only. They do not prove a broker write, account
equality, or any later proof phase.

## One sealed proof attempt

Exactly one sealed `codex_native_v1` command ran for `119e93d` and exited 1. Its authenticated
publication records:

| Field | Authenticated value |
| --- | --- |
| Result kind | `verified_child_failure` |
| Completed producer phases | `sim_preflight` |
| Current producer phase | `agent_evaluation` |
| Child exit | 1 |
| Reason | `installed_agent_evaluation_command_failed` |
| Child SIM preflight | passed |
| Network provenance | `network_call_made=true` |
| Execution performed | true, because the SIM preflight made a request |
| Model events | 11 |
| MCP events | 42 |
| Saxo event count | unknown at the outer receipt |
| Broker write, LIVE mutation, purchase, disclaimer response | unknown |
| Evaluation cases | 3 passed and 8 failed |
| Primary failed-case errors | 7 `out_of_grant_tool`; 1 `transcript_assertion_failed` |
| Direct per-case MCP preflight | complete with the exact granted surface |

The producer stopped after model evaluation and before offline numerical proof, the fresh 60-tool SIM
matrix, Saxo reconciliation, account equality, or activation. The 54 analysis kinds and 54
expected receipt IDs in the publication bind the contract only. They are not completed numerical
receipts.

No false zero, no-write, unchanged-account, no-purchase, or no-disclaimer claim is made for facts
that the child did not prove. The separate SIM preflight proves only its own read-only request.

## Local corrective candidate

The sealed failure showed that the direct `list_tools` preflight saw the intended per-case grant,
while the separately spawned contained plugin exposed additional analytics capability and deletion
tools to Codex. The model called those visible extras in seven cases. The research-to-precheck case
used its granted tool but omitted the required final receipt wording.

Candidate `fb29483` atomically installs one owner-only per-case contained-plugin `.mcp.json` with
requested and effective SIM, LIVE reads and writes disabled, and the exact logical grant filter.
The direct preflight and later Codex model launch use the same bound config and digests. The config
is checked before and after model execution and restored afterward; unexpected Saxo environment
keys or config tamper fail closed. A late failure preserves the already-proven model facts rather
than falsely claiming that model work did not start.

The native prompt now states that analytics capabilities are already current, the harness owns
cleanup, and ungranted capability or deletion calls are forbidden. Research-to-precheck requires a
final receipt containing an analysis ID, one allowlisted state, and the exact phrase
`stop before broker write`. Authenticated failed-case summaries add only message presence and
ordered assertion booleans plus MCP-config digests. They retain no assertion text, transcript,
arguments, handles, broker payloads, account data, stderr, or private paths.

Local validation for `fb29483` reports:

- 8 focused tests passed twice;
- 200 related evaluation and proof tests passed;
- 239 auth, credential, privacy, redaction, secret-scan, and token-cache tests passed;
- Ruff passed and BasedPyright reported 0 errors, 0 warnings, and 0 notes;
- plugin, static, catalog, and 34-case evaluation-manifest checks passed; and
- an 11-file bounded scan reported 0 findings and 0 scan errors.

No model, Saxo API, browser, or broker activity ran in this corrective batch. A separate Notion
documentation update changed no proof or broker state. The candidate
must pass a new exact install, signed full suite, required readbacks, SIM safety preflight, and one
separately authorized sealed proof before any downstream gate can resume.

## Cleanup and activation

The sealed attempt's runtime-consumption and cleanup receipts prove the bound proof runtime was
removed. No fresh 60-tool SIM matrix, controlled lifecycle, numerical proof, Saxo reconciliation,
artifact parity proof, or attempt-bound account equality receipt exists for the latest sealed
attempt.

No proof profile is active. The branch remains local and unpushed.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.
