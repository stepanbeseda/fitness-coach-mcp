"""Admin CLI rules: password length, grant revocation by prefix, minted token lifetime."""

from __future__ import annotations

import io
import sys
import time

from conftest import HEVY

from gateway import cli
from gateway.config import SCOPE
from gateway.security import hash_token, new_id, new_token
from gateway.store import Grant, Token


def test_hash_password_enforces_minimum_length(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * (cli.MIN_PASSWORD_LENGTH - 1) + "\n"))
    assert cli.main(["hash-password", "--stdin"]) == 1
    assert f"at least {cli.MIN_PASSWORD_LENGTH} characters" in capsys.readouterr().err

    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * cli.MIN_PASSWORD_LENGTH + "\n"))
    assert cli.main(["hash-password", "--stdin"]) == 0
    assert capsys.readouterr().out.startswith("$argon2id$")


def _add_grant(h, grant_id: str, now: int) -> None:
    grant = Grant(grant_id, cli.CLI_CLIENT_ID, HEVY, SCOPE, None, now, now, None, None)
    access = Token(new_id(), hash_token(new_token()), "access", grant_id, now, now + 600, None, None, None)
    h.store.create_grant_with_tokens(grant, access, None)


def test_grants_revoke_needs_an_unambiguous_prefix(make_harness, tmp_path, monkeypatch):
    h = make_harness()
    monkeypatch.setenv("GATEWAY_CONFIG", str(tmp_path / "config.toml"))
    now = int(time.time())
    first, second = "abcd1111" + "0" * 16, "abcd2222" + "0" * 16
    _add_grant(h, first, now)
    _add_grant(h, second, now)

    def revoked(grant_id: str) -> bool:
        grant = h.store.get_grant(grant_id)
        assert grant is not None
        return grant.revoked_at is not None

    assert cli.main(["grants", "revoke", "abcd"]) == 1
    assert not revoked(first) and not revoked(second)
    assert cli.main(["grants", "revoke"]) == 1
    assert cli.main(["grants", "revoke", "zzzz"]) == 1
    assert not revoked(first) and not revoked(second)

    assert cli.main(["grants", "revoke", "abcd1"]) == 0
    assert revoked(first) and not revoked(second)
    stored = h.store.get_grant(first)
    assert stored is not None and stored.revoked_reason == "revoked by admin"

    assert cli.main(["grants", "revoke", "--all"]) == 0
    assert revoked(second)


def test_mint_token_clamps_ttl_and_rejects_unknown_backend(make_harness, tmp_path, monkeypatch, capsys):
    h = make_harness()
    monkeypatch.setenv("GATEWAY_CONFIG", str(tmp_path / "config.toml"))
    # The harness start wrote the gateway.started audit record to the captured stdout.
    capsys.readouterr()
    for ttl, expected in [("5", 60), ("99999", 3600), ("600", 600)]:
        assert cli.main(["mint-token", "--backend", "hevy", "--ttl", ttl]) == 0
        token = capsys.readouterr().out.strip()
        row = h.store.get_token(hash_token(token))
        assert row is not None
        assert row.expires_at - row.created_at == expected

    assert cli.main(["mint-token", "--backend", "nope"]) == 1
    assert "Unknown backend nope" in capsys.readouterr().err
