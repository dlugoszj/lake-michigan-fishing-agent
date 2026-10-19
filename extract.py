"""LLM structured extraction of port chunks into SQLite.

Usage:
  python extract.py --model claude-opus-5 [--limit 5] [--all-regions] [--chunk-ids a,b,c]

Each chunk is extracted with structured outputs (schema-constrained decoding + Pydantic
validation). A deterministic check then verifies every `evidence` sentence actually appears
in the source text; on failure we retry once with the error fed back. Every row records
model, prompt version, tokens, and latency so models can be compared side by side.
"""
import argparse
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from schema import PROMPT_VERSION, PROMPTS, ChunkExtraction, user_prompt

load_dotenv()  # reads ANTHROPIC_API_KEY from .env

DB = Path("fishing.db")
# $ per 1M tokens (input, output)
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100K tokens
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reports (report_id TEXT PRIMARY KEY, url TEXT, date TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS port_chunks (
  chunk_id TEXT PRIMARY KEY, report_id TEXT, date TEXT, region TEXT, port TEXT,
  lake_port INTEGER, raw_text TEXT);
CREATE TABLE IF NOT EXISTS extractions (
  chunk_id TEXT, model TEXT, prompt_version TEXT, status TEXT, attempts INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER, cost_usd REAL,
  conditions TEXT, access_issues TEXT, no_fishing_info INTEGER, error TEXT,
  PRIMARY KEY (chunk_id, model, prompt_version));
CREATE TABLE IF NOT EXISTS observations (
  chunk_id TEXT, model TEXT, prompt_version TEXT, species TEXT, method TEXT,
  bottom_min_ft INTEGER, bottom_max_ft INTEGER, fish_min_ft INTEGER, fish_max_ft INTEGER,
  lures TEXT, lure_details TEXT, catch_quality INTEGER, evidence TEXT, evidence_ok INTEGER);
CREATE INDEX IF NOT EXISTS obs_chunk ON observations(chunk_id, model, prompt_version);
"""


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def bad_evidence(ext: ChunkExtraction, text: str) -> list[str]:
    """Evidence sentences that don't appear verbatim (whitespace/case-insensitive) in the source."""
    src = norm(text)
    return [s for o in ext.observations for s in o.evidence if norm(s) not in src]


def load_inputs(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
    if "lure_details" not in cols:  # db created before prompt v7
        conn.execute("ALTER TABLE observations ADD COLUMN lure_details TEXT")
    for r in json.loads(Path("reports.json").read_text()):
        conn.execute("INSERT OR REPLACE INTO reports VALUES (?,?,?,?)",
                     (r["report_id"], r["url"], r["date"], r["title"]))
    for line in Path("chunks.jsonl").read_text().splitlines():
        c = json.loads(line)
        conn.execute("INSERT OR REPLACE INTO port_chunks VALUES (?,?,?,?,?,?,?)",
                     (c["chunk_id"], c["report_id"], c["date"], c["region"], c["port"],
                      int(c["lake_port"]), c["raw_text"]))
    conn.commit()


def extract_one(client: anthropic.Anthropic, model: str, chunk: dict, system: str) -> dict:
    messages = [{"role": "user", "content": user_prompt(chunk["port"], chunk["date"], chunk["raw_text"])}]
    in_tok = out_tok = 0
    t0 = time.monotonic()
    ext, error, status = None, None, "ok"
    for attempt in (1, 2):
        try:
            resp = client.messages.parse(
                model=model, max_tokens=8000, system=system,
                messages=messages, output_format=ChunkExtraction,
            )
        except anthropic.APIStatusError as e:
            error, status = f"{e.status_code}: {e.message}", "api_error"
            break
        except Exception as e:  # Pydantic validation (client-side constraints like 0-4)
            error, status = str(e)[:500], "invalid"
            if attempt == 1:
                messages.append({"role": "user", "content": f"Your previous output failed validation: {error}. Try again."})
                continue
            break
        in_tok += resp.usage.input_tokens
        out_tok += resp.usage.output_tokens
        if resp.stop_reason != "end_turn" or resp.parsed_output is None:
            error, status = f"stop_reason={resp.stop_reason}", "invalid"
            break
        ext = resp.parsed_output
        bad = bad_evidence(ext, chunk["raw_text"])
        if not bad:
            status, error = "ok", None
            break
        status, error = "evidence_mismatch", f"not verbatim: {bad}"
        if attempt == 1:
            messages += [
                {"role": "assistant", "content": ext.model_dump_json()},
                {"role": "user", "content": f"These evidence sentences do not appear verbatim in the report text: {bad}. "
                                            "Copy each sentence exactly as its own list item and return the full extraction again."},
            ]
    p_in, p_out = PRICES.get(model, (0, 0))
    return {
        "chunk": chunk, "ext": ext, "status": status, "error": error, "attempts": attempt,
        "in_tok": in_tok, "out_tok": out_tok, "latency_ms": int((time.monotonic() - t0) * 1000),
        "cost": (in_tok * p_in + out_tok * p_out) / 1e6,
    }


def save(conn: sqlite3.Connection, model: str, r: dict, pv: str) -> None:
    cid, ext = r["chunk"]["chunk_id"], r["ext"]
    key = (cid, model, pv)
    conn.execute("DELETE FROM observations WHERE chunk_id=? AND model=? AND prompt_version=?", key)
    conn.execute(
        "INSERT OR REPLACE INTO extractions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*key, r["status"], r["attempts"], r["in_tok"], r["out_tok"], r["latency_ms"], r["cost"],
         None,  # conditions: dropped in v10, will come from weather data
         ext.access_issues if ext else None,
         int(ext.no_fishing_info) if ext else None, r["error"]))
    if ext:
        src = norm(r["chunk"]["raw_text"])
        for o in ext.observations:
            b, f = o.bottom_depth_ft, o.fish_depth_ft
            conn.execute(
                "INSERT INTO observations (chunk_id, model, prompt_version, species, method, bottom_min_ft, bottom_max_ft,"
                " fish_min_ft, fish_max_ft, lures, lure_details, catch_quality, evidence, evidence_ok)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*key, o.species, o.method,
                 b.min_ft if b else None, b.max_ft if b else None,
                 f.min_ft if f else None, f.max_ft if f else None,
                 json.dumps(o.lures), o.lure_details, o.catch_quality, json.dumps(o.evidence),
                 int(all(norm(s) in src for s in o.evidence))))
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--all-regions", action="store_true", help="include non-SWLP and inland chunks")
    ap.add_argument("--chunk-ids", help="comma-separated chunk ids (e.g. the eval set)")
    ap.add_argument("--redo", action="store_true", help="re-extract chunks already done for this model/prompt")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--prompt-version", default=PROMPT_VERSION, choices=sorted(PROMPTS))
    args = ap.parse_args()

    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    load_inputs(conn)

    q = "SELECT * FROM port_chunks WHERE port IS NOT NULL"
    params: list = []
    if args.chunk_ids:
        ids = args.chunk_ids.split(",")
        q += f" AND chunk_id IN ({','.join('?' * len(ids))})"
        params += ids
    elif not args.all_regions:
        q += " AND lake_port = 1"
    if not args.redo:
        q += " AND chunk_id NOT IN (SELECT chunk_id FROM extractions WHERE model=? AND prompt_version=? AND status='ok')"
        params += [args.model, args.prompt_version]
    q += " ORDER BY date, chunk_id"
    if args.limit:
        q += f" LIMIT {args.limit}"
    chunks = [dict(r) for r in conn.execute(q, params)]
    print(f"{len(chunks)} chunks to extract with {args.model} (prompt {args.prompt_version})")

    client = anthropic.Anthropic()
    totals = {"cost": 0.0, "ok": 0}
    with ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(extract_one, client, args.model, c, PROMPTS[args.prompt_version]) for c in chunks]
        for fut in as_completed(futures):
            r = fut.result()
            save(conn, args.model, r, args.prompt_version)
            totals["cost"] += r["cost"]
            totals["ok"] += r["status"] == "ok"
            n_obs = len(r["ext"].observations) if r["ext"] else 0
            print(f"  {r['chunk']['date']} {r['chunk']['port']:<18} {r['status']:<17} "
                  f"obs={n_obs} {r['latency_ms']}ms ${r['cost']:.4f}" + (f"  {r['error'][:100]}" if r["error"] else ""))
    print(f"\n{totals['ok']}/{len(chunks)} ok, total ${totals['cost']:.3f}")


if __name__ == "__main__":
    main()
