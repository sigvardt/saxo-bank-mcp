from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_codex_install import (
    CodexInstallEvidenceReport,
    load_verified_codex_install_report,
)
from saxo_bank_mcp.agent_skill_evidence_io import git_output
from saxo_bank_mcp.agent_skill_install_discovery import parse_mcp_server_count, skill_inventory
from saxo_bank_mcp.agent_skill_install_paths import (
    codex_global_state_fingerprint,
    export_publishable_tree,
    installed_inventory_check,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOL_COUNT = 60


def _build_report_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    run_root = tmp_path / "run"
    clone = run_root / "source-clone"
    git = shutil.which("git")
    assert git is not None
    subprocess.run(
        (git, "clone", "--no-local", "--quiet", str(ROOT), str(clone)),
        check=True,
        text=True,
        capture_output=True,
    )
    commit = git_output(clone, "rev-parse", "HEAD")
    assert commit is not None
    project = tomllib.loads((clone / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    home = run_root / "home"
    codex_home = run_root / "codex-home"
    cache = codex_home / "plugins" / "cache" / "sigvardt" / "saxo-bank-mcp" / version
    for path in (run_root, clone, home, codex_home):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    export_publishable_tree(clone, cache)
    (codex_home / "config.toml").write_text(
        '[plugins."saxo-bank-mcp@sigvardt"]\nenabled = true\n',
        encoding="utf-8",
    )
    inventory = installed_inventory_check(clone, cache)
    startup: dict[str, JsonValue] = {
        "source": {"status": "passed", "tool_count": 60, "annotations_missing": []},
        "cache": {"status": "passed", "tool_count": 60, "annotations_missing": []},
        "list_tools": {"status": "passed", "tool_count": 60, "annotations_missing": []},
    }
    receipt: dict[str, JsonValue] = {
        "name": "codex_plugin_add",
        "argv": ["codex", "plugin", "add"],
        "cwd": str(clone),
        "exit_code": 0,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
        "timed_out": False,
        "cleanup_attempted": True,
    }
    global_home = tmp_path / "global-codex"
    global_home.mkdir()
    state = codex_global_state_fingerprint(global_home)
    payload: dict[str, JsonValue] = {
        "status": "passed",
        "execution_mode": "codex_installed_verification",
        "harness_policy": "codex_native_v1",
        "repo": str(ROOT),
        "candidate_commit": commit,
        "clone": {
            "path": str(clone),
            "commit": commit,
            "source_repo": str(ROOT),
            "no_local": True,
            "clean": True,
            "mode": "0o700",
        },
        "expected_skills": 9,
        "expected_mcp_servers": 1,
        "expected_tools": 60,
        "global_codex_state": {
            "before": {"codex": state["codex"]},
            "after": {"codex": state["codex"]},
            "scope": state["scope"],
        },
        "global_codex_state_unchanged": True,
        "codex": {
            "installed": True,
            "cache_root": str(cache),
            "identity": "saxo-bank-mcp",
            "version": version,
            "cache_root_source": "codex_plugin_add",
            "skill_count": len(skill_inventory(cache)),
            "skills": list(skill_inventory(cache)),
            "mcp_server_count": parse_mcp_server_count(cache),
            "tool_count": 60,
            "annotations_missing": [],
            "source_annotations_missing": [],
            "cache_annotations_missing": [],
            "list_tools_annotations_missing": [],
            "forbidden_cache_paths": [],
            "installed_bytes_match": True,
            "install_command_exit_code": 0,
            "startup": startup,
            "command_receipts": [receipt],
            "inventory": inventory,
        },
        "installed_byte_checks": {
            "complete": True,
            "compared_files": inventory["compared_files"],
            "metadata_exceptions": inventory["metadata_exceptions"],
            "required_files_present": inventory["required_files_present"],
            "forbidden_files_absent": True,
            "mismatches": [],
            "inventory_exact_match": True,
        },
        "process_cleanup": {
            "complete": True,
            "remaining_pids": [],
            "remaining_pgids": [],
            "observed_pids": [],
            "observed_pgids": [],
        },
        "project_version": version,
        "run_root": str(run_root),
        "preserved_modes": {
            "run_root": "0o700",
            "clone": "0o700",
            "home": "0o700",
            "codex_home": "0o700",
            "codex_cache": "0o700",
        },
        "owner_only": True,
        "privacy_scan_passed": True,
        "errors": [],
    }
    report = tmp_path / "install.json"
    write_json(report, payload)
    report.chmod(0o600)
    return report, cache, global_home


def test_codex_install_report_has_no_claude_surface(tmp_path: Path) -> None:
    report_path, _cache, _global_home = _build_report_fixture(tmp_path)
    report = CodexInstallEvidenceReport.model_validate_json(
        report_path.read_text(encoding="utf-8"),
    )

    assert report.harness_policy == "codex_native_v1"
    assert report.codex.tool_count == EXPECTED_TOOL_COUNT
    payload = report.model_dump(mode="json")
    assert "claude" not in payload
    assert set(report.global_codex_state.before) == {"codex"}
    command_surface = json.dumps(
        [receipt.model_dump(mode="json") for receipt in report.codex.command_receipts],
    )
    assert "claude" not in command_surface.lower()


def test_codex_install_verifier_rejects_changed_cache_bytes(tmp_path: Path) -> None:
    report_path, cache, global_home = _build_report_fixture(tmp_path)
    (cache / "README.md").write_text("tampered\n", encoding="utf-8")

    report, errors = load_verified_codex_install_report(
        report_path,
        codex_global_home=global_home,
    )

    assert report is None
    assert "installed_inventory_mismatch" in errors


def test_codex_install_script_contains_no_claude_arguments() -> None:
    script = (ROOT / "scripts/qa_codex_plugin_install.py").read_text(encoding="utf-8")

    assert "--claude" not in script.lower()
    assert "claude_home" not in script.lower()
