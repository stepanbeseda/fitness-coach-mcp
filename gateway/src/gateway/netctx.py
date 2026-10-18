"""Where did a request come from?

The gateway listens on 127.0.0.2 inside the Tailscale sidecar's network namespace. tailscaled runs in userspace
mode, where netstack forwards a tailnet connection to any port without a serve handler to 127.0.0.1:<port>, so
nothing may listen on 127.0.0.1 (config.listen_address_problem refuses it, scripts/check-ingress.py checks the
wiring) and every request arrives through `tailscale serve` on :443, which
- adds `Tailscale-Funnel-Request: ?1` to requests that came through Funnel,
- adds `Tailscale-User-Login` for requests from users' own (untagged) tailnet devices,
- strips client-supplied copies of both, and sets X-Forwarded-For to the real client address (one entry; the
  public IP for Funnel traffic). The right-most entry is used, so a client-supplied entry can never be the one read.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.requests import Request


@dataclass(frozen=True)
class NetContext:
    via_funnel: bool
    tailscale_login: str | None
    client_ip: str | None

    @property
    def source(self) -> str:
        if self.via_funnel:
            return "funnel"
        return "tailnet" if self.tailscale_login else "unknown"

    def is_owner(self, owner_login: str) -> bool:
        """A request from one of the owner's devices over the tailnet (not Funnel)."""
        return (
            not self.via_funnel
            and self.tailscale_login is not None
            and self.tailscale_login.casefold() == owner_login.casefold()
        )


def net_context(request: Request) -> NetContext:
    headers = request.headers
    via_funnel = "tailscale-funnel-request" in headers
    login = None if via_funnel else (headers.get("tailscale-user-login") or "").strip() or None
    forwarded = headers.get("x-forwarded-for", "")
    client_ip = forwarded.rsplit(",", 1)[-1].strip() or None
    return NetContext(via_funnel=via_funnel, tailscale_login=login, client_ip=client_ip)
