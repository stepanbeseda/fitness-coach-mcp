"""Correlations between stored daily history metrics (``history_days``).

Pure functions over the stored history with today's date passed in; nothing here reads the clock or calls
Garmin. A day without a value is skipped, never counted as zero (an activity total is the exception, as in
``trends``: a day whose activities were fetched and found empty is a real zero). The maths is the stdlib
``statistics.correlation``.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import tools
from .store import Store

# The per-day reader is shared with get_trends on purpose, so the two tools cannot drift apart.
from .trends import DEFAULT_RANGE_DAYS, MAX_LONG_RANGE, METRIC_NAMES, _daily

MIN_PAIRS = 14
MAX_LAG_DAYS = 3
MAX_METRICS = 6
DECIMALS = 3
CAVEAT = (
    "A correlation is not a cause: two metrics can move together because of a third factor "
    "(a training block, illness, the season) or by chance, especially with few paired days."
)


def _coef(xs: list[float], ys: list[float], method: str) -> float:
    # ``+ 0.0`` turns -0.0 into 0.0.
    return round(statistics.correlation(xs, ys, method=method), DECIMALS) + 0.0  # type: ignore[arg-type]


def _pair(
    a: str, b: str, series: dict[str, dict[dt.date, float]], lag_days: int
) -> dict[str, Any]:
    sa, sb = series[a], series[b]
    shift = dt.timedelta(days=lag_days)
    xs: list[float] = []
    ys: list[float] = []
    for day in sorted(sa):
        other = day + shift
        if other in sb:
            xs.append(sa[day])
            ys.append(sb[other])
    n = len(xs)
    if n < MIN_PAIRS:
        return {"a": a, "b": b, "n_pairs": n, "available": False, "reason": "too_few_pairs"}
    # Exact equality, not variance: the mean of identical floats need not be exact.
    constant = [m for m, vals in ((a, xs), (b, ys)) if len(set(vals)) == 1]
    if constant:
        return {"a": a, "b": b, "n_pairs": n, "available": False, "reason": "constant", "constant": constant}
    return {
        "a": a,
        "b": b,
        "n_pairs": n,
        "available": True,
        "pearson": _coef(xs, ys, "linear"),
        "spearman": _coef(xs, ys, "ranked"),
    }


def get_correlations(
    store: Store,
    today: dt.date,
    *,
    metrics: list[str],
    start_date: str | None,
    end_date: str | None,
    lag_days: int,
) -> dict[str, Any]:
    names = list(dict.fromkeys(metrics))
    for name in names:
        if name not in METRIC_NAMES:
            raise ToolError(f"unknown metric: {name}")
    if len(names) < 2:
        raise ToolError("at least two different metrics are needed")
    if len(names) > MAX_METRICS:
        raise ToolError(f"at most {MAX_METRICS} different metrics")
    if not 0 <= lag_days <= MAX_LAG_DAYS:
        raise ToolError(f"lag_days must be 0 to {MAX_LAG_DAYS}")
    end = tools.parse_date(end_date, "end_date") if end_date is not None else today - dt.timedelta(days=1)
    if end > today:
        raise ToolError("end_date must not be after today")
    start = tools.parse_date(start_date, "start_date") if start_date is not None else end - dt.timedelta(
        days=DEFAULT_RANGE_DAYS - 1
    )
    if start > end:
        raise ToolError("start_date must be on or before end_date")
    if (end - start).days + 1 > MAX_LONG_RANGE:
        raise ToolError(f"date range too long (max {MAX_LONG_RANGE} days)")

    rows = store.history_rows(start, end)
    series: dict[str, dict[dt.date, float]] = {}
    for name in names:
        values = ((dt.date.fromisoformat(row["date"]), _daily(row, name)) for row in rows)
        series[name] = {day: value for day, value in values if value is not None}
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "lag_days": lag_days,
        "min_pairs": MIN_PAIRS,
        "caveat": CAVEAT,
        "pairs": [
            _pair(names[i], names[j], series, lag_days)
            for i in range(len(names))
            for j in range(i + 1, len(names))
        ],
    }
