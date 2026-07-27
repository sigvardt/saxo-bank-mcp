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
from saxo_bank_mcp.agent_skill_install_paths import MARKETPLACE_NAME, PLUGIN_NAME

LEDGER_EVENT: Final = "fixture_retained"
DEFAULT_RETENTION_DAYS: Final = 30
REQUIRED_PRESERVED_COUNT: Final = 6


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


def future_cleanup_deadline(*, days: int = DEFAULT_RETENTION_DAYS) -> str:
    return (datetime.now(tz=UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()


def expected_preserved_paths(run_root: Path, *, version: str) -> tuple[str, ...]:
    """Exact six canonical preserved absolute paths under run_root."""
    root = run_root.resolve()
    codex_cache = (
        root / "codex-home" / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME / version
    )
    claude_cache = (
        root / "home" / ".claude" / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME / version
    )
    return (
        str((root / "source-clone").resolve()),
        str(codex_cache.resolve()),
        str(claude_cache.resolve()),
        str((root / "home").resolve()),
        str((root / "codex-home").resolve()),
        str((root / "claude-home").resolve()),
    )


def ledger_path_digest(ledger_path: Path) -> str:
    return hashlib.sha256(str(ledger_path.resolve()).encode()).hexdigest()


def append_fixture_ledger_event(  # noqa: PLR0913
    ledger_path: Path,
    *,
    candidate_commit: str,
    run_root: Path,
    version: str,
    consumers: tuple[str, ...],
    cleanup_deadline: str | None = None,
    registered_at: str | None = None,
) -> FixtureLedgerBinding:
    """Main-thread helper only: append a fixture_retained event before evidence generation.

    real_install_report must not call this; it only binds an existing matching event.
    """
    if tuple(consumers) != REQUIRED_FIXTURE_CONSUMERS:
        msg = "consumers_mismatch"
        raise ValueError(msg)
    if not ledger_path.is_absolute():
        msg = "ledger_path_must_be_absolute"
        raise ValueError(msg)
    canonical_root = _canonical(run_root)
    preserved = expected_preserved_paths(run_root, version=version)
    now = datetime.now(tz=UTC).replace(microsecond=0)
    registered = registered_at or now.isoformat()
    deadline = cleanup_deadline or future_cleanup_deadline()
    event = FixtureLedgerEvent(
        candidate_commit=candidate_commit,
        run_root=canonical_root,
        preserved_paths=preserved,
        consumers=REQUIRED_FIXTURE_CONSUMERS,
        teardown_owner=FIXTURE_TEARDOWN_OWNER,
        owner_only=True,
        cleanup_deadline=deadline,
        registered_at=registered,
    )
    line = event.model_dump_json() + "\n"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return _binding_from_event_line(ledger_path, line, event)


def bind_existing_ledger_event(
    ledger_path: Path,
    *,
    candidate_commit: str,
    run_root: Path,
    version: str,
) -> tuple[FixtureLedgerBinding | None, list[str]]:
    """Require an existing matching ledger event; do not append."""
    if not ledger_path.is_absolute():
        return None, ["ledger_path_must_be_absolute"]
    if not ledger_path.is_file():
        return None, ["fixture_ledger_missing"]
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, ["fixture_ledger_unreadable"]
    match = _latest_matching_event(
        lines,
        candidate_commit=candidate_commit,
        run_root=run_root,
    )
    if match is None:
        return None, ["fixture_ledger_event_missing"]
    line, event = match
    binding = _binding_from_event_line(ledger_path, line, event)
    errors = _event_field_errors(
        event,
        binding,
        candidate_commit=candidate_commit,
        run_root=run_root,
        version=version,
    )
    return (binding, errors) if not errors else (None, errors)


def verify_fixture_ledger_binding(  # noqa: PLR0911
    binding: FixtureLedgerBinding,
    *,
    candidate_commit: str,
    run_root: Path,
    version: str,
    ledger_path: Path | None,
) -> list[str]:
    """Verify ledger binding against an explicit durable absolute ledger path."""
    if ledger_path is None:
        return ["fixture_ledger_required"]
    if not ledger_path.is_absolute():
        return ["fixture_ledger_path_not_absolute"]
    resolved = ledger_path.resolve()
    if not resolved.is_file():
        return ["fixture_ledger_missing"]
    if ledger_path_digest(resolved) != binding.ledger_path_sha256:
        return ["fixture_ledger_path_digest_mismatch"]
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ["fixture_ledger_unreadable"]
    match = _latest_matching_event(
        lines,
        candidate_commit=candidate_commit,
        run_root=run_root,
    )
    if match is None:
        return ["fixture_ledger_event_missing"]
    line, event = match
    digest = hashlib.sha256((line + "\n").encode()).hexdigest()
    if digest != binding.event_sha256:
        return ["fixture_ledger_digest_mismatch"]
    return _event_field_errors(
        event,
        binding,
        candidate_commit=candidate_commit,
        run_root=run_root,
        version=version,
    )


def _binding_from_event_line(
    ledger_path: Path,
    line: str,
    event: FixtureLedgerEvent,
) -> FixtureLedgerBinding:
    normalized = line if line.endswith("\n") else line + "\n"
    return FixtureLedgerBinding(
        ledger_path_sha256=ledger_path_digest(ledger_path),
        event_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
        candidate_commit=event.candidate_commit,
        consumers=event.consumers,
        teardown_owner=event.teardown_owner,
        owner_only=True,
        cleanup_deadline=event.cleanup_deadline,
        # Privacy-safe label only; absolute root lives in the external ledger event.
        run_root="run_root",
    )


def _latest_matching_event(
    lines: list[str],
    *,
    candidate_commit: str,
    run_root: Path,
) -> tuple[str, FixtureLedgerEvent] | None:
    canonical_root = _canonical(run_root)
    for raw in reversed(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            event = FixtureLedgerEvent.model_validate_json(line)
        except ValidationError:
            continue
        if event.event != LEDGER_EVENT:
            continue
        if event.candidate_commit != candidate_commit:
            continue
        if _canonical(Path(event.run_root)) != canonical_root:
            continue
        return line, event
    return None


def _event_field_errors(  # noqa: C901, PLR0912
    event: FixtureLedgerEvent,
    binding: FixtureLedgerBinding,
    *,
    candidate_commit: str,
    run_root: Path,
    version: str,
) -> list[str]:
    errors: list[str] = []
    canonical_root = _canonical(run_root)
    expected = expected_preserved_paths(run_root, version=version)
    if event.candidate_commit != candidate_commit or binding.candidate_commit != candidate_commit:
        errors.append("fixture_ledger_commit_mismatch")
    if (
        event.consumers != REQUIRED_FIXTURE_CONSUMERS
        or binding.consumers != REQUIRED_FIXTURE_CONSUMERS
    ):
        errors.append("fixture_ledger_consumers_mismatch")
    if (
        event.teardown_owner != FIXTURE_TEARDOWN_OWNER
        or binding.teardown_owner != FIXTURE_TEARDOWN_OWNER
    ):
        errors.append("fixture_ledger_teardown_owner_invalid")
    if event.owner_only is not True or binding.owner_only is not True:
        errors.append("fixture_ledger_owner_only_invalid")
    if _canonical(Path(event.run_root)) != canonical_root:
        errors.append("fixture_ledger_run_root_mismatch")
    if binding.run_root not in {"run_root", canonical_root}:
        errors.append("fixture_ledger_binding_run_root_invalid")
    if tuple(event.preserved_paths) != expected:
        errors.append("fixture_ledger_preserved_paths_mismatch")
    if len(event.preserved_paths) != REQUIRED_PRESERVED_COUNT:
        errors.append("fixture_ledger_preserved_paths_count_invalid")
    for path_str in event.preserved_paths:
        path = Path(path_str)
        if not path.is_absolute():
            errors.append("fixture_ledger_preserved_path_not_absolute")
            continue
        if not path.resolve().is_relative_to(Path(canonical_root)):
            errors.append("fixture_ledger_preserved_path_outside_run_root")
        if not path.exists():
            errors.append("fixture_ledger_preserved_path_missing")
    registered = _parse_time(event.registered_at)
    deadline = _parse_time(event.cleanup_deadline)
    now = datetime.now(tz=UTC)
    if registered is None:
        errors.append("fixture_ledger_registered_at_invalid")
    elif registered > now:
        errors.append("fixture_ledger_registered_at_future")
    if deadline is None:
        errors.append("fixture_ledger_deadline_invalid")
    elif registered is not None and deadline <= registered:
        errors.append("fixture_ledger_deadline_not_after_registered_at")
    elif deadline <= now:
        errors.append("fixture_ledger_deadline_not_future")
    if event.cleanup_deadline != binding.cleanup_deadline:
        errors.append("fixture_ledger_deadline_mismatch")
    return errors


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _canonical(path: Path) -> str:
    return str(path.expanduser().resolve())
