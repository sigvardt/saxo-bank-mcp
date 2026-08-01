# Area B quote quality correction report

## Result

Implemented the field-level quote quality correction without Saxo access, authentication, browser
use, or an official source matrix run.

An HTTP 200 quote with `PriceTypeBid=NoAccess` and `PriceTypeAsk=NoAccess` now carries a value-free
field limitation proof. The analytics receipt and source matrix classify it as `reduced` with
denied entitlement. Store ingestion persists the proof and derives a `partial` dataset. Delayed,
missing, and null optional quote fields also derive `partial`. Only a fully proved quote derives
`complete`.

## RED evidence

The initial focused provider, store, and matrix run failed with 10 expected failures:

- provider pages had no `source_quality`
- stored datasets had no exposed `quality_state`
- the matrix refused the new proof shape instead of reducing valid field limitations
- the matrix receipt had no `source_quality`

The separate receipt regression failed with `KeyError: 'source_quality'`.

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
- Kept transport, retry, pagination, schema quarantine, store replay, and privacy boundaries intact.

## GREEN evidence

- Focused quote regressions: provider 6 passed, receipt 1 passed, store replay 2 passed, matrix 3
  passed.
- Source and Area B regression command: 230 passed across source contracts, provider, receipt,
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

- Candidate identity: `864eb5c8277179a59854ea91113d5d40ce4c5307338e549c46a6443925b0d4e3`
- Checked manifest SHA-256: `3f83a35ac3db595a489c524d187e99433d2c2ba586eefcf93e877d656773c1cd`
- Final manifest A SHA-256: `3f83a35ac3db595a489c524d187e99433d2c2ba586eefcf93e877d656773c1cd`
- Final manifest B SHA-256: `3f83a35ac3db595a489c524d187e99433d2c2ba586eefcf93e877d656773c1cd`
- Portable runtime tree SHA-256: `04a72a41a14a700a942478a7098855b489af652ac4ad1cae95c00371c3b333d3`
- Harness build SHA-256: `9cb98ed1ce69b68a0b1c10205eefe04102e5127456cad22f5e9ef18799786d87`
- Source build SHA-256: `527fb9e68d0717928f25d242739bf54e0e971dbb83684263adf6bc468717f823`
- Installed build SHA-256: `c8697d0b76230befe9925d5b36cfaf239928b8f6e472699094e9a4c3e95a3f39`
- Source contract catalog SHA-256:
  `ca5c53480842bb6c3e48dc16bac10f43c49edf73a09b206eacf3af40f0b20038`
- Final wheel SHA-256: `9500f43f31d93e69400652e9a833b4562962304495ac2212b0e83c2ef5f8c7ed`
- Retained runtime root: `/Volumes/ssd_1/codex/tmp/area-b-quote-quality.gPXU1e`

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
  `/Volumes/ssd_1/codex/tmp/area-b-quote-scoped-privacy.IXFGNf/secret-scan.json`
- Offline checked and final manifests:
  `/Volumes/ssd_1/codex/tmp/area-b-quote-quality.gPXU1e`
