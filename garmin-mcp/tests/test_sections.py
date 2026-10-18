"""The section rules of plan section 1, tested once against `sections.fetch_sections` with FakeGarmin (PR 25)."""

import logging
import threading

import pytest
from garminconnect import GarminConnectNotFoundError
from mcp.server.mcpserver.exceptions import ToolError

from conftest import AUTH_ERROR, CONN_ERROR
from garmin_coach_mcp.garmin_client import NOT_LOGGED_IN, SESSION_EXPIRED
from garmin_coach_mcp.sections import UNAVAILABLE, Call, Section, fetch_sections

DAY = "2026-09-30"
NOT_FOUND = GarminConnectNotFoundError("API Error 404")
FAILED = "Garmin Connect request failed: API Error 500 - boom"


def sleep_call(**flags):
    return Call(lambda c: c.get_sleep_data(DAY), **flags)


def summary_call(**flags):
    return Call(lambda c: c.get_user_summary(DAY), **flags)


def seen(raw):
    """A shaper that shows what it was given."""
    return {"payload": raw}


def boom(raw, *extra):
    raise KeyError("calendarDate")


def names(fake):
    return [n for n, _ in fake.calls if n != "login"]


# S1 -------------------------------------------------------------- shaped sections, order, shared call
@pytest.mark.anyio
async def test_s1_sections_are_shaped_in_declaration_order_and_share_a_call(service, fake):
    fake.responses["get_user_summary"] = {"restingHeartRate": 49}
    out = await fetch_sections(
        service,
        {"summary": summary_call(), "sleep": sleep_call()},
        {
            "sleep": Section("sleep", lambda raw: "sleep shaped"),
            "resting_hr": Section("summary", lambda raw: raw["restingHeartRate"]),
            "summary_keys": Section("summary", lambda raw: sorted(raw)),
        },
    )
    assert out == {"sleep": "sleep shaped", "resting_hr": 49, "summary_keys": ["restingHeartRate"]}
    assert list(out) == ["sleep", "resting_hr", "summary_keys"]
    assert sorted(names(fake)) == ["get_sleep_data", "get_user_summary"]  # one library call per Call


# S2 ------------------------------------------------------------------------------ 404 as empty
@pytest.mark.anyio
async def test_s2_404_is_empty_only_when_declared(service, fake):
    fake.fail["get_sleep_data"] = NOT_FOUND
    out = await fetch_sections(
        service,
        {"empty": sleep_call(missing_is_empty=True), "strict": summary_call()},
        {"empty": Section("empty", seen), "strict": Section("strict", seen)},
    )
    assert out["empty"] == {"payload": None}
    assert out["strict"] == {"payload": fake.responses["get_user_summary"]}
    fake.fail["get_user_summary"] = NOT_FOUND
    out = await fetch_sections(service, {"strict": summary_call()}, {"strict": Section("strict", seen)})
    assert out == {"strict": {"error": "Not found in Garmin Connect (API Error 404).", "error_kind": "not_found"}}


@pytest.mark.anyio
async def test_s2_only_a_404_is_swallowed_by_missing_is_empty(service, fake):
    fake.fail["get_sleep_data"] = CONN_ERROR
    out = await fetch_sections(
        service, {"sleep": sleep_call(missing_is_empty=True)}, {"sleep": Section("sleep", seen)}
    )
    assert out == {"sleep": {"error": FAILED, "error_kind": "garmin_unavailable"}}


# S3 ------------------------------------------------------------------------------ section in error
@pytest.mark.anyio
async def test_s3_failed_call_is_the_section_error_and_the_others_survive(service, fake):
    fake.fail["get_sleep_data"] = CONN_ERROR
    out = await fetch_sections(
        service,
        {"sleep": sleep_call(), "summary": summary_call()},
        {
            "sleep": Section("sleep", seen),
            "sleep_laps": Section("sleep", seen, if_error=lambda message: {"laps_error": message}),
            "omitted": Section("sleep", seen, if_error=lambda message: None),
            "summary": Section("summary", lambda raw: "ok"),
        },
    )
    assert out == {
        "sleep": {"error": FAILED, "error_kind": "garmin_unavailable"},
        "sleep_laps": {"laps_error": FAILED},
        "omitted": None,
        "summary": "ok",
    }


# S4 ---------------------------------------------------------------------------- shaper exception
@pytest.mark.anyio
async def test_s4_shaper_exception_degrades_the_section_and_logs_no_data(service, fake, caplog):
    fake.responses["get_sleep_data"] = {"secret-marker": 4242}
    caplog.set_level(logging.DEBUG)
    out = await fetch_sections(
        service,
        {"sleep": sleep_call(required=True), "summary": summary_call()},
        {
            "sleep": Section("sleep", boom),
            "custom": Section("sleep", boom, if_error=lambda message: {"laps_error": message}),
            "summary": Section("summary", lambda raw: "ok"),
        },
    )
    assert out == {
        "sleep": {"error": "unexpected Garmin response for sleep", "error_kind": "unexpected_answer"},
        "custom": {"laps_error": "unexpected Garmin response for sleep"},
        "summary": "ok",
    }
    lines = [(r.name, r.levelno, r.getMessage()) for r in caplog.records if r.name.startswith("garmin_coach_mcp")]
    assert lines == [("garmin_coach_mcp.sections", logging.WARNING, "could not shape sleep: KeyError")] * 2
    assert "secret-marker" not in caplog.text and "4242" not in caplog.text and "calendarDate" not in caplog.text


# S5 -------------------------------------------------------------------------- authentication abort
@pytest.mark.anyio
async def test_s5_authentication_failure_aborts_the_tool(service, fake):
    fake.fail["get_user_summary"] = AUTH_ERROR
    with pytest.raises(ToolError) as caught:
        await fetch_sections(
            service,
            {"sleep": sleep_call(), "summary": summary_call()},
            {"sleep": Section("sleep", seen), "summary": Section("summary", seen)},
        )
    assert str(caught.value) == SESSION_EXPIRED


@pytest.mark.anyio
async def test_s5_not_logged_in_aborts_before_any_call(service, fake, tokens_dir):
    (tokens_dir / "garmin_tokens.json").unlink()
    with pytest.raises(ToolError) as caught:
        await fetch_sections(service, {"sleep": sleep_call()}, {"sleep": Section("sleep", seen)})
    assert str(caught.value) == NOT_LOGGED_IN
    assert fake.calls == []


# S6 ---------------------------------------------------------------------------- required section
@pytest.mark.anyio
async def test_s6_only_a_failed_required_call_fails_the_tool(service, fake):
    fake.fail["get_sleep_data"] = CONN_ERROR
    with pytest.raises(ToolError) as caught:
        await fetch_sections(
            service,
            {"sleep": sleep_call(required=True), "summary": summary_call()},
            {"sleep": Section("sleep", seen), "summary": Section("summary", seen)},
        )
    assert str(caught.value) == FAILED
    fake.fail["get_sleep_data"] = NOT_FOUND  # a declared 404 is empty, not a failure
    out = await fetch_sections(
        service, {"sleep": sleep_call(required=True, missing_is_empty=True)}, {"sleep": Section("sleep", seen)}
    )
    assert out == {"sleep": {"payload": None}}


# S7 ------------------------------------------------------------------------------------- uses
@pytest.mark.anyio
async def test_s7_uses_passes_further_payloads_and_none_for_a_failed_one(service, fake):
    out = await fetch_sections(
        service,
        {"sleep": sleep_call(), "summary": summary_call()},
        {"pair": Section("sleep", lambda raw, summary: (raw is not None, summary is not None), uses=("summary",))},
    )
    assert out == {"pair": (True, True)}
    fake.fail["get_user_summary"] = CONN_ERROR
    out = await fetch_sections(
        service,
        {"sleep": sleep_call(), "summary": summary_call()},
        {"pair": Section("sleep", lambda raw, summary: (raw is not None, summary), uses=("summary",))},
    )
    assert out == {"pair": (True, None)}


@pytest.mark.anyio
async def test_s7_several_uses_arrive_in_declaration_order(service, fake):
    out = await fetch_sections(
        service,
        {"sleep": sleep_call(), "summary": summary_call()},
        {
            "order": Section(
                "sleep",
                lambda raw, first, second: (first, second),
                uses=("summary", "sleep"),
            )
        },
    )
    assert out == {"order": (fake.responses["get_user_summary"], fake.responses["get_sleep_data"])}


# S8 ------------------------------------------------------------------------------- concurrency
@pytest.mark.anyio
async def test_s8_calls_run_concurrently(service, fake):
    barrier = threading.Barrier(2, timeout=5)

    def waiting(name):
        def fetch(client):
            barrier.wait()  # a sequential run breaks the barrier after 5 s
            return getattr(client, name)(DAY)

        return fetch

    out = await fetch_sections(
        service,
        {"sleep": Call(waiting("get_sleep_data")), "summary": Call(waiting("get_user_summary"))},
        {"sleep": Section("sleep", lambda raw: "a"), "summary": Section("summary", lambda raw: "b")},
    )
    assert out == {"sleep": "a", "summary": "b"}


# S9 ------------------------------------------------------------------------------ declaration
@pytest.mark.anyio
async def test_s9_undeclared_call_is_a_programming_error_before_any_call(service, fake):
    with pytest.raises(ValueError, match="undeclared"):
        await fetch_sections(service, {"sleep": sleep_call()}, {"x": Section("sleep", seen, uses=("nope",))})
    assert fake.calls == []


@pytest.mark.anyio
async def test_s9_undeclared_section_call_is_a_programming_error_before_any_call(service, fake):
    with pytest.raises(ValueError, match="undeclared"):
        await fetch_sections(service, {"sleep": sleep_call()}, {"x": Section("nope", seen)})
    assert fake.calls == []


def test_unavailable_value():
    assert UNAVAILABLE == {"available": False}
