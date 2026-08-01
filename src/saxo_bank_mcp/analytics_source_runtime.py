from __future__ import annotations

import hashlib
import importlib.machinery
import io
import json
import os
import platform
import re
import stat
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field

from saxo_bank_mcp.analytics_source_contracts import source_contract_catalog_sha256
from saxo_bank_mcp.analytics_source_process import CHILD_BOOTSTRAP, ChildBootstrapPaths

_CANDIDATE_RESOURCE_DIR: Final = "_analytics_source_matrix"
_CANDIDATE_RESOURCE_NAME: Final = "source_matrix_candidate.json"
_CANDIDATE_SOURCE_PATH: Final = (
    Path(__file__).resolve().parents[2] / "data" / "analytics" / _CANDIDATE_RESOURCE_NAME
)
_SOURCE_RUNNERS: Final = (
    "scripts/generate_analytics_source_matrix_candidate.py",
    "scripts/prepare_analytics_source_matrix_runtime.py",
    "scripts/run_analytics_source_matrix.py",
    "scripts/saxo-bank-analytics-source-matrix",
    "scripts/saxo-bank-analytics-source-matrix-generate",
)
_OFFICIAL_LAUNCHER_NAMES: Final = (
    "saxo-bank-analytics-source-matrix",
    "saxo-bank-analytics-source-matrix-generate",
)
_INSTALLER_GENERATED_METADATA: Final = frozenset(
    {"INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"},
)
_EXPECTED_INSTALLER: Final = "uv"
_OFFICIAL_LAUNCHER_ENV: Final = "SAXO_BANK_MCP_OFFICIAL_ISOLATED_LAUNCHER"
_PYTHON_BYTECODE_SUFFIXES: Final = frozenset(
    {*importlib.machinery.BYTECODE_SUFFIXES, ".pyo"},
)
_IMPORTABLE_ARTIFACT_SUFFIXES: Final = frozenset(
    {
        *importlib.machinery.SOURCE_SUFFIXES,
        *importlib.machinery.BYTECODE_SUFFIXES,
        *importlib.machinery.EXTENSION_SUFFIXES,
    },
)
_RUNTIME_TREE_EXCLUDED_PARTS: Final = frozenset({"dist-packages", "site-packages"})
_PYTHON_BUILD_CONFIG_KEYS: Final = (
    "ABIFLAGS",
    "CONFIG_ARGS",
    "EXT_SUFFIX",
    "LDLIBRARY",
    "LIBRARY",
    "MULTIARCH",
    "Py_ENABLE_SHARED",
    "Py_GIL_DISABLED",
    "SOABI",
    "VERSION",
    "WITH_PYMALLOC",
)
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_SELF_REFERENCE_EXCLUSION_COUNT: Final = 2
_ENTRYPOINT_TARGET_PART_COUNT: Final = 2
_PORTABLE_ENTRYPOINT_MIN_LINES: Final = 4
_OWNER_DIRECTORY_MODE: Final = 0o700
_SEALED_DIRECTORY_MODE: Final = 0o500
_SEALED_FILE_MODE: Final = 0o400
_SEALED_EXECUTABLE_MODE: Final = 0o500
_CPYTHON_312_DEFAULT_INT_MAX_STR_DIGITS: Final = 4300
_ALLOWED_INTERPRETER_LINKS: Final = {
    "bin/python": "python3.12",
    "bin/python3": "python3.12",
}
_RUN_ROOT_PATTERN: Final = re.compile(r"^\.saxo-source-matrix-run\.[1-9][0-9]*$")
_READ_FLAGS: Final = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS: Final = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW_FLAGS: Final = getattr(os, "O_NOFOLLOW", 0)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    ).hexdigest()


class SourceMatrixCandidateIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    harness_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    implementation: str
    cache_tag: str
    python_version: str
    platform: str
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    executable_projection_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    python_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    build_config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stdlib_merkle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stdlib_file_count: int = Field(ge=1)
    platstdlib_merkle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    platstdlib_file_count: int = Field(ge=1)
    shared_runtime_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    shared_runtime_file_count: int = Field(ge=0)
    startup_configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    startup_configuration_file_count: int = Field(ge=0)
    interpreter_policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    importable_suffixes: tuple[str, ...]


class _DependencyDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: str
    files: Mapping[str, str]
    installer_metadata: Mapping[str, str]


class _SourceMatrixCandidateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["5"]
    source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_files: Mapping[str, str]
    installed_files: Mapping[str, str]
    dependency_distributions: Mapping[str, _DependencyDistribution]
    runtime_identity: _RuntimeIdentity
    installed_metadata_projection: Mapping[str, str]
    console_scripts: Mapping[str, str]
    source_wheel_projection_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    portable_runtime_tree_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    portable_runtime_entry_count: int = Field(ge=1)
    child_bootstrap_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_exclusions: tuple[str, ...]
    installed_exclusions: tuple[str, ...]
    source_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    installed_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    harness_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class ExternalRunLayout:
    root: Path = field(repr=False)
    coordinator_cache: Path = field(repr=False)
    coordinator_work: Path = field(repr=False)
    coordinator_tmp: Path = field(repr=False)
    child_cache: Path = field(repr=False)
    child_work: Path = field(repr=False)
    child_tmp: Path = field(repr=False)


type RuntimeEntryKind = Literal["directory", "file", "symlink"]


@dataclass(frozen=True, slots=True)
class RuntimeEntrySnapshot:
    relative_path: str
    entry_kind: RuntimeEntryKind
    device: int = field(repr=False)
    inode: int = field(repr=False)
    uid: int = field(repr=False)
    mode: int
    size: int
    mtime_ns: int = field(repr=False)
    ctime_ns: int = field(repr=False)
    content_sha256: str | None
    link_target: str | None


@dataclass(frozen=True, slots=True)
class PortableRuntimeProjection:
    sha256: str
    entry_count: int


type CandidateRuntimeReason = Literal[
    "runtime_path_invalid",
    "runtime_entry_invalid",
    "runtime_identity_mismatch",
    "runtime_scalar_mismatch",
    "runtime_ancestor_mismatch",
    "external_layout_invalid",
]


@dataclass(frozen=True, slots=True)
class CandidateRuntimeError(ValueError):
    reason: CandidateRuntimeReason

    def __str__(self) -> str:
        """Return only the finite refusal reason."""
        return self.reason


@dataclass(frozen=True, slots=True)
class CandidateRuntimeSeal:
    identity: SourceMatrixCandidateIdentity
    portable_identity_sha256: str
    instance_identity_sha256: str
    runtime_root: Path = field(repr=False)
    executable: Path = field(repr=False)
    site_packages: Path = field(repr=False)
    root_descriptor: int = field(repr=False)
    ancestor_descriptors: tuple[int, ...] = field(repr=False)
    entry_snapshot: tuple[RuntimeEntrySnapshot, ...] = field(repr=False)


def _expected_owner_uid() -> int:
    return os.getuid()


def _safe_artifact_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _excluded_outside_site_record_path(value: str) -> bool:
    path = PurePosixPath(value)
    prefix = ("..", "..", "..")
    target = path.parts[len(prefix) :]
    return (
        path.as_posix() == value
        and path.parts[: len(prefix)] == prefix
        and bool(target[1:])
        and target[0] in {"bin", "share"}
        and all(part not in {"", ".", ".."} and "\\" not in part for part in target[1:])
    )


def _official_launcher_record_path(value: str) -> bool:
    return value in {f"../../../bin/{name}" for name in _OFFICIAL_LAUNCHER_NAMES}


def _validated_closure_entry_metadata(
    path: Path,
    *,
    scope: str,
    allowed_symlinks: frozenset[Path] = frozenset(),
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"source matrix {scope} entry is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) and path.absolute() not in allowed_symlinks:
        raise ValueError(f"source matrix {scope} contains a symlink")
    if path.name == "__pycache__" or path.suffix.casefold() in _PYTHON_BYTECODE_SUFFIXES:
        raise ValueError(f"source matrix {scope} contains a Python cache entry")
    return metadata


def _validate_directory_entry_tree(
    root: Path,
    *,
    scope: str,
    allowed_symlinks: frozenset[Path] = frozenset(),
) -> None:
    root_metadata = _validated_closure_entry_metadata(root, scope=scope)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError(f"source matrix {scope} is not a directory")
    for path in sorted(root.rglob("*"), key=os.fsencode):
        if _inactive_runtime_cache_allowed(path):
            continue
        _validated_closure_entry_metadata(
            path,
            scope=scope,
            allowed_symlinks=allowed_symlinks,
        )


def _source_candidate_files(repository_root: Path) -> dict[str, str]:
    source_package = repository_root / "src" / "saxo_bank_mcp"
    selected: list[Path] = [
        repository_root / "pyproject.toml",
        repository_root / "uv.lock",
        repository_root / "data" / "analytics" / "source_contracts.json",
        repository_root / "data" / "saxo" / "openapi_inventory.json",
        *(repository_root / runner for runner in _SOURCE_RUNNERS),
        *(repository_root / "data" / "analytics" / "migrations").rglob("*"),
        *source_package.rglob("*"),
    ]
    result: dict[str, str] = {}
    for path in sorted(set(selected), key=os.fsencode):
        metadata = _validated_closure_entry_metadata(path, scope="source closure")
        if not stat.S_ISREG(metadata.st_mode):
            continue
        relative = path.relative_to(repository_root).as_posix()
        if relative == "data/analytics/source_matrix_candidate.json":
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _inactive_runtime_cache_allowed(path: Path) -> bool:
    if path.name != "__pycache__" and path.suffix.casefold() not in _PYTHON_BYTECODE_SUFFIXES:
        return False
    if os.environ.get(_OFFICIAL_LAUNCHER_ENV) == "1":
        return False
    try:
        resolved = path.absolute()
        base = Path(sys.base_prefix).absolute()
        if resolved.is_relative_to(base):
            return True
        return any(
            resolved.is_relative_to(Path(raw).absolute())
            for raw in sys.path
            if raw and Path(raw).name in {"site-packages", "dist-packages"}
        )
    except (OSError, ValueError):
        return False


def _runtime_file_projection(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("source matrix runtime file is a symlink")
    if not path.is_file():
        raise ValueError("source matrix runtime file is unavailable")
    content = path.read_bytes()
    if path.name == "pyvenv.cfg":
        content = _normalize_pyvenv_content(content, path.parent)
    return hashlib.sha256(content).hexdigest()


def _runtime_linked_file_projection(path: Path) -> tuple[str, str]:
    current = path.absolute()
    seen: set[Path] = set()
    while current.is_symlink():
        if current in seen:
            raise ValueError("source matrix runtime file link cycle")
        seen.add(current)
        target = current.readlink()
        current = (target if target.is_absolute() else current.parent / target).absolute()
    resolved = current.resolve(strict=True)
    file_sha256 = _runtime_file_projection(resolved)
    return file_sha256, _digest(
        {
            "mode": stat.S_IMODE(resolved.stat().st_mode),
            "resolved_file_sha256": file_sha256,
        },
    )


def _runtime_tree_entries(root: Path) -> tuple[dict[str, str], set[Path]]:
    root_metadata = _validated_closure_entry_metadata(root, scope="runtime tree")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("source matrix runtime tree is unavailable")
    entries: dict[str, str] = {}
    paths: set[Path] = set()
    for path in sorted(root.rglob("*"), key=os.fsencode):
        if _inactive_runtime_cache_allowed(path):
            continue
        metadata = _validated_closure_entry_metadata(path, scope="runtime tree")
        relative_path = path.relative_to(root)
        if any(part in _RUNTIME_TREE_EXCLUDED_PARTS for part in relative_path.parts):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        entries[relative_path.as_posix()] = _runtime_file_projection(path)
        paths.add(path.resolve(strict=True))
    return entries, paths


def _runtime_tree_projection(root: Path) -> tuple[str, int]:
    entries, _paths = _runtime_tree_entries(root)
    if not entries:
        raise ValueError("source matrix runtime tree projection is empty")
    return _digest(entries), len(entries)


def _official_runtime_root() -> Path:
    executable = Path(sys.executable).absolute()
    root = executable.parent.parent
    if executable.name != "python3.12" or executable.parent.name != "bin":
        raise ValueError("source matrix isolated runtime root is unavailable")
    return root


def _normalized_runtime_path(value: str, runtime_root: Path) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise CandidateRuntimeError("runtime_scalar_mismatch")
    absolute = path.resolve(strict=False)
    if absolute == runtime_root:
        return "."
    if absolute.is_relative_to(runtime_root):
        return absolute.relative_to(runtime_root).as_posix()
    if (
        absolute.parent.parent == runtime_root.parent
        and _RUN_ROOT_PATTERN.fullmatch(absolute.parent.name) is not None
    ):
        return f"external/{absolute.name}"
    if absolute.is_relative_to(runtime_root.parent) and absolute.parent == runtime_root.parent:
        return f"external/{absolute.name}"
    raise CandidateRuntimeError("runtime_scalar_mismatch")


def _scalar_material(runtime_root: Path, layout: ExternalRunLayout) -> dict[str, object]:
    cache_prefix = sys.pycache_prefix
    if cache_prefix is None:
        raise CandidateRuntimeError("runtime_scalar_mismatch")
    return {
        "cache_prefix": _normalized_runtime_path(cache_prefix, runtime_root),
        "dont_write_bytecode": sys.flags.dont_write_bytecode,
        "executable": _normalized_runtime_path(sys.executable, runtime_root),
        "implementation": sys.implementation.name,
        "int_max_str_digits": sys.get_int_max_str_digits(),
        "isolated": sys.flags.isolated,
        "no_site": sys.flags.no_site,
        "prefix": _normalized_runtime_path(sys.prefix, runtime_root),
        "base_prefix": _normalized_runtime_path(sys.base_prefix, runtime_root),
        "safe_path": sys.flags.safe_path,
        "sys_path": tuple(_normalized_runtime_path(item, runtime_root) for item in sys.path),
        "version": platform.python_version(),
        "workdir": _normalized_runtime_path(os.fspath(layout.coordinator_work), runtime_root),
        "tmpdir": _normalized_runtime_path(os.fspath(layout.coordinator_tmp), runtime_root),
    }


def _remove_empty_directories(paths: Sequence[Path]) -> None:
    # ponytail: trust the local owner; same-owner swaps need OS-user or sandbox isolation.
    for path in reversed(paths):
        path.rmdir()


def _runtime_layout_from_process(runtime_root: Path) -> ExternalRunLayout:
    raw_cache = sys.pycache_prefix
    raw_tmp = os.environ.get("TMPDIR")
    if raw_cache is None or raw_tmp is None:
        raise CandidateRuntimeError("external_layout_invalid")
    coordinator_cache = Path(raw_cache)
    coordinator_work = Path.cwd()
    coordinator_tmp = Path(raw_tmp)
    if any(
        not path.is_absolute()
        for path in (coordinator_cache, coordinator_work, coordinator_tmp)
    ):
        raise CandidateRuntimeError("external_layout_invalid")
    roots = {path.parent for path in (coordinator_cache, coordinator_work, coordinator_tmp)}
    if len(roots) != 1:
        raise CandidateRuntimeError("external_layout_invalid")
    root = roots.pop()
    if (
        coordinator_cache.name != "coordinator-cache"
        or coordinator_work.name != "coordinator-work"
        or coordinator_tmp.name != "coordinator-tmp"
        or root.parent != runtime_root.parent
        or _RUN_ROOT_PATTERN.fullmatch(root.name) is None
    ):
        raise CandidateRuntimeError("external_layout_invalid")
    layout = ExternalRunLayout(
        root=root,
        coordinator_cache=coordinator_cache,
        coordinator_work=coordinator_work,
        coordinator_tmp=coordinator_tmp,
        child_cache=root / "child-cache",
        child_work=root / "child-work",
        child_tmp=root / "child-tmp",
    )
    created: list[Path] = []
    try:
        for child in (layout.child_cache, layout.child_work, layout.child_tmp):
            child.mkdir(mode=_OWNER_DIRECTORY_MODE)
            created.append(child)
    except OSError as error:
        try:
            _remove_empty_directories(created)
        except OSError as cleanup_error:
            raise CandidateRuntimeError("external_layout_invalid") from cleanup_error
        raise CandidateRuntimeError("external_layout_invalid") from error
    return layout


def _layout_paths(layout: ExternalRunLayout) -> tuple[Path, ...]:
    return (
        layout.coordinator_cache,
        layout.coordinator_work,
        layout.coordinator_tmp,
        layout.child_cache,
        layout.child_work,
        layout.child_tmp,
    )


def _validate_external_layout(
    layout: ExternalRunLayout,
    runtime_root: Path,
    *,
    require_official_name: bool,
) -> None:
    expected = (
        layout.root / "coordinator-cache",
        layout.root / "coordinator-work",
        layout.root / "coordinator-tmp",
        layout.root / "child-cache",
        layout.root / "child-work",
        layout.root / "child-tmp",
    )
    if (
        not layout.root.is_absolute()
        or _layout_paths(layout) != expected
        or (require_official_name and _RUN_ROOT_PATTERN.fullmatch(layout.root.name) is None)
    ):
        raise CandidateRuntimeError("external_layout_invalid")
    root_absolute = layout.root.resolve(strict=False)
    runtime_absolute = runtime_root.resolve(strict=False)
    if root_absolute == runtime_absolute or root_absolute.is_relative_to(runtime_absolute):
        raise CandidateRuntimeError("external_layout_invalid")
    for path in (layout.root, *_layout_paths(layout)):
        try:
            metadata = path.lstat()
        except OSError as error:
            raise CandidateRuntimeError("external_layout_invalid") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != _expected_owner_uid()
            or stat.S_IMODE(metadata.st_mode) != _OWNER_DIRECTORY_MODE
            or (path != layout.root and any(path.iterdir()))
        ):
            raise CandidateRuntimeError("external_layout_invalid")
    if {item.name for item in layout.root.iterdir()} != {
        "coordinator-cache",
        "coordinator-work",
        "coordinator-tmp",
        "child-cache",
        "child-work",
        "child-tmp",
    }:
        raise CandidateRuntimeError("external_layout_invalid")


def _normalize_pyvenv_content(content: bytes, runtime_root: Path) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CandidateRuntimeError("runtime_entry_invalid") from error
    normalized: list[str] = []
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() in {"home", "executable", "command"}:
            value = value.strip().replace(os.fspath(runtime_root), "{runtime}")
            normalized.append(f"{key.strip().casefold()} = {value}")
        else:
            normalized.append(line)
    return ("\n".join(normalized) + "\n").encode("utf-8")


def _portable_file_content(relative: str, content: bytes, runtime_root: Path) -> bytes | None:
    if (
        relative.endswith(
            "/saxo_bank_mcp/_analytics_source_matrix/source_matrix_candidate.json",
        )
        or (".dist-info/" in relative and relative.endswith("/RECORD"))
    ) and ("saxo_bank_mcp-" in relative or "_analytics_source_matrix" in relative):
        return None
    if relative == "pyvenv.cfg":
        return _normalize_pyvenv_content(content, runtime_root)
    if relative.startswith("bin/") and content.startswith(b"#!"):
        first, separator, rest = content.partition(b"\n")
        runtime_token = os.fsencode(runtime_root)
        interpreter_token = os.fsencode(runtime_root / "bin/python3.12")
        if runtime_token in first:
            if first != b"#!" + interpreter_token or separator != b"\n":
                raise CandidateRuntimeError("runtime_entry_invalid")
            return b"#!bin/python3.12" + separator + rest
        lines = content.splitlines(keepends=True)
        startup = b"".join(lines[:3])
        if first == b"#!/bin/sh" and runtime_token in startup:
            expected_exec = (
                b"'''exec' '"
                + interpreter_token
                + b"' \"$0\" \"$@\"\n"
            )
            if (
                len(lines) < _PORTABLE_ENTRYPOINT_MIN_LINES
                or lines[0] != b"#!/bin/sh\n"
                or lines[1] != expected_exec
                or lines[2] != b"' '''\n"
            ):
                raise CandidateRuntimeError("runtime_entry_invalid")
            lines[1] = b"'''exec' 'bin/python3.12' \"$0\" \"$@\"\n"
            return b"".join(lines)
    return content


def _snapshot_from_stat(
    relative: str,
    kind: RuntimeEntryKind,
    metadata: os.stat_result,
    *,
    content_sha256: str | None = None,
    link_target: str | None = None,
) -> RuntimeEntrySnapshot:
    return RuntimeEntrySnapshot(
        relative_path=relative,
        entry_kind=kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
        content_sha256=content_sha256,
        link_target=link_target,
    )


def _matching_file_metadata(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_uid,
        first.st_mode,
        first.st_nlink,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_uid,
        second.st_mode,
        second.st_nlink,
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _read_descriptor_bytes(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, io.DEFAULT_BUFFER_SIZE)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _validate_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or os.fsdecode(os.fsencode(name)) != name
    ):
        raise CandidateRuntimeError("runtime_entry_invalid")


def _walk_runtime_descriptor(  # noqa: C901, PLR0915
    root_descriptor: int,
    runtime_root: Path,
) -> tuple[tuple[RuntimeEntrySnapshot, ...], list[dict[str, str]]]:
    try:
        root_metadata = os.fstat(root_descriptor)
    except OSError as error:
        raise CandidateRuntimeError("runtime_entry_invalid") from error
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != _expected_owner_uid()
        or stat.S_IMODE(root_metadata.st_mode) != _SEALED_DIRECTORY_MODE
    ):
        raise CandidateRuntimeError("runtime_entry_invalid")
    snapshots = [_snapshot_from_stat(".", "directory", root_metadata)]
    portable: list[dict[str, str]] = [
        {"mode": "0500", "path": ".", "type": "directory"},
    ]

    def visit(  # noqa: C901, PLR0912, PLR0915
        directory_descriptor: int,
        prefix: str,
    ) -> None:
        try:
            names = sorted(os.listdir(directory_descriptor), key=os.fsencode)
        except OSError as error:
            raise CandidateRuntimeError("runtime_entry_invalid") from error
        for name in names:
            _validate_name(name)
            relative = f"{prefix}/{name}" if prefix else name
            if name == "__pycache__" or Path(name).suffix.casefold() in _PYTHON_BYTECODE_SUFFIXES:
                raise CandidateRuntimeError("runtime_entry_invalid")
            try:
                metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            except OSError as error:
                raise CandidateRuntimeError("runtime_entry_invalid") from error
            if metadata.st_uid != _expected_owner_uid():
                raise CandidateRuntimeError("runtime_entry_invalid")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                if mode != _SEALED_DIRECTORY_MODE:
                    raise CandidateRuntimeError("runtime_entry_invalid")
                try:
                    child = os.open(
                        name,
                        _DIRECTORY_FLAGS | _NOFOLLOW_FLAGS,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise CandidateRuntimeError("runtime_entry_invalid") from error
                try:
                    opened = os.fstat(child)
                    if not _matching_file_metadata(metadata, opened):
                        raise CandidateRuntimeError("runtime_entry_invalid")
                    snapshots.append(_snapshot_from_stat(relative, "directory", opened))
                    portable.append(
                        {"mode": "0500", "path": relative, "type": "directory"},
                    )
                    visit(child, relative)
                finally:
                    os.close(child)
                continue
            if stat.S_ISREG(metadata.st_mode):
                if (
                    mode not in {_SEALED_FILE_MODE, _SEALED_EXECUTABLE_MODE}
                    or metadata.st_nlink != 1
                ):
                    raise CandidateRuntimeError("runtime_entry_invalid")
                try:
                    descriptor = os.open(
                        name,
                        _READ_FLAGS | _NOFOLLOW_FLAGS,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise CandidateRuntimeError("runtime_entry_invalid") from error
                try:
                    opened = os.fstat(descriptor)
                    if not _matching_file_metadata(metadata, opened):
                        raise CandidateRuntimeError("runtime_entry_invalid")
                    content = _read_descriptor_bytes(descriptor)
                    after = os.fstat(descriptor)
                    if not _matching_file_metadata(opened, after):
                        raise CandidateRuntimeError("runtime_entry_invalid")
                finally:
                    os.close(descriptor)
                normalized = _portable_file_content(relative, content, runtime_root)
                content_sha256 = (
                    None if normalized is None else hashlib.sha256(normalized).hexdigest()
                )
                portable_sha256 = content_sha256 or _digest(
                    {"excluded_runtime_content": relative},
                )
                snapshots.append(
                    _snapshot_from_stat(
                        relative,
                        "file",
                        after,
                        content_sha256=content_sha256,
                    ),
                )
                portable.append(
                    {
                        "mode": format(mode, "04o"),
                        "path": relative,
                        "sha256": portable_sha256,
                        "type": "file",
                    },
                )
                continue
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(name, dir_fd=directory_descriptor)
                except OSError as error:
                    raise CandidateRuntimeError("runtime_entry_invalid") from error
                if _ALLOWED_INTERPRETER_LINKS.get(relative) != target:
                    raise CandidateRuntimeError("runtime_entry_invalid")
                snapshots.append(
                    _snapshot_from_stat(relative, "symlink", metadata, link_target=target),
                )
                portable.append(
                    {
                        "mode": "0777",
                        "path": relative,
                        "target": target,
                        "type": "symlink",
                    },
                )
                continue
            raise CandidateRuntimeError("runtime_entry_invalid")

    visit(root_descriptor, "")
    observed_aliases = {
        item.relative_path: item.link_target
        for item in snapshots
        if item.entry_kind == "symlink"
    }
    if observed_aliases != _ALLOWED_INTERPRETER_LINKS:
        raise CandidateRuntimeError("runtime_entry_invalid")
    executable = next(
        (item for item in snapshots if item.relative_path == "bin/python3.12"),
        None,
    )
    if (
        executable is None
        or executable.entry_kind != "file"
        or executable.mode != _SEALED_EXECUTABLE_MODE
    ):
        raise CandidateRuntimeError("runtime_entry_invalid")
    return tuple(snapshots), portable


def _open_descriptor_chain(path: Path) -> tuple[tuple[int, ...], int]:
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise CandidateRuntimeError("runtime_path_invalid")
    descriptors: list[int] = []
    try:
        current = os.open("/", _DIRECTORY_FLAGS | _NOFOLLOW_FLAGS)
        descriptors.append(current)
        for part in path.parts[1:]:
            _validate_name(part)
            current = os.open(
                part,
                _DIRECTORY_FLAGS | _NOFOLLOW_FLAGS,
                dir_fd=current,
            )
            descriptors.append(current)
    except (OSError, CandidateRuntimeError) as error:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if isinstance(error, CandidateRuntimeError):
            raise
        raise CandidateRuntimeError("runtime_path_invalid") from error
    return tuple(descriptors[:-1]), descriptors[-1]


def _metadata_identity(metadata: os.stat_result) -> dict[str, int]:
    return {
        "ctime_ns": metadata.st_ctime_ns,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "mtime_ns": metadata.st_mtime_ns,
        "size": metadata.st_size,
        "uid": metadata.st_uid,
    }


def _ancestor_identity(metadata: os.stat_result) -> dict[str, int]:
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }


def _snapshot_identity(snapshot: RuntimeEntrySnapshot) -> dict[str, object]:
    return {
        "content_sha256": snapshot.content_sha256,
        "ctime_ns": snapshot.ctime_ns,
        "device": snapshot.device,
        "entry_kind": snapshot.entry_kind,
        "inode": snapshot.inode,
        "link_target": snapshot.link_target,
        "mode": snapshot.mode,
        "mtime_ns": snapshot.mtime_ns,
        "relative_path": snapshot.relative_path,
        "size": snapshot.size,
        "uid": snapshot.uid,
    }


def _instance_identity(
    ancestor_descriptors: tuple[int, ...],
    root_descriptor: int,
    snapshots: tuple[RuntimeEntrySnapshot, ...],
) -> str:
    try:
        ancestors = [_ancestor_identity(os.fstat(item)) for item in ancestor_descriptors]
        root = _metadata_identity(os.fstat(root_descriptor))
    except OSError as error:
        raise CandidateRuntimeError("runtime_ancestor_mismatch") from error
    return _digest(
        {
            "ancestors": ancestors,
            "entries": [_snapshot_identity(item) for item in snapshots],
            "root": root,
        },
    )


def _portable_projection_from_descriptor(
    descriptor: int,
    runtime_root: Path,
) -> tuple[PortableRuntimeProjection, tuple[RuntimeEntrySnapshot, ...]]:
    snapshots, entries = _walk_runtime_descriptor(descriptor, runtime_root)
    return PortableRuntimeProjection(_digest(entries), len(entries)), snapshots


def portable_runtime_projection() -> PortableRuntimeProjection:
    root = _official_runtime_root()
    ancestors, descriptor = _open_descriptor_chain(root)
    try:
        projection, _snapshots = _portable_projection_from_descriptor(descriptor, root)
        return projection
    finally:
        os.close(descriptor)
        for ancestor in reversed(ancestors):
            os.close(ancestor)


def _runtime_roots(runtime_root: Path) -> tuple[Path, Path, Path]:
    base_root = runtime_root / "_python"
    stdlib = base_root / "lib" / "python3.12"
    platstdlib = stdlib / "lib-dynload"
    if not base_root.is_dir() or not stdlib.is_dir() or not platstdlib.is_dir():
        raw_stdlib = sysconfig.get_path("stdlib")
        raw_platstdlib = sysconfig.get_config_var("DESTSHARED")
        if not raw_stdlib or not isinstance(raw_platstdlib, str) or not raw_platstdlib:
            raise ValueError("source matrix standard library roots are unavailable")
        base_root = Path(sys.base_prefix)
        stdlib = Path(raw_stdlib)
        platstdlib = Path(raw_platstdlib)
    return base_root, stdlib, platstdlib


def _runtime_identity() -> _RuntimeIdentity:
    executable = Path(sys.executable).absolute()
    if not executable.is_file():
        raise ValueError("source matrix interpreter is unavailable")
    cache_tag = sys.implementation.cache_tag
    if not cache_tag:
        raise ValueError("source matrix interpreter cache tag is unavailable")
    runtime_root = executable.parent.parent
    base_root, stdlib_root, platstdlib_root = _runtime_roots(runtime_root)
    executable_sha256, executable_projection_sha256 = _runtime_linked_file_projection(executable)
    base_executable = base_root / "bin" / "python3.12"
    if not base_executable.is_file():
        base_executable = Path(str(getattr(sys, "_base_executable", sys.executable)))
    base_executable_sha256 = _runtime_linked_file_projection(base_executable)[0]
    stdlib_merkle_sha256, stdlib_file_count = _runtime_tree_projection(stdlib_root)
    platstdlib_merkle_sha256, platstdlib_file_count = _runtime_tree_projection(platstdlib_root)
    shared_files = sorted(
        (
            path
            for path in (base_root / "lib").glob("libpython*")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda item: os.fsencode(item.name),
    )
    shared_material = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in shared_files
    }
    startup_files = tuple(
        path
        for path in (runtime_root / "pyvenv.cfg",)
        if path.is_file() and not path.is_symlink()
    )
    startup_material = {
        path.name: _runtime_file_projection(path) for path in startup_files
    }
    build_config = {
        key: hashlib.sha256(
            str(sysconfig.get_config_var(key))
            .replace(os.fspath(runtime_root), "{runtime}")
            .encode(),
        ).hexdigest()
        for key in _PYTHON_BUILD_CONFIG_KEYS
    }
    if os.environ.get(_OFFICIAL_LAUNCHER_ENV) == "1":
        layout = _runtime_layout_from_process_without_creation(runtime_root)
        policy_sha256 = _digest(_scalar_material(runtime_root, layout))
    else:
        policy_sha256 = _digest(
            {
                "implementation": sys.implementation.name,
                "version": platform.python_version(),
            },
        )
    return _RuntimeIdentity(
        implementation=sys.implementation.name,
        cache_tag=cache_tag,
        python_version=platform.python_version(),
        platform=platform.platform(),
        executable_sha256=executable_sha256,
        executable_projection_sha256=executable_projection_sha256,
        base_executable_sha256=base_executable_sha256,
        python_build_sha256=_digest(platform.python_build()),
        build_config_sha256=_digest(build_config),
        stdlib_merkle_sha256=stdlib_merkle_sha256,
        stdlib_file_count=stdlib_file_count,
        platstdlib_merkle_sha256=platstdlib_merkle_sha256,
        platstdlib_file_count=platstdlib_file_count,
        shared_runtime_sha256=_digest(shared_material),
        shared_runtime_file_count=len(shared_material),
        startup_configuration_sha256=_digest(startup_material),
        startup_configuration_file_count=len(startup_material),
        interpreter_policy_sha256=policy_sha256,
        importable_suffixes=tuple(sorted(_IMPORTABLE_ARTIFACT_SUFFIXES)),
    )


def _runtime_layout_from_process_without_creation(runtime_root: Path) -> ExternalRunLayout:
    raw_cache = sys.pycache_prefix
    raw_tmp = os.environ.get("TMPDIR")
    if raw_cache is None or raw_tmp is None:
        raise CandidateRuntimeError("external_layout_invalid")
    coordinator_cache = Path(raw_cache)
    coordinator_work = Path.cwd()
    coordinator_tmp = Path(raw_tmp)
    root = coordinator_cache.parent
    layout = ExternalRunLayout(
        root=root,
        coordinator_cache=coordinator_cache,
        coordinator_work=coordinator_work,
        coordinator_tmp=coordinator_tmp,
        child_cache=root / "child-cache",
        child_work=root / "child-work",
        child_tmp=root / "child-tmp",
    )
    if (
        coordinator_work.parent != root
        or coordinator_tmp.parent != root
        or root.parent != runtime_root.parent
    ):
        raise CandidateRuntimeError("external_layout_invalid")
    return layout


def _dependency_distributions() -> dict[str, _DependencyDistribution]:  # noqa: C901, PLR0912, PLR0915
    try:
        root = distribution("saxo-bank-mcp")
    except PackageNotFoundError as error:
        raise ValueError("installed source matrix distribution is unavailable") from error
    _validate_directory_entry_tree(
        Path(str(root.locate_file(""))),
        scope="dependency installation directory",
    )
    selected: dict[str, set[str]] = {}
    pending: list[tuple[Distribution, set[str]]] = [(root, set())]
    processed: dict[str, set[str]] = {}
    while pending:
        current, extras = pending.pop()
        current_name = canonicalize_name(current.metadata["Name"])
        selected.setdefault(current_name, set()).update(extras)
        previous = processed.get(current_name)
        if previous is not None and extras <= previous:
            continue
        processed.setdefault(current_name, set()).update(extras)
        for raw_requirement in current.requires or ():
            requirement = Requirement(raw_requirement)
            marker = requirement.marker
            if marker is not None and not (
                marker.evaluate({"extra": ""})
                or any(marker.evaluate({"extra": extra}) for extra in selected[current_name])
            ):
                continue
            dependency_name = canonicalize_name(requirement.name)
            try:
                dependency = distribution(dependency_name)
            except PackageNotFoundError as error:
                raise ValueError(
                    f"source matrix dependency {dependency_name} is unavailable",
                ) from error
            new_extras = set(requirement.extras)
            known = selected.setdefault(dependency_name, set())
            if dependency_name not in processed or not new_extras <= known:
                known.update(new_extras)
                pending.append((dependency, set(known)))
    selected.pop("saxo-bank-mcp", None)
    result: dict[str, _DependencyDistribution] = {}
    for name in sorted(selected):
        dependency = distribution(name)
        dependency_files = dependency.files
        if dependency_files is None:
            raise ValueError(f"source matrix dependency {name} has no RECORD")
        file_map: dict[str, str] = {}
        installer_metadata: dict[str, str] = {}
        for package_path in dependency_files:
            relative = str(package_path).replace(os.sep, "/")
            path = Path(str(dependency.locate_file(package_path)))
            if ".." in Path(relative).parts:
                if _excluded_outside_site_record_path(relative):
                    continue
                raise ValueError(
                    f"source matrix dependency {name} RECORD path is invalid",
                )
            metadata = _validated_closure_entry_metadata(path, scope=f"dependency {name}")
            if ".dist-info/" in relative and Path(relative).name in _INSTALLER_GENERATED_METADATA:
                if Path(relative).name == "INSTALLER":
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError(f"source matrix dependency {name} installer is invalid")
                    normalized = path.read_text(encoding="utf-8").strip().casefold()
                    installer_metadata["INSTALLER"] = hashlib.sha256(
                        normalized.encode(),
                    ).hexdigest()
                continue
            if not _safe_artifact_path(relative) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"source matrix dependency {name} file is invalid")
            file_map[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if not file_map or installer_metadata.get("INSTALLER") is None:
            raise ValueError(f"source matrix dependency {name} projection is incomplete")
        result[name] = _DependencyDistribution(
            version=dependency.version,
            files=dict(sorted(file_map.items())),
            installer_metadata=installer_metadata,
        )
    return result


def _root_installed_metadata_projection() -> dict[str, str]:
    installed_distribution = distribution("saxo-bank-mcp")
    installer = next(
        (
            Path(str(installed_distribution.locate_file(item)))
            for item in installed_distribution.files or ()
            if str(item).replace(os.sep, "/").endswith(".dist-info/INSTALLER")
        ),
        None,
    )
    if installer is None or not installer.is_file() or installer.is_symlink():
        raise ValueError("installed source matrix installer projection is unavailable")
    normalized = installer.read_text(encoding="utf-8").strip().casefold()
    return {"INSTALLER": hashlib.sha256(normalized.encode()).hexdigest()}


def _installer_entrypoint_projection(value: str, console_scripts: Mapping[str, str]) -> bool:
    path = Path(value)
    return path.name in console_scripts and "bin" in path.parts and value not in console_scripts


def _installer_entrypoint_projection_valid(  # noqa: PLR0911
    path: Path,
    target: str,
    *,
    interpreter: Path | None = None,
) -> bool:
    try:
        metadata = _validated_closure_entry_metadata(
            path,
            scope="installed console entry point",
        )
    except ValueError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o111 == 0:
        return False
    target_parts = target.split(":")
    if (
        len(target_parts) != _ENTRYPOINT_TARGET_PART_COUNT
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", target_parts[0]) is None
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target_parts[1]) is None
    ):
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, ValueError):
        return False
    module, callable_name = target_parts
    expected_body = (
        "# -*- coding: utf-8 -*-\n"
        "import sys\n"
        f"from {module} import {callable_name}\n"
        'if __name__ == "__main__":\n'
        '    if sys.argv[0].endswith("-script.pyw"):\n'
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        '    elif sys.argv[0].endswith(".exe"):\n'
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        f"    sys.exit({callable_name}())\n"
    )
    expected_interpreter = Path(sys.executable) if interpreter is None else interpreter
    if not lines:
        return False
    first_line = lines[0]
    body_lines = lines[1:]
    if (
        first_line == "#!/bin/sh"
        and len(lines) >= _PORTABLE_ENTRYPOINT_MIN_LINES
        and lines[1].startswith("'''exec' '")
        and lines[1].endswith("' \"$0\" \"$@\"")
        and lines[2] == "' '''"
    ):
        raw_interpreter = lines[1].removeprefix("'''exec' '").removesuffix(
            "' \"$0\" \"$@\"",
        )
        shebang_interpreter = Path(raw_interpreter)
        body_lines = lines[3:]
    elif first_line.startswith("#!/"):
        shebang_interpreter = Path(first_line.removeprefix("#!"))
    else:
        return False
    try:
        interpreter_matches = (
            shebang_interpreter.resolve(strict=True)
            == expected_interpreter.resolve(strict=True)
        )
    except OSError:
        return False
    return interpreter_matches and "\n".join(body_lines) + "\n" == expected_body


def _validate_installed_execution_closure(manifest: _SourceMatrixCandidateManifest) -> None:
    if _root_installed_metadata_projection() != dict(manifest.installed_metadata_projection):
        raise ValueError("installed source matrix metadata projection mismatch")
    installed_distribution = distribution("saxo-bank-mcp")
    actual_console_scripts = {
        entry_point.name: entry_point.value
        for entry_point in installed_distribution.entry_points
        if entry_point.group == "console_scripts"
    }
    if actual_console_scripts != dict(manifest.console_scripts):
        raise ValueError("installed source matrix entry point projection mismatch")
    distribution_files = installed_distribution.files or ()
    wrappers = {
        Path(str(installed_distribution.locate_file(item))).name: Path(
            str(installed_distribution.locate_file(item)),
        )
        for item in distribution_files
        if _installer_entrypoint_projection(str(item), actual_console_scripts)
    }
    if set(wrappers) != set(actual_console_scripts) or any(
        not _installer_entrypoint_projection_valid(
            wrappers[name],
            target,
            interpreter=Path(sys.executable),
        )
        for name, target in actual_console_scripts.items()
    ):
        raise ValueError("installed console entry point projection is invalid")


def _installed_candidate_files(  # noqa: C901
    exclusions: Sequence[str],
) -> dict[str, str]:
    try:
        installed_distribution = distribution("saxo-bank-mcp")
    except PackageNotFoundError as error:
        raise ValueError("installed source matrix distribution is unavailable") from error
    distribution_files = installed_distribution.files
    if distribution_files is None:
        raise ValueError("installed source matrix RECORD is unavailable")
    _validate_directory_entry_tree(
        Path(str(installed_distribution.locate_file(""))),
        scope="installed package directory",
    )
    excluded = set(exclusions)
    console_scripts = {
        entry_point.name: entry_point.value
        for entry_point in installed_distribution.entry_points
        if entry_point.group == "console_scripts"
    }
    observed_exclusions: set[str] = set()
    result: dict[str, str] = {}
    for package_path in distribution_files:
        name = str(package_path).replace(os.sep, "/")
        normalized_name = (
            f"../../../{name}"
            if name in {f"bin/{item}" for item in _OFFICIAL_LAUNCHER_NAMES}
            else name
        )
        path = Path(str(installed_distribution.locate_file(package_path)))
        metadata = _validated_closure_entry_metadata(path, scope="installed package")
        if _installer_entrypoint_projection(name, console_scripts):
            if not _installer_entrypoint_projection_valid(
                path,
                console_scripts[path.name],
            ):
                raise ValueError("installed console entry point projection is invalid")
            continue
        if not _safe_artifact_path(name) and not _official_launcher_record_path(normalized_name):
            raise ValueError("installed source matrix path is invalid")
        if ".dist-info/" in name and Path(name).name in _INSTALLER_GENERATED_METADATA:
            continue
        if name in excluded:
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("installed source matrix exclusion is unavailable")
            observed_exclusions.add(name)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("installed source matrix file is unavailable")
        result[normalized_name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed_exclusions != excluded:
        raise ValueError("installed source matrix exclusions are incomplete")
    return dict(sorted(result.items()))


def _validate_candidate_manifest_paths(manifest: _SourceMatrixCandidateManifest) -> None:
    candidate_resource = "saxo_bank_mcp/_analytics_source_matrix/source_matrix_candidate.json"
    if (
        manifest.source_exclusions != ("data/analytics/source_matrix_candidate.json",)
        or len(manifest.installed_exclusions) != _SELF_REFERENCE_EXCLUSION_COUNT
        or candidate_resource not in manifest.installed_exclusions
        or not any(name.endswith(".dist-info/RECORD") for name in manifest.installed_exclusions)
    ):
        raise ValueError("source matrix candidate exclusions are invalid")
    if not manifest.source_files or any(
        not _safe_artifact_path(name) or _SHA256_PATTERN.fullmatch(sha256) is None
        for name, sha256 in manifest.source_files.items()
    ):
        raise ValueError("source matrix candidate file map is invalid")
    if not manifest.installed_files or any(
        (not _safe_artifact_path(name) and not _official_launcher_record_path(name))
        or _SHA256_PATTERN.fullmatch(sha256) is None
        for name, sha256 in manifest.installed_files.items()
    ):
        raise ValueError("source matrix candidate file map is invalid")
    if set(manifest.source_files) & set(manifest.source_exclusions) or set(
        manifest.installed_files,
    ) & set(manifest.installed_exclusions):
        raise ValueError("source matrix candidate closure includes an exclusion")
    if (
        not manifest.dependency_distributions
        or any(
            canonicalize_name(name) != name or not item.version or not item.files
            for name, item in manifest.dependency_distributions.items()
        )
        or dict(manifest.installed_metadata_projection)
        != {"INSTALLER": hashlib.sha256(_EXPECTED_INSTALLER.encode()).hexdigest()}
        or not manifest.console_scripts
    ):
        raise ValueError("source matrix installed execution projection is invalid")


def _manifest_identity(
    manifest: _SourceMatrixCandidateManifest,
) -> SourceMatrixCandidateIdentity:
    catalog_sha256 = source_contract_catalog_sha256()
    source_build_sha256 = _digest(dict(manifest.source_files))
    installed_build_sha256 = _digest(dict(manifest.installed_files))
    child_bootstrap_sha256 = hashlib.sha256(CHILD_BOOTSTRAP.encode("utf-8")).hexdigest()
    harness_sha256 = _digest(
        {
            "child_bootstrap_sha256": child_bootstrap_sha256,
            "console_scripts": dict(manifest.console_scripts),
            "dependency_distributions": {
                name: item.model_dump(mode="json")
                for name, item in manifest.dependency_distributions.items()
            },
            "installed_build_sha256": installed_build_sha256,
            "installed_exclusions": manifest.installed_exclusions,
            "installed_metadata_projection": dict(manifest.installed_metadata_projection),
            "portable_runtime_entry_count": manifest.portable_runtime_entry_count,
            "portable_runtime_tree_sha256": manifest.portable_runtime_tree_sha256,
            "runtime_identity": manifest.runtime_identity.model_dump(mode="json"),
            "schema_version": manifest.schema_version,
            "source_build_sha256": source_build_sha256,
            "source_exclusions": manifest.source_exclusions,
            "source_wheel_projection_sha256": manifest.source_wheel_projection_sha256,
        },
    )
    candidate_sha256 = _digest(
        {
            "harness_build_sha256": harness_sha256,
            "source_contract_catalog_sha256": catalog_sha256,
        },
    )
    if (
        catalog_sha256 != manifest.source_contract_catalog_sha256
        or source_build_sha256 != manifest.source_build_sha256
        or installed_build_sha256 != manifest.installed_build_sha256
        or child_bootstrap_sha256 != manifest.child_bootstrap_sha256
        or harness_sha256 != manifest.harness_build_sha256
        or candidate_sha256 != manifest.candidate_identity_sha256
    ):
        raise ValueError("installed source matrix candidate identity mismatch")
    return SourceMatrixCandidateIdentity(
        source_contract_catalog_sha256=catalog_sha256,
        harness_build_sha256=harness_sha256,
        candidate_identity_sha256=candidate_sha256,
    )


def _load_candidate_manifest() -> tuple[_SourceMatrixCandidateManifest, bool]:
    source_mode = _CANDIDATE_SOURCE_PATH.is_file()
    if source_mode:
        text = _CANDIDATE_SOURCE_PATH.read_text(encoding="utf-8")
    else:
        resource = files("saxo_bank_mcp").joinpath(
            _CANDIDATE_RESOURCE_DIR,
            _CANDIDATE_RESOURCE_NAME,
        )
        if not resource.is_file():
            raise ValueError("source matrix candidate manifest is unavailable")
        text = resource.read_text(encoding="utf-8")
    return _SourceMatrixCandidateManifest.model_validate_json(text, strict=True), source_mode


def source_matrix_candidate_identity() -> SourceMatrixCandidateIdentity:
    manifest, source_mode = _load_candidate_manifest()
    _validate_candidate_manifest_paths(manifest)
    repository_root = Path(__file__).resolve().parents[2] if source_mode else None
    actual_files = (
        _source_candidate_files(cast("Path", repository_root))
        if source_mode
        else _installed_candidate_files(manifest.installed_exclusions)
    )
    expected_files = dict(manifest.source_files) if source_mode else dict(manifest.installed_files)
    if actual_files != expected_files:
        raise ValueError("source matrix candidate artifact closure mismatch")
    if _runtime_identity() != manifest.runtime_identity:
        raise ValueError("source matrix runtime identity mismatch")
    if _dependency_distributions() != dict(manifest.dependency_distributions):
        raise ValueError("source matrix dependency closure mismatch")
    if not source_mode:
        _validate_installed_execution_closure(manifest)
        portable = portable_runtime_projection()
        if (
            portable.sha256 != manifest.portable_runtime_tree_sha256
            or portable.entry_count != manifest.portable_runtime_entry_count
        ):
            raise ValueError("source matrix portable runtime identity mismatch")
    return _manifest_identity(manifest)


def _validate_scalar_projection(
    manifest: _SourceMatrixCandidateManifest,
    runtime_root: Path,
    layout: ExternalRunLayout,
) -> None:
    if (
        sys.implementation.name != "cpython"
        or sys.version_info[:2] != (3, 12)
        or sys.get_int_max_str_digits() != _CPYTHON_312_DEFAULT_INT_MAX_STR_DIGITS
        or sys.flags.isolated != 1
        or sys.flags.dont_write_bytecode != 1
        or sys.flags.no_site != 1
        or sys.flags.ignore_environment != 1
        or sys.flags.safe_path is not True
        or Path(sys.executable).absolute() != runtime_root / "bin" / "python3.12"
        or _digest(_scalar_material(runtime_root, layout))
        != manifest.runtime_identity.interpreter_policy_sha256
    ):
        raise CandidateRuntimeError("runtime_scalar_mismatch")


def _open_seal(  # noqa: PLR0913
    runtime_root: Path,
    manifest: _SourceMatrixCandidateManifest,
    identity: SourceMatrixCandidateIdentity,
    layout: ExternalRunLayout,
    *,
    validate_scalar: bool,
    require_official_layout: bool,
) -> CandidateRuntimeSeal:
    _validate_external_layout(
        layout,
        runtime_root,
        require_official_name=require_official_layout,
    )
    if validate_scalar:
        _validate_scalar_projection(manifest, runtime_root, layout)
    ancestors, root_descriptor = _open_descriptor_chain(runtime_root)
    try:
        portable, snapshots = _portable_projection_from_descriptor(
            root_descriptor,
            runtime_root,
        )
        if (
            portable.sha256 != manifest.portable_runtime_tree_sha256
            or portable.entry_count != manifest.portable_runtime_entry_count
        ):
            raise CandidateRuntimeError("runtime_identity_mismatch")  # noqa: TRY301
        instance_sha256 = _instance_identity(ancestors, root_descriptor, snapshots)
        return CandidateRuntimeSeal(
            identity=identity,
            portable_identity_sha256=portable.sha256,
            instance_identity_sha256=instance_sha256,
            runtime_root=runtime_root,
            executable=runtime_root / "bin" / "python3.12",
            site_packages=runtime_root / "lib" / "python3.12" / "site-packages",
            root_descriptor=root_descriptor,
            ancestor_descriptors=ancestors,
            entry_snapshot=snapshots,
        )
    except Exception:
        os.close(root_descriptor)
        for ancestor in reversed(ancestors):
            os.close(ancestor)
        raise


def open_candidate_runtime_seal() -> tuple[CandidateRuntimeSeal, ExternalRunLayout]:
    runtime_root = _official_runtime_root()
    layout = _runtime_layout_from_process(runtime_root)
    try:
        manifest, source_mode = _load_candidate_manifest()
        if source_mode:
            raise CandidateRuntimeError("runtime_identity_mismatch")  # noqa: TRY301
        identity = source_matrix_candidate_identity()
        seal = _open_seal(
            runtime_root,
            manifest,
            identity,
            layout,
            validate_scalar=True,
            require_official_layout=True,
        )
    except Exception as error:
        try:
            _remove_empty_directories(
                (layout.child_cache, layout.child_work, layout.child_tmp),
            )
        except OSError:
            raise CandidateRuntimeError("external_layout_invalid") from None
        if isinstance(error, CandidateRuntimeError):
            raise
        raise CandidateRuntimeError("runtime_identity_mismatch") from None
    return seal, layout


def _open_candidate_runtime_seal_for_test(  # pyright: ignore[reportUnusedFunction]
    runtime_root: Path,
    manifest_text: str,
    layout: ExternalRunLayout,
) -> CandidateRuntimeSeal:
    try:
        manifest = _SourceMatrixCandidateManifest.model_validate_json(
            manifest_text,
            strict=True,
        )
        _validate_candidate_manifest_paths(manifest)
        identity = _manifest_identity(manifest)
    except Exception as error:
        raise CandidateRuntimeError("runtime_identity_mismatch") from error
    return _open_seal(
        runtime_root,
        manifest,
        identity,
        layout,
        validate_scalar=False,
        require_official_layout=False,
    )


def _descriptor_identity_matches(left: int, right: int) -> bool:
    try:
        first = os.fstat(left)
        second = os.fstat(right)
    except OSError as error:
        raise CandidateRuntimeError("runtime_ancestor_mismatch") from error
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def revalidate_candidate_runtime(
    seal: CandidateRuntimeSeal,
    layout: ExternalRunLayout,
) -> None:
    _validate_external_layout(
        layout,
        seal.runtime_root,
        require_official_name=_RUN_ROOT_PATTERN.fullmatch(layout.root.name) is not None,
    )
    reopened_ancestors, reopened_root = _open_descriptor_chain(seal.runtime_root)
    try:
        if len(reopened_ancestors) != len(seal.ancestor_descriptors) or any(
            not _descriptor_identity_matches(current, original)
            for current, original in zip(
                reopened_ancestors,
                seal.ancestor_descriptors,
                strict=True,
            )
        ) or not _descriptor_identity_matches(reopened_root, seal.root_descriptor):
            raise CandidateRuntimeError("runtime_ancestor_mismatch")
    finally:
        os.close(reopened_root)
        for descriptor in reversed(reopened_ancestors):
            os.close(descriptor)
    portable, snapshots = _portable_projection_from_descriptor(
        seal.root_descriptor,
        seal.runtime_root,
    )
    if (
        portable.sha256 != seal.portable_identity_sha256
        or snapshots != seal.entry_snapshot
        or _instance_identity(
            seal.ancestor_descriptors,
            seal.root_descriptor,
            snapshots,
        )
        != seal.instance_identity_sha256
    ):
        raise CandidateRuntimeError("runtime_identity_mismatch")


def close_candidate_runtime_seal(seal: CandidateRuntimeSeal) -> None:
    descriptors = (seal.root_descriptor, *reversed(seal.ancestor_descriptors))
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            continue


def prepare_child_run_paths(
    seal: CandidateRuntimeSeal,
    layout: ExternalRunLayout,
) -> ChildBootstrapPaths:
    _validate_external_layout(
        layout,
        seal.runtime_root,
        require_official_name=_RUN_ROOT_PATTERN.fullmatch(layout.root.name) is not None,
    )
    return ChildBootstrapPaths(
        runtime_root=seal.runtime_root,
        executable=seal.executable,
        site_packages=seal.site_packages,
        pycache_prefix=layout.child_cache,
        workdir=layout.child_work,
        tmpdir=layout.child_tmp,
    )


__all__ = [
    "CandidateRuntimeError",
    "CandidateRuntimeSeal",
    "ExternalRunLayout",
    "PortableRuntimeProjection",
    "RuntimeEntrySnapshot",
    "SourceMatrixCandidateIdentity",
    "close_candidate_runtime_seal",
    "open_candidate_runtime_seal",
    "portable_runtime_projection",
    "prepare_child_run_paths",
    "revalidate_candidate_runtime",
    "source_matrix_candidate_identity",
]
