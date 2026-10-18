"""Score extractions in fishing.db against the hand labels in labels/.

Usage:
  python eval.py --model claude-opus-5 [--model claude-haiku-4-5] [--prompt-version v8]
  python eval.py --model claude-opus-5 --errors          # list dev mismatches for error review
  python eval.py --model claude-opus-5 --split test      # final number; mismatches stay hidden

Deterministic, no LLM. Rows are matched by (species, method), then compared field by field.
Two scopes: all species, and salmon only (the species recommendations depend on).
Test-split mismatches are never printed unless --show-test-errors, so the prompt can't be
tuned against them.
"""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from label_tools import is_blank
from schema import PROMPT_VERSION

LABELS = Path("labels")
SALMON = {"chinook", "coho", "salmon_unspecified", "fish_unspecified"}
FIELDS = ["bottom_depth", "fish_depth", "lures", "catch_quality"]


# ---------- loading ----------

def evidence_text(raw) -> str:
    """Evidence is a JSON list of sentences since prompt v10 (plain text before); return display text."""
    if raw and raw.startswith("["):
        return " … ".join(json.loads(raw))
    return raw or ""


def depth(d) -> tuple | None:
    """Label depth {"min_ft", "max_ft"} or model (min, max) -> comparable tuple; all-null -> None."""
    if d is None:
        return None
    t = (d["min_ft"], d["max_ft"]) if isinstance(d, dict) else tuple(d)
    return None if t == (None, None) else t


def load_labels(split: str) -> dict[str, dict]:
    out = {}
    for p in sorted(LABELS.glob("*-*.json")):
        lab = json.loads(p.read_text())
        if lab["status"] != "done" or (split != "all" and lab["split"] != split):
            continue
        lab["rows"] = [{
            "species": o["species"], "method": o["method"],
            "bottom_depth": depth(o["bottom_depth_ft"]), "fish_depth": depth(o["fish_depth_ft"]),
            "lures": frozenset(o["lures"]), "catch_quality": o["catch_quality"],
            "optional": bool(o.get("optional")),  # the text supports both "row" and "no row"
            "lure_details": o.get("lure_details", ""), "notes": o.get("notes", ""),  # shown in review, not scored
        } for o in lab["observations"] if not is_blank(o)]
        out[lab["chunk_id"]] = lab
    return out


def load_model(conn: sqlite3.Connection, model: str, pv: str, chunk_ids) -> dict[str, dict]:
    out = {}
    for cid in chunk_ids:
        e = conn.execute("SELECT * FROM extractions WHERE chunk_id=? AND model=? AND prompt_version=?",
                         (cid, model, pv)).fetchone()
        if e is None:
            continue
        rows = [{
            "species": r["species"], "method": r["method"],
            "bottom_depth": depth((r["bottom_min_ft"], r["bottom_max_ft"])),
            "fish_depth": depth((r["fish_min_ft"], r["fish_max_ft"])),
            "lures": frozenset(json.loads(r["lures"])), "catch_quality": r["catch_quality"],
            "evidence": evidence_text(r["evidence"]), "lure_details": r["lure_details"],
        } for r in conn.execute("SELECT * FROM observations WHERE chunk_id=? AND model=? AND prompt_version=?",
                                (cid, model, pv))]
        out[cid] = {
            "status": e["status"], "rows": rows,
            "access_issues": bool(e["access_issues"]),
            "no_fishing_info": bool(e["no_fishing_info"]) if e["no_fishing_info"] is not None else None,
            "cost": e["cost_usd"] or 0.0, "latency_ms": e["latency_ms"] or 0,
            "in_tok": e["input_tokens"] or 0, "out_tok": e["output_tokens"] or 0,
        }
    return out


# ---------- comparison ----------

def quality_ok(label, pred) -> bool:
    accepted = label if isinstance(label, list) else [label]
    return pred in accepted


def quality_within1(label, pred) -> bool:
    accepted = [q for q in (label if isinstance(label, list) else [label]) if q is not None]
    if pred is None or not accepted:
        return quality_ok(label, pred)
    return any(abs(pred - q) <= 1 for q in accepted)


def score_chunk(lab: dict, pred: dict | None, scope: set | None, m: Counter, errors: list) -> None:
    keep = (lambda r: r["species"] in scope) if scope else (lambda r: True)
    gold = [r for r in lab["rows"] if keep(r)]
    got = [r for r in (pred["rows"] if pred else []) if keep(r)]

    gold_by = {(r["species"], r["method"]): r for r in gold}
    got_by: dict[tuple, dict] = {}
    for r in got:
        k = (r["species"], r["method"])
        if k in got_by:
            m["duplicate_rows"] += 1
            m["rows_extra"] += 1
            errors.append(("duplicate row", k, None, r))
        else:
            got_by[k] = r

    for k, g in gold_by.items():
        p = got_by.get(k)
        if p is None:
            if g.get("optional"):  # model left out a row the label marks optional: not a miss
                m["optional_skipped"] += 1
                continue
            m["rows_missed"] += 1
            errors.append(("missed row", k, g, None))
            continue
        m["rows_matched"] += 1
        checks = {
            "bottom_depth": g["bottom_depth"] == p["bottom_depth"],
            "fish_depth": g["fish_depth"] == p["fish_depth"],
            "lures": g["lures"] == p["lures"],
            "catch_quality": quality_ok(g["catch_quality"], p["catch_quality"]),
        }
        for f, ok in checks.items():
            m[f"{f}_ok"] += ok
            if not ok:
                errors.append((f"wrong {f}", k, g, p))
        m["catch_quality_within1"] += quality_within1(g["catch_quality"], p["catch_quality"])
        m["lures_tp"] += len(g["lures"] & p["lures"])
        m["lures_fp"] += len(p["lures"] - g["lures"])
        m["lures_fn"] += len(g["lures"] - p["lures"])
        # the confusion the prompt warns about: water depth and lure depth swapped
        if (g["bottom_depth"] != g["fish_depth"] and g["bottom_depth"] == p["fish_depth"]
                and g["fish_depth"] == p["bottom_depth"]):
            m["depth_swaps"] += 1
    for k, p in got_by.items():
        if k not in gold_by:
            m["rows_extra"] += 1
            errors.append(("extra row", k, None, p))

    if scope is None and pred is not None:  # chunk-level fields only make sense on the full chunk
        m["chunks_scored"] += 1
        for f in ("access_issues", "no_fishing_info"):
            ok = lab[f] == pred[f]
            m[f"{f}_ok"] += ok
            if not ok:
                errors.append((f"wrong {f}", None, lab[f], pred[f]))


def pct(a, b) -> str:
    return f"{100 * a / b:5.1f}%" if b else "   n/a"


def f1(tp, fp, fn) -> str:
    return pct(2 * tp, 2 * tp + fp + fn)


def evaluate(conn, model: str, pv: str, labels: dict) -> tuple[dict, dict]:
    preds = load_model(conn, model, pv, labels)
    results, errors = {}, defaultdict(list)
    for scope_name, scope in (("all species", None), ("salmon only", SALMON)):
        m = Counter()
        for cid, lab in labels.items():
            errs: list = []
            score_chunk(lab, preds.get(cid), scope, m, errs)
            if scope is None:
                errors[cid] = errs
        results[scope_name] = m
    meta = {
        "extracted": len(preds), "labeled": len(labels),
        "ok": sum(p["status"] == "ok" for p in preds.values()),
        "cost": sum(p["cost"] for p in preds.values()),
        "latency_ms": sorted(p["latency_ms"] for p in preds.values()),
        "in_tok": sum(p["in_tok"] for p in preds.values()), "out_tok": sum(p["out_tok"] for p in preds.values()),
    }
    return {"metrics": results, "meta": meta}, errors


# ---------- reporting ----------

def report(name: str, res: dict) -> None:
    meta = res["meta"]
    lat = meta["latency_ms"]
    print(f"\n=== {name} ===")
    print(f"extracted {meta['extracted']}/{meta['labeled']} labeled chunks ({meta['ok']} status ok)", end="")
    if lat:
        n = meta["extracted"]
        print(f" | ${meta['cost']:.3f} total, ${meta['cost'] / n:.4f}/chunk | "
              f"latency median {lat[len(lat) // 2] / 1000:.1f}s, p90 {lat[int(len(lat) * 0.9)] / 1000:.1f}s | "
              f"tokens/chunk in {meta['in_tok'] // n}, out {meta['out_tok'] // n}")
    else:
        print()
    for scope, m in res["metrics"].items():
        matched = m["rows_matched"]
        gold = matched + m["rows_missed"]
        pred = matched + m["rows_extra"]
        print(f"  [{scope}]  rows: recall {pct(matched, gold)} ({matched}/{gold})  "
              f"precision {pct(matched, pred)} ({matched}/{pred})  duplicates {m['duplicate_rows']}")
        print(f"    on matched rows:  bottom depth {pct(m['bottom_depth_ok'], matched)}  "
              f"fish depth {pct(m['fish_depth_ok'], matched)}  depth swaps {m['depth_swaps']}")
        print(f"                      lures exact {pct(m['lures_ok'], matched)}  "
              f"lure-type F1 {f1(m['lures_tp'], m['lures_fp'], m['lures_fn'])}  "
              f"catch quality {pct(m['catch_quality_ok'], matched)} (within 1: {pct(m['catch_quality_within1'], matched)})")
        if m["chunks_scored"]:
            c = m["chunks_scored"]
            print(f"    chunk level:      access {pct(m['access_issues_ok'], c)}  no-info {pct(m['no_fishing_info_ok'], c)}")


def fmt_row(r) -> str:
    if r is None:
        return "-"
    if not isinstance(r, dict):
        return repr(r)
    lures = ",".join(sorted(r["lures"])) or "-"
    return f"bottom={r['bottom_depth']} fish={r['fish_depth']} lures={lures} q={r['catch_quality']}"


def print_errors(errors: dict, labels: dict, show_test: bool) -> None:
    print("\n--- mismatches ---")
    kinds = Counter()
    for cid, errs in errors.items():
        if not errs:
            continue
        if labels[cid]["split"] == "test" and not show_test:
            continue
        print(f"\n{cid} {labels[cid]['port']} {labels[cid]['date']} ({labels[cid]['split']})")
        print("  " + " ".join(labels[cid]["text"]))
        for kind, key, g, p in errs:
            kinds[kind] += 1
            k = f"{key[0]}/{key[1]}" if key else ""
            print(f"  ✗ {kind:<22} {k:<28} label: {fmt_row(g)}")
            print(f"    {'':<22} {'':<28} model: {fmt_row(p)}")
            if isinstance(p, dict) and p.get("evidence"):
                print(f"    {'':<22} {'':<28} evidence: {p['evidence'][:140]}")
    print("\nmismatch counts:", dict(kinds.most_common()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", required=True)
    ap.add_argument("--prompt-version", default=PROMPT_VERSION)
    ap.add_argument("--split", choices=["dev", "test", "all"], default="all")
    ap.add_argument("--errors", action="store_true", help="list mismatches (dev chunks only)")
    ap.add_argument("--show-test-errors", action="store_true", help="also list test mismatches (final review only)")
    ap.add_argument("--db", default="fishing.db")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    splits = ["dev", "test"] if args.split == "all" else [args.split]
    for model in args.model:
        for split in splits:
            labels = load_labels(split)
            res, errors = evaluate(conn, model, args.prompt_version, labels)
            report(f"{model} / prompt {args.prompt_version} / {split} ({len(labels)} chunks)", res)
            if args.errors:
                print_errors(errors, labels, args.show_test_errors)


if __name__ == "__main__":
    main()
