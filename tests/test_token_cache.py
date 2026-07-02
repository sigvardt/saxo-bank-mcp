from __future__ import annotations

import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.token_cache import (
    TokenCachePathError,
    delete_pending_authorization,
    save_token_cache,
    token_cache_path,
)

OWNER_FILE_MODE = 0o600
OWNER_DIR_MODE = 0o700


def test_pending_authorization_delete_fails_closed_on_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_os_error(_self: Path) -> None:
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", raise_os_error)

    delete_pending_authorization(tmp_path / "pending.json")


def test_token_cache_path_refuses_repo_and_common_sync_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(TokenCachePathError):
        token_cache_path(repo / ".saxo-token.json", repo_root=repo)
    with pytest.raises(TokenCachePathError):
        token_cache_path(home / "Desktop" / "saxo-token.json", repo_root=repo)


def test_token_cache_path_auto_discovers_repo_root_if_none_provided() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    inside_repo_path = repo_root / "tests" / "dummy-cache.json"

    with pytest.raises(TokenCachePathError) as error:
        token_cache_path(inside_repo_path, repo_root=None)

    assert "inside repository" in str(error.value)


def test_token_cache_save_uses_owner_only_file_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    monkeypatch.setenv("HOME", str(home))
    cache = token_cache_path(home / ".local/state/saxo-bank-mcp/token.json", repo_root=repo)
    token = SaxoTokenSet(
        access_token="access-token-value",  # noqa: S106
        refresh_token="refresh-token-value",  # noqa: S106
        code_verifier="verifier-value",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    save_token_cache(cache, token)

    assert stat.S_IMODE(cache.stat().st_mode) == OWNER_FILE_MODE
    assert stat.S_IMODE(cache.parent.stat().st_mode) == OWNER_DIR_MODE
    assert list(cache.parent.glob(f".{cache.name}.*.tmp")) == []


@pytest.mark.anyio
async def test_http_client_factory_uses_canonical_safe_defaults() -> None:
    client = create_async_client(base_url="https://example.invalid")

    try:
        assert client.follow_redirects is False
        assert str(client.base_url) == "https://example.invalid"
    finally:
        await client.aclose()


def test_network_code_uses_httpx2_factory_without_forbidden_clients() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("src/saxo_bank_mcp").glob("*.py")
    )

    assert "import requests" not in sources
    assert "import httpx\n" not in sources
    assert "from httpx " not in sources
    bare_async_client = re.compile(r"httpx2\.AsyncClient\(\s*\)")
    assert bare_async_client.search("httpx2.AsyncClient()") is not None
    assert bare_async_client.search(sources) is None
