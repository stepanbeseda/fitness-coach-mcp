import sqlite3
from datetime import date

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import TODAY
from coach_mcp import store as store_module
from coach_mcp import tools
from coach_mcp.tools import PlannedSession

NOW = "2026-10-01T00:00:00+00:00"


def seed(store, day, status="planned", type_="running", description="Easy run", moved_from_id=None) -> int:
    conn = sqlite3.connect(store.path)
    try:
        cur = conn.execute(
            "INSERT INTO sessions (date, type, description, status, moved_from_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (day, type_, description, status, moved_from_id, NOW, NOW),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def count(store, table) -> int:
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def statuses(store) -> list[tuple[str, str]]:
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute("SELECT date, status FROM sessions ORDER BY id").fetchall()
    finally:
        conn.close()


def planned(day, description="Run", **kw) -> PlannedSession:
    return PlannedSession(date=day, type=kw.pop("type", "running"), description=description, **kw)


def err(fn, *args, **kwargs) -> str:
    with pytest.raises(ToolError) as exc:
        fn(*args, **kwargs)
    return str(exc.value)


# ------------------------------------------------------------------ context


def test_context_has_four_sections_on_empty_store(store):
    ctx = tools.get_coach_context(store, TODAY)
    for key in ("goals", "constraints", "sessions", "notes"):
        assert ctx[key] == []
    assert ctx["today"] == "2026-10-07"
    assert ctx["week"] == {"start": "2026-10-05", "end": "2026-10-11", "iso_week": "2026-W41"}


def test_context_lists_active_goals_with_days_remaining(store):
    store.insert_goal({"title": "A", "target_date": "2026-10-17"})
    store.insert_goal({"title": "B"})
    store.insert_goal({"title": "C", "target_date": "2026-10-01"})
    store.insert_goal({"title": "D", "status": "achieved"})
    store.insert_goal({"title": "E", "status": "abandoned"})
    goals = tools.get_coach_context(store, TODAY)["goals"]
    assert [g["title"] for g in goals] == ["C", "A", "B"]
    assert [g.get("days_remaining") for g in goals] == [-6, 10, None]
    assert "days_remaining" not in goals[2]


def test_context_lists_only_active_constraints(store):
    store.insert_constraint({"kind": "injury", "title": "Knee", "details": "left"})
    store.insert_constraint({"kind": "equipment", "title": "No pool", "status": "resolved"})
    constraints = tools.get_coach_context(store, TODAY)["constraints"]
    assert constraints == [{"id": 1, "kind": "injury", "title": "Knee", "status": "active", "details": "left"}]


def test_context_sessions_are_this_iso_week_without_superseded(store):
    seed(store, "2026-10-04")
    mon = seed(store, "2026-10-05")
    seed(store, "2026-10-08", status="superseded")
    sun = seed(store, "2026-10-11")
    seed(store, "2026-10-12")
    sessions = tools.get_coach_context(store, TODAY)["sessions"]
    assert [s["id"] for s in sessions] == [mon, sun]
    assert [s["weekday"] for s in sessions] == ["Mon", "Sun"]


def test_context_returns_latest_ten_notes_newest_first(store):
    ids = []
    for i in range(12):
        day = date(2026, 10, 1 + i // 2)  # two notes share a date
        ids.append(store.insert_note(day, f"note {i}"))
    notes = tools.get_coach_context(store, TODAY)["notes"]
    assert len(notes) == 10
    assert [n["id"] for n in notes] == sorted(ids[2:], reverse=True)
    assert ids[0] not in [n["id"] for n in notes] and ids[1] not in [n["id"] for n in notes]
    dates = [n["date"] for n in notes]
    assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------- save_plan


def seed_week_41(store):
    mon = seed(store, "2026-10-05", status="done")
    tue = seed(store, "2026-10-06", status="planned")
    thu = seed(store, "2026-10-08", status="planned")
    sat = seed(store, "2026-10-10", status="skipped")
    fri = seed(store, "2026-10-09", status="moved")
    copy = seed(store, "2026-10-14", status="planned", moved_from_id=fri)
    return {"mon": mon, "tue": tue, "thu": thu, "sat": sat, "fri": fri, "copy": copy}


def test_save_plan_replaces_planned_keeps_done_and_past(store):
    ids = seed_week_41(store)
    result = tools.save_plan(
        store,
        TODAY,
        week="2026-10-09",
        sessions=[planned("2026-10-08", "New Thu"), planned("2026-10-09", "New Fri")],
    )
    assert result["superseded"] == 1
    assert result["week"] == {"start": "2026-10-05", "end": "2026-10-11", "iso_week": "2026-W41"}
    assert len(result["created_ids"]) == 2
    got = [(s["date"], s["status"], s["description"]) for s in result["sessions"]]
    assert got == [
        ("2026-10-05", "done", "Easy run"),
        ("2026-10-06", "planned", "Easy run"),
        ("2026-10-08", "planned", "New Thu"),
        ("2026-10-09", "moved", "Easy run"),
        ("2026-10-09", "planned", "New Fri"),
        ("2026-10-10", "skipped", "Easy run"),
    ]
    assert dict(statuses(store))["2026-10-14"] == "planned"  # W42 copy untouched
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("SELECT status FROM sessions WHERE id = ?", (ids["thu"],)).fetchone()[0] == "superseded"
        assert conn.execute("SELECT status FROM sessions WHERE id = ?", (ids["copy"],)).fetchone()[0] == "planned"
    finally:
        conn.close()


def test_save_plan_with_empty_list_supersedes_future_planned(store):
    past = seed(store, "2026-10-06", status="planned")
    future = seed(store, "2026-10-09", status="planned")
    result = tools.save_plan(store, TODAY, week="2026-10-07", sessions=[])
    assert result["superseded"] == 1 and result["created_ids"] == []
    assert [s["id"] for s in result["sessions"]] == [past]
    assert store.get_session(past)["status"] == "planned"
    assert store.get_session(future)["status"] == "superseded"


def test_save_plan_rejects_date_outside_week_without_writing(store):
    seed(store, "2026-10-09", status="planned")
    before = statuses(store)
    message = err(
        tools.save_plan,
        store,
        TODAY,
        week="2026-10-07",
        sessions=[planned("2026-10-08"), planned("2026-10-13")],
    )
    assert "sessions[1].date" in message and "2026-10-05..2026-10-11" in message
    assert statuses(store) == before


def test_save_plan_rejects_past_session_date(store):
    seed(store, "2026-10-09", status="planned")
    before = statuses(store)
    message = err(tools.save_plan, store, TODAY, week="2026-10-07", sessions=[planned("2026-10-06")])
    assert "in the past" in message
    assert statuses(store) == before


def test_save_plan_accepts_today_as_session_date(store):
    result = tools.save_plan(store, TODAY, week="2026-10-07", sessions=[planned("2026-10-07")])
    assert [s["date"] for s in result["sessions"]] == ["2026-10-07"]


def test_save_plan_rejects_invalid_week_and_dates(store):
    assert "must be YYYY-MM-DD" in err(tools.save_plan, store, TODAY, week="2026-W41", sessions=[])
    assert "is not a valid date" in err(tools.save_plan, store, TODAY, week="2026-02-30", sessions=[])
    message = err(tools.save_plan, store, TODAY, week="2026-10-07", sessions=[planned("7.10.2026")])
    assert "sessions[0].date must be YYYY-MM-DD" in message
    assert count(store, "sessions") == 0


def test_save_plan_rejects_blank_description_and_bad_ids_without_writing(store):
    seed(store, "2026-10-09", status="planned")
    before = statuses(store)
    assert err(tools.save_plan, store, TODAY, week="2026-10-07", sessions=[planned("2026-10-08", "   ")]) == (
        "sessions[0].description must not be empty"
    )
    message = err(
        tools.save_plan,
        store,
        TODAY,
        week="2026-10-07",
        sessions=[planned("2026-10-08", garmin_workout_id="x" * 65)],
    )
    assert "sessions[0].garmin_workout_id" in message
    assert statuses(store) == before


def test_save_plan_stores_optional_fields_and_int_ids(store):
    result = tools.save_plan(
        store,
        TODAY,
        week="2026-10-07",
        sessions=[
            planned(
                "2026-10-08",
                "Tempo",
                intensity="  RPE 7 ",
                duration_min=45,
                distance_km=8.5,
                garmin_workout_id=424242,
                hevy_routine_id="b1c2",
            ),
            planned("2026-10-09", "Plain", type="strength"),
        ],
    )
    full, plain = result["sessions"]
    assert full["garmin_workout_id"] == "424242"
    assert (full["hevy_routine_id"], full["duration_min"], full["distance_km"], full["intensity"]) == (
        "b1c2",
        45,
        8.5,
        "RPE 7",
    )
    assert full["weekday"] == "Thu" and full["type"] == "running"
    for key in ("intensity", "duration_min", "distance_km", "garmin_workout_id", "hevy_routine_id"):
        assert key not in plain


def test_planned_session_shape_is_validated_by_pydantic():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PlannedSession(date="2026-10-08", type="yoga", description="x")
    with pytest.raises(pydantic.ValidationError):
        PlannedSession(date="2026-10-08", type="running", description="x", rpe=7)
    with pytest.raises(pydantic.ValidationError):
        PlannedSession(date="2026-10-08", type="running", description="x" * 2001)


# ------------------------------------------------------------------ get_plan


def seed_adherence(store):
    seed(store, "2026-10-01", status="done")
    seed(store, "2026-10-02", status="done")
    seed(store, "2026-10-03", status="skipped")
    seed(store, "2026-10-06", status="planned")  # overdue
    seed(store, "2026-10-09", status="planned")
    moved = seed(store, "2026-10-08", status="moved")
    copy = seed(store, "2026-10-13", status="planned", moved_from_id=moved)
    gone = seed(store, "2026-10-10", status="superseded")
    return moved, copy, gone


def test_get_plan_adherence_counts(store):
    moved, copy, gone = seed_adherence(store)
    result = tools.get_plan(store, TODAY, start_date="2026-10-01", end_date="2026-10-14")
    assert result["adherence"] == {
        "planned": 3,
        "done": 2,
        "skipped": 1,
        "moved": 1,
        "overdue_planned": 1,
        "completion_rate": 0.67,
    }
    by_id = {s["id"]: s for s in result["sessions"]}
    assert gone not in by_id
    assert by_id[moved]["moved_to"] == {"id": copy, "date": "2026-10-13"}
    assert by_id[copy]["moved_from_id"] == moved
    assert "moved_to" not in by_id[copy]


def test_get_plan_include_superseded_lists_but_does_not_count(store):
    moved, copy, gone = seed_adherence(store)
    base = tools.get_plan(store, TODAY, start_date="2026-10-01", end_date="2026-10-14")
    result = tools.get_plan(store, TODAY, start_date="2026-10-01", end_date="2026-10-14", include_superseded=True)
    assert gone in [s["id"] for s in result["sessions"]]
    assert result["adherence"] == base["adherence"]


def test_get_plan_completion_rate_is_null_without_done_or_skipped(store):
    seed(store, "2026-10-08")
    result = tools.get_plan(store, TODAY)
    assert result["adherence"]["completion_rate"] is None
    assert result["adherence"]["planned"] == 1 and result["adherence"]["overdue_planned"] == 0


def test_get_plan_defaults_to_this_iso_week(store):
    seed(store, "2026-10-12")
    result = tools.get_plan(store, TODAY)
    assert (result["start"], result["end"]) == ("2026-10-05", "2026-10-11")
    assert result["sessions"] == []
    result = tools.get_plan(store, TODAY, start_date="2026-10-12")
    assert (result["start"], result["end"]) == ("2026-10-12", "2026-10-18")
    assert len(result["sessions"]) == 1


def test_get_plan_rejects_bad_ranges(store):
    assert "is not a valid date" in err(tools.get_plan, store, TODAY, start_date="2026-13-01")
    assert "must be YYYY-MM-DD" in err(tools.get_plan, store, TODAY, start_date="07.10.2026")
    assert err(tools.get_plan, store, TODAY, start_date="2026-10-08", end_date="2026-10-07") == (
        "start_date must be on or before end_date"
    )
    assert err(tools.get_plan, store, TODAY, start_date="2026-01-01", end_date="2026-04-03") == (
        "date range too long (max 92 days)"
    )
    assert tools.get_plan(store, TODAY, start_date="2026-01-01", end_date="2026-04-02")["sessions"] == []


# ------------------------------------------------------------ update_session


def test_update_session_done_with_note_in_one_call(store):
    sid = seed(store, "2026-10-06")
    result = tools.update_session(store, TODAY, session_id=sid, status="done", note="felt easy")
    assert result["session"]["status"] == "done"
    assert result["note"]["text"] == "felt easy" and result["note"]["date"] == "2026-10-07"
    assert result["note"]["session_id"] == sid
    assert "moved_copy" not in result
    assert store.latest_notes(5)[0]["session_id"] == sid


def test_update_session_moved_creates_linked_copy(store, monkeypatch):
    sid = tools.save_plan(
        store,
        TODAY,
        week="2026-10-07",
        sessions=[planned("2026-10-08", "Long run", intensity="easy", duration_min=90, distance_km=15, garmin_workout_id=7)],
    )["created_ids"][0]
    result = tools.update_session(store, TODAY, session_id=sid, status="moved", new_date="2026-10-10", note="knee")
    copy = result["moved_copy"]
    assert result["session"]["status"] == "moved"
    assert result["session"]["moved_to"] == {"id": copy["id"], "date": "2026-10-10"}
    assert copy["date"] == "2026-10-10" and copy["status"] == "planned" and copy["moved_from_id"] == sid
    for key in ("type", "description", "intensity", "duration_min", "distance_km", "garmin_workout_id"):
        assert copy[key] == result["session"][key]
    assert result["note"]["session_id"] == sid and result["note"]["date"] == "2026-10-07"

    # one transaction: a failing note insert leaves no status change, no copy, no note
    other = seed(store, "2026-10-09")
    sessions_before, notes_before = count(store, "sessions"), count(store, "notes")

    def boom(*_args, **_kwargs):
        raise sqlite3.IntegrityError("boom")

    monkeypatch.setattr(store_module, "_insert_note", boom)
    with pytest.raises(sqlite3.IntegrityError):
        tools.update_session(store, TODAY, session_id=other, status="moved", new_date="2026-10-12", note="x")
    assert store.get_session(other)["status"] == "planned"
    assert (count(store, "sessions"), count(store, "notes")) == (sessions_before, notes_before)


def test_update_session_move_rules(store):
    planned_id = seed(store, "2026-10-08")
    done_id = seed(store, "2026-10-06", status="done")
    moved_id = seed(store, "2026-10-09", status="moved")
    copy_id = seed(store, "2026-10-12", moved_from_id=moved_id)
    gone_id = seed(store, "2026-10-09", status="superseded")
    before = statuses(store)
    notes_before = count(store, "notes")

    def upd(**kw):
        return err(tools.update_session, store, TODAY, **kw)

    assert upd(session_id=planned_id, status="moved") == "new_date is required for status 'moved'"
    assert upd(session_id=planned_id, status="moved", new_date="2026-10-08") == (
        "new_date must differ from the session date"
    )
    assert upd(session_id=planned_id, status="moved", new_date="2026-10-06") == "new_date must be today or later"
    assert upd(session_id=planned_id, status="done", new_date="2026-10-10") == (
        "new_date is only allowed with status 'moved'"
    )
    assert upd(session_id=done_id, status="moved", new_date="2026-10-10") == "only a planned session can be moved"
    assert upd(session_id=moved_id, status="done") == (
        f"session {moved_id} was already moved to session {copy_id} on 2026-10-12; update that one"
    )
    assert upd(session_id=gone_id, status="done") == (
        f"session {gone_id} was superseded by a later plan and cannot be updated"
    )
    assert upd(session_id=planned_id, status="done", note="   ") == "note must not be empty"
    assert "is not a valid date" in upd(session_id=planned_id, status="moved", new_date="2026-02-30")
    assert statuses(store) == before and count(store, "notes") == notes_before


def test_update_session_move_to_today_is_allowed(store):
    sid = seed(store, "2026-10-06")
    result = tools.update_session(store, TODAY, session_id=sid, status="moved", new_date="2026-10-07")
    assert result["moved_copy"]["date"] == "2026-10-07"


def test_update_session_unknown_id(store):
    assert err(tools.update_session, store, TODAY, session_id=999, status="done") == "session 999 not found"


def test_update_session_back_to_planned_and_same_status(store):
    sid = seed(store, "2026-10-06", status="done")
    assert tools.update_session(store, TODAY, session_id=sid, status="planned")["session"]["status"] == "planned"
    before = (count(store, "sessions"), count(store, "notes"))
    result = tools.update_session(store, TODAY, session_id=sid, status="planned")
    assert result["session"]["status"] == "planned" and "note" not in result and "moved_copy" not in result
    assert (count(store, "sessions"), count(store, "notes")) == before
    result = tools.update_session(store, TODAY, session_id=sid, status="planned", note="again")
    assert result["note"]["text"] == "again" and count(store, "notes") == before[1] + 1


# ------------------------------------------------- goals, constraints, notes


def test_set_goal_create_update_and_clear(store):
    created = tools.set_goal(store, TODAY, title="Half marathon", target_date="2026-10-17", target="sub 1:45")
    goal = created["goal"]
    assert created["created"] is True and goal["status"] == "active" and goal["days_remaining"] == 10
    gid = goal["id"]

    achieved = tools.set_goal(store, TODAY, goal_id=gid, status="achieved")
    assert achieved["created"] is False
    assert achieved["goal"]["status"] == "achieved"
    assert achieved["goal"]["title"] == "Half marathon" and achieved["goal"]["target_date"] == "2026-10-17"

    cleared = tools.set_goal(store, TODAY, goal_id=gid, target_date="")["goal"]
    assert "target_date" not in cleared and "days_remaining" not in cleared and cleared["target"] == "sub 1:45"

    renamed = tools.set_goal(store, TODAY, goal_id=gid, title="New")["goal"]
    assert renamed["title"] == "New" and renamed["status"] == "achieved" and renamed["target"] == "sub 1:45"


def test_set_goal_errors(store):
    gid = tools.set_goal(store, TODAY, title="Keep", target_date="2026-12-01")["goal"]["id"]
    before = store.list_goals()
    assert err(tools.set_goal, store, TODAY) == "title is required to create a goal"
    assert err(tools.set_goal, store, TODAY, title="   ") == "title must not be empty"
    assert err(tools.set_goal, store, TODAY, goal_id=gid, title="") == "title must not be empty"
    assert err(tools.set_goal, store, TODAY, goal_id=999, title="x") == "goal 999 not found"
    assert err(tools.set_goal, store, TODAY, goal_id=gid) == "nothing to update"
    assert "must be YYYY-MM-DD" in err(tools.set_goal, store, TODAY, title="x", target_date="soon")
    assert "is not a valid date" in err(tools.set_goal, store, TODAY, goal_id=gid, target_date="2026-02-30")
    assert store.list_goals() == before


def test_set_goal_create_treats_blank_optionals_as_absent(store):
    goal = tools.set_goal(store, TODAY, title=" Run ", target="  ", target_date="", target_result="")["goal"]
    assert goal == {"id": goal["id"], "title": "Run", "status": "active"}


def test_set_constraint_create_resolve_and_errors(store):
    created = tools.set_constraint(store, TODAY, kind="injury", title="Knee", details="left, no downhill")
    assert created["created"] is True
    assert created["constraint"] == {
        "id": created["constraint"]["id"],
        "kind": "injury",
        "title": "Knee",
        "status": "active",
        "details": "left, no downhill",
    }
    cid = created["constraint"]["id"]
    assert tools.set_constraint(store, TODAY, constraint_id=cid, status="resolved")["constraint"]["status"] == "resolved"
    assert tools.get_coach_context(store, TODAY)["constraints"] == []

    assert err(tools.set_constraint, store, TODAY, title="No kind") == (
        "kind and title are required to create a constraint"
    )
    assert err(tools.set_constraint, store, TODAY, kind="injury") == (
        "kind and title are required to create a constraint"
    )
    assert err(tools.set_constraint, store, TODAY, constraint_id=cid, title="  ") == "title must not be empty"
    assert err(tools.set_constraint, store, TODAY, constraint_id=999, status="resolved") == "constraint 999 not found"
    assert err(tools.set_constraint, store, TODAY, constraint_id=cid) == "nothing to update"
    cleared = tools.set_constraint(store, TODAY, constraint_id=cid, details="")["constraint"]
    assert "details" not in cleared and cleared["title"] == "Knee"
    assert tools.set_constraint(store, TODAY, constraint_id=cid, status="active", kind="limitation")["constraint"][
        "kind"
    ] == "limitation"


def test_add_note_defaults_to_today_and_links_session(store):
    sid = seed(store, "2026-10-08")
    assert tools.add_note(store, TODAY, text="  hello ")["note"] == {"id": 1, "date": "2026-10-07", "text": "hello"}
    explicit = tools.add_note(store, TODAY, text="old", date="2026-09-01")["note"]
    assert explicit["date"] == "2026-09-01"
    linked = tools.add_note(store, TODAY, text="felt heavy", session_id=sid)["note"]
    assert linked["session_id"] == sid
    count_before = count(store, "notes")
    assert err(tools.add_note, store, TODAY, text="x", session_id=999) == "session 999 not found"
    assert "must be YYYY-MM-DD" in err(tools.add_note, store, TODAY, text="x", date="yesterday")
    assert err(tools.add_note, store, TODAY, text="  ") == "text must not be empty"
    assert count(store, "notes") == count_before


# ------------------------------------------------------------- no hard delete


def test_writes_never_reduce_row_counts(store):
    tables = ("goals", "athlete_constraints", "sessions", "notes")

    def snapshot():
        return {t: count(store, t) for t in tables}

    def step(fn, *args, **kwargs):
        before = snapshot()
        fn(store, TODAY, *args, **kwargs)
        after = snapshot()
        assert all(after[t] >= before[t] for t in tables), (fn.__name__, before, after)

    step(tools.set_goal, title="G1")
    step(tools.set_goal, title="G2", target_date="2027-01-01")
    step(tools.set_goal, goal_id=1, status="abandoned")
    step(tools.set_constraint, kind="injury", title="Knee")
    step(tools.set_constraint, constraint_id=1, status="resolved")
    plan = [planned("2026-10-08"), planned("2026-10-09")]
    step(tools.save_plan, week="2026-10-07", sessions=plan)
    step(tools.save_plan, week="2026-10-07", sessions=plan[:1])
    step(tools.save_plan, week="2026-10-07", sessions=[])
    sid = store.sessions_between(date(2026, 10, 5), date(2026, 10, 11), include_superseded=True)[-1]["id"]
    seed_id = seed(store, "2026-10-09")
    step(tools.update_session, session_id=seed_id, status="moved", new_date="2026-10-10", note="n")
    step(tools.update_session, session_id=seed_id + 1, status="done")
    step(tools.add_note, text="n2", session_id=sid)
    assert snapshot()["sessions"] >= 5
