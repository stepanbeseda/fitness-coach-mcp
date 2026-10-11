"""SQLite persistence for pending authorizations, codes, grants and tokens.

Only SHA-256 hashes of codes, tokens and transaction ids are stored. One
connection guarded by a lock is plenty for a single-user gateway.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_requests (
    txn_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource TEXT NOT NULL,
    scope TEXT NOT NULL,
    state TEXT,
    tailscale_login TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource TEXT NOT NULL,
    scope TEXT NOT NULL,
    tailscale_login TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    grant_id TEXT
);
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    scope TEXT NOT NULL,
    tailscale_login TEXT,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER,
    revoked_reason TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    grant_id TEXT NOT NULL REFERENCES grants(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    rotated_at INTEGER,
    replaced_by TEXT
);
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens(grant_id);
CREATE TABLE IF NOT EXISTS login_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    failures INTEGER NOT NULL DEFAULT 0,
    last_failure_at INTEGER,
    locked_until INTEGER,
    last_totp_step INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO login_state (id) VALUES (1);
"""


@dataclass(frozen=True)
class AuthRequest:
    txn_hash: str
    csrf_hash: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    scope: str
    state: str | None
    tailscale_login: str | None
    attempts: int
    created_at: int
    expires_at: int


@dataclass(frozen=True)
class AuthCode:
    code_hash: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    scope: str
    tailscale_login: str | None
    created_at: int
    expires_at: int
    used_at: int | None
    grant_id: str | None


@dataclass(frozen=True)
class Grant:
    id: str
    client_id: str
    resource: str
    scope: str
    tailscale_login: str | None
    created_at: int
    last_used_at: int | None
    revoked_at: int | None
    revoked_reason: str | None


@dataclass(frozen=True)
class Token:
    id: str
    token_hash: str
    kind: str
    grant_id: str
    created_at: int
    expires_at: int
    revoked_at: int | None
    rotated_at: int | None
    replaced_by: str | None


@dataclass(frozen=True)
class LoginState:
    failures: int
    last_failure_at: int | None
    locked_until: int | None
    last_totp_step: int


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    # -- pending authorization requests ------------------------------------------

    def create_auth_request(self, req: AuthRequest) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO auth_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    req.txn_hash, req.csrf_hash, req.client_id, req.redirect_uri, req.code_challenge,
                    req.resource, req.scope, req.state, req.tailscale_login, req.attempts,
                    req.created_at, req.expires_at,
                ),
            )

    def get_auth_request(self, txn_hash: str, now: int) -> AuthRequest | None:
        row = self._one("SELECT * FROM auth_requests WHERE txn_hash = ? AND expires_at > ?", (txn_hash, now))
        return AuthRequest(**dict(row)) if row else None

    def bump_auth_request_attempts(self, txn_hash: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE auth_requests SET attempts = attempts + 1 WHERE txn_hash = ?", (txn_hash,))

    def delete_auth_request(self, txn_hash: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM auth_requests WHERE txn_hash = ?", (txn_hash,))

    # -- authorization codes -----------------------------------------------------

    def create_code(self, code: AuthCode) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO auth_codes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    code.code_hash, code.client_id, code.redirect_uri, code.code_challenge, code.resource,
                    code.scope, code.tailscale_login, code.created_at, code.expires_at, None, None,
                ),
            )

    def take_code(self, code_hash: str, now: int) -> tuple[str, AuthCode | None]:
        """Atomically consume a code. Returns (status, code) where status is
        'ok', 'unknown', 'expired' or 'reused'."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM auth_codes WHERE code_hash = ?", (code_hash,)).fetchone()
            if row is None:
                return "unknown", None
            code = AuthCode(**dict(row))
            if code.used_at is not None:
                return "reused", code
            if code.expires_at <= now:
                return "expired", code
            db.execute("UPDATE auth_codes SET used_at = ? WHERE code_hash = ?", (now, code_hash))
            return "ok", code

    def attach_grant_to_code(self, code_hash: str, grant_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE auth_codes SET grant_id = ? WHERE code_hash = ?", (grant_id, code_hash))

    # -- grants and tokens ---------------------------------------------------------

    def create_grant(self, grant: Grant) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    grant.id, grant.client_id, grant.resource, grant.scope, grant.tailscale_login,
                    grant.created_at, grant.last_used_at, None, None,
                ),
            )

    def create_grant_with_tokens(
        self, grant: Grant, access: Token, refresh: Token | None, code_hash: str | None = None
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    grant.id, grant.client_id, grant.resource, grant.scope, grant.tailscale_login,
                    grant.created_at, grant.last_used_at, None, None,
                ),
            )
            self._insert_token(db, access)
            if refresh is not None:
                self._insert_token(db, refresh)
            if code_hash is not None:
                db.execute("UPDATE auth_codes SET grant_id = ? WHERE code_hash = ?", (grant.id, code_hash))

    def get_grant(self, grant_id: str) -> Grant | None:
        row = self._one("SELECT * FROM grants WHERE id = ?", (grant_id,))
        return Grant(**dict(row)) if row else None

    def touch_grant(self, grant_id: str, now: int) -> None:
        with self.transaction() as db:
            db.execute("UPDATE grants SET last_used_at = ? WHERE id = ?", (now, grant_id))

    def revoke_grant(self, grant_id: str, reason: str, now: int) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE grants SET revoked_at = ?, revoked_reason = ? WHERE id = ? AND revoked_at IS NULL",
                (now, reason, grant_id),
            )
            db.execute("UPDATE tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL", (now, grant_id))

    def list_grants(self, include_revoked: bool = False) -> list[tuple[Grant, int | None]]:
        """Grants with the expiry of their newest live refresh token."""
        sql = (
            "SELECT g.*, (SELECT MAX(t.expires_at) FROM tokens t WHERE t.grant_id = g.id AND t.kind = 'refresh'"
            " AND t.revoked_at IS NULL AND t.rotated_at IS NULL) AS refresh_expires_at FROM grants g"
        )
        if not include_revoked:
            sql += " WHERE g.revoked_at IS NULL"
        sql += " ORDER BY g.created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            refresh_expires_at = data.pop("refresh_expires_at")
            result.append((Grant(**data), refresh_expires_at))
        return result

    def insert_token(self, token: Token) -> None:
        with self.transaction() as db:
            self._insert_token(db, token)

    @staticmethod
    def _insert_token(db: sqlite3.Connection, token: Token) -> None:
        db.execute(
            "INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,?)",
            (
                token.id, token.token_hash, token.kind, token.grant_id, token.created_at,
                token.expires_at, token.revoked_at, token.rotated_at, token.replaced_by,
            ),
        )

    def get_token(self, token_hash: str) -> Token | None:
        row = self._one("SELECT * FROM tokens WHERE token_hash = ?", (token_hash,))
        return Token(**dict(row)) if row else None

    def get_token_by_id(self, token_id: str) -> Token | None:
        row = self._one("SELECT * FROM tokens WHERE id = ?", (token_id,))
        return Token(**dict(row)) if row else None

    def revoke_token(self, token_id: str, now: int) -> None:
        with self.transaction() as db:
            db.execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, token_id))

    def rotate_refresh(
        self, old: Token, new_access: Token, new_refresh: Token, now: int, rotated_not_before: int
    ) -> bool:
        """Exchange a refresh token for a new access/refresh pair in one transaction.

        A token that was already rotated is accepted again only if the first
        rotation happened at or after `rotated_not_before` (client retry after a
        lost response, or two concurrent refreshes). Returns False otherwise, or
        if the token was revoked meanwhile."""
        with self.transaction() as db:
            current = db.execute("SELECT * FROM tokens WHERE id = ?", (old.id,)).fetchone()
            if current is None or current["revoked_at"] is not None:
                return False
            if current["rotated_at"] is not None and current["rotated_at"] < rotated_not_before:
                return False
            self._insert_token(db, new_access)
            self._insert_token(db, new_refresh)
            db.execute(
                "UPDATE tokens SET rotated_at = COALESCE(rotated_at, ?), replaced_by = ? WHERE id = ?",
                (now, new_refresh.id, old.id),
            )
            db.execute("UPDATE grants SET last_used_at = ? WHERE id = ?", (now, old.grant_id))
            return True

    def lookup_access_token(self, token_hash: str, now: int) -> tuple[Token, Grant] | None:
        row = self._one(
            "SELECT t.id AS t_id, g.id AS g_id FROM tokens t JOIN grants g ON g.id = t.grant_id"
            " WHERE t.token_hash = ? AND t.kind = 'access' AND t.revoked_at IS NULL AND t.expires_at > ?"
            " AND g.revoked_at IS NULL",
            (token_hash, now),
        )
        if row is None:
            return None
        token = self.get_token_by_id(row["t_id"])
        grant = self.get_grant(row["g_id"])
        if token is None or grant is None:
            return None
        return token, grant

    # -- login throttling -----------------------------------------------------------

    def get_login_state(self) -> LoginState:
        row = self._one("SELECT failures, last_failure_at, locked_until, last_totp_step FROM login_state WHERE id = 1")
        assert row is not None
        return LoginState(**dict(row))

    def save_login_state(self, state: LoginState) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE login_state SET failures = ?, last_failure_at = ?, locked_until = ?, last_totp_step = ?"
                " WHERE id = 1",
                (state.failures, state.last_failure_at, state.locked_until, state.last_totp_step),
            )

    # -- housekeeping -----------------------------------------------------------------

    def cleanup(self, now: int) -> None:
        day = 86400
        with self.transaction() as db:
            db.execute("DELETE FROM auth_requests WHERE expires_at <= ?", (now,))
            db.execute("DELETE FROM auth_codes WHERE expires_at <= ?", (now - day,))
            db.execute("DELETE FROM tokens WHERE expires_at <= ?", (now - day,))
            db.execute(
                "DELETE FROM grants WHERE revoked_at IS NOT NULL AND revoked_at <= ?"
                " AND NOT EXISTS (SELECT 1 FROM tokens t WHERE t.grant_id = grants.id)",
                (now - 30 * day,),
            )
