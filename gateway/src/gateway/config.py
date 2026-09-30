"""Configuration: config.toml, environment overrides and Docker secret files.

Everything is validated at startup; an invalid or incomplete configuration stops
the gateway instead of running with weaker settings.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = "/app/config.toml"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SCOPE = "mcp"

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Client:
    client_id: str
    name: str
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True)
class Backend:
    name: str
    title: str
    path: str
    upstream: str
    token_file: str
    tool_denylist: frozenset[str]
    resource: str

    @property
    def metadata_path(self) -> str:
        return "/.well-known/oauth-protected-resource" + self.path


@dataclass(frozen=True)
class RateLimit:
    per_minute: float
    burst: int


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    listen_host: str
    listen_port: int
    db_path: str
    audit_log_path: str | None
    owner_tailscale_login: str
    login_tailnet_only: bool
    password_hash_file: str
    totp_secret_file: str
    access_token_ttl: int
    refresh_token_ttl: int
    auth_code_ttl: int
    auth_request_ttl: int
    refresh_reuse_grace: int
    login_max_failures: int
    login_failure_window: int
    login_lockout_base: int
    login_lockout_max: int
    funnel_token_cidrs: tuple[IPNetwork, ...]
    rate_public_per_ip: RateLimit
    rate_token_endpoint: RateLimit
    rate_authorize: RateLimit
    rate_mcp_per_grant: RateLimit
    clients: dict[str, Client]
    backends: tuple[Backend, ...]
    secure_cookies: bool = True

    @property
    def issuer(self) -> str:
        return self.public_base_url

    def backend_for_resource(self, resource: str) -> Backend | None:
        for backend in self.backends:
            if backend.resource == resource:
                return backend
        return None

    def ip_allowed_for_tokens(self, ip: str | None) -> bool:
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.funnel_token_cidrs)


def is_loopback_redirect(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def normalize_resource(value: str, *, allow_http: bool = False) -> str | None:
    """Canonical form of a resource URL (RFC 8707): lowercase scheme and host,
    no default port, no trailing slash, no query or fragment."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in (("https", "http") if allow_http else ("https",)):
        return None
    if not parts.hostname or parts.query or parts.fragment or parts.username or parts.password:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _int(section: dict, key: str, env: str | None = None) -> int:
    raw = _env(env) if env else None
    value = raw if raw is not None else section.get(key)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected an integer, got {value!r}") from None
    if number <= 0:
        raise ConfigError(f"{key}: must be positive")
    return number


def _rate(section: dict, key: str) -> RateLimit:
    value = section.get(key)
    if not (isinstance(value, list) and len(value) == 2):
        raise ConfigError(f"rate_limits.{key}: expected [per_minute, burst]")
    per_minute, burst = value
    if per_minute <= 0 or burst <= 0:
        raise ConfigError(f"rate_limits.{key}: values must be positive")
    return RateLimit(float(per_minute), int(burst))


def _validate_base_url(url: str, allow_http: bool) -> str:
    parts = urlsplit(url)
    if parts.scheme not in (("https", "http") if allow_http else ("https",)):
        raise ConfigError("public_base_url must use https")
    if not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError("public_base_url must be scheme://host[:port] without a path")
    normalized = normalize_resource(url, allow_http=allow_http)
    assert normalized is not None
    return normalized


def _validate_redirect_uri(uri: str) -> str:
    parts = urlsplit(uri)
    if parts.fragment or parts.username or parts.password:
        raise ConfigError(f"redirect URI {uri!r} must not contain a fragment or credentials")
    if parts.scheme == "https" and parts.hostname:
        return uri
    if is_loopback_redirect(uri):
        return uri
    raise ConfigError(f"redirect URI {uri!r} must be https or an http loopback address")


def load_settings(path: str | None = None, *, allow_http: bool = False) -> Settings:
    config_path = Path(path or os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG_PATH))
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc

    server = raw.get("server", {})
    auth = raw.get("auth", {})
    rates = raw.get("rate_limits", {})

    base_url = _validate_base_url(_env("PUBLIC_BASE_URL") or server.get("public_base_url", ""), allow_http)
    owner = (_env("OWNER_TAILSCALE_LOGIN") or auth.get("owner_tailscale_login", "")).strip()
    if not owner:
        raise ConfigError("owner_tailscale_login is required")

    cidr_source = _env("FUNNEL_TOKEN_CIDRS")
    cidr_values = cidr_source.split(",") if cidr_source else auth.get("funnel_token_cidrs", [])
    try:
        cidrs = tuple(ipaddress.ip_network(c.strip(), strict=True) for c in cidr_values if c.strip())
    except ValueError as exc:
        raise ConfigError(f"funnel_token_cidrs: {exc}") from exc

    clients: dict[str, Client] = {}
    for entry in raw.get("clients", []):
        client_id = str(entry.get("client_id", "")).strip()
        uris = tuple(_validate_redirect_uri(str(u)) for u in entry.get("redirect_uris", []))
        if not client_id or not uris:
            raise ConfigError("every client needs client_id and redirect_uris")
        if client_id in clients:
            raise ConfigError(f"duplicate client {client_id}")
        clients[client_id] = Client(client_id, str(entry.get("name") or client_id), uris)
    if not clients:
        raise ConfigError("no clients configured")

    backends: list[Backend] = []
    for entry in raw.get("backends", []):
        name = str(entry.get("name", "")).strip()
        route = str(entry.get("path", "")).strip()
        upstream = str(entry.get("upstream", "")).strip()
        token_file = str(entry.get("token_file", "")).strip()
        if not (name and route.startswith("/") and not route.endswith("/") and upstream and token_file):
            raise ConfigError(f"backend {name or '?'}: name, path (/x/y), upstream and token_file are required")
        resource = normalize_resource(base_url + route, allow_http=allow_http)
        if resource is None or any(b.path == route or b.name == name for b in backends):
            raise ConfigError(f"backend {name}: invalid or duplicate path")
        backends.append(
            Backend(
                name=name,
                title=str(entry.get("title") or name),
                path=route,
                upstream=upstream,
                token_file=token_file,
                tool_denylist=frozenset(str(t) for t in entry.get("tool_denylist", [])),
                resource=resource,
            )
        )
    if not backends:
        raise ConfigError("no backends configured")

    return Settings(
        public_base_url=base_url,
        listen_host=str(server.get("listen_host", "127.0.0.1")),
        listen_port=_int(server, "listen_port"),
        db_path=str(server.get("db_path", "/data/gateway.sqlite3")),
        audit_log_path=str(server["audit_log_path"]) if server.get("audit_log_path") else None,
        owner_tailscale_login=owner,
        login_tailnet_only=bool(auth.get("login_tailnet_only", True)),
        password_hash_file=str(auth.get("password_hash_file", "/run/secrets/gateway_password_hash")),
        totp_secret_file=str(auth.get("totp_secret_file", "/run/secrets/gateway_totp_secret")),
        access_token_ttl=_int(auth, "access_token_ttl", "ACCESS_TOKEN_TTL"),
        refresh_token_ttl=_int(auth, "refresh_token_ttl", "REFRESH_TOKEN_TTL"),
        auth_code_ttl=_int(auth, "auth_code_ttl"),
        auth_request_ttl=_int(auth, "auth_request_ttl"),
        refresh_reuse_grace=_int(auth, "refresh_reuse_grace"),
        login_max_failures=_int(auth, "login_max_failures"),
        login_failure_window=_int(auth, "login_failure_window"),
        login_lockout_base=_int(auth, "login_lockout_base"),
        login_lockout_max=_int(auth, "login_lockout_max"),
        funnel_token_cidrs=cidrs,
        rate_public_per_ip=_rate(rates, "public_per_ip"),
        rate_token_endpoint=_rate(rates, "token_endpoint"),
        rate_authorize=_rate(rates, "authorize"),
        rate_mcp_per_grant=_rate(rates, "mcp_per_grant"),
        clients=clients,
        backends=tuple(backends),
    )


def read_secret(path: str) -> str:
    """Read a Docker secret file; missing or empty secrets are fatal."""
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read secret {path}: {exc.strerror}") from exc
    if not value:
        raise ConfigError(f"secret {path} is empty")
    return value
