"""Error kinds: the machine-readable half of every garmin-mcp error (PR 31).

A tool error keeps its text for the model and carries its kind in the result's ``_meta`` under
``ERROR_KIND_KEY``; an error section carries it next to its text as ``"error_kind"``. coach-mcp reads the kind
(``rate_limited`` and ``garmin_auth``) instead of matching the text. The kinds are a contract: README.md
("Error kinds") lists them and ``tests/contract/garmin_answers.json`` pins the answers coach-mcp reads.
"""

from __future__ import annotations

import contextvars
from typing import Any

from mcp.server.extension import Extension
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

ERROR_KIND_KEY = "local.garmin-coach-mcp/error_kind"

GARMIN_AUTH = "garmin_auth"  # not logged in, or Garmin rejected the stored session
RATE_LIMITED = "rate_limited"  # Garmin Connect answered HTTP 429
NOT_FOUND = "not_found"  # Garmin Connect has no such item
GARMIN_UNAVAILABLE = "garmin_unavailable"  # Garmin Connect failed or could not be reached
UNEXPECTED_ANSWER = "unexpected_answer"  # Garmin answered, but not in a shape garmin-mcp can use
INVALID_REQUEST = "invalid_request"  # the call was rejected: its arguments, or an unknown tool
READ_ONLY = "read_only"  # the read-only token asked for a tool that is not annotated read-only
INTERNAL = "internal"  # garmin-mcp itself failed
ERROR_KINDS = (
    GARMIN_AUTH,
    RATE_LIMITED,
    NOT_FOUND,
    GARMIN_UNAVAILABLE,
    UNEXPECTED_ANSWER,
    INVALID_REQUEST,
    READ_ONLY,
    INTERNAL,
)


class GarminError(ToolError):
    """A tool failure with its kind; the text is what the model reads."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def kind_of(exc: BaseException) -> str:
    """The kind of an exception a tool raised: its own; ``invalid_request`` for a plain ToolError (the argument
    checks of the tool modules); ``internal`` for anything else (a crash)."""
    if isinstance(exc, GarminError):
        return exc.kind
    if isinstance(exc, ToolError):
        return INVALID_REQUEST
    return INTERNAL


# The kind of the failing tools/call: ErrorKinds puts an empty holder here, server._logged fills it in.
_CALL_KIND: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("garmin_call_kind")


def record_kind(exc: BaseException) -> None:
    """Remember the kind of the exception the running tool call raised (no-op outside a tools/call)."""
    holder = _CALL_KIND.get(None)
    if holder is not None:
        holder["kind"] = kind_of(exc)


def with_kind(result: CallToolResult, kind: str) -> CallToolResult:
    return result.model_copy(update={"meta": {**(result.meta or {}), ERROR_KIND_KEY: kind}})


class ErrorKinds(Extension):
    """Adds the kind to ``_meta`` of every error result of tools/call; the text stays the SDK's.

    The SDK turns an exception raised by a tool into ``CallToolResult(is_error=True)`` with the text
    ``Error executing tool <name>: <message>`` (only ``Error executing tool <name>`` for a crash) and logs it;
    this interceptor adds the kind and nothing else. An error the tool body never saw (arguments that fail the
    input schema, an unknown tool) is ``invalid_request``; a result that already has a kind keeps it.
    """

    identifier = "local.garmin-coach-mcp/error-kinds"

    async def intercept_tool_call(self, params: Any, ctx: Any, call_next: Any) -> Any:
        holder: dict[str, str] = {}
        token = _CALL_KIND.set(holder)
        try:
            result = await call_next(ctx)
        finally:
            _CALL_KIND.reset(token)
        if isinstance(result, CallToolResult) and result.is_error and ERROR_KIND_KEY not in (result.meta or {}):
            return with_kind(result, holder.get("kind", INVALID_REQUEST))
        return result
