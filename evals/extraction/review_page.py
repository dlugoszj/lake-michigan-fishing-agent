"""Eval report page: score progression across prompt versions, the change history, and an
observation-by-observation comparison of your labels with each model (dev split only).

Usage: python -m evals.extraction.review_page OUT.html [--prompt-version v8 --prompt-version v10-def ...] [--model ...]

Test chunks are never shown, so the prompt can't be tuned against them.
"""
import argparse
import html
import json
import sqlite3
from pathlib import Path

from evals.extraction import score as ev
from evals.extraction.history import HISTORY
from extraction.schema import PROMPT_VERSION
from config.paths import DB

# Reviewed causes per prompt version. A = rule missing from (or conflicting in) the prompt,
# B = label differs from the written rules, C = model mistake. v8 notes are kept as written at
# the time; some labels they mention have since been corrected (see the history).
NOTES_BY_VERSION = {
    "v8": {
        "39b4ba0-010": [("B", "“Boat anglers found Chinook” has no quality word; the guide says null, the label said 2. (Label since corrected.)"),
                        ("C", "A fish cleaning station opening is not an access issue.")],
        "39e2127-014": [("B", "Steelhead, drum and catfish were labeled method unknown; the sentences follow “Pier fishing was on the slow side”, so the model read them as pier. (Label since corrected.)")],
        "3df50cf-010": [("A", "“45ft out past 100ft” → max should be null; the prompt never said how to handle “past / beyond / over”. (Fixed in v9.)"),
                        ("A", "“Coho along the beach” belongs in notes, not a shore row. (Fixed in v10.)"),
                        ("A", "“Pier fishing has slowed” followed by named species → no separate fish_unspecified row. (Fixed in v10.)"),
                        ("C", "“No perch reports” → the prompt says quality 0; the model returned null."),
                        ("C", "Opus stitched non-adjacent sentences into one evidence string. (Fixed in v10: evidence is a list.)")],
        "3eab77c-008": [("A", "“to over 120 feet” → max should be null. (Fixed in v9.)"),
                        ("C", "Shared rating (“salmon fishing was a little slow”) not applied to Chinook.")],
        "3ecb2c3-005": [("A", "“well beyond 120 feet” → max should be null. (Fixed in v9.)"),
                        ("C", "Haiku stitched non-adjacent sentences into one evidence string. (Fixed in v10.)")],
        "3f5314a-008": [("B", "“A couple of salmon caught” on the pier is 1 in the wording table; the label said 0. (Label since corrected to [0, 1].)"),
                        ("B", "Perch row was pier + spoons, but the text only says “Perch fishing was slow”. (Label since corrected.)"),
                        ("A", "“Pier fishing was a little slow” followed by named salmon → no separate fish row. (Fixed in v10.)")],
        "3f7335a-010": [("B", "“Decent numbers” is 2 in the wording table; the label said 3. (Label since corrected.)")],
        "425abbc-004": [("B", "The offshore sentence gives no lure or rating for steelhead and coho; the label had quality 2. (Label since corrected to null.)")],
        "42c1dbe-007": [("A", "“Near the Berrien Springs Dam … similar” → the label merges it into the boat row; the model made a separate river row.")],
        "42c1dbe-008": [("C", "“A few fish were also taken by anglers trolling” should resolve to the Chinook and coho just named (rule 4).")],
    },
    "v10-def": {
        "3a65224-005": [("C", "“Spoons and flasher/fly combinations were reported to have worked well” rates the lures, not the catch; both models gave 3, the label is null.")],
        "39b4ba0-010": [("C", "Haiku rates “Boat anglers found Chinook” as 3; presence is not a rating.")],
        "39e2127-014": [("C", "Haiku gives coho 3; the shared rating is “decent” (2) and “mostly” is not a quantity word.")],
        "3eab77c-008": [("C", "Shared rating (“salmon fishing was a little slow”) still not applied to Chinook (both versions, both models)."),
                        ("C", "Opus and Haiku rate drum 2 from “slow for all species except freshwater drum”; the text doesn't say how well drum fishing went.")],
        "3df50cf-011": [("C", "Haiku rates “some coho salmon and brown trout mixed in” as 2; rule 3 gives the species' own word (“some” = 1).")],
        "3df50cf-010": [("C", "Haiku rates Chinook and coho 1 (“slowed slightly from last week”); the label accepts 2 or 3 for a “pretty good week”.")],
        "42c1dbe-007": [("A", "Rule conflict: the prompt sends mixed wording (“a few salmon, though fishing was generally slow”) to the lower level (0); the guide accepts both levels, but the label says 1. Pending label fix to [0, 1].")],
        "3f0b284-007": [("A", "Same mixed-wording conflict for perch (“good for one day, but the rest of the week was challenging”). Pending label decision.")],
    },
}
NOTES_BY_VERSION["v11-def"] = {
    "3b2f6aa-007": [("B", "Open edge case: the lure-scope rule gives the offshore steelhead the j-plugs; the labeler reads the j-plug sentence as channel-only and kept the label. The lure rule may need a placement refinement.")],
    "3eab77c-008": [("B", "Borderline: the label gives Chinook the “fish were spread from 65 to over 120 feet” depth; Opus leaves it null, since the Chinook sentence is about lures."),
                    ("C", "Drum rated 2 from “slow for all species except freshwater drum”; the text doesn't say how well drum fishing went (null).")],
}
NOTES_SCOPE = {
    "v8": "Notes from the v8 error review (Opus mismatches), kept as written at the time.",
    "v10": "Not reviewed separately; superseded by the A/B comparison with v10-def.",
    "v10-def": "Notes cover catch-quality differences; the other differences were reviewed afterwards and led to the v11 rules.",
    "v11-def": "All Opus differences are annotated. Haiku's remaining differences are the structural mistakes described in the history (river vs. boat, shore/pier for perch, lure mappings, over-rating).",
}
CAUSE = {"A": "Rule missing from or conflicting in the prompt", "B": "Label vs. written rules", "C": "Model mistake"}
SHORT = {"claude-opus-5": "Opus 5", "claude-haiku-4-5": "Haiku 4.5"}
VERSION_LABEL = {"v8": "v8 · first eval", "v10": "v10 · word table", "v10-def": "v10-def · definitions", "v11-def": "v11-def · current"}


def esc(x) -> str:
    return html.escape(str(x))


def fmt_depth(d):
    if d is None:
        return "not stated"
    lo, hi = d
    if hi is None:
        return f"{lo}+ ft"
    if lo is None:
        return f"up to {hi} ft"
    return f"{lo} ft" if lo == hi else f"{lo}–{hi} ft"


def fmt(field, v):
    if field in ("bottom_depth", "fish_depth"):
        return fmt_depth(v)
    if field == "lures":
        return ", ".join(sorted(v)) or "none"
    if field == "catch_quality":
        if isinstance(v, list):
            return " or ".join(map(str, v))
        return "not stated" if v is None else str(v)
    if field == "conditions":
        return ", ".join(sorted(v)) or "none"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def field_ok(field, gold, pred):
    if field == "catch_quality":
        return ev.quality_ok(gold, pred)
    if field == "conditions":
        return set(gold) == set(pred)
    return gold == pred


FIELD_NAMES = {"bottom_depth": "Water depth", "fish_depth": "Fish depth", "lures": "Lures", "catch_quality": "Catch quality"}
CHUNK_FIELDS = {"access_issues": "Access issues", "no_fishing_info": "No fishing info"}


def obs_cell(obs, gold, extra=False):
    """One observation as a field list. Fields that differ from the label are marked."""
    if obs is None:
        return "<td class='obs empty-obs'><span>Empty observation</span></td>"
    rows, any_bad = [], extra
    for f, name in FIELD_NAMES.items():
        bad = extra or (gold is not None and not field_ok(f, gold[f], obs[f]))
        any_bad |= bad
        rows.append(f"<dt>{name}</dt><dd class='{'bad' if bad else ''}'>{esc(fmt(f, obs[f]))}</dd>")
    details = obs.get("lure_details")
    if details:
        rows.append(f"<dt>Lure details</dt><dd class='aside'>{esc(details)}</dd>")
    if obs.get("notes"):
        rows.append(f"<dt>Notes</dt><dd class='aside'>{esc(obs['notes'])}</dd>")
    ev_html = f"<p class='ev'>“{esc(obs['evidence'])}”</p>" if obs.get("evidence") else ""
    return f"<td class='obs{' has-bad' if any_bad else ''}'><dl>{''.join(rows)}</dl>{ev_html}</td>"


def chunk_table(lab, preds, models, names):
    """Rows = observations (species / method). Columns = your label, then each model. Returns (html, differs)."""
    gold_by = {(r["species"], r["method"]): r for r in lab["rows"]}
    got_by = {m: {} for m in models}
    for m in models:
        for r in (preds[m]["rows"] if preds[m] else []):
            got_by[m].setdefault((r["species"], r["method"]), r)
    keys = list(gold_by) + sorted({k for g in got_by.values() for k in g} - set(gold_by))
    differs = False
    out = ["<div class='scroll'><table class='cmp'><thead><tr><th>Observation</th><th>Your label</th>"
           + "".join(f"<th>{esc(n)}</th>" for n in names) + "</tr></thead><tbody>"]
    for k in keys:
        g = gold_by.get(k)
        cells, status = [], []
        for m, n in zip(models, names):
            r = got_by[m].get(k)
            if g is None and r is not None:
                status.append(f"extra in {n}")
            elif g is not None and r is None and not g.get("optional"):
                status.append(f"missed by {n}")
            elif g is not None and any(not field_ok(f, g[f], r[f]) for f in FIELD_NAMES):
                status.append(f"{n} differs")
            cells.append(obs_cell(r, g, extra=(g is None)))
        differs |= bool(status)
        chip = ("<span class='chip bad'>" + "</span><span class='chip bad'>".join(map(esc, status)) + "</span>"
                if status else "<span class='chip ok'>all match</span>")
        if g is not None and g.get("optional"):
            chip += "<span class='chip opt'>optional row</span>"
        out.append(f"<tr><th class='key'><span class='mono'>{esc(k[0])}</span><span class='method'>{esc(k[1])}</span>{chip}</th>"
                   f"{obs_cell(g, None)}{''.join(cells)}</tr>")
    # whole-paragraph fields
    rows = []
    for f, name in CHUNK_FIELDS.items():
        vals = []
        for m in models:
            p = preds[m]
            if p is None:
                vals.append("<dd class='bad'>not extracted</dd>")
                differs = True
                continue
            ok = field_ok(f, lab[f], p[f])
            differs |= not ok
            vals.append(f"<dd class='{'' if ok else 'bad'}'>{esc(fmt(f, p[f]))}</dd>")
        rows.append((name, f"<dd>{esc(fmt(f, lab[f]))}</dd>", vals))
    out.append("<tr class='para'><th class='key'><span>Whole paragraph</span></th>"
               + "<td class='obs'><dl>" + "".join(f"<dt>{n}</dt>{g}" for n, g, _ in rows) + "</dl></td>"
               + "".join("<td class='obs'><dl>" + "".join(f"<dt>{n}</dt>{v[i]}" for n, _, v in rows) + "</dl></td>"
                         for i in range(len(models)))
               + "</tr></tbody></table></div>")
    return "".join(out), differs


CSS = """
:root{--bg:#f2f5f6;--panel:#ffffff;--ink:#14242e;--muted:#5b6d78;--line:#d6dfe3;--accent:#1d6a83;
--ok:#2c7a57;--ok-bg:#e3f2ea;--bad:#b1442f;--bad-bg:#fbe7e2;--empty:#eef2f4;
--a:#9a6b12;--a-bg:#fbf0d8;--b:#5a4bb0;--b-bg:#ebe8fa;--c:#b1442f;--c-bg:#fbe7e2;--quote:#e8eef1}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0f171c;--panel:#16222a;--ink:#e3ecf0;
--muted:#93a6b0;--line:#2a3a44;--accent:#6cb8d1;--ok:#7fcfa6;--ok-bg:#163126;--bad:#f0927d;--bad-bg:#3a1f19;--empty:#1b2830;
--a:#e3b660;--a-bg:#33270f;--b:#b3a8f2;--b-bg:#25213f;--c:#f0927d;--c-bg:#3a1f19;--quote:#1c2b34}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0f171c;--panel:#16222a;--ink:#e3ecf0;--muted:#93a6b0;--line:#2a3a44;
--accent:#6cb8d1;--ok:#7fcfa6;--ok-bg:#163126;--bad:#f0927d;--bad-bg:#3a1f19;--empty:#1b2830;--a:#e3b660;--a-bg:#33270f;
--b:#b3a8f2;--b-bg:#25213f;--c:#f0927d;--c-bg:#3a1f19;--quote:#1c2b34}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif;margin:0;padding-inline:16px;padding-block:28px 64px}
.wrap{max-width:1180px;margin:0 auto;display:grid;gap:28px}
h1{font-size:28px;line-height:1.2;margin:0;text-wrap:balance;font-weight:600}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 10px;font-weight:600}
.lede{color:var(--muted);max-width:70ch;margin:6px 0 0}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}
.scores th,.scores td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
.scores th{font-size:12px;color:var(--muted);font-weight:600}
.scores .num{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.scores .scope td{font-weight:600;color:var(--accent);padding-top:14px}
.legend{display:flex;flex-wrap:wrap;gap:10px 18px;color:var(--muted);font-size:13px;align-items:center}
.tag{display:inline-block;font-size:11px;font-weight:600;letter-spacing:.04em;padding:1px 7px;border-radius:4px;white-space:nowrap}
.tag.A{color:var(--a);background:var(--a-bg)}.tag.B{color:var(--b);background:var(--b-bg)}.tag.C{color:var(--c);background:var(--c-bg)}
.swatch{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:4px}
.filters{display:flex;flex-wrap:wrap;gap:8px;position:sticky;top:env(safe-area-inset-top,0px);background:var(--bg);padding-block:10px;z-index:2}
.filters button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:5px 12px;border-radius:999px;cursor:pointer}
.filters button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--panel)}
.filters button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.chunk{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px;display:grid;gap:14px}
.chunk header{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline}
.chunk header b{font-size:17px}
.chunk header span{color:var(--muted);font-size:13px}
blockquote{margin:0;background:var(--quote);padding:12px 14px;border-radius:6px;font-family:"Source Serif 4",Georgia,serif;font-size:15.5px;line-height:1.6;max-width:90ch}
.notes{display:grid;gap:6px;margin:0;padding:0;list-style:none}
.notes li{display:flex;gap:10px;align-items:baseline}
.unreviewed{color:var(--muted);font-size:13px;font-style:italic;margin:0}
table.cmp{min-width:760px;table-layout:fixed}
table.cmp thead th{font-size:12px;color:var(--muted);font-weight:600;text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}
table.cmp thead th:first-child{width:150px}
table.cmp tbody tr{border-bottom:1px solid var(--line)}
th.key{text-align:left;vertical-align:top;padding:10px;font-weight:600;display:table-cell}
th.key span{display:block}
th.key .method{color:var(--muted);font-weight:400;font-size:13px}
.chip{display:inline-block!important;font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;margin-top:6px;margin-right:4px}
.chip.ok{color:var(--ok);background:var(--ok-bg)}.chip.opt{color:var(--b);background:var(--b-bg)}.chip.bad{color:var(--bad);background:var(--bad-bg)}
td.obs{vertical-align:top;padding:10px;border-left:1px solid var(--line)}
td.obs dl{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin:0;font-size:13.5px}
td.obs dt{color:var(--muted);white-space:nowrap}
td.obs dd{margin:0;font-family:"IBM Plex Mono",monospace;font-size:13px}
td.obs dd.bad{color:var(--bad);background:var(--bad-bg);border-radius:3px;padding:0 4px;margin-left:-4px;font-weight:500}
td.obs dd.aside{font-family:inherit;color:var(--muted)}
td.empty-obs{background:repeating-linear-gradient(135deg,var(--empty) 0 8px,transparent 8px 16px)}
td.empty-obs span{display:inline-block;color:var(--muted);font-size:13px;font-style:italic;background:var(--panel);padding:2px 8px;border-radius:4px;border:1px dashed var(--line)}
p.ev{margin:8px 0 0;font-family:"Source Serif 4",Georgia,serif;font-size:12.5px;color:var(--muted);line-height:1.45}
tr.para th.key span{color:var(--muted);font-weight:500}
"""

def metric_table(results, models, versions):
    """Score progression, salmon only: one column per (model, version)."""
    cols = [(m, v) for m in models for v in versions if (m, v) in results]
    def g(r, k): return r["metrics"]["salmon only"][k]
    rows = [
        ("Row recall: your observations the model found", lambda r: ev.pct(g(r, "rows_matched"), g(r, "rows_matched") + g(r, "rows_missed"))),
        ("Row precision: model observations that are in your labels", lambda r: ev.pct(g(r, "rows_matched"), g(r, "rows_matched") + g(r, "rows_extra"))),
        ("Water depth correct", lambda r: ev.pct(g(r, "bottom_depth_ok"), g(r, "rows_matched"))),
        ("Fish depth correct", lambda r: ev.pct(g(r, "fish_depth_ok"), g(r, "rows_matched"))),
        ("Lures exact", lambda r: ev.pct(g(r, "lures_ok"), g(r, "rows_matched"))),
        ("Catch quality exact", lambda r: ev.pct(g(r, "catch_quality_ok"), g(r, "rows_matched"))),
        ("Catch quality within 1", lambda r: ev.pct(g(r, "catch_quality_within1"), g(r, "rows_matched"))),
        ("Cost per chunk", lambda r: f"${r['meta']['cost'] / max(r['meta']['extracted'], 1):.4f}"),
    ]
    out = ["<div class='scroll'><table class='scores'><thead><tr><th></th>"]
    for m in models:
        n = sum((m, v) in results for v in versions)
        out.append(f"<th class='grp' colspan='{n}'>{esc(SHORT.get(m, m))}</th>")
    out.append("</tr><tr><th>Dev, salmon only</th>")
    out += [f"<th class='num'>{esc(v)}</th>" for _, v in cols]
    out.append("</tr></thead><tbody>")
    for name, f in rows:
        out.append(f"<tr><td>{esc(name)}</td>" + "".join(f"<td class='num'>{esc(f(results[c]).strip())}</td>" for c in cols) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def history_html():
    out = ["<ol class='timeline'>"]
    for h in HISTORY:
        parts = [f"<li><div class='when'><span class='mono'>{esc(h['version'])}</span><span>{esc(h['date'])}</span></div>",
                 f"<div class='what'><h3>{esc(h['title'])}</h3><p><b>Ran:</b> {esc(h['ran'])}</p>"]
        if h.get("scores"):
            parts.append(f"<p><b>Scores:</b> {esc(h['scores'])}</p>")
        for key, label in (("found", "Found"), ("changed", "Changed")):
            if h[key]:
                parts.append(f"<p class='lbl'>{label}</p><ul>" + "".join(f"<li>{esc(x)}</li>" for x in h[key]) + "</ul>")
        if h.get("caveat"):
            parts.append(f"<p class='caveat'><b>Caveat:</b> {esc(h['caveat'])}</p>")
        parts.append("</div></li>")
        out += parts
    out.append("</ol>")
    return "".join(out)


EXTRA_CSS = """
.scores th.grp{text-align:center;color:var(--ink);border-bottom:1px solid var(--line)}
.tabs{display:flex;flex-wrap:wrap;gap:8px}
.tabs button,.filters button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:5px 12px;border-radius:999px;cursor:pointer}
.tabs button[aria-pressed="true"],.filters button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--panel)}
.tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.controls{display:grid;gap:8px;position:sticky;top:env(safe-area-inset-top,0px);background:var(--bg);padding-block:10px;z-index:2}
.controls .filters{position:static;padding:0}
.scopenote{color:var(--muted);font-size:13px;margin:0}
.timeline{list-style:none;margin:0;padding:0;display:grid;gap:0}
.timeline>li{display:grid;grid-template-columns:150px 1fr;gap:16px;padding-block:14px;border-top:1px solid var(--line)}
.timeline .when{display:grid;align-content:start;gap:2px;color:var(--muted);font-size:13px}
.timeline .when .mono{color:var(--accent);font-size:14px;font-weight:500}
.timeline h3{margin:0 0 6px;font-size:16px;font-weight:600;text-wrap:balance}
.timeline p{margin:4px 0;max-width:80ch}
.timeline p.lbl{margin-top:8px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.timeline ul{margin:2px 0 0;padding-left:18px;max-width:80ch}
.timeline ul li{margin:2px 0}
.caveat{background:var(--a-bg);color:var(--ink);padding:8px 10px;border-radius:6px}
@media (max-width:640px){.timeline>li{grid-template-columns:1fr;gap:4px}}
"""

JS = """
const tabs=[...document.querySelectorAll('.tabs button')], fbtns=[...document.querySelectorAll('.filters button')];
let ver=tabs.length?tabs[tabs.length-1].dataset.v:'', filt='diff';
try{ver=localStorage.getItem('rv-ver')||ver; filt=localStorage.getItem('rv-filt')||filt}catch(e){}
if(!tabs.some(t=>t.dataset.v===ver)) ver=tabs[tabs.length-1].dataset.v;
function render(){
 tabs.forEach(t=>t.setAttribute('aria-pressed',t.dataset.v===ver));
 fbtns.forEach(b=>b.setAttribute('aria-pressed',b.dataset.f===filt));
 document.querySelectorAll('.vpanel').forEach(p=>{p.hidden=p.dataset.v!==ver});
 document.querySelectorAll('.vpanel .chunk').forEach(c=>{c.hidden=(filt==='diff'&&c.dataset.differs!=='1')});
 try{localStorage.setItem('rv-ver',ver);localStorage.setItem('rv-filt',filt)}catch(e){}}
tabs.forEach(t=>t.addEventListener('click',()=>{ver=t.dataset.v;render()}));
fbtns.forEach(b=>b.addEventListener('click',()=>{filt=b.dataset.f;render()}));
render();
"""


def build(models, versions, db) -> str:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    labels = ev.load_labels("dev")
    names = [SHORT.get(m, m) for m in models]
    results, preds = {}, {}
    for v in versions:
        for m in models:
            r, _ = ev.evaluate(conn, m, v, labels)
            if r["meta"]["extracted"]:
                results[(m, v)] = r
                preds[(m, v)] = ev.load_model(conn, m, v, labels)
    versions = [v for v in versions if any((m, v) in results for m in models)]

    out = ["<title>Salmon Extractor Eval</title>",
           '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
           '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400&display=swap">',
           f"<style>{CSS}{EXTRA_CSS}</style><div class='wrap'>"]
    out.append("<div><h1>Salmon report extractor: how the eval improved it</h1><p class='lede'>"
               "An LLM turns Michigan DNR weekly fishing reports into structured observations (species, method, depths, lures, "
               f"catch quality). Each prompt version is scored against {len(labels)} hand-labeled dev paragraphs. All scores below "
               "are recomputed against the current labels. The 7 test paragraphs are held out and never shown here.</p></div>")

    out.append("<section><h2>Scores by prompt version</h2>" + metric_table(results, models, versions) + "</section>")
    out.append("<section><h2>What was run, found and changed</h2>" + history_html() + "</section>")

    out.append("<section><h2>How to read the comparison</h2><div class='legend'>"
               "<span><span class='swatch' style='background:var(--bad-bg);outline:1px solid var(--bad)'></span>Differs from your label</span>"
               "<span><span class='swatch' style='background:var(--empty);outline:1px dashed var(--muted)'></span>Empty observation (model didn't create it, or you didn't)</span>"
               + "".join(f"<span><span class='tag {k}'>{k}</span> {esc(v)}</span>" for k, v in CAUSE.items())
               + "</div></section>")
    out.append("<div class='controls'><nav class='tabs' aria-label='Prompt version'>"
               + "".join(f"<button type='button' id='v-{esc(v)}' data-v='{esc(v)}' aria-pressed='false'>{esc(VERSION_LABEL.get(v, v))}</button>" for v in versions)
               + "</nav><nav class='filters' aria-label='Which paragraphs to show'>"
               "<button type='button' id='f-diff' data-f='diff' aria-pressed='true'>Paragraphs with differences</button>"
               "<button type='button' id='f-all' data-f='all' aria-pressed='false'>All dev paragraphs</button></nav></div>")

    for v in versions:
        vm = [m for m in models if (m, v) in results]
        vn = [SHORT.get(m, m) for m in vm]
        notes = NOTES_BY_VERSION.get(v, {})
        out.append(f"<section class='vpanel' data-v='{esc(v)}'><p class='scopenote'>{esc(NOTES_SCOPE.get(v, ''))}</p>")
        for cid, lab in sorted(labels.items(), key=lambda x: x[1]["date"]):
            table, differs = chunk_table(lab, {m: preds[(m, v)].get(cid) for m in vm}, vm, vn)
            who = "you" if "labeled_by" not in lab else "Claude draft, reviewed by you"
            c = [f"<article class='chunk' id='c-{esc(v)}-{esc(cid)}' data-differs='{1 if differs else 0}'>"
                 f"<header><b>{esc(lab['port'])}</b><span>{esc(lab['date'])}</span><span class='mono'>{esc(cid)}</span>"
                 f"<span>labeled by {who}</span></header>",
                 f"<blockquote>{esc(' '.join(lab['text']))}</blockquote>"]
            if cid in notes:
                c.append("<ul class='notes'>" + "".join(f"<li><span class='tag {k}'>{k}</span><span>{esc(t)}</span></li>"
                                                         for k, t in notes[cid]) + "</ul>")
            elif differs:
                c.append("<p class='unreviewed'>Causes not reviewed for this version.</p>")
            c.append(table + "</article>")
            out += c
        out.append("</section>")
    out.append(f"</div><script>{JS}</script>")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--model", action="append")
    ap.add_argument("--prompt-version", action="append")
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args()
    models = a.model or ["claude-opus-5", "claude-haiku-4-5"]
    versions = a.prompt_version or ["v8", "v10", "v10-def", "v11-def"]
    Path(a.out).write_text(build(models, versions, a.db))
    print("wrote", a.out)


if __name__ == "__main__":
    main()
