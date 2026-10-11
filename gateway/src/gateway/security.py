"""Cryptographic helpers: opaque tokens, PKCE, password + TOTP verification and
login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, replace
from typing import TypeGuard

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .store import LoginState, Store

_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9\-_]{43}$")
_TOTP_RE = re.compile(r"^\d{6}$")
TOTP_STEP = 30


def new_token() -> str:
    """256 bits of randomness, URL-safe."""
    return secrets.token_urlsafe(32)


def new_id() -> str:
    return secrets.token_hex(12)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_valid_code_challenge(challenge: str | None) -> TypeGuard[str]:
    # bool(challenge) is False for None, so match() never gets None
    return bool(challenge) and bool(_CHALLENGE_RE.match(challenge))  # type: ignore[arg-type]


def verify_pkce(verifier: str | None, challenge: str) -> bool:
    if not verifier or not _VERIFIER_RE.match(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


class PasswordVerifier:
    def __init__(self, encoded_hash: str) -> None:
        if not encoded_hash.startswith("$argon2id$"):
            raise ValueError("password hash must be an argon2id hash")
        self._hash = encoded_hash
        self._hasher = PasswordHasher()

    def verify(self, password: str) -> bool:
        if not password:
            return False
        try:
            return self._hasher.verify(self._hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


class TotpVerifier:
    """RFC 6238 (SHA-1, 6 digits, 30 s) accepting one step of clock drift."""

    def __init__(self, secret: str) -> None:
        cleaned = secret.replace(" ", "").upper()
        base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))  # raises on garbage
        if len(cleaned) < 26:
            raise ValueError("TOTP secret is too short (need at least 128 bits)")
        self._totp = pyotp.TOTP(cleaned)

    def matching_step(self, code: str, now: float | None = None) -> int | None:
        code = code.replace(" ", "")
        if not _TOTP_RE.match(code):
            return None
        now = time.time() if now is None else now
        current = int(now // TOTP_STEP)
        for step in (current - 1, current, current + 1):
            if hmac.compare_digest(self._totp.generate_otp(step), code):
                return step
        return None


@dataclass(frozen=True)
class ThrottlePolicy:
    max_failures: int
    failure_window: int
    lockout_base: int
    lockout_max: int


class LoginGuard:
    """Password + TOTP check with persistent brute-force lockout and TOTP replay
    protection (each time step can be used once)."""

    def __init__(self, store: Store, passwords: PasswordVerifier, totp: TotpVerifier, policy: ThrottlePolicy):
        self._store = store
        self._passwords = passwords
        self._totp = totp
        self._policy = policy

    def locked_for(self, now: int) -> int:
        state = self._store.get_login_state()
        if state.locked_until and state.locked_until > now:
            return state.locked_until - now
        return 0

    def attempt(self, password: str, code: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        now_i = int(now)
        if self.locked_for(now_i):
            return False
        # Evaluate both factors every time so timing does not reveal which failed.
        password_ok = self._passwords.verify(password)
        step = self._totp.matching_step(code, now)
        state = self._store.get_login_state()
        totp_ok = step is not None and step > state.last_totp_step
        if password_ok and totp_ok:
            # totp_ok is True only when step is not None
            self._store.save_login_state(LoginState(0, None, None, step))  # type: ignore[arg-type]
            return True
        self._record_failure(state, now_i)
        return False

    def _record_failure(self, state: LoginState, now: int) -> None:
        failures = state.failures
        if state.last_failure_at is None or now - state.last_failure_at > self._policy.failure_window:
            failures = 0
        failures += 1
        locked_until = state.locked_until
        if failures >= self._policy.max_failures:
            exponent = min(failures - self._policy.max_failures, 16)
            locked_until = now + min(self._policy.lockout_max, self._policy.lockout_base * 2**exponent)
        self._store.save_login_state(replace(state, failures=failures, last_failure_at=now, locked_until=locked_until))
