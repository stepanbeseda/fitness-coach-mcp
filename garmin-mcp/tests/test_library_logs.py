"""What garminconnect itself logs for the calls PR 19 added (plan section 1: logs carry no arguments or health
values). Real library, stub inner client, no network."""

import logging

import pytest
from garminconnect import Garmin, GarminConnectConnectionError, GarminConnectNotFoundError

from garmin_coach_mcp.__main__ import configure_logging

ACTIVITY_ID = 20003
WEATHER_PATH = f"/activity-service/activity/{ACTIVITY_ID}/weather"
SETS_PATH = f"/activity-service/activity/{ACTIVITY_ID}/exerciseSets"
TOLERANCE_PATH = "/metrics-service/metrics/runningtolerance/stats"
RACE_PATH = "/metrics-service/metrics/racepredictions/daily/athlete"

# (call, path, logs_debug). The DEBUG line is the positive control that the logger is listening; a call without
# one (the daily race predictions) gets its control from the 503 test, which captures its ERROR line through the
# same caplog set-up.
CALLS = {
    "weather": (lambda g: g.get_activity_weather(ACTIVITY_ID), WEATHER_PATH, True),
    "exercise_sets": (lambda g: g.get_activity_exercise_sets(ACTIVITY_ID), SETS_PATH, True),
    "running_tolerance": (
        lambda g: g.get_running_tolerance("2026-08-06", "2026-09-30", "weekly"), TOLERANCE_PATH, True),
    "race_predictions": (lambda g: g.get_race_predictions("2026-09-01", "2026-09-30", "daily"), RACE_PATH, False),
}


class Stub:
    """Stands in for the library's inner client: records the path, raises or answers {}."""

    def __init__(self, exc: Exception | None) -> None:
        self.exc = exc
        self.paths: list[str] = []

    def connectapi(self, path: str, **kwargs):
        self.paths.append(path)
        if self.exc is not None:
            raise self.exc
        return {}


def _garmin(exc: Exception | None) -> tuple[Garmin, Stub]:
    garmin = Garmin(retry_attempts=0)
    garmin.display_name = "athlete"  # else the library first asks /userprofile-service/socialProfile
    stub = Stub(exc)
    garmin.client = stub
    return garmin, stub


@pytest.mark.parametrize("name", list(CALLS))
@pytest.mark.parametrize("exc", [None, GarminConnectNotFoundError("API Error 404")], ids=["success", "404"])
def test_new_calls_log_nothing_on_success_or_404(caplog, name, exc):
    call, path, logs_debug = CALLS[name]
    garmin, stub = _garmin(exc)
    caplog.set_level(logging.DEBUG, logger="garminconnect")
    if exc is None:
        assert call(garmin) == {}
    else:
        with pytest.raises(GarminConnectNotFoundError):
            call(garmin)
    assert stub.paths == [path]
    records = [r for r in caplog.records if r.name.startswith("garminconnect")]
    assert any(r.levelno == logging.DEBUG for r in records) is logs_debug  # control: the logger is listening
    assert [r for r in records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("name", list(CALLS))
def test_new_calls_log_path_on_server_error(caplog, name):
    call, path, _ = CALLS[name]
    garmin, _ = _garmin(GarminConnectConnectionError("API Error 503 - x"))
    caplog.set_level(logging.DEBUG, logger="garminconnect")
    with pytest.raises(GarminConnectConnectionError):
        call(garmin)
    loud = [r for r in caplog.records if r.name.startswith("garminconnect") and r.levelno >= logging.WARNING]
    assert len(loud) == 1 and loud[0].levelno == logging.ERROR
    assert loud[0].getMessage() == f"API call failed for path '{path}' (status=503)"


def test_configure_logging_keeps_garminconnect_above_debug():
    names = ("garminconnect", "mcp", "httpx", "httpx2", "httpcore", "httpcore2", "sse_starlette")
    before = {n: logging.getLogger(n).level for n in names}
    lg = logging.getLogger("garminconnect")
    try:
        lg.setLevel(logging.NOTSET)
        configure_logging("DEBUG")
        assert lg.level == logging.WARNING  # its own level; the effective level passes vacuously under pytest
    finally:
        for n, level in before.items():
            logging.getLogger(n).setLevel(level)
