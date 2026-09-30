import time

import pyotp
import pytest

from gateway.config import Client, ConfigError, load_settings, normalize_resource
from gateway.oauth import redirect_uri_allowed
from gateway.proxy import ResponseObserver, filter_tools
from gateway.ratelimit import RateLimiter
from gateway.security import TotpVerifier, is_valid_code_challenge, verify_pkce


def test_pkce_rfc7636_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert is_valid_code_challenge(challenge)
    assert verify_pkce(verifier, challenge)
    assert not verify_pkce(verifier[:-1] + "x", challenge)
    assert not verify_pkce("short", challenge)
    assert not verify_pkce(None, challenge)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://Host.Example/hevy/mcp/", "https://host.example/hevy/mcp"),
        ("https://host.example:443/hevy/mcp", "https://host.example/hevy/mcp"),
        ("https://host.example:8443/x", "https://host.example:8443/x"),
        ("http://host.example/x", None),
        ("https://host.example/x?y=1", None),
        ("https://host.example/x#f", None),
        ("https://user@host.example/x", None),
        ("not a url", None),
    ],
)
def test_normalize_resource(value, expected):
    assert normalize_resource(value) == expected


def test_redirect_uri_matching():
    client = Client("c", "c", ("https://claude.ai/api/mcp/auth_callback", "http://localhost/callback"))
    assert redirect_uri_allowed(client, "https://claude.ai/api/mcp/auth_callback")
    assert not redirect_uri_allowed(client, "https://claude.ai/api/mcp/auth_callback/")
    assert not redirect_uri_allowed(client, "https://claude.ai/api/mcp/auth_callback?x=1")
    assert redirect_uri_allowed(client, "http://localhost:1234/callback")
    assert redirect_uri_allowed(client, "http://localhost/callback")
    assert not redirect_uri_allowed(client, "http://127.0.0.1:1234/callback")
    assert not redirect_uri_allowed(client, "http://localhost:99999/callback")
    assert not redirect_uri_allowed(client, "http://localhost:1234/callback#frag")
    assert not redirect_uri_allowed(client, "http://evil.example/callback")


def test_rate_limiter():
    limiter = RateLimiter(per_minute=60, burst=2)
    assert limiter.hit("k", now=0) is None
    assert limiter.hit("k", now=0) is None
    assert limiter.hit("k", now=0) == 1
    assert limiter.hit("k", now=1.01) is None
    assert limiter.hit("other", now=0) is None


def test_totp_window():
    secret = pyotp.random_base32(32)
    verifier = TotpVerifier(secret)
    now = time.time()
    totp = pyotp.TOTP(secret)
    step = int(now // 30)
    assert verifier.matching_step(totp.generate_otp(step), now) == step
    assert verifier.matching_step(totp.generate_otp(step - 1), now) == step - 1
    assert verifier.matching_step(totp.generate_otp(step + 2), now) is None
    assert verifier.matching_step("12345", now) is None
    with pytest.raises(ValueError):
        TotpVerifier("TOOSHORT")


def test_filter_tools_json_and_observer():
    raw = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"a"},{"name":"feedback"}]}}'
    out = filter_tools(raw, "application/json", frozenset({"feedback"}))
    assert b"feedback" not in out and b'"a"' in out
    observer = ResponseObserver("application/json")
    observer.feed(b'{"jsonrpc":"2.0","id":2,"result":{"isError":true,"content":[]}}')
    assert observer.summary() == "tool_error"
    sse = ResponseObserver("text/event-stream")
    sse.feed(b'event: message\ndata: {"jsonrpc":"2.0","id":3,"error":{"code":1,"message":"x"}}\n')
    sse.feed(b"\n")
    assert sse.summary() == "rpc_error"


def _write_config(tmp_path, body):
    path = tmp_path / "c.toml"
    path.write_text(body)
    return str(path)


BASE_CONFIG = """
[server]
public_base_url = "{url}"
listen_port = 8080
[auth]
owner_tailscale_login = "o@github"
access_token_ttl = 1
refresh_token_ttl = 1
auth_code_ttl = 1
auth_request_ttl = 1
refresh_reuse_grace = 1
login_max_failures = 1
login_failure_window = 1
login_lockout_base = 1
login_lockout_max = 1
[rate_limits]
public_per_ip = [1, 1]
token_endpoint = [1, 1]
authorize = [1, 1]
mcp_per_grant = [1, 1]
[[clients]]
client_id = "c"
redirect_uris = ["{redirect}"]
[[backends]]
name = "a"
path = "/a/mcp"
upstream = "http://a/mcp"
token_file = "/x"
"""


def test_config_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("OWNER_TAILSCALE_LOGIN", raising=False)
    ok = load_settings(_write_config(tmp_path, BASE_CONFIG.format(url="https://h.example", redirect="https://x/cb")))
    assert ok.backends[0].resource == "https://h.example/a/mcp"
    with pytest.raises(ConfigError):
        load_settings(_write_config(tmp_path, BASE_CONFIG.format(url="http://h.example", redirect="https://x/cb")))
    with pytest.raises(ConfigError):
        load_settings(_write_config(tmp_path, BASE_CONFIG.format(url="https://h.example/p", redirect="https://x/cb")))
    with pytest.raises(ConfigError):
        load_settings(_write_config(tmp_path, BASE_CONFIG.format(url="https://h.example", redirect="http://x/cb")))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://other.example")
    env = load_settings(_write_config(tmp_path, BASE_CONFIG.format(url="https://h.example", redirect="https://x/cb")))
    assert env.issuer == "https://other.example"
