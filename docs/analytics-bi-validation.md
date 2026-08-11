# Saxo Analytics and BI Suite — Final Candidate Validation

Status: final SIM validation for the frozen candidate
Date: 2026-08-11
Scope: Task 24 of `docs/superpowers/plans/2026-07-30-saxo-analytics-bi-suite.md`
Result: **partially validated — offline and installed gates passed, Saxo SIM execution refused**

All values below are redacted. This document contains no credentials, account identifiers,
balances, holdings, money values, local paths, or raw broker payloads.

## Candidate identity

| Item | Value |
| --- | --- |
| Candidate commit | `b2b9872934e02dea63f90291f95693d0bfa83267` |
| Candidate tree | `416cb7734186e7cdc82a12c7ffff4de009e89a0e` |
| Worktree state at freeze | clean (0 modified paths) |
| Dependency lock fingerprint | `uv.lock` SHA-256 `eb4584bf6f712062fac281b6ff1d2ba8205d91f026e115abae7fae201baa3482` |
| Plugin/project version | `0.1.0` |
| Base | `origin/main` at `0c5b4bbab6e25d257b3ce703debdd97fdc48d067` |

The predecessor candidate `a9c4f242d4c1d0dcf2fa85a6b28e07b189ad01f8` passed the complete
deterministic suite (2593 tests). The only source change after it is a validator correction that
stops starting the same MCP root twice; see [Candidate history](#candidate-history).

## Headline safety result

| Claim | Value |
| --- | --- |
| `live_events` | 0 |
| `live_mutation_calls` | 0 |
| `purchase_occurred` | false |
| `disclaimer_response_made` | false |
| LIVE endpoint called | no |
| LIVE write performed | no |
| Visible browser opened, focused, or controlled | no |
| Effective read environment proven before any Saxo call | SIM |
| LIVE reads enabled | false |
| LIVE writes enabled | false |

SIM needs no human approval.

## Deterministic suite, lint, and types

| Gate | Result |
| --- | --- |
| Full deterministic suite (`scripts/run-pytest`, guarded launcher) | 2593 passed, 0 failed at `a9c4f24`; executed total equals collected total |
| Suite collection at this candidate | 2596 tests (3 new validator-reuse tests) |
| Focused validation for the validator change | 82 passed (install validator, fail-closed, paths, update probe, evidence fail-closed, new reuse tests) |
| Privacy, redaction, and secret-scan suites | 234 passed |
| Artifact render and export suites | 83 passed |
| `ruff check .` | clean |
| `ruff format --check` on changed files | clean |
| `basedpyright` | 0 errors, 0 warnings, 0 notes |
| `git diff --check` | clean |

Every pytest invocation ran through `scripts/run-pytest`. The launcher refuses unless the resolved
temporary root is on the external volume and the system disk has at least 50 GiB free; at the final
check the system disk had 88.4 GiB free and the external volume 1725.3 GiB free.

`ruff format` is not a CI or plan gate for this repository and reports pre-existing drift in 60
baseline files; it is recorded here as a style observation only and was not applied to the frozen
candidate.

## Plugin, skill, and catalog gates

| Gate | Result |
| --- | --- |
| `scripts/validators/validate_plugin.py .` | passed |
| `claude plugin validate --strict .` | passed |
| `quick_validate.py` for all 9 skills | 9 passed |
| Agent skill static gates | passed; 9 skills, version parity true, 0 wildcard, 0 link, 0 frontmatter, 0 nested-reference, 0 cache-dangerous findings |
| Generated catalog check | tool_count 60, analytics_tool_count 21, operation_count 294, implemented 182, refused 112, service groups 17, scenarios 60, analytics scenarios 10, skills 9 |
| Eval manifest validation | passed; 33 cases, 60 tools, 9 skills, 0 errors |

## Isolated installation (passed)

One isolated dual-client installation and one separate verification ran for this exact candidate.

| Item | Codex | Claude |
| --- | --- | --- |
| Installed | true | true |
| Tools listed from the installed MCP | 60 | 60 |
| Skills | 9 | 9 |
| MCP servers | 1 | 1 |
| Installed bytes match candidate | true | true |
| Missing tool annotations | none | none |

| Installation gate | Result |
| --- | --- |
| Status | passed, 0 errors |
| Execution mode | installed verification |
| Startup proof per distinct root | source tree, installed Codex cache, installed Claude cache each started and listed 60 tools |
| Installed byte comparison | 1164 files compared, inventory exact match, forbidden files absent, 0 mismatches |
| Update probe | version bump and restore proved, inventory exact match, 12 update receipts, 19 required receipts present |
| Isolated client global state | before equals after, unchanged |
| Local process cleanup | complete; 28 observed process groups, 0 remaining processes, 0 remaining process groups |
| Privacy scan of installed evidence | clean, 0 findings, 0 scan errors, 26 files, 5 scopes |
| Privacy self-scan | clean, 0 findings |
| Auth material published | none copied, no values published |
| Separate verification step | passed, startup verified, global state recomputed and unchanged |

## Proof-profile coverage

| Item | Value |
| --- | --- |
| Production analysis kinds | 54 |
| Per-analysis evidence receipts defined | 54 |
| Proof execution contract digest | `d1052988772301f8e3b6bbd0a3fd0e9fddc793610b6c1cab3b35286904a3f0af` |
| Contract coverage | every analysis kind, metric, artifact template, and evidence receipt represented exactly once |
| Executed per-analysis proof result | **refused** — `proof_sim_auth_lease_unavailable` |
| `execution_performed` | false |
| `live_mutation_calls` during the attempt | 0 |
| `broker_write_made` during the attempt | false |

The installed candidate itself verified successfully, so the refusal is bound to the absent Saxo
SIM session rather than to the candidate. Deterministic numerical correctness (known answers,
properties, metamorphic cases, independent reference paths, mutation kills, accounting identities,
and artifact parity) is exercised by the deterministic suite above; the Saxo reconciliation and
executable SIM legs of each proof profile remain unexecuted.

## 60-tool SIM matrix (refused)

| Item | Value |
| --- | --- |
| Environment | SIM |
| Matrix status | blocked |
| Primary reason | `sim_session_auth_required` (surfaced as a token-refresh state mismatch) |
| Secondary reasons | `account_allowlist_unresolved`, `fixture_reference_invalid` |
| Tool receipts captured | 4 of 60 |
| Analytics tool receipts | 0 of 21 |
| Controlled SIM lifecycle calls | 0 |
| Registered trading write operations | 0 |
| Uncleaned resources | 0 |
| `live_events` / `live_mutation_calls` | 0 / 0 |
| `purchase_occurred` | false |
| `disclaimer_response_made` | false |
| Publication | redacted |

The matrix reached the installed MCP, proved `environment=SIM`, and stopped at authentication. No
order, subscription, or account mutation was attempted.

## Agent evaluation

| Evaluation | Result |
| --- | --- |
| Offline matched dual fixture (Codex and Claude) | **passed** — 2 of 2 cases, both harnesses selected `saxo_propose_trade_from_analysis` and stopped before any broker write; 0 external calls, 0 broker writes, 0 disclaimer responses |
| Installed matched hard-task evaluation against the real MCP | **refused** — `codex_file_auth_missing`; 14 cases selected, 0 executed |
| Model prompts issued during the refused run | 0 |
| Saxo events during the refused run | 0 |
| Logical tools invoked during the refused run | 0 |
| Isolated client global state | before equals after, unchanged |
| Installation fixture | preserved for later consumers |

The installed evaluation needs file-backed agent CLI credentials. The retained owner-only agent home
contains none, the previously retained Claude credential was rejected by its service, and its one
permitted refresh was also rejected. An isolated headless agent login then reached a human
verification challenge. No Keychain, `security` CLI, or `osascript` path was used, and no visible
window appeared.

## Cleanup and unchanged-state proof

| Item | Value |
| --- | --- |
| Controlled SIM activity performed | no |
| Reason | no brokerage session; the owner-only SIM token cache is expired and refresh was rejected |
| Resources requiring cleanup | none — 0 lifecycle calls, 0 trading writes, 0 uncleaned resources |
| Brokerage before/after comparison | not performed, because no session existed to read state and nothing was created |
| Isolated Codex and Claude client state | unchanged in both the installation and evaluation runs |
| Local process cleanup | complete in every run; 0 remaining processes |
| Orphan processes created by this work | 0 |
| Processes terminated by this work | 0 |
| External temporary state | diagnostic scratch removed; the retained installation fixture was preserved under its recorded ledger consumers and teardown owner |

The matrix receipt reports `account_state_unchanged=false` and `cleanup_complete=false`. Those flags
mean the comparison could not run without a session. They do not mean account state changed or that
resources leaked: nothing was created, and the uncleaned-resource count is 0.

## Privacy result

| Scan | Result |
| --- | --- |
| Exact CI public secret scan over published paths | passed, 0 findings |
| Installed-evidence privacy scan | clean, 0 findings, 0 scan errors |
| Installed-evidence privacy self-scan | clean, 0 findings |
| Bounded changed-file secret scan | passed, 0 findings |
| Gitignore secret probe | passed |
| Privacy, redaction, and secret-scan test suites | 234 passed |
| Private account values in public evidence | none |
| Credentials, raw identifiers, URLs, or local paths in public evidence | none |

Private owner-only receipts remain outside the repository in the ignored evidence area. Published
evidence carries only schemas, digests, counts, safe aliases, and pass/fail states.

Secrets are never accepted through a chat channel:

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.

## LIVE boundary probes

| Probe | Result |
| --- | --- |
| LIVE write refusal | refused, no network call made |
| LIVE read refusal | refused (`missing_live_read_enablement`), no network call made |
| Local MCP health | passed, SIM mode, `live_writes=false` |

## External Saxo limitations encountered

1. The owner-only SIM token cache is expired. A refresh of the most recent retained token was
   rejected by the SIM token endpoint with HTTP 401. Saxo SIM refresh tokens are short-lived, and
   both retained caches are more than seven days old.
2. Completing a fresh PKCE authorization requires a registered redirect URI and an authorization
   code returned to that redirect. The remaining machine-completable blocker is
   `sim_redirect_uri_missing`, and the missing material is a registered redirect URI plus an
   authorization code from the Saxo redirect.
3. The retained isolated headless browser profile holds zero cookies, so no single-sign-on session
   survives for a headless replay; a replay would land on the interactive login form.
4. The only login-credential source wired into the retained headless login script is the macOS
   Keychain, which is out of bounds for this run. It was not invoked.
5. Saxo SIM entitlement and fixture references could not be resolved without a session, so
   entitlement-dependent degradation paths were exercised only by their deterministic tests.

These are environment and credential limitations. They are not defects in the candidate, and none of
them was worked around with non-Saxo data.

## Independent review

The final independent review of this exact candidate and its redacted evidence has **not** been
performed in this session. It is scheduled as a separate persistent review session. This document
therefore records no final independent verdict.

## Full-suite completion status

| Completion gate | State |
| --- | --- |
| Every one of the 60 MCP tools passes the actual SIM matrix | **not met** — refused at authentication |
| Every in-scope analysis kind has a current source-bound proof profile | met for definition and contract coverage; executable SIM leg **not met** |
| Material metrics pass known-answer, property, metamorphic, mutation, numerical, and independent-reference checks | met |
| Applicable Saxo reconciliation checks | **not met** — no session |
| All accounting identities pass | met |
| Artifacts contain the same verified values as structured output and pass visual QA | met |
| Codex and Claude complete the hard workflows with installed skills and the actual MCP | **not met** — agent CLI credentials unavailable |
| Missing Saxo data and entitlements produce the proved degradation or refusal | met |
| Cleanup succeeds and SIM account state is unchanged after controlled activity | not applicable — no controlled activity occurred; nothing required cleanup |
| Public evidence contains no credentials, identifiers, private values, paths, raw URLs, or raw payloads | met |
| Final independent review finds no reproducible blocker | pending |
| No LIVE endpoint called, no LIVE mutation, no purchase | met |

Task 24 is complete for every gate that does not require a Saxo SIM session or an agent CLI login.
The remaining gates are blocked on external credentials and are recorded above as honest refusals
rather than as passes.

## Candidate history

| Candidate | Change | Reason |
| --- | --- | --- |
| `a9c4f24` | isolated evaluation credential diagnostics | prior correction round |
| `b2b9872` | start each install proof root once | The isolated installation validator started the installed Codex and Claude cache roots twice: once for the cache startup claim and again for a `list_tools` claim about the same root. Duplicate claims now reuse the one successful result. Independent proof for the source tree, the installed Codex cache, and the installed Claude cache is unchanged, and each root is still started and listed. |

The correction is validator-only. It changes no analytics formula, MCP tool, tool count, catalog,
schema, broker behaviour, or public contract.

This document and the vision-status update are committed on top of the candidate as a
documentation-only change. They do not alter the validated bytes, so the evidence above stays bound
to `b2b9872` and was not regenerated for them.
