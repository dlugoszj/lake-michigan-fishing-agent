"""Score every predictor on the backtest cases (no API calls).

  python -m evals.agent.score                    # dev: baselines, plus Sonnet 5.5 agent results (results_dev_<model>.jsonl)
  python -m evals.agent.score --model claude-haiku-5-5
  python -m evals.agent.score --split test
  python -m evals.agent.score --pilot            # the first 10-week pilot (results_pilot.jsonl)

Two tables per method:
  SAME PORTS (headline): only the (case, port) pairs that every predictor, in every run, gave a rating for,
             so all predictors are scored on exactly the same ports.
  ALL PORTS: every port the answer-key report rates; predictors that skip ports show lower coverage.
Metrics, with 'worth going' = rating >= GOOD[method] (pier 1, boat 2; see common.py):
  accuracy   got worth-going vs. not right
  precision  of the ports it called worth going, how many were worth going in the report
  recall     of the ports worth going in the report, how many it called worth going (unrated by it = missed)
  within 1   its rating was within 1 point of the report's (over ports it rated)
  error      mean absolute difference (over ports it rated)
  coverage   share of rated ports it gave a rating for
Agent predictors run several times per case; each metric is averaged over runs, with the min-max range.
"""
import argparse
import json
from collections import defaultdict

from evals.agent.baselines import BASELINES
from evals.agent.common import GOOD, HERE, METHODS, PORTS, SPLITS, cases_file, results_file


def metrics(pairs: list[tuple[int, int | None]], good_at: int) -> dict:
    """pairs: (truth, prediction or None) for every rated port; good_at: the method's worth-going cutoff."""
    called = [(t, p) for t, p in pairs if p is not None and p >= good_at]
    good = [(t, p) for t, p in pairs if t >= good_at]
    rated = [(t, p) for t, p in pairs if p is not None]
    ratio = lambda num, den: num / den if den else None
    return {"accuracy": ratio(sum((t >= good_at) == (p >= good_at) for t, p in rated), len(rated)),
            "precision": ratio(sum(t >= good_at for t, _ in called), len(called)),
            "recall": ratio(sum(p is not None and p >= good_at for _, p in good), len(good)),
            "within1": ratio(sum(abs(t - p) <= 1 for t, p in rated), len(rated)),
            "error": ratio(sum(abs(t - p) for t, p in rated), len(rated)),
            "coverage": ratio(len(rated), len(pairs)), "n": len(pairs)}


def fmt(vals: list, pct: bool = True) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "-"
    f = (lambda x: f"{x:.0%}") if pct else (lambda x: f"{x:.2f}")
    mean = sum(vals) / len(vals)
    return f(mean) + (f" ({f(min(vals))}-{f(max(vals))})" if len(vals) > 1 and max(vals) > min(vals) else "")


def load_predictions(cases: dict, results) -> dict[str, list[dict]]:
    """predictor -> one {case_id: {port: rating}} per run."""
    preds = {name: [{cid: fn(c) for cid, c in cases.items()}] for name, fn in BASELINES.items()}
    if results.exists():
        runs = defaultdict(lambda: defaultdict(dict))  # predictor -> rep -> case_id -> ratings
        for r in map(json.loads, results.read_text().splitlines()):
            if r.get("status") == "ok" and r["case_id"] in cases:
                runs[r["predictor"]][r["rep"]][r["case_id"]] = r["ratings"]
        for name, reps in runs.items():
            preds[name] = [reps[k] for k in sorted(reps)]
    return preds


def table(title: str, keys: list, cases: dict, preds: dict, good_at: int) -> None:
    print(title)
    print(f"  {'predictor':<22}{'accuracy':<15}{'precision':<15}{'recall':<15}{'within 1':<15}{'error':<19}{'coverage':<10}")
    for name, runs in preds.items():
        ms = [metrics([(cases[cid]["truth"][port], run.get(cid, {}).get(port)) for cid, port in keys], good_at)
              for run in runs]
        print(f"  {name:<22}{fmt([m['accuracy'] for m in ms]):<15}{fmt([m['precision'] for m in ms]):<15}"
              f"{fmt([m['recall'] for m in ms]):<15}{fmt([m['within1'] for m in ms]):<15}"
              f"{fmt([m['error'] for m in ms], pct=False):<19}{fmt([m['coverage'] for m in ms]):<10}"
              + (f"  {len(ms)} runs" if len(ms) > 1 else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=SPLITS, default="dev")
    ap.add_argument("--model", default="claude-sonnet-5-5", help="the agent model whose results to score")
    ap.add_argument("--pilot", action="store_true", help="score the first 10-week pilot instead")
    args = ap.parse_args()
    cases_path = HERE / "cases_pilot.jsonl" if args.pilot else cases_file(args.split)
    results = HERE / "results_pilot.jsonl" if args.pilot else results_file(args.split, args.model)
    cases = {c["case_id"]: c for c in map(json.loads, cases_path.read_text().splitlines())}
    preds = load_predictions(cases, results)
    weeks = len({c["report_id"] for c in cases.values()})
    print(f"{len(cases)} cases ({weeks} weeks x pier/boat) from {cases_path.name}, results from {results.name}")
    if results.exists():
        commits = defaultdict(set)
        for r in map(json.loads, results.read_text().splitlines()):
            commits[r["predictor"]].add(r.get("commit", "before commits were recorded"))
        for name, cs in sorted(commits.items()):
            print(f"  {name}: commit {', '.join(sorted(cs))}" + ("  <- MIXED VERSIONS" if len(cs) > 1 else ""))
    print()
    for m in METHODS:
        rated = [(cid, port) for cid, c in cases.items() if c["method"] == m for port in PORTS if c["truth"][port] is not None]
        same = [(cid, port) for cid, port in rated
                if all(run.get(cid, {}).get(port) is not None for runs in preds.values() for run in runs)]
        base = sum(cases[cid]["truth"][port] >= GOOD[m] for cid, port in same) / len(same) if same else 0
        print(f"== {m} (worth going = rating >= {GOOD[m]}) ==")
        table(f"SAME PORTS: {len(same)} of {len(rated)} rated ports, rated by every predictor "
              f"({base:.0%} worth going in the reports)", same, cases, preds, GOOD[m])
        table(f"ALL PORTS: {len(rated)} rated ports", rated, cases, preds, GOOD[m])
        print()


if __name__ == "__main__":
    main()
