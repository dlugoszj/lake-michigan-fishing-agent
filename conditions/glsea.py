"""Daily satellite surface water temperature per port (NOAA GLERL GLSEA), stored in fishing.db.

  python -m conditions.glsea update                         # from the newest stored day to GLSEA's newest
                                                            # (empty table: the whole history from 2022-04-01)
  python -m conditions.glsea download 2024-06-01 2024-06-30 # any range by hand, e.g. to redo a month

Source: GLSEA_ACSPO_GCS on GLERL's ERDDAP (daily, ~1.4 km grid, 2006 to now). It is an analysis: each day blends
the satellite passes that saw the water, and cloudy cells are filled from earlier days, so it has a value every
day but can lag a fast change. At Holland it showed the October 2026 upwelling ~2 days after buoy 45029, with
the right size of drop; before the drop it was within ~0.3 C of the buoy.

Points are the lake model's (pier, 40, 80, 120, 160 ft of water; see lake_model.py), each matched once to the
nearest GLSEA water cell and kept in glsea_points. GLSEA leaves the strip nearest shore empty, so the pier cell
sits ~1-2 km offshore; glsea_points records each cell's distance from its lake-model point.
"""
import calendar
import csv
import math
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

from conditions.lake_model import ALIAS, PORTS
from conditions.water_weather import DB, F

ERDDAP = "https://apps.glerl.noaa.gov/erddap"
DATASET = "GLSEA_ACSPO_GCS"
START = "2022-04-01"
PAD = 0.02  # degrees added around each port's lake-model points
RETRIES = (10, 30, 60)  # seconds to wait before each retry

SCHEMA = """
CREATE TABLE IF NOT EXISTS glsea_points (port TEXT, point TEXT, lat REAL, lon REAL, km_from_point REAL,
    PRIMARY KEY (port, point));
CREATE TABLE IF NOT EXISTS glsea_daily (port TEXT, point TEXT, day TEXT, temp_c REAL, PRIMARY KEY (port, point, day));"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    return conn


def model_points(port: str) -> dict[str, tuple[float, float]]:
    rows = sqlite3.connect(DB).execute("SELECT point, lat, lon FROM lmhofs_points WHERE port=?", (port,)).fetchall()
    return {p: (lat, lon) for p, lat, lon in rows}


def box(port: str) -> tuple[float, float, float, float]:
    pts = model_points(port).values()
    return (min(p[0] for p in pts) - PAD, max(p[0] for p in pts) + PAD,
            min(p[1] for p in pts) - PAD, max(p[1] for p in pts) + PAD)


def latest_day() -> str:
    """Newest day GLSEA has (it updates daily, about a day behind)."""
    text = urllib.request.urlopen(f"{ERDDAP}/griddap/{DATASET}.csv?time%5Blast%5D", timeout=60).read().decode()
    return list(csv.reader(text.splitlines()))[2][0][:10]


def fetch(port: str, first: str, last: str) -> str:
    """One request: the port's box for days first..last (inclusive), as CSV text. Retried on errors; stores nothing."""
    lat0, lat1, lon0, lon1 = box(port)
    url = (f"{ERDDAP}/griddap/{DATASET}.csv?sst%5B({first}T12:00:00Z):1:({last}T12:00:00Z)%5D"
           f"%5B({lat0:.3f}):1:({lat1:.3f})%5D%5B({lon0:.3f}):1:({lon1:.3f})%5D")
    for wait in (*RETRIES, None):
        try:
            text = urllib.request.urlopen(url, timeout=120).read().decode()
            if not text.startswith("time,latitude,longitude,sst"):
                raise ValueError(f"unexpected reply: {text[:100]!r}")
            return text
        except Exception as e:
            if wait is None:
                raise
            print(f"    {type(e).__name__}: {e}; retrying in {wait}s", flush=True)
            time.sleep(wait)


def parse(text: str) -> list[tuple[str, float, float, float]]:
    """CSV text -> (day, lat, lon, C) for water cells; land and missing values (NaN) are dropped."""
    return [(r[0][:10], round(float(r[1]), 4), round(float(r[2]), 4), float(r[3]))
            for r in list(csv.reader(text.splitlines()))[2:] if r[3] not in ("NaN", "")]


def cells(conn: sqlite3.Connection, port: str, rows: list) -> dict[tuple[float, float], list[str]]:
    """(lat, lon) -> points for this port. Picked once, from the first reply, as the nearest water cell to each
    lake-model point; later runs reuse glsea_points so a point always means the same cell. Near shore two points
    can share a cell (GLSEA leaves the strip nearest shore empty, e.g. pier and 40 ft), hence a list."""
    saved = conn.execute("SELECT point, lat, lon FROM glsea_points WHERE port=?", (port,)).fetchall()
    out = defaultdict(list)
    if saved:
        for p, lat, lon in saved:
            out[(lat, lon)].append(p)
        return out
    water = {(lat, lon) for _, lat, lon, _ in rows}
    for point, (la, lo) in model_points(port).items():
        km = lambda c: math.hypot((c[0] - la) * 111.2, (c[1] - lo) * 111.2 * math.cos(math.radians(la)))
        cell = min(water, key=km)
        conn.execute("INSERT INTO glsea_points VALUES (?,?,?,?,?)", (port, point, *cell, round(km(cell), 2)))
        out[cell].append(point)
        print(f"  {port} {point}: GLSEA cell {cell[0]}, {cell[1]}, {km(cell):.1f} km from the lake-model point")
    return out


def pieces(start: str, end: str) -> list[tuple[str, str]]:
    """Split start..end at month boundaries, so no request covers more than a month."""
    out, first = [], date.fromisoformat(start)
    while first <= date.fromisoformat(end):
        month_end = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        last = min(month_end, date.fromisoformat(end))
        out.append((first.isoformat(), last.isoformat()))
        first = last + timedelta(days=1)
    return out


def download(start: str, end: str) -> None:
    """Fetch start..end for every port and store each point's daily value (existing days are overwritten)."""
    conn = connect()
    todo = pieces(start, end)
    print(f"GLSEA {start} to {end}: {len(todo)} piece(s) x {len(PORTS)} ports", flush=True)
    for i, (first, last) in enumerate(todo, 1):
        for port in PORTS:
            t0 = time.monotonic()
            rows = parse(fetch(port, first, last))
            points_of = cells(conn, port, rows)
            values = [(port, p, day, c) for day, lat, lon, c in rows for p in points_of.get((lat, lon), [])]
            conn.executemany("INSERT OR REPLACE INTO glsea_daily VALUES (?,?,?,?)", values)
            conn.commit()
            print(f"  [{i}/{len(todo)}] {port} {first} to {last}: {len(values)} values, {time.monotonic() - t0:.0f}s", flush=True)


def update() -> None:
    """From the day after the newest stored day (the earliest across ports, so a run that stopped partway
    resumes) to GLSEA's newest day. An empty table starts at START."""
    conn = connect()
    newest = [conn.execute("SELECT MAX(day) FROM glsea_daily WHERE port=?", (p,)).fetchone()[0] for p in PORTS]
    start = START if None in newest else (date.fromisoformat(min(newest)) + timedelta(days=1)).isoformat()
    end = latest_day()
    if start > end:
        print(f"GLSEA up to date ({end})")
        return
    download(start, end)


def week_summary(port: str, end: str, days: int = 7) -> dict | None:
    """GLSEA at the port's pier cell over the `days` before `end` (inclusive): average, change over the week and
    over the last 3 days, in F, plus how far the cell is from the pierhead."""
    port = ALIAS.get(port, port)
    conn = sqlite3.connect(DB)
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT day, temp_c FROM glsea_daily WHERE port=? AND point='pier' AND day>? AND day<=? ORDER BY day",
                        (port, start, end)).fetchall()
    if len(rows) < 4:
        return None
    km = conn.execute("SELECT km_from_point FROM glsea_points WHERE port=? AND point='pier'", (port,)).fetchone()[0]
    f = [F(c) for _, c in rows]
    return {"days": len(rows), "last_day": rows[-1][0], "km": km, "mean_f": round(sum(f) / len(f), 1),
            "last_f": round(f[-1], 1), "change_f": round(f[-1] - f[0], 1), "change_3d_f": round(f[-1] - f[-4], 1)}


def week_summary_text(port: str, end: str) -> str:
    w = week_summary(port, end)
    if not w:
        return "surface water near the pier (GLSEA satellite): no data"
    return (f"surface water near the pier (GLSEA satellite, cell ~{w['km']:.1f} km off the pierhead): avg {w['mean_f']:.0f} F, "
            f"{round(w['change_f']):+d} F over the week, {round(w['change_3d_f']):+d} F in the last 3 days")


def latest_text(port: str, as_of: str) -> str:
    """Live view: the newest GLSEA day at or before as_of (GLSEA runs about a day behind)."""
    w = week_summary(port, as_of)
    if not w:
        return "Surface water near the pier (GLSEA satellite): no recent data"
    return (f"Surface water near the pier (GLSEA satellite, {w['last_day']}, cell ~{w['km']:.1f} km off the pierhead; "
            f"can lag fast changes by 1-2 days): {w['last_f']:.0f} F, {round(w['change_3d_f']):+d} F over the last 3 days")


if __name__ == "__main__":
    if sys.argv[1:] == ["update"]:
        update()
    elif len(sys.argv) == 4 and sys.argv[1] == "download":
        download(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
