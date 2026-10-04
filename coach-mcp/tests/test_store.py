import sqlite3
from datetime import date

import pytest

from coach_mcp import store as store_module
from coach_mcp.store import MIGRATIONS, SCHEMA_VERSION, SchemaTooNewError, Store

NOW = "2026-10-07T00:00:00+00:00"


def _tables(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def _user_version(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_empty_file_gets_schema_and_version(db_path):
    assert not db_path.exists()
    s = Store(db_path)
    assert {"goals", "athlete_constraints", "sessions", "notes"} <= _tables(db_path)
    assert _user_version(db_path) == 1 == SCHEMA_VERSION == len(MIGRATIONS)
    assert s.check() == 1


def test_data_survives_reopen(db_path):
    s = Store(db_path)
    goal_id = s.insert_goal({"title": "Half marathon", "target_date": date(2027, 4, 5)})
    del s
    again = Store(db_path)
    goal = again.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Half marathon" and goal["target_date"] == "2027-04-05"
    assert goal["status"] == "active"
    assert again.check() == 1


def test_newer_schema_refuses_to_open(db_path):
    s = Store(db_path)
    s.insert_goal({"title": "Keep me"})
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    with pytest.raises(SchemaTooNewError, match="newer"):
        Store(db_path)
    assert _user_version(db_path) == 2
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT title FROM goals").fetchall() == [("Keep me",)]
    finally:
        conn.close()


def test_failed_migration_rolls_back(db_path, monkeypatch):
    Store(db_path)
    broken = (MIGRATIONS[0], "CREATE TABLE extra (a INTEGER); INSERT INTO missing_table VALUES (1);")
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        Store(db_path)
    assert _user_version(db_path) == 1
    assert "extra" not in _tables(db_path)


def test_migration_applies_pending_entries_in_order(db_path, monkeypatch):
    Store(db_path)
    extended = (MIGRATIONS[0], "CREATE TABLE extra (a INTEGER);")
    monkeypatch.setattr(store_module, "MIGRATIONS", extended)
    Store(db_path)
    assert _user_version(db_path) == 2
    assert "extra" in _tables(db_path)


def test_missing_directory_fails_at_start(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path / "nope" / "coach.db")


def test_status_and_length_checks_in_schema(store, db_path):
    conn = sqlite3.connect(db_path)
    try:
        bad = [
            "INSERT INTO goals (title, status, created_at, updated_at) VALUES ('x', 'deleted', 'a', 'a')",
            f"INSERT INTO goals (title, created_at, updated_at) VALUES ('{'x' * 201}', 'a', 'a')",
            "INSERT INTO sessions (date, type, description, created_at, updated_at) "
            "VALUES ('2026-10-07', 'yoga', 'x', 'a', 'a')",
            "INSERT INTO sessions (date, type, description, status, created_at, updated_at) "
            "VALUES ('2026-10-07', 'running', 'x', 'deleted', 'a', 'a')",
            "INSERT INTO athlete_constraints (kind, title, created_at, updated_at) VALUES ('mood', 'x', 'a', 'a')",
            "INSERT INTO notes (date, text, created_at) VALUES ('2026-10-07', '', 'a')",
        ]
        for sql in bad:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    finally:
        conn.close()


def test_foreign_keys_are_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_note(date(2026, 10, 7), "orphan", session_id=999)


def test_unknown_columns_are_rejected(store):
    with pytest.raises(ValueError):
        store.insert_goal({"title": "x", "evil = 1; --": "y"})
    goal_id = store.insert_goal({"title": "x"})
    with pytest.raises(ValueError):
        store.update_goal(goal_id, {"id": 5})
