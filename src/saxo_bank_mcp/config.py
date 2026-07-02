from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from saxo_bank_mcp.auth_status import (
    AuthStatusInputs,
    EffectiveReadEnvironment,
    SaxoAuthStatus,
    SimCredentialSource,
    build_auth_status,
)
from saxo_bank_mcp.credentials import CredentialFileError, parse_sim_pkce_credentials_file
from saxo_bank_mcp.token_cache import (
    TokenCacheInspection,
    TokenCachePathError,
    default_token_cache_path,
    inspect_token_cache,
    load_pending_authorization,
    pending_authorization_path,
    token_cache_path,
)


@unique
class SaxoEnvironment(StrEnum):
    SIM = "SIM"
    LIVE = "LIVE"


type SimAuthSettingsErrorCode = Literal[
    "sim_credentials_missing",
    "sim_redirect_uri_missing",
    "sim_endpoint_untrusted",
    "token_cache_path_refused",
]

DEFAULT_SIM_CREDENTIAL_FILE: Final = Path("/Users/user/Desktop/saxo_bank_mcp_DEMO_credentials.txt")
TRUSTED_SIM_AUTH_HOST: Final = "sim.logonvalidation.net"


class SaxoEndpoints(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorization_url: str
    token_url: str
    rest_base_url: str


@dataclass(frozen=True, slots=True)
class SimAuthSettingsError(Exception):
    code: SimAuthSettingsErrorCode
    detail: str


class SimAuthSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_key: str
    authorization_url: str
    token_url: str
    rest_base_url: str
    redirect_uri: str
    cache_path: Path


SIM_ENDPOINTS: Final = SaxoEndpoints(
    authorization_url="https://sim.logonvalidation.net/authorize",
    token_url="https://sim.logonvalidation.net/token",  # noqa: S106
    rest_base_url="https://gateway.saxobank.com/sim/openapi/",
)
LIVE_ENDPOINTS: Final = SaxoEndpoints(
    authorization_url="https://live.logonvalidation.net/authorize",
    token_url="https://live.logonvalidation.net/token",  # noqa: S106
    rest_base_url="https://gateway.saxobank.com/openapi",
)


class SaxoRuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_environment: SaxoEnvironment
    live_reads_enabled: bool
    sim_credentials_present: bool
    sim_credential_source: SimCredentialSource
    live_credentials_present: bool
    sim_redirect_uri_present: bool
    cache_path: Path
    token_cache_path_refused: bool

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        repo_root: Path | None = None,
    ) -> SaxoRuntimeConfig:
        source = os.environ if environ is None else environ
        requested = SaxoEnvironment(source.get("SAXO_MCP_ENVIRONMENT", "SIM").upper())
        cache_refused = False
        try:
            cache = token_cache_path(_requested_token_cache_path(source), repo_root=repo_root)
        except TokenCachePathError as error:
            cache = error.path.expanduser()
            cache_refused = True
        sim_credential_source = _sim_credential_source(source)
        redirect_uri = _sim_redirect_uri(source, cache, allow_pending=False)
        return cls(
            requested_environment=requested,
            live_reads_enabled=source.get("SAXO_MCP_ENABLE_LIVE_READS") == "1",
            sim_credentials_present=sim_credential_source != "missing",
            sim_credential_source=sim_credential_source,
            live_credentials_present=_credentials_present(source, "LIVE"),
            sim_redirect_uri_present=bool(redirect_uri),
            cache_path=cache,
            token_cache_path_refused=cache_refused,
        )

    def effective_read_environment(self) -> EffectiveReadEnvironment:
        match self.requested_environment:
            case SaxoEnvironment.SIM:
                return "SIM"
            case SaxoEnvironment.LIVE:
                if self.live_reads_enabled and self.live_credentials_present:
                    return "LIVE"
                return "LIVE_READ_DISABLED"

    def redacted_status(self) -> SaxoAuthStatus:
        cache: TokenCacheInspection
        if self.token_cache_path_refused:
            cache = {"present": False, "readable": False, "token": None}
            pending_pkce_authorization_present = False
        else:
            cache = inspect_token_cache(self.cache_path)
            pending_pkce_authorization_present = _pending_pkce_authorization_present(
                self.cache_path,
            )
        token = cache["token"]
        token_status = token.redacted_status() if token is not None else None
        return build_auth_status(
            AuthStatusInputs(
                requested_environment=self.requested_environment.value,
                effective_read_environment=self.effective_read_environment(),
                live_reads_enabled=self.live_reads_enabled,
                sim_credentials_present=self.sim_credentials_present,
                sim_credential_source=self.sim_credential_source,
                live_credentials_present=self.live_credentials_present,
                sim_redirect_uri_present=self.sim_redirect_uri_present,
                pending_pkce_authorization_present=pending_pkce_authorization_present,
                token_cache_path_refused=self.token_cache_path_refused,
                token_cache_present=cache["present"],
                token_cache_readable=cache["readable"],
                token_cache_expired=None if token_status is None else token_status["is_expired"],
                token_cache_refresh_supported=(
                    token.refresh_material() is not None if token is not None else None
                ),
                token_cache_environment=(
                    None if token_status is None else token_status["environment"]
                ),
                token_cache_path=str(self.cache_path),
            ),
        )


def environment_endpoints(environment: SaxoEnvironment) -> SaxoEndpoints:
    match environment:
        case SaxoEnvironment.SIM:
            return SIM_ENDPOINTS
        case SaxoEnvironment.LIVE:
            return LIVE_ENDPOINTS


def _pending_pkce_authorization_present(cache_path: Path) -> bool:
    try:
        return pending_authorization_path(cache_path).exists()
    except OSError:
        return False


def resolve_sim_auth_settings(
    environ: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
    require_redirect: bool = True,
    allow_pending_redirect: bool = True,
) -> SimAuthSettings:
    source = os.environ if environ is None else environ
    try:
        cache = token_cache_path(_requested_token_cache_path(source), repo_root=repo_root)
    except TokenCachePathError as error:
        raise SimAuthSettingsError("token_cache_path_refused", str(error)) from error
    redirect_uri = _sim_redirect_uri(source, cache, allow_pending=allow_pending_redirect)
    if require_redirect and not redirect_uri:
        raise SimAuthSettingsError(
            "sim_redirect_uri_missing",
            "SAXO_MCP_SIM_REDIRECT_URI is required and must match the Saxo app redirect URL",
        )

    app_key = _env_sim_app_key(source)
    endpoints = SIM_ENDPOINTS
    if app_key is None:
        credential_file = Path(
            source.get("SAXO_MCP_SIM_CREDENTIAL_FILE", str(DEFAULT_SIM_CREDENTIAL_FILE)),
        )
        try:
            parsed = parse_sim_pkce_credentials_file(credential_file)
        except CredentialFileError as error:
            raise SimAuthSettingsError(
                "sim_credentials_missing",
                "SIM PKCE credentials missing",
            ) from error
        app_key = parsed.app_key
        endpoints = SaxoEndpoints(
            authorization_url=parsed.auth_endpoint,
            token_url=parsed.token_endpoint,
            rest_base_url=SIM_ENDPOINTS.rest_base_url,
        )

    authorization_url = source.get("SAXO_MCP_SIM_AUTH_URL", endpoints.authorization_url)
    token_url = source.get("SAXO_MCP_SIM_TOKEN_URL", endpoints.token_url)
    _require_trusted_sim_endpoint("authorization", authorization_url)
    _require_trusted_sim_endpoint("token", token_url)
    return SimAuthSettings(
        app_key=app_key,
        authorization_url=authorization_url,
        token_url=token_url,
        rest_base_url=SIM_ENDPOINTS.rest_base_url,
        redirect_uri=redirect_uri,
        cache_path=cache,
    )


def _credentials_present(environ: Mapping[str, str], prefix: Literal["SIM", "LIVE"]) -> bool:
    client_id = environ.get(f"SAXO_MCP_{prefix}_CLIENT_ID", "")
    client_secret = environ.get(f"SAXO_MCP_{prefix}_CLIENT_SECRET", "")
    return bool(client_id.strip() and client_secret.strip())


def _requested_token_cache_path(environ: Mapping[str, str]) -> Path:
    if "SAXO_MCP_TOKEN_CACHE_PATH" in environ:
        return Path(environ["SAXO_MCP_TOKEN_CACHE_PATH"])
    return default_token_cache_path()


def _env_sim_app_key(environ: Mapping[str, str]) -> str | None:
    for key in ("SAXO_MCP_SIM_APP_KEY", "SAXO_MCP_SIM_CLIENT_ID"):
        value = environ.get(key, "").strip()
        if value:
            return value
    return None


def _sim_redirect_uri(
    environ: Mapping[str, str],
    cache_path: Path,
    *,
    allow_pending: bool = True,
) -> str:
    configured = environ.get("SAXO_MCP_SIM_REDIRECT_URI", "").strip()
    if configured:
        return configured
    if not allow_pending:
        return ""
    pending = load_pending_authorization(pending_authorization_path(cache_path))
    if pending is None:
        return ""
    return pending.redirect_uri


def _require_trusted_sim_endpoint(label: str, url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc.lower() == TRUSTED_SIM_AUTH_HOST:
        return
    raise SimAuthSettingsError(
        "sim_endpoint_untrusted",
        f"SIM {label} endpoint must use https://{TRUSTED_SIM_AUTH_HOST}",
    )


def _sim_credential_source(environ: Mapping[str, str]) -> SimCredentialSource:
    if _env_sim_app_key(environ) is not None:
        return "env"

    credential_file = Path(
        environ.get("SAXO_MCP_SIM_CREDENTIAL_FILE", str(DEFAULT_SIM_CREDENTIAL_FILE)),
    )
    if not credential_file.exists():
        return "missing"
    try:
        parse_sim_pkce_credentials_file(credential_file)
    except CredentialFileError:
        return "missing"
    return "file"
