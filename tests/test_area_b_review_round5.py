from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).parents[1]


def test_installed_launcher_builds_a_fresh_locked_isolated_closure() -> None:
    preparation = (_ROOT / "scripts/prepare_analytics_source_matrix_runtime.py").read_text(
        encoding="utf-8",
    )
    assert 'runtime / "_python"' in preparation
    assert '"venv"' in preparation
    assert '"--copies"' in preparation
    assert '"--without-pip"' in preparation
    assert "_remove_python_caches(runtime)" in preparation
    assert "_seal_runtime(runtime)" in preparation

    for name, module in (
        ("saxo-bank-analytics-source-matrix", "saxo_bank_mcp.qa_analytics_source_matrix"),
        (
            "saxo-bank-analytics-source-matrix-generate",
            "saxo_bank_mcp.generate_analytics_source_matrix_candidate",
        ),
    ):
        launcher = (_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert ".saxo-source-matrix-run.$$" in launcher
        assert all(
            directory in launcher
            for directory in (
                "coordinator-cache",
                "coordinator-work",
                "coordinator-tmp",
            )
        )
        assert '"$python" -I -B -S' in launcher
        assert "os.rmdir(path)" in launcher
        assert module in launcher
        assert ".saxo-bank-mcp-pycache" not in launcher
