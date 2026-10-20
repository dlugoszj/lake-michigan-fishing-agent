"""Daily wind history per port (Open-Meteo Historical Forecast archive), stored in fishing.db.

  python -m conditions.wind_history build    # April 2022 to yesterday, all ports (5 requests, free)

Why this archive: compared with measured buoy wind (May-Oct 2024-2025), its hourly speeds correlated
0.71-0.84 vs 0.55-0.72 for the ERA5 reanalysis, whose grid points fell 10-15 km inland.
The raw hourly download is kept in data/cache/ (not committed), so the table can be rebuilt offline.

upwelling_hours: hours with wind from the north round to the east (315-135 deg) at >= 8 mph. On Michigan's
east shore, north wind pushes surface water offshore (Coriolis turns it right) and east wind blows it
offshore; cold water wells up in its place and brings salmon and bait toward the piers.
"""
import json
import math
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

from conditions.lake_model import ALIAS, PORTS
from conditions.water_weather import CACHE, DB, PORT_XY

API = "https://historical-forecast-api.open-meteo.com/v1/forecast"
START = "2022-04-01"
UPWELLING_FROM = (315, 135)  # wind direction (from), wrapping through north
UPWELLING_MIN_MPH = 8

SCHEMA = """CREATE TABLE IF NOT EXISTS wind_daily (
    port TEXT, day TEXT, mean_mph REAL, max_mph REAL, gust_max_mph REAL, dir_deg INTEGER,
    upwelling_hours INTEGER, hours INTEGER, PRIMARY KEY (port, day))"""


def is_upwelling_wind(deg: float, mph: float) -> bool:
    lo, hi = UPWELLING_FROM
    return mph >= UPWELLING_MIN_MPH and (deg >= lo or deg <= hi)


def download(port: str, end: str) -> dict:
    """Hourly wind for a port, START to end, in Michigan local time; cached in data/cache/."""
    lat, lon = PORT_XY[port]
    url = (f"{API}?latitude={lat}&longitude={lon}&start_date={START}&end_date={end}"
           "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m&wind_speed_unit=mph&timezone=America%2FDetroit")
    data = json.loads(urllib.request.urlopen(url, timeout=300).read())
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"wind_{port.replace(' ', '_').replace('.', '')}.json").write_text(json.dumps(data))
    return data


def daily(hourly: dict) -> list[tuple]:
    """Hourly arrays -> one (day, mean, max, gust max, vector-mean direction, upwelling hours, hours) per day."""
    by_day = defaultdict(list)
    for t, mph, deg, gust in zip(hourly["time"], hourly["wind_speed_10m"], hourly["wind_direction_10m"],
                                 hourly["wind_gusts_10m"]):
        if mph is not None and deg is not None:
            by_day[t[:10]].append((mph, deg, gust))
    out = []
    for day, hrs in sorted(by_day.items()):
        speeds = [h[0] for h in hrs]
        # vector mean of the "from" direction, weighted by speed: 350 and 10 average to 0, not 180
        s = sum(m * math.sin(math.radians(d)) for m, d, _ in hrs)
        c = sum(m * math.cos(math.radians(d)) for m, d, _ in hrs)
        direction = round(math.degrees(math.atan2(s, c)) % 360) if (s or c) else None
        gusts = [g for _, _, g in hrs if g is not None]
        out.append((day, round(sum(speeds) / len(speeds), 1), round(max(speeds), 1), round(max(gusts), 1) if gusts else None,
                    direction, sum(is_upwelling_wind(d, m) for m, d, _ in hrs), len(hrs)))
    return out


def build() -> None:
    conn = sqlite3.connect(DB)
    conn.execute(SCHEMA)
    end = (date.today() - timedelta(days=1)).isoformat()
    for port in PORTS:
        rows = daily(download(port, end)["hourly"])
        conn.executemany("INSERT OR REPLACE INTO wind_daily VALUES (?,?,?,?,?,?,?,?)", [(port, *r) for r in rows])
        conn.commit()
        print(f"{port}: {len(rows)} days ({rows[0][0]} to {rows[-1][0]})")


def week_summary(port: str, end: str, days: int = 7) -> dict | None:
    """Wind over the `days` before `end` (inclusive): average speed, strongest gust, upwelling hours in the last 3 and 7 days."""
    port = ALIAS.get(port, port)
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    rows = sqlite3.connect(DB).execute(
        "SELECT day, mean_mph, gust_max_mph, upwelling_hours FROM wind_daily WHERE port=? AND day>? AND day<=? ORDER BY day",
        (port, start, end)).fetchall()
    if len(rows) < days - 1:
        return None
    last3 = (date.fromisoformat(end) - timedelta(days=3)).isoformat()
    return {"days": len(rows), "mean_mph": round(sum(r[1] for r in rows) / len(rows), 1),
            "gust_max_mph": max((r[2] for r in rows if r[2] is not None), default=None),
            "upwelling_hours_7d": sum(r[3] for r in rows), "upwelling_hours_3d": sum(r[3] for r in rows if r[0] > last3)}


def week_summary_text(port: str, end: str) -> str:
    w = week_summary(port, end)
    if not w:
        return "wind that week: no wind history"
    gust = f", gusts to {w['gust_max_mph']:.0f} mph" if w["gust_max_mph"] is not None else ""
    return (f"wind that week: avg {w['mean_mph']:.0f} mph{gust}; upwelling wind (N to E, 8+ mph) "
            f"{w['upwelling_hours_7d']} h in 7 days, {w['upwelling_hours_3d']} h in the last 3")


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build()
    else:
        print(__doc__)
