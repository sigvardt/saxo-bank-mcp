# Saxo Analytics and BI Suite - Final Candidate Validation

Status: Task 24 partially validated; analytics activation is not approved
Date: 2026-08-16
Scope: Task 24 of `docs/superpowers/plans/2026-07-30-saxo-analytics-bi-suite.md`
Result: **60-tool SIM execution is complete and safe, but the per-analysis proof and activation
gates are not met**
Independent review: **pending separate native Codex review**

All public values below are redacted. This document contains no credentials, account identifiers,
balances, holdings, money values, local private paths, raw broker payloads, or raw private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Frozen runtime candidate | `97472e4720b44f31a623e0da6d96f18370186fca` |
| Candidate tree | `2d1834543223f1ef1ea4e7f3d83f0a5f4e255097` |
| Dependency lock fingerprint | `uv.lock` SHA-256 `eb4584bf6f712062fac281b6ff1d2ba8205d91f026e115abae7fae201baa3482` |
| Plugin/project version | `0.1.0` |
| Tool catalog | 60 tools, including 21 analytics tools |
| Proof catalog | 54 analysis kinds, all quarantined as `implementation_pending` |

The later Claude-launcher experiment was stopped by the owner. Corrective commit `134771d` removes
that experiment and has the exact same Git tree as the frozen candidate. It therefore restores the
already installed and tested candidate bytes rather than creating a new analytics candidate.

The controlled SIM matrix used the installed `ea1f8f3` fixture. Its `src`, `tests`,
`pyproject.toml`, and `uv.lock` objects are byte-identical to `97472e4`; only this validation document
changed between those commits. A separate exact `97472e4` installation report also passed. No
runtime or analytics implementation difference is being hidden by that evidence binding.

## Headline safety result

| Claim | Value |
| --- | --- |
| Effective broker environment | SIM |
| `live_events` | 0 |
| `live_mutation_calls` | 0 |
| `purchase_occurred` | false |
| `disclaimer_response_made` | false |
| LIVE endpoint called | no |
| LIVE write performed | no |
| Visible browser automated by this work | no |
| Keychain used by this work | no |
| Account state after controlled activity | unchanged |
| Controlled resources after cleanup | 0 |

SIM needs no human approval.

## Deterministic and static validation

The frozen candidate completed the repository CI workflow successfully. The GitHub run for
`97472e4` collected 2,599 tests: 2,592 passed and 7 sealed-runtime or platform-specific tests were
skipped with their recorded-runtime guards. Ruff, BasedPyright, plugin validation, static gates,
catalog checks, eval-manifest checks, public secret scanning, and LIVE refusal probes passed in that
workflow.

Before the final candidate, the recording host ran 2,596 tests with no failures for `b2b9872`; the
only later source change was the install-evidence path-redaction correction in `ea1f8f3` and its
three focused tests. No analytics formula, schema, tool behavior, artifact renderer, or proof
contract changed after that run.

The native-only resume removed the stopped launcher experiment and reran the directly affected
eval-runtime suite twice through `scripts/run-pytest`: 22 of 22 tests passed in each run. Fresh Ruff
and BasedPyright checks then passed with 0 errors, 0 warnings, and 0 notes. The system disk had 85 GiB
free, above the 50 GiB guard. Direct `pytest` was not used.

The historical `claude plugin validate` and dual-client installation were completed before the
owner stopped all Claude usage. They were not rerun and are not presented as a new native-only pass.

## Isolated installation

The preserved exact-candidate installation report for `97472e4` is passed and contains no errors.

| Gate | Result |
| --- | --- |
| Codex installed tools and skills | 60 tools, 9 skills, 1 MCP server |
| Historical second-client installation | 60 tools, 9 skills, 1 MCP server |
| Installed byte comparison | 1,166 files compared, exact inventory, no mismatches |
| Required files and annotations | present |
| Update and restore probe | passed |
| Client global state | unchanged |
| Process cleanup | complete, 0 remaining processes and process groups |
| Evidence privacy | clean, 0 findings, 0 scan errors |

This installation evidence predates the native-only instruction. No Claude command or process was
started during the 2026-08-16 native resume.

## 60-tool SIM matrix

The controlled matrix did execute against a fresh owner-authenticated SIM session. It did not pass
the full completion gate because the checked-in proof catalog deliberately refused analytical
claims.

| Item | Value |
| --- | --- |
| Environment | SIM |
| Matrix status | blocked |
| Tool receipts | 60 of 60 |
| Analytics tool receipts | 21 of 21 |
| Analysis execution receipts | 54 |
| Controlled lifecycle calls | 22 |
| Registered trading-write operations covered | 38 |
| Account allowlist resolved | yes |
| Fixture references validated | yes |
| Session capability read | passed |
| Account state unchanged | true |
| Cleanup complete | true |
| Uncleaned resources | 0 |
| `live_events` / `live_mutation_calls` | 0 / 0 |
| `purchase_occurred` | false |
| Disclaimer refusal observed | yes |
| Disclaimer response made | false |

The session reported authenticated, standard-data, orders-only SIM capability. The overall matrix
status remained blocked because these eight analytics or analysis-dependent tools returned
well-formed structured refusals while all proof profiles were quarantined:

1. `saxo_analyze_market`
2. `saxo_analyze_instruments`
3. `saxo_backtest_strategy`
4. `saxo_propose_trade_from_analysis`
5. `saxo_render_analysis`
6. `saxo_export_analysis`
7. `saxo_explain_analysis`
8. `saxo_manage_analysis_job`

The receipt also carries `controlled_sim_lifecycle_unverified` as an overall blocking error. This is
not reported as a pass. Separately, the same receipt proves `cleanup_complete=true`,
`account_state_unchanged=true`, and `uncleaned_resources=0`.

## Per-analysis proof and activation

| Item | Value |
| --- | --- |
| Production analysis kinds | 54 |
| Proof contracts and receipts defined | 54 |
| Proof contract digest | `d1052988772301f8e3b6bbd0a3fd0e9fddc793610b6c1cab3b35286904a3f0af` |
| Executed per-analysis proof | no |
| Reported refusal | `proof_sim_auth_lease_unavailable` |
| Actual inner blocker | missing file-backed Claude CLI auth in a producer that hardcodes both harnesses |
| Codex file-backed auth | present and owner-only |
| Broker write made by refused proof | false |
| LIVE mutation calls | 0 |
| Profiles activated | 0 of 54 |

The outer refusal label is misleading: the fresh SIM session was available. The proof producer
failed earlier while preparing both agent CLI homes. Codex auth resolved correctly; the missing
input was Claude-only file-backed auth.

The owner has now prohibited all Claude CLI, model, evaluation, and process use. That instruction
supersedes the original dual-agent execution method, but it does not turn the unexecuted proof into a
pass. A compliant future attempt must first implement and validate a Codex-only proof producer as a
new candidate. Until then, activation would be invented evidence and is forbidden.

## Agent evaluation and final review

| Gate | State |
| --- | --- |
| Earlier offline matched fixture | historically passed before the native-only instruction |
| Installed matched hard-task evaluation | not met |
| Original dual-agent requirement | superseded as an allowed method by the owner's native-only instruction |
| Codex-only replacement hard-task evaluation | not implemented for this frozen candidate |
| Separate final Codex review | pending orchestrator review |

No Claude pass is claimed. No Claude command, model, evaluation, launcher, or process was invoked in
the native resume. The stopped launcher work was removed rather than relabeled as evidence.

## Artifact and numerical QA

The successful deterministic suite covers known-answer, property, metamorphic, mutation,
independent-reference, accounting-identity, schema, artifact-value-parity, render, export, and
visual-integrity checks. The prior focused artifact run passed 83 render and export tests. No
analytics or artifact implementation changed after those results.

Saxo reconciliation inside the unexecuted per-analysis producer is still not met. Deterministic
coverage does not substitute for that broker-bound proof.

## Cleanup and unchanged-state proof

The controlled SIM run created bounded activity only in SIM. It captured 22 lifecycle calls, then
proved account state unchanged, cleanup complete, and zero uncleaned resources. It made no LIVE
call, purchase, or disclaimer response.

The native resume performed local auth preflight only. It proved the configured environment was
SIM with LIVE reads and writes disabled. The current token was expired, so both guarded SIM-auth
preflights stopped with `blocked_external_auth_material` and `network_call_made=false`. There was no
refresh attempt, browser launch, Saxo request, or controlled resource to clean during the resume.

The exact-candidate install fixture remains in owner-only local storage under its evidence ledger.
It is deliberately retained for the registered final consumers and has a recorded teardown owner;
it is not a leaked broker resource or an untracked temporary test directory.

## Privacy result

| Scan | Result |
| --- | --- |
| Exact-candidate installed-evidence scan | clean, 0 findings, 0 scan errors |
| Exact-candidate privacy self-scan | clean, 0 findings |
| GitHub CI public secret scan | passed |
| Native closeout scan over public docs and the Task 24 report | passed, 0 findings, 0 scan errors |
| Account identifiers or private financial values in this document | none |
| Credentials, raw URLs, raw broker payloads, or private paths in this document | none |
| Private evidence storage | owner-only ignored evidence root |

Private receipts remain outside publication. Public material contains only redacted counts,
digests, aliases, safety booleans, and status labels.

Secrets are never accepted through a chat channel:

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.

## External and policy limitations

The successful SIM matrix used a fresh owner-driven login. That session is not assumed to remain
fresh. Current local preflight reports expired bearer material and does not justify another broker
run without a new owner login.

The remaining proof blocker is not Saxo market data. It is the frozen producer's mandatory
dual-agent auth path combined with the owner's newer native-only policy. Fixing that correctly means
creating a new Codex-only producer and a new candidate, then repeating the install, proof,
reconciliation, matrix, cleanup, privacy, and review gates once. The frozen candidate cannot be
silently activated or reinterpreted.

No non-Saxo market or account data was substituted for missing broker results. Missing proof or
permissions remain reduced or refused.

## Full-suite completion status

| Completion gate | State |
| --- | --- |
| All 60 tools produce their expected passing SIM state | **not met**; 60 receipts exist, but 8 tools refused under quarantine |
| Every analysis kind has an executed source-bound proof profile | **not met**; 54 contracts exist, 0 active profiles |
| Deterministic numerical and accounting checks | met |
| Applicable Saxo reconciliation in the per-analysis producer | **not met** |
| Artifact value parity and visual QA | met in deterministic validation |
| Matched installed hard workflows | **not met**; original method is now disallowed |
| Missing-data and permission behavior | met by structured reduction or refusal |
| Controlled SIM cleanup and unchanged account state | met |
| Public privacy | met; preserved scans and the native closeout scan passed |
| Separate native Codex review | pending |
| No LIVE call, LIVE mutation, purchase, or disclaimer response | met |
| Analysis claims enabled in normal runtime | **not met**; all profiles remain quarantined |

Task 24 is closed as a truthful partial validation, not as full-suite completion. The implementation
is installable and its refusal boundary is safe, but the analytics-producing surface is not approved
for activation.

## Candidate history

| Commit | Meaning |
| --- | --- |
| `a9c4f24` | Complete local deterministic candidate, 2,593 tests passed |
| `b2b9872` | Install-validator startup reuse correction, then 2,596 local tests passed |
| `ea1f8f3` | Host-independent private-path redaction plus three focused tests |
| `97472e4` | Frozen runtime candidate and exhausted-auth documentation |
| `181ad08`, `d010df0` | Later Claude credential and launcher experiments; not accepted as final evidence |
| `134771d` | Removes the stopped experiments and restores the exact `97472e4` tree |

Documentation and the Task 24 report are committed on top of the frozen runtime candidate. They do
not change `src`, `tests`, `pyproject.toml`, or `uv.lock` candidate bytes.
