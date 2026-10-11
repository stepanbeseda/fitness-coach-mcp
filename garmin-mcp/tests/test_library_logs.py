"""What garminconnect itself logs for the calls PR 19 and PR 17 added (plan section 1: logs carry no arguments or
health values). Real library, stub inner client, no network. The upload of a workout (PR 07) goes through the
library's real inner client with a fake HTTP session, because the stub would hide what it logs and re-sends."""

import json
import logging

import pytest
from garminconnect import Garmin, GarminConnectConnectionError, GarminConnectNotFoundError

import fixtures as fx
from garmin_coach_mcp.__main__ import configure_logging

ACTIVITY_ID = 20003
WEATHER_PATH = f"/activity-service/activity/{ACTIVITY_ID}/weather"
SETS_PATH = f"/activity-service/activity/{ACTIVITY_ID}/exerciseSets"
TOLERANCE_PATH = "/metrics-service/metrics/runningtolerance/stats"
RACE_PATH = "/metrics-service/metrics/racepredictions/daily/athlete"
DEVICE_LAST_USED_PATH = "/device-service/deviceservice/mylastused"
DEVICES_PATH = "/device-service/deviceregistration/devices"

# (call, path, logs_debug). The DEBUG line is the positive control that the logger is listening; a call without
# one (the daily race predictions) gets its control from the 503 test, which captures its ERROR line through the
# same caplog set-up.
CALLS = {
    "weather": (lambda g: g.get_activity_weather(ACTIVITY_ID), WEATHER_PATH, True),
    "exercise_sets": (lambda g: g.get_activity_exercise_sets(ACTIVITY_ID), SETS_PATH, True),
    "running_tolerance": (
        lambda g: g.get_running_tolerance("2026-08-06", "2026-09-30", "weekly"), TOLERANCE_PATH, True),
    "race_predictions": (lambda g: g.get_race_predictions("2026-09-01", "2026-09-30", "daily"), RACE_PATH, False),
    "device_last_used": (lambda g: g.get_device_last_used(), DEVICE_LAST_USED_PATH, True),
    "devices": (lambda g: g.get_devices(), DEVICES_PATH, True),
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


# ----------------------------------------------------- upload_workout through the real inner client (PR 07)

UPLOAD_URL = "https://connectapi.garmin.com/workout-service/workout"
ERROR_BODY = '{"message": "Invalid step", "echo": "BENCH_PRESS body-marker"}'


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.text = '{"workoutId": 424242}' if status == 200 else ERROR_BODY
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Stands in for the library's HTTP session: records every request, answers the given statuses in turn."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.requests: list[tuple[str, str, object]] = []

    def request(self, method, url, headers=None, **kwargs):
        self.requests.append((method, url, kwargs.get("json")))
        return FakeResponse(self.statuses.pop(0))


def _real_garmin(statuses: list[int]) -> tuple[Garmin, FakeSession, list[int]]:
    garmin = Garmin(retry_attempts=2)
    garmin.client.di_token = "fake-token"  # not a JWT, so the library does not think it has expired
    session = FakeSession(statuses)
    garmin.client._api_session = session
    refreshed: list[int] = []
    garmin.client._refresh_di_token = lambda: refreshed.append(1)  # the real one calls Garmin's token endpoint
    return garmin, session, refreshed


def _library_records(caplog):
    return [r for r in caplog.records if r.name.startswith("garminconnect")]


@pytest.mark.parametrize("statuses", [[200], [400], [500], [401, 200]], ids=["200", "400", "500", "401-then-200"])
def test_upload_workout_requests_and_library_records(caplog, statuses):
    garmin, session, refreshed = _real_garmin(statuses)
    caplog.set_level(logging.DEBUG, logger="garminconnect")
    if statuses[-1] == 200:
        assert garmin.upload_workout(fx.STRENGTH_UPLOAD) == {"workoutId": 424242}
    else:
        with pytest.raises(GarminConnectConnectionError, match=f"API Error {statuses[-1]} - Invalid step"):
            garmin.upload_workout(fx.STRENGTH_UPLOAD)
    # one POST, also after a 500 with retry_attempts=2; only a 401 makes the library refresh the token and send the
    # same body once more
    assert session.requests == [("POST", UPLOAD_URL, fx.STRENGTH_UPLOAD)] * len(statuses)
    assert len(refreshed) == statuses.count(401)
    records = _library_records(caplog)
    assert [r for r in records if r.levelno >= logging.WARNING] == []
    assert (records[0].name, records[0].levelno, records[0].getMessage()) == (
        "garminconnect", logging.DEBUG, "Uploading workout using /workout-service/workout")
    bodies = [r for r in records[1:] if r.name == "garminconnect.client"]
    if statuses[-1] == 200:
        assert bodies == []
    else:  # the positive control of the next test: at DEBUG the library does write Garmin's answer
        assert len(bodies) == 1 and bodies[0].levelno == logging.DEBUG
        assert bodies[0].getMessage() == f"API error response: status={statuses[-1]} body={ERROR_BODY!r}"


def test_upload_error_body_never_reaches_the_server_log(caplog):
    names = ("", "garminconnect", "garminconnect.client", "mcp")
    before = {name: logging.getLogger(name).level for name in names}
    try:
        for name in names[1:]:
            logging.getLogger(name).setLevel(logging.NOTSET)
        configure_logging("DEBUG")
        # basicConfig is a no-op under pytest; without this line the test passes whatever the configuration does
        logging.getLogger().setLevel(logging.DEBUG)
        for status in (400, 500):
            garmin, _, _ = _real_garmin([status])
            with pytest.raises(GarminConnectConnectionError):
                garmin.upload_workout(fx.STRENGTH_UPLOAD)
        assert _library_records(caplog) == []
        assert [r for r in caplog.records if "body-marker" in r.getMessage() or "BENCH_PRESS" in r.getMessage()] == []
        assert logging.getLogger("garminconnect").level == logging.WARNING
        # garminconnect.client inherits; a level of its own would bypass the parent
        assert logging.getLogger("garminconnect.client").level == logging.NOTSET
        assert logging.getLogger("mcp").level == logging.WARNING
    finally:
        for name, level in before.items():
            logging.getLogger(name).setLevel(level)
