from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def run_live_login(
    *,
    timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
) -> dict[str, JsonValue]:
    settings = resolve_live_oauth_settings()
    pending = prepare_live_login(settings)
    callback_targets: list[str] = []
    server = _callback_server(settings.redirect_uri, callback_targets)
    server.timeout = timeout_seconds
    if not webbrowser.open(pending.authorization_url, new=2):
        server.server_close()
        raise LiveLoginCallbackError("browser_open_failed")
    try:
        server.handle_request()
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


def _callback_server(redirect_uri: str, callback_targets: list[str]) -> HTTPServer:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise LiveLoginCallbackError("redirect_uri_must_be_local_http")
    if parsed.port is None:
        raise LiveLoginCallbackError("redirect_uri_port_missing")

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            callback_targets.append(self.path)
            accepted = urlparse(self.path).path == parsed.path
            self.send_response(200 if accepted else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = (
                "Saxo live login received. You can close this tab."
                if accepted
                else "Saxo live login failed. Return to the terminal."
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_live_login(timeout_seconds=float(args.timeout_seconds))
    except (LiveLoginCallbackError, OAuthRequestError) as error:
        sys.stdout.write(json.dumps({"status": "login_failed", "reason": str(error)}))
        sys.stdout.write("\n")
        return 1
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
