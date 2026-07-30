from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Final
from urllib.parse import urlparse

_DEFAULT_PAGE_LIMIT: Final = 100
_MAX_PAGE_LIMIT: Final = 1_000

type PaginationFetcher[PageValue] = Callable[
    [str],
    Awaitable[Mapping[str, PageValue]],
]


class PaginationError(RuntimeError):
    """Base error for rejected Saxo pagination."""

    def __init__(self, code: str, message: str) -> None:
        """Retain a stable code without retaining a returned link or payload."""
        super().__init__(message)
        self.code = code


class UnsafePaginationLinkError(PaginationError):
    """Raised when a returned continuation link is not a safe relative route."""

    def __init__(self) -> None:
        """Reject the link without retaining its potentially sensitive value."""
        super().__init__(
            "unsafe_pagination_link",
            "returned pagination link is not a safe registered relative route",
        )


class PaginationCycleError(PaginationError):
    """Raised when Saxo returns the same continuation link more than once."""

    def __init__(self, page_number: int) -> None:
        """Retain only the safe page number where the cycle was detected."""
        super().__init__(
            "pagination_cycle_detected",
            "returned pagination link repeated",
        )
        self.page_number = page_number


class PaginationLimitError(PaginationError):
    """Raised when another returned page would exceed the fixed bound."""

    def __init__(self, page_limit: int) -> None:
        """Retain the fixed limit without retaining the continuation link."""
        super().__init__(
            "pagination_page_limit_exceeded",
            "returned pagination exceeds the configured page limit",
        )
        self.page_limit = page_limit


class DuplicateSourcePageError(PaginationError):
    """Raised when two pages contain identical source rows."""

    def __init__(self, page_number: int) -> None:
        """Retain only the safe page number of the duplicate."""
        super().__init__(
            "duplicate_source_page",
            "a returned source page duplicates an earlier page",
        )
        self.page_number = page_number


class InvalidPaginationPayloadError(PaginationError):
    """Raised when pagination metadata has an invalid JSON type."""

    def __init__(self, code: str) -> None:
        """Retain a structural code without retaining invalid metadata."""
        super().__init__(code, "returned pagination metadata is invalid")


async def follow_registered_pagination[PageValue](
    first_page: Mapping[str, PageValue],
    fetch_next: PaginationFetcher[PageValue],
    *,
    max_pages: int = _DEFAULT_PAGE_LIMIT,
) -> AsyncIterator[Mapping[str, PageValue]]:
    """Yield Saxo pages in returned order and follow only returned relative links."""
    if max_pages < 1 or max_pages > _MAX_PAGE_LIMIT:
        raise ValueError("max_pages must be between 1 and 1000")
    current = first_page
    seen_links: set[str] = set()
    seen_page_fingerprints: set[str] = set()
    page_number = 0
    while True:
        page_number += 1
        next_link = _returned_next_link(current)
        if next_link is not None:
            _require_relative_link(next_link)
        page_fingerprint = _source_rows_fingerprint(current)
        if page_fingerprint in seen_page_fingerprints:
            raise DuplicateSourcePageError(page_number)
        seen_page_fingerprints.add(page_fingerprint)
        yield current

        if next_link is None:
            return
        if page_number >= max_pages:
            raise PaginationLimitError(max_pages)
        if next_link in seen_links:
            raise PaginationCycleError(page_number)
        seen_links.add(next_link)
        current = await fetch_next(next_link)


def _returned_next_link(page: Mapping[str, object]) -> str | None:
    value = page.get("__next")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidPaginationPayloadError("next_link_not_string")
    return value


def _require_relative_link(link: str) -> None:
    parsed = urlparse(link)
    if parsed.scheme or parsed.netloc or not link.startswith("/") or link.startswith("//"):
        raise UnsafePaginationLinkError
    if not parsed.path or any(part in {".", ".."} for part in parsed.path.split("/")):
        raise UnsafePaginationLinkError
    if "%" in parsed.path or any(not character.isprintable() for character in link):
        raise UnsafePaginationLinkError


def _source_rows_fingerprint(page: Mapping[str, object]) -> str:
    material: object
    if "Data" in page:
        material = page["Data"]
    else:
        material = {
            key: value for key, value in page.items() if key not in {"__next", "__count", "MaxRows"}
        }
    serialized = json.dumps(
        material,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()
