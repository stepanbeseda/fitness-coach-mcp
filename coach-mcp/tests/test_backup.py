"""Online backup and restore of coach.db (coach_mcp.backup)."""

from __future__ import annotations

import io
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from coach_mcp import backup
from coach_mcp.__main__ import main
from coach_mcp.backup import SnapshotError, check_coach, restore_command, snapshot, write_normalised
from coach_mcp.store import MIGRATIONS, SCHEMA_VERSION, Store

DAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 9, 10, 15, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 9, 11, 0, 0, tzinfo=UTC)
ASIDE = "pred-obnovou-20261009T101500Z"


def make_db(path: Path, titles=("Run a marathon", "Sleep 8 h"), notes: int = 8) -> Path:
    store = Store(path)
    for title in titles:
        store.insert_goal({"title": title})
    for i in range(notes):
        store.insert_note(DAY, f"note {i} " + "x" * 1000)
    return path


def open_copy(data: bytes) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.deserialize(data)
    return conn


def goal_titles(data: bytes) -> list[str]:
    conn = open_copy(data)
    try:
        return [r[0] for r in conn.execute("SELECT title FROM goals ORDER BY id")]
    finally:
        conn.close()


def titles_on_disk(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT title FROM goals ORDER BY id")]
    finally:
        conn.close()


def integrity(data: bytes) -> list[tuple]:
    conn = open_copy(data)
    try:
        return conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()


def normalised(data: bytes) -> bytes:
    out = io.BytesIO()
    write_normalised(data, out)
    return out.getvalue()


# --------------------------------------------------------------------- snapshot


def test_snapshot_includes_a_commit_made_during_the_backup(db_path):
    make_db(db_path)
    calls: list[int] = []

    def hook(status, remaining, total):
        calls.append(remaining)
        if len(calls) == 2:
            other = sqlite3.connect(db_path)
            with other:
                other.execute(
                    "INSERT INTO goals (title, created_at, updated_at) VALUES ('Added mid-backup', 'x', 'x')"
                )
            other.close()

    data, version = snapshot(db_path, check_coach, pages=1, progress=hook)
    assert len(calls) > 2
    assert integrity(data) == [("ok",)]
    assert goal_titles(data) == ["Run a marathon", "Sleep 8 h", "Added mid-backup"]
    assert version == SCHEMA_VERSION


def test_snapshot_ignores_an_open_write_transaction(db_path):
    make_db(db_path)
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO goals (title, created_at, updated_at) VALUES ('Pending', 'x', 'x')")
    try:
        data, _ = snapshot(db_path, check_coach)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert goal_titles(data) == ["Run a marathon", "Sleep 8 h"]
    assert integrity(data) == [("ok",)]


def test_snapshot_gives_up_when_the_database_stays_locked(db_path):
    make_db(db_path)
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO goals (title, created_at, updated_at) VALUES ('Pending', 'x', 'x')")
    started = time.monotonic()
    try:
        with pytest.raises(SnapshotError, match="locked"):
            snapshot(db_path, check_coach, deadline_s=0.2, busy_timeout_s=0.05)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert time.monotonic() - started < 5


def test_snapshot_deadline_ends_a_backup_that_turns_busy_midway(db_path):
    make_db(db_path)
    holder = sqlite3.connect(db_path, isolation_level=None)
    seen: list[int] = []

    def hook(status, remaining, total):
        seen.append(status)
        if len(seen) == 1:  # the first page is copied; now another connection locks the file
            holder.execute("BEGIN EXCLUSIVE")
        # Safety net: without the deadline CPython's backup() would loop on BUSY forever.
        assert len(seen) < 12, "the deadline did not end the backup"

    started = time.monotonic()
    try:
        with pytest.raises(SnapshotError, match="stayed locked for 0.3 s"):
            snapshot(db_path, check_coach, deadline_s=0.3, busy_timeout_s=0.05, pages=1, progress=hook)
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
    assert time.monotonic() - started < 5
    assert len(seen) >= 2


def test_snapshot_deadline_uses_the_injected_clock(db_path):
    make_db(db_path)
    ticks = iter(range(0, 1000, 10))
    with pytest.raises(SnapshotError, match="stayed locked for 5 s"):
        snapshot(db_path, check_coach, deadline_s=5, pages=1, clock=lambda: float(next(ticks)))


def test_write_normalised_fixes_a_wal_header(db_path, tmp_path):
    make_db(db_path)
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    conn.close()
    data, _ = snapshot(db_path, check_coach)
    assert data[18:20] == b"\x02\x02"
    out = normalised(data)
    assert out[18:20] == b"\x01\x01"
    assert out[:18] == data[:18]
    assert out[20:] == data[20:]
    assert len(out) == len(data)

    target = tmp_path / "copy" / "coach.db"
    target.parent.mkdir()
    target.write_bytes(out)
    ro = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
    assert ro.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert ro.execute("SELECT count(*) FROM goals").fetchone() == (2,)
    ro.close()
    assert [p.name for p in target.parent.iterdir()] == ["coach.db"]


def test_write_normalised_leaves_a_rollback_journal_database_alone(db_path):
    make_db(db_path)
    data, _ = snapshot(db_path, check_coach)
    assert data[18:20] == b"\x01\x01"
    assert normalised(data) == data


def test_snapshot_refuses_missing_garbage_and_oversized(tmp_path, db_path, monkeypatch):
    missing = tmp_path / "nope.db"
    with pytest.raises(sqlite3.OperationalError):
        snapshot(missing, check_coach)
    assert not missing.exists()

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a database " * 200)
    with pytest.raises(sqlite3.DatabaseError):
        snapshot(garbage, check_coach)

    make_db(db_path)
    monkeypatch.setattr(backup, "MAX_SNAPSHOT_BYTES", 4096)
    with pytest.raises(SnapshotError, match="larger than 4096 bytes"):
        snapshot(db_path, check_coach)


def test_check_coach(tmp_path, db_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(SnapshotError, match="no goals table"):
        snapshot(empty, check_coach)

    newer = make_db(tmp_path / "newer.db")
    conn = sqlite3.connect(newer)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(SnapshotError) as excinfo:
        snapshot(newer, check_coach)
    assert str(SCHEMA_VERSION + 1) in str(excinfo.value)
    assert f"({SCHEMA_VERSION})" in str(excinfo.value)

    unversioned = tmp_path / "unversioned.db"
    conn = sqlite3.connect(unversioned)
    conn.execute("CREATE TABLE goals (id INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(SnapshotError, match="schema version 0"):
        snapshot(unversioned, check_coach)

    make_db(db_path)
    data, version = snapshot(db_path, check_coach)
    assert version == SCHEMA_VERSION
    assert goal_titles(data) == ["Run a marathon", "Sleep 8 h"]


def test_snapshot_memory_peak(tmp_path):
    """Guard against extra copies of the database: peak RSS grows by < 3.5 x the database size.

CPython's ``serialize()`` itself holds three copies for a moment (the ``:memory:`` pages, SQLite's
buffer and the ``bytes`` object); measured 3.2 x. One more copy anywhere in the production path
(``bytes(data)``, a ``data[:18] + ... + data[20:]`` header fix, a second ``deserialize``) goes above 4 x.
"""
    script = textwrap.dedent(
        """
        import resource, sqlite3, sys
        from datetime import UTC, date, datetime
        from pathlib import Path
        from coach_mcp.backup import check_coach, snapshot, write_normalised
        from coach_mcp.store import Store

        class Sink:
            n = 0
            def write(self, b):
                self.n += len(b)

        path = Path(sys.argv[1])
        Store(path)
        conn = sqlite3.connect(path)
        with conn:
            conn.executemany(
                "INSERT INTO notes (date, text, created_at) VALUES ('2026-10-07', ?, 'x')",
                (("x" * 1000,) for _ in range(8000)),
            )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        size = path.stat().st_size
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        data, _ = snapshot(path, check_coach)
        sink = Sink()
        write_normalised(data, sink)
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(size, (after - before) * 1024, sink.n)
        """
    )
    src = Path(backup.__file__).parent.parent
    env = {"PYTHONPATH": str(src), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "big.db")],
        capture_output=True, text=True, check=True, env=env,
    )
    size, growth, written = (int(x) for x in result.stdout.split())
    assert size > 8 * 1024 * 1024
    assert written == size
    assert growth < 3.5 * size, f"peak grew by {growth} bytes for a {size} byte database"


# ------------------------------------------------------------------ the command


def test_main_backup_writes_the_database_to_stdout(db_path, monkeypatch, capsysbinary):
    make_db(db_path)
    monkeypatch.setenv("COACH_MCP_DB_PATH", str(db_path))
    assert main(["backup"]) == 0
    captured = capsysbinary.readouterr()
    assert captured.err == b""
    assert goal_titles(captured.out) == ["Run a marathon", "Sleep 8 h"]
    assert integrity(captured.out) == [("ok",)]


def test_main_backup_normalises_a_wal_database_on_the_way_out(db_path, monkeypatch, capsysbinary):
    make_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    monkeypatch.setenv("COACH_MCP_DB_PATH", str(db_path))
    assert main(["backup"]) == 0
    assert capsysbinary.readouterr().out[18:20] == b"\x01\x01"


def test_main_backup_refuses_a_terminal_and_missing_db(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "gone" / "coach.db"
    monkeypatch.setenv("COACH_MCP_DB_PATH", str(missing))
    assert main(["backup"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("backup failed: no database at")
    assert captured.out == ""
    assert not missing.parent.exists()

    make_db(tmp_path / "coach.db")
    monkeypatch.setenv("COACH_MCP_DB_PATH", str(tmp_path / "coach.db"))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert main(["backup"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing to write a database to a terminal" in captured.err

    monkeypatch.undo()
    assert main(["backup", "x"]) == 2
    assert capsys.readouterr().err.startswith("usage: python -m coach_mcp")


def test_main_backup_reports_a_bad_database_path_setting(monkeypatch, capsys):
    monkeypatch.setenv("COACH_MCP_DB_PATH", "relative/coach.db")
    assert main(["backup"]) == 2
    assert "COACH_MCP_DB_PATH must be an absolute path" in capsys.readouterr().err


def test_main_backup_fails_cleanly_on_a_garbage_file(tmp_path, monkeypatch, capsysbinary):
    bad = tmp_path / "coach.db"
    bad.write_bytes(b"not a database at all " * 100)
    monkeypatch.setenv("COACH_MCP_DB_PATH", str(bad))
    assert main(["backup"]) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert captured.err.startswith(b"backup failed:")


# ---------------------------------------------------------------------- restore


def test_restore_into_an_empty_directory(tmp_path, capsys):
    src = make_db(tmp_path / "src.db")
    target = tmp_path / "vol" / "coach.db"
    target.parent.mkdir()
    assert restore_command(target, str(src), now=lambda: NOW) == 0
    assert titles_on_disk(target) == ["Run a marathon", "Sleep 8 h"]
    assert [p.name for p in target.parent.iterdir()] == ["coach.db"]
    assert "there was no previous database" in capsys.readouterr().err


def test_restore_moves_the_previous_files_aside(tmp_path, capsys):
    (tmp_path / "src").mkdir()
    src = make_db(tmp_path / "src" / "coach.db")
    vol = tmp_path / "vol"
    vol.mkdir()
    target = make_db(vol / "coach.db", titles=("Old goal",))
    old_bytes = target.read_bytes()
    (vol / "coach.db-journal").write_bytes(b"stale journal")
    (vol / "coach.db-wal").write_bytes(b"stale wal")

    assert restore_command(target, str(src), now=lambda: NOW) == 0
    assert titles_on_disk(target) == ["Run a marathon", "Sleep 8 h"]
    assert (vol / f"coach.db.{ASIDE}").read_bytes() == old_bytes
    assert (vol / f"coach.db.{ASIDE}-journal").read_bytes() == b"stale journal"
    assert (vol / f"coach.db.{ASIDE}-wal").read_bytes() == b"stale wal"
    assert not (vol / "coach.db-journal").exists()
    assert not (vol / "coach.db-wal").exists()
    assert sorted(p.name for p in vol.iterdir()) == sorted(
        ["coach.db", f"coach.db.{ASIDE}", f"coach.db.{ASIDE}-journal", f"coach.db.{ASIDE}-wal"]
    )
    assert f"previous files kept as coach.db.{ASIDE}" in capsys.readouterr().err


def test_restore_from_stdin(db_path, tmp_path, monkeypatch, capsys):
    data, _ = snapshot(make_db(tmp_path / "src.db"), check_coach)
    monkeypatch.setenv("COACH_MCP_DB_PATH", str(db_path))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
    assert main(["restore"]) == 0
    assert titles_on_disk(db_path) == ["Run a marathon", "Sleep 8 h"]
    assert [p.name for p in db_path.parent.iterdir() if p.name.startswith("coach.db")] == ["coach.db"]

    before = db_path.read_bytes()
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    assert main(["restore"]) == 1
    assert "no input on stdin" in capsys.readouterr().err
    assert db_path.read_bytes() == before

    stdin = io.TextIOWrapper(io.BytesIO(data))
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stdin)
    assert main(["restore"]) == 2
    assert "refusing to read a database from a terminal" in capsys.readouterr().err
    assert db_path.read_bytes() == before


def _bad_inputs(tmp_path: Path) -> dict[str, bytes]:
    no_goals = tmp_path / "no_goals.db"
    conn = sqlite3.connect(no_goals)
    conn.execute("CREATE TABLE other (id INTEGER)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    newer = make_db(tmp_path / "newer.db")
    conn = sqlite3.connect(newer)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    return {
        "garbage": b"garbage, not sqlite " * 300,
        "empty": b"",
        "no-goals": no_goals.read_bytes(),
        "newer-schema": newer.read_bytes(),
    }


@pytest.mark.parametrize("case", ["garbage", "empty", "no-goals", "newer-schema", "oversized"])
def test_restore_rejects_bad_input_and_changes_nothing(case, tmp_path, monkeypatch, capsys):
    vol = tmp_path / "vol"
    vol.mkdir()
    target = make_db(vol / "coach.db", titles=("Keep me",))
    (vol / "coach.db-journal").write_bytes(b"")  # a leftover must not be touched either
    snapshot_before = {p.name: p.read_bytes() for p in vol.iterdir()}
    if case == "oversized":
        payload = make_db(tmp_path / "ok.db").read_bytes()
        monkeypatch.setattr(backup, "MAX_SNAPSHOT_BYTES", len(payload) - 1)
    else:
        payload = _bad_inputs(tmp_path)[case]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))

    assert restore_command(target, "-", now=lambda: NOW) == 1

    err = capsys.readouterr().err
    assert err.startswith("restore failed: ") and err.endswith("; nothing changed\n")
    assert {p.name: p.read_bytes() for p in vol.iterdir()} == snapshot_before


def test_restore_accepts_an_older_schema_and_store_migrates_it(tmp_path, capsys):
    assert SCHEMA_VERSION > 1
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript(MIGRATIONS[0])
    conn.execute("INSERT INTO goals (title, created_at, updated_at) VALUES ('From the past', 'x', 'x')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    vol = tmp_path / "vol"
    vol.mkdir()
    target = vol / "coach.db"

    assert restore_command(target, str(old), now=lambda: NOW) == 0

    err = capsys.readouterr().err
    assert "schema version 1" in err
    assert f"migrates it to {SCHEMA_VERSION} on its next start" in err
    store = Store(target)
    assert store.check() == SCHEMA_VERSION
    assert [g["title"] for g in store.list_goals()] == ["From the past"]


def test_restore_refuses_its_own_target_and_same_second_aside(tmp_path, capsys):
    vol = tmp_path / "vol"
    vol.mkdir()
    target = make_db(vol / "coach.db", titles=("Mine",))
    before = target.read_bytes()
    assert restore_command(target, str(target), now=lambda: NOW) == 1
    assert "source is the database being replaced" in capsys.readouterr().err
    assert target.read_bytes() == before

    src = make_db(tmp_path / "src.db")
    (vol / f"coach.db.{ASIDE}").write_bytes(b"earlier side copy")
    assert restore_command(target, str(src), now=lambda: NOW) == 1
    assert "retry in a second" in capsys.readouterr().err
    assert target.read_bytes() == before
    assert sorted(p.name for p in vol.iterdir()) == ["coach.db", f"coach.db.{ASIDE}"]


def test_restore_refuses_a_missing_source_file(tmp_path, capsys):
    target = tmp_path / "coach.db"
    assert restore_command(target, str(tmp_path / "missing.db"), now=lambda: NOW) == 1
    assert "source file not found" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_restore_back_from_the_aside_copy(tmp_path):
    src = make_db(tmp_path / "src.db", titles=("Restored",))
    vol = tmp_path / "vol"
    vol.mkdir()
    target = make_db(vol / "coach.db", titles=("Original",))
    assert restore_command(target, str(src), now=lambda: NOW) == 0
    assert titles_on_disk(target) == ["Restored"]

    assert restore_command(target, str(vol / f"coach.db.{ASIDE}"), now=lambda: LATER) == 0
    assert titles_on_disk(target) == ["Original"]
    assert (vol / "coach.db.pred-obnovou-20261009T110000Z").exists()


def test_restore_applies_a_hot_journal_paired_with_the_source_copy(tmp_path):
    """A side copy keeps its journal under the paired name; restoring from it must see committed data only."""
    vol = tmp_path / "vol"
    vol.mkdir()
    aside = make_db(vol / "coach.db", titles=("Committed",))
    # Leave a hot journal behind: start a write, copy file + journal while the writer is open.
    writer = sqlite3.connect(aside, isolation_level=None)
    writer.execute("PRAGMA cache_size = 1")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE notes SET text = 'overwritten ' || text")
    # With a tiny cache the pages are spilled to the file and the journal holds the originals.
    journal = Path(f"{aside}-journal")
    assert journal.exists() and journal.stat().st_size > 0
    side = vol / "side.db"
    side.write_bytes(aside.read_bytes())
    Path(f"{side}-journal").write_bytes(journal.read_bytes())
    writer.execute("ROLLBACK")
    writer.close()

    target = tmp_path / "target" / "coach.db"
    target.parent.mkdir()
    assert restore_command(target, str(side), now=lambda: NOW) == 0
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT count(*) FROM notes WHERE text LIKE 'overwritten%'").fetchone() == (0,)
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        conn.close()
