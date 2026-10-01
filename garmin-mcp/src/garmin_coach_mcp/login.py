"""One-time interactive Garmin Connect login that stores tokens for the MCP server.

Depends only on ``garminconnect`` and the standard library, so it also runs as a
plain script on another machine (fallback when Garmin/Cloudflare blocks logins
from the server's IP):

    pip install garminconnect==0.3.17
    python login.py --tokens-dir ./garmin-tokens

The password is never stored or logged. Only the resulting token file
``<tokens-dir>/garmin_tokens.json`` (mode 0600, directory 0700) is written.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

TOKEN_FILE_NAME = "garmin_tokens.json"
MAX_MFA_ATTEMPTS = 3
MAX_INPUT_ATTEMPTS = 3

EXIT_OK, EXIT_FAIL, EXIT_BAD_CREDENTIALS, EXIT_RATE_LIMITED = 0, 1, 2, 3


def _jwt_exp(token: str | None) -> datetime | None:
    """Read the (unverified) ``exp`` claim, only to show when a token expires."""
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(float(data["exp"])).astimezone()
    except (ValueError, KeyError, TypeError):
        return None


def _enable_utf8_erase() -> None:
    """Make Backspace erase whole UTF-8 characters on this terminal (tty flag IUTF8).

    Without it the kernel erases a single byte, so fixing a typo right after a
    character like "á" leaves half of it in the (hidden) password."""
    try:
        import termios
    except ImportError:
        return
    flag = getattr(termios, "IUTF8", 0)
    if not flag:
        return
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return
    try:
        attrs = termios.tcgetattr(fd)
        if not attrs[0] & flag:
            attrs[0] |= flag
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass
    finally:
        os.close(fd)


def _input_problem(value: str) -> str | None:
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        return "obsahuje rozbitý znak (diakritika nedorazila jako UTF-8, typicky po opravě Backspacem)"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "obsahuje řídicí znak (terminál neprovedl Backspace nebo šipku)"
    return None


def _prompt(label: str, *, secret: bool) -> str:
    """Read one value; reject corrupted input instead of sending it to Garmin."""
    _enable_utf8_erase()
    for _ in range(MAX_INPUT_ATTEMPTS):
        value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ").strip()
        problem = _input_problem(value)
        if problem is None:
            return value
        print(f"{label} {problem}. Zadej ho znovu bez oprav, případně zkontroluj rozložení klávesnice.",
              file=sys.stderr)
    raise ValueError(f"{label}: neplatný vstup")


def _normalize_tokens_dir(raw: str) -> Path:
    """Resolve symlinked parents (macOS: /tmp -> /private/tmp). garminconnect refuses
    any symlink in the token path; a symlinked token directory itself is still
    rejected by the library, as intended."""
    path = Path(raw).expanduser()
    return path.parent.resolve() / path.name


def _prepare_dir(tokens_dir: Path) -> None:
    tokens_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(tokens_dir, 0o700)


def _ensure_writable(tokens_dir: Path) -> str | None:
    """Create the token directory and prove it is writable before contacting Garmin,
    so a successful login is never lost on a bad path."""
    if tokens_dir.is_symlink():
        return f"{tokens_dir} je symlink, zvol jiný adresář"
    try:
        _prepare_dir(tokens_dir)
        probe = tokens_dir / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"do {tokens_dir} nejde zapisovat ({exc.strerror or exc})"
    return None


def _describe_tokens(tokens_dir: Path) -> str:
    try:
        data = json.loads((tokens_dir / TOKEN_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    parts = []
    access = _jwt_exp(data.get("di_token"))
    refresh = _jwt_exp(data.get("di_refresh_token"))
    if access:
        parts.append(f"access token do {access:%Y-%m-%d %H:%M} (obnovuje se automaticky)")
    if refresh:
        parts.append(f"refresh token do {refresh:%Y-%m-%d %H:%M}")
    return "; ".join(parts)


def _verify(tokens_dir: Path) -> str:
    """Load the stored tokens exactly like the server does and make real calls."""
    client = Garmin()
    client.login(tokenstore=str(tokens_dir))
    summary = client.get_user_summary(date.today().isoformat())
    steps = summary.get("totalSteps") if isinstance(summary, dict) else None
    who = client.get_full_name() or "?"
    return f"přihlášen jako {who}; dnešní kroky: {steps if steps is not None else 'n/a'}"


def interactive_login(tokens_dir: Path, email: str | None) -> int:
    print("Garmin Connect login pro fitness-coach-mcp.")
    print(f"Heslo se nikam neukládá; uloží se jen tokeny do {tokens_dir / TOKEN_FILE_NAME} (0600).")
    if problem := _ensure_writable(tokens_dir):
        print(f"Tokeny nepůjde uložit: {problem}. Použij např. --tokens-dir ~/garmin-tokens.", file=sys.stderr)
        return EXIT_FAIL
    try:
        email = email or _prompt("Garmin e-mail", secret=False)
        password = _prompt("Garmin heslo", secret=True)
    except ValueError as exc:
        print(f"{exc}. Do Garminu jsem nic neposlal.", file=sys.stderr)
        return EXIT_FAIL
    if not email or not password:
        print("E-mail i heslo jsou povinné.", file=sys.stderr)
        return EXIT_FAIL

    client = Garmin(email=email, password=password, return_on_mfa=True)
    del password
    try:
        status, state = client.login()
        if status == "needs_mfa":
            for attempt in range(1, MAX_MFA_ATTEMPTS + 1):
                code = _prompt("MFA kód (z e-mailu / SMS / aplikace)", secret=False)
                try:
                    client.resume_login(state, code)
                    break
                except GarminConnectAuthenticationError:
                    if attempt == MAX_MFA_ATTEMPTS:
                        raise
                    print("Kód nesedí, zkus to znovu.", file=sys.stderr)
        client.password = None
        _prepare_dir(tokens_dir)
        client.client.dump(str(tokens_dir))
        print("Tokeny uložené, ověřuji...")
        print("OK:", _verify(tokens_dir))
        details = _describe_tokens(tokens_dir)
        if details:
            print("Platnost:", details)
        return EXIT_OK
    except GarminConnectAuthenticationError as exc:
        print(f"Přihlášení selhalo (špatný e-mail/heslo/MFA kód nebo zamčený účet): {exc}", file=sys.stderr)
        return EXIT_BAD_CREDENTIALS
    except GarminConnectTooManyRequestsError:
        print(
            "Garmin/Cloudflare přihlášení blokuje (HTTP 429). Zkus to za pár minut, případně se přihlas "
            "z domácí sítě a token importuj (garmin-mcp/README.md, sekce 'Záložní login').",
            file=sys.stderr,
        )
        return EXIT_RATE_LIMITED
    except GarminConnectConnectionError as exc:
        print(f"Chyba spojení s Garminem: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except ValueError as exc:
        print(f"{exc}. Přihlášení zrušeno.", file=sys.stderr)
        return EXIT_FAIL


def check(tokens_dir: Path) -> int:
    token_file = tokens_dir / TOKEN_FILE_NAME
    if not token_file.is_file() or token_file.stat().st_size == 0:
        print(f"CHYBÍ: {token_file} neexistuje. Spusť scripts/garmin-login.sh.")
        return EXIT_FAIL
    try:
        info = _verify(tokens_dir)
    except GarminConnectAuthenticationError:
        print("NEPLATNÉ: Garmin uložené tokeny odmítl (vypršely/odvolané). Spusť scripts/garmin-login.sh.")
        return EXIT_FAIL
    except GarminConnectTooManyRequestsError:
        print("NEOVĚŘENO: Garmin vrací HTTP 429 (rate limit), zkus to později.")
        return EXIT_FAIL
    except GarminConnectConnectionError as exc:
        print(f"NEOVĚŘENO: chyba spojení s Garminem: {exc}")
        return EXIT_FAIL
    print("OK:", info)
    details = _describe_tokens(tokens_dir)
    if details:
        print("Platnost:", details)
    return EXIT_OK


def import_tokens(tokens_dir: Path, source: str, verify: bool) -> int:
    """Install a token file produced elsewhere (e.g. by this script on a laptop)."""
    from garminconnect.client import Client

    raw = sys.stdin.read() if source == "-" else Path(source).expanduser().read_text(encoding="utf-8")
    holder = Client()
    try:
        holder.loads(raw)
    except GarminConnectConnectionError:
        print("Soubor nevypadá jako garmin_tokens.json z python-garminconnect 0.3.x.", file=sys.stderr)
        return EXIT_FAIL
    _prepare_dir(tokens_dir)
    holder.dump(str(tokens_dir))
    print(f"Tokeny importované do {tokens_dir / TOKEN_FILE_NAME}.")
    return check(tokens_dir) if verify else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Garmin Connect login for garmin-coach-mcp")
    parser.add_argument(
        "--tokens-dir",
        default=os.environ.get("GARMINTOKENS", "~/.garminconnect"),
        help="directory for garmin_tokens.json (default: $GARMINTOKENS or ~/.garminconnect)",
    )
    parser.add_argument("--email", help="Garmin account e-mail (otherwise prompted)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate the stored tokens (exit 0 = OK)")
    mode.add_argument("--import-file", metavar="PATH", help="install a token file ('-' = stdin)")
    parser.add_argument("--no-verify", action="store_true", help="with --import-file: skip the online check")
    args = parser.parse_args(argv)

    tokens_dir = _normalize_tokens_dir(args.tokens_dir)
    try:
        if args.check:
            return check(tokens_dir)
        if args.import_file:
            return import_tokens(tokens_dir, args.import_file, verify=not args.no_verify)
        return interactive_login(tokens_dir, args.email)
    except KeyboardInterrupt:
        print("\nPřerušeno.", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
