"""Self-test for score.py (the extraction scorer): no API calls.

Builds a temporary DB with two fake "models" made from the hand labels:
  perfect: an exact copy of the labels      -> must score 100% everywhere
  flawed:  the copy with known, planted bugs -> the metrics must count exactly those bugs

Run from the project root: python -m evals.extraction.selftest
"""
import copy
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from evals.extraction import score as ev
from extraction.extract import SCHEMA_SQL

PV = "test"


def insert(conn, model, cid, lab, rows):
    conn.execute("INSERT INTO extractions (chunk_id, model, prompt_version, status, attempts, input_tokens, output_tokens,"
                 " latency_ms, cost_usd, conditions, access_issues, no_fishing_info, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, model, PV, "ok", 1, 3000, 400, 5000, 0.02, None,
                  "launch closed" if lab["access_issues"] else None, int(lab["no_fishing_info"]), None))
    for r in rows:
        b, f = r["bottom_depth"] or (None, None), r["fish_depth"] or (None, None)
        q = r["catch_quality"][0] if isinstance(r["catch_quality"], list) else r["catch_quality"]
        conn.execute(
            "INSERT INTO observations (chunk_id, model, prompt_version, species, method, bottom_min_ft, bottom_max_ft,"
            " fish_min_ft, fish_max_ft, lures, lure_details, catch_quality, evidence, evidence_ok)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, model, PV, r["species"], r["method"], *b, *f, json.dumps(sorted(r["lures"])), None, q, "x", 1))


def main() -> None:
    labels = ev.load_labels("all")
    with tempfile.TemporaryDirectory() as d:
        conn = sqlite3.connect(Path(d) / "t.db")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)

        for cid, lab in labels.items():
            insert(conn, "perfect", cid, lab, lab["rows"])

        # plant bugs in "flawed", each in a different chunk, and record what we expect
        ids = sorted(labels)
        swap_cid = next(c for c in ids if any(r["bottom_depth"] and r["fish_depth"]
                                              and r["bottom_depth"] != r["fish_depth"] for r in labels[c]["rows"]))
        multi = [c for c in ids if len(labels[c]["rows"]) >= 2 and c != swap_cid]
        miss_cid, extra_cid, qual_cid, lure_cid, dup_cid = multi[:5]
        noinfo_cid = next(c for c in ids if c not in (swap_cid, *multi[:5]))
        for cid, lab in labels.items():
            rows = copy.deepcopy(lab["rows"])
            lab2 = copy.deepcopy(lab)
            if cid == swap_cid:  # swap water depth and lure depth on one row
                r = next(r for r in rows if r["bottom_depth"] and r["fish_depth"] and r["bottom_depth"] != r["fish_depth"])
                r["bottom_depth"], r["fish_depth"] = r["fish_depth"], r["bottom_depth"]
            if cid == miss_cid:  # model misses a row
                rows.pop()
            if cid == extra_cid:  # model invents a row
                rows.append({"species": "pink", "method": "shore", "bottom_depth": None, "fish_depth": None,
                             "lures": frozenset(), "catch_quality": 4})
            if cid == qual_cid:  # quality off by more than one, outside any accepted range
                r = rows[0]
                accepted = r["catch_quality"] if isinstance(r["catch_quality"], list) else [r["catch_quality"]]
                r["catch_quality"] = next(q for q in (4, 0) if all(a is None or abs(q - a) > 1 for a in accepted))
            if cid == lure_cid:  # one extra lure type
                rows[0]["lures"] = rows[0]["lures"] | {"other"} if "other" not in rows[0]["lures"] else frozenset()
            if cid == dup_cid:  # same (species, method) twice
                rows.append(copy.deepcopy(rows[0]))
            if cid == noinfo_cid:
                lab2["no_fishing_info"] = not lab["no_fishing_info"]
            insert(conn, "flawed", cid, lab2, rows)
        conn.commit()

        n_rows = sum(len(l["rows"]) for l in labels.values())
        res, _ = ev.evaluate(conn, "perfect", PV, labels)
        m = res["metrics"]["all species"]
        assert m["rows_matched"] == n_rows and m["rows_missed"] == 0 and m["rows_extra"] == 0, m
        for f in ev.FIELDS:
            assert m[f"{f}_ok"] == n_rows, (f, m)
        assert m["depth_swaps"] == 0 and m["no_fishing_info_ok"] == len(labels)
        print(f"perfect: 100% on {n_rows} rows / {len(labels)} chunks  ✓")

        res, errors = ev.evaluate(conn, "flawed", PV, labels)
        m = res["metrics"]["all species"]
        expect = {
            "rows_missed": 1, "rows_extra": 2, "duplicate_rows": 1, "depth_swaps": 1,
            "rows_matched": n_rows - 1,
            "bottom_depth_ok": n_rows - 2, "fish_depth_ok": n_rows - 2,  # the swapped row fails both
            "catch_quality_ok": n_rows - 2, "lures_ok": n_rows - 2,       # one planted + the missed row isn't matched
            "no_fishing_info_ok": len(labels) - 1,
        }
        # the missed row is unmatched, so "matched rows" = n_rows - 1 and each field has exactly one planted failure
        expect.update({k: n_rows - 1 - 1 for k in ("bottom_depth_ok", "fish_depth_ok", "catch_quality_ok", "lures_ok")})
        for k, v in expect.items():
            assert m[k] == v, f"{k}: expected {v}, got {m[k]}"
        kinds = sorted({e[0] for errs in errors.values() for e in errs})
        print(f"flawed: every planted bug counted exactly once  ✓   ({', '.join(kinds)})")

        # salmon-only scope must be a subset of all-species counts
        s = res["metrics"]["salmon only"]
        assert s["rows_matched"] <= m["rows_matched"] and s["rows_missed"] <= m["rows_missed"]
        print("salmon-only scope is consistent with all-species  ✓")

        # optional rows: absent -> not missed; present -> matched and scored, not extra
        cid = next(c for c in ids if len(labels[c]["rows"]) >= 2)
        lab = copy.deepcopy(labels[cid])
        lab["rows"][0]["optional"] = True
        one = {cid: lab}
        insert(conn, "opt_absent", cid, lab, lab["rows"][1:])
        insert(conn, "opt_present", cid, lab, lab["rows"])
        conn.commit()
        a = ev.evaluate(conn, "opt_absent", PV, one)[0]["metrics"]["all species"]
        b = ev.evaluate(conn, "opt_present", PV, one)[0]["metrics"]["all species"]
        n = len(lab["rows"])
        assert a["rows_missed"] == 0 and a["rows_extra"] == 0 and a["rows_matched"] == n - 1 and a["optional_skipped"] == 1, a
        assert b["rows_missed"] == 0 and b["rows_extra"] == 0 and b["rows_matched"] == n, b
        print("optional rows: not missed when absent, matched when present  ✓")
        conn.close()


if __name__ == "__main__":
    main()
