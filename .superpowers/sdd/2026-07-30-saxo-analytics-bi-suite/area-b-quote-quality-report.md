# Area B quote quality correction report

## Result

Implemented the field-level quote quality correction without Saxo access, authentication, browser
use, or an official source matrix run.

An HTTP 200 quote with `PriceTypeBid=NoAccess` and `PriceTypeAsk=NoAccess` now carries a value-free
field limitation proof. The analytics receipt and source matrix classify it as `reduced` with
denied entitlement. Store ingestion persists the proof and derives a `partial` dataset. Delayed,
missing, and null optional quote fields also derive `partial`. Only a fully proved quote derives
`complete`.

The post-review correction additionally prevents a caller from upgrading a persisted limited quote
page to a `complete` dataset and binds every quality-proof field path to the exact quote contract.

## RED evidence

The initial focused provider, store, and matrix run failed with 10 expected failures:

- provider pages had no `source_quality`
- stored datasets had no exposed `quality_state`
- the matrix refused the new proof shape instead of reducing valid field limitations
- the matrix receipt had no `source_quality`

The separate receipt regression failed with `KeyError: 'source_quality'`.

The post-review focused reproduction failed in four places before the correction:

- `SourceQualityProof` accepted `entitlement_limited_fields=["Invented.Field"]`.
- `AnalyticsStore.create_dataset` accepted caller `complete` over a persisted NoAccess page.
- the matrix accepted `Invented.Field` for `info_price_v1`.
- the matrix accepted the known but wrong-contract `PriceTypeBid` path for
  `info_prices_list_v1`.

The pre-change full suite also had one unrelated existing failure at 8 percent:
`tests/test_agent_skill_evidence_fail_closed.py::test_install_normal_mode_runs_instrumented_real_producer_path`.
It expected return code 0 and received 1. This task did not change that surface.

## Correction

- Added one strict `SourceQualityProof` model with only state and sorted safe field paths.
- Derived quote quality from the frozen source contract, compatible schema comparison, and
  validated rows.
- Detected `NoAccess`, delayed price types, positive delay minutes, and missing or null optional
  quote fields.
- Bound page and aggregate quality into the value-free analytics contract receipt.
- Required exact, internally consistent quality metadata in matrix validation.
- Classified field-level `NoAccess` as `reduced` and entitlement `denied`.
- Persisted the value-free proof in source-page payload metadata and derived dataset quality from
  all captured pages.
- Revalidated every persisted quality proof during direct dataset creation and downgraded a caller
  request for `complete` to `partial` whenever any selected page is limited.
- Restricted entitlement, delay, and missing-field proof categories to known quote fields, then
  required their union to be a subset of the selected contract's exact field paths.
- Kept transport, retry, pagination, schema quarantine, store replay, and privacy boundaries intact.

## GREEN evidence

- Focused quote regressions: provider 6 passed, receipt 1 passed, store replay 2 passed, matrix 3
  passed.
- Post-review focused regressions: 4 passed after 4 expected RED failures.
- Source and Area B regression command: 234 passed across source contracts, provider, receipt,
  store, store bridge, matrix, and Area B review rounds 2 through 6.
- `uv run ruff check .`: passed.
- Exact `uv run basedpyright`: 0 errors, 0 warnings, 0 notes.
- `uv sync --frozen --offline`: checked 97 packages.
- Changed-file privacy scan: passed with 0 findings and 0 scan errors.
- Repository-wide privacy scan reported seven existing regex findings in untouched files. They are
  outside this diff and are retained as baseline evidence at
  `/Volumes/ssd_1/codex/tmp/area-b-quote-privacy.XzdQJT/secret-scan.json`.
- `git diff --check`: passed.

## Offline candidate and sealed runtimes

- Candidate identity: `2534df0a57ebe1c6f6e4cb86e7e03fc7973542d05cbf2e4698ee719c5e34e886`
- Checked manifest SHA-256: `b125fb1df8d3433235b5b720430ccc9907ae2d68ce27ffce27f51c6d3ac33120`
- Final manifest A SHA-256: `b125fb1df8d3433235b5b720430ccc9907ae2d68ce27ffce27f51c6d3ac33120`
- Final manifest B SHA-256: `b125fb1df8d3433235b5b720430ccc9907ae2d68ce27ffce27f51c6d3ac33120`
- Portable runtime tree SHA-256: `2da0f802dbe4f9af5e7854334427be69aa79fd9d9e030e54a95d7f8ca79d25d1`
- Harness build SHA-256: `99109cd483c3af14e494b748db715db4aed3877954bbbfb587e8f4d7b856fb7b`
- Source build SHA-256: `14621597d0c86b933a2aa91fc5bb27fbf81ce41cd77dd12c8bfdd3721ac6d788`
- Installed build SHA-256: `7dfa452925c07bfc6e2a114e9ea8c075e8d08b093dc0d18de762b942a17eb474`
- Source contract catalog SHA-256:
  `ca5c53480842bb6c3e48dc16bac10f43c49edf73a09b206eacf3af40f0b20038`
- Source wheel projection SHA-256:
  `fef8e0194a557009293a4ce573a33ba6e312a3118d61739f7797c95afabbc39f`
- Final wheel SHA-256: `e5c64c77e6615a20de73d819e762ab7a408ce2359510a086f03c6fb2d812a3c4`
- Retained runtime root: `/Volumes/ssd_1/codex/tmp/area-b-quote-quality-fix.nFhlra`

The checked, A, and B manifests are byte-identical. Both final runtimes returned the same identity.
Both local preflights returned `closure=sealed`, `dont_write_bytecode=true`,
`ignore_environment=true`, `isolated=true`, and `no_site=true`.

The first offline build attempt correctly refused a generated `src/saxo_bank_mcp/__pycache__`.
Only that generated cache directory was removed. The successful rebuild used a fresh artifact root.

The immutable prior official candidate was not retried, deleted, replaced, or used for another
official source matrix run. The task commit SHA is returned in the handoff because a commit cannot
contain its own final SHA.

## Retained local evidence

- Changed-file privacy report:
  `/Volumes/ssd_1/codex/tmp/area-b-quote-quality-fix-privacy.qa2fNl/secret-scan.json`
- Offline checked and final manifests:
  `/Volumes/ssd_1/codex/tmp/area-b-quote-quality-fix.nFhlra`
