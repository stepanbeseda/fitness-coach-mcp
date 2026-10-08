"""SQLite store of the coach memory: schema, migrations and all SQL.

One connection per operation, default rollback journal (no WAL), so a stopped
container leaves a single self-contained database file. Every write runs in one
transaction. Nothing in this module deletes rows.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# Entry i migrates the schema from version i to i + 1. Existing entries are never
# edited; a later PR appends new ones.
MIGRATIONS: tuple[str, ...] = (
    """
CREATE TABLE goals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  target        TEXT CHECK (target IS NULL OR length(target) <= 200),
  target_date   TEXT,
  target_result TEXT CHECK (target_result IS NULL OR length(target_result) <= 200),
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','achieved','abandoned')),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE athlete_constraints (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL CHECK (kind IN ('injury','limitation','availability','equipment','other')),
  title      TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  details    TEXT CHECK (details IS NULL OR length(details) <= 2000),
  status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','resolved')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE sessions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  date              TEXT NOT NULL,
  type              TEXT NOT NULL CHECK (type IN ('running','cycling','swimming','strength','mobility','other')),
  description       TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
  intensity         TEXT CHECK (intensity IS NULL OR length(intensity) <= 200),
  duration_min      INTEGER CHECK (duration_min IS NULL OR duration_min BETWEEN 1 AND 1440),
  distance_km       REAL CHECK (distance_km IS NULL OR (distance_km > 0 AND distance_km <= 1000)),
  garmin_workout_id TEXT CHECK (garmin_workout_id IS NULL OR length(garmin_workout_id) BETWEEN 1 AND 64),
  hevy_routine_id   TEXT CHECK (hevy_routine_id IS NULL OR length(hevy_routine_id) BETWEEN 1 AND 64),
  status            TEXT NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned','done','skipped','moved','superseded')),
  moved_from_id     INTEGER REFERENCES sessions(id),
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX sessions_date ON sessions(date);
CREATE INDEX sessions_moved_from ON sessions(moved_from_id);
CREATE TABLE notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  date       TEXT NOT NULL,
  text       TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
  session_id INTEGER REFERENCES sessions(id),
  created_at TEXT NOT NULL
);
CREATE INDEX notes_date ON notes(date, id);
""",
    """
CREATE TABLE briefing_runs (
  date    TEXT PRIMARY KEY,
  verdict TEXT NOT NULL CHECK (verdict IN ('green','yellow','red')),
  sent_at TEXT NOT NULL
);
CREATE TABLE alert_state (
  key          TEXT PRIMARY KEY
               CHECK (key IN ('hrv_below_baseline','resting_hr_elevated','acwr_out_of_range','missed_workout','garmin_auth')),
  value        INTEGER NOT NULL CHECK (value IN (0, 1)),
  evaluated_on TEXT NOT NULL,
  fired_on     TEXT
);
""",
    """
CREATE TABLE history_days (
  date                  TEXT PRIMARY KEY,
  recovery_fetched_on   TEXT,
  recovery_failures     INTEGER NOT NULL DEFAULT 0 CHECK (recovery_failures >= 0),
  activities_fetched_on TEXT,
  activities_failures   INTEGER NOT NULL DEFAULT 0 CHECK (activities_failures >= 0),
  readiness             REAL,
  hrv_last_night_ms     REAL,
  hrv_weekly_avg_ms     REAL,
  hrv_status            TEXT CHECK (hrv_status IS NULL OR length(hrv_status) <= 40),
  sleep_duration_h      REAL,
  sleep_score           REAL,
  resting_hr            REAL,
  body_battery_high     REAL,
  body_battery_low      REAL,
  stress_avg            REAL,
  acute_load            REAL,
  chronic_load          REAL,
  acwr_ratio            REAL,
  vo2max_running        REAL,
  vo2max_cycling        REAL,
  activities            TEXT,
  updated_at            TEXT NOT NULL
);
""",
    # Plan reconciliation (PR 13): the alert key missed_planned_session (SQLite cannot alter a CHECK, so
    # alert_state is rebuilt with all its rows) and the nightly suggestion per session.
    """
CREATE TABLE alert_state_new (
  key          TEXT PRIMARY KEY
               CHECK (key IN ('hrv_below_baseline','resting_hr_elevated','acwr_out_of_range','missed_workout','garmin_auth','missed_planned_session')),
  value        INTEGER NOT NULL CHECK (value IN (0, 1)),
  evaluated_on TEXT NOT NULL,
  fired_on     TEXT
);
INSERT INTO alert_state_new (key, value, evaluated_on, fired_on)
  SELECT key, value, evaluated_on, fired_on FROM alert_state;
DROP TABLE alert_state;
ALTER TABLE alert_state_new RENAME TO alert_state;
CREATE TABLE session_suggestions (
  session_id   INTEGER PRIMARY KEY REFERENCES sessions(id),
  result       TEXT NOT NULL CHECK (result IN ('matched','no_activity')),
  final        INTEGER NOT NULL CHECK (final IN (0, 1)),
  evidence     TEXT CHECK (evidence IS NULL OR (json_valid(evidence) AND length(evidence) <= 2000)),
  evaluated_on TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  CHECK ((result = 'matched') = (evidence IS NOT NULL))
);
""",
)

SCHEMA_VERSION = len(MIGRATIONS)

# Daily values of history_days that the recovery snapshot fills (the activity part has its own JSON column).
HISTORY_COLUMNS: tuple[str, ...] = (
    "readiness",
    "hrv_last_night_ms",
    "hrv_weekly_avg_ms",
    "hrv_status",
    "sleep_duration_h",
    "sleep_score",
    "resting_hr",
    "body_battery_high",
    "body_battery_low",
    "stress_avg",
    "acute_load",
    "chronic_load",
    "acwr_ratio",
    "vo2max_running",
    "vo2max_cycling",
)
HISTORY_PARTS = ("recovery", "activities")

GOAL_FIELDS = frozenset({"title", "target", "target_date", "target_result", "status"})
CONSTRAINT_FIELDS = frozenset({"kind", "title", "details", "status"})
SESSION_FIELDS = (
    "date",
    "type",
    "description",
    "intensity",
    "duration_min",
    "distance_km",
    "garmin_workout_id",
    "hevy_routine_id",
)


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer coach-mcp than this one."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


def _check_fields(fields: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown column(s): {', '.join(sorted(unknown))}")


def _insert_note(conn: sqlite3.Connection, day: date, text: str, session_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO notes (date, text, session_id, created_at) VALUES (?, ?, ?, ?)",
        (day.isoformat(), text, session_id, _now()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _insert_session(
    conn: sqlite3.Connection, values: Mapping[str, Any], status: str, moved_from_id: int | None
) -> int:
    now = _now()
    cur = conn.execute(
        f"INSERT INTO sessions ({', '.join(SESSION_FIELDS)}, status, moved_from_id, created_at, updated_at) "
        f"VALUES ({', '.join('?' for _ in SESSION_FIELDS)}, ?, ?, ?, ?)",
        (*(_iso(values.get(f)) for f in SESSION_FIELDS), status, moved_from_id, now, now),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._migrate()

    # ------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(self._connect()) as conn:
            yield conn

    def _migrate(self) -> None:
        with self._conn() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            latest = len(MIGRATIONS)
            if version > latest:
                raise SchemaTooNewError(
                    f"database schema version {version} is newer than this server supports "
                    f"({latest}); deploy the newer coach-mcp or restore a backup"
                )
            for target in range(version + 1, latest + 1):
                script = MIGRATIONS[target - 1]
                try:
                    conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")
                except sqlite3.Error:
                    if conn.in_transaction:
                        conn.rollback()
                    raise

    def check(self) -> int:
        """Schema version after a trivial read (used by /healthz); raises sqlite3.Error."""
        with self._conn() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.execute("SELECT 1 FROM goals LIMIT 1").fetchall()
            return int(version)

    # --------------------------------------------------------------- goals

    def insert_goal(self, fields: Mapping[str, Any]) -> int:
        _check_fields(fields, GOAL_FIELDS)
        values = {k: _iso(v) for k, v in fields.items()}
        now = _now()
        columns = [*values, "created_at", "updated_at"]
        with self._conn() as conn, conn:
            cur = conn.execute(
                f"INSERT INTO goals ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                (*values.values(), now, now),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def update_goal(self, goal_id: int, fields: Mapping[str, Any]) -> bool:
        _check_fields(fields, GOAL_FIELDS)
        return self._update("goals", goal_id, {k: _iso(v) for k, v in fields.items()})

    def get_goal(self, goal_id: int) -> dict[str, Any] | None:
        return self._get("goals", goal_id)

    def list_goals(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM goals"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY target_date IS NULL, target_date, id"
        return self._all(sql, params)

    # --------------------------------------------------------- constraints

    def insert_constraint(self, fields: Mapping[str, Any]) -> int:
        _check_fields(fields, CONSTRAINT_FIELDS)
        now = _now()
        columns = [*fields, "created_at", "updated_at"]
        with self._conn() as conn, conn:
            cur = conn.execute(
                f"INSERT INTO athlete_constraints ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                (*fields.values(), now, now),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def update_constraint(self, constraint_id: int, fields: Mapping[str, Any]) -> bool:
        _check_fields(fields, CONSTRAINT_FIELDS)
        return self._update("athlete_constraints", constraint_id, dict(fields))

    def get_constraint(self, constraint_id: int) -> dict[str, Any] | None:
        return self._get("athlete_constraints", constraint_id)

    def list_constraints(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM athlete_constraints"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        return self._all(sql + " ORDER BY id", params)

    # ------------------------------------------------------------ sessions

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        return self._get("sessions", session_id)

    def sessions_between(self, start: date, end: date, include_superseded: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions WHERE date BETWEEN ? AND ?"
        if not include_superseded:
            sql += " AND status != 'superseded'"
        return self._all(sql + " ORDER BY date, id", (start.isoformat(), end.isoformat()))

    def replace_planned(
        self, start: date, end: date, from_date: date, new_sessions: Sequence[Mapping[str, Any]]
    ) -> tuple[int, list[int]]:
        """Supersede planned sessions in [start, end] dated from_date or later, insert the new ones."""
        with self._conn() as conn, conn:
            cur = conn.execute(
                "UPDATE sessions SET status = 'superseded', updated_at = ? "
                "WHERE status = 'planned' AND date BETWEEN ? AND ? AND date >= ?",
                (_now(), start.isoformat(), end.isoformat(), from_date.isoformat()),
            )
            superseded = cur.rowcount
            ids = [_insert_session(conn, s, "planned", None) for s in new_sessions]
        return superseded, ids

    def moved_to(self, ids: Sequence[int]) -> dict[int, tuple[int, str]]:
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = self._all(
            f"SELECT id, date, moved_from_id FROM sessions WHERE moved_from_id IN ({marks}) ORDER BY id",
            tuple(ids),
        )
        return {row["moved_from_id"]: (row["id"], row["date"]) for row in rows}

    def upsert_suggestions(self, rows: Sequence[tuple[int, str, bool, str | None]], evaluated_on: date) -> None:
        """Store (session_id, result, final, evidence JSON) for each row in one transaction; nothing is deleted."""
        now = _now()
        with self._conn() as conn, conn:
            for session_id, result, final, evidence in rows:
                conn.execute(
                    "INSERT INTO session_suggestions (session_id, result, final, evidence, evaluated_on, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET result = excluded.result, final = excluded.final, "
                    "evidence = excluded.evidence, evaluated_on = excluded.evaluated_on, "
                    "updated_at = excluded.updated_at",
                    (session_id, result, int(final), evidence, evaluated_on.isoformat(), now),
                )

    def suggestions_for(self, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """session_id -> {session_id, result, final, evidence, evaluated_on} for the ids that have a row."""
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = self._all(
            f"SELECT session_id, result, final, evidence, evaluated_on FROM session_suggestions "
            f"WHERE session_id IN ({marks})",
            tuple(ids),
        )
        return {row["session_id"]: row for row in rows}

    def update_session_status(
        self, session_id: int, status: str, new_date: date | None, note: tuple[date, str] | None
    ) -> tuple[int | None, int | None]:
        """Set the status; for 'moved' also insert the planned copy; optionally add a note.

        One transaction. Returns (copy_id, note_id).
        """
        with self._conn() as conn, conn:
            cur = conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), session_id)
            )
            if cur.rowcount == 0:
                raise LookupError("session not found")
            copy_id = None
            if status == "moved":
                assert new_date is not None
                original = dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())
                original["date"] = new_date.isoformat()
                copy_id = _insert_session(conn, original, "planned", session_id)
            note_id = _insert_note(conn, note[0], note[1], session_id) if note is not None else None
        return copy_id, note_id

    # ------------------------------------------------------------ briefing

    def briefing_sent(self, day: date) -> bool:
        return bool(self._all("SELECT 1 FROM briefing_runs WHERE date = ?", (day.isoformat(),)))

    def alert_states(self) -> dict[str, dict[str, Any]]:
        """Alert key -> ``{value, evaluated_on, fired_on}`` for every alert evaluated so far."""
        rows = self._all("SELECT key, value, evaluated_on, fired_on FROM alert_state")
        return {row["key"]: {k: v for k, v in row.items() if k != "key"} for row in rows}

    def record_briefing_sent(self, day: date, verdict: str, alerts: Mapping[str, tuple[bool, bool]]) -> None:
        """Record a successful scheduled push and the alert states it evaluated, in one transaction.

        ``alerts`` maps a key to ``(holds, new)`` and contains only conditions whose value is known.
        A second call for the same day raises ``sqlite3.IntegrityError``.
        """
        with self._conn() as conn, conn:
            conn.execute(
                "INSERT INTO briefing_runs (date, verdict, sent_at) VALUES (?, ?, ?)",
                (day.isoformat(), verdict, _now()),
            )
            for key, (holds, new) in alerts.items():
                conn.execute(
                    "INSERT INTO alert_state (key, value, evaluated_on, fired_on) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, evaluated_on = excluded.evaluated_on, "
                    "fired_on = COALESCE(excluded.fired_on, alert_state.fired_on)",
                    (key, int(holds), day.isoformat(), day.isoformat() if new else None),
                )

    # ------------------------------------------------------------- history

    def history_rows(self, start: date | None, end: date) -> list[dict[str, Any]]:
        """All columns of the days up to ``end`` (and from ``start`` when given), oldest first."""
        sql, params = "SELECT * FROM history_days WHERE date <= ?", [end.isoformat()]
        if start is not None:
            sql += " AND date >= ?"
            params.append(start.isoformat())
        return self._all(sql + " ORDER BY date", tuple(params))

    def upsert_history_recovery(self, day: date, values: Mapping[str, Any], fetched_on: date) -> None:
        """Store the daily values of ``day``; columns not in ``values`` keep their stored value."""
        _check_fields(values, frozenset(HISTORY_COLUMNS))
        columns = [*values, "recovery_fetched_on", "recovery_failures", "updated_at"]
        sets = [f"{c} = excluded.{c}" for c in columns]
        with self._conn() as conn, conn:
            conn.execute(
                f"INSERT INTO history_days (date, {', '.join(columns)}) "
                f"VALUES (?, {', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(date) DO UPDATE SET {', '.join(sets)}",
                (day.isoformat(), *values.values(), fetched_on.isoformat(), 0, _now()),
            )

    def upsert_history_activities(self, totals: Mapping[date, str], fetched_on: date) -> None:
        """Store the activity totals (JSON text) of every day in ``totals`` in one transaction."""
        now = _now()
        with self._conn() as conn, conn:
            for day, text in totals.items():
                conn.execute(
                    "INSERT INTO history_days (date, activities, activities_fetched_on, activities_failures, "
                    "updated_at) VALUES (?, ?, ?, 0, ?) "
                    "ON CONFLICT(date) DO UPDATE SET activities = excluded.activities, "
                    "activities_fetched_on = excluded.activities_fetched_on, activities_failures = 0, "
                    "updated_at = excluded.updated_at",
                    (day.isoformat(), text, fetched_on.isoformat(), now),
                )

    def record_history_failure(self, days: Sequence[date], part: str) -> None:
        """Count one item-level error of ``part`` for each of ``days`` (reset by the next success)."""
        if part not in HISTORY_PARTS:
            raise ValueError(f"unknown history part: {part!r}")
        column = f"{part}_failures"
        now = _now()
        with self._conn() as conn, conn:
            for day in days:
                conn.execute(
                    f"INSERT INTO history_days (date, {column}, updated_at) VALUES (?, 1, ?) "
                    f"ON CONFLICT(date) DO UPDATE SET {column} = {column} + 1, updated_at = excluded.updated_at",
                    (day.isoformat(), now),
                )

    # --------------------------------------------------------------- notes

    def insert_note(self, day: date, text: str, session_id: int | None = None) -> int:
        with self._conn() as conn, conn:
            return _insert_note(conn, day, text, session_id)

    def latest_notes(self, limit: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM notes ORDER BY date DESC, id DESC LIMIT ?", (limit,))

    def get_note(self, note_id: int) -> dict[str, Any] | None:
        return self._get("notes", note_id)

    # ------------------------------------------------------------- helpers

    def _get(self, table: str, row_id: int) -> dict[str, Any] | None:
        rows = self._all(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        return rows[0] if rows else None

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def _update(self, table: str, row_id: int, fields: Mapping[str, Any]) -> bool:
        columns = [*fields, "updated_at"]
        with self._conn() as conn, conn:
            cur = conn.execute(
                f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in columns)} WHERE id = ?",
                (*fields.values(), _now(), row_id),
            )
            return cur.rowcount > 0
