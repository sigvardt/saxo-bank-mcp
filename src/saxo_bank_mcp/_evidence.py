from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | Sequence[JsonValue] | Mapping[str, JsonValue]


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def archive_if_changed(path: Path, next_text: str) -> None:
    if not path.exists():
        return
    previous_text = path.read_text(encoding="utf-8")
    if previous_text == next_text:
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path.with_name(f"{path.name}.{stamp}.bak").write_text(previous_text, encoding="utf-8")


def write_json(path: Path, data: Mapping[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    next_text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    archive_if_changed(path, next_text)
    path.write_text(next_text, encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    archive_if_changed(path, text)
    path.write_text(text, encoding="utf-8")
