from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from saxo_bank_mcp.agent_skill_eval_runner import EvalRunOptions, run_eval_suite
from saxo_bank_mcp.agent_skill_router_eval_execution import (
    ROUTER_SOURCE_PATHS,
    RouterBindingRequest,
    RouterSourceBinding,
    resolve_router_source_binding,
    router_source_digest,
    router_source_file_digests,
)

ROOT: Final = Path(__file__).resolve().parents[1]
CASE_ROOT: Final = ROOT / "evals/saxo-bank"
GIT: Final = shutil.which("git") or "git"
BOTH_HARNESS_RECORD_COUNT: Final = 2


def test_empty_case_selection_fails_even_when_nonzero_on_skip_is_false(tmp_path: Path) -> None:
    # Given: a filter that selects no cases, with skip-failure disabled.
    out = tmp_path / "empty.json"
    options = _options(
        OptionsInput(
            out=out,
            case_id="does-not-exist",
            dry_run=True,
            nonzero_on_skip=False,
        ),
    )

    # When: the suite is run.
    code = run_eval_suite(options)
    payload = json.loads(out.read_text(encoding="utf-8"))

    # Then: empty selection is an unconditional failure.
    assert code != 0
    assert payload["status"] == "failed"
    assert payload["selected_case_count"] == 0
    assert payload["case_count"] == 0
    assert payload["records"] == []


def test_empty_tag_selection_fails_in_dry_run(tmp_path: Path) -> None:
    # Given: a nonexistent tag filter.
    out = tmp_path / "empty-tag.json"
    options = _options(
        OptionsInput(
            out=out,
            tag="does-not-exist-tag",
            dry_run=True,
            nonzero_on_skip=False,
        ),
    )

    # When: the suite is run.
    code = run_eval_suite(options)
    payload = json.loads(out.read_text(encoding="utf-8"))

    # Then: dry-run still fails rather than reporting planned/passed.
    assert code != 0
    assert payload["status"] == "failed"
    assert payload["selected_case_count"] == 0


def test_nonempty_dry_run_stays_planned(tmp_path: Path) -> None:
    # Given: one real case selected for dry-run planning.
    out = tmp_path / "planned.json"
    options = _options(
        OptionsInput(
            out=out,
            case_id="router-auth",
            dry_run=True,
            nonzero_on_skip=False,
        ),
    )

    # When: the suite is run.
    code = run_eval_suite(options)
    payload = json.loads(out.read_text(encoding="utf-8"))

    # Then: nonempty dry-run remains planned and succeeds.
    assert code == 0
    assert payload["status"] == "planned"
    assert payload["selected_case_count"] == 1
    assert payload["case_count"] == BOTH_HARNESS_RECORD_COUNT


def test_router_model_run_requires_matching_source_binding(tmp_path: Path) -> None:
    # Given: two plugin roots with different router source bytes.
    left = tmp_path / "left"
    right = tmp_path / "right"
    _copy_router_package(ROOT, left)
    _copy_router_package(ROOT, right)
    (right / "skills/saxo-bank/SKILL.md").write_text(
        (right / "skills/saxo-bank/SKILL.md").read_text(encoding="utf-8") + "\n# drift\n",
        encoding="utf-8",
    )
    out = tmp_path / "bound.json"
    commit = _git_head(ROOT)
    expected_digest = router_source_digest(ROOT)
    options = _options(
        OptionsInput(
            out=out,
            case_id="router-auth",
            dry_run=False,
            nonzero_on_skip=False,
            codex_plugin_root=left,
            claude_plugin_root=right,
            expected_source_commit=commit,
            expected_router_source_sha256=expected_digest,
        ),
    )

    # When: a model-backed router run is attempted.
    code = run_eval_suite(options)
    payload = json.loads(out.read_text(encoding="utf-8"))

    # Then: the suite fails before clients run and reports source mismatch.
    assert code != 0
    assert payload["status"] == "failed"
    assert payload["selected_case_count"] == 1
    assert payload["case_count"] == 0
    assert payload["records"] == []
    assert "source_binding" in payload["cleanup"]
    assert payload["cleanup"]["source_binding"]["status"] == "failed"


def test_installed_cache_roots_bind_public_bytes_to_expected_commit(tmp_path: Path) -> None:
    # Given: non-git cache roots whose router files match the expected commit digests.
    commit = _git_head(ROOT)
    expected = resolve_router_source_binding(
        RouterBindingRequest(
            repo=ROOT,
            expected_source_commit=commit,
            expected_router_source_sha256=None,
            codex_plugin_root=ROOT,
            claude_plugin_root=ROOT,
            require_git_checkout=False,
        ),
    )
    assert isinstance(expected, RouterSourceBinding)
    cache_a = tmp_path / "cache-a"
    cache_b = tmp_path / "cache-b"
    _write_router_files(cache_a, expected.file_contents)
    _write_router_files(cache_b, expected.file_contents)

    # When: both caches are validated against the expected commit bytes.
    bound = resolve_router_source_binding(
        RouterBindingRequest(
            repo=ROOT,
            expected_source_commit=commit,
            expected_router_source_sha256=expected.router_source_sha256,
            codex_plugin_root=cache_a,
            claude_plugin_root=cache_b,
            require_git_checkout=False,
        ),
    )

    # Then: both roots bind to the same commit and digest without being git checkouts.
    assert isinstance(bound, RouterSourceBinding)
    assert bound.source_commit == expected.source_commit
    assert bound.router_source_sha256 == expected.router_source_sha256
    assert bound.file_digests == expected.file_digests
    assert router_source_file_digests(cache_a) == bound.file_digests
    assert router_source_digest(cache_b) == bound.router_source_sha256


def test_expected_digest_must_match_commit_bytes() -> None:
    # Given: a correct commit paired with a wrong expected digest.
    commit = _git_head(ROOT)
    wrong = hashlib.sha256(b"not-the-router-source").hexdigest()

    # When: binding is resolved.
    result = resolve_router_source_binding(
        RouterBindingRequest(
            repo=ROOT,
            expected_source_commit=commit,
            expected_router_source_sha256=wrong,
            codex_plugin_root=ROOT,
            claude_plugin_root=ROOT,
            require_git_checkout=False,
        ),
    )

    # Then: binding fails before model work.
    assert isinstance(result, str)
    assert "expected_router_source_sha256" in result


def test_crlf_installed_cache_bytes_fail_source_binding_before_model(
    tmp_path: Path,
) -> None:
    # Given: installed-cache roots whose text matches the commit after newline
    # normalization, but whose on-disk bytes use CRLF instead of LF.
    commit = _git_head(ROOT)
    expected = resolve_router_source_binding(
        RouterBindingRequest(
            repo=ROOT,
            expected_source_commit=commit,
            expected_router_source_sha256=None,
            codex_plugin_root=ROOT,
            claude_plugin_root=ROOT,
            require_git_checkout=False,
        ),
    )
    assert isinstance(expected, RouterSourceBinding)
    cache_a = tmp_path / "crlf-a"
    cache_b = tmp_path / "crlf-b"
    _write_router_files_crlf(cache_a, expected.file_contents)
    _write_router_files_crlf(cache_b, expected.file_contents)
    out = tmp_path / "crlf-bound.json"
    options = _options(
        OptionsInput(
            out=out,
            case_id="router-auth",
            dry_run=False,
            nonzero_on_skip=False,
            codex_plugin_root=cache_a,
            claude_plugin_root=cache_b,
            expected_source_commit=commit,
            expected_router_source_sha256=expected.router_source_sha256,
        ),
    )

    # When: a model-backed router run is attempted against the CRLF caches.
    code = run_eval_suite(options)
    payload = json.loads(out.read_text(encoding="utf-8"))

    # Then: binding fails before any model call because the raw bytes differ.
    assert code != 0
    assert payload["status"] == "failed"
    assert payload["case_count"] == 0
    assert payload["records"] == []
    assert payload["cleanup"]["source_binding"]["status"] == "failed"
    assert "digest_mismatch" in payload["cleanup"]["source_binding"]["error"]


@dataclass(frozen=True, slots=True)
class OptionsInput:
    out: Path
    dry_run: bool
    nonzero_on_skip: bool
    case_id: str | None = None
    tag: str | None = None
    codex_plugin_root: Path = ROOT
    claude_plugin_root: Path = ROOT
    expected_source_commit: str | None = None
    expected_router_source_sha256: str | None = None


def _options(values: OptionsInput) -> EvalRunOptions:
    return EvalRunOptions(
        harness="both",
        case_id=values.case_id,
        tag=values.tag,
        environment=None,
        case_root=CASE_ROOT,
        codex_plugin_root=values.codex_plugin_root,
        claude_plugin_root=values.claude_plugin_root,
        codex_home=None,
        claude_home=None,
        out=values.out,
        dry_run=values.dry_run,
        nonzero_on_skip=values.nonzero_on_skip,
        expected_source_commit=values.expected_source_commit,
        expected_router_source_sha256=values.expected_router_source_sha256,
    )


def _git_head(repo: Path) -> str:
    return subprocess.check_output(
        [GIT, "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()


def _copy_router_package(source: Path, target: Path) -> None:
    for relative in ROUTER_SOURCE_PATHS:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / relative).read_bytes())


def _write_router_files(root: Path, contents: dict[str, str]) -> None:
    for relative, text in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def _write_router_files_crlf(root: Path, contents: dict[str, str]) -> None:
    for relative, text in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
