"""Every agent run (web page, CLI, eval) saved to data/runs.db in one format: a summary row + every event.

  python -m agent.runlog              # the 20 most recent runs
  python -m agent.runlog show RUN_ID  # one run's events, in order

Wrap run_agent()'s event stream with logged(): events pass through unchanged, and when the run ends (answer,
error, crash, or the caller stops early, e.g. the browser closed) the run is written. Eval runs carry their case
in `meta`; how a run was scored stays in the eval's results file, which records the run_id.
Not committed to git: it holds real users' questions and changes with every run.
"""
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache

from config.paths import RUNS_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY, started_at TEXT, source TEXT, question TEXT, as_of TEXT, settings TEXT, meta TEXT,
    commit_id TEXT, model TEXT, answered_by TEXT, status TEXT, error TEXT, answer TEXT,
    model_calls INTEGER, tool_calls INTEGER, tool_errors INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    cost REAL, seconds REAL);
CREATE TABLE IF NOT EXISTS agent_run_events (run_id TEXT, seq INTEGER, t REAL, type TEXT, data TEXT,
    PRIMARY KEY (run_id, seq));
CREATE INDEX IF NOT EXISTS runs_started ON agent_runs(started_at);"""
TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(RUNS_DB, timeout=30)  # eval runs save from several threads
    conn.executescript(SCHEMA)
    return conn


@lru_cache(maxsize=None)
def git_commit() -> str:
    """Short commit id of the code this process loaded, '+dirty' if agent/ or conditions/ had uncommitted changes."""
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "agent", "conditions"], capture_output=True, text=True).stdout.strip()
    return sha + ("+dirty" if dirty else "")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def status_of(events: list[dict], crash: str | None) -> tuple[str, str | None]:
    """ok | truncated | refused | max_turns | error | incomplete (stopped by the caller before an answer)."""
    if crash:
        return "error", crash
    errors = [e["message"] for e in events if e["type"] == "error"]
    if errors:
        msg = "; ".join(errors)
        return ("refused" if "declined" in msg else "max_turns" if "stopped after" in msg else "error"), msg
    answer = next((e for e in events if e["type"] == "answer"), None)
    if answer:
        return ("truncated" if answer["truncated"] else "ok"), None
    return "incomplete", None


def save(run_id: str, started: float, timed: list[tuple[float, dict]], crash: str | None, *, source: str,
         question: str, as_of: str, model: str, settings: dict, meta: dict | None) -> None:
    events = [e for _, e in timed]
    calls = [e for e in events if e["type"] == "model_call"]
    status, error = status_of(events, crash)
    answer = next((e["text"] for e in events if e["type"] == "answer"), None)
    row = {"run_id": run_id, "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
           "source": source, "question": question, "as_of": as_of, "settings": json.dumps(settings),
           "meta": json.dumps(meta) if meta else None, "commit_id": git_commit(), "model": model,
           "answered_by": ",".join(sorted({e["model"] for e in calls})) or None, "status": status, "error": error,
           "answer": answer, "model_calls": len(calls), "tool_calls": sum(e["type"] == "tool_call" for e in events),
           "tool_errors": sum(e["type"] == "tool_result" and e["is_error"] for e in events),
           **{k: sum(e[k] for e in calls) for k in TOKENS}, "cost": sum(e["cost"] for e in calls),
           "seconds": round(time.time() - started, 1)}
    conn = connect()
    with conn:
        conn.execute(f"INSERT INTO agent_runs ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        conn.executemany("INSERT INTO agent_run_events VALUES (?,?,?,?,?)",
                         [(run_id, i, round(t, 2), e["type"], json.dumps(e)) for i, (t, e) in enumerate(timed)])
    conn.close()


def logged(events, *, source: str, question: str, as_of: str, model: str, settings: dict,
           meta: dict | None = None, run_id: str | None = None):
    """Pass run_agent()'s events through and save the run when it ends, however it ends. Exceptions are re-raised."""
    run_id, started, timed, crash = run_id or new_id(), time.time(), [], None
    try:
        for e in events:
            timed.append((time.time() - started, e))
            yield e
    except GeneratorExit:  # the caller stopped reading (browser closed, eval timeout): saved as incomplete
        raise
    except Exception as ex:
        crash = f"{type(ex).__name__}: {ex}"
        raise
    finally:
        try:
            save(run_id, started, timed, crash, source=source, question=question, as_of=as_of, model=model,
                 settings=settings, meta=meta)
        except Exception as ex:  # logging must never break an answer
            print(f"runlog: could not save run {run_id}: {ex}", file=sys.stderr)


def recent(n: int = 20) -> None:
    rows = connect().execute("""SELECT run_id, started_at, source, model, status, model_calls, tool_calls, cost, seconds,
                                       question FROM agent_runs ORDER BY started_at DESC LIMIT ?""", (n,)).fetchall()
    for r in rows:
        print(f"{r[0]}  {r[1][:16]}  {r[2]:<4} {r[3]:<18} {r[4]:<10} {r[5]} calls {r[6]} tools  ${r[7]:.3f}  {r[8]:>5.0f}s  "
              f"{r[9][:60]!r}")


def show(run_id: str) -> None:
    conn = connect()
    run = conn.execute("SELECT source, question, model, status, cost, seconds FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        sys.exit(f"no run {run_id}")
    print(f"{run_id}: {run[0]}, {run[2]}, {run[3]}, ${run[4]:.3f}, {run[5]:.0f}s\nQ: {run[1]}\n")
    for t, typ, data in conn.execute("SELECT t, type, data FROM agent_run_events WHERE run_id=? ORDER BY seq", (run_id,)):
        e = json.loads(data)
        detail = {"tool_call": lambda: f"{e['name']}({json.dumps(e['input'])})",
                  "tool_result": lambda: f"{e['name']}: {e['chars']:,} chars{' ERROR' if e['is_error'] else ''}",
                  "model_call": lambda: f"{e['model']}: {e['input_tokens']:,} in / {e['output_tokens']:,} out, ${e['cost']:.4f}",
                  "answer": lambda: e["text"][:300].replace("\n", " ") + "...",
                  "error": lambda: e["message"]}.get(typ, lambda: "")()
        print(f"{t:6.1f}s  {typ:<12} {detail}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "show":
        show(sys.argv[2])
    elif len(sys.argv) == 1:
        recent()
    else:
        print(__doc__)
