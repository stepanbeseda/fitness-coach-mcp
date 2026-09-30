"""Admin CLI: `python -m gateway.cli <command>` (run inside the gateway container).

Secrets are printed only where a script needs to capture them (stdout); prompts,
QR codes and messages go to stderr.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from datetime import UTC, datetime

import pyotp
import segno
from argon2 import PasswordHasher

from .config import SCOPE, ConfigError, load_settings, read_secret
from .security import TotpVerifier, hash_token, new_id, new_token
from .store import Grant, Store, Token

CLI_CLIENT_ID = "gateway-cli"
MIN_PASSWORD_LENGTH = 12


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _fmt(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def cmd_hash_password(args: argparse.Namespace) -> int:
    if args.stdin or not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Nové heslo: ")
        if getpass.getpass("Heslo znovu: ") != password:
            _err("Hesla se neshodují.")
            return 1
    if len(password) < MIN_PASSWORD_LENGTH:
        _err(f"Heslo musí mít aspoň {MIN_PASSWORD_LENGTH} znaků (doporučeno 16+ nebo passphrase).")
        return 1
    print(PasswordHasher().hash(password))
    return 0


def cmd_new_totp(args: argparse.Namespace) -> int:
    secret = pyotp.random_base32(length=32)
    uri = pyotp.TOTP(secret).provisioning_uri(name=args.account, issuer_name=args.issuer)
    _err("Naskenuj QR kód v autentizátoru (Google Authenticator, 1Password, Aegis, ...):\n")
    segno.make(uri, error="m").terminal(out=sys.stderr, compact=True)
    _err(f"\nNebo zadej ručně: {uri}\n")
    print(secret)
    return 0


def cmd_verify_totp(args: argparse.Namespace) -> int:
    lines = sys.stdin.read().splitlines()
    if len(lines) < 2:
        _err("Na stdin čekám dva řádky: secret a kód.")
        return 2
    try:
        verifier = TotpVerifier(lines[0].strip())
    except ValueError as exc:
        _err(f"Neplatný TOTP secret: {exc}")
        return 2
    return 0 if verifier.matching_step(lines[1].strip()) is not None else 1


def _store() -> tuple[Store, object]:
    settings = load_settings()
    return Store(settings.db_path), settings


def cmd_grants(args: argparse.Namespace) -> int:
    store, settings = _store()
    if args.action == "list":
        rows = store.list_grants(include_revoked=args.all)
        if not rows:
            _err("Žádné granty.")
            return 0
        print(f"{'ID':10} {'KLIENT':40} {'RESOURCE':8} {'VYTVOŘENO':16} {'POUŽITO':16} {'REFRESH DO':16} STAV")
        for grant, refresh_expires in rows:
            client = settings.clients.get(grant.client_id)
            backend = settings.backend_for_resource(grant.resource)
            state = f"revoked ({grant.revoked_reason})" if grant.revoked_at else "active"
            print(
                f"{grant.id[:8]:10} {(client.name if client else grant.client_id)[:40]:40} "
                f"{(backend.name if backend else '?'):8} {_fmt(grant.created_at):16} {_fmt(grant.last_used_at):16} "
                f"{_fmt(refresh_expires):16} {state}"
            )
        return 0

    # revoke
    now = int(time.time())
    targets = [g for g, _ in store.list_grants()]
    if not args.all:
        targets = [g for g in targets if g.id.startswith(args.grant_id or "\0")]
        if len(targets) != 1:
            _err("Zadej jednoznačný prefix ID grantu (viz `grants list`) nebo --all.")
            return 1
    for grant in targets:
        store.revoke_grant(grant.id, "revoked by admin", now)
        _err(f"Zrušen grant {grant.id[:8]}")
    return 0


def cmd_mint_token(args: argparse.Namespace) -> int:
    store, settings = _store()
    backend = next((b for b in settings.backends if b.name == args.backend), None)
    if backend is None:
        _err(f"Neznámý backend {args.backend}; dostupné: {', '.join(b.name for b in settings.backends)}")
        return 1
    ttl = max(60, min(args.ttl, 3600))
    now = int(time.time())
    grant = Grant(new_id(), CLI_CLIENT_ID, backend.resource, SCOPE, None, now, now, None, None)
    token = new_token()
    store.create_grant_with_tokens(
        grant, Token(new_id(), hash_token(token), "access", grant.id, now, now + ttl, None, None, None), None
    )
    _err(f"Access token pro {backend.resource} platný {ttl} s (grant {grant.id[:8]}).")
    print(token)
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    settings = load_settings()
    read_secret(settings.password_hash_file)
    TotpVerifier(read_secret(settings.totp_secret_file))
    for backend in settings.backends:
        read_secret(backend.token_file)
    print(f"issuer: {settings.issuer}")
    print(f"owner: {settings.owner_tailscale_login} (login tailnet-only: {settings.login_tailnet_only})")
    print(f"funnel token networks: {', '.join(str(n) for n in settings.funnel_token_cidrs) or '-'}")
    for client in settings.clients.values():
        print(f"client: {client.name} <{client.client_id}>")
    for backend in settings.backends:
        print(f"backend: {backend.resource} -> {backend.upstream} (denylist: {sorted(backend.tool_denylist)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("hash-password", help="argon2id hash of a new password (stdout)")
    p.add_argument("--stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=cmd_hash_password)

    p = sub.add_parser("new-totp", help="generate a TOTP secret (stdout) and show its QR code (stderr)")
    p.add_argument("--account", default="owner")
    p.add_argument("--issuer", default="fitness-coach-mcp")
    p.set_defaults(func=cmd_new_totp)

    p = sub.add_parser("verify-totp", help="stdin: secret and code on two lines; exit 0 if valid")
    p.set_defaults(func=cmd_verify_totp)

    p = sub.add_parser("grants", help="list or revoke OAuth grants")
    p.add_argument("action", choices=["list", "revoke"])
    p.add_argument("grant_id", nargs="?")
    p.add_argument("--all", action="store_true", help="list: include revoked; revoke: revoke every grant")
    p.set_defaults(func=cmd_grants)

    p = sub.add_parser("mint-token", help="short-lived access token for smoke tests (stdout)")
    p.add_argument("--backend", required=True)
    p.add_argument("--ttl", type=int, default=600)
    p.set_defaults(func=cmd_mint_token)

    p = sub.add_parser("check-config", help="validate configuration and secrets")
    p.set_defaults(func=cmd_check_config)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        _err(f"Chyba konfigurace: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
