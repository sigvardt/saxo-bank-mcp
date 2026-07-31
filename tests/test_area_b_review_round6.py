# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from pathlib import Path

import pytest
from analytics_source_matrix_support import (
    BOUNDARY_FORBIDDEN_SCALARS,
    BoundaryEventFixture,
)


@pytest.fixture
def boundary_fixture(tmp_path: Path) -> BoundaryEventFixture:
    return BoundaryEventFixture(
        root=tmp_path,
        forbidden_scalars=BOUNDARY_FORBIDDEN_SCALARS,
    )


@pytest.mark.anyio
async def test_postexit_static_change_wins_over_tool_exception_and_publication(
    boundary_fixture: BoundaryEventFixture,
) -> None:
    result = await boundary_fixture.run(
        child_scenario="postclaim",
        postexit_runtime_mutation="content",
    )

    assert result.command_exit_code != 0
    assert json.loads(result.published_text) == {
        "reason": "candidate_static_runtime_changed",
        "status": "failed",
    }
    assert result.requests_replayed == 0
    assert result.publication_count == 1


def test_evidence_publication_refuses_a_swapped_claimed_ancestor(
    boundary_fixture: BoundaryEventFixture,
) -> None:
    result = boundary_fixture.swap_parent_then_publish()

    assert result.command_exit_code != 0
    assert result.guard_path.is_file()
    assert not result.redirected_evidence.exists()
    assert not result.original_evidence.exists()
