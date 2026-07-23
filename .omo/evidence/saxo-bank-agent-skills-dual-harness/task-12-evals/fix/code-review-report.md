# Task 12 fail-closed code review

Verdict: PASS.

Scope reviewed: existing dirty diff for install, matrix, and release helper families, plus the model split and focused fail-closed tests. The QA skill and eval manifests were not edited.

Direct programming review:
- `qa_dual_plugin_install.py` no longer emits a synthetic pass for missing install evidence. Planning emits `planned`; installed verification requires schema-valid install evidence, resolved commit, clone binding, global fingerprints, client cache bytes, startup/list-tools counts, cleanup, and expected counts.
- `run_mcp_tool_matrix.py` fails closed when `--install-report` is missing or invalid. Manifest-only execution emits `validated` with zero calls, never `passed`; `--verify-only` requires real tool-call evidence.
- `assemble_agent_skill_release.py` fails closed on unresolved or stale commits, missing or empty evidence roots, missing plan, missing LIVE proof, missing task evidence, missing install/SIM/LIVE/privacy evidence, stale artifacts, dirty source repos, and catalog count mismatches. Failed release attempts do not update `latest.json`.
- Shared git and file hash helpers live in `src/saxo_bank_mcp/agent_skill_evidence_io.py` to keep touched files at or below the 250 pure-line limit.

Remove-ai-slops review:
- Removed false-confidence behavior from the retained dirty diff: hardcoded pass manifests, manifest-only pass states, and zero-call SIM pass paths.
- No broad `except Exception`, `BaseException`, `type: ignore`, `cast`, or raw `object` annotation remains in the reviewed source scope.
- Size check after final source edits: `agent_skill_install_qa.py` 246 pure lines, `agent_skill_release.py` 250 pure lines, `agent_skill_matrix.py` 208 pure lines, `agent_skill_evidence_io.py` 20 pure lines.
- No new abstraction beyond the small shared I/O helper. It replaces duplicated git and SHA helpers already present in the dirty diff.

Verification artifacts:
- Focused pytest: `commands/focused-pytest.txt`, exit 0, 18 passed.
- Ruff: `commands/ruff-check.txt`, exit 0.
- basedpyright: `commands/basedpyright.txt`, exit 0, 0 errors.
- Eval validation and generated catalog check: `commands/generated-stability.txt`, validation and catalog checks each ran twice, all exit 0, pre/post diff hash unchanged.
- Adversarial CLI smokes: `adversarial-cli/summary.json`, 8 assertions passed.
- Dry-run proof: `dry-run/summary.json`, install `planned`, matrix `validated`, neither `passed`.
- Scoped secret scan: `scoped-secret-scan.json`, 0 findings, 0 scan errors.

Decision: ready to commit as `fix(evals): fail closed on missing release evidence`.
