from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
import pyotp
import pytest
from argon2 import PasswordHasher
from starlette.testclient import TestClient

from gateway.app import create_app
from gateway.audit import Audit
from gateway.config import load_settings
from gateway.store import Store

BASE = "https://gw.test"
OWNER = "owner@github"
PASSWORD = "correct horse battery staple"
TOTP_SECRET = pyotp.random_base32(32)
ANTHROPIC_IP = "160.79.104.10"
PUBLIC_IP = "203.0.113.7"
CLAUDE = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
HEVY = f"{BASE}/hevy/mcp"
GARMIN = f"{BASE}/garmin/mcp"

TAILNET = {"Tailscale-User-Login": OWNER, "X-Forwarded-For": "100.101.102.103"}
FUNNEL_ANTHROPIC = {"Tailscale-Funnel-Request": "?1", "X-Forwarded-For": ANTHROPIC_IP}
FUNNEL_PUBLIC = {"Tailscale-Funnel-Request": "?1", "X-Forwarded-For": PUBLIC_IP}

CONFIG = """
[server]
public_base_url = "{base}"
listen_host = "127.0.0.1"
listen_port = 8080
db_path = "{db}"

[auth]
owner_tailscale_login = "{owner}"
login_tailnet_only = true
password_hash_file = "{secrets}/password_hash"
totp_secret_file = "{secrets}/totp_secret"
access_token_ttl = 3600
refresh_token_ttl = 7776000
auth_code_ttl = 120
auth_request_ttl = 600
refresh_reuse_grace = 120
login_max_failures = 5
login_failure_window = 900
login_lockout_base = 60
login_lockout_max = 3600
funnel_token_cidrs = ["160.79.104.0/21"]

[rate_limits]
public_per_ip = [600, 300]
token_endpoint = [600, 300]
authorize = [600, 300]
mcp_per_grant = [{mcp_rate}, {mcp_burst}]

[[clients]]
client_id = "https://claude.ai/oauth/mcp-oauth-client-metadata"
name = "Claude"
redirect_uris = ["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"]

[[clients]]
client_id = "https://claude.ai/oauth/claude-code-client-metadata"
name = "Claude Code"
redirect_uris = ["http://localhost/callback", "http://127.0.0.1/callback"]

[[backends]]
name = "hevy"
title = "Hevy"
path = "/hevy/mcp"
upstream = "http://hevy.internal/mcp"
token_file = "{secrets}/hevy_backend_token"
tool_denylist = ["feedback"]

[[backends]]
name = "garmin"
title = "Garmin Connect"
path = "/garmin/mcp"
upstream = "http://garmin.internal/mcp"
token_file = "{secrets}/garmin_backend_token"
tool_denylist = []
"""


@dataclass
class FakeBackend:
    """Minimal Streamable-HTTP MCP backend served through httpx.MockTransport."""

    name: str
    sse: bool = False
    down: bool = False
    reject_auth: bool = False
    requests: list[httpx.Request] = field(default_factory=list)

    @property
    def secret(self) -> str:
        return f"backend-secret-{self.name}"

    def respond(self, message: dict, session: str = "sess-1") -> httpx.Response:
        headers = {"mcp-session-id": session}
        if self.sse:
            body = f"event: message\ndata: {json.dumps(message)}\n\n".encode()
            return httpx.Response(200, headers={**headers, "content-type": "text/event-stream"}, content=body)
        return httpx.Response(200, headers={**headers, "content-type": "application/json"}, json=message)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.reject_auth or request.headers.get("authorization") != f"Bearer {self.secret}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/ping"}\n\n',
            )
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        method, msg_id = message.get("method"), message.get("id")
        if msg_id is None:
            return httpx.Response(202)
        if method == "initialize":
            return self.respond(
                {"jsonrpc": "2.0", "id": msg_id, "result": {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": "1"}}}
            )
        if method == "tools/list":
            tools = [{"name": "get-workouts", "inputSchema": {"type": "object"}},
                     {"name": "feedback", "inputSchema": {"type": "object"}}]
            return self.respond({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})
        if method == "tools/call":
            name = message["params"]["name"]
            return self.respond(
                {"jsonrpc": "2.0", "id": msg_id,
                 "result": {"content": [{"type": "text", "text": f"called {name}"}], "isError": name == "boom"}}
            )
        return self.respond({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "nope"}})


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class Harness:
    def __init__(self, client: TestClient, backends: dict[str, FakeBackend], store: Store) -> None:
        self.client = client
        self.backends = backends
        self.store = store
        self._last_step = 0

    # TOTP codes: each successful login burns a time step, so hand out increasing
    # steps within the accepted ±1 window.
    def totp(self) -> str:
        while True:
            current = int(time.time() // 30)
            used = max(self._last_step, self.store.get_login_state().last_totp_step)
            step = max(used + 1, current - 1)
            if step <= current + 1:
                return pyotp.TOTP(TOTP_SECRET).generate_otp(step)
            time.sleep(1)

    def burn(self, code: str) -> None:
        """Remember a code that was handed out explicitly (replay tests)."""
        current = int(time.time() // 30)
        for step in (current - 1, current, current + 1):
            if pyotp.TOTP(TOTP_SECRET).generate_otp(step) == code:
                self._last_step = max(self._last_step, step)

    def start(self, *, resource: str = HEVY, client_id: str = CLAUDE, redirect_uri: str = CLAUDE_CB,
              headers: dict | None = None, challenge: str | None = None, **extra) -> httpx.Response:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge or pkce_pair()[1],
            "code_challenge_method": "S256",
            "state": "st-123",
            "resource": resource,
            "scope": "mcp",
        }
        params.update(extra)
        params = {k: v for k, v in params.items() if v is not None}
        return self.client.get("/authorize", params=params, headers=headers if headers is not None else TAILNET, follow_redirects=False)

    @staticmethod
    def txn(page: httpx.Response) -> str:
        marker = 'name="txn" value="'
        start = page.text.index(marker) + len(marker)
        return page.text[start:page.text.index('"', start)]

    def submit(self, txn: str, *, password: str = PASSWORD, code: str | None = None, decision: str = "approve",
               headers: dict | None = None) -> httpx.Response:
        return self.client.post(
            "/authorize",
            data={"txn": txn, "password": password, "totp": code or self.totp(), "decision": decision},
            headers=headers if headers is not None else TAILNET,
            follow_redirects=False,
        )

    def authorize(self, *, resource: str = HEVY, client_id: str = CLAUDE, redirect_uri: str = CLAUDE_CB) -> tuple[str, str]:
        """Run the browser part of the flow; returns (code, verifier)."""
        verifier, challenge = pkce_pair()
        page = self.start(resource=resource, client_id=client_id, redirect_uri=redirect_uri, challenge=challenge)
        assert page.status_code == 200, page.text
        done = self.submit(self.txn(page))
        assert done.status_code == 303, done.text
        query = parse_qs(urlsplit(done.headers["location"]).query)
        return query["code"][0], verifier

    def exchange(self, code: str, verifier: str, *, client_id: str = CLAUDE, redirect_uri: str = CLAUDE_CB,
                 headers: dict | None = None, **extra) -> httpx.Response:
        data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "client_id": client_id, "redirect_uri": redirect_uri}
        data.update(extra)
        return self.client.post("/token", data=data, headers=headers if headers is not None else FUNNEL_ANTHROPIC)

    def tokens(self, *, resource: str = HEVY) -> dict:
        code, verifier = self.authorize(resource=resource)
        response = self.exchange(code, verifier, resource=resource)
        assert response.status_code == 200, response.text
        return response.json()

    def refresh(self, refresh_token: str, *, client_id: str = CLAUDE, headers: dict | None = None) -> httpx.Response:
        return self.client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
            headers=headers if headers is not None else FUNNEL_ANTHROPIC,
        )

    def mcp(self, token: str | None, message: dict | None = None, *, path: str = "/hevy/mcp",
            headers: dict | None = None, method: str = "POST") -> httpx.Response:
        hdrs = dict(headers if headers is not None else FUNNEL_ANTHROPIC)
        hdrs["accept"] = "application/json, text/event-stream"
        if token:
            hdrs["authorization"] = f"Bearer {token}"
        if method == "POST":
            return self.client.post(path, json=message or {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                    headers=hdrs)
        return self.client.request(method, path, headers=hdrs)


@pytest.fixture
def make_harness(tmp_path, monkeypatch):
    for var in ("PUBLIC_BASE_URL", "OWNER_TAILSCALE_LOGIN", "FUNNEL_TOKEN_CIDRS", "ACCESS_TOKEN_TTL",
                "REFRESH_TOKEN_TTL"):
        monkeypatch.delenv(var, raising=False)
    clients: list[TestClient] = []

    def factory(*, mcp_rate: int = 6000, mcp_burst: int = 1000, sse: bool = False) -> Harness:
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(exist_ok=True)
        hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
        (secrets_dir / "password_hash").write_text(hasher.hash(PASSWORD) + "\n")
        (secrets_dir / "totp_secret").write_text(TOTP_SECRET + "\n")
        backends = {"hevy": FakeBackend("hevy", sse=sse), "garmin": FakeBackend("garmin", sse=sse)}
        for backend in backends.values():
            (secrets_dir / f"{backend.name}_backend_token").write_text(backend.secret)
        config = tmp_path / "config.toml"
        config.write_text(CONFIG.format(base=BASE, db=tmp_path / "gw.sqlite3", owner=OWNER, secrets=secrets_dir,
                                        mcp_rate=mcp_rate, mcp_burst=mcp_burst))
        settings = load_settings(str(config))

        async def dispatch(request: httpx.Request) -> httpx.Response:
            return await backends[request.url.host.split(".")[0]].handle(request)

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        store = Store(settings.db_path)
        app = create_app(settings, upstream_client=upstream, store=store, audit=Audit(None))
        client = TestClient(app, base_url=BASE)
        client.__enter__()
        clients.append(client)
        return Harness(client, backends, store)

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def h(make_harness) -> Harness:
    return make_harness()
