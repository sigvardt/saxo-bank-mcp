# Area B quote quality correction report

## Result

Implemented the field-level quote quality correction without Saxo access, authentication, browser
use, or an official source matrix run.

An HTTP 200 quote with `PriceTypeBid=NoAccess` and `PriceTypeAsk=NoAccess` now carries a value-free
field limitation proof. The analytics receipt and source matrix classify it as `reduced` with
denied entitlement. Store ingestion persists the proof and derives a `partial` dataset. Delayed,
missing, and null optional quote fields also derive `partial`. Only a fully proved quote derives
`complete`.

The first post-review correction prevents a caller from upgrading a persisted limited quote page to
a `complete` dataset and binds every quality-proof field path to the exact quote contract. The
second closes the public low-level store path by requiring the persisted `info_price_v1` proof to
equal the canonical quality recomputed from its exact stored contract and rows.

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

The second post-review direct-store reproduction then failed twice as expected:

- an exact `info_price_v1` NoAccess row with omitted `source_quality` created a `complete` dataset.
- the same row with a forged syntactically valid `complete` proof created a `complete` dataset.

The valid Realtime/complete and NoAccess/limited controls both passed before the correction.

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
- Required direct `info_price_v1` pages to carry the checked-in contract SHA, exactly one stored row
  matching `row_count`, a compatible schema, and a quality proof equal to canonical recomputation.
- Preserved the existing opaque low-level page behavior for non-quote and non-analytics contracts.
- Kept transport, retry, pagination, schema quarantine, store replay, and privacy boundaries intact.

## GREEN evidence

- Focused quote regressions: provider 6 passed, receipt 1 passed, store replay 2 passed, matrix 3
  passed.
- Post-review focused regressions: 4 passed after 4 expected RED failures.
- Direct-store regressions: 4 passed after 2 expected RED failures and 2 valid control passes.
- Source and Area B regression command: 238 passed across source contracts, provider, receipt,
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

- Candidate identity: `b766b2c2a9be0525085d2738126701d41fb928e2282c556b6dd10f22139ab719`
- Checked manifest SHA-256: `8bce95c3f294db7cd2581a68001dae6af4cf8c7546ee1bf7e63d5dc4651ef080`
- Final manifest A SHA-256: `8bce95c3f294db7cd2581a68001dae6af4cf8c7546ee1bf7e63d5dc4651ef080`
- Final manifest B SHA-256: `8bce95c3f294db7cd2581a68001dae6af4cf8c7546ee1bf7e63d5dc4651ef080`
- Portable runtime tree SHA-256: `b312ac3dc4a8d565a7e5c4c96aae3b2aeb0c7b08cc8f9239eb99933883bcbcaf`
- Harness build SHA-256: `2f80f32e16534abc2320a939eff4f6e13832f295b52630c84b45db9f8f45209f`
- Source build SHA-256: `582ca18fa57f36b6a12f4c48c629ef4bfcf11f3acbc393e2c72b5786ec8fcdef`
- Installed build SHA-256: `d7e5a3853e0abf4125b9e1f6dd899ffbafa629044989288fe94fecff37419eb5`
- Source contract catalog SHA-256:
  `ca5c53480842bb6c3e48dc16bac10f43c49edf73a09b206eacf3af40f0b20038`
- Source wheel projection SHA-256:
  `fef8e0194a557009293a4ce573a33ba6e312a3118d61739f7797c95afabbc39f`
- Final wheel SHA-256: `93e3e884d15ac3f4e934c474187addf86dd902fb4a5ff185d0b68dbc9e944199`
- Retained runtime root: `/Volumes/ssd_1/codex/tmp/area-b-quote-quality-store-fix.nU8G7s`

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
  `/Volumes/ssd_1/codex/tmp/area-b-quote-quality-store-fix-final-privacy.AHkF0q/secret-scan.json`
- Offline checked and final manifests:
  `/Volumes/ssd_1/codex/tmp/area-b-quote-quality-store-fix.nU8G7s`
