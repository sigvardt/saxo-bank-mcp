from __future__ import annotations

import hashlib
import json
import math
import os
import select
import stat
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, cast

import anyio
from anyio.abc import Process, TaskGroup
from anyio.lowlevel import checkpoint
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.message import SessionMessage

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
type ProcessState = Literal[
    "new",
    "spawned",
    "initialized",
    "listed",
    "running",
    "closing",
    "exited",
]
type ProcessFailureReason = Literal[
    "invalid_transition",
    "spawn_failed",
    "initialize_failed",
    "tool_list_failed",
    "protocol_failed",
    "call_failed",
    "shutdown_failed",
    "nonzero_exit",
]

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
_MAX_PROTOCOL_LINE_BYTES: Final = 1_048_576
_READ_CHUNK_BYTES: Final = 65_536
_SHUTDOWN_TIMEOUT_SECONDS: Final = 2.0
_MAX_STDERR_BYTE_COUNT: Final = (1 << 63) - 1
_PIPE_PAIR_COUNT: Final = 3
_PIPE_PROBE_BYTES: Final = b"saxo-mcp-pipe-pair-proof"
_PIPE_PROBE_TIMEOUT_SECONDS: Final = 0.25
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


@dataclass(frozen=True, slots=True)
class PipeIdentity:
    endpoint_kind: Literal["fifo"]
    parent_device: int = field(repr=False)
    parent_inode: int = field(repr=False)
    child_device: int = field(repr=False)
    child_inode: int = field(repr=False)

    def canonical_private_material(self) -> dict[str, int]:
        return {
            "child_device": self.child_device,
            "child_inode": self.child_inode,
            "parent_device": self.parent_device,
            "parent_inode": self.parent_inode,
        }


@dataclass(frozen=True, slots=True)
class ProcessSessionFacts:
    coordinator_pid: int = field(repr=False)
    child_pid: int = field(repr=False)
    executable_identity_sha256: str
    stdin_identity: PipeIdentity = field(repr=False)
    stdout_identity: PipeIdentity = field(repr=False)
    stderr_byte_count: int = field(repr=False)
    listed_tool_names: tuple[str, ...] = field(repr=False)
    child_spawn_count: Literal[1]
    mcp_session_count: Literal[1]
    mcp_initialize_count: Literal[1]
    tool_list_count: Literal[1]
    reconnect_count: Literal[0]
    restart_count: Literal[0]
    child_exit_code: int
    stdout_protocol_only: bool


@dataclass(frozen=True, slots=True)
class ProcessSessionError(RuntimeError):
    reason: ProcessFailureReason

    def __str__(self) -> str:  # noqa: D105
        return self.reason


class ProcessMatrixSession(MatrixSession, Protocol):
    async def spawn(self) -> None:
        raise NotImplementedError

    async def initialize(self) -> None:
        raise NotImplementedError

    async def list_tools_once(self) -> tuple[str, ...]:
        raise NotImplementedError

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        raise NotImplementedError

    async def close(self) -> ProcessSessionFacts:
        raise NotImplementedError

    async def abort(self) -> ProcessSessionFacts:
        raise NotImplementedError


class OneShotProcessSession(ProcessMatrixSession):
    """Own one direct child process and one MCP session for its full lifetime."""

    def __init__(self, config: ChildLaunchConfig) -> None:
        """Create a new unspawned session for one fixed child configuration."""
        self._config = config
        self._state: ProcessState = "new"
        self._process: Process | None = None
        self._task_group: TaskGroup | None = None
        self._task_group_entered = False
        self._client_session: ClientSession | None = None
        self._client_session_entered = False
        self._incoming_send: MemoryObjectSendStream[SessionMessage | Exception] | None = None
        self._incoming_receive: MemoryObjectReceiveStream[SessionMessage | Exception] | None = None
        self._outgoing_send: MemoryObjectSendStream[SessionMessage] | None = None
        self._outgoing_receive: MemoryObjectReceiveStream[SessionMessage] | None = None
        self._stdin_descriptor: int | None = None
        self._stdout_descriptor: int | None = None
        self._stderr_descriptor: int | None = None
        self._stdin_identity: PipeIdentity | None = None
        self._stdout_identity: PipeIdentity | None = None
        self._stderr_identity: PipeIdentity | None = None
        self._stdin_done: anyio.Event | None = None
        self._stdout_done: anyio.Event | None = None
        self._stderr_done: anyio.Event | None = None
        self._coordinator_pid = os.getpid()
        self._child_pid: int | None = None
        self._child_exit_code: int | None = None
        self._stderr_byte_count = 0
        self._listed_tool_names: tuple[str, ...] = ()
        self._child_spawn_count = 0
        self._mcp_session_count = 0
        self._mcp_initialize_count = 0
        self._tool_list_count = 0
        self._reconnect_count: Literal[0] = 0
        self._restart_count: Literal[0] = 0
        self._pump_failure: Literal["protocol_failed", "shutdown_failed"] | None = None
        self._call_failed = False

    async def spawn(self) -> None:  # noqa: PLR0915
        self._require_state("new")
        self._child_spawn_count += 1
        descriptors: list[int] = []
        process: Process | None = None
        try:
            child_stdin, parent_stdin = _cloexec_pipe()
            descriptors.extend((child_stdin, parent_stdin))
            parent_stdout, child_stdout = _cloexec_pipe()
            descriptors.extend((parent_stdout, child_stdout))
            parent_stderr, child_stderr = _cloexec_pipe()
            descriptors.extend((parent_stderr, child_stderr))

            stdin_identity = _pipe_identity(parent_stdin, child_stdin)
            stdout_identity = _pipe_identity(parent_stdout, child_stdout)
            stderr_identity = _pipe_identity(parent_stderr, child_stderr)
            _require_distinct_pipe_pairs(
                (stdin_identity, stdout_identity, stderr_identity),
            )

            process = await anyio.open_process(
                self._config.command,
                stdin=child_stdin,
                stdout=child_stdout,
                stderr=child_stderr,
                env=self._config.environment,
                cwd=self._config.cwd,
                start_new_session=True,
            )
            self._process = process
            for descriptor in (child_stdin, child_stdout, child_stderr):
                _close_descriptor(descriptor)
                descriptors.remove(descriptor)
            for descriptor in (parent_stdin, parent_stdout, parent_stderr):
                os.set_blocking(descriptor, False)

            child_pid = process.pid
            _require_distinct_child_pid(child_pid, self._coordinator_pid)

            self._child_pid = child_pid
            self._stdin_descriptor = parent_stdin
            self._stdout_descriptor = parent_stdout
            self._stderr_descriptor = parent_stderr
            self._stdin_identity = stdin_identity
            self._stdout_identity = stdout_identity
            self._stderr_identity = stderr_identity
            descriptors.clear()

            incoming_send, incoming_receive = anyio.create_memory_object_stream[
                SessionMessage | Exception
            ](0)
            outgoing_send, outgoing_receive = anyio.create_memory_object_stream[SessionMessage](0)
            self._incoming_send = incoming_send
            self._incoming_receive = incoming_receive
            self._outgoing_send = outgoing_send
            self._outgoing_receive = outgoing_receive
            self._client_session = ClientSession(incoming_receive, outgoing_send)
            self._stdin_done = anyio.Event()
            self._stdout_done = anyio.Event()
            self._stderr_done = anyio.Event()
            task_group = anyio.create_task_group()
            await task_group.__aenter__()
            self._task_group = task_group
            self._task_group_entered = True
            task_group.start_soon(self._stdin_pump)
            task_group.start_soon(self._stdout_pump)
            task_group.start_soon(self._stderr_pump)
            self._state = "spawned"
        except BaseException as exc:
            for descriptor in descriptors:
                _close_descriptor(descriptor)
            if process is not None:
                self._process = process
            self._shield_owned_task_group()
            if self._task_group_entered:
                await self._cleanup_failed_spawn()
            else:
                with anyio.CancelScope(shield=True):
                    await self._cleanup_failed_spawn()
            self._state = "exited"
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            if not isinstance(exc, Exception):
                raise
            raise ProcessSessionError("spawn_failed") from None

    async def initialize(self) -> None:
        self._require_state("spawned")
        if self._mcp_initialize_count != 0 or self._mcp_session_count != 0:
            raise ProcessSessionError("invalid_transition")
        client_session = self._require_client_session()
        self._mcp_session_count = 1
        self._mcp_initialize_count = 1
        try:
            await client_session.__aenter__()
            self._client_session_entered = True
            await client_session.initialize()
        except Exception:  # noqa: BLE001
            raise ProcessSessionError(self._stage_failure("initialize_failed")) from None
        self._state = "initialized"

    async def list_tools_once(self) -> tuple[str, ...]:
        self._require_state("initialized")
        if self._tool_list_count != 0:
            raise ProcessSessionError("invalid_transition")
        self._tool_list_count = 1
        try:
            result = await self._require_client_session().list_tools()
        except Exception:  # noqa: BLE001
            raise ProcessSessionError(self._stage_failure("tool_list_failed")) from None
        names = tuple(tool.name for tool in result.tools)
        self._listed_tool_names = names
        self._state = "listed"
        return names

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        self._require_state("listed", "running")
        if self._call_failed or name not in SOURCE_MATRIX_CHILD_TOOLS:
            raise ProcessSessionError("call_failed")
        self._state = "running"
        try:
            result = await self._require_client_session().call_tool(name, arguments)
            return _validated_tool_result(result)
        except Exception:  # noqa: BLE001
            self._call_failed = True
            raise ProcessSessionError(self._stage_failure("call_failed")) from None

    async def close(self) -> ProcessSessionFacts:
        self._require_state("listed", "running")
        self._state = "closing"
        self._shield_owned_task_group()
        shutdown_failed = False
        try:
            try:
                await self._consume_pending_outer_cancellation()
            finally:
                try:
                    await self._close_client_session()
                except Exception:  # noqa: BLE001
                    shutdown_failed = True
                with anyio.CancelScope(shield=True):
                    try:
                        await self._close_outgoing_stream()
                    except Exception:  # noqa: BLE001
                        shutdown_failed = True
                    try:
                        with anyio.fail_after(_SHUTDOWN_TIMEOUT_SECONDS):
                            await self._require_event(self._stdin_done).wait()
                            self._child_exit_code = await self._require_process().wait()
                            await self._require_event(self._stdout_done).wait()
                            await self._require_event(self._stderr_done).wait()
                    except Exception:  # noqa: BLE001
                        shutdown_failed = True
                        self._close_parent_stdin()
                        self._child_exit_code = await self._terminate_and_reap_same_child()
                    self._state = "exited"
                await self._finalize_resources()
        finally:
            if self._state != "exited":
                with anyio.CancelScope(shield=True):
                    self._close_parent_stdin()
                    self._child_exit_code = await self._terminate_and_reap_same_child()
                    self._state = "exited"
        await checkpoint()
        if self._pump_failure == "protocol_failed":
            raise ProcessSessionError("protocol_failed")
        if shutdown_failed or self._pump_failure == "shutdown_failed":
            raise ProcessSessionError("shutdown_failed")
        if self._child_exit_code is None:
            raise ProcessSessionError("shutdown_failed")
        if self._child_exit_code != 0:
            raise ProcessSessionError("nonzero_exit")
        return self._facts()

    async def abort(self) -> ProcessSessionFacts:
        if self._state in {"new", "closing", "exited"}:
            raise ProcessSessionError("invalid_transition")
        self._state = "closing"
        self._shield_owned_task_group()
        cleanup_failed = False
        try:
            try:
                await self._consume_pending_outer_cancellation()
            finally:
                try:
                    await self._close_client_session()
                except Exception:  # noqa: BLE001
                    cleanup_failed = True
                with anyio.CancelScope(shield=True):
                    try:
                        await self._close_outgoing_stream()
                    except Exception:  # noqa: BLE001
                        cleanup_failed = True
                    self._close_parent_stdin()
                    self._child_exit_code = await self._terminate_and_reap_same_child()
                    if self._child_exit_code is None:
                        cleanup_failed = True
                    self._state = "exited"
                await self._finalize_resources()
        finally:
            if self._state != "exited":
                with anyio.CancelScope(shield=True):
                    self._close_parent_stdin()
                    self._child_exit_code = await self._terminate_and_reap_same_child()
                    self._state = "exited"
        await checkpoint()
        if cleanup_failed or self._pump_failure == "shutdown_failed":
            raise ProcessSessionError("shutdown_failed")
        if self._pump_failure == "protocol_failed":
            raise ProcessSessionError("protocol_failed")
        return self._facts()

    async def _stdin_pump(self) -> None:
        outgoing_receive = self._outgoing_receive
        if outgoing_receive is None:
            self._set_pump_failure("shutdown_failed")
            return
        try:
            async with outgoing_receive:
                async for session_message in outgoing_receive:
                    encoded = (
                        session_message.message.model_dump_json(
                            by_alias=True,
                            exclude_none=True,
                        )
                        + "\n"
                    ).encode("utf-8")
                    await self._write_all(encoded)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
            if self._state not in {"closing", "exited"}:
                self._set_pump_failure("shutdown_failed")
        finally:
            self._close_parent_stdin()
            if self._stdin_done is not None:
                self._stdin_done.set()

    async def _stdout_pump(self) -> None:  # noqa: C901
        incoming_send = self._incoming_send
        if incoming_send is None:
            self._set_pump_failure("protocol_failed")
            return
        buffer = bytearray()
        try:
            async with incoming_send:
                while True:
                    chunk = await self._read_descriptor(self._stdout_descriptor)
                    if not chunk:
                        if buffer:
                            self._set_pump_failure("protocol_failed")
                            await self._send_protocol_failure(incoming_send)
                        break
                    if not await self._consume_stdout_chunk(chunk, buffer, incoming_send):
                        break
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            if self._state not in {"closing", "exited"}:
                self._set_pump_failure("protocol_failed")
        except OSError:
            if self._state not in {"closing", "exited"}:
                self._set_pump_failure("protocol_failed")
                await self._send_protocol_failure(incoming_send)
        finally:
            self._close_parent_stdout()
            if self._stdout_done is not None:
                self._stdout_done.set()

    async def _consume_stdout_chunk(
        self,
        chunk: bytes,
        buffer: bytearray,
        incoming_send: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> bool:
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            segment_end = newline if newline >= 0 else len(chunk)
            segment = chunk[start:segment_end]
            if len(segment) > _MAX_PROTOCOL_LINE_BYTES - len(buffer):
                self._set_pump_failure("protocol_failed")
                await self._send_protocol_failure(incoming_send)
                return False
            buffer.extend(segment)
            if newline < 0:
                return True
            if not buffer:
                self._set_pump_failure("protocol_failed")
                await self._send_protocol_failure(incoming_send)
                return False
            try:
                line = bytes(buffer).decode("utf-8", errors="strict")
                message = types.JSONRPCMessage.model_validate_json(line)
            except (UnicodeDecodeError, ValueError):
                self._set_pump_failure("protocol_failed")
                await self._send_protocol_failure(incoming_send)
                return False
            buffer.clear()
            await incoming_send.send(SessionMessage(message))
            start = newline + 1
        return True

    async def _stderr_pump(self) -> None:
        try:
            while chunk := await self._read_descriptor(self._stderr_descriptor):
                self._stderr_byte_count = min(
                    _MAX_STDERR_BYTE_COUNT,
                    self._stderr_byte_count + len(chunk),
                )
        except OSError:
            if self._state not in {"closing", "exited"}:
                self._set_pump_failure("shutdown_failed")
        finally:
            self._close_parent_stderr()
            if self._stderr_done is not None:
                self._stderr_done.set()

    async def _write_all(self, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            descriptor = self._stdin_descriptor
            if descriptor is None:
                raise OSError("stdin_closed")
            await anyio.wait_writable(descriptor)
            try:
                written = os.write(descriptor, payload[offset:])
            except BlockingIOError:
                continue
            if written <= 0:
                raise OSError("stdin_write_failed")
            offset += written

    @staticmethod
    async def _send_protocol_failure(
        incoming_send: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> None:
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await incoming_send.send(ProcessSessionError("protocol_failed"))

    @staticmethod
    async def _read_descriptor(descriptor: int | None) -> bytes:
        if descriptor is None:
            raise OSError("descriptor_closed")
        while True:
            await anyio.wait_readable(descriptor)
            try:
                return os.read(descriptor, _READ_CHUNK_BYTES)
            except BlockingIOError:
                continue

    async def _close_client_session(self) -> None:
        if not self._client_session_entered:
            return
        client_session = self._require_client_session()
        await client_session.__aexit__(None, None, None)
        self._client_session_entered = False

    async def _close_outgoing_stream(self) -> None:
        if self._outgoing_send is not None:
            await self._outgoing_send.aclose()

    async def _terminate_and_reap_same_child(self) -> int | None:
        process = self._process
        if process is None:
            return None
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        exit_code: int | None = None
        with anyio.move_on_after(_SHUTDOWN_TIMEOUT_SECONDS) as terminate_scope:
            exit_code = await process.wait()
        if not terminate_scope.cancel_called and exit_code is not None:
            return exit_code
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        exit_code = None
        with anyio.move_on_after(_SHUTDOWN_TIMEOUT_SECONDS) as kill_scope:
            exit_code = await process.wait()
        if kill_scope.cancel_called or exit_code is None:
            return None
        return exit_code

    async def _cleanup_failed_spawn(self) -> None:
        self._close_parent_stdin()
        self._close_parent_stdout()
        self._close_parent_stderr()
        if self._process is not None:
            self._child_exit_code = await self._terminate_and_reap_same_child()
        await self._finalize_resources()

    def _shield_owned_task_group(self) -> None:
        if self._task_group is not None and self._task_group_entered:
            self._task_group.cancel_scope.shield = True

    @staticmethod
    async def _consume_pending_outer_cancellation() -> None:
        try:
            await checkpoint()
        except anyio.get_cancelled_exc_class():
            pass

    async def _finalize_resources(self) -> None:
        self._close_parent_stdin()
        self._close_parent_stdout()
        self._close_parent_stderr()
        for stream in (
            self._incoming_send,
            self._incoming_receive,
            self._outgoing_send,
            self._outgoing_receive,
        ):
            if stream is not None:
                with suppress(BaseException):
                    await stream.aclose()
        if self._task_group is not None and self._task_group_entered:
            try:
                await self._task_group.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                self._set_pump_failure("shutdown_failed")
            finally:
                self._task_group_entered = False
        if self._process is not None and self._process.returncode is not None:
            with suppress(BaseException):
                await self._process.aclose()

    def _facts(self) -> ProcessSessionFacts:
        if (
            self._child_pid is None
            or self._child_exit_code is None
            or self._stdin_identity is None
            or self._stdout_identity is None
            or self._child_spawn_count != 1
            or self._mcp_session_count != 1
            or self._mcp_initialize_count != 1
            or self._tool_list_count != 1
        ):
            raise ProcessSessionError("shutdown_failed")
        return ProcessSessionFacts(
            coordinator_pid=self._coordinator_pid,
            child_pid=self._child_pid,
            executable_identity_sha256=self._config.executable_identity_sha256,
            stdin_identity=self._stdin_identity,
            stdout_identity=self._stdout_identity,
            stderr_byte_count=self._stderr_byte_count,
            listed_tool_names=self._listed_tool_names,
            child_spawn_count=self._child_spawn_count,
            mcp_session_count=self._mcp_session_count,
            mcp_initialize_count=self._mcp_initialize_count,
            tool_list_count=self._tool_list_count,
            reconnect_count=self._reconnect_count,
            restart_count=self._restart_count,
            child_exit_code=self._child_exit_code,
            stdout_protocol_only=self._pump_failure != "protocol_failed",
        )

    def _stage_failure(
        self,
        default: Literal["initialize_failed", "tool_list_failed", "call_failed"],
    ) -> ProcessFailureReason:
        return self._pump_failure or default

    def _set_pump_failure(
        self,
        reason: Literal["protocol_failed", "shutdown_failed"],
    ) -> None:
        if self._pump_failure is None or reason == "protocol_failed":
            self._pump_failure = reason

    def _require_state(self, *expected: ProcessState) -> None:
        if self._state not in expected:
            raise ProcessSessionError("invalid_transition")

    def _require_process(self) -> Process:
        if self._process is None:
            raise ProcessSessionError("shutdown_failed")
        return self._process

    def _require_client_session(self) -> ClientSession:
        if self._client_session is None:
            raise ProcessSessionError("invalid_transition")
        return self._client_session

    @staticmethod
    def _require_event(event: anyio.Event | None) -> anyio.Event:
        if event is None:
            raise ProcessSessionError("shutdown_failed")
        return event

    def _close_parent_stdin(self) -> None:
        self._stdin_descriptor = _close_optional_descriptor(self._stdin_descriptor)

    def _close_parent_stdout(self) -> None:
        self._stdout_descriptor = _close_optional_descriptor(self._stdout_descriptor)

    def _close_parent_stderr(self) -> None:
        self._stderr_descriptor = _close_optional_descriptor(self._stderr_descriptor)


def _pipe_identity(parent_descriptor: int, child_descriptor: int) -> PipeIdentity:
    parent_stat = os.fstat(parent_descriptor)
    child_stat = os.fstat(child_descriptor)
    same_kernel_identity = (parent_stat.st_dev, parent_stat.st_ino) == (
        child_stat.st_dev,
        child_stat.st_ino,
    )
    # Darwin reports distinct inode values for the two ends of one anonymous pipe.
    if (
        not stat.S_ISFIFO(parent_stat.st_mode)
        or not stat.S_ISFIFO(child_stat.st_mode)
        or (sys.platform != "darwin" and not same_kernel_identity)
    ):
        raise OSError("pipe_identity_invalid")
    _probe_and_drain_pipe_pair(parent_descriptor, child_descriptor)
    return PipeIdentity(
        endpoint_kind="fifo",
        parent_device=parent_stat.st_dev,
        parent_inode=parent_stat.st_ino,
        child_device=child_stat.st_dev,
        child_inode=child_stat.st_ino,
    )


def _probe_and_drain_pipe_pair(first_descriptor: int, second_descriptor: int) -> None:
    first_blocking = os.get_blocking(first_descriptor)
    second_blocking = os.get_blocking(second_descriptor)
    try:
        os.set_blocking(first_descriptor, False)
        os.set_blocking(second_descriptor, False)
        _exchange_and_verify_pipe_probe(first_descriptor, second_descriptor)
    except (OSError, ValueError):
        raise OSError("pipe_identity_invalid") from None
    finally:
        with suppress(OSError):
            os.set_blocking(first_descriptor, first_blocking)
        with suppress(OSError):
            os.set_blocking(second_descriptor, second_blocking)


def _exchange_and_verify_pipe_probe(first_descriptor: int, second_descriptor: int) -> None:
    first_writes = _descriptor_accepts_write(first_descriptor)
    second_writes = _descriptor_accepts_write(second_descriptor)
    if first_writes == second_writes:
        raise OSError("pipe_identity_invalid")
    writer, reader = (
        (first_descriptor, second_descriptor)
        if first_writes
        else (second_descriptor, first_descriptor)
    )
    deadline = time.monotonic() + _PIPE_PROBE_TIMEOUT_SECONDS
    _write_pipe_probe(writer, deadline)
    if _read_pipe_probe(reader, deadline) != _PIPE_PROBE_BYTES:
        raise OSError("pipe_identity_invalid")
    try:
        os.read(reader, 1)
    except BlockingIOError:
        return
    raise OSError("pipe_identity_invalid")


def _descriptor_accepts_write(descriptor: int) -> bool:
    try:
        os.write(descriptor, b"")
    except OSError:
        return False
    return True


def _write_pipe_probe(descriptor: int, deadline: float) -> None:
    offset = 0
    while offset < len(_PIPE_PROBE_BYTES):
        _wait_for_pipe_descriptor(descriptor, deadline, writable=True)
        try:
            written = os.write(descriptor, _PIPE_PROBE_BYTES[offset:])
        except BlockingIOError:
            continue
        if written <= 0:
            raise OSError("pipe_identity_invalid")
        offset += written


def _read_pipe_probe(descriptor: int, deadline: float) -> bytes:
    received = bytearray()
    while len(received) < len(_PIPE_PROBE_BYTES):
        _wait_for_pipe_descriptor(descriptor, deadline, writable=False)
        try:
            chunk = os.read(descriptor, len(_PIPE_PROBE_BYTES) - len(received))
        except BlockingIOError:
            continue
        if not chunk:
            raise OSError("pipe_identity_invalid")
        received.extend(chunk)
    return bytes(received)


def _wait_for_pipe_descriptor(descriptor: int, deadline: float, *, writable: bool) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OSError("pipe_identity_invalid")
    readable_descriptors = [] if writable else [descriptor]
    writable_descriptors = [descriptor] if writable else []
    readable, ready_to_write, _ = select.select(
        readable_descriptors,
        writable_descriptors,
        [],
        remaining,
    )
    if not readable and not ready_to_write:
        raise OSError("pipe_identity_invalid")


def _require_distinct_pipe_pairs(identities: tuple[PipeIdentity, ...]) -> None:
    complete_pair_ids = {
        (
            identity.parent_device,
            identity.parent_inode,
            identity.child_device,
            identity.child_inode,
        )
        for identity in identities
    }
    if len(complete_pair_ids) != _PIPE_PAIR_COUNT:
        raise OSError("pipe_identity_invalid")
    if sys.platform == "darwin":
        endpoint_ids = {
            endpoint
            for identity in identities
            for endpoint in (
                (identity.parent_device, identity.parent_inode),
                (identity.child_device, identity.child_inode),
            )
        }
        if len(endpoint_ids) != _PIPE_PAIR_COUNT * 2:
            raise OSError("pipe_identity_invalid")


def _require_distinct_child_pid(child_pid: int, coordinator_pid: int) -> None:
    if child_pid <= 0 or child_pid == coordinator_pid:
        raise OSError("child_pid_invalid")


def _cloexec_pipe() -> tuple[int, int]:
    pipe2 = getattr(os, "pipe2", None)
    if callable(pipe2):
        return cast("tuple[int, int]", pipe2(os.O_CLOEXEC))
    read_descriptor, write_descriptor = os.pipe()
    os.set_inheritable(read_descriptor, False)  # noqa: FBT003
    os.set_inheritable(write_descriptor, False)  # noqa: FBT003
    return read_descriptor, write_descriptor


def _validated_json_object(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError("structured_content_invalid")
    validated = _validated_json_value(cast("dict[object, object]", value))
    return cast("dict[str, JsonValue]", validated)


def _validated_tool_result(result: types.CallToolResult) -> dict[str, JsonValue]:
    if result.isError is True:
        raise ValueError("tool_result_error")
    return _validated_json_object(result.structuredContent)


def _validated_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("structured_content_invalid")
    if isinstance(value, list):
        return [_validated_json_value(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in cast("dict[object, object]", value).items():
            if not isinstance(key, str):
                raise TypeError("structured_content_invalid")
            result[key] = _validated_json_value(item)
        return result
    raise ValueError("structured_content_invalid")


def _close_optional_descriptor(descriptor: int | None) -> None:
    if descriptor is not None:
        _close_descriptor(descriptor)


def _close_descriptor(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)


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
    "OneShotProcessSession",
    "PipeIdentity",
    "ProcessMatrixSession",
    "ProcessSessionError",
    "ProcessSessionFacts",
    "RegisteredCallProfile",
    "RegisteredResponseMode",
    "build_child_environment",
    "build_child_launch_config",
)
