"""Text sent to the athlete per language (``COACH_LANGUAGE``), English (default) and Czech: the morning briefing, the
Monday report and the replies to the athlete's Telegram messages.

Only words live here; the rules, numbers and layout stay in ``briefing.py``, ``weekly.py`` and ``inbound.py``. Every
catalogue has the same fields, so adding a language means adding one more ``Texts`` instance and a ``LANGUAGES`` entry,
a ``WeeklyTexts`` instance with a ``WEEKLY_LANGUAGES`` entry (the Monday report) and a ``ReplyTexts`` instance with a
``REPLY_LANGUAGES`` entry (the replies).
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
    reason_last_sync: str  # {when}
    sync_today: str  # {time}
    sync_yesterday: str  # {time}
    sync_on: str  # {day} {time}
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
    alert_missed_session_stale: str  # {sessions}
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
    reason_last_sync="last sync with Garmin: {when}",
    sync_today="today {time}",
    sync_yesterday="yesterday {time}",
    sync_on="{day} {time}",
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
    alert_missed_session_stale=(
        "Planned yesterday with no activity in Garmin at the nightly check, before the watch's last sync "
        "(may be out of date): {sessions}."
    ),
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
    reason_last_sync="poslední synchronizace s Garminem: {when}",
    sync_today="dnes {time}",
    sync_yesterday="včera {time}",
    sync_on="{day} {time}",
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
    alert_missed_session_stale=(
        "Včera v plánu bez aktivity v Garminu při noční kontrole, před poslední synchronizací hodinek "
        "(nemusí platit): {sessions}."
    ),
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

@dataclass(frozen=True)
class WeeklyTexts:
    """Words of the Monday report (``weekly.render_weekly``); the layout and the numbers stay there."""

    title: str  # {week} {days}
    hours_minutes: str  # {h} {m}
    minutes_only: str  # {m}
    activities: Callable[[int], str]
    workouts: Callable[[int], str]
    endurance: str  # {activities} {time} {km} {load} {average}
    endurance_average: str  # {time} {load}
    strength: str  # {workouts} {sets} {volume} {average}
    strength_average: str  # {sets} {volume}
    average: str  # {value}
    sleep: str  # {time} {average}
    hrv: str  # {value} {average}
    resting_hr: str  # {value} {average}
    plan: str  # {done} {skipped} {moved} {planned} {rate}
    plan_rate: str  # {rate}
    new_bests: str  # {items}
    best: Mapping[str, str]  # metric of get_weekly_report new_bests -> {value} {previous} {exercise}
    flags: str  # {items}
    flag: Mapping[str, str]  # flag of get_weekly_report flags -> {pct} {minutes} {bpm}
    no_data: str
    goals: str
    goal_metric: Mapping[str, str]  # goal metric -> name
    goal_line: str  # {title} {metric} {current} {ago} {trend} {outlook}
    goal_ago: str  # {value}
    goal_trend: str  # {change}
    goal_on_track: str  # {projected} {date} {target}
    goal_behind: str  # {projected} {date} {target}
    goal_projection: str  # {projected} {date}
    goal_reason: str  # {reason}
    goal_unavailable: str  # {title} {metric} {reason}
    goal_reasons: Mapping[str, str]  # reason code of get_goal_progress -> text
    goals_without_metric: str  # {n}



def _plural_en(one: str, many: str) -> Callable[[int], str]:
    return lambda n: f"1 {one}" if n == 1 else f"{n} {many}"


def _plural_cs(one: str, few: str, many: str) -> Callable[[int], str]:
    return lambda n: f"1 {one}" if n == 1 else f"{n} {few}" if 2 <= n <= 4 else f"{n} {many}"

WEEKLY_EN = WeeklyTexts(
    title="\U0001f4ca Weekly report {week} · {days}",
    hours_minutes="{h} h {m} min", minutes_only="{m} min",
    activities=_plural_en("activity", "activities"), workouts=_plural_en("workout", "workouts"),
    endurance="Endurance: {activities}, {time}, {km} km, load {load}{average}",
    endurance_average=" (4-week average {time}, load {load})",
    strength="Strength (Hevy): {workouts}, working sets {sets}, volume {volume} kg{average}",
    strength_average=" (4-week average: sets {sets}, volume {volume} kg)",
    average=" (4-week average {value})",
    sleep="Sleep: average {time}{average}",
    hrv="HRV: average {value} ms{average}",
    resting_hr="Resting HR: average {value} bpm{average}",
    plan="Plan: done {done}, skipped {skipped}, moved {moved}, still planned {planned}{rate}",
    plan_rate=" – completion {rate} %",
    new_bests="New bests: {items}.",
    best={
        "vo2max_running": "VO2max running {value} (was {previous})",
        "vo2max_cycling": "VO2max cycling {value} (was {previous})",
        "running_distance_km": "running {value} km in a week (was {previous} km)",
        "cycling_distance_km": "cycling {value} km in a week (was {previous} km)",
        "swimming_distance_km": "swimming {value} km in a week (was {previous} km)",
        "endurance_duration_min": "endurance time {value} in a week (was {previous})",
        "endurance_training_load": "endurance load {value} in a week (was {previous})",
        "e1rm_kg": "estimated 1RM {exercise} {value} kg (was {previous} kg)",
    },
    flags="Watch: {items}.",
    flag={
        "endurance_load_up": "endurance load {pct} % above the 4-week average",
        "endurance_load_down": "endurance load {pct} % below the 4-week average",
        "endurance_time_up": "endurance time {pct} % above the 4-week average",
        "sleep_down": "sleep {minutes} min a night shorter than the 4-week average",
        "hrv_down": "HRV {pct} % below the 4-week average",
        "resting_hr_up": "resting HR {bpm} bpm above the 4-week average",
        "strength_volume_up": "strength volume {pct} % above the 4-week average",
    },
    no_data="No stored data for this week (the nightly history is off or still loading).",
    goals="Goals:",
    goal_metric={
        "race_5k": "5 km prediction",
        "race_10k": "10 km prediction",
        "race_half_marathon": "half marathon prediction",
        "race_marathon": "marathon prediction",
        "vo2max_running": "VO2max running",
    },
    goal_line="- {title} ({metric}): now {current}{ago}{trend}{outlook}",
    goal_ago=", 4 weeks ago {value}",
    goal_trend=", {change} a week",
    goal_on_track="; at this trend {projected} on {date}, target {target}: on track",
    goal_behind="; at this trend {projected} on {date}, target {target}: behind",
    goal_projection="; at this trend {projected} on {date}",
    goal_reason="; {reason}",
    goal_unavailable="- {title} ({metric}): not enough data yet ({reason})",
    goal_reasons={
        "no_recent_value": "no value in the last 14 days",
        "too_few_values": "fewer than 14 values over 4 weeks",
        "no_target_date": "no target date",
        "target_date_passed": "the target date has passed",
        "target_date_too_far": "the target date is more than 26 weeks away",
        "trend_too_steep": "the trend is too steep to project",
    },
    goals_without_metric="Goals without a measurable metric: {n}.",
)

WEEKLY_CS = WeeklyTexts(
    title="\U0001f4ca Týdenní report {week} · {days}",
    hours_minutes="{h} h {m} min", minutes_only="{m} min",
    activities=_plural_cs("aktivita", "aktivity", "aktivit"), workouts=_plural_cs("trénink", "tréninky", "tréninků"),
    endurance="Vytrvalost: {activities}, {time}, {km} km, zátěž {load}{average}",
    endurance_average=" (průměr 4 týdnů {time}, zátěž {load})",
    strength="Síla (Hevy): {workouts}, pracovní série {sets}, objem {volume} kg{average}",
    strength_average=" (průměr 4 týdnů: série {sets}, objem {volume} kg)",
    average=" (průměr 4 týdnů {value})",
    sleep="Spánek: průměr {time}{average}",
    hrv="HRV: průměr {value} ms{average}",
    resting_hr="Klidová tepovka: průměr {value} bpm{average}",
    plan="Plán: splněno {done}, přeskočeno {skipped}, přesunuto {moved}, zbývá v plánu {planned}{rate}",
    plan_rate=" – plnění {rate} %",
    new_bests="Nové rekordy: {items}.",
    best={
        "vo2max_running": "VO2max běh {value} (dříve {previous})",
        "vo2max_cycling": "VO2max kolo {value} (dříve {previous})",
        "running_distance_km": "běh {value} km za týden (dříve {previous} km)",
        "cycling_distance_km": "kolo {value} km za týden (dříve {previous} km)",
        "swimming_distance_km": "plavání {value} km za týden (dříve {previous} km)",
        "endurance_duration_min": "vytrvalost {value} za týden (dříve {previous})",
        "endurance_training_load": "vytrvalostní zátěž {value} za týden (dříve {previous})",
        "e1rm_kg": "odhad 1RM {exercise} {value} kg (dříve {previous} kg)",
    },
    flags="Pozor: {items}.",
    flag={
        "endurance_load_up": "vytrvalostní zátěž o {pct} % nad průměrem 4 týdnů",
        "endurance_load_down": "vytrvalostní zátěž o {pct} % pod průměrem 4 týdnů",
        "endurance_time_up": "čas vytrvalosti o {pct} % nad průměrem 4 týdnů",
        "sleep_down": "spánek o {minutes} min za noc kratší než průměr 4 týdnů",
        "hrv_down": "HRV o {pct} % pod průměrem 4 týdnů",
        "resting_hr_up": "klidová tepovka o {bpm} bpm nad průměrem 4 týdnů",
        "strength_volume_up": "objem síly o {pct} % nad průměrem 4 týdnů",
    },
    no_data="Pro tento týden nejsou uložená data (noční historie je vypnutá nebo se ještě načítá).",
    goals="Cíle:",
    goal_metric={
        "race_5k": "predikce 5 km",
        "race_10k": "predikce 10 km",
        "race_half_marathon": "predikce půlmaratonu",
        "race_marathon": "predikce maratonu",
        "vo2max_running": "VO2max běh",
    },
    goal_line="- {title} ({metric}): teď {current}{ago}{trend}{outlook}",
    goal_ago=", před 4 týdny {value}",
    goal_trend=", {change} za týden",
    goal_on_track="; tímto tempem {projected} k {date}, cíl {target}: na dobré cestě",
    goal_behind="; tímto tempem {projected} k {date}, cíl {target}: zaostává",
    goal_projection="; tímto tempem {projected} k {date}",
    goal_reason="; {reason}",
    goal_unavailable="- {title} ({metric}): zatím málo dat ({reason})",
    goal_reasons={
        "no_recent_value": "žádná hodnota za posledních 14 dní",
        "too_few_values": "méně než 14 hodnot za 4 týdny",
        "no_target_date": "bez cílového data",
        "target_date_passed": "cílové datum už minulo",
        "target_date_too_far": "cílové datum je dál než 26 týdnů",
        "trend_too_steep": "trend je příliš strmý na odhad",
    },
    goals_without_metric="Cíle bez měřitelné metriky: {n}.",
)

WEEKLY_LANGUAGES: Mapping[str, WeeklyTexts] = {"en": WEEKLY_EN, "cs": WEEKLY_CS}


def weekly_texts(language: str) -> WeeklyTexts:
    """The Monday-report catalogue for ``language``; same keys as ``LANGUAGES``."""
    return WEEKLY_LANGUAGES[language]


@dataclass(frozen=True)
class ReplyTexts:
    """Words of the replies to the athlete's Telegram messages (``inbound.py``).

    The rules are in ``plan.py`` and ``inbound.py``.
    """

    weekdays: tuple[str, ...]  # Monday first
    help: str
    unknown_command: str
    unsupported: str
    usage_today: str
    usage_plan: str
    usage_done: str
    usage_skip: str
    open_today: str
    no_open_today: str
    plan_title: str  # {week} {start} {end}
    plan_empty: str
    recorded: str  # {line}
    unknown_session: str  # {id}
    session_superseded: str  # {id}
    session_moved: str  # {id}
    session_moved_to: str  # {id} {copy} {day}
    session_moved_superseded: str  # {id} {copy} {day}
    session_future: str  # {id} {day}
    cannot_change: str  # {id}
    note_saved: str  # {day}
    note_too_long: str  # {length} {limit}
    note_refused: str
    briefing_failed: str
    failed: str


REPLY_EN = ReplyTexts(
    weekdays=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    help=(
        "Commands:\n"
        "/today (or /dnes) – today's briefing\n"
        "/plan – this week's plan with the session ids\n"
        "/done <id> – mark a session as done\n"
        "/skip <id> – mark a session as skipped\n"
        "Any other text is saved as a note for today."
    ),
    unknown_command="Unknown command.",
    unsupported="Only text messages are supported.",
    usage_today="Usage: /today (or /dnes) with nothing after it.",
    usage_plan="Usage: /plan with nothing after it.",
    usage_done="Usage: /done <id>, for example /done 12. Nothing was changed.",
    usage_skip="Usage: /skip <id>, for example /skip 12. Nothing was changed.",
    open_today="Open today:",
    no_open_today="No open session today.",
    plan_title="Plan {week} ({start} – {end}):",
    plan_empty="No sessions this week.",
    recorded="Recorded: {line}",
    unknown_session="Session #{id} does not exist.",
    session_superseded="Session #{id} was replaced by a later plan and cannot be changed.",
    session_moved="Session #{id} was moved and cannot be changed.",
    session_moved_to="Session #{id} was moved to #{copy} ({day}); change that one.",
    session_moved_superseded=(
        "Session #{id} was moved to #{copy} ({day}), which a later plan replaced; nothing is left to change."
    ),
    session_future="Session #{id} is planned for {day} and cannot be done yet.",
    cannot_change="Session #{id} cannot be changed.",
    note_saved="Note saved ({day}).",
    note_too_long="The note is too long ({length} characters, at most {limit}). Nothing was saved.",
    note_refused="The note could not be saved.",
    briefing_failed="The briefing could not be computed right now. Try again later.",
    failed="Something went wrong (internal error).",
)

REPLY_CS = ReplyTexts(
    weekdays=("po", "út", "st", "čt", "pá", "so", "ne"),
    help=(
        "Příkazy:\n"
        "/dnes (nebo /today) – dnešní briefing\n"
        "/plan – plán na tento týden s id tréninků\n"
        "/done <id> – označí trénink jako splněný\n"
        "/skip <id> – označí trénink jako přeskočený\n"
        "Jakýkoli jiný text se uloží jako dnešní poznámka."
    ),
    unknown_command="Neznámý příkaz.",
    unsupported="Umím jen textové zprávy.",
    usage_today="Použití: /dnes (nebo /today), bez dalšího textu.",
    usage_plan="Použití: /plan, bez dalšího textu.",
    usage_done="Použití: /done <id>, například /done 12. Nic se nezměnilo.",
    usage_skip="Použití: /skip <id>, například /skip 12. Nic se nezměnilo.",
    open_today="Dnes otevřené:",
    no_open_today="Dnes není žádný otevřený trénink.",
    plan_title="Plán {week} ({start} – {end}):",
    plan_empty="Tento týden není nic v plánu.",
    recorded="Zapsáno: {line}",
    unknown_session="Trénink #{id} neexistuje.",
    session_superseded="Trénink #{id} nahradil pozdější plán a nejde změnit.",
    session_moved="Trénink #{id} byl přesunut a nejde změnit.",
    session_moved_to="Trénink #{id} byl přesunut na #{copy} ({day}); změň ten.",
    session_moved_superseded=(
        "Trénink #{id} byl přesunut na #{copy} ({day}) a ten nahradil pozdější plán; není co měnit."
    ),
    session_future="Trénink #{id} je v plánu až {day} a ještě nemůže být splněný.",
    cannot_change="Trénink #{id} nejde změnit.",
    note_saved="Poznámka uložena ({day}).",
    note_too_long="Poznámka je moc dlouhá ({length} znaků, nejvýš {limit}). Nic se neuložilo.",
    note_refused="Poznámku se nepodařilo uložit.",
    briefing_failed="Briefing se teď nepodařilo spočítat. Zkus to později.",
    failed="Něco se pokazilo (vnitřní chyba).",
)

REPLY_LANGUAGES: Mapping[str, ReplyTexts] = {"en": REPLY_EN, "cs": REPLY_CS}


def reply_texts(language: str) -> ReplyTexts:
    """The reply catalogue for ``language``; same keys as ``LANGUAGES``."""
    return REPLY_LANGUAGES[language]
