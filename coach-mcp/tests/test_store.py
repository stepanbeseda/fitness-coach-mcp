import sqlite3
from datetime import date

import pytest

from coach_mcp import store as store_module
from coach_mcp.briefing import ALERT_KEYS
from coach_mcp.store import MIGRATIONS, SCHEMA_VERSION, SchemaTooNewError, Store


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
    assert {"goals", "athlete_constraints", "sessions", "notes", "briefing_runs", "alert_state"} <= _tables(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION == len(MIGRATIONS) == 2
    assert s.check() == SCHEMA_VERSION


def test_data_survives_reopen(db_path):
    s = Store(db_path)
    goal_id = s.insert_goal({"title": "Half marathon", "target_date": date(2027, 4, 5)})
    del s
    again = Store(db_path)
    goal = again.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Half marathon" and goal["target_date"] == "2027-04-05"
    assert goal["status"] == "active"
    assert again.check() == SCHEMA_VERSION


def test_newer_schema_refuses_to_open(db_path):
    s = Store(db_path)
    s.insert_goal({"title": "Keep me"})
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(SchemaTooNewError, match="newer"):
        Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION + 1
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT title FROM goals").fetchall() == [("Keep me",)]
    finally:
        conn.close()


def test_failed_migration_rolls_back(db_path, monkeypatch):
    Store(db_path)
    broken = (*MIGRATIONS, "CREATE TABLE extra (a INTEGER); INSERT INTO missing_table VALUES (1);")
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION
    assert "extra" not in _tables(db_path)


def test_failed_migration_leaves_no_open_transaction(db_path, monkeypatch):
    """The failure path rolls back explicitly: no connection is closed mid-transaction."""
    Store(db_path)
    open_at_close: list[bool] = []
    real_connect = sqlite3.connect

    class Recording(sqlite3.Connection):
        def close(self):
            open_at_close.append(self.in_transaction)
            super().close()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=Recording, **kw))
    broken = (*MIGRATIONS, "CREATE TABLE extra (a INTEGER); INSERT INTO missing_table VALUES (1);")
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        Store(db_path)
    assert open_at_close == [False]


def test_migration_applies_pending_entries_in_order(db_path, monkeypatch):
    Store(db_path)
    extended = (*MIGRATIONS, "CREATE TABLE extra (a INTEGER);")
    monkeypatch.setattr(store_module, "MIGRATIONS", extended)
    Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION + 1
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


def test_migration_2_upgrades_a_version_1_file_with_data(db_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:1])
        old = Store(db_path)
        goal_id = old.insert_goal({"title": "Keep me"})
        (session_id,) = old.replace_planned(
            date(2026, 10, 5),
            date(2026, 10, 11),
            date(2026, 10, 5),
            [{"date": date(2026, 10, 7), "type": "running", "description": "Easy run"}],
        )[1]
        note_id = old.insert_note(date(2026, 10, 7), "A note", session_id)
        assert _user_version(db_path) == 1
        assert "briefing_runs" not in _tables(db_path)
    new = Store(db_path)
    assert _user_version(db_path) == 2
    goal = new.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"
    session = new.get_session(session_id)
    assert session is not None and session["description"] == "Easy run"
    note = new.get_note(note_id)
    assert note is not None and note["text"] == "A note"
    assert new.alert_states() == {}
    assert new.briefing_sent(date(2026, 10, 7)) is False


def test_record_briefing_sent_and_alert_state_upsert(store):
    day1, day2 = date(2026, 10, 7), date(2026, 10, 8)
    assert store.briefing_sent(day1) is False
    store.record_briefing_sent(day1, "yellow", {"hrv_below_baseline": (True, True), "garmin_auth": (False, False)})
    assert store.briefing_sent(day1) is True
    assert store.briefing_sent(day2) is False
    states = store.alert_states()
    assert states["hrv_below_baseline"] == {"value": 1, "evaluated_on": "2026-10-07", "fired_on": "2026-10-07"}
    assert states["garmin_auth"] == {"value": 0, "evaluated_on": "2026-10-07", "fired_on": None}

    # Next day: still holds but not new -> fired_on is kept; garmin_auth becomes true and new.
    store.record_briefing_sent(day2, "green", {"hrv_below_baseline": (True, False), "garmin_auth": (True, True)})
    states = store.alert_states()
    assert states["hrv_below_baseline"] == {"value": 1, "evaluated_on": "2026-10-08", "fired_on": "2026-10-07"}
    assert states["garmin_auth"] == {"value": 1, "evaluated_on": "2026-10-08", "fired_on": "2026-10-08"}

    # An alert absent from the update (unknown condition) is left alone.
    store.record_briefing_sent(date(2026, 10, 9), "green", {})
    assert store.alert_states() == states

    with pytest.raises(sqlite3.IntegrityError):
        store.record_briefing_sent(day1, "red", {"hrv_below_baseline": (False, False)})
    # The failed call rolled back as a whole.
    assert store.alert_states() == states

    # Every alert key the code knows is accepted by the CHECK in migration 2.
    store.record_briefing_sent(date(2026, 10, 10), "green", {key: (True, True) for key in ALERT_KEYS})
    assert set(store.alert_states()) == set(ALERT_KEYS)


# Keep in sync with the downgrade command in coach-mcp/README.md.
DOWNGRADE_SQL = "BEGIN IMMEDIATE; DROP TABLE alert_state; DROP TABLE briefing_runs; PRAGMA user_version = 1; COMMIT;"


def test_downgrade_sql_returns_to_a_version_1_file(db_path, monkeypatch):
    new = Store(db_path)
    goal_id = new.insert_goal({"title": "Keep me"})
    new.record_briefing_sent(date(2026, 10, 7), "green", {"garmin_auth": (False, False)})
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DOWNGRADE_SQL)
    finally:
        conn.close()
    assert _user_version(db_path) == 1
    assert "briefing_runs" not in _tables(db_path) and "alert_state" not in _tables(db_path)
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:1])
    old = Store(db_path)
    goal = old.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"
