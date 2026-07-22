from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final, overload

import mcp.types
from fastmcp import FastMCP
from fastmcp.server.tasks.config import TaskMeta
from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import FunctionTool, Tool, ToolResult
from fastmcp.utilities.versions import VersionSpec
from pydantic import ValidationError

_FASTMCP_OPERATIONS_LOGGER_NAME: Final = "fastmcp.server.mixins.mcp_operations"
_FASTMCP_SERVER_LOGGER_NAME: Final = "fastmcp.server.server"
_RAW_TOOL_ARGUMENT_MESSAGE: Final = "Handler called: call_tool %s with %s"
_RAW_VALIDATION_MESSAGE: Final = "Invalid arguments for tool %r: %s"
_GENERIC_VALIDATION_MESSAGE: Final = "Tool input validation failed."
_GENERIC_EXECUTION_MESSAGE: Final = "Tool execution failed."
_GENERIC_UNKNOWN_TOOL_MESSAGE: Final = "Unknown tool."
_TOOL_EXECUTION_ERRORS: Final[tuple[type[Exception], ...]] = (Exception,)


class _UnsafeToolInputFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if str(record.msg) == _RAW_VALIDATION_MESSAGE:
            record.msg = _GENERIC_VALIDATION_MESSAGE
            record.args = ()
        return _RAW_TOOL_ARGUMENT_MESSAGE not in str(record.msg)


_UNSAFE_TOOL_INPUT_FILTER: Final = _UnsafeToolInputFilter()


def install_fastmcp_argument_log_filter() -> None:
    for logger_name in (
        _FASTMCP_OPERATIONS_LOGGER_NAME,
        _FASTMCP_SERVER_LOGGER_NAME,
    ):
        logger = logging.getLogger(logger_name)
        if _UNSAFE_TOOL_INPUT_FILTER not in logger.filters:
            logger.addFilter(_UNSAFE_TOOL_INPUT_FILTER)


class _ValidationSafeFunctionTool(FunctionTool):
    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            return await super().run(arguments)
        except ValidationError:
            return ToolResult(
                content=_GENERIC_VALIDATION_MESSAGE,
                structured_content={
                    "status": "invalid_arguments",
                    "message": _GENERIC_VALIDATION_MESSAGE,
                },
                is_error=True,
            )
        except _TOOL_EXECUTION_ERRORS:
            return generic_tool_error_result()


def generic_tool_error_result() -> ToolResult:
    return ToolResult(
        content=_GENERIC_EXECUTION_MESSAGE,
        structured_content={
            "status": "tool_error",
            "message": _GENERIC_EXECUTION_MESSAGE,
        },
        is_error=True,
    )


def _validation_safe_tool(tool: Tool) -> Tool:
    if not isinstance(tool, FunctionTool) or isinstance(tool, _ValidationSafeFunctionTool):
        return tool
    return _ValidationSafeFunctionTool.model_validate(tool, from_attributes=True)


class _ValidationSafetyTransform(Transform):
    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [_validation_safe_tool(tool) for tool in tools]

    async def get_tool(
        self,
        name: str,
        call_next: GetToolNext,
        *,
        version: VersionSpec | None = None,
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None:
            return None
        return _validation_safe_tool(tool)


FASTMCP_VALIDATION_SAFETY_TRANSFORM: Final[Transform] = _ValidationSafetyTransform()


class SafeFastMCP(FastMCP):
    @overload
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        version: VersionSpec | None = None,
        run_middleware: bool = True,
        task_meta: None = None,
    ) -> ToolResult: ...

    @overload
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        version: VersionSpec | None = None,
        run_middleware: bool = True,
        task_meta: TaskMeta,
    ) -> mcp.types.CreateTaskResult: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        version: VersionSpec | None = None,
        run_middleware: bool = True,
        task_meta: TaskMeta | None = None,
    ) -> ToolResult | mcp.types.CreateTaskResult:
        if await self.get_tool(name, version=version) is None:
            return ToolResult(
                content=_GENERIC_UNKNOWN_TOOL_MESSAGE,
                structured_content={
                    "status": "unknown_tool",
                    "message": _GENERIC_UNKNOWN_TOOL_MESSAGE,
                },
                is_error=True,
            )
        return await super().call_tool(
            name,
            arguments,
            version=version,
            run_middleware=run_middleware,
            task_meta=task_meta,
        )
