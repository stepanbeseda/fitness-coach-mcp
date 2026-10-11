import sqlite3
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from coach_mcp import store as store_module
from coach_mcp.briefing import ALERT_KEYS
from coach_mcp.store import (
    HISTORY_COLUMNS,
    MIGRATIONS,
    SCHEMA_VERSION,
    STRENGTH_TABLES,
    SchemaTooNewError,
    Store,
)

# The schema version of the strength tables: the only number that changes when this migration is renumbered.
STRENGTH_VERSION = 5
# The latest schema version (goal progress, PR 14); the other number pinned by hand.
LATEST_VERSION = 8


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
    assert {
        "goals",
        "athlete_constraints",
        "sessions",
        "notes",
        "briefing_runs",
        "alert_state",
        "history_days",
        "session_suggestions",
        "strength_workouts",
        "hevy_templates",
        "hevy_sync",
        "race_predictions",
        "prediction_sync",
        "weekly_reports",
    } <= _tables(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION == len(MIGRATIONS) == LATEST_VERSION
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
    assert _user_version(db_path) == SCHEMA_VERSION
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

    # Every alert key the code knows is accepted by the CHECK (rebuilt in migration 4).
    store.record_briefing_sent(date(2026, 10, 10), "green", {key: (True, True) for key in ALERT_KEYS})
    assert set(store.alert_states()) == set(ALERT_KEYS)


# Keep in sync with the downgrade command in coach-mcp/README.md.
DOWNGRADE_SQL = "BEGIN IMMEDIATE; DROP TABLE alert_state; DROP TABLE briefing_runs; PRAGMA user_version = 1; COMMIT;"


def test_downgrade_sql_returns_to_a_version_1_file(db_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:2])
        new = Store(db_path)
        goal_id = new.insert_goal({"title": "Keep me"})
        new.record_briefing_sent(date(2026, 10, 7), "green", {"garmin_auth": (False, False)})
        assert _user_version(db_path) == 2
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


def test_migration_3_upgrades_a_version_2_file_with_data(db_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:2])
        old = Store(db_path)
        goal_id = old.insert_goal({"title": "Keep me"})
        (session_id,) = old.replace_planned(
            date(2026, 10, 5),
            date(2026, 10, 11),
            date(2026, 10, 5),
            [{"date": date(2026, 10, 7), "type": "running", "description": "Easy run"}],
        )[1]
        note_id = old.insert_note(date(2026, 10, 7), "A note", session_id)
        old.record_briefing_sent(date(2026, 10, 7), "yellow", {"hrv_below_baseline": (True, True)})
        assert _user_version(db_path) == 2
        assert "history_days" not in _tables(db_path)
    new = Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION
    goal = new.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"
    session = new.get_session(session_id)
    assert session is not None and session["description"] == "Easy run"
    note = new.get_note(note_id)
    assert note is not None and note["text"] == "A note"
    assert new.briefing_sent(date(2026, 10, 7)) is True
    assert new.alert_states()["hrv_below_baseline"]["value"] == 1
    assert "history_days" in _tables(db_path)
    assert new.history_rows(None, date(2030, 1, 1)) == []


def test_migration_3_upgrades_a_version_1_file_with_data(db_path, monkeypatch):
    """A file written by the PR 03 schema (no briefing tables yet) goes through migrations 2 and 3."""
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
        assert not {"briefing_runs", "alert_state", "history_days"} & _tables(db_path)
    new = Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION
    assert {"briefing_runs", "alert_state", "history_days"} <= _tables(db_path)
    goal = new.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"
    session = new.get_session(session_id)
    assert session is not None and session["description"] == "Easy run"
    note = new.get_note(note_id)
    assert note is not None and note["text"] == "A note"
    assert new.alert_states() == {}
    assert new.history_rows(None, date(2030, 1, 1)) == []


# Keep in sync with the 3 -> 2 downgrade command in coach-mcp/README.md.
DOWNGRADE_3_SQL = "BEGIN IMMEDIATE; DROP TABLE history_days; PRAGMA user_version = 2; COMMIT;"


def test_downgrade_3_sql_returns_to_a_version_2_file(db_path, monkeypatch):
    # It is a 3 -> 2 recipe: the file is built at version 3 (a version 4 file must first go 4 -> 3).
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:3])
        new = Store(db_path)
        goal_id = new.insert_goal({"title": "Keep me"})
        new.upsert_history_recovery(date(2026, 10, 6), {"readiness": 70}, date(2026, 10, 7))
        assert len(new.history_rows(None, date(2026, 10, 7))) == 1
        assert _user_version(db_path) == 3
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DOWNGRADE_3_SQL)
    finally:
        conn.close()
    assert _user_version(db_path) == 2
    assert "history_days" not in _tables(db_path)
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:2])
    old = Store(db_path)
    goal = old.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"


def _one_session(store, day=date(2026, 10, 6)):
    (session_id,) = store.replace_planned(
        date(2026, 10, 5),
        date(2026, 10, 11),
        date(2026, 10, 5),
        [{"date": day, "type": "running", "description": "Easy run"}],
    )[1]
    return session_id


def test_migration_4_upgrades_a_version_3_file_with_data(db_path, monkeypatch):
    old_keys = ("hrv_below_baseline", "resting_hr_elevated", "acwr_out_of_range", "missed_workout", "garmin_auth")
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:3])
        old = Store(db_path)
        goal_id = old.insert_goal({"title": "Keep me"})
        session_id = _one_session(old)
        old.upsert_history_recovery(date(2026, 10, 5), {"readiness": 70}, date(2026, 10, 6))
        old.record_briefing_sent(
            date(2026, 10, 7), "yellow", {key: (i % 2 == 0, i % 3 == 0) for i, key in enumerate(old_keys)}
        )
        before = old.alert_states()
        assert set(before) == set(old_keys)
        assert any(row["fired_on"] for row in before.values()) and any(not row["fired_on"] for row in before.values())
        assert _user_version(db_path) == 3
        assert "session_suggestions" not in _tables(db_path)
    new = Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION
    assert new.alert_states() == before
    goal = new.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"
    session = new.get_session(session_id)
    assert session is not None and session["description"] == "Easy run"
    assert len(new.history_rows(None, date(2030, 1, 1))) == 1
    assert "session_suggestions" in _tables(db_path)
    assert new.suggestions_for([session_id]) == {}
    new.record_briefing_sent(date(2026, 10, 8), "green", {"missed_planned_session": (True, True)})
    assert new.alert_states()["missed_planned_session"] == {
        "value": 1,
        "evaluated_on": "2026-10-08",
        "fired_on": "2026-10-08",
    }
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO alert_state (key, value, evaluated_on) VALUES ('bogus', 1, 'a')")
    finally:
        conn.close()


# Keep in sync with the 4 -> 3 downgrade command in coach-mcp/README.md.
DOWNGRADE_4_SQL = "BEGIN IMMEDIATE; DROP TABLE session_suggestions; PRAGMA user_version = 3; COMMIT;"


def test_downgrade_4_sql_returns_to_a_version_3_file(db_path, monkeypatch):
    # It is a 4 -> 3 recipe: the file is built at version 4 (a version 5 file must first go 5 -> 4).
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:4])
        new = Store(db_path)
        goal_id = new.insert_goal({"title": "Keep me"})
        session_id = _one_session(new)
        new.upsert_suggestions([(session_id, "no_activity", False, None)], date(2026, 10, 7))
        new.record_briefing_sent(
            date(2026, 10, 7), "green", {"garmin_auth": (False, False), "missed_planned_session": (True, True)}
        )
        assert _user_version(db_path) == 4
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DOWNGRADE_4_SQL)
    finally:
        conn.close()
    assert _user_version(db_path) == 3
    assert "session_suggestions" not in _tables(db_path)
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:3])
        old = Store(db_path)
        goal = old.get_goal(goal_id)
        assert goal is not None and goal["title"] == "Keep me"
        assert set(old.alert_states()) == {"garmin_auth", "missed_planned_session"}  # the old code ignores the new key
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:4])
        again = Store(db_path)
        assert _user_version(db_path) == 4
        assert set(again.alert_states()) == {"garmin_auth", "missed_planned_session"}
        assert again.alert_states()["missed_planned_session"]["value"] == 1
        assert "session_suggestions" in _tables(db_path)
        assert again.suggestions_for([session_id]) == {}


def test_suggestion_checks_in_schema(store, db_path):
    session_id = _one_session(store)
    cols = "(session_id, result, final, evidence, evaluated_on, updated_at)"
    ok_evidence = '{"count":1}'
    bad = [
        f"INSERT INTO session_suggestions {cols} VALUES ({session_id}, 'done', 0, NULL, 'a', 'a')",
        f"INSERT INTO session_suggestions {cols} VALUES ({session_id}, 'matched', 0, NULL, 'a', 'a')",
        f"INSERT INTO session_suggestions {cols} VALUES ({session_id}, 'no_activity', 0, '{ok_evidence}', 'a', 'a')",
        f"INSERT INTO session_suggestions {cols} VALUES ({session_id}, 'matched', 0, '{{not json', 'a', 'a')",
        f"INSERT INTO session_suggestions {cols} VALUES ({session_id}, 'matched', 0, "
        f"'{{\"x\":\"{'y' * 3000}\"}}', 'a', 'a')",
        f"INSERT INTO session_suggestions {cols} VALUES ({session_id}, 'matched', 2, '{ok_evidence}', 'a', 'a')",
    ]
    for sql in bad:
        conn = sqlite3.connect(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        finally:
            conn.close()
    # The foreign key: a fresh connection whose first statement switches the PRAGMA on.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO session_suggestions {cols} VALUES (999, 'matched', 0, '{ok_evidence}', 'a', 'a')")
    finally:
        conn.close()
    assert store.suggestions_for([session_id]) == {}
    # The valid shapes are accepted.
    store.upsert_suggestions([(session_id, "matched", True, ok_evidence)], date(2026, 10, 7))
    assert store.suggestions_for([session_id])[session_id]["result"] == "matched"


def test_suggestion_times_are_the_utc_update_times(store):
    first, second = _one_session(store), _one_session(store, date(2026, 10, 5))
    assert store.suggestion_times([]) == {}
    assert store.suggestion_times([first, second]) == {}  # no suggestion yet
    store.upsert_suggestions([(first, "no_activity", False, None), (second, "no_activity", True, None)], D3)
    got = store.suggestion_times([first, second, 999])
    assert set(got) == {first, second}
    stored = datetime.fromisoformat(got[first])
    assert stored.tzinfo is not None and stored.utcoffset().total_seconds() == 0
    assert abs((datetime.now(UTC) - stored).total_seconds()) < 60
    store.upsert_suggestions([(first, "matched", False, '{"count":1,"sport":"running"}')], date(2026, 10, 8))
    assert datetime.fromisoformat(store.suggestion_times([first])[first]) >= stored
    conn = sqlite3.connect(store.path)
    try:  # the value is the update time, not the evaluation day
        conn.execute("UPDATE session_suggestions SET updated_at = ? WHERE session_id = ?",
                     ("2026-10-07T01:30:00+00:00", first))
        conn.commit()
    finally:
        conn.close()
    assert store.suggestion_times([first]) == {first: "2026-10-07T01:30:00+00:00"}


def test_upsert_suggestions_and_suggestions_for(store):
    first, second = _one_session(store), _one_session(store, date(2026, 10, 5))
    evidence = '{"count":1,"sport":"running"}'
    store.upsert_suggestions([(first, "matched", False, evidence), (second, "no_activity", True, None)], D3)
    got = store.suggestions_for([first, second, 999])
    assert got == {
        first: {"session_id": first, "result": "matched", "final": 0, "evidence": evidence, "evaluated_on": "2026-10-07"},
        second: {"session_id": second, "result": "no_activity", "final": 1, "evidence": None, "evaluated_on": "2026-10-07"},
    }
    store.upsert_suggestions([(first, "no_activity", True, None)], date(2026, 10, 8))
    got = store.suggestions_for([first, second])
    assert got[first] == {
        "session_id": first,
        "result": "no_activity",
        "final": 1,
        "evidence": None,
        "evaluated_on": "2026-10-08",
    }
    assert got[second]["evaluated_on"] == "2026-10-07"
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("SELECT count(*) FROM session_suggestions").fetchone()[0] == 2
    finally:
        conn.close()
    assert store.suggestions_for([]) == {}
    assert store.suggestions_for([999]) == {}


# Position of the notes full-text migration (renumbered on rebase without touching these tests).
NOTES_FTS_VERSION = next(i + 1 for i, script in enumerate(MIGRATIONS) if "notes_fts" in script)
# Position of the history repair migration (PR 18), derived the same way.
REPAIR_VERSION = next(i + 1 for i, script in enumerate(MIGRATIONS) if "recovery_missing" in script)
# Keep in sync with the notes_fts downgrade command in coach-mcp/README.md.
DOWNGRADE_NOTES_FTS_SQL = (
    "BEGIN IMMEDIATE; DROP TRIGGER notes_fts_insert; DROP TABLE notes_fts; "
    f"PRAGMA user_version = {NOTES_FTS_VERSION - 1}; COMMIT;"
)


def _fts_rows(path) -> int:
    """Rows in the FTS index storage itself (count(*) FROM notes_fts would read the notes table instead)."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM notes_fts_docsize").fetchone()[0]
    finally:
        conn.close()


def _trigger_names(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    finally:
        conn.close()


def test_notes_fts_migration_indexes_existing_notes(db_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[: NOTES_FTS_VERSION - 1])
        old = Store(db_path)
        (session_id,) = old.replace_planned(
            date(2026, 10, 5),
            date(2026, 10, 11),
            date(2026, 10, 5),
            [{"date": date(2026, 10, 7), "type": "running", "description": "Easy run"}],
        )[1]
        first = old.insert_note(date(2026, 10, 5), "Bolí mě pravé koleno po běhu")
        second = old.insert_note(date(2026, 10, 6), "Únava, špatný spánek")
        _, third = old.update_session_status(session_id, "done", None, (date(2026, 10, 7), "Easy run felt fine"))
        assert third is not None
        assert not any(name.startswith("notes_fts") for name in _tables(db_path))
    new = Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION
    assert _fts_rows(db_path) == 3
    assert [n["id"] for n in new.search_notes("koleno", None, None, None, 10)] == [first]
    assert [n["id"] for n in new.search_notes("spanek", None, None, None, 10)] == [second]
    assert [n["id"] for n in new.search_notes("easy", None, None, None, 10)] == [third]


def test_notes_fts_downgrade_sql_returns_to_the_previous_version(db_path, monkeypatch):
    # A 6 -> 5 recipe: the file is built at the notes version (a later file must first go back to it).
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:NOTES_FTS_VERSION])
        new = Store(db_path)
    new.insert_note(date(2026, 10, 6), "Bolí mě pravé koleno po běhu")
    assert _fts_rows(db_path) == 1
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DOWNGRADE_NOTES_FTS_SQL)
    finally:
        conn.close()
    assert _user_version(db_path) == NOTES_FTS_VERSION - 1
    assert not any(name.startswith("notes_fts") for name in _tables(db_path))
    assert "notes_fts_insert" not in _trigger_names(db_path)
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[: NOTES_FTS_VERSION - 1])
        old = Store(db_path)
        # A leftover trigger would raise "no such table: notes_fts" here.
        old.insert_note(date(2026, 10, 7), "Nová poznámka po návratu")
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:NOTES_FTS_VERSION])
    again = Store(db_path)
    assert _user_version(db_path) == NOTES_FTS_VERSION
    assert [n["text"] for n in again.search_notes("navratu", None, None, None, 10)] == ["Nová poznámka po návratu"]
    assert len(again.search_notes("koleno", None, None, None, 10)) == 1
    assert _fts_rows(db_path) == 2


def test_notes_fts_trigger_and_rebuild_agree(db_path):
    s = Store(db_path)
    for i, text in enumerate(["Bolí koleno", "Easy run", "Únava", "Dobrý spánek"]):
        s.insert_note(date(2026, 10, 1 + i), text)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('integrity-check')")
        notes = conn.execute("SELECT count(*) FROM notes").fetchone()[0]
    finally:
        conn.close()
    assert notes == 4 == _fts_rows(db_path)


D1, D2, D3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)


def test_history_upsert_overwrites_only_given_columns(store):
    store.upsert_history_recovery(D1, {"readiness": 70, "hrv_last_night_ms": 55.5, "hrv_status": "BALANCED"}, D2)
    store.upsert_history_activities({D1: '{"running":{"count":1}}'}, D2)
    (row,) = store.history_rows(None, D3)
    assert row["date"] == "2026-10-05"
    assert (row["readiness"], row["hrv_last_night_ms"], row["hrv_status"]) == (70, 55.5, "BALANCED")
    assert row["recovery_fetched_on"] == "2026-10-06" and row["activities_fetched_on"] == "2026-10-06"
    assert row["activities"] == '{"running":{"count":1}}'
    assert row["sleep_score"] is None

    # A later fetch that carries one column and an explicit None: the other columns keep their value.
    store.upsert_history_recovery(D1, {"hrv_last_night_ms": None, "sleep_score": 80}, D3)
    (row,) = store.history_rows(None, D3)
    assert row["hrv_last_night_ms"] is None  # an explicit None overwrites
    assert row["sleep_score"] == 80
    assert (row["readiness"], row["hrv_status"]) == (70, "BALANCED")  # untouched
    assert row["recovery_fetched_on"] == "2026-10-07"
    # The activity part was not touched by a recovery upsert.
    assert row["activities"] == '{"running":{"count":1}}' and row["activities_fetched_on"] == "2026-10-06"

    # And an activities upsert leaves the recovery columns alone.
    store.upsert_history_activities({D1: "{}"}, D3)
    (row,) = store.history_rows(None, D3)
    assert row["activities"] == "{}" and row["activities_fetched_on"] == "2026-10-07"
    assert row["readiness"] == 70 and row["recovery_fetched_on"] == "2026-10-07"


def test_history_failures_count_and_reset_on_success(store):
    store.record_history_failure([D1, D2], "recovery")
    store.record_history_failure([D1], "recovery")
    store.record_history_failure([D1], "activities")
    rows = {r["date"]: r for r in store.history_rows(None, D3)}
    assert (rows["2026-10-05"]["recovery_failures"], rows["2026-10-05"]["activities_failures"]) == (2, 1)
    assert (rows["2026-10-06"]["recovery_failures"], rows["2026-10-06"]["activities_failures"]) == (1, 0)
    assert rows["2026-10-05"]["recovery_fetched_on"] is None  # a failure is not a fetch

    store.upsert_history_recovery(D1, {"readiness": 60}, D3)
    row = store.history_rows(D1, D1)[0]
    assert row["recovery_failures"] == 0 and row["activities_failures"] == 1  # only the fetched part resets
    store.upsert_history_activities({D1: "{}"}, D3)
    assert store.history_rows(D1, D1)[0]["activities_failures"] == 0
    assert store.history_rows(D2, D2)[0]["recovery_failures"] == 1


def test_history_unknown_column_or_part_is_rejected(store):
    with pytest.raises(ValueError, match="unknown column"):
        store.upsert_history_recovery(D1, {"readiness": 1, "evil = 1; --": 2}, D2)
    with pytest.raises(ValueError, match="unknown column"):
        store.upsert_history_recovery(D1, {"activities": "{}"}, D2)  # not a recovery column
    with pytest.raises(ValueError, match="history part"):
        store.record_history_failure([D1], "recovery_failures = 0; --")
    assert store.history_rows(None, D3) == []


def test_history_rows_range_and_order(store):
    for day in (D3, D1, D2):
        store.upsert_history_recovery(day, {"readiness": day.day}, D3)
    assert [r["date"] for r in store.history_rows(None, D3)] == ["2026-10-05", "2026-10-06", "2026-10-07"]
    assert [r["date"] for r in store.history_rows(D2, D3)] == ["2026-10-06", "2026-10-07"]
    assert [r["date"] for r in store.history_rows(D1, D2)] == ["2026-10-05", "2026-10-06"]
    assert [r["date"] for r in store.history_rows(None, D1)] == ["2026-10-05"]
    assert store.history_rows(date(2026, 10, 8), date(2026, 10, 9)) == []
    assert set(HISTORY_COLUMNS) <= set(store.history_rows(None, D1)[0])


def test_history_status_check_in_schema(store, db_path):
    conn = sqlite3.connect(db_path)
    try:
        for sql in (
            f"INSERT INTO history_days (date, hrv_status, updated_at) VALUES ('2026-10-05', '{'x' * 41}', 'a')",
            "INSERT INTO history_days (date, recovery_failures, updated_at) VALUES ('2026-10-05', -1, 'a')",
            "INSERT INTO history_days (date, activities_failures, updated_at) VALUES ('2026-10-05', -1, 'a')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        conn.execute(f"INSERT INTO history_days (date, hrv_status, updated_at) VALUES ('2026-10-05', '{'x' * 40}', 'a')")
    finally:
        conn.close()


# ------------------------------------------------------------- strength (migration 5)


def test_migration_5_upgrades_a_version_4_file_with_data(db_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:4])
        old = Store(db_path)
        goal_id = old.insert_goal({"title": "Keep me"})
        constraint_id = old.insert_constraint({"kind": "injury", "title": "Left knee"})
        session_id = _one_session(old)
        note_id = old.insert_note(date(2026, 10, 7), "A note", session_id)
        old.record_briefing_sent(
            date(2026, 10, 7), "yellow", {"hrv_below_baseline": (True, True), "missed_planned_session": (True, True)}
        )
        old.upsert_suggestions([(session_id, "matched", True, '{"source":"garmin","sport":"running","count":1}')], D3)
        suggestions = old.suggestions_for([session_id])
        old.upsert_history_recovery(D1, {"readiness": 70, "hrv_last_night_ms": 55.5, "hrv_status": "BALANCED"}, D2)
        old.upsert_history_recovery(D2, {"readiness": 65}, D3)
        old.upsert_history_activities({D1: '{"running":{"count":1}}', D2: "{}"}, D3)
        before = old.history_rows(None, D3)
        assert _user_version(db_path) == 4 and len(before) == 2 and suggestions
        assert not set(STRENGTH_TABLES) & _tables(db_path)
    new = Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION
    # Every old column of every row is unchanged (the repair columns of PR 18 are asserted in S1).
    assert [{k: r[k] for k in before[0]} for r in new.history_rows(None, D3)] == before
    assert new.suggestions_for([session_id]) == suggestions  # PR 13's table is untouched
    goal, constraint = new.get_goal(goal_id), new.get_constraint(constraint_id)
    assert goal is not None and goal["title"] == "Keep me"
    assert constraint is not None and constraint["title"] == "Left knee"
    session, note = new.get_session(session_id), new.get_note(note_id)
    assert session is not None and session["description"] == "Easy run"
    assert note is not None and note["text"] == "A note"
    assert new.briefing_sent(date(2026, 10, 7)) is True and new.alert_states()["hrv_below_baseline"]["value"] == 1
    assert new.alert_states()["missed_planned_session"]["value"] == 1
    assert set(STRENGTH_TABLES) <= _tables(db_path)
    assert new.strength_workout_rows(None, date(2030, 1, 1)) == []
    assert new.hevy_templates() == {} and new.hevy_sync_state() is None and new.strength_template_ids() == set()


def test_migration_5_upgrades_a_version_1_file(db_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:1])
        old = Store(db_path)
        goal_id = old.insert_goal({"title": "Keep me"})
        assert _user_version(db_path) == 1
    new = Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION  # through 2, 3, 4 and 5
    assert set(STRENGTH_TABLES) <= _tables(db_path) and {"history_days", "session_suggestions"} <= _tables(db_path)
    goal = new.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"


# Keep in sync with the 5 -> 4 downgrade command in coach-mcp/README.md.
DOWNGRADE_5_SQL = (
    "BEGIN IMMEDIATE; DROP TABLE strength_workouts; DROP TABLE hevy_templates; DROP TABLE hevy_sync; "
    "PRAGMA user_version = 4; COMMIT;"
)


def test_downgrade_5_sql_returns_to_a_version_4_file(db_path, monkeypatch):
    # It is a 5 -> 4 recipe: the file is built at version 5 (a version 6 file must first go 6 -> 5).
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:STRENGTH_VERSION])
        new = Store(db_path)
    goal_id = new.insert_goal({"title": "Keep me"})
    session_id = _one_session(new)
    new.upsert_suggestions([(session_id, "no_activity", False, None)], D3)
    new.upsert_history_recovery(D1, {"readiness": 70}, D2)
    new.apply_strength_events({"W1": (D1, "2026-10-05T10:00:00.000000Z", "[]")}, ["W2"])
    new.upsert_hevy_template("T1", found=True, title="Bench", type_="weight_reps", group="chest", attempted_on=D2)
    new.record_hevy_sync(D1, datetime(2026, 10, 6, 1, 30, tzinfo=UTC), D2)
    assert _user_version(db_path) == STRENGTH_VERSION
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DOWNGRADE_5_SQL)
    finally:
        conn.close()
    assert _user_version(db_path) == 4
    assert not set(STRENGTH_TABLES) & _tables(db_path)
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:4])
    old = Store(db_path)
    goal = old.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"
    assert [r["readiness"] for r in old.history_rows(None, D3)] == [70]
    assert old.suggestions_for([session_id])[session_id]["result"] == "no_activity"  # PR 13's data stays


STRENGTH_UPDATED = "2026-10-05T10:00:00.000000Z"


def _workouts(path) -> dict[str, tuple]:
    conn = sqlite3.connect(path)
    try:
        return {
            r[0]: r[1:]
            for r in conn.execute("SELECT workout_id, date, deleted, hevy_updated_at, exercises FROM strength_workouts")
        }
    finally:
        conn.close()


def test_strength_upsert_replaces_a_workout_and_older_updates_lose(store, db_path):
    store.apply_strength_events({"W1": (D1, "2026-10-05T10:00:00.000000Z", '[{"a":1}]')}, [])
    store.apply_strength_events({"W1": (D2, "2026-10-05T12:00:00.000000Z", '[{"a":2}]')}, [])  # newer: replaces
    assert _workouts(db_path)["W1"] == ("2026-10-06", 0, "2026-10-05T12:00:00.000000Z", '[{"a":2}]')
    store.apply_strength_events({"W1": (D1, "2026-10-05T11:00:00.000000Z", '[{"a":3}]')}, [])  # older: ignored
    assert _workouts(db_path)["W1"] == ("2026-10-06", 0, "2026-10-05T12:00:00.000000Z", '[{"a":2}]')
    store.apply_strength_events({"W1": (D3, "2026-10-05T12:00:00.000000Z", '[{"a":4}]')}, [])  # equal: re-read
    assert _workouts(db_path)["W1"] == ("2026-10-07", 0, "2026-10-05T12:00:00.000000Z", '[{"a":4}]')
    store.apply_strength_events({"W1": (D1, None, '[{"a":5}]')}, [])  # no timestamp: cannot be older
    assert _workouts(db_path)["W1"] == ("2026-10-05", 0, None, '[{"a":5}]')
    store.apply_strength_events({"W1": (D2, "2026-10-05T09:00:00.000000Z", '[{"a":6}]')}, [])  # stored has none
    assert _workouts(db_path)["W1"][0] == "2026-10-06"


def test_strength_tombstone_is_terminal_and_older_updates_lose(store, db_path):
    store.apply_strength_events({"W1": (D1, STRENGTH_UPDATED, '[{"a":1}]')}, [])
    store.apply_strength_events({}, ["W1", "W9"])  # W9 was never seen
    assert _workouts(db_path)["W1"] == ("2026-10-05", 1, STRENGTH_UPDATED, "[]")  # the day is kept
    assert _workouts(db_path)["W9"] == (None, 1, None, "[]")
    store.apply_strength_events({"W1": (D2, "2030-01-01T00:00:00.000000Z", '[{"a":2}]'), "W9": (D2, None, "[]")}, [])
    assert _workouts(db_path)["W1"] == ("2026-10-05", 1, STRENGTH_UPDATED, "[]")  # never revived, even by a newer one
    assert _workouts(db_path)["W9"] == (None, 1, None, "[]")
    store.apply_strength_events({}, ["W1"])  # deleting twice is harmless
    assert _workouts(db_path)["W1"][1] == 1


def test_strength_events_are_one_transaction(store, db_path):
    store.apply_strength_events({"W1": (D1, STRENGTH_UPDATED, "[]")}, [])
    with pytest.raises(sqlite3.IntegrityError):
        store.apply_strength_events({"W2": (D1, STRENGTH_UPDATED, "[]")}, ["x" * 65])  # the delete is invalid
    assert set(_workouts(db_path)) == {"W1"}  # W2 was rolled back with it


def test_strength_rows_exclude_deleted_and_filter_dates(store):
    store.apply_strength_events(
        {
            "W3": (D3, STRENGTH_UPDATED, "[]"),
            "W1": (D1, STRENGTH_UPDATED, "[]"),
            "W2b": (D2, STRENGTH_UPDATED, "[]"),
            "W2a": (D2, STRENGTH_UPDATED, "[]"),
            "Wdeleted": (D2, STRENGTH_UPDATED, "[]"),
        },
        [],
    )
    store.apply_strength_events({}, ["Wdeleted"])
    assert [r["workout_id"] for r in store.strength_workout_rows(None, D3)] == ["W1", "W2a", "W2b", "W3"]
    assert [r["workout_id"] for r in store.strength_workout_rows(D2, D3)] == ["W2a", "W2b", "W3"]
    assert [r["workout_id"] for r in store.strength_workout_rows(D2, D2)] == ["W2a", "W2b"]
    assert [r["workout_id"] for r in store.strength_workout_rows(None, D1)] == ["W1"]
    assert store.strength_workout_rows(date(2026, 10, 8), date(2026, 10, 9)) == []
    assert set(store.strength_workout_rows(None, D1)[0]) >= {"workout_id", "date", "exercises", "hevy_updated_at"}


def test_strength_template_ids_skip_invalid_json(store):
    good = '[{"template_id":"T1","sets":1},{"template_id":"T2"},{"template_id":5},{"template_id":""},"x",{"sets":1}]'
    store.apply_strength_events(
        {
            "W1": (D1, STRENGTH_UPDATED, good),
            "W2": (D1, STRENGTH_UPDATED, "not json"),
            "W3": (D1, STRENGTH_UPDATED, '{"template_id":"T9"}'),  # not a list
            "W4": (D1, STRENGTH_UPDATED, '[{"template_id":"T3"}]'),
            "W5": (D1, STRENGTH_UPDATED, '[{"template_id":"T4"}]'),
        },
        [],
    )
    store.apply_strength_events({}, ["W5"])  # a deleted workout references nothing
    assert store.strength_template_ids() == {"T1", "T2", "T3"}


def test_template_failure_does_not_overwrite_a_resolved_template(store):
    store.record_hevy_template_failure("T1", D1)
    assert store.hevy_templates()["T1"]["found"] is None and store.hevy_templates()["T1"]["attempted_on"] == "2026-10-05"
    store.record_hevy_template_failure("T1", D2)
    assert store.hevy_templates()["T1"]["attempted_on"] == "2026-10-06"
    store.upsert_hevy_template("T1", found=True, title="Bench", type_="weight_reps", group="chest", attempted_on=D2)
    store.record_hevy_template_failure("T1", D3)
    row = store.hevy_templates()["T1"]
    assert (row["found"], row["title"], row["primary_muscle_group"], row["attempted_on"]) == (1, "Bench", "chest", "2026-10-06")
    store.upsert_hevy_template("T2", found=False, title=None, type_=None, group=None, attempted_on=D1)
    store.record_hevy_template_failure("T2", D3)
    assert store.hevy_templates()["T2"]["found"] == 0 and store.hevy_templates()["T2"]["attempted_on"] == "2026-10-05"
    store.upsert_hevy_template("T1", found=True, title="Renamed", type_="x", group=None, attempted_on=D3)
    assert store.hevy_templates()["T1"]["title"] == "Renamed" and store.hevy_templates()["T1"]["primary_muscle_group"] is None


def test_hevy_sync_single_row(store, db_path):
    assert store.hevy_sync_state() is None
    store.record_hevy_sync(D1, datetime(2026, 10, 6, 1, 30, tzinfo=UTC), D2)
    assert store.hevy_sync_state() == {"covered_from": "2026-10-05", "synced_at": "2026-10-06T01:30:00+00:00", "synced_on": "2026-10-06"}
    prague = datetime(2026, 10, 7, 3, 30, tzinfo=ZoneInfo("Europe/Prague"))
    store.record_hevy_sync(D2, prague, D3)
    assert store.hevy_sync_state() == {"covered_from": "2026-10-06", "synced_at": "2026-10-07T01:30:00+00:00", "synced_on": "2026-10-07"}
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT count(*) FROM hevy_sync").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO hevy_sync VALUES (2, 'a', 'b', 'c', 'd')")
    finally:
        conn.close()


def test_strength_check_constraints(store, db_path):
    conn = sqlite3.connect(db_path)
    try:
        bad = [
            "INSERT INTO strength_workouts (workout_id, deleted, updated_at) VALUES ('W1', 0, 'a')",  # live, no date
            "INSERT INTO strength_workouts (workout_id, date, deleted, updated_at) VALUES ('W1', '2026-10-05', 2, 'a')",
            f"INSERT INTO strength_workouts (workout_id, date, updated_at) VALUES ('{'x' * 65}', '2026-10-05', 'a')",
            "INSERT INTO strength_workouts (workout_id, date, updated_at) VALUES ('', '2026-10-05', 'a')",
            f"INSERT INTO strength_workouts (workout_id, date, hevy_updated_at, updated_at) "
            f"VALUES ('W1', '2026-10-05', '{'x' * 41}', 'a')",
            f"INSERT INTO hevy_templates (template_id, updated_at) VALUES ('{'x' * 65}', 'a')",
            "INSERT INTO hevy_templates (template_id, found, updated_at) VALUES ('T1', 2, 'a')",
            f"INSERT INTO hevy_templates (template_id, title, updated_at) VALUES ('T1', '{'x' * 201}', 'a')",
            f"INSERT INTO hevy_templates (template_id, type, updated_at) VALUES ('T1', '{'x' * 41}', 'a')",
            f"INSERT INTO hevy_templates (template_id, primary_muscle_group, updated_at) VALUES ('T1', '{'x' * 41}', 'a')",
        ]
        for sql in bad:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        conn.execute("INSERT INTO strength_workouts (workout_id, date, updated_at) VALUES ('x' || '', '2026-10-05', 'a')")
        conn.execute("INSERT INTO strength_workouts (workout_id, deleted, updated_at) VALUES ('W2', 1, 'a')")  # tombstone
    finally:
        conn.close()


# ------------------------------------------------------- history repair (migration 7, PR 18)

# Columns per recovery section, spelled out here on purpose: the migration's SQL repeats them by hand and a
# test that derived them from the code would follow a wrong copy.
HISTORY_SECTIONS: dict[str, tuple[str, ...]] = {
    "readiness": ("readiness",),
    "hrv": ("hrv_last_night_ms", "hrv_weekly_avg_ms", "hrv_status"),
    "sleep": ("sleep_duration_h", "sleep_score"),
    "body_battery": ("body_battery_high", "body_battery_low"),
    "resting_hr": ("resting_hr",),
    "stress": ("stress_avg",),
    "training_status": ("acute_load", "chronic_load", "acwr_ratio", "vo2max_running", "vo2max_cycling"),
}
REPAIR_COLUMNS = ("recovery_missing", "recovery_repairs", "recovery_repaired_on")
ALL_SECTION_KEYS = ",".join(HISTORY_SECTIONS)
# Keep in sync with the 7 -> 6 downgrade command in coach-mcp/README.md.
DOWNGRADE_7_SQL = (
    "BEGIN IMMEDIATE; ALTER TABLE history_days DROP COLUMN recovery_repaired_on; "
    "ALTER TABLE history_days DROP COLUMN recovery_repairs; ALTER TABLE history_days DROP COLUMN recovery_missing; "
    "PRAGMA user_version = 6; COMMIT;"
)


def _insert_v6_row(path, day: str, fetched_on: str | None, values: dict[str, object], failures: int = 0) -> None:
    """A history row as the code before migration 7 wrote it (raw SQL: the store of that version is gone)."""
    columns = ["date", "recovery_fetched_on", "recovery_failures", "updated_at", *values]
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                f"INSERT INTO history_days ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                (day, fetched_on, failures, "2026-10-01T00:00:00+00:00", *values.values()),
            )
    finally:
        conn.close()


def _all_columns_set(skip: tuple[str, ...] = (), only: tuple[str, ...] | None = None) -> dict[str, object]:
    """Every recovery column set to a value, except those in ``skip`` (NULL) or, with ``only``, all but those."""
    out: dict[str, object] = {}
    for columns in HISTORY_SECTIONS.values():
        for column in columns:
            holds = column not in skip if only is None else column in only
            out[column] = None if not holds else ("BALANCED" if column == "hrv_status" else 7.5)
    return out


def _v6_store(db_path, monkeypatch) -> Store:
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[: REPAIR_VERSION - 1])
        old = Store(db_path)
    assert _user_version(db_path) == REPAIR_VERSION - 1
    return old


def test_history_sections_table_matches_the_job_and_the_schema():
    from coach_mcp.history import _SECTION_COLUMNS, SNAPSHOT_SECTIONS

    assert tuple(HISTORY_SECTIONS) == SNAPSHOT_SECTIONS
    assert {k: tuple(c for c, _ in v) for k, v in _SECTION_COLUMNS.items()} == HISTORY_SECTIONS
    assert {c for columns in HISTORY_SECTIONS.values() for c in columns} == set(HISTORY_COLUMNS)


def test_migration_7_upgrades_a_version_6_file_with_data(db_path, monkeypatch):
    old = _v6_store(db_path, monkeypatch)
    goal_id = old.insert_goal({"title": "Keep me"})
    constraint_id = old.insert_constraint({"kind": "injury", "title": "Left knee"})
    session_id = _one_session(old)
    note_id = old.insert_note(date(2026, 10, 7), "A note", session_id)
    old.record_briefing_sent(date(2026, 10, 7), "yellow", {"hrv_below_baseline": (True, True)})
    old.upsert_suggestions([(session_id, "matched", True, '{"source":"garmin","sport":"running","count":1}')], D3)
    old.upsert_history_recovery(D1, {"readiness": 70, "hrv_last_night_ms": 55.5, "hrv_status": "BALANCED"}, D2)
    old.upsert_history_recovery(D2, {"readiness": 65, "sleep_score": 80}, D3)
    old.upsert_history_activities({D1: '{"running":{"count":1}}', D2: "{}"}, D3)
    old.record_history_failure([D3], "recovery")
    before = old.history_rows(None, D3)
    assert len(before) == 3 and not set(REPAIR_COLUMNS) & set(before[0])
    new = Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION
    after = new.history_rows(None, D3)
    # Every old column of every row is unchanged.
    assert [{k: r[k] for k in before[0]} for r in after] == before
    assert [r["recovery_repairs"] for r in after] == [0, 0, 0]
    assert [r["recovery_repaired_on"] for r in after] == [None, None, None]
    goal, constraint = new.get_goal(goal_id), new.get_constraint(constraint_id)
    assert goal is not None and goal["title"] == "Keep me"
    assert constraint is not None and constraint["title"] == "Left knee"
    session, note = new.get_session(session_id), new.get_note(note_id)
    assert session is not None and session["description"] == "Easy run"
    assert note is not None and note["text"] == "A note"
    assert new.briefing_sent(date(2026, 10, 7)) is True
    assert new.suggestions_for([session_id])[session_id]["result"] == "matched"
    assert [n["id"] for n in new.search_notes("note", None, None, None, 10)] == [note_id]


@pytest.mark.parametrize("section", list(HISTORY_SECTIONS))
def test_migration_7_classifies_legacy_rows(section, db_path, monkeypatch):
    _v6_store(db_path, monkeypatch)
    # Only the columns of ``section`` are NULL: exactly that section is missing.
    _insert_v6_row(db_path, "2026-09-01", "2026-09-02", _all_columns_set(skip=HISTORY_SECTIONS[section]))
    new = Store(db_path)
    (row,) = new.history_rows(None, date(2026, 9, 30))
    assert row["recovery_missing"] == section
    assert row["recovery_repairs"] == 0 and row["recovery_repaired_on"] is None
    assert row["updated_at"] == "2026-10-01T00:00:00+00:00"  # the classification is not an update


def test_migration_7_classifies_the_other_legacy_rows(db_path, monkeypatch):
    _v6_store(db_path, monkeypatch)
    expected: dict[str, str | None] = {}

    def add(day: str, fetched_on: str | None, values: dict[str, object], want: str | None, failures: int = 0) -> None:
        _insert_v6_row(db_path, day, fetched_on, values, failures)
        expected[day] = want

    add("2026-09-01", "2026-09-02", _all_columns_set(), "")  # complete
    every_column = tuple(c for columns in HISTORY_SECTIONS.values() for c in columns)
    add("2026-09-02", "2026-09-03", _all_columns_set(skip=every_column), ALL_SECTION_KEYS)
    # A section with one NULL column and the rest set is not missing.
    for n, section in enumerate(("hrv", "sleep", "body_battery", "training_status")):
        add(f"2026-09-1{n}", f"2026-09-2{n}", _all_columns_set(skip=HISTORY_SECTIONS[section][:1]), "")
    # Only one column holds a value: its section is not missing, whichever column it is.
    day = 20
    for section in ("hrv", "sleep", "body_battery", "training_status"):
        for column in HISTORY_SECTIONS[section]:
            others = tuple(c for c in HISTORY_SECTIONS[section] if c != column)
            values = _all_columns_set(skip=others)
            assert values[column] is not None and all(values[c] is None for c in others)
            add(f"2026-08-{day}", "2026-09-30", values, "")
            day += 1
    assert day - 20 == 12
    # Never loaded (failures only) and activities-only rows keep NULL.
    add("2026-07-01", None, {}, None, failures=2)
    add("2026-07-02", None, {"activities": "{}"}, None)
    new = Store(db_path)
    rows = {r["date"]: r for r in new.history_rows(None, date(2026, 12, 31))}
    assert {d: rows[d]["recovery_missing"] for d in expected} == expected


def test_history_repair_columns_have_checks(store, db_path):
    conn = sqlite3.connect(db_path)
    try:
        for sql, params in (
            ("INSERT INTO history_days (date, recovery_repairs, updated_at) VALUES ('2026-10-05', -1, 'a')", ()),
            ("INSERT INTO history_days (date, recovery_repairs, updated_at) VALUES ('2026-10-05', NULL, 'a')", ()),
            (
                "INSERT INTO history_days (date, recovery_missing, updated_at) VALUES ('2026-10-05', ?, 'a')",
                ("x" * 201,),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, params)
        conn.execute(
            "INSERT INTO history_days (date, recovery_missing, updated_at) VALUES ('2026-10-05', ?, 'a')", ("x" * 200,)
        )
        row = conn.execute("SELECT recovery_repairs, recovery_repaired_on FROM history_days").fetchone()
        assert row == (0, None)  # the default of a new row
    finally:
        conn.close()


def test_downgrade_7_sql_returns_to_a_version_6_file(db_path, monkeypatch):
    # It is a 7 -> 6 recipe: the file is built at the repair version (a later file must first go back to it).
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:REPAIR_VERSION])
        new = Store(db_path)
    goal_id = new.insert_goal({"title": "Keep me"})
    new.upsert_history_recovery(D1, {"readiness": 70, "sleep_score": 80}, D2, missing="hrv")
    new.repair_history_recovery(D1, {"hrv_last_night_ms": 50}, "", D3, charged=True)
    assert new.history_rows(None, D3)[0]["recovery_repairs"] == 1
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DOWNGRADE_7_SQL)
    finally:
        conn.close()
    assert _user_version(db_path) == REPAIR_VERSION - 1
    conn = sqlite3.connect(db_path)
    try:
        assert not set(REPAIR_COLUMNS) & {r[1] for r in conn.execute("PRAGMA table_info(history_days)")}
    finally:
        conn.close()
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[: REPAIR_VERSION - 1])
    old = Store(db_path)
    goal = old.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"
    (row,) = old.history_rows(None, D3)
    assert (row["readiness"], row["sleep_score"], row["hrv_last_night_ms"]) == (70, 80, 50)  # repaired value stays
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:REPAIR_VERSION])
    again = Store(db_path)
    assert _user_version(db_path) == REPAIR_VERSION
    (row,) = again.history_rows(None, D3)
    # Reclassified by the migration: only the sections without any value are missing.
    assert row["recovery_missing"] == "body_battery,resting_hr,stress,training_status" and row["recovery_repairs"] == 0


def test_repair_history_recovery_writes_only_given_columns(store):
    store.upsert_history_recovery(D1, {"readiness": 70, "sleep_score": 80}, D2, missing="hrv,sleep")
    store.record_history_failure([D1, D1], "recovery")
    (before,) = store.history_rows(None, D3)
    assert before["recovery_failures"] == 2 and before["recovery_missing"] == "hrv,sleep"
    store.repair_history_recovery(D1, {"hrv_last_night_ms": 50}, "sleep", date(2026, 10, 9), charged=True)
    (row,) = store.history_rows(None, D3)
    assert row["hrv_last_night_ms"] == 50
    assert (row["sleep_score"], row["readiness"]) == (80, 70)
    assert row["recovery_missing"] == "sleep"
    assert row["recovery_repairs"] == 1 and row["recovery_repaired_on"] == "2026-10-09"
    assert row["recovery_failures"] == 0
    assert row["recovery_fetched_on"] == before["recovery_fetched_on"] == "2026-10-06"
    store.repair_history_recovery(D1, {}, "sleep", date(2026, 10, 10), charged=False)
    (row,) = store.history_rows(None, D3)
    assert row["recovery_repairs"] == 1 and row["recovery_repaired_on"] == "2026-10-10"
    assert row["hrv_last_night_ms"] == 50  # empty values: only the bookkeeping is written
    with pytest.raises(ValueError, match="unknown column"):
        store.repair_history_recovery(D1, {"hrv_status": "X", "evil = 1; --": 2}, "", D3, charged=True)
    (row,) = store.history_rows(None, D3)
    assert row["hrv_status"] is None and row["recovery_repairs"] == 1


def test_upsert_history_recovery_missing_is_optional(store):
    store.upsert_history_recovery(D1, {"readiness": 70}, D2, missing="hrv")
    store.upsert_history_recovery(D1, {"readiness": 71}, D3)  # no ``missing``: the stored value stays
    (row,) = store.history_rows(None, D3)
    assert row["recovery_missing"] == "hrv" and row["readiness"] == 71 and row["recovery_fetched_on"] == "2026-10-07"
    store.upsert_history_recovery(D1, {"readiness": 72}, D3, missing="")  # '' overwrites
    assert store.history_rows(None, D3)[0]["recovery_missing"] == ""
    store.upsert_history_recovery(D2, {"readiness": 60}, D3)  # a new row without ``missing`` stays NULL
    assert store.history_rows(D2, D2)[0]["recovery_missing"] is None


# ------------------------------------------------- goal progress (migration 8, PR 14)

PREDICTION_VERSION = next(i + 1 for i, script in enumerate(MIGRATIONS) if "race_predictions" in script)
# Keep in sync with the 8 -> 7 downgrade command in coach-mcp/README.md.
DOWNGRADE_8_SQL = (
    "BEGIN IMMEDIATE; DROP TABLE weekly_reports; DROP TABLE prediction_sync; DROP TABLE race_predictions; "
    "ALTER TABLE goals DROP COLUMN target_value; ALTER TABLE goals DROP COLUMN metric; "
    "PRAGMA user_version = 7; COMMIT;"
)
NEW_TABLES = {"race_predictions", "prediction_sync", "weekly_reports"}


def _old_store(db_path, monkeypatch) -> Store:
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[: PREDICTION_VERSION - 1])
        old = Store(db_path)
    assert _user_version(db_path) == PREDICTION_VERSION - 1
    return old


def _raw(db_path, sql: str, params: tuple = ()) -> None:
    """Run one statement on a fresh connection (the CHECK constraints are the subject)."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(sql, params)
    finally:
        conn.close()


def test_migration_8_upgrades_a_version_7_file_with_data(db_path, monkeypatch):
    old = _old_store(db_path, monkeypatch)
    goal_id = old.insert_goal({"title": "Keep me", "target": "1:40", "target_date": date(2027, 4, 5)})
    constraint_id = old.insert_constraint({"kind": "injury", "title": "Left knee"})
    session_id = _one_session(old)
    note_id = old.insert_note(date(2026, 10, 7), "A note", session_id)
    old.record_briefing_sent(date(2026, 10, 7), "yellow", {"hrv_below_baseline": (True, True)})
    old.upsert_history_recovery(D1, {"readiness": 70, "hrv_last_night_ms": 55.5}, D2)
    old.upsert_history_activities({D1: '{"running":{"count":1}}'}, D3)
    goals_before = old.list_goals()
    history_before = old.history_rows(None, D3)
    assert NEW_TABLES.isdisjoint(_tables(db_path))
    new = Store(db_path)
    assert _user_version(db_path) == SCHEMA_VERSION
    assert NEW_TABLES <= _tables(db_path)
    (goal,) = new.list_goals()
    assert {k: goal[k] for k in goals_before[0]} == goals_before[0]
    assert goal["metric"] is None and goal["target_value"] is None
    assert new.history_rows(None, D3) == history_before
    constraint = new.get_constraint(constraint_id)
    assert constraint is not None and constraint["title"] == "Left knee"
    session, note = new.get_session(session_id), new.get_note(note_id)
    assert session is not None and session["description"] == "Easy run"
    assert note is not None and note["text"] == "A note"
    assert new.briefing_sent(date(2026, 10, 7)) is True
    assert [n["id"] for n in new.search_notes("note", None, None, None, 10)] == [note_id]


def test_migration_8_checks(store, db_path):
    gid = store.insert_goal({"title": "Goal"})
    goal = "UPDATE goals SET metric = ?, target_value = ? WHERE id = ?"
    for bad in (("race_1k", None), (None, 1800.0), ("race_5k", 0), ("race_5k", 1000000)):
        with pytest.raises(sqlite3.IntegrityError):
            _raw(db_path, goal, (*bad, gid))
    _raw(db_path, goal, ("race_5k", 999999.9, gid))
    _raw(db_path, goal, (None, None, gid))  # clearing both in one statement
    with pytest.raises(sqlite3.IntegrityError):  # metric NULL alone while a target is stored
        _raw(db_path, goal, ("race_5k", 1800.0, gid))
        _raw(db_path, "UPDATE goals SET metric = NULL WHERE id = ?", (gid,))
    insert = (
        "INSERT INTO race_predictions (date, {col}, fetched_on, updated_at) VALUES (?, ?, '2026-10-07', 'x')"
    )
    for column in ("time_5k_s", "time_10k_s", "time_half_marathon_s", "time_marathon_s"):
        for bad in (0, 86401):
            with pytest.raises(sqlite3.IntegrityError):
                _raw(db_path, insert.format(col=column), ("2026-10-01", bad))
    _raw(db_path, insert.format(col="time_5k_s"), ("2026-10-01", 86400))
    with pytest.raises(sqlite3.IntegrityError):
        _raw(db_path, insert.format(col="time_5k_s"), ("2026-10-0", 1290))
    with pytest.raises(sqlite3.IntegrityError):
        _raw(db_path, "INSERT INTO prediction_sync (id, covered_from, synced_on, updated_at) VALUES (2, 'a', 'b', 'c')")
    with pytest.raises(sqlite3.IntegrityError):
        _raw(db_path, "INSERT INTO weekly_reports (week, sent_at) VALUES ('2026-W4', 'x')")
    _raw(db_path, "INSERT INTO weekly_reports (week, sent_at) VALUES ('2026-W41', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        _raw(db_path, "INSERT INTO weekly_reports (week, sent_at) VALUES ('2026-W41', 'y')")


def test_store_race_predictions_upserts_whole_days(store):
    d1, d2 = date(2026, 10, 5), date(2026, 10, 6)
    store.store_race_predictions(
        {d1: {"time_5k_s": 1300, "time_10k_s": 2700}, d2: {"time_5k_s": 1290, "time_marathon_s": 12000.5}},
        date(2026, 10, 7), date(2026, 9, 27),
    )
    assert store.prediction_sync_state() == {"covered_from": "2026-09-27", "synced_on": "2026-10-07"}
    rows = {r["date"]: r for r in store.race_prediction_rows(None, date(2026, 10, 7))}
    assert (rows["2026-10-05"]["time_5k_s"], rows["2026-10-05"]["time_10k_s"]) == (1300, 2700)
    assert rows["2026-10-05"]["time_half_marathon_s"] is None and rows["2026-10-06"]["time_marathon_s"] == 12000.5
    store.store_race_predictions({d2: {"time_5k_s": 1280}}, date(2026, 10, 8), date(2026, 9, 20))
    rows = {r["date"]: r for r in store.race_prediction_rows(None, date(2026, 10, 7))}
    assert rows["2026-10-06"]["time_5k_s"] == 1280 and rows["2026-10-06"]["time_marathon_s"] is None  # all four replaced
    assert rows["2026-10-06"]["fetched_on"] == "2026-10-08"
    assert rows["2026-10-05"]["time_10k_s"] == 2700 and rows["2026-10-05"]["fetched_on"] == "2026-10-07"
    assert store.prediction_sync_state() == {"covered_from": "2026-09-20", "synced_on": "2026-10-08"}
    store.store_race_predictions({}, date(2026, 10, 9), date(2026, 9, 20))
    assert store.prediction_sync_state()["synced_on"] == "2026-10-09" and len(store.race_prediction_rows(None, d2)) == 2
    with pytest.raises(ValueError, match="unknown column"):
        store.store_race_predictions({d1: {"time_1k_s": 1}}, date(2026, 10, 9), date(2026, 9, 20))


def test_race_prediction_rows_range(store):
    days = [date(2026, 10, n) for n in (1, 2, 3, 4)]
    store.store_race_predictions({d: {"time_5k_s": 1300 + d.day} for d in days}, date(2026, 10, 7), days[0])
    assert [r["date"] for r in store.race_prediction_rows(date(2026, 10, 2), date(2026, 10, 3))] == [
        "2026-10-02", "2026-10-03"]
    assert [r["date"] for r in store.race_prediction_rows(None, date(2026, 10, 2))] == ["2026-10-01", "2026-10-02"]


def test_store_race_predictions_is_one_transaction(store):
    d1, d2 = date(2026, 10, 5), date(2026, 10, 6)
    with pytest.raises(sqlite3.IntegrityError):
        store.store_race_predictions({d1: {"time_5k_s": 1290}, d2: {"time_5k_s": 0}}, date(2026, 10, 7), d1)
    assert store.race_prediction_rows(None, date(2026, 10, 7)) == []
    assert store.prediction_sync_state() is None


def test_weekly_report_sent_records_once(store):
    assert store.weekly_report_sent("2026-W41") is False
    store.record_weekly_report_sent("2026-W41")
    assert store.weekly_report_sent("2026-W41") is True and store.weekly_report_sent("2026-W42") is False
    with pytest.raises(sqlite3.IntegrityError):
        store.record_weekly_report_sent("2026-W41")


def test_downgrade_8_sql_returns_to_a_version_7_file(db_path, monkeypatch):
    # It is an 8 -> 7 recipe: the file is built at the prediction version.
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[:PREDICTION_VERSION])
        new = Store(db_path)
    goal_id = new.insert_goal({"title": "Keep me", "metric": "race_10k", "target_value": 2700.0})
    new.upsert_history_recovery(D1, {"readiness": 70}, D2)
    new.store_race_predictions({D1: {"time_5k_s": 1290}}, D3, D1)
    new.record_weekly_report_sent("2026-W41")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DOWNGRADE_8_SQL)
    finally:
        conn.close()
    assert _user_version(db_path) == PREDICTION_VERSION - 1
    assert NEW_TABLES.isdisjoint(_tables(db_path))
    conn = sqlite3.connect(db_path)
    try:
        assert not {"metric", "target_value"} & {r[1] for r in conn.execute("PRAGMA table_info(goals)")}
    finally:
        conn.close()
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[: PREDICTION_VERSION - 1])
    old = Store(db_path)
    goal = old.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me"
    assert old.history_rows(None, D3)[0]["readiness"] == 70
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:PREDICTION_VERSION])
    again = Store(db_path)
    assert _user_version(db_path) == PREDICTION_VERSION
    goal = again.get_goal(goal_id)
    assert goal is not None and goal["title"] == "Keep me" and goal["metric"] is None
