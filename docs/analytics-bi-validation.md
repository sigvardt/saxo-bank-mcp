# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Latest sealed evidence candidate `bd26296dc0a2749240e4771bef6a4171a5dacf72`, tree
`7d6d458562d1ed587411806c0c562ae10a566299`, passed its exact Codex-only install,
signed 2,751-test full suite, and required local gates. Its one sealed proof stopped during native
registration preflight before model work. All 11 cases reported
`codex_native_registration_invalid`. No retry ran.

Local corrective candidate `1277f488f32a5c03041812a207fa5f3e5791631e`, tree
`e2e267f2c08ec1f840198c5446b927a8a58350e5`, fixes the confirmed disposable plugin-path defect and
retains strict privacy-safe registration failure evidence. It has passed local tests and static
checks only. It has not had an exact install, signed full suite, SIM preflight, or sealed proof.
All 54 proof profiles remain quarantined.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, transcripts, model arguments, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Latest sealed source | `bd26296dc0a2749240e4771bef6a4171a5dacf72` |
| Latest sealed tree | `7d6d458562d1ed587411806c0c562ae10a566299` |
| Local corrective source | `1277f488f32a5c03041812a207fa5f3e5791631e` |
| Local corrective tree | `e2e267f2c08ec1f840198c5446b927a8a58350e5` |
| Harness policy | `codex_native_v1` |
| Installed surface | 60 tools, including 21 analytics tools; 9 skills; 1 MCP server |
| Saxo operation catalog | 294 operations: 182 implemented and 112 refused |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The historical `dual_v1` policy and its evidence remain unchanged. The native policy uses only
the Codex harness. Shared package inventory metadata does not constitute another client, account,
process, or evaluation.

## Exact sealed-candidate gates

The retained-runtime installation for `bd26296` passed with 60 tools, 9 skills, 1 MCP server, 614
exact compared files, no inventory mismatch, unchanged caller state, owner-only evidence, and zero
remaining install processes or process groups.

The signed exact-candidate full-suite receipt reports:

- 2,751 tests passed;
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

Exactly one sealed `codex_native_v1` command ran for `bd26296` and exited 1. Its authenticated
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
| Model events | 0 |
| MCP and Saxo event counts | unknown |
| Broker write, LIVE mutation, purchase, disclaimer response | unknown |
| Evaluation cases | 11 failed of 11 |
| Per-case error | `codex_native_registration_invalid` |
| Required logical tools invoked | 0 in every case |

The producer stopped before model execution, offline numerical proof, the fresh 60-tool SIM
matrix, Saxo reconciliation, account equality, or activation. The 54 analysis kinds and 54
expected receipt IDs in the publication bind the contract only. They are not completed numerical
receipts.

No false zero, no-write, unchanged-account, no-purchase, or no-disclaimer claim is made for facts
that the child did not prove. The separate SIM preflight proves only its own read-only request.

## Local corrective candidate

The defect was in disposable plugin-path selection. The runner could register a retained plugin
inside the isolated Codex home, then pass the retained cache path outside that home to preflight
when the retained cache was not under the authentication home. Preflight refused that path before
running `codex plugin list`.

Candidate `1277f48` now derives the disposable path from the retained marketplace, plugin, and
version manifests. Registration preflight matches the exact plugin name, version, marketplace,
plugin ID, installed state, and enabled state. It reports separate safe reasons for binding,
plugin-list command exit, JSON or schema failure, identity cardinality, installed state, and
enabled state.

Failed case receipts retain only the plugin-list exit code and a digest of the output schema.
They do not retain raw output, values, paths, arguments, broker data, or transcripts. Missing or
malformed evidence still remains unknown.

Local validation for `1277f48` reports:

- 63 focused tests passed twice;
- 234 related tests passed;
- 275 privacy and redaction tests passed;
- Ruff passed and BasedPyright reported 0 errors, 0 warnings, and 0 notes;
- plugin, static, catalog, and 34-case evaluation-manifest checks passed; and
- a seven-path bounded scan reported 0 findings and 0 scan errors.

No model, Saxo API, browser, or broker activity ran in this corrective batch. A separate Notion
documentation update changed no repository, proof, or broker state. The candidate
must pass a new exact install, signed full suite, required readbacks, SIM safety preflight, and one
separately authorized sealed proof before any downstream gate can resume.

## Cleanup and activation

The sealed attempt's runtime-consumption and cleanup receipts prove the bound proof runtime was
removed. No fresh 60-tool SIM matrix, controlled lifecycle, numerical proof, Saxo reconciliation,
artifact parity proof, or attempt-bound account equality receipt exists for the latest sealed
attempt.

No proof profile is active. The branch remains local and unpushed.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.
