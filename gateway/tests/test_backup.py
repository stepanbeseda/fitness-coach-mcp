"""Online backup and restore of the gateway database (gateway.backup, CLI backup-db / restore-db)."""

from __future__ import annotations

import io
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gateway import backup, cli
from gateway.backup import SnapshotError, check_gateway, restore_command, snapshot, write_normalised
from gateway.store import Grant, Store, Token

NOW = datetime(2026, 10, 9, 10, 15, 0, tzinfo=UTC)
ASIDE = "pred-obnovou-20261009T101500Z"
T0 = 1_760_000_000


def add_grant(store: Store, n: int) -> str:
    grant = Grant(f"grant-{n}", "client", "https://gw.test/hevy/mcp", "mcp", None, T0 + n, T0 + n, None, None)
    access = Token(f"access-{n}", f"hash-access-{n}", "access", grant.id, T0, T0 + 3600, None, None, None)
    refresh = Token(f"refresh-{n}", f"hash-refresh-{n}", "refresh", grant.id, T0, T0 + 86400, None, None, None)
    store.create_grant_with_tokens(grant, access, refresh)
    return grant.id


def grant_ids(path: Path) -> list[str]:
    store = Store(str(path))
    try:
        return sorted(g.id for g, _ in store.list_grants())
    finally:
        store.close()


def normalised(data: bytes) -> bytes:
    out = io.BytesIO()
    write_normalised(data, out)
    return out.getvalue()


def open_copy(data: bytes) -> sqlite3.Connection:
    # A WAL-flagged image cannot be opened from memory; the delivery path clears the flag, so do the same.
    conn = sqlite3.connect(":memory:")
    conn.deserialize(normalised(data))
    return conn


def copy_grants(data: bytes) -> list[str]:
    conn = open_copy(data)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM grants ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "gateway.sqlite3"


# --------------------------------------------------------------------- snapshot


def test_snapshot_reads_committed_wal_content_with_a_writer_open(db):
    store = Store(str(db))
    add_grant(store, 1)
    add_grant(store, 2)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO grants VALUES ('grant-pending', 'client', 'r', 'mcp', NULL, 1, NULL, NULL, NULL)"
    )
    try:
        wal = Path(f"{db}-wal")
        assert wal.exists() and wal.stat().st_size > 0  # committed rows are only in the WAL
        data, _ = snapshot(db, check_gateway)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        store.close()
    assert copy_grants(data) == ["grant-1", "grant-2"]
    conn = open_copy(data)
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    conn.close()
    assert data[18:20] == b"\x02\x02"
    assert normalised(data)[18:20] == b"\x01\x01"


def test_snapshot_includes_a_commit_made_during_the_backup(db):
    store = Store(str(db))
    for n in range(1, 4):
        add_grant(store, n)
    calls: list[int] = []

    def hook(status, remaining, total):
        calls.append(remaining)
        if len(calls) == 2:
            add_grant(store, 99)

    try:
        data, _ = snapshot(db, check_gateway, pages=1, progress=hook)
    finally:
        store.close()
    assert len(calls) > 2
    assert copy_grants(data) == ["grant-1", "grant-2", "grant-3", "grant-99"]
    conn = open_copy(data)
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    conn.close()


def test_snapshot_gives_up_when_the_database_stays_locked(db):
    store = Store(str(db))
    add_grant(store, 1)
    store.close()
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO grants VALUES ('pending', 'c', 'r', 'mcp', NULL, 1, NULL, NULL, NULL)")
    started = time.monotonic()
    try:
        with pytest.raises(SnapshotError, match="locked"):
            snapshot(db, check_gateway, deadline_s=0.2, busy_timeout_s=0.05)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert time.monotonic() - started < 5


def test_write_normalised_leaves_a_rollback_journal_database_alone(tmp_path):
    path = tmp_path / "plain.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE grants (id TEXT); CREATE TABLE tokens (id TEXT); INSERT INTO grants VALUES ('g');"
    )
    conn.close()
    data, _ = snapshot(path, check_gateway)
    assert data[18:20] == b"\x01\x01"
    assert normalised(data) == data


def test_write_normalised_changes_only_the_header_flags(db):
    store = Store(str(db))
    add_grant(store, 1)
    data, _ = snapshot(db, check_gateway)
    store.close()
    out = normalised(data)
    assert (data[18:20], out[18:20]) == (b"\x02\x02", b"\x01\x01")
    assert out[:18] == data[:18] and out[20:] == data[20:]


def test_snapshot_refuses_missing_garbage_and_oversized(tmp_path, db, monkeypatch):
    missing = tmp_path / "nope.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        snapshot(missing, check_gateway)
    assert not missing.exists()
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"this is not a database " * 200)
    with pytest.raises(sqlite3.DatabaseError):
        snapshot(garbage, check_gateway)
    Store(str(db)).close()
    monkeypatch.setattr(backup, "MAX_SNAPSHOT_BYTES", 4096)
    with pytest.raises(SnapshotError, match="larger than 4096 bytes"):
        snapshot(db, check_gateway)


def test_snapshot_deadline_ends_a_backup_that_turns_busy_midway(db):
    store = Store(str(db))
    for n in range(1, 4):
        add_grant(store, n)
    store.close()
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("PRAGMA journal_mode=DELETE")
    seen: list[int] = []

    def hook(status, remaining, total):
        seen.append(status)
        if len(seen) == 1:
            holder.execute("BEGIN EXCLUSIVE")
        assert len(seen) < 12, "the deadline did not end the backup"

    try:
        with pytest.raises(SnapshotError, match="stayed locked for 0.3 s"):
            snapshot(db, check_gateway, deadline_s=0.3, busy_timeout_s=0.05, pages=1, progress=hook)
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()


def test_check_gateway(tmp_path, db):
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    with pytest.raises(SnapshotError, match="chybí tabulka grants/tokens"):
        snapshot(empty, check_gateway)

    coach_like = tmp_path / "coach.db"
    conn = sqlite3.connect(coach_like)
    conn.execute("CREATE TABLE goals (id INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(SnapshotError, match="není databáze gateway"):
        snapshot(coach_like, check_gateway)

    only_grants = tmp_path / "only_grants.sqlite3"
    conn = sqlite3.connect(only_grants)
    conn.execute("CREATE TABLE grants (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(SnapshotError, match="není databáze gateway"):
        snapshot(only_grants, check_gateway)

    store = Store(str(db))
    add_grant(store, 1)
    store.close()
    data, info = snapshot(db, check_gateway)
    assert info is None
    assert copy_grants(data) == ["grant-1"]


# ------------------------------------------------------------------ the commands


def test_cli_backup_db_writes_to_stdout(db, tmp_path, capsysbinary):
    store = Store(str(db))
    add_grant(store, 1)
    try:
        assert cli.main(["backup-db", "--db", str(db)]) == 0
    finally:
        store.close()
    captured = capsysbinary.readouterr()
    assert captured.err == b""
    assert captured.out[18:20] == b"\x01\x01"
    assert copy_grants(captured.out) == ["grant-1"]

    target = tmp_path / "copy" / "gateway.sqlite3"
    target.parent.mkdir()
    target.write_bytes(captured.out)
    ro = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
    assert ro.execute("SELECT count(*) FROM grants").fetchone() == (1,)
    ro.close()
    assert [p.name for p in target.parent.iterdir()] == ["gateway.sqlite3"]


def test_cli_backup_db_refuses_a_terminal_and_a_missing_database(db, tmp_path, monkeypatch, capsys):
    assert cli.main(["backup-db", "--db", str(tmp_path / "gone" / "gateway.sqlite3")]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("záloha selhala: žádná databáze v ")
    assert captured.out == ""
    assert not (tmp_path / "gone").exists()

    Store(str(db)).close()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert cli.main(["backup-db", "--db", str(db)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nepíšu databázi na terminál" in captured.err


def test_cli_backup_db_does_not_need_the_gateway_configuration(db, monkeypatch, capsysbinary):
    """The CLI must not call load_settings(): restore and backup run in a bare container without config or secrets."""

    def boom():
        raise AssertionError("load_settings was called")

    monkeypatch.setattr(cli, "load_settings", boom)
    Store(str(db)).close()
    assert cli.main(["backup-db", "--db", str(db)]) == 0
    assert cli.main(["restore-db", "--db", str(db.parent / "other.sqlite3"), str(db)]) == 0


def test_restore_db_moves_stale_wal_aside(tmp_path, capsys):
    src_path = tmp_path / "src.sqlite3"
    store = Store(str(src_path))
    add_grant(store, 1)
    add_grant(store, 2)
    store.close()
    vol = tmp_path / "vol"
    vol.mkdir()
    target = vol / "gateway.sqlite3"
    old = Store(str(target))
    add_grant(old, 7)
    old.close()
    old_bytes = target.read_bytes()
    (vol / "gateway.sqlite3-wal").write_bytes(b"stale wal")
    (vol / "gateway.sqlite3-shm").write_bytes(b"stale shm")

    assert restore_command(target, str(src_path), now=lambda: NOW) == 0

    assert not (vol / "gateway.sqlite3-wal").exists()
    assert not (vol / "gateway.sqlite3-shm").exists()
    assert (vol / f"gateway.sqlite3.{ASIDE}-wal").read_bytes() == b"stale wal"
    assert (vol / f"gateway.sqlite3.{ASIDE}-shm").read_bytes() == b"stale shm"
    assert (vol / f"gateway.sqlite3.{ASIDE}").read_bytes() == old_bytes
    assert grant_ids(target) == ["grant-1", "grant-2"]
    assert f"obnoveno gateway.sqlite3; předchozí soubory jsou jako gateway.sqlite3.{ASIDE}" in capsys.readouterr().err


def test_restore_db_through_the_cli_from_stdin(tmp_path, monkeypatch):
    src_path = tmp_path / "src.sqlite3"
    store = Store(str(src_path))
    add_grant(store, 1)
    store.close()
    target = tmp_path / "gateway.sqlite3"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(src_path.read_bytes())))
    assert cli.main(["restore-db", "--db", str(target)]) == 0
    assert grant_ids(target) == ["grant-1"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["gateway.sqlite3", "src.sqlite3"]


@pytest.mark.parametrize("case", ["garbage", "empty", "no-grants"])
def test_restore_db_rejects_bad_input_and_changes_nothing(case, tmp_path, monkeypatch, capsys):
    vol = tmp_path / "vol"
    vol.mkdir()
    target = vol / "gateway.sqlite3"
    store = Store(str(target))
    add_grant(store, 1)
    store.close()
    (vol / "gateway.sqlite3-wal").write_bytes(b"")
    before = {p.name: p.read_bytes() for p in vol.iterdir()}
    if case == "no-grants":
        other = tmp_path / "other.db"
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE goals (id INTEGER)")
        conn.commit()
        conn.close()
        payload = other.read_bytes()
    else:
        payload = {"garbage": b"not a database " * 300, "empty": b""}[case]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))

    assert cli.main(["restore-db", "--db", str(target)]) == 1

    err = capsys.readouterr().err
    assert err.startswith("obnova selhala: ") and err.endswith("; nic se nezměnilo\n")
    assert {p.name: p.read_bytes() for p in vol.iterdir()} == before


def test_restore_db_refuses_a_terminal(tmp_path, monkeypatch, capsys):
    stdin = io.TextIOWrapper(io.BytesIO(b"x"))
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stdin)
    assert cli.main(["restore-db", "--db", str(tmp_path / "gateway.sqlite3")]) == 2
    assert "nečtu databázi z terminálu" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_restore_db_fresh_directory(tmp_path, capsys):
    src_path = tmp_path / "src.sqlite3"
    store = Store(str(src_path))
    add_grant(store, 1)
    store.close()
    vol = tmp_path / "vol"
    vol.mkdir()
    target = vol / "gateway.sqlite3"
    assert restore_command(target, str(src_path), now=lambda: NOW) == 0
    assert [p.name for p in vol.iterdir()] == ["gateway.sqlite3"]
    assert grant_ids(target) == ["grant-1"]
    assert "předchozí databáze neexistovala" in capsys.readouterr().err


def test_restore_db_refuses_its_own_target_and_same_second_aside(tmp_path, capsys):
    vol = tmp_path / "vol"
    vol.mkdir()
    target = vol / "gateway.sqlite3"
    store = Store(str(target))
    add_grant(store, 1)
    store.close()
    before = target.read_bytes()
    assert restore_command(target, str(target), now=lambda: NOW) == 1
    assert "zdroj je nahrazovaná databáze" in capsys.readouterr().err

    src_path = tmp_path / "src.sqlite3"
    Store(str(src_path)).close()
    (vol / f"gateway.sqlite3.{ASIDE}").write_bytes(b"earlier")
    assert restore_command(target, str(src_path), now=lambda: NOW) == 1
    assert "zkus to za sekundu" in capsys.readouterr().err
    assert target.read_bytes() == before
    assert sorted(p.name for p in vol.iterdir()) == ["gateway.sqlite3", f"gateway.sqlite3.{ASIDE}"]


def test_restore_db_back_from_the_aside_copy(tmp_path):
    src_path = tmp_path / "src.sqlite3"
    store = Store(str(src_path))
    add_grant(store, 1)
    store.close()
    vol = tmp_path / "vol"
    vol.mkdir()
    target = vol / "gateway.sqlite3"
    old = Store(str(target))
    add_grant(old, 5)
    old.close()
    assert restore_command(target, str(src_path), now=lambda: NOW) == 0
    assert grant_ids(target) == ["grant-1"]
    later = datetime(2026, 10, 9, 11, 0, 0, tzinfo=UTC)
    assert restore_command(target, str(vol / f"gateway.sqlite3.{ASIDE}"), now=lambda: later) == 0
    assert grant_ids(target) == ["grant-5"]
