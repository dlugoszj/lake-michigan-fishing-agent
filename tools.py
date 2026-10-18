"""Read-only tools the agent can call. Each queries fishing.db and returns compact text.

Every observation comes back with its report date, port, evidence sentence and bulletin URL,
so the agent can cite its sources. Nothing dated after `as_of` is ever returned, which keeps
backtests honest (the agent can't see the future).
"""
import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

import conditions
from schema import PROMPT_VERSION

DB = "fishing.db"
MODEL = "claude-opus-5"  # which extraction to read
PV = PROMPT_VERSION

SPECIES_GROUPS = {
    "salmon": ["chinook", "coho", "salmon_unspecified", "pink"],
    "salmon_and_trout": ["chinook", "coho", "salmon_unspecified", "pink", "steelhead", "lake_trout", "brown_trout"],
    "all": None,
}
QUALITY = {0: "slow", 1: "below average", 2: "average", 3: "good", 4: "excellent", None: "not rated"}


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _depth(lo, hi):
    if lo is None and hi is None:
        return None
    if hi is None:
        return f"{lo}+ ft"
    if lo is None:
        return f"up to {hi} ft"
    return f"{lo} ft" if lo == hi else f"{lo}-{hi} ft"


def _evidence(raw):
    if raw and raw.startswith("["):
        return " … ".join(json.loads(raw))
    return raw or ""


def _observations(where: str, params: list, species_group: str) -> str:
    q = f"""
        SELECT p.date, p.port, p.chunk_id, r.url, o.species, o.method, o.bottom_min_ft, o.bottom_max_ft,
               o.fish_min_ft, o.fish_max_ft, o.lures, o.lure_details, o.catch_quality, o.evidence, e.access_issues
          FROM observations o
          JOIN port_chunks p USING (chunk_id)
          JOIN reports r ON r.report_id = p.report_id
          JOIN extractions e ON e.chunk_id = o.chunk_id AND e.model = o.model AND e.prompt_version = o.prompt_version
         WHERE o.model = ? AND o.prompt_version = ? AND p.lake_port = 1 AND {where}"""
    params = [MODEL, PV] + params
    species = SPECIES_GROUPS.get(species_group)
    if species:
        q += f" AND o.species IN ({','.join('?' * len(species))})"
        params += species
    q += " ORDER BY p.date DESC, p.port"
    rows = _conn().execute(q, params).fetchall()
    if not rows:
        return "No matching observations."
    by_report = defaultdict(list)
    for r in rows:
        by_report[(r["date"], r["port"], r["url"], r["access_issues"])].append(r)
    out = []
    for (d, port, url, access), obs in by_report.items():
        out.append(f"## {d} · {port} · {url}\n   {conditions.week_summary_text(port, d)}" + (f"\n   ACCESS ISSUE: {access}" if access else ""))
        for r in obs:
            parts = [f"{r['species']} / {r['method']}"]
            if (w := _depth(r["bottom_min_ft"], r["bottom_max_ft"])):
                parts.append(f"water {w}")
            if (f := _depth(r["fish_min_ft"], r["fish_max_ft"])):
                parts.append(f"fish {f} down")
            lures = json.loads(r["lures"]) if r["lures"] else []
            if lures:
                parts.append("lures " + ", ".join(lures) + (f" ({r['lure_details']})" if r["lure_details"] else ""))
            q_ = r["catch_quality"]
            parts.append(f"quality {q_ if q_ is not None else '-'} ({QUALITY.get(q_)})")
            out.append("- " + "; ".join(parts) + f'\n  evidence: "{_evidence(r["evidence"])}"')
    return "\n".join(out)


def list_ports(as_of: str) -> str:
    rows = _conn().execute("""
        SELECT p.port, COUNT(DISTINCT p.report_id) reports, MIN(p.date) first, MAX(p.date) last
          FROM port_chunks p JOIN extractions e USING (chunk_id)
         WHERE e.model = ? AND e.prompt_version = ? AND p.lake_port = 1 AND p.date <= ?
         GROUP BY p.port ORDER BY reports DESC""", (MODEL, PV, as_of)).fetchall()
    today = date.fromisoformat(as_of)
    lines = [f"Today is {as_of}. Ports with DNR reports (Lake Michigan, Southwest Lower Peninsula):"]
    for r in rows:
        age = (today - date.fromisoformat(r["last"])).days
        lines.append(f"- {r['port']}: {r['reports']} weekly reports, {r['first']} to {r['last']} (latest is {age} days old)")
    return "\n".join(lines)


def get_recent_reports(ports: list[str], weeks_back: int, species_group: str, as_of: str) -> str:
    start = (date.fromisoformat(as_of) - timedelta(weeks=weeks_back)).isoformat()
    ph = ",".join("?" * len(ports))
    body = _observations(f"p.port IN ({ph}) AND p.date > ? AND p.date <= ?", [*ports, start, as_of], species_group)
    return f"Reports for {', '.join(ports)} from {start} to {as_of} ({species_group}):\n{body}"


def find_similar_weeks(ports: list[str], around_date: str, window_days: int, species_group: str, as_of: str,
                       surface_temp_f: float | None = None, temp_tolerance_f: float | None = None) -> str:
    """Same time of year in earlier seasons; optionally only weeks whose buoy surface temperature was similar."""
    target = date.fromisoformat(around_date)
    clauses, params = [], []
    for year in range(2017, date.fromisoformat(as_of).year + 1):
        try:
            center = target.replace(year=year)
        except ValueError:  # Feb 29
            center = target.replace(year=year, day=28)
        lo, hi = center - timedelta(days=window_days), min(center + timedelta(days=window_days), date.fromisoformat(as_of))
        if lo <= hi:
            clauses.append("(p.date BETWEEN ? AND ?)")
            params += [lo.isoformat(), hi.isoformat()]
    if not clauses:
        return "No earlier seasons in range."
    ph = ",".join("?" * len(ports))
    where, args = f"p.port IN ({ph}) AND ({' OR '.join(clauses)})", [*ports, *params]
    desc = f"within {window_days} days of {target.strftime('%B %d')} in any year up to {as_of}"
    if surface_temp_f is not None:
        tol = temp_tolerance_f if temp_tolerance_f is not None else 4.0
        weeks = _conn().execute(f"SELECT DISTINCT p.port, p.date FROM port_chunks p WHERE p.lake_port = 1 AND {where}", args).fetchall()
        keep = [(w["port"], w["date"]) for w in weeks
                if (s := conditions.week_summary(w["port"], w["date"])) and abs(s["mean_f"] - surface_temp_f) <= tol]
        if not keep:
            return f"No report weeks {desc} had surface water within {tol:.0f} F of {surface_temp_f:.0f} F."
        where = "(" + " OR ".join("(p.port = ? AND p.date = ?)" for _ in keep) + ")"
        args = [x for k in keep for x in k]
        desc += f", with that week's surface water within {tol:.0f} F of {surface_temp_f:.0f} F"
    return f"Reports for {', '.join(ports)} {desc} ({species_group}):\n{_observations(where, args, species_group)}"


def get_current_conditions(ports: list[str], as_of: str) -> str:
    """Live buoy + model + forecast when as_of is today; for a past as_of, only that week's buoy summary (no peeking ahead)."""
    if (date.today() - date.fromisoformat(as_of)).days > 1:
        return "\n".join([f"Backtest mode (today is {as_of}): live data is unavailable; buoy summary for the week before {as_of}:"] +
                         [f"- {p}: {conditions.week_summary_text(p, as_of)}" for p in ports])
    return "\n\n".join(conditions.current_conditions_text(p) for p in ports)


PORT_ENUM = ["Muskegon", "Muskegon Lake", "Grand Haven", "Holland", "South Haven", "Black River", "St. Joseph", "St. Joseph River"]
GROUP_ENUM = list(SPECIES_GROUPS)

TOOLS = [
    {
        "name": "list_ports",
        "description": "List the ports that have DNR weekly fishing reports, how many reports each has, and how old the latest one is. "
                       "Call this first to know what data exists.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "get_recent_reports",
        "description": "Observations from the most recent DNR reports for the given ports: species, method (boat/pier/shore/river), "
                       "water depth, fish depth, lures, catch quality (0 slow … 4 excellent, '-' = not rated), the verbatim evidence "
                       "sentence, report date and bulletin URL. This is the strongest signal because it reflects this year's conditions.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "ports": {"type": "array", "items": {"type": "string", "enum": PORT_ENUM}},
                "weeks_back": {"type": "integer", "description": "How many weeks back from today to include (e.g. 3-8)."},
                "species_group": {"type": "string", "enum": GROUP_ENUM,
                                  "description": "salmon = chinook, coho, unspecified salmon; salmon_and_trout adds steelhead, lake/brown trout."},
            },
            "required": ["ports", "weeks_back", "species_group"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_similar_weeks",
        "description": "Observations from the same time of year in earlier seasons (calendar match within a window of days). "
                       "Use it to see what typically happens at this point in the season, especially when recent reports are old or sparse.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "ports": {"type": "array", "items": {"type": "string", "enum": PORT_ENUM}},
                "around_date": {"type": "string", "format": "date", "description": "Center date, usually today or the trip date."},
                "window_days": {"type": "integer", "description": "Days on each side of that date, e.g. 14."},
                "species_group": {"type": "string", "enum": GROUP_ENUM},
                "surface_temp_f": {"type": ["number", "null"],
                                   "description": "Optional: only weeks whose average buoy surface temperature was close to this (e.g. today's reading). null = calendar match only."},
                "temp_tolerance_f": {"type": ["number", "null"], "description": "Allowed difference in F for surface_temp_f (default 4)."},
            },
            "required": ["ports", "around_date", "window_days", "species_group", "surface_temp_f", "temp_tolerance_f"],
            "additionalProperties": False,
        },
    },
]


TOOLS.append({
    "name": "get_current_conditions",
    "description": "Current water and weather for ports: MEASURED surface water temperature from the nearest NOAA buoy with its "
                   "3-day trend (a sharp drop suggests an upwelling), the MODELED cold-layer depth offshore (NOAA LMHOFS: depth where "
                   "water first reaches 55 F and the 52-58 F band, at points in ~80/120/160 ft of water), and a 3-day wind forecast.",
    "strict": True,
    "input_schema": {"type": "object", "properties": {"ports": {"type": "array", "items": {"type": "string", "enum": PORT_ENUM}}},
                     "required": ["ports"], "additionalProperties": False},
})


def run_tool(name: str, args: dict, as_of: str) -> str:
    if name == "list_ports":
        return list_ports(as_of)
    if name == "get_recent_reports":
        return get_recent_reports(args["ports"], max(1, int(args["weeks_back"])), args["species_group"], as_of)
    if name == "find_similar_weeks":
        return find_similar_weeks(args["ports"], args["around_date"], max(1, int(args["window_days"])), args["species_group"], as_of,
                                  args.get("surface_temp_f"), args.get("temp_tolerance_f"))
    if name == "get_current_conditions":
        return get_current_conditions(args["ports"], as_of)
    raise ValueError(f"unknown tool {name}")
