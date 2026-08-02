from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pytest_disk_guard import (
    current_pytest_disk_guard_snapshot,
)
from pytest_disk_guard import (
    pytest_disk_guard_errors as disk_guard_errors,
)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    raw_basetemp = cast("object", config.getoption("basetemp"))
    basetemp = None if raw_basetemp is None else Path(str(raw_basetemp))
    errors = disk_guard_errors(current_pytest_disk_guard_snapshot(basetemp))
    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise pytest.UsageError(
            "pytest disk safety guard refused before test execution:\n"
            f"{details}\n"
            "Run scripts/run-pytest so all temporary files stay on /Volumes/ssd_1.",
        )
