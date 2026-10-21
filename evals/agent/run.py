"""Run the agent on the backtest cases and save each answer's per-port ratings (costs money).

  python -m evals.agent.run                      # every predictor on the dev cases, 2 runs per case, Sonnet 5.5
  python -m evals.agent.run --reps 1 --predictor agent --predictor agent_history
  python -m evals.agent.run --split test         # the held-out weeks, once the agent is settled

Predictors (agent settings): "agent" (as in the demo), "agent_no_reports" (report tools off, conditions only),
and the same two with conditions history on ("_history": GLSEA satellite water near the pier + wind), to measure
whether that data helps.
Each run: run_agent() as of the day before the answer-key report -> the final answer -> Haiku 5.5 reads
the 0-4 rating the answer gives each port (null if it doesn't rate one). One row per (predictor, case, run)
goes to results_<split>_<model>.jsonl as soon as it finishes, so a crash loses nothing and a re-run skips finished rows.
Each row records the git commit it ran on, so results from different agent versions are never mixed up silently.
Every run's full transcript goes to data/runs.db (source 'eval', see agent/runlog.py) and its row here records
the run_id; traces/ holds the transcripts of earlier runs (pilot, first Haiku dev run) as files.
Runs that fail before an answer go to errors.jsonl, never scored as zeros.
"""
import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from agent import runlog
from agent.run import run_agent
from config.pricing import cost, usage_tokens
from evals.agent.common import HERE, PORTS, SPLITS, cases_file, results_file

ERRORS = HERE / "errors.jsonl"
PREDICTORS = {  # name -> run_agent settings
    "agent": {"report_tools": True, "conditions_history": False},
    "agent_no_reports": {"report_tools": False, "conditions_history": False},
    "agent_history": {"report_tools": True, "conditions_history": True},
    "agent_no_reports_history": {"report_tools": False, "conditions_history": True},
}
READER_MODEL = "claude-haiku-5-5"
CASE_TIMEOUT_S = 300


class PortRatings(BaseModel):
    st_joseph: Optional[int] = Field(ge=0, le=4, description="Rating the answer gives St. Joseph, or null")
    south_haven: Optional[int] = Field(ge=0, le=4, description="Rating the answer gives South Haven, or null")
    grand_haven: Optional[int] = Field(ge=0, le=4, description="Rating the answer gives Grand Haven, or null")
    muskegon: Optional[int] = Field(ge=0, le=4, description="Rating the answer gives Muskegon, or null")
    go_to: list[str] = Field(description="Ports the answer says to go to, as written")


READER_SYSTEM = """You read a fishing assistant's answer and record the 0-4 rating it gives each port \
(0 slow, 1 below average, 2 average, 3 good, 4 excellent). Copy the answer's own ratings; do not judge the advice.
- If it gives a range ("1-2"), use the lower number.
- If it describes a port in words but gives no number, map the words: slow/poor 0, a few/below average 1, \
fair/average 2, good 3, excellent 4.
- Null if the answer says nothing about how fishing will be at that port.
- go_to: the ports it recommends going to, as written."""


def read_ratings(client: anthropic.Anthropic, answer: str) -> tuple[dict, dict]:
    resp = client.messages.parse(model=READER_MODEL, max_tokens=4000, system=READER_SYSTEM,
                                 messages=[{"role": "user", "content": f"<answer>\n{answer}\n</answer>"}],
                                 output_format=PortRatings)
    r = resp.parsed_output
    ratings = dict(zip(PORTS, (r.st_joseph, r.south_haven, r.grand_haven, r.muskegon)))
    return {"ratings": ratings, "go_to": r.go_to}, usage_tokens(resp.usage)


def run_one(client, predictor: str, case: dict, rep: int, model: str, split: str, commit: str, run_id: str) -> dict:
    t0 = time.monotonic()
    events, answer, done = [], None, None
    settings = PREDICTORS[predictor]
    stream = runlog.logged(run_agent(case["question"], case["as_of"], model, **settings), source="eval",
                           question=case["question"], as_of=case["as_of"], model=model, settings=settings, run_id=run_id,
                           meta={"split": split, "case_id": case["case_id"], "predictor": predictor, "rep": rep})
    try:
        for e in stream:
            events.append(e)
            if e["type"] == "answer":
                answer = e["text"]
            elif e["type"] == "done":
                done = e
            if time.monotonic() - t0 > CASE_TIMEOUT_S:
                raise TimeoutError(f"case took over {CASE_TIMEOUT_S}s")
    finally:
        stream.close()  # saves the run now, also on a timeout
    if not answer:
        errs = [e["message"] for e in events if e["type"] == "error"]
        raise RuntimeError("no answer: " + "; ".join(errs or ["unknown"]))
    read, reader_usage = read_ratings(client, answer)
    return {"predictor": predictor, "case_id": case["case_id"], "rep": rep, "status": "ok", "model": model,
            "commit": commit, "run_id": run_id, **settings,
            **read, "agent_cost": done["cost"], "reader_cost": cost(READER_MODEL, **reader_usage),
            "model_calls": done["model_calls"],
            "tool_calls": sum(e["type"] == "tool_call" for e in events), "seconds": round(time.monotonic() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictor", action="append", choices=list(PREDICTORS))
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--split", choices=SPLITS, default="dev")
    args = ap.parse_args()

    cases = [json.loads(l) for l in cases_file(args.split).read_text().splitlines()]
    RESULTS = results_file(args.split, args.model)
    commit = runlog.git_commit()
    if commit.endswith("+dirty"):
        print(f"warning: agent code has uncommitted changes ({commit}); results won't map to a commit")
    done = set()
    if RESULTS.exists():
        done = {(r["predictor"], r["case_id"], r["rep"]) for r in map(json.loads, RESULTS.read_text().splitlines())}
    jobs = [(p, c, k) for p in (args.predictor or list(PREDICTORS)) for c in cases for k in range(args.reps)
            if (p, c["case_id"], k) not in done]
    print(f"{len(jobs)} runs to do ({len(done)} already in {RESULTS.name}), model {args.model}, commit {commit}")

    client = anthropic.Anthropic()
    lock, spent = threading.Lock(), 0.0
    with ThreadPoolExecutor(args.workers) as pool:
        jobs = [(p, c, k, runlog.new_id()) for p, c, k in jobs]
        futs = {pool.submit(run_one, client, p, c, k, args.model, args.split, commit, rid): (p, c, k, rid)
                for p, c, k, rid in jobs}
        for fut in as_completed(futs):
            p, c, k, rid = futs[fut]
            try:
                row = fut.result()
                out, line = RESULTS, row
                spent += row["agent_cost"] + row["reader_cost"]
                msg = f"ok  {' '.join('-' if v is None else str(v) for v in row['ratings'].values())}  ${row['agent_cost']:.3f}"
            except Exception as e:  # recorded, not scored
                out, line = ERRORS, {"predictor": p, "case_id": c["case_id"], "rep": k, "split": args.split, "model": args.model,
                                     "commit": commit, "run_id": rid,
                                     "error": f"{type(e).__name__}: {e}"}
                msg = f"ERROR {line['error'][:120]}"
            with lock:
                with out.open("a") as f:
                    f.write(json.dumps(line) + "\n")
            print(f"  {p:<17} {c['case_id']:<14} rep{k}  {msg}   (total ${spent:.2f})", flush=True)
    print(f"\ndone, spent ${spent:.2f}. Score with: python -m evals.agent.score --split {args.split} --model {args.model}")


if __name__ == "__main__":
    main()
