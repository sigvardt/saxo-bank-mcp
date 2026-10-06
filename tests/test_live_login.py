from __future__ import annotations

import json
import socket
import threading
from http import HTTPStatus
from pathlib import Path
from typing import Final
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

from saxo_bank_mcp import live_login
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.live_login import (
    LiveLoginCallbackError,
    authorization_url_announcer,
    main,
    parse_live_login_callback,
    prepare_live_login,
    run_live_login,
)
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings

OWNER_ONLY_FILE_MODE: Final = 0o600
ARGPARSE_USAGE_EXIT_CODE: Final = 2


class _FakeToken:
    def redacted_status(self) -> dict[str, str | bool]:
        return {"environment": "LIVE", "is_expired": False, "has_refresh_token": True}


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _configure_live_oauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redirect_uri: str = "http://localhost:8080/callback",
) -> None:
    credential_file = tmp_path / "live-credentials.json"
    credential_file.write_text(
        """{
  "AppKey": "sim-app-key",
  "GrantType": "PKCE",
  "AuthorizationEndpoint": "https://live.logonvalidation.net/authorize",
  "TokenEndpoint": "https://live.logonvalidation.net/token"
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", str(credential_file))
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(tmp_path / "live-token.json"))
    monkeypatch.setenv("SAXO_MCP_LIVE_REDIRECT_URI", redirect_uri)


def test_live_login_prepares_saxo_pkce_url_without_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_live_oauth(tmp_path, monkeypatch)

    pending = prepare_live_login(resolve_live_oauth_settings())
    parsed = urlparse(pending.authorization_url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.hostname == "live.logonvalidation.net"
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://localhost:8080/callback"]
    assert "scope" not in query
    assert pending.code_verifier not in pending.authorization_url


def test_live_login_callback_requires_matching_state() -> None:
    with pytest.raises(LiveLoginCallbackError, match="callback_state_mismatch"):
        parse_live_login_callback(
            "/callback?code=authorization-code&state=wrong-state",
            expected_state="expected-state",
            expected_path="/callback",
        )


def test_live_login_callback_returns_code_without_echoing_it() -> None:
    result = parse_live_login_callback(
        "/callback?code=authorization-code&state=expected-state",
        expected_state="expected-state",
        expected_path="/callback",
    )

    assert result == "authorization-code"


def test_live_login_callback_accepts_configured_path() -> None:
    result = parse_live_login_callback(
        "/saxo?code=authorization-code&state=expected-state",
        expected_state="expected-state",
        expected_path="/saxo",
    )

    assert result == "authorization-code"


def test_live_login_keeps_waiting_past_stray_requests_until_the_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a local callback port and a token exchange that records the received code.
    port = _free_local_port()
    _configure_live_oauth(tmp_path, monkeypatch, f"http://localhost:{port}/callback")
    exchanged_codes: list[str] = []
    stray_statuses: list[int] = []
    client_errors: list[BaseException] = []

    async def exchange(
        settings: SimAuthSettings,
        *,
        code: str,
        code_verifier: str,
        environment: str,
    ) -> _FakeToken:
        del settings, code_verifier, environment
        exchanged_codes.append(code)
        return _FakeToken()

    def save(path: Path, token: _FakeToken) -> None:
        del token
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)

    monkeypatch.setattr(live_login, "exchange_authorization_code", exchange)
    monkeypatch.setattr(live_login, "save_token_cache", save)

    def browser_side(url: str) -> None:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=5).close()
            try:
                urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5)
            except HTTPError as error:
                stray_statuses.append(error.code)
            state = parse_qs(urlparse(url).query)["state"][0]
            callback = f"http://127.0.0.1:{port}/callback?code=authorization-code&state={state}"
            with urlopen(callback, timeout=5) as response:  # noqa: S310
                assert response.status == HTTPStatus.OK
        except BaseException as error:  # noqa: BLE001
            client_errors.append(error)

    threads: list[threading.Thread] = []

    def open_url(url: str) -> bool:
        thread = threading.Thread(target=browser_side, args=(url,))
        thread.start()
        threads.append(thread)
        return True

    # When: an idle connection and a wrong-path request arrive before the real callback.
    result = run_live_login(timeout_seconds=15, open_url=open_url)
    for thread in threads:
        thread.join(timeout=10)

    # Then: the stray requests are ignored and the login completes with the callback code.
    assert client_errors == []
    assert stray_statuses == [404]
    assert exchanged_codes == ["authorization-code"]
    assert result["status"] == "live_token_cached"
    assert result["cache_owner_only"] is True


def test_live_login_times_out_when_no_callback_arrives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_local_port()
    _configure_live_oauth(tmp_path, monkeypatch, f"http://localhost:{port}/callback")

    with pytest.raises(LiveLoginCallbackError, match="callback_timeout"):
        run_live_login(timeout_seconds=0.2, open_url=lambda _url: True)


def test_live_login_announcer_writes_owner_only_url_file_and_stderr_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url_file = tmp_path / "login-url.txt"
    url = "https://live.logonvalidation.net/authorize?state=example"

    assert authorization_url_announcer(url_file)(url) is True

    assert url_file.read_text(encoding="utf-8") == f"{url}\n"
    assert url_file.stat().st_mode & 0o777 == OWNER_ONLY_FILE_MODE
    event = json.loads(capsys.readouterr().err)
    assert event == {"status": "authorization_url_ready", "authorization_url": url}


def test_live_login_announcer_refuses_a_symlinked_url_file(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("", encoding="utf-8")
    url_file = tmp_path / "login-url.txt"
    url_file.symlink_to(target)

    with pytest.raises(LiveLoginCallbackError, match="authorization_url_file_unwritable"):
        authorization_url_announcer(url_file)("https://live.logonvalidation.net/authorize")

    assert target.read_text(encoding="utf-8") == ""


def test_live_login_cli_requires_no_browser_for_url_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--url-file", str(tmp_path / "login-url.txt")])

    assert exit_info.value.code == ARGPARSE_USAGE_EXIT_CODE
