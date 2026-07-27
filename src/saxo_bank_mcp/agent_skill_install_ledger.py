from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp.agent_skill_install_models import (
    FIXTURE_TEARDOWN_OWNER,
    REQUIRED_FIXTURE_CONSUMERS,
    FixtureLedgerBinding,
)

LEDGER_EVENT: Final = "fixture_retained"
DEFAULT_RETENTION_DAYS: Final = 30


class FixtureLedgerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["fixture_retained"] = "fixture_retained"
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    run_root: str = Field(min_length=1)
    preserved_paths: tuple[str, ...] = Field(min_length=1)
    consumers: tuple[str, ...] = Field(min_length=1)
    teardown_owner: Literal["post-final-completion-gate"]
    owner_only: Literal[True]
    cleanup_deadline: str = Field(min_length=1)
    registered_at: str = Field(min_length=1)


def default_ledger_path(run_root: Path) -> Path:
    """Safe deterministic default: isolated under run_root, never the main worktree ledger."""
    return run_root.resolve() / "fixture-cleanup-ledger.jsonl"


def future_cleanup_deadline(*, days: int = DEFAULT_RETENTION_DAYS) -> str:
    return (datetime.now(tz=UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()


def append_fixture_ledger_event(  # noqa: PLR0913
    ledger_path: Path,
    *,
    candidate_commit: str,
    run_root: Path,
    preserved_paths: tuple[str, ...],
    consumers: tuple[str, ...],
    cleanup_deadline: str | None = None,
) -> FixtureLedgerBinding:
    if tuple(consumers) != REQUIRED_FIXTURE_CONSUMERS:
        msg = "consumers_mismatch"
        raise ValueError(msg)
    canonical_root = _canonical(run_root)
    event = FixtureLedgerEvent(
        candidate_commit=candidate_commit,
        run_root=canonical_root,
        preserved_paths=tuple(
            _canonical(Path(path)) if not Path(path).is_absolute() else str(Path(path).resolve())
            for path in preserved_paths
        ),
        consumers=REQUIRED_FIXTURE_CONSUMERS,
        teardown_owner=FIXTURE_TEARDOWN_OWNER,
        owner_only=True,
        cleanup_deadline=cleanup_deadline or future_cleanup_deadline(),
        registered_at=datetime.now(tz=UTC).replace(microsecond=0).isoformat(),
    )
    line = event.model_dump_json() + "\n"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return FixtureLedgerBinding(
        ledger_path=_public_ledger_path(ledger_path, run_root=run_root),
        event_sha256=hashlib.sha256(line.encode()).hexdigest(),
        candidate_commit=candidate_commit,
        consumers=REQUIRED_FIXTURE_CONSUMERS,
        teardown_owner=FIXTURE_TEARDOWN_OWNER,
        owner_only=True,
        cleanup_deadline=event.cleanup_deadline,
        # Privacy-safe: report stores ledger-relative path label, not absolute home roots.
        run_root="run_root",
    )


def verify_fixture_ledger_binding(  # noqa: PLR0911
    binding: FixtureLedgerBinding,
    *,
    candidate_commit: str,
    run_root: Path,
    ledger_path: Path | None = None,
) -> list[str]:
    path = ledger_path or _resolve_ledger_path(binding.ledger_path, run_root=run_root)
    if not path.is_file():
        return ["fixture_ledger_missing"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ["fixture_ledger_unreadable"]
    if not lines:
        return ["fixture_ledger_empty"]
    latest = lines[-1].strip()
    if not latest:
        return ["fixture_ledger_empty"]
    digest = hashlib.sha256((latest + "\n").encode()).hexdigest()
    if digest != binding.event_sha256:
        return ["fixture_ledger_digest_mismatch"]
    try:
        event = FixtureLedgerEvent.model_validate_json(latest)
    except ValidationError:
        return ["fixture_ledger_event_invalid"]
    return _event_field_errors(event, binding, candidate_commit=candidate_commit, run_root=run_root)


def _event_field_errors(
    event: FixtureLedgerEvent,
    binding: FixtureLedgerBinding,
    *,
    candidate_commit: str,
    run_root: Path,
) -> list[str]:
    errors: list[str] = []
    if (
        event.candidate_commit != candidate_commit
        or event.candidate_commit != binding.candidate_commit
    ):
        errors.append("fixture_ledger_commit_mismatch")
    if event.consumers != REQUIRED_FIXTURE_CONSUMERS or event.consumers != binding.consumers:
        errors.append("fixture_ledger_consumers_mismatch")
    if event.teardown_owner != FIXTURE_TEARDOWN_OWNER:
        errors.append("fixture_ledger_teardown_owner_invalid")
    if event.owner_only is not True or binding.owner_only is not True:
        errors.append("fixture_ledger_owner_only_invalid")
    if _canonical(Path(event.run_root)) != _canonical(run_root):
        errors.append("fixture_ledger_run_root_mismatch")
    if binding.run_root not in {"run_root", _canonical(run_root), str(run_root)}:
        errors.append("fixture_ledger_binding_run_root_invalid")
    if not _deadline_is_future(event.cleanup_deadline):
        errors.append("fixture_ledger_deadline_not_future")
    if event.cleanup_deadline != binding.cleanup_deadline:
        errors.append("fixture_ledger_deadline_mismatch")
    return errors


def _deadline_is_future(value: str) -> bool:
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline > datetime.now(tz=UTC)


def _canonical(path: Path) -> str:
    return str(path.expanduser().resolve())


def _public_ledger_path(ledger_path: Path, *, run_root: Path) -> str:
    resolved = ledger_path.resolve()
    root = run_root.resolve()
    if resolved == root or resolved.is_relative_to(root):
        relative = resolved.relative_to(root)
        return str(relative) if str(relative) != "." else "fixture-cleanup-ledger.jsonl"
    return resolved.name


def _resolve_ledger_path(reported: str, *, run_root: Path) -> Path:
    candidate = Path(reported)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    under_run = (run_root / reported).resolve()
    if under_run.is_file():
        return under_run
    return candidate
