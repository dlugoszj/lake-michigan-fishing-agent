"""The two free predictors the agent has to beat (no AI).

  same_as_last_week: the previous report's rating for each port (same answer-key rules as the truth).
  same_week_past_years: the port's average rating in reports within 14 days of the same calendar date
                        in EARLIER years only, rounded (no peeking at later years).
  always_0 ... always_4: the same rating for every port, every week. The floor: a predictor that can't beat
                        the best of these isn't telling you anything the typical rating doesn't.
"""
from datetime import date

from evals.agent.common import PORTS, rating, reports


def same_as_last_week(case: dict) -> dict[str, int | None]:
    return {p: rating(case["prev_report_id"], p, case["method"]) for p in PORTS}


def same_week_past_years(case: dict, window_days: int = 14) -> dict[str, int | None]:
    target = date.fromisoformat(case["report_date"])
    out = {}
    for p in PORTS:
        vals = []
        for d, rid in reports():
            dd = date.fromisoformat(d)
            if dd.year >= target.year:
                continue
            try:
                same_day = target.replace(year=dd.year)
            except ValueError:  # Feb 29
                same_day = target.replace(year=dd.year, day=28)
            if abs((dd - same_day).days) <= window_days and (r := rating(rid, p, case["method"])) is not None:
                vals.append(r)
        out[p] = int(sum(vals) / len(vals) + 0.5) if vals else None  # round half up
    return out


def always(k: int):
    return lambda case: {p: k for p in PORTS}


BASELINES = {"same_as_last_week": same_as_last_week, "same_week_past_years": same_week_past_years,
             **{f"always_{k}": always(k) for k in range(5)}}
