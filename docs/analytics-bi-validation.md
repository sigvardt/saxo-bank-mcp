# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Latest sealed evidence candidate `d62082f2af861d5dfa088855c89dbe5d0451ffac`, tree
`02b85b84266c733118fd5430a6710a13f1031e8b`, passed its exact Codex-only install,
signed 2,795-test full suite, and required local gates. Its one sealed proof command ended at an
authenticated candidate-runner boundary failure. The old launcher then overwrote the child result,
so child model, MCP, Saxo, mutation, purchase, disclaimer, and cleanup facts are unknown.

Local corrective source `06fb8e1731af158825d4c83a6f01cadb489dec83`, tree
`db36b068962bdb266d5f7b49da4d0e9b2234b738`, separates the child result from the final
publication, verifies its complete native binding, and retains only authenticated child evidence
when runner cleanup or receipt publication fails. It has passed local tests and static checks only.
It has not had an exact install, signed full suite, SIM preflight, or sealed proof.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, transcripts, model arguments, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Latest sealed source | `d62082f2af861d5dfa088855c89dbe5d0451ffac` |
| Latest sealed tree | `02b85b84266c733118fd5430a6710a13f1031e8b` |
| Local corrective source | `06fb8e1731af158825d4c83a6f01cadb489dec83` |
| Local corrective tree | `db36b068962bdb266d5f7b49da4d0e9b2234b738` |
| Harness policy | `codex_native_v1` |
| Installed surface | 60 tools, including 21 analytics tools; 9 skills; 1 MCP server |
| Saxo operation catalog | 294 operations: 182 implemented and 112 refused |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The historical `dual_v1` policy and its evidence remain unchanged. The native policy uses only
the Codex harness. Shared package inventory metadata does not constitute another client, account,
process, or evaluation.

## Exact sealed-candidate gates

The retained-runtime installation for `d62082f` passed with 60 tools, 9 skills, 1 MCP server, 616
exact compared files, no inventory mismatch, unchanged caller state during the install window,
owner-only evidence, and complete install-process cleanup.

The signed exact-candidate full-suite receipt reports:

- 2,795 tests passed;
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

## SIM authorization gate

Before the sealed command, local status proved requested and effective environment `SIM`, with
LIVE reads disabled and LIVE writes disabled. One separate read-only session-capability call passed
and truthfully recorded `network_call_made=true`.

That receipt proves only its own SIM request. Because the child publication was lost at the later
runner boundary, it does not prove the child's preflight, broker writes, account equality, or any
later proof phase.

## One sealed proof attempt

Exactly one sealed `codex_native_v1` command ran for `d62082f` and exited 1. The final outer
publication and candidate-runner receipt authenticate only this boundary:

| Field | Authenticated value |
| --- | --- |
| Result kind | `boundary_failure` |
| Boundary phase | `candidate_runner` |
| Reason | `proof_candidate_runner_cleanup_failed` |
| Candidate runner command state | completed |
| Candidate runner spawned | true |
| Candidate runner exit | 1 |
| Candidate runner cleanup | failed |
| Candidate result presence | true, with a bound digest in the runner receipt |
| Child publication after outer handling | unavailable because the final path was overwritten |
| Child model, MCP, and Saxo events | unknown |
| Child execution and SIM network provenance | unknown |
| Broker write, LIVE mutation, purchase, disclaimer response | unknown |

The old candidate runner passed the final publication path directly to the child. After the child
wrote a result, runner cleanup failed and the outer exception handler published its refusal to the
same path. This destroyed the only child publication while retaining only its digest and presence
in the runner receipt. No child fact is reconstructed from that digest.

The producer did not deliver authenticated numerical receipts, a fresh 60-tool SIM matrix, Saxo
reconciliation, account equality, or activation to the final publication. No retry ran.

## Local corrective source

Source `06fb8e1` gives the candidate child a distinct owner-only sidecar. The final output is
reserved for the parent publication. Before trusting the sidecar, the parent verifies:

- regular-file, single-link, owner, directory, and mode requirements;
- the complete authenticated native publication schema and digest;
- exact candidate commit, `codex_native_v1` policy, proof contract, analysis-kind count, and
  evidence-receipt count.

If runner cleanup completes, the parent publishes the verified bytes and verifies them again. If
cleanup fails, the parent publishes an overall refusal while retaining the authenticated sidecar
and binding both its digest and the candidate-runner cleanup receipt. A runner-receipt write failure
can bind the verified sidecar without inventing cleanup facts. Missing, malformed, tampered,
mismatched, linked, or incorrectly permissioned output is not trusted, and no child execution,
network, model, MCP, Saxo, mutation, purchase, or disclaimer fact is copied from it.

Local validation for `06fb8e1` reports:

- 95 focused publication, failure-envelope, and candidate-launcher tests passed twice;
- 166 related proof, bootstrap, profile, launcher, publication, and install tests passed;
- 245 auth, session, redaction, privacy, and secret-scan tests passed;
- Ruff passed and the four changed files match Ruff format;
- BasedPyright reported 0 errors, 0 warnings, and 0 notes;
- plugin, nine-skill static, 60-tool/294-operation catalog, and 34-case evaluation-manifest checks
  passed; and
- a bounded four-file privacy scan reported 0 findings and 0 scan errors.

No model, MCP server, Saxo API, browser, broker, or network activity ran in this corrective batch.
The source requires a new exact retained-runtime install, signed full suite, local readbacks, SIM
safety preflight, and separately authorized sealed proof before any downstream gate can resume.

## Cleanup and activation

Post-attempt local receipts prove the consumed proof runtime was removed, the proof temporary root
was absent, and no exact-candidate process remained. Those local facts do not prove broker account
equality or any unknown child behavior.

No fresh controlled SIM lifecycle, 60-tool matrix, numerical proof, Saxo reconciliation, artifact
parity proof, or attempt-bound account equality receipt exists for the latest sealed attempt. All
54 profiles remain quarantined and 0 are active. The branch remains local and unpushed.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.
