#!/usr/bin/env python3
"""Offline wiring check: the gateway is reachable only through `tailscale serve`.

In userspace mode tailscaled forwards a tailnet TCP connection to a port without a serve handler to
127.0.0.1:<port> inside the sidecar's network namespace, which the gateway shares. So nothing may listen on 127.0.0.1
(or ::1) there. This script reads the real files and fails when
- the gateway's listen address (gateway/config.toml) is not another IPv4 loopback address,
- the serve proxy target, the gateway healthcheck, TS_LOCAL_ADDR_PORT and the ts healthcheck disagree with it,
- the ts node advertises a route or an exit node (that would make more destinations "local" for its packet filter).

Usage:
  scripts/check-ingress.py              check the tree (needs `docker compose`; no .env, no secrets)
  scripts/check-ingress.py --self-test  run the embedded baseline and mutants; no Docker

Runs on the host's Python (>= 3.11 for tomllib). The listen-address rule is the gateway's own
(gateway.config.listen_address_problem), so the two can never disagree.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway" / "src"))

from gateway.config import DEFAULT_LISTEN_HOST, listen_address_problem  # noqa: E402

URL = re.compile(r"https?://[^\s'\"]+")
Trio = tuple[dict[str, Any], dict[str, Any], dict[str, Any]]


def _urls(service: dict[str, Any]) -> list[str]:
    test = service.get("healthcheck", {}).get("test", [])
    return URL.findall(" ".join(str(part) for part in test))


def check(compose: dict[str, Any], serve: dict[str, Any], gateway: dict[str, Any]) -> list[str]:
    """The problems found in the three parsed files; empty when the wiring is fine."""
    problems: list[str] = []

    # 1. the gateway's own listen address
    server = gateway.get("server", {})
    host = str(server.get("listen_host", DEFAULT_LISTEN_HOST))
    if (problem := listen_address_problem(host)) is not None:
        problems.append(f"gateway/config.toml listen_host: {problem}")
    backend = f"http://{host}:{server.get('listen_port')}"

    # 2. every serve handler proxies to it, and there is at least one
    proxies = [h.get("Proxy") for web in serve.get("Web", {}).values() for h in web.get("Handlers", {}).values()]
    if not proxies or any(proxy != backend for proxy in proxies):
        problems.append(f"tailscale/serve.json: every Proxy must be {backend}, found {proxies}")

    services = compose.get("services", {})
    gw, ts = services.get("gateway", {}), services.get("ts", {})

    # 3. the gateway shares the sidecar's namespace
    if gw.get("network_mode") != "service:ts":
        found = gw.get("network_mode")
        problems.append(f"docker-compose.yml: gateway network_mode must be service:ts, found {found!r}")

    # 4. the gateway healthcheck calls exactly that address
    if _urls(gw) != [f"{backend}/healthz"]:
        problems.append(f"docker-compose.yml: gateway healthcheck must call {backend}/healthz, found {_urls(gw)}")

    # 5. tailscaled's own health endpoint is bound to another IPv4 loopback address too
    env = ts.get("environment", {}) or {}
    local = str(env.get("TS_LOCAL_ADDR_PORT") or "")
    local_host, sep, local_port = local.rpartition(":")
    if not sep or not local_port.isdigit():
        problems.append(f"docker-compose.yml: TS_LOCAL_ADDR_PORT must be <IPv4 loopback>:<port>, found {local!r}")
    elif (problem := listen_address_problem(local_host)) is not None:
        problems.append(f"docker-compose.yml TS_LOCAL_ADDR_PORT: {problem}")

    # 6. the ts healthcheck calls exactly that address
    if _urls(ts) != [f"http://{local}/healthz"]:
        problems.append(f"docker-compose.yml: ts healthcheck must call http://{local}/healthz, found {_urls(ts)}")

    # 7. no advertised route or exit node: peers could then address more than the node itself
    extra = str(env.get("TS_EXTRA_ARGS") or "")
    if env.get("TS_ROUTES") or "--advertise-routes" in extra or "--advertise-exit-node" in extra:
        problems.append("docker-compose.yml: the ts node must not advertise routes or an exit node")

    return problems


def _baseline() -> Trio:
    compose = {"services": {
        "ts": {
            "environment": {"TS_EXTRA_ARGS": "--advertise-tags=${TS_TAG:-tag:fitness-mcp}",
                            "TS_LOCAL_ADDR_PORT": "127.0.0.2:9002"},
            "healthcheck": {"test": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.2:9002/healthz"]},
        },
        "gateway": {
            "network_mode": "service:ts",
            "healthcheck": {"test": [
                "CMD", "python", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.2:8080/healthz', timeout=3)"]},
        },
    }}
    serve = {"Web": {"${TS_CERT_DOMAIN}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.2:8080"}}}}}
    gateway = {"server": {"listen_host": "127.0.0.2", "listen_port": 8080}}
    return compose, serve, gateway


def _mutants() -> dict[str, Callable[[dict, dict, dict], object]]:
    def ts(c: dict) -> dict:
        return c["services"]["ts"]

    def gw(c: dict) -> dict:
        return c["services"]["gateway"]

    def handlers(s: dict) -> dict:
        return s["Web"]["${TS_CERT_DOMAIN}:443"]["Handlers"]

    def gw_check(url: str) -> list[str]:
        return ["CMD", "python", "-c", f"import urllib.request; urllib.request.urlopen('{url}', timeout=3)"]

    return {
        "listen_host 127.0.0.1": lambda c, s, g: g["server"].update(listen_host="127.0.0.1"),
        "listen_host ::1": lambda c, s, g: g["server"].update(listen_host="::1"),
        "listen_host localhost": lambda c, s, g: g["server"].update(listen_host="localhost"),
        "listen_host 0.0.0.0": lambda c, s, g: g["server"].update(listen_host="0.0.0.0"),
        "serve proxy 127.0.0.1": lambda c, s, g: handlers(s)["/"].update(Proxy="http://127.0.0.1:8080"),
        "serve proxy port 8081": lambda c, s, g: handlers(s)["/"].update(Proxy="http://127.0.0.2:8081"),
        "serve extra handler on 127.0.0.1": lambda c, s, g: handlers(s).update(
            {"/x": {"Proxy": "http://127.0.0.1:8080"}}),
        "gateway healthcheck 127.0.0.1": lambda c, s, g: gw(c)["healthcheck"].update(
            test=gw_check("http://127.0.0.1:8080/healthz")),
        "gateway network_mode removed": lambda c, s, g: gw(c).pop("network_mode"),
        "TS_LOCAL_ADDR_PORT 127.0.0.1": lambda c, s, g: ts(c)["environment"].update(
            TS_LOCAL_ADDR_PORT="127.0.0.1:9002"),
        "TS_LOCAL_ADDR_PORT unset": lambda c, s, g: ts(c)["environment"].pop("TS_LOCAL_ADDR_PORT"),
        "TS_LOCAL_ADDR_PORT [::]:9002": lambda c, s, g: ts(c)["environment"].update(TS_LOCAL_ADDR_PORT="[::]:9002"),
        "ts healthcheck 127.0.0.1": lambda c, s, g: ts(c)["healthcheck"].update(
            test=["CMD", "wget", "http://127.0.0.1:9002/healthz"]),
        "TS_EXTRA_ARGS advertises routes": lambda c, s, g: ts(c)["environment"].update(
            TS_EXTRA_ARGS="--advertise-tags=tag:x --advertise-routes=10.0.0.0/24"),
        "TS_ROUTES set": lambda c, s, g: ts(c)["environment"].update(TS_ROUTES="10.0.0.0/24"),
        "TS_EXTRA_ARGS advertises an exit node": lambda c, s, g: ts(c)["environment"].update(
            TS_EXTRA_ARGS="--advertise-exit-node"),
        # Consistent reverts: only the predicate rules (1, 5) and "at least one" (2) can catch these,
        # because every other rule compares one file with another.
        "gateway side consistently on 127.0.0.1": lambda c, s, g: (
            g["server"].update(listen_host="127.0.0.1"),
            handlers(s)["/"].update(Proxy="http://127.0.0.1:8080"),
            gw(c)["healthcheck"].update(test=gw_check("http://127.0.0.1:8080/healthz"))),
        "ts side consistently on 127.0.0.1": lambda c, s, g: (
            ts(c)["environment"].update(TS_LOCAL_ADDR_PORT="127.0.0.1:9002"),
            ts(c)["healthcheck"].update(test=["CMD", "wget", "http://127.0.0.1:9002/healthz"])),
        "TS_LOCAL_ADDR_PORT port not digits": lambda c, s, g: (
            ts(c)["environment"].update(TS_LOCAL_ADDR_PORT="127.0.0.2:abc"),
            ts(c)["healthcheck"].update(test=["CMD", "wget", "http://127.0.0.2:abc/healthz"])),
        "serve without handlers": lambda c, s, g: s["Web"]["${TS_CERT_DOMAIN}:443"].update(Handlers={}),
    }


def self_test() -> int:
    failed: list[str] = []
    if problems := check(*_baseline()):
        failed.append(f"baseline reports problems: {problems}")
    compose, serve, gateway = _baseline()
    del gateway["server"]["listen_host"]  # the default is the safe address
    if problems := check(compose, serve, gateway):
        failed.append(f"baseline without listen_host reports problems: {problems}")
    mutants = _mutants()
    for name, mutate in mutants.items():
        trio = copy.deepcopy(_baseline())
        mutate(*trio)
        if not check(*trio):
            failed.append(f"mutant survives: {name}")
    if failed:
        for line in failed:
            print(line)
        print(f"INGRESS SELF-TEST FAILED ({len(failed)})")
        return 1
    print(f"INGRESS SELF-TEST OK ({len(mutants)} mutants, 1 control)")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    if argv:
        print("usage: check-ingress.py [--self-test]", file=sys.stderr)
        return 2
    compose = json.loads(subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "--project-name", "fcm-ingress-check",
         "config", "--format", "json", "--no-interpolate"],
        capture_output=True, text=True, check=True).stdout)
    serve = json.loads((ROOT / "tailscale" / "serve.json").read_text(encoding="utf-8"))
    gateway = tomllib.loads((ROOT / "gateway" / "config.toml").read_text(encoding="utf-8"))
    problems = check(compose, serve, gateway)
    for problem in problems:
        print(f"problem: {problem}")
    print("INGRESS OK" if not problems else f"INGRESS FAILED ({len(problems)})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
