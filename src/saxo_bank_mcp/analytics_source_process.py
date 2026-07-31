from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SimAuthSettingsError, resolve_sim_auth_settings
from saxo_bank_mcp.config_credentials import DEFAULT_SIM_CREDENTIAL_FILE
from saxo_bank_mcp.endpoint_registry import load_inventory
from saxo_bank_mcp.server_eval_tool_filter import (
    EVAL_ALLOWED_TOOLS_ENV,
    EVAL_TOOL_FILTER_FLAG,
    EvalToolFilterError,
    derive_eval_tool_filter_env,
    resolve_eval_tool_filter,
)

SOURCE_MATRIX_CHILD_TOOLS: Final[tuple[str, ...]] = (
    "saxo_auth_status",
    "saxo_list_registered_endpoints",
    "saxo_get_session_capabilities",
    "saxo_get_entitlements",
    "saxo_get_safe_request_ledger",
    "saxo_call_registered_endpoint",
)


def _digest(value: JsonValue) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


CHILD_TOOL_IDS_SHA256: Final = _digest(tuple(sorted(SOURCE_MATRIX_CHILD_TOOLS)))

type ChildConfigurationReason = Literal[
    "ambient_filter_configuration",
    "environment_not_sim",
    "live_reads_enabled",
    "live_writes_enabled",
    "child_environment_invalid",
    "child_path_invalid",
    "registered_call_profile_invalid",
    "tool_call_profile_invalid",
]

type LedgerPhase = Literal["clear", "readback", "complete"]
type RegisteredResponseMode = Literal["fingerprint_only", "analytics_contract_receipt"]

_SIM_ENVIRONMENT: Final = "SAXO_MCP_ENVIRONMENT"
_LIVE_READS_ENV: Final = "SAXO_MCP_ENABLE_LIVE_READS"
_LIVE_WRITES_ENV: Final = "SAXO_MCP_ENABLE_LIVE_WRITES"
_SIM_APP_KEY_ENV: Final = "SAXO_MCP_SIM_APP_KEY"
_SIM_CLIENT_ID_ENV: Final = "SAXO_MCP_SIM_CLIENT_ID"
_SIM_CREDENTIAL_FILE_ENV: Final = "SAXO_MCP_SIM_CREDENTIAL_FILE"
_SIM_REDIRECT_URI_ENV: Final = "SAXO_MCP_SIM_REDIRECT_URI"
_SIM_AUTH_URL_ENV: Final = "SAXO_MCP_SIM_AUTH_URL"
_SIM_TOKEN_URL_ENV: Final = "SAXO_MCP_SIM_TOKEN_URL"  # noqa: S105
_TOKEN_CACHE_PATH_ENV: Final = "SAXO_MCP_TOKEN_CACHE_PATH"  # noqa: S105
_TMPDIR_ENV: Final = "TMPDIR"
_REGISTRY_PAGE_SIZE: Final = 100
_SSL_ENV_KEYS: Final[frozenset[Literal["SSL_CERT_FILE", "SSL_CERT_DIR"]]] = frozenset(
    {"SSL_CERT_FILE", "SSL_CERT_DIR"},
)
_CHILD_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        _SIM_ENVIRONMENT,
        _LIVE_READS_ENV,
        _LIVE_WRITES_ENV,
        EVAL_TOOL_FILTER_FLAG,
        EVAL_ALLOWED_TOOLS_ENV,
        _SIM_APP_KEY_ENV,
        _SIM_CLIENT_ID_ENV,
        _SIM_CREDENTIAL_FILE_ENV,
        _SIM_REDIRECT_URI_ENV,
        _SIM_AUTH_URL_ENV,
        _SIM_TOKEN_URL_ENV,
        _TOKEN_CACHE_PATH_ENV,
        _TMPDIR_ENV,
        "LC_ALL",
        "LANG",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    },
)

CHILD_BOOTSTRAP: Final = """import os
import runpy
import sys
from pathlib import Path

def checked_path(raw, directory):
    path = Path(raw)
    if not path.is_absolute() or any(part == \"..\" for part in path.parts):
        raise ValueError(\"child_bootstrap_refused\")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index + 1 < len(path.parts[1:]) or directory:
                flags |= os.O_DIRECTORY
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return Path(os.path.realpath(path))
    finally:
        os.close(descriptor)

if len(sys.argv) != 7:
    raise SystemExit(\"child_bootstrap_refused\")
try:
    (
        _,
        runtime_root_raw,
        executable_raw,
        site_packages_raw,
        pycache_prefix_raw,
        workdir_raw,
        tmpdir_raw,
    ) = sys.argv
    runtime_root = checked_path(runtime_root_raw, True)
    executable = checked_path(executable_raw, False)
    site_packages = checked_path(site_packages_raw, True)
    pycache_prefix = checked_path(pycache_prefix_raw, True)
    workdir = checked_path(workdir_raw, True)
    tmpdir = checked_path(tmpdir_raw, True)
    prefix = checked_path(sys.prefix, True)
    base_prefix = checked_path(sys.base_prefix, True)
    current_executable = checked_path(sys.executable, False)
    required_flags = (
        sys.flags.isolated == 1,
        sys.flags.dont_write_bytecode == 1,
        sys.flags.no_site == 1,
        sys.flags.ignore_environment == 1,
        sys.flags.safe_path is True,
    )
    required_paths = (
        os.path.samefile(current_executable, executable),
        prefix.is_relative_to(runtime_root),
        base_prefix.is_relative_to(runtime_root),
        site_packages.is_relative_to(runtime_root),
        Path(sys.pycache_prefix or \"\") == pycache_prefix,
        Path.cwd() == workdir,
        Path(os.environ.get(\"TMPDIR\", \"\")) == tmpdir,
    )
except (OSError, ValueError):
    raise SystemExit(\"child_bootstrap_refused\") from None
if not all(required_flags) or not all(required_paths):
    raise SystemExit(\"child_bootstrap_refused\")
sys.path.insert(0, os.fspath(site_packages))
sys.argv[:] = [\"saxo-bank-mcp\", \"--transport\", \"stdio\"]
runpy.run_module(\"saxo_bank_mcp.server\", run_name=\"__main__\", alter_sys=True)
"""


@dataclass(frozen=True, slots=True)
class ChildConfigurationError(ValueError):
    reason: ChildConfigurationReason

    def __str__(self) -> str:  # noqa: D105
        return self.reason


@dataclass(frozen=True, slots=True)
class ChildBootstrapPaths:
    runtime_root: Path = field(repr=False)
    executable: Path = field(repr=False)
    site_packages: Path = field(repr=False)
    pycache_prefix: Path = field(repr=False)
    workdir: Path = field(repr=False)
    tmpdir: Path = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChildLaunchConfig:
    command: tuple[str, ...] = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    cwd: Path = field(repr=False)
    executable_identity_sha256: str


@dataclass(frozen=True, slots=True)
class RegisteredCallProfile:
    path: str
    params: Mapping[str, str]
    response_mode: RegisteredResponseMode
    analytics_contract_id: str | None

    def __post_init__(self) -> None:  # noqa: D105
        if (
            not self.path.startswith("/")
            or "?" in self.path
            or "://" in self.path
            or self.response_mode not in {"fingerprint_only", "analytics_contract_receipt"}
            or any(not key or not value for key, value in self.params.items())
        ):
            raise ChildConfigurationError("registered_call_profile_invalid")


class MatrixSession(Protocol):
    async def list_tools_once(self) -> tuple[str, ...]:
        raise NotImplementedError

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        raise NotImplementedError


@dataclass(slots=True)
class MatrixCallPolicy:
    registry_page_offsets: Mapping[str, tuple[int, ...]]
    registered_calls: tuple[RegisteredCallProfile, ...]
    _registry_positions: dict[str, int] = field(init=False, repr=False)
    _ledger_phase: LedgerPhase = field(init=False, repr=False, default="clear")

    def __post_init__(self) -> None:  # noqa: D105
        self._registry_positions = {}
        for service_group, offsets in self.registry_page_offsets.items():
            if (
                not service_group
                or not offsets
                or any(offset < 0 or offset % _REGISTRY_PAGE_SIZE != 0 for offset in offsets)
                or tuple(sorted(set(offsets))) != offsets
            ):
                raise ChildConfigurationError("registered_call_profile_invalid")

    @classmethod
    def from_local_registry(
        cls,
        registered_calls: tuple[RegisteredCallProfile, ...],
    ) -> MatrixCallPolicy:
        inventory = load_inventory()
        offsets = {
            group: tuple(range(0, count, _REGISTRY_PAGE_SIZE)) or (0,)
            for group, count in inventory.service_group_counts.items()
        }
        return cls(registry_page_offsets=offsets, registered_calls=registered_calls)

    def validate(self, name: str, arguments: dict[str, JsonValue]) -> None:
        if name in {
            "saxo_auth_status",
            "saxo_get_session_capabilities",
            "saxo_get_entitlements",
        }:
            self._require_exact_arguments(arguments, {})
            return
        if name == "saxo_get_safe_request_ledger":
            self._validate_ledger(arguments)
            return
        if name == "saxo_list_registered_endpoints":
            self._validate_registry_page(arguments)
            return
        if name == "saxo_call_registered_endpoint":
            self._validate_registered_call(arguments)
            return
        raise ChildConfigurationError("tool_call_profile_invalid")

    def observe(
        self,
        name: str,
        arguments: dict[str, JsonValue],
        payload: Mapping[str, JsonValue],
    ) -> None:
        if name != "saxo_list_registered_endpoints":
            return
        service_group, offsets, position = self._registered_page(arguments)
        expected = offsets[position + 1] if position + 1 < len(offsets) else None
        actual = payload.get("next_offset")
        if expected is None:
            if "next_offset" in payload and actual is not None:
                raise ChildConfigurationError("tool_call_profile_invalid")
        elif actual != expected or not isinstance(actual, int) or isinstance(actual, bool):
            raise ChildConfigurationError("tool_call_profile_invalid")
        self._registry_positions[service_group] = position + 1

    def _validate_ledger(self, arguments: dict[str, JsonValue]) -> None:
        if self._ledger_phase == "clear":
            self._require_exact_arguments(arguments, {"clear": True})
            self._ledger_phase = "readback"
            return
        if self._ledger_phase == "readback":
            self._require_exact_arguments(arguments, {})
            self._ledger_phase = "complete"
            return
        raise ChildConfigurationError("tool_call_profile_invalid")

    def _validate_registry_page(self, arguments: dict[str, JsonValue]) -> None:
        self._registered_page(arguments)

    def _registered_page(
        self,
        arguments: Mapping[str, JsonValue],
    ) -> tuple[str, tuple[int, ...], int]:
        service_group = arguments.get("service_group")
        limit = arguments.get("limit")
        offset = arguments.get("offset")
        if (
            set(arguments) != {"service_group", "limit", "offset"}
            or not isinstance(service_group, str)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit != _REGISTRY_PAGE_SIZE
            or not isinstance(offset, int)
            or isinstance(offset, bool)
        ):
            raise ChildConfigurationError("tool_call_profile_invalid")
        offsets = self.registry_page_offsets.get(service_group)
        position = self._registry_positions.get(service_group, 0)
        if offsets is None or position >= len(offsets) or offset != offsets[position]:
            raise ChildConfigurationError("tool_call_profile_invalid")
        return service_group, offsets, position

    def _validate_registered_call(self, arguments: dict[str, JsonValue]) -> None:
        for profile in self.registered_calls:
            expected: dict[str, JsonValue] = {
                "method": "GET",
                "path": profile.path,
                "response_mode": profile.response_mode,
            }
            if profile.params:
                expected["params"] = dict(profile.params)
            if profile.analytics_contract_id is not None:
                expected["analytics_contract_id"] = profile.analytics_contract_id
            if dict(arguments) == expected:
                return
        raise ChildConfigurationError("tool_call_profile_invalid")

    @staticmethod
    def _require_exact_arguments(
        arguments: Mapping[str, JsonValue],
        expected: Mapping[str, JsonValue],
    ) -> None:
        if dict(arguments) != dict(expected):
            raise ChildConfigurationError("tool_call_profile_invalid")


def build_child_environment(
    caller_env: Mapping[str, str],
    *,
    tmpdir: Path,
    ssl_runtime_entry: tuple[Literal["SSL_CERT_FILE", "SSL_CERT_DIR"], Path] | None = None,
) -> dict[str, str]:
    """Return the complete child environment or refuse without retaining values."""
    _require_sim_caller_environment(caller_env)
    if not tmpdir.is_absolute():
        raise ChildConfigurationError("child_environment_invalid")
    try:
        _canonical_no_follow_path(tmpdir, require_directory=True)
        settings = resolve_sim_auth_settings(caller_env)
        credential_key, credential_value = _child_credential_source(caller_env)
        _require_runtime_ssl_entry(ssl_runtime_entry)
    except (OSError, SimAuthSettingsError, ValueError):
        raise ChildConfigurationError("child_environment_invalid") from None
    base = {
        _SIM_ENVIRONMENT: "SIM",
        _LIVE_READS_ENV: "0",
        _LIVE_WRITES_ENV: "",
        credential_key: credential_value,
        _SIM_REDIRECT_URI_ENV: settings.redirect_uri,
        _SIM_AUTH_URL_ENV: settings.authorization_url,
        _SIM_TOKEN_URL_ENV: settings.token_url,
        _TOKEN_CACHE_PATH_ENV: str(settings.cache_path),
        _TMPDIR_ENV: str(tmpdir),
        "LC_ALL": "C",
        "LANG": "C",
    }
    if ssl_runtime_entry is not None:
        key, path = ssl_runtime_entry
        base[key] = str(path)
    try:
        child = derive_eval_tool_filter_env(base, SOURCE_MATRIX_CHILD_TOOLS)
        _require_exact_child_filter(child)
    except (EvalToolFilterError, ValueError):
        raise ChildConfigurationError("child_environment_invalid") from None
    if (
        not set(child).issubset(_CHILD_ENV_KEYS)
        or any(key.startswith("PYTHON") or "PROXY" in key for key in child)
        or any(key.startswith("SAXO_MCP_") and key not in _CHILD_ENV_KEYS for key in child)
    ):
        raise ChildConfigurationError("child_environment_invalid")
    return child


def _require_sim_caller_environment(caller_env: Mapping[str, str]) -> None:
    if EVAL_TOOL_FILTER_FLAG in caller_env or EVAL_ALLOWED_TOOLS_ENV in caller_env:
        raise ChildConfigurationError("ambient_filter_configuration")
    if caller_env.get(_SIM_ENVIRONMENT) != "SIM":
        raise ChildConfigurationError("environment_not_sim")
    if caller_env.get(_LIVE_READS_ENV) != "0":
        raise ChildConfigurationError("live_reads_enabled")
    if caller_env.get(_LIVE_WRITES_ENV) != "":
        raise ChildConfigurationError("live_writes_enabled")


def _require_exact_child_filter(child: Mapping[str, str]) -> None:
    if resolve_eval_tool_filter(child) != frozenset(SOURCE_MATRIX_CHILD_TOOLS):
        raise ValueError("child_filter_mismatch")


def build_child_launch_config(
    paths: ChildBootstrapPaths,
    caller_env: Mapping[str, str],
    *,
    ssl_runtime_entry: tuple[Literal["SSL_CERT_FILE", "SSL_CERT_DIR"], Path] | None = None,
) -> ChildLaunchConfig:
    _validate_child_paths(paths)
    environment = build_child_environment(
        caller_env,
        tmpdir=paths.tmpdir,
        ssl_runtime_entry=ssl_runtime_entry,
    )
    command = (
        str(paths.executable),
        "-I",
        "-B",
        "-S",
        "-X",
        f"pycache_prefix={paths.pycache_prefix}",
        "-c",
        CHILD_BOOTSTRAP,
        str(paths.runtime_root),
        str(paths.executable),
        str(paths.site_packages),
        str(paths.pycache_prefix),
        str(paths.workdir),
        str(paths.tmpdir),
    )
    return ChildLaunchConfig(
        command=command,
        environment=environment,
        cwd=paths.workdir,
        executable_identity_sha256=_hash_regular_file_no_follow(paths.executable),
    )


def _child_credential_source(caller_env: Mapping[str, str]) -> tuple[str, str]:
    app_key = caller_env.get(_SIM_APP_KEY_ENV, "").strip()
    if app_key:
        return _SIM_APP_KEY_ENV, app_key
    client_id = caller_env.get(_SIM_CLIENT_ID_ENV, "").strip()
    if client_id:
        return _SIM_CLIENT_ID_ENV, client_id
    credential_file = Path(
        caller_env.get(_SIM_CREDENTIAL_FILE_ENV, str(DEFAULT_SIM_CREDENTIAL_FILE)),
    ).expanduser().resolve(strict=True)
    return _SIM_CREDENTIAL_FILE_ENV, str(credential_file)


def _require_runtime_ssl_entry(
    entry: tuple[Literal["SSL_CERT_FILE", "SSL_CERT_DIR"], Path] | None,
) -> None:
    if entry is None:
        return
    key, path = entry
    if key not in _SSL_ENV_KEYS:
        raise ValueError("ssl_runtime_entry_invalid")
    _canonical_no_follow_path(path, require_directory=False)


def _validate_child_paths(paths: ChildBootstrapPaths) -> None:
    checked = (
        paths.runtime_root,
        paths.executable,
        paths.site_packages,
        paths.pycache_prefix,
        paths.workdir,
        paths.tmpdir,
    )
    if any(not path.is_absolute() for path in checked):
        raise ChildConfigurationError("child_path_invalid")
    try:
        runtime_root, runtime_stat = _canonical_no_follow_path(
            paths.runtime_root,
            require_directory=True,
        )
        executable, executable_stat = _canonical_no_follow_path(
            paths.executable,
            require_directory=False,
        )
        site_packages, site_stat = _canonical_no_follow_path(
            paths.site_packages,
            require_directory=True,
        )
        pycache_prefix, cache_stat = _canonical_no_follow_path(
            paths.pycache_prefix,
            require_directory=True,
        )
        workdir, work_stat = _canonical_no_follow_path(paths.workdir, require_directory=True)
        tmpdir, temp_stat = _canonical_no_follow_path(paths.tmpdir, require_directory=True)
    except OSError:
        raise ChildConfigurationError("child_path_invalid") from None
    if (
        not stat.S_ISDIR(runtime_stat.st_mode)
        or not stat.S_ISREG(executable_stat.st_mode)
        or not stat.S_ISDIR(site_stat.st_mode)
        or not stat.S_ISDIR(cache_stat.st_mode)
        or not stat.S_ISDIR(work_stat.st_mode)
        or not stat.S_ISDIR(temp_stat.st_mode)
        or not executable.is_relative_to(runtime_root)
        or not site_packages.is_relative_to(runtime_root)
        or any(
            path.is_relative_to(runtime_root)
            for path in (pycache_prefix, workdir, tmpdir)
        )
    ):
        raise ChildConfigurationError("child_path_invalid")
    try:
        _hash_regular_file_no_follow(paths.executable)
    except OSError:
        raise ChildConfigurationError("child_path_invalid") from None


def _canonical_no_follow_path(
    path: Path,
    *,
    require_directory: bool,
) -> tuple[Path, os.stat_result]:
    descriptor = _open_no_follow_path(path, require_directory=require_directory)
    try:
        return Path(os.path.realpath(path)), os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _open_no_follow_path(path: Path, *, require_directory: bool) -> int:
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise OSError("child_path_invalid")
    parts = path.parts[1:]
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index + 1 < len(parts) or require_directory:
                flags |= os.O_DIRECTORY
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    else:
        return descriptor


def _hash_regular_file_no_follow(path: Path) -> str:
    descriptor = _open_no_follow_path(path, require_directory=False)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not_a_regular_file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


__all__ = (
    "CHILD_BOOTSTRAP",
    "CHILD_TOOL_IDS_SHA256",
    "SOURCE_MATRIX_CHILD_TOOLS",
    "ChildBootstrapPaths",
    "ChildConfigurationError",
    "ChildLaunchConfig",
    "MatrixCallPolicy",
    "MatrixSession",
    "RegisteredCallProfile",
    "build_child_environment",
    "build_child_launch_config",
)
