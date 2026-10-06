from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlparse

import anyio

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SaxoEnvironment, SimAuthSettings
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings
from saxo_bank_mcp.oauth import OAuthRequestError, exchange_authorization_code
from saxo_bank_mcp.pkce import (
    AuthorizationUrlRequest,
    build_authorization_url,
    create_pkce_pair,
    create_state,
)
from saxo_bank_mcp.token_cache import save_token_cache

DEFAULT_LOGIN_TIMEOUT_SECONDS: Final = 3600.0
CALLBACK_REQUEST_TIMEOUT_SECONDS: Final = 10.0


@dataclass(frozen=True, slots=True)
class PreparedLiveLogin:
    authorization_url: str
    state: str
    code_verifier: str


class LiveLoginCallbackError(Exception):
    pass


def prepare_live_login(settings: SimAuthSettings) -> PreparedLiveLogin:
    pkce = create_pkce_pair()
    state = create_state()
    return PreparedLiveLogin(
        authorization_url=build_authorization_url(
            AuthorizationUrlRequest(
                environment=SaxoEnvironment.LIVE,
                client_id=settings.app_key,
                redirect_uri=settings.redirect_uri,
                pkce=pkce,
                state=state,
                authorization_url=settings.authorization_url,
            ),
        ),
        state=state,
        code_verifier=pkce.verifier,
    )


def parse_live_login_callback(
    target: str,
    *,
    expected_state: str,
    expected_path: str,
) -> str:
    parsed = urlparse(target)
    query = parse_qs(parsed.query)
    if parsed.path != expected_path:
        raise LiveLoginCallbackError("callback_path_mismatch")
    if query.get("state") != [expected_state]:
        raise LiveLoginCallbackError("callback_state_mismatch")
    if query.get("error"):
        raise LiveLoginCallbackError("authorization_rejected")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise LiveLoginCallbackError("authorization_code_missing")
    return codes[0]


def open_in_browser(url: str) -> bool:
    return webbrowser.open(url, new=2)


def authorization_url_announcer(url_file: Path | None) -> Callable[[str], bool]:
    def announce(url: str) -> bool:
        if url_file is not None:
            try:
                _write_owner_only(url_file, f"{url}\n")
            except OSError as error:
                raise LiveLoginCallbackError("authorization_url_file_unwritable") from error
        event = {"status": "authorization_url_ready", "authorization_url": url}
        sys.stderr.write(json.dumps(event))
        sys.stderr.write("\n")
        sys.stderr.flush()
        return True

    return announce


def run_live_login(
    *,
    timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    open_url: Callable[[str], bool] = open_in_browser,
) -> dict[str, JsonValue]:
    settings = resolve_live_oauth_settings()
    pending = prepare_live_login(settings)
    callback_targets: list[str] = []
    server = _callback_server(settings.redirect_uri, callback_targets)
    try:
        if not open_url(pending.authorization_url):
            raise LiveLoginCallbackError("browser_open_failed")
        _wait_for_callback(server, callback_targets, timeout_seconds)
    finally:
        server.server_close()
    if not callback_targets:
        raise LiveLoginCallbackError("callback_timeout")
    code = parse_live_login_callback(
        callback_targets[0],
        expected_state=pending.state,
        expected_path=urlparse(settings.redirect_uri).path,
    )
    exchange = partial(
        exchange_authorization_code,
        settings,
        code=code,
        code_verifier=pending.code_verifier,
        environment="LIVE",
    )
    token = anyio.run(exchange)
    save_token_cache(settings.cache_path, token)
    status = token.redacted_status()
    return {
        "status": "live_token_cached",
        "environment": status["environment"],
        "is_expired": status["is_expired"],
        "has_refresh_token": status["has_refresh_token"],
        "cache_owner_only": (settings.cache_path.stat().st_mode & 0o077) == 0,
    }


def _wait_for_callback(
    server: HTTPServer,
    callback_targets: list[str],
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not callback_targets:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        server.timeout = remaining
        server.handle_request()


def _write_owner_only(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


def _callback_server(redirect_uri: str, callback_targets: list[str]) -> HTTPServer:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise LiveLoginCallbackError("redirect_uri_must_be_local_http")
    if parsed.port is None:
        raise LiveLoginCallbackError("redirect_uri_port_missing")

    class CallbackHandler(BaseHTTPRequestHandler):
        timeout = CALLBACK_REQUEST_TIMEOUT_SECONDS

        def do_GET(self) -> None:
            accepted = urlparse(self.path).path == parsed.path
            if accepted:
                callback_targets.append(self.path)
            self.send_response(200 if accepted else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = (
                "Saxo live login received. You can close this tab."
                if accepted
                else "This is not the Saxo live login callback."
            )
            self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode())

        def log_message(
            self,
            format: str,  # noqa: A002
            *_args: str | float | None,
        ) -> None:
            del format

    return HTTPServer(("127.0.0.1", parsed.port), CallbackHandler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Complete Saxo LIVE PKCE login and cache owner-only tokens.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_LOGIN_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser on this machine; print the authorization URL as a "
        "JSON line on stderr instead, for headless hosts.",
    )
    parser.add_argument(
        "--url-file",
        type=Path,
        help="With --no-browser, also write the authorization URL to this owner-only file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    url_file: Path | None = args.url_file
    if url_file is not None and not args.no_browser:
        parser.error("--url-file requires --no-browser")
    open_url = authorization_url_announcer(url_file) if args.no_browser else open_in_browser
    try:
        result = run_live_login(timeout_seconds=float(args.timeout_seconds), open_url=open_url)
    except (LiveLoginCallbackError, OAuthRequestError) as error:
        sys.stdout.write(json.dumps({"status": "login_failed", "reason": str(error)}))
        sys.stdout.write("\n")
        return 1
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
