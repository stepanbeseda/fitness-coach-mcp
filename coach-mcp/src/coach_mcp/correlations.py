"""Correlations between stored daily history metrics (``history_days``).

Pure functions over the history view (``history_view``) with today's date passed in; nothing here reads the
clock or calls Garmin. A day without a value is skipped, never counted as zero (an activity total is the
exception, as in ``trends``: a day whose activities were fetched and found empty is a real zero). The maths is
the stdlib ``statistics.correlation``.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .history_view import HistoryReader, HistoryView, history_range
from .store import Store

# The per-day reader is shared with get_trends on purpose, so the two tools cannot drift apart.
from .trends import METRIC_NAMES, _daily

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
    history: HistoryReader | None = None,
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
    start, end = history_range(today, start_date, end_date)
    days = (history if history is not None else HistoryView(store)).days(start, end)
    series: dict[str, dict[dt.date, float]] = {}
    for name in names:
        values = ((day.date, _daily(day, name)) for day in days)
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
