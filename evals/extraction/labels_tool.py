"""Eval set tooling: pick chunks to hand-label and validate the label files.

Usage:
  python -m evals.extraction.labels_tool select   # writes labels/<chunk_id>.json templates (won't overwrite existing)
  python -m evals.extraction.labels_tool check    # validates every label file against LABELING_GUIDE.md rules

Selection is deterministic (fixed seed): a few hand-picked hard cases, then round-robin
over (port, season) strata so every port and season is covered. Every 3rd chunk goes to
a held-out test split that we don't look at while tuning the prompt.
"""
import json
import re
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import get_args

from extraction.schema import Lure, Method, Species
from config.paths import CHUNKS, LABELS, REPORTS

N_TOTAL = 30
SEED = 19  # demo day
# Already extracted during the smoke test and seen by the labeler -> would bias labels
EXCLUDE_REPORTS = {"32cff90"}
# Hard cases we want regardless of the draw, as (report_id, port, reason)
MUST_INCLUDE_BY = [
    ("3b980f9", "Black River", "Word-pasted HTML, un-bolded port name"),
    ("3df50cf", "Muskegon Lake", "inland lake, perch only"),
    ("3f1ef58", "South Haven", "rough lake, few boats out"),
    ("3f0b284", "South Haven", "'fish' with no species named"),
    ("3fa205d", "Muskegon", "steelhead only, end of season"),
    ("41827ed", "St. Joseph River", "walleye only, river"),
    ("3f7335a", "St. Joseph River", "river salmon, short"),
]


N_BLANK_OBS = 4  # fixed count so the number of blanks doesn't hint at the answer


def blank_obs() -> dict:
    return {"species": "", "method": "", "bottom_depth_ft": None, "fish_depth_ft": None,
            "lures": [], "lure_details": "", "catch_quality": None, "notes": ""}


def is_blank(o: dict) -> bool:
    """Unused pre-filled rows are ignored by check and eval."""
    return all(not v for v in o.values())


def sentences(text: str) -> list[str]:
    """Split into sentences for readability; don't break on abbreviations like 'St. Joseph'."""
    parts = re.split(r"(?<!\bSt\.)(?<!\bMt\.)(?<!\bNo\.)(?<!\bft\.)(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text.strip())
    return [s for p in parts for s in p.split("\n") if s.strip()]


def season(date: str) -> str:
    m = int(date[5:7])
    return "spring" if m <= 5 else "summer" if m <= 8 else "fall"


def select() -> None:
    chunks = [json.loads(l) for l in CHUNKS.read_text().splitlines()]
    reports = {r["report_id"]: r for r in json.loads(REPORTS.read_text())}
    pool = [c for c in chunks if c["lake_port"] and c["report_id"] not in EXCLUDE_REPORTS]

    picked: list[tuple[dict, str]] = []
    for rid, port, why in MUST_INCLUDE_BY:
        c = next(c for c in pool if c["report_id"] == rid and c["port"] == port)
        picked.append((c, why))

    rng = random.Random(SEED)
    strata = defaultdict(list)
    for c in pool:
        if c not in [p[0] for p in picked]:
            strata[(c["port"], season(c["date"]))].append(c)
    for s in strata.values():
        rng.shuffle(s)
    keys = sorted(strata)
    rng.shuffle(keys)
    while len(picked) < N_TOTAL and any(strata.values()):
        for k in keys:
            if strata[k] and len(picked) < N_TOTAL:
                picked.append((strata[k].pop(), f"stratum {k[0]} / {k[1]}"))

    # split within each port so dev and test both cover every port
    by_port = defaultdict(list)
    for c, why in picked:
        by_port[c["port"]].append((c, why))
    assigned = []
    for port in sorted(by_port):
        rng.shuffle(by_port[port])
        assigned += [(c, why, "test" if k % 3 == 2 else "dev") for k, (c, why) in enumerate(by_port[port])]

    LABELS.mkdir(exist_ok=True)
    manifest, written = [], 0
    for c, why, split in assigned:
        manifest.append({"chunk_id": c["chunk_id"], "port": c["port"], "date": c["date"],
                         "season": season(c["date"]), "split": split, "why": why})
        path = LABELS / f"{c['chunk_id']}.json"
        if path.exists():
            continue
        template = {
            "chunk_id": c["chunk_id"],
            "port": c["port"],
            "date": c["date"],
            "url": reports[c["report_id"]]["url"],
            "split": split,
            "text": sentences(c["raw_text"]),  # for reading only; eval uses the original from chunks
            "status": "todo",
            "no_fishing_info": None,
            "access_issues": False,
            "observations": [blank_obs() for _ in range(N_BLANK_OBS)],
            "notes": "",
            "_observation_template": blank_obs(),
        }
        path.write_text(json.dumps(template, indent=2) + "\n")
        written += 1
    manifest.sort(key=lambda m: (m["split"], m["date"]))
    (LABELS / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")

    print(f"{len(picked)} chunks selected, {written} new templates in {LABELS}/")
    by = defaultdict(int)
    for m in manifest:
        by[(m["split"], m["port"], m["season"])] += 1
    for k in sorted(by):
        print(f"  {k[0]:<5} {k[1]:<18} {k[2]:<7} {by[k]}")


def check_depth(v, where: str, errs: list) -> None:
    if v is None:
        return
    if not isinstance(v, dict) or set(v) != {"min_ft", "max_ft"}:
        errs.append(f"{where}: must be null or {{\"min_ft\": int|null, \"max_ft\": int|null}}")
        return
    lo, hi = v["min_ft"], v["max_ft"]
    for x in (lo, hi):
        if x is not None and not isinstance(x, int):
            errs.append(f"{where}: {x!r} is not an integer")
    if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
        errs.append(f"{where}: min {lo} > max {hi}")


def check() -> None:
    species, methods = set(get_args(Species)), set(get_args(Method))
    lures = set(get_args(Lure))
    files = sorted(p for p in LABELS.glob("*.json") if p.name != "manifest.json")
    done = 0
    bad = 0
    for p in files:
        lab = json.loads(p.read_text())
        errs: list[str] = []
        if lab["status"] not in ("todo", "done", "skip"):
            errs.append(f"status must be todo/done/skip, got {lab['status']!r}")
        if lab["status"] != "done":
            continue
        done += 1
        if not isinstance(lab["no_fishing_info"], bool):
            errs.append("no_fishing_info must be true/false")
        if not isinstance(lab["access_issues"], bool):
            errs.append("access_issues must be true/false")
        seen = set()
        filled = [(i, o) for i, o in enumerate(lab["observations"]) if not is_blank(o)]
        for i, o in filled:
            w = f"observations[{i}]"
            if o.get("species") not in species:
                errs.append(f"{w}.species {o.get('species')!r} not in {sorted(species)}")
            if o.get("method") not in methods:
                errs.append(f"{w}.method {o.get('method')!r} not in {sorted(methods)}")
            key = (o.get("species"), o.get("method"))
            if key in seen:
                errs.append(f"{w}: duplicate (species, method) {key}; merge into one observation")
            seen.add(key)
            if not isinstance(o.get("optional", False), bool):
                errs.append(f"{w}.optional must be true or false")
            check_depth(o.get("bottom_depth_ft"), f"{w}.bottom_depth_ft", errs)
            check_depth(o.get("fish_depth_ft"), f"{w}.fish_depth_ft", errs)
            if not isinstance(o.get("lures"), list):
                errs.append(f"{w}.lures must be a list")
            else:
                for x in o["lures"]:
                    if x not in lures:
                        errs.append(f"{w}.lures: {x!r} not in {sorted(lures)}; put colors/sizes/rigging in lure_details")
            if not isinstance(o.get("lure_details", ""), str):
                errs.append(f"{w}.lure_details must be text (\"\" if none)")
            q = o.get("catch_quality")
            qs = q if isinstance(q, list) else [q]
            if not all(x is None or (isinstance(x, int) and 0 <= x <= 4) for x in qs):
                errs.append(f"{w}.catch_quality must be 0-4, null, or a list of acceptable values")
        if lab["no_fishing_info"] and filled:
            errs.append("no_fishing_info is true but there are observations")
        if errs:
            bad += 1
            print(f"✗ {p.name}")
            for e in errs:
                print(f"    {e}")
    print(f"\n{done}/{len(files)} labeled, {bad} with errors")


def reformat() -> None:
    """Replace a one-line raw_text with a sentence list in existing label files, keeping all labels."""
    n = 0
    for p in sorted(LABELS.glob("*.json")):
        if p.name == "manifest.json":
            continue
        lab = json.loads(p.read_text())
        if "raw_text" not in lab:
            continue
        lab = {("text" if k == "raw_text" else k): (sentences(v) if k == "raw_text" else v) for k, v in lab.items()}
        p.write_text(json.dumps(lab, indent=2) + "\n")
        n += 1
    print(f"reformatted {n} files")


def prefill() -> None:
    """Add blank observation rows to label files that have none yet. Never touches started files."""
    n = 0
    for p in sorted(LABELS.glob("*-*.json")):
        lab = json.loads(p.read_text())
        if lab["observations"]:
            continue
        lab["observations"] = [blank_obs() for _ in range(N_BLANK_OBS)]
        p.write_text(json.dumps(lab, indent=2) + "\n")
        n += 1
    print(f"added {N_BLANK_OBS} blank observations to {n} files")


if __name__ == "__main__":
    {"select": select, "check": check, "reformat": reformat, "prefill": prefill}[sys.argv[1] if len(sys.argv) > 1 else "check"]()
