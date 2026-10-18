"""A snapshot section in a shape the coach does not know counts as a section in error (PR 31, finding (c)).

garmin-mcp's shape is pinned by the contract (test_garmin_contract.py); this is the runtime net for a garmin-mcp
deployed without the matching coach-mcp: the section is not stored as NULLs for good, it is logged and repaired.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pytest
from fakes import FakeHistorySource
from garmin_payloads import HISTORY_ROW, HISTORY_SECTIONS, history_snapshot
from history_helpers import local, make_job, run_first, seed_day

from coach_mcp import history
from coach_mcp.history import drifted_sections, recovery_values, section_errors, snapshot_failed

TODAY = date(2026, 10, 7)
START = local(TODAY, 3, 29)
RENAMED = {"renamed_value": 1}


@pytest.mark.parametrize("section", HISTORY_SECTIONS)
@pytest.mark.parametrize(
    ("value", "drifted"),
    [
        (RENAMED, True),  # keys, none of them known
        ({}, False),  # a day without values
        ({"available": False}, False),
        ({"available": False, "renamed_value": 1}, False),
        ({"error": "x", "renamed_value": 1}, False),  # an error section is in error already
        ([1], False),  # not an object: unavailable, as before
    ],
)
def test_a_section_drifts_only_with_keys_and_none_known(section, value, drifted):
    payload = history_snapshot("2026-10-06", **{section: value})
    assert drifted_sections(payload) == ({section} if drifted else frozenset())


@pytest.mark.parametrize("section", HISTORY_SECTIONS)
def test_one_known_key_among_unknown_ones_is_not_drift(section):
    known = sorted(history.SNAPSHOT_KEYS[section])[0]
    payload = history_snapshot("2026-10-06", **{section: {"renamed_value": 1, known: None}})
    assert drifted_sections(payload) == frozenset()


@pytest.mark.parametrize("section", HISTORY_SECTIONS)
def test_a_drifted_section_is_in_error_and_keeps_the_stored_value(section):
    payload = history_snapshot("2026-10-06", **{section: RENAMED})
    assert section_errors(payload) == {section}
    got = recovery_values(payload)
    assert set(got) == set(HISTORY_ROW) - {c for c, s in history._SECTION_OF.items() if s == section}


def test_a_snapshot_with_every_section_drifted_has_failed():
    payload = history_snapshot("2026-10-06", **dict.fromkeys(HISTORY_SECTIONS, RENAMED))
    assert snapshot_failed(payload) is True
    payload["stress"] = {"available": False}
    assert snapshot_failed(payload) is False


def test_every_field_the_history_reads_is_a_known_key():
    for key, columns in history._SECTION_COLUMNS.items():
        assert {field for _, field in columns} <= history.SNAPSHOT_KEYS[key], key
    assert "morning_score" in history.SNAPSHOT_KEYS["readiness"]


@pytest.mark.anyio
@pytest.mark.parametrize(("hrv", "warning"), [(RENAMED, True), ({"available": False}, False)])
async def test_drift_is_stored_as_missing_and_logged_once_per_run(store, caplog, hrv, warning):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")

    def recovery(day: date, index: int) -> tuple[str, dict[str, Any]]:
        return "ok", history_snapshot(day.isoformat(), hrv=hrv)

    job, clock = make_job(store, FakeHistorySource(recovery=recovery), START, history_backfill_days=2)
    await run_first(job, clock)
    rows = {r["date"]: r for r in store.history_rows(None, TODAY)}
    assert {r["recovery_missing"] for r in rows.values()} == ({"hrv"} if warning else {""})
    assert all(r["hrv_last_night_ms"] is None for r in rows.values())
    lines = [r.getMessage() for r in caplog.records if "contract_warning" in r.getMessage()]
    assert lines == (["history status=contract_warning sections=hrv"] if warning else [])


@pytest.mark.anyio
async def test_drift_in_a_repair_answer_is_logged(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    old = date(2026, 9, 20)  # outside the re-fetch days, inside the backfill window: only a repair asks for it
    columns = {k: v for k, v in HISTORY_ROW.items() if history._SECTION_OF[k] != "hrv"}
    seed_day(store, old, fetched_on=date(2026, 9, 23), missing="hrv", **columns)  # D+3: no regular re-fetch

    def recovery(day: date, index: int) -> tuple[str, dict[str, Any]]:
        hrv = RENAMED if day == old else {"last_night_avg_ms": 55, "weekly_avg_ms": 57, "status": "BALANCED"}
        return "ok", history_snapshot(day.isoformat(), hrv=hrv)

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=30)
    await run_first(job, clock)
    assert ("get_recovery_snapshot", {"date": old.isoformat()}) in source.calls
    row = {r["date"]: r for r in store.history_rows(None, TODAY)}[old.isoformat()]
    assert row["recovery_missing"] == "hrv"
    lines = [r.getMessage() for r in caplog.records if "contract_warning" in r.getMessage()]
    assert lines == ["history status=contract_warning sections=hrv"]


@pytest.mark.anyio
async def test_a_snapshot_with_every_section_drifted_is_logged(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")

    def recovery(day: date, index: int) -> tuple[str, dict[str, Any]]:
        return "ok", history_snapshot(day.isoformat(), **dict.fromkeys(HISTORY_SECTIONS, RENAMED))

    job, clock = make_job(store, FakeHistorySource(recovery=recovery), START, history_backfill_days=2)
    await run_first(job, clock)
    lines = [r.getMessage() for r in caplog.records if "contract_warning" in r.getMessage()]
    assert lines == ["history status=contract_warning sections=" + history.missing_text(history.ALL_SECTIONS)]
    assert all(r["hrv_last_night_ms"] is None for r in store.history_rows(None, TODAY))  # nothing read: no values
