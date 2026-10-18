"""Read-only role: refuses a tools/call of a tool not annotated read-only before it runs.

The bearer middleware (auth.py) puts the role of the presented token into the ASGI scope; this
SDK extension intercepts every ``tools/call`` (also of a tool registered without the ``tool()``
helper, or of an unknown name) and lets it through only for the gateway's token or for a tool
whose registered annotations say ``readOnlyHint: true``. A tool missing from the map is a write.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.extension import Extension
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .auth import ROLE_FULL, ROLE_KEY, ROLE_READ_ONLY
from .errors import READ_ONLY, with_kind

log = logging.getLogger("garmin_coach_mcp.access")

READ_ONLY_REFUSAL = (
    "Refused: this token has read-only access to garmin-mcp, and this tool is not annotated read-only."
)


def request_role(ctx: Any) -> str | None:
    """Role the bearer middleware stored for this HTTP request; None when unknown."""
    scope = getattr(getattr(ctx, "request", None), "scope", None)
    role = scope.get(ROLE_KEY) if isinstance(scope, dict) else None
    return role if role in (ROLE_FULL, ROLE_READ_ONLY) else None


class ReadOnlyGuard(Extension):
    identifier = "local.garmin-coach-mcp/read-only-guard"

    def __init__(self) -> None:
        self._read_only: dict[str, bool] = {}

    def register(self, name: str, annotations: ToolAnnotations | None) -> None:
        self._read_only[name] = annotations is not None and annotations.read_only_hint is True

    def allows(self, role: str | None, name: str) -> bool:
        return role == ROLE_FULL or self._read_only.get(name, False)

    async def intercept_tool_call(self, params: Any, ctx: Any, call_next: Any) -> Any:
        if self.allows(request_role(ctx), params.name):
            return await call_next(ctx)
        shown = params.name if params.name in self._read_only else "<unregistered>"
        log.warning("tool=%s status=refused reason=read_only", shown)
        refusal = CallToolResult(content=[TextContent(type="text", text=READ_ONLY_REFUSAL)], is_error=True)
        return with_kind(refusal, READ_ONLY)
