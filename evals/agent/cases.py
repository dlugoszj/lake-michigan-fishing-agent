"""Pick backtest weeks and write evals/agent/cases.jsonl (no API calls).

  python -m evals.agent.cases                  # every qualifying dev week -> cases_dev.jsonl
  python -m evals.agent.cases --split test     # every test week -> cases_test.jsonl (run once, at the end)
  python -m evals.agent.cases --n 10           # a smaller sample, spread over years and months

A case is one report R (the answer key) and one method (pier or boat). The agent runs "as of" the day
before R, so it never sees R or anything later. R qualifies if it rates >= 2 ports for pier AND for boat,
and a previous report exists within 14 days (for the "same as last week" baseline).
Every qualifying R is put in dev or test by a hash of its id (1 in 3 test), so the split never changes;
the first pilot (cases_pilot.jsonl) was 10 dev weeks.
"""
import argparse
import hashlib
import json
import random
from collections import defaultdict
from datetime import date, timedelta

from evals.agent.common import METHODS, PORTS, cases_file, ratings, reports

QUESTIONS = {
    "pier": "I fish from the pier for salmon. For this week, how good do you expect pier fishing for salmon to be at each of "
            "St. Joseph, South Haven, Grand Haven and Muskegon? Rate each port 0-4 (0 slow, 1 below average, 2 average, "
            "3 good, 4 excellent) and say which you'd go to.",
    "boat": "I fish for salmon from a boat. For this week, how good do you expect boat fishing for salmon to be at each of "
            "St. Joseph, South Haven, Grand Haven and Muskegon? Rate each port 0-4 (0 slow, 1 below average, 2 average, "
            "3 good, 4 excellent) and say which you'd go to.",
}


def split_of(report_id: str) -> str:
    return "test" if int(hashlib.sha256(report_id.encode()).hexdigest(), 16) % 3 == 0 else "dev"


def candidates() -> list[dict]:
    reps = reports()
    out = []
    for i, (d, rid) in enumerate(reps):
        if not 5 <= int(d[5:7]) <= 10 or i == 0:
            continue
        prev_d, prev_id = reps[i - 1]
        if (date.fromisoformat(d) - date.fromisoformat(prev_d)).days > 14:
            continue
        truth = {m: ratings(rid, m) for m in METHODS}
        if all(sum(v is not None for v in truth[m].values()) >= 2 for m in METHODS):
            out.append({"report_id": rid, "report_date": d, "prev_report_id": prev_id, "prev_date": prev_d,
                        "truth": truth, "split": split_of(rid)})
    return out


def pick(pool: list[dict], n: int, seed: int = 7) -> list[dict]:
    """n weeks spread over years, then months: round-robin over years, a different month each time where possible."""
    rng = random.Random(seed)
    by_year = defaultdict(list)
    for c in pool:
        by_year[c["report_date"][:4]].append(c)
    for v in by_year.values():
        rng.shuffle(v)
    chosen, used_months = [], defaultdict(set)
    while len(chosen) < n and any(by_year.values()):
        for y in sorted(by_year):
            if len(chosen) == n or not by_year[y]:
                continue
            fresh = [c for c in by_year[y] if c["report_date"][5:7] not in used_months[y]]
            c = (fresh or by_year[y])[0]
            by_year[y].remove(c)
            used_months[y].add(c["report_date"][5:7])
            chosen.append(c)
    return sorted(chosen, key=lambda c: c["report_date"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=0, help="number of weeks (each gives a pier and a boat case); 0 = all")
    ap.add_argument("--split", choices=["dev", "test"], default="dev")
    args = ap.parse_args()

    pool = candidates()
    print(f"qualifying weeks: {len(pool)} ({sum(c['split'] == 'dev' for c in pool)} dev, "
          f"{sum(c['split'] == 'test' for c in pool)} test)")
    split_pool = [c for c in pool if c["split"] == args.split]
    weeks = pick(split_pool, args.n or len(split_pool))
    out = cases_file(args.split)
    with out.open("w") as f:
        for w in weeks:
            as_of = (date.fromisoformat(w["report_date"]) - timedelta(days=1)).isoformat()
            for m in METHODS:
                f.write(json.dumps({"case_id": f"{w['report_id']}-{m}", "method": m, "as_of": as_of,
                                    "question": QUESTIONS[m], "truth": w["truth"][m], **{k: w[k] for k in
                                    ("report_id", "report_date", "prev_report_id", "prev_date", "split")}}) + "\n")
    print(f"wrote {2 * len(weeks)} cases ({len(weeks)} weeks x pier/boat) to {out}")


if __name__ == "__main__":
    main()
