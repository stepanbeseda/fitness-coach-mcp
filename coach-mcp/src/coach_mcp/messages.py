"""Text of the morning briefing per language (``COACH_LANGUAGE``): English (default) and Czech.

Only words live here; the rules, numbers and layout stay in ``briefing.py``. Every catalogue has the
same fields, so adding a language means adding one more ``Texts`` instance and a ``LANGUAGES`` entry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date

DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True)
class Texts:
    decimal_separator: str
    level: Mapping[str, str]  # green / yellow / red
    criterion: Mapping[str, str]  # readiness / hrv / sleep / body_battery
    session_type: Mapping[str, str]  # running / cycling / ... / other
    status_suffix: Mapping[str, str]  # done / skipped / moved
    advice: Mapping[str, str]  # green / yellow / red
    acwr: Mapping[str, str]  # Garmin acwrStatus LOW / HIGH / VERY_HIGH
    nights: Callable[[int], str]
    day_title: Callable[[date], str]
    untitled: str
    # Why criteria are missing (``briefing._missing_reason``).
    reason_auth: str
    reason_no_token: str
    reason_unreachable: str
    reason_no_data: str
    reason_baseline_pending: str
    reason_not_synced: str
    # HRV line.
    baseline_from: str  # {low}
    baseline_range: str  # {low} {high}
    below_baseline: str
    in_a_row: str  # {nights}
    # Alerts.
    alert_hrv: str  # {nights}
    alert_resting_hr: str  # {today} {delta} {avg}
    alert_acwr: str  # {word}
    alert_acwr_ratio: str  # {ratio} {word}
    alert_missed_workout: str  # {names}
    alert_missed_session: str  # {sessions}
    alert_garmin_auth: str
    # Message lines.
    verdict: str  # {level} {advice}
    missing_value: str
    readiness: str
    hrv: str
    sleep: str
    sleep_value: str  # {h} {m} {score} {level}
    sleep_score: str  # {score}
    body_battery: str
    hrv_baseline: str
    missing_line: str  # {names} {reason}
    target_readiness: str  # {value}
    target_hrv: str  # {value}
    target_sleep: str
    target_body_battery: str  # {value}
    full_load: str  # {items}
    today_plan: str
    today_garmin: str  # {names}
    today_nothing: str
    alerts: str
    title: str  # {circle} {warning} {level} {day}


def _nights_en(n: int) -> str:
    return "1 night" if n == 1 else f"{n} nights"


def _nights_cs(n: int) -> str:
    if n == 1:
        return "1 noc"
    if 2 <= n <= 4:
        return f"{n} noci"
    return f"{n} nocí"


_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

EN = Texts(
    decimal_separator=".",
    level={"green": "green", "yellow": "yellow", "red": "red"},
    criterion={"readiness": "readiness", "hrv": "HRV", "sleep": "sleep", "body_battery": "Body Battery"},
    session_type={
        "running": "run",
        "cycling": "ride",
        "swimming": "swim",
        "strength": "strength",
        "mobility": "mobility",
        "other": "other",
    },
    status_suffix={"done": " – done", "skipped": " – skipped", "moved": " – moved"},
    advice={
        "green": "full plan, you can add progression.",
        "yellow": "cut volume by 20–30 %, intensity at most RPE 7–8, cardio in zone 2.",
        "red": "recovery only: easy zone 1–2 cardio for 20–40 min, mobility, or a rest day.",
    },
    acwr={"LOW": "low", "HIGH": "high", "VERY_HIGH": "very high"},
    nights=_nights_en,
    day_title=lambda day: f"{day.day} {_MONTHS_EN[day.month - 1]}",
    untitled="untitled",
    reason_auth="Garmin is not logged in",
    reason_no_token="the internal token for garmin-mcp is missing (secrets/garmin_readonly_token)",
    reason_unreachable="garmin-mcp does not respond",
    reason_no_data="the Garmin data could not be loaded",
    reason_baseline_pending="Garmin computes it only after about 3 weeks of wearing the watch",
    reason_not_synced="the watch may not have synced yet",
    baseline_from="baseline from {low} ms",
    baseline_range="baseline {low}–{high} ms",
    below_baseline=", below baseline",
    in_a_row=" {nights} in a row",
    alert_hrv="HRV below baseline {nights} in a row.",
    alert_resting_hr="Resting HR {today} bpm, {delta} above the 7-day average of {avg} bpm.",
    alert_acwr="The acute to chronic load ratio is outside Garmin's optimal range ({word}).",
    alert_acwr_ratio="The acute to chronic load ratio {ratio} is outside Garmin's optimal range ({word}).",
    alert_missed_workout="Yesterday's scheduled workout was not completed: {names}.",
    alert_missed_session="Planned yesterday with no activity in Garmin: {sessions}.",
    alert_garmin_auth="Garmin is not logged in: run scripts/garmin-login.sh on the server.",
    verdict="Verdict: {level} – {advice}",
    missing_value="missing",
    readiness="Readiness",
    hrv="HRV",
    sleep="Sleep",
    sleep_value="Sleep: {h} h {m} min{score} ({level})",
    sleep_score=", score {score}",
    body_battery="Body Battery at wake",
    hrv_baseline="HRV baseline",
    missing_line="Missing: {names} ({reason}). Without these data the verdict cannot be green.",
    target_readiness="readiness at least {value}",
    target_hrv="HRV back to at least {value} ms",
    target_sleep="sleep at least 7 h",
    target_body_battery="Body Battery at wake at least {value}",
    full_load="For full load: {items}.",
    today_plan="Today's plan:",
    today_garmin="Today in Garmin: {names}",
    today_nothing="Today's plan: nothing.",
    alerts="Alerts:",
    title="{circle}{warning} {level} · morning briefing {day}",
)

CS = Texts(
    decimal_separator=",",
    level={"green": "zelená", "yellow": "žlutá", "red": "červená"},
    criterion={"readiness": "readiness", "hrv": "HRV", "sleep": "spánek", "body_battery": "Body Battery"},
    session_type={
        "running": "běh",
        "cycling": "kolo",
        "swimming": "plavání",
        "strength": "síla",
        "mobility": "mobilita",
        "other": "jiné",
    },
    status_suffix={"done": " – splněno", "skipped": " – přeskočeno", "moved": " – přesunuto"},
    advice={
        "green": "plný plán, můžeš přidat progresi.",
        "yellow": "sniž objem o 20–30 %, intenzitu nejvýš RPE 7–8, kardio v zóně 2.",
        "red": "jen regenerace: lehké kardio v zóně 1–2 na 20–40 min, mobilita, nebo volno.",
    },
    acwr={"LOW": "nízký", "HIGH": "vysoký", "VERY_HIGH": "velmi vysoký"},
    nights=_nights_cs,
    day_title=lambda day: f"{day.day}. {day.month}.",
    untitled="bez názvu",
    reason_auth="Garmin není přihlášený",
    reason_no_token="chybí interní token pro garmin-mcp (secrets/garmin_readonly_token)",
    reason_unreachable="garmin-mcp neodpovídá",
    reason_no_data="data z Garminu se nepodařilo načíst",
    reason_baseline_pending="Garmin ji počítá až po zhruba 3 týdnech nošení hodinek",
    reason_not_synced="hodinky možná ještě nesynchronizovaly",
    baseline_from="baseline od {low} ms",
    baseline_range="baseline {low}–{high} ms",
    below_baseline=", pod baseline",
    in_a_row=" {nights} po sobě",
    alert_hrv="HRV pod baseline {nights} po sobě.",
    alert_resting_hr="Klidová tepovka {today} bpm, o {delta} víc než 7denní průměr {avg} bpm.",
    alert_acwr="Poměr akutní a chronické zátěže je mimo optimální rozsah Garminu ({word}).",
    alert_acwr_ratio="Poměr akutní a chronické zátěže {ratio} je mimo optimální rozsah Garminu ({word}).",
    alert_missed_workout="Včera naplánovaný trénink nebyl splněn: {names}.",
    alert_missed_session="Včera v plánu bez aktivity v Garminu: {sessions}.",
    alert_garmin_auth="Garmin není přihlášený: na serveru spusť scripts/garmin-login.sh.",
    verdict="Verdikt: {level} – {advice}",
    missing_value="chybí",
    readiness="Readiness",
    hrv="HRV",
    sleep="Spánek",
    sleep_value="Spánek: {h} h {m} min{score} ({level})",
    sleep_score=", skóre {score}",
    body_battery="Body Battery ráno",
    hrv_baseline="HRV baseline",
    missing_line="Chybí: {names} ({reason}). Bez těchto dat nemůže být verdikt zelený.",
    target_readiness="readiness aspoň {value}",
    target_hrv="HRV zpět aspoň na {value} ms",
    target_sleep="spánek aspoň 7 h",
    target_body_battery="Body Battery ráno aspoň {value}",
    full_load="Pro plnou zátěž: {items}.",
    today_plan="Dnes v plánu:",
    today_garmin="Dnes v Garminu: {names}",
    today_nothing="Dnes v plánu: nic.",
    alerts="Upozornění:",
    title="{circle}{warning} {level} · ranní briefing {day}",
)

LANGUAGES: Mapping[str, Texts] = {"en": EN, "cs": CS}


def texts(language: str) -> Texts:
    """The catalogue for ``language``; ``config.Settings`` only accepts keys of ``LANGUAGES``."""
    return LANGUAGES[language]
