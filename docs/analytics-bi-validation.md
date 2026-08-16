# Saxo Analytics and BI Suite - Task 24 Native Validation

Status: Task 24 is partially validated; analytics activation is not approved
Date: 2026-08-16
Scope: Task 24 of `docs/superpowers/plans/2026-07-30-saxo-analytics-bi-suite.md`
Result: **the evidence-bound Codex-native candidate passed deterministic, install, static, type,
and privacy gates; a post-review correction now blocks all model/offline proof work until SIM
capabilities pass; fresh broker-bound proof remains stopped at the rejected expired-session refresh**
Independent review: **pending the root orchestrator's separate native review**

All public values below are redacted. This document contains no credentials, account identifiers,
balances, holdings, money values, local private paths, raw broker payloads, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Frozen native source candidate | `9c94bf17eeabead8941d5b15f85aa08eb17c0dce` |
| Candidate tree | `a6bbb9cbd8fcf71a90d687fee228eae0df3d52b6` |
| Post-review corrective source | `9c5945e21ac3d3911bfe2839759cb1aae5011ebe` |
| Corrective source tree | `652803edccec83f337ebcee5665bd3bb72ddd3c9` |
| Harness policy | `codex_native_v1` |
| Dependency lock fingerprint | `uv.lock` SHA-256 `eb4584bf6f712062fac281b6ff1d2ba8205d91f026e115abae7fae201baa3482` |
| Plugin/project version | `0.1.0` |
| Tool catalog | 60 tools, including 21 analytics tools |
| Proof catalog | 54 analysis kinds, all quarantined as `implementation_pending` |

This candidate adds a parallel native policy, Codex-only installed-candidate report, sealed native
proof child, and Codex-only hard-workflow selection. It does not change, relabel, or consume the old
dual-agent report types or evidence. Only the allowed model quorum changes: the native path requires
exactly one real Codex harness record. Numerical proof, source binding, Saxo reconciliation,
artifact parity, cleanup, privacy, SIM-only execution, and no-write requirements are unchanged.

The evidence ledger uses a new candidate-specific namespace. Historical receipts remain bound to
their historical commits and cannot satisfy `codex_native_v1`.

## Headline safety result

| Claim | Value |
| --- | --- |
| Requested and effective broker environment | SIM |
| LIVE reads enabled | false |
| LIVE writes enabled | false |
| SIM OAuth requests in the native preflight | 1 |
| SIM account, market, order, subscription, or analytics-job calls | 0 |
| `live_events` / `live_mutation_calls` | 0 / 0 |
| `purchase_occurred` | false |
| `disclaimer_response_made` | false |
| Visible browser opened by this work | no |
| Account mutation calls | 0 |
| Current account-state readback | unavailable because session capabilities did not pass |
| Checked-in proof profiles activated | 0 of 54 |

SIM needs no human approval.

## Native implementation and TDD

The native path is explicit and tested:

- `codex_native_v1` requires exactly the `codex` harness and a one-model quorum.
- Native runtime preparation copies only the minimum Codex file-backed authentication and SIM
  material; it does not resolve, copy, launch, or promote any second-client state.
- The Codex-only install report has no second-client field, command, process, state, or receipt.
- The native proof producer rejects a non-Codex record, a skipped model call, LIVE scope, incomplete
  catalog coverage, failed numerical or Saxo proof, cleanup residue, state change, privacy finding,
  broker write, purchase, or disclaimer response.
- Eleven tagged native hard cases cover the 60-tool logical catalog contract using only LOCAL or
  SIM scope. The historical 33-case dual selection remains unchanged; the complete manifest now
  contains 34 cases because the native-only safety case is excluded from the historical selector.
- Native failure and refusal results preserve observed network provenance. Missing or unobserved
  provenance is not rewritten as `false`.
- The native CLI performs the local SIM-environment check and current session-capability call
  before any hard-model or offline proof work. A blocked preflight emits a typed redacted receipt;
  an observed HTTP status forces `network_call_made=true`, and a later proof error retains the
  already-observed preflight provenance.

Tests were written RED first for the policy, runtime isolation, Codex-only installation, native
producer, hard-suite coverage, LIVE rejection, and legacy-case preservation. The focused suites
then passed twice. The related proof, install, evaluation, and catalog suite also passed before the
candidate was frozen. Ruff and BasedPyright were clean after the source changes.

The post-review preflight correction added four focused tests, run twice, plus a 17-test related
native suite. Ruff and BasedPyright then passed. The nine-skill static gate reported zero findings,
and the bounded credential/private-value scan over the changed public documents and private report
reported zero findings and zero scan errors. Per instruction, the guarded full suite, clean
installation, Saxo preflight, and broker matrices were not rerun; their evidence remains bound to
`9c94bf1` and is not relabeled as evidence for `9c5945e`.

## Deterministic and static validation

| Gate | Result |
| --- | --- |
| Guarded full repository suite | 2,618 collected; reached 100%; exit 0 |
| Pytest launcher | `scripts/run-pytest` only, with the required external temp root |
| Ruff | passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Agent skill static gates | passed, 9 skills, 0 findings |
| Generated catalogs | passed: 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Eval manifest validation | passed: 34 cases, 60 tools, 9 skills, 0 errors |
| Focused public-redaction/privacy tests | 214 passed |
| System disk guard | approximately 84 GiB free after validation; requirement at least 50 GiB |

The guarded full suite was run once for this frozen candidate. No direct `pytest` invocation was
used, no active pytest directory was deleted, and expensive final gates were not repeated.

The deterministic suite covers known answers, properties, metamorphic relations, seeded mutations,
independent references, accounting identities, schemas, artifact value parity, render/export
integrity, and structured reduced/refusal behavior. These checks do not replace broker-bound Saxo
reconciliation.

## Codex-only isolated installation

The one clean installation run is bound to the exact frozen commit and passed with no errors.

| Gate | Result |
| --- | --- |
| Execution mode | `codex_installed_verification` |
| Harness policy | `codex_native_v1` |
| Installed surface | 60 tools, 9 skills, 1 MCP server |
| Startup probes | source, installed cache, and registered tool list each passed with 60 tools |
| Installed byte comparison | 591 files compared, exact inventory, no mismatches |
| Required files and annotations | present; no missing annotations |
| Forbidden files | absent |
| Caller Codex state | unchanged |
| Process cleanup | complete, no remaining run-owned processes or process groups |
| Evidence ownership and privacy | owner-only and clean |

This work did not start a Claude CLI, model, evaluation, launcher, or process. No historical
second-client result is presented as native evidence.

## Guarded SIM authentication

The network-free local status read first proved:

- requested environment `SIM`;
- effective read environment `SIM`;
- LIVE reads and writes disabled;
- a readable SIM cache with refresh support;
- expired bearer material;
- no configured registered redirect URI.

The single permitted capability call attempted to refresh the expired bearer at Saxo's SIM OAuth
endpoint. Saxo returned HTTP 401. The nested capability result and enclosing preflight both record
`network_call_made=true`; an HTTP response is never described as no request. Session capabilities,
account access, and trading readiness are therefore unproved.

The local PKCE-start fallback stopped at the missing redirect-URI gate. It did not open a browser.
A second network-free status read matched the first status metadata exactly. No successful token
refresh or save was observed.

The attempt did not reach a session, account, market-data, order, subscription, or analytics-job
endpoint. It created no controlled broker resource and made no account mutation. Because current
account access was unavailable, this candidate does not claim a fresh broker account-state
readback. The older controlled SIM run retains its own unchanged-state proof, but that historical
receipt is not relabeled for this candidate.

Per the frozen plan's fail-closed rule, all remaining Saxo and model proof activity stopped after
the rejected refresh. There was no blind retry and no browser flow.

## Fresh 60-tool SIM matrix

The fresh matrix was **not run** for `9c94bf1` because SIM session capabilities did not pass.

The historical controlled run at `97472e4` remains useful only as historical safety evidence: it
captured 60 tool receipts, cleaned its controlled activity, and proved its own account state
unchanged. Native review correctly reclassified that historical run as three structured quarantine
refusals plus five invalid-argument coverage gaps. Commit `8f8b00b`, included in the native
candidate, gives those five downstream tools schema-valid local arguments; focused real-FastMCP
tests prove their intended structured refusal path with `mcp_is_error=false` and no Saxo request.
Those focused tests are not a fresh broker matrix and are not reported as one.

## Per-analysis proof, reconciliation, and activation

| Item | Value |
| --- | --- |
| Production analysis kinds | 54 |
| Proof contracts defined | 54 |
| Native producer implemented | yes |
| Native producer executed against Saxo SIM | no |
| Fresh per-analysis proof receipts | 0 |
| Fresh Saxo reconciliation | not run |
| Profiles activated | 0 of 54 |

The producer retains the existing known-answer, property, independent-reference, mutation,
numerical-tolerance, accounting, Saxo-reconciliation, artifact, recovery, privacy, and agent-use
requirements. The auth refusal occurred before those broker-bound receipts could be generated.
Deterministic formula coverage does not authorize activation.

No checked-in proof catalog changed. Activation remains forbidden until all 54 receipts pass for
one exact installed candidate. Any activation commit would itself be a new candidate and would need
the affected install, proof, SIM, cleanup, privacy, and review gates again.

## Native hard workflow and final review

| Gate | State |
| --- | --- |
| Native hard-suite policy and manifest coverage | validated |
| Real installed Codex model evaluation | not run after SIM auth refusal |
| Historical matched model evidence | unchanged and not accepted for `codex_native_v1` |
| Separate final Codex review | pending root orchestrator |

No Claude pass is claimed. The native hard-workflow harness exists and is structurally validated,
but a real model run is not invented from unit tests or manifest coverage.

## Privacy and storage

| Scan | Result |
| --- | --- |
| Codex-only installation privacy self-scan | passed |
| Focused redaction/privacy suite | 214 passed |
| Public documentation and Task 24 report scan | passed, 0 findings, 0 scan errors |
| Credentials, account identifiers, or private financial values in this document | none |
| Private evidence storage | owner-only ignored evidence root |

Private receipts remain outside publication. Public material contains only redacted counts,
digests, safety booleans, and status labels.

Secrets are never accepted through a chat channel:

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.

## Full-suite completion status

| Completion gate | State |
| --- | --- |
| Exact 60-tool catalog | met |
| Deterministic numerical, accounting, artifact, static, type, and privacy checks | met |
| Exact Codex-only install | met |
| Fresh 60-tool SIM matrix | **not met**; stopped at SIM OAuth HTTP 401 |
| Every analysis kind has executed source-bound proof | **not met**; 0 of 54 active |
| Applicable Saxo reconciliation | **not met** |
| Real installed native hard workflow | **not met** |
| Controlled cleanup and current account-state readback | no resource existed to clean; account readback unavailable |
| No LIVE call, broker mutation, purchase, or disclaimer response | met |
| Separate native review | pending |
| Analysis claims enabled in normal runtime | **not met**; all profiles remain quarantined |

Task 24 remains a truthful partial validation. The native-only implementation and its deterministic
candidate gates pass, but broker-bound completion and analytics activation do not.

## Candidate history

| Commit | Meaning |
| --- | --- |
| `97472e4` | Historical controlled SIM candidate; evidence stays historical |
| `134771d` | Removed the stopped Claude-launcher experiment and restored the historical tree |
| `8f8b00b` | Preserved OAuth provenance and added valid five-tool local refusal coverage |
| `59b5dbb` | Approved the parallel Codex-native proof design |
| `0dbc4d9` | Added the native proof implementation plan |
| `f7d78c6` | Added the explicit `codex_native_v1` policy and runtime isolation |
| `4458899` | Added exact Codex-only plugin installation evidence |
| `9c94bf1` | Added the parallel native proof producer and hard-suite policy; frozen source candidate |
| `9c5945e` | Gates native proof work on current SIM capabilities and preserves blocked provenance |

## Exact next gate

A fresh owner-local SIM login or valid owner-only SIM cache must make
`saxo_get_session_capabilities` pass. Only then may one new controlled run execute the native hard
workflow, full per-analysis proof and Saxo reconciliation, fresh 60-tool SIM matrix, cleanup,
account readback, privacy, and activation decision. No browser is opened by this task and no
non-Saxo market or account data may substitute for missing broker results.
