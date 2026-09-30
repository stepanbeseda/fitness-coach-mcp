"""Authenticated reverse proxy for the MCP endpoints (Streamable HTTP).

For every request: validate the bearer token (must be bound to this endpoint),
apply network and rate limits, block denylisted tools, replace the client's
Authorization header with the backend's own secret, stream the response back and
write one audit record (method + tool name + outcome, never arguments/results).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .audit import Audit
from .config import SCOPE, Backend, Settings, read_secret
from .netctx import NetContext, net_context
from .oauth import NO_STORE, OAuthServer, read_body
from .ratelimit import RateLimiter
from .security import hash_token
from .store import Grant, Store

log = logging.getLogger("gateway.proxy")

FORWARD_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
    "mcp-method",
    "mcp-name",
)
FORWARD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "cache-control", "retry-after")
MAX_REQUEST_BYTES = 1024 * 1024
MAX_BUFFERED_RESPONSE = 8 * 1024 * 1024
OBSERVE_LIMIT = 512 * 1024
MAX_CONCURRENT_REQUESTS = 16
MAX_CONCURRENT_STREAMS = 8
_EVENT_SPLIT = re.compile(r"\r?\n\r?\n")


@dataclass
class RpcSummary:
    methods: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def method_str(self) -> str | None:
        return ",".join(self.methods) or None

    def tool_str(self) -> str | None:
        return ",".join(self.tools) or None


def _sse_data(event: str) -> tuple[list[str], str | None]:
    other, data = [], []
    for line in event.splitlines():
        if line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        else:
            other.append(line)
    return other, ("\n".join(data) if data else None)


def _outcome(message: object) -> str | None:
    if not isinstance(message, dict) or "id" not in message:
        return None
    if "error" in message:
        return "rpc_error"
    result = message.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        return "tool_error"
    return "ok" if "result" in message else None


class ResponseObserver:
    """Watches (a bounded prefix of) a response to record the JSON-RPC outcome."""

    def __init__(self, content_type: str) -> None:
        self._sse = content_type.startswith("text/event-stream")
        self._json = content_type.startswith("application/json")
        self._buffer = bytearray()
        self._seen = 0
        self.outcomes: list[str] = []

    def feed(self, chunk: bytes) -> None:
        if not (self._sse or self._json) or self._seen > OBSERVE_LIMIT:
            return
        self._seen += len(chunk)
        self._buffer.extend(chunk)
        if self._sse:
            text = self._buffer.decode("utf-8", errors="ignore")
            parts = _EVENT_SPLIT.split(text)
            for event in parts[:-1]:
                self._record(_sse_data(event)[1])
            self._buffer = bytearray(parts[-1].encode("utf-8"))

    def _record(self, data: str | None) -> None:
        if not data:
            return
        try:
            parsed = json.loads(data)
        except ValueError:
            return
        for message in parsed if isinstance(parsed, list) else [parsed]:
            if outcome := _outcome(message):
                self.outcomes.append(outcome)

    def summary(self) -> str | None:
        if self._json and self._buffer and self._seen <= OBSERVE_LIMIT:
            self._record(self._buffer.decode("utf-8", errors="ignore"))
            self._buffer = bytearray()
        if not self.outcomes:
            return None
        for worst in ("rpc_error", "tool_error"):
            if worst in self.outcomes:
                return worst
        return "ok"


def filter_tools(raw: bytes, content_type: str, denylist: frozenset[str]) -> bytes:
    """Remove denylisted tools from a tools/list response (JSON or SSE)."""

    def strip(obj: object) -> object:
        if isinstance(obj, list):
            return [strip(o) for o in obj]
        if isinstance(obj, dict):
            result = obj.get("result")
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                result["tools"] = [
                    t for t in result["tools"] if not (isinstance(t, dict) and t.get("name") in denylist)
                ]
        return obj

    text = raw.decode("utf-8")
    if content_type.startswith("application/json"):
        return json.dumps(strip(json.loads(text)), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if content_type.startswith("text/event-stream"):
        events = []
        for event in _EVENT_SPLIT.split(text):
            if not event.strip():
                continue
            other, data = _sse_data(event)
            if data is None:
                events.append(event)
                continue
            payload = json.dumps(strip(json.loads(data)), ensure_ascii=False, separators=(",", ":"))
            events.append("\n".join([*other, f"data: {payload}"]))
        return ("\n\n".join(events) + "\n\n").encode("utf-8")
    return raw


def _short_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else None


class _Slot:
    """Concurrency slot that is released exactly once."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._held = False

    async def acquire(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout)
        except TimeoutError:
            return False
        self._held = True
        return True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._semaphore.release()


class McpProxy:
    def __init__(
        self, settings: Settings, store: Store, audit: Audit, oauth: OAuthServer, client: httpx.AsyncClient
    ) -> None:
        self.settings = settings
        self.store = store
        self.audit = audit
        self.oauth = oauth
        self.client = client
        self._backend_tokens = {b.name: read_secret(b.token_file) for b in settings.backends}
        self._limit_grant = RateLimiter(settings.rate_mcp_per_grant.per_minute, settings.rate_mcp_per_grant.burst)
        self._requests = {b.name: asyncio.Semaphore(MAX_CONCURRENT_REQUESTS) for b in settings.backends}
        self._streams = {b.name: asyncio.Semaphore(MAX_CONCURRENT_STREAMS) for b in settings.backends}

    def challenge(self, backend: Backend, error: str | None = None, description: str | None = None) -> JSONResponse:
        params = [
            f'resource_metadata="{self.settings.issuer}{backend.metadata_path}"',
            f'scope="{SCOPE}"',
        ]
        if error:
            params.append(f'error="{error}"')
        if description:
            params.append(f'error_description="{description}"')
        return JSONResponse(
            {"error": error or "unauthorized", "error_description": description or "authorization required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer " + ", ".join(params), **NO_STORE},
        )

    def _client_name(self, client_id: str) -> str:
        client = self.settings.clients.get(client_id)
        return client.name if client else client_id

    async def handle(self, request: Request, backend: Backend) -> Response:
        ctx = net_context(request)
        if request.method not in ("GET", "POST", "DELETE"):
            return Response(status_code=405, headers={"Allow": "GET, POST, DELETE"})

        header = request.headers.get("authorization")
        if not header:
            return self.challenge(backend)
        scheme, _, presented = header.partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented:
            return self.challenge(backend, "invalid_request", "Bearer token required")
        if not self.oauth.token_origin_allowed(ctx):
            self.audit.event("mcp.origin_denied", backend=backend.name, src=ctx.source, ip=ctx.client_ip)
            return JSONResponse(
                {"error": "forbidden", "error_description": "not allowed from this network"}, status_code=403
            )
        found = self.store.lookup_access_token(hash_token(presented), int(time.time()))
        if found is None:
            return self.challenge(backend, "invalid_token", "The access token is invalid or expired")
        _, grant = found
        if grant.resource != backend.resource:
            self.audit.event("mcp.audience_mismatch", backend=backend.name, grant=grant.id[:8], src=ctx.source)
            return self.challenge(backend, "invalid_token", "The access token is not valid for this resource")
        if retry := self._limit_grant.hit(grant.id):
            return JSONResponse(
                {"error": "rate_limited"}, status_code=429, headers={"Retry-After": str(retry)}
            )
        return await self._forward(request, backend, grant, ctx)

    async def _forward(self, request: Request, backend: Backend, grant: Grant, ctx: NetContext) -> Response:
        started = time.monotonic()
        rpc = RpcSummary()
        body = b""
        audit_base = {
            "backend": backend.name,
            "client": self._client_name(grant.client_id),
            "grant": grant.id[:8],
            "src": ctx.source,
            "ip": ctx.client_ip if ctx.via_funnel else None,
        }

        if request.method == "POST":
            read = await read_body(request, MAX_REQUEST_BYTES)
            if read is None:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body = read
            try:
                payload = json.loads(body)
            except ValueError:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                    status_code=400,
                )
            for message in payload if isinstance(payload, list) else [payload]:
                if not isinstance(message, dict):
                    return JSONResponse(
                        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                        status_code=400,
                    )
                method = message.get("method")
                if isinstance(method, str):
                    rpc.methods.append(method[:64])
                if method == "tools/call":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    name = params.get("name")
                    tool = name[:64] if isinstance(name, str) else "?"
                    rpc.tools.append(tool)
                    if tool in backend.tool_denylist:
                        self.audit.event("mcp.tool_blocked", **audit_base, tool=tool)
                        return JSONResponse(
                            {
                                "jsonrpc": "2.0",
                                "id": message.get("id"),
                                "error": {"code": -32602, "message": f"Unknown tool: {tool}"},
                            }
                        )

        headers = {k: v for k in FORWARD_REQUEST_HEADERS if (v := request.headers.get(k)) is not None}
        headers["authorization"] = f"Bearer {self._backend_tokens[backend.name]}"
        headers["accept-encoding"] = "identity"
        headers["user-agent"] = "fitness-coach-mcp-gateway"
        is_stream = request.method == "GET"
        slot = _Slot((self._streams if is_stream else self._requests)[backend.name])
        if not await slot.acquire(timeout=1.0 if is_stream else 10.0):
            self.audit.event("mcp.busy", **audit_base, method=rpc.method_str())
            return JSONResponse({"error": "busy"}, status_code=503, headers={"Retry-After": "5"})

        upstream_request = self.client.build_request(
            request.method,
            backend.upstream,
            content=body if request.method == "POST" else None,
            headers=headers,
            timeout=httpx.Timeout(connect=5.0, read=None if is_stream else 300.0, write=30.0, pool=10.0),
        )
        try:
            upstream = await self.client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            slot.release()
            self.audit.event("mcp.backend_unavailable", **audit_base, method=rpc.method_str(), error=type(exc).__name__)
            return JSONResponse({"error": "backend_unavailable"}, status_code=502)

        if upstream.status_code in (401, 403):
            await upstream.aclose()
            slot.release()
            self.audit.event("mcp.backend_auth_failed", **audit_base, status=upstream.status_code)
            return JSONResponse({"error": "backend_misconfigured"}, status_code=502)

        response_headers = {k: v for k in FORWARD_RESPONSE_HEADERS if (v := upstream.headers.get(k)) is not None}
        content_type = upstream.headers.get("content-type", "")
        session = request.headers.get("mcp-session-id") or upstream.headers.get("mcp-session-id")
        event_name = {"GET": "mcp.stream", "DELETE": "mcp.session_end"}.get(request.method, "mcp.request")

        def finish(outcome: str | None) -> None:
            self.audit.event(
                event_name,
                **audit_base,
                method=rpc.method_str(),
                tool=rpc.tool_str(),
                status=upstream.status_code,
                outcome=outcome,
                session=_short_hash(session),
                ms=round((time.monotonic() - started) * 1000),
            )

        if backend.tool_denylist and "tools/list" in rpc.methods:
            try:
                raw = bytearray()
                async for chunk in upstream.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BUFFERED_RESPONSE:
                        raise ValueError("tools/list response too large")
            except (httpx.HTTPError, ValueError) as exc:
                finish("proxy_error")
                return JSONResponse({"error": "backend_error", "error_description": str(exc)}, status_code=502)
            finally:
                await upstream.aclose()
                slot.release()
            try:
                filtered = filter_tools(bytes(raw), content_type, backend.tool_denylist)
            except (ValueError, UnicodeDecodeError):
                log.warning("could not filter tools/list response from %s", backend.name)
                filtered = bytes(raw)
            observer = ResponseObserver(content_type)
            observer.feed(filtered)
            finish(observer.summary())
            return Response(filtered, status_code=upstream.status_code, headers=response_headers)

        observer = ResponseObserver(content_type)
        closed = False

        async def cleanup(outcome: str | None = None) -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
            slot.release()
            finish(outcome if outcome is not None else observer.summary())

        async def relay():
            outcome = None
            try:
                async for chunk in upstream.aiter_bytes():
                    observer.feed(chunk)
                    yield chunk
            except httpx.HTTPError:
                outcome = "upstream_disconnected"
            finally:
                await cleanup(outcome)

        return _RelayResponse(relay(), cleanup, status_code=upstream.status_code, headers=response_headers)


class _RelayResponse(StreamingResponse):
    """StreamingResponse that always releases the upstream connection, even when
    the client disconnects before the body iterator starts."""

    def __init__(self, content, on_close, **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._on_close()
