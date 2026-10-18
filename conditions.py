"""Water and weather conditions for the agent: measured surface temperature (NDBC buoys), the depth of the
cold layer (NOAA LMHOFS model), and wind forecasts (Open-Meteo).

  python conditions.py build     # download buoy history 2022-today into fishing.db (table buoy_daily)
  python conditions.py now PORT  # print current conditions for a port

Middle-ground design: two numbers per port, not full profiles.
  - surface temperature: measured by the buoy (accurate) + its trend
  - cold-layer depth: modeled (LMHOFS); the depth where water first drops to 55 F, plus the 52-58 F band
    (Chinook/coho preferred range, NY DEC; Michigan Sea Grant: mature Chinook hold in water of 55 F or less)
The model's surface was ~2 C off the buoys in our check, so cold-layer depths are approximate.
"""
import gzip
import json
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

DB = "fishing.db"
CACHE = Path("cache")
F = lambda c: c * 9 / 5 + 32
FT = 3.28084
COLD_F, BAND_F = 55.0, (52.0, 58.0)
UPWELLING_DROP_C = 3.0  # heuristic: daily-mean surface drop of >= 3 C within two days

# Nearest NDBC buoy(s) per port, first available wins. Grand Haven sits between Muskegon and Holland.
BUOYS = {
    "Muskegon": ["45161", "45029"], "Muskegon Lake": ["45161", "45029"], "Grand Haven": ["45161", "45029"],
    "Holland": ["45029", "45168"], "South Haven": ["45168", "45029"], "Black River": ["45168", "45029"],
    "St. Joseph": ["45026", "45168"], "St. Joseph River": ["45026", "45168"],
}
# Approximate harbor-mouth coordinates (offshore model points are picked west of these).
PORT_XY = {
    "Muskegon": (43.227, -86.343), "Muskegon Lake": (43.227, -86.343), "Grand Haven": (43.058, -86.256),
    "Holland": (42.773, -86.213), "South Haven": (42.402, -86.287), "Black River": (42.402, -86.287),
    "St. Joseph": (42.116, -86.488), "St. Joseph River": (42.116, -86.488),
}
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _get(url: str, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _parse_stdmet(text: str):
    """NDBC standard meteorological text -> list of (utc datetime, wtmp C, wspd m/s, wdir deg, wvht m)."""
    rows = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        p = line.split()
        try:
            t = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
        val = lambda i: None if p[i] in ("MM", "99.0", "999.0", "999", "99.00") else float(p[i])
        rows.append((t, val(14), val(6), val(5), val(8)))
    return rows


def _station_text(station: str, year: int, month: int | None = None) -> str:
    """Yearly file for past years; monthly file for the current year (cached on disk)."""
    CACHE.mkdir(exist_ok=True)
    name = f"{station}h{year}" if month is None else f"{station}{month}{year}"
    path = CACHE / f"ndbc_{name}.txt"
    if path.exists():
        return path.read_text()
    folder = "data/historical/stdmet/" if month is None else f"data/stdmet/{MON[month - 1]}/"
    try:
        raw = _get(f"https://www.ndbc.noaa.gov/view_text_file.php?filename={name}.txt.gz&dir={folder}")
    except Exception:
        return ""
    text = gzip.decompress(raw).decode() if raw[:2] == b"\x1f\x8b" else raw.decode(errors="replace")
    if not text.startswith("#YY"):
        return ""
    path.write_text(text)
    return text


def build(first_year: int = 2022) -> None:
    """Daily surface-temperature summaries per buoy, 2022 to today, into buoy_daily."""
    conn = sqlite3.connect(DB)
    conn.executescript("""CREATE TABLE IF NOT EXISTS buoy_daily (
        station TEXT, day TEXT, wtmp_mean_c REAL, wtmp_min_c REAL, wtmp_max_c REAL, wspd_mean_ms REAL, n INTEGER,
        PRIMARY KEY (station, day))""")
    today = date.today()
    for station in sorted({s for v in BUOYS.values() for s in v}):
        rows = []
        for y in range(first_year, today.year):
            rows += _parse_stdmet(_station_text(station, y))
        for m in range(1, today.month + 1):
            rows += _parse_stdmet(_station_text(station, today.year, m))
        try:
            rows += _parse_stdmet(_get(f"https://www.ndbc.noaa.gov/data/realtime2/{station}.txt").decode())
        except Exception:
            pass
        days = defaultdict(list)
        winds = defaultdict(list)
        for t, wt, ws, _, _ in rows:
            local = (t - timedelta(hours=4)).date().isoformat()  # Eastern (summer) day, close enough for daily means
            if wt is not None:
                days[local].append(wt)
            if ws is not None:
                winds[local].append(ws)
        for d, v in days.items():
            w = winds.get(d)
            conn.execute("INSERT OR REPLACE INTO buoy_daily VALUES (?,?,?,?,?,?,?)",
                         (station, d, round(float(np.mean(v)), 2), min(v), max(v), round(float(np.mean(w)), 2) if w else None, len(v)))
        conn.commit()
        print(f"{station}: {len(days)} days with water temperature ({min(days) if days else '-'} to {max(days) if days else '-'})")


def week_summary(port: str, end: str, days: int = 7) -> dict | None:
    """Surface temperature over the `days` before `end` from the port's buoy: mean, change, possible upwelling."""
    conn = sqlite3.connect(DB)
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    for station in BUOYS.get(port, []):
        rows = conn.execute("SELECT day, wtmp_mean_c FROM buoy_daily WHERE station=? AND day>? AND day<=? ORDER BY day",
                            (station, start, end)).fetchall()
        if len(rows) >= 3:
            temps = [r[1] for r in rows]
            drops = [temps[i] - min(temps[i + 1:i + 3]) for i in range(len(temps) - 1)]
            return {"station": station, "days": len(rows), "mean_f": round(F(float(np.mean(temps))), 1),
                    "change_f": round((temps[-1] - temps[0]) * 9 / 5, 1), "min_f": round(F(min(temps)), 1),
                    "max_f": round(F(max(temps)), 1), "possible_upwelling": max(drops) >= UPWELLING_DROP_C}
    return None


def week_summary_text(port: str, end: str) -> str:
    w = week_summary(port, end)
    if not w:
        return "surface water that week: no buoy data (buoys are out roughly Nov-Apr)"
    s = f"surface water that week (buoy {w['station']}): avg {w['mean_f']:.0f} F, {w['change_f']:+.0f} F over the week"
    return s + (", possible upwelling (sharp drop)" if w["possible_upwelling"] else "")


# ---------- current conditions ----------

def buoy_now(port: str) -> dict | None:
    for station in BUOYS.get(port, []):
        try:
            rows = _parse_stdmet(_get(f"https://www.ndbc.noaa.gov/data/realtime2/{station}.txt", 30).decode())
        except Exception:
            continue
        rows = [r for r in rows if r[1] is not None]
        if not rows or (datetime.now(timezone.utc) - rows[0][0]).total_seconds() > 6 * 3600:
            continue  # buoy offline or pulled for the season
        latest = rows[0]
        ago = [r for r in rows if (latest[0] - r[0]).total_seconds() >= 72 * 3600]
        return {"station": station, "time": latest[0].isoformat(), "surface_f": round(F(latest[1]), 1),
                "change_72h_f": round((latest[1] - ago[0][1]) * 9 / 5, 1) if ago else None,
                "wind_mph": round(latest[2] * 2.237) if latest[2] is not None else None,
                "waves_ft": round(latest[4] * FT, 1) if latest[4] is not None else None}
    return None


def wind_forecast(port: str, days: int = 3) -> list[dict]:
    lat, lon = PORT_XY[port]
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=wind_speed_10m_max,"
           f"wind_gusts_10m_max,wind_direction_10m_dominant&wind_speed_unit=mph&forecast_days={days}&timezone=America%2FDetroit")
    d = json.loads(_get(url, 30))["daily"]
    compass = lambda deg: ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) // 45) % 8]
    return [{"day": d["time"][i], "wind_max_mph": round(d["wind_speed_10m_max"][i]), "gusts_mph": round(d["wind_gusts_10m_max"][i]),
             "from": compass(d["wind_direction_10m_dominant"][i])} for i in range(len(d["time"]))]


def _latest_grid_file() -> Path:
    """Most recent LMHOFS regular-grid nowcast hour (f000), downloaded once per model cycle and cached."""
    (CACHE / "lmhofs").mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    for back in range(0, 3):
        d = now - timedelta(days=back)
        for cyc in ("18", "12", "06", "00"):
            name = f"lmhofs.t{cyc}z.{d:%Y%m%d}.regulargrid.f000.nc"
            path = CACHE / "lmhofs" / name
            if path.exists():
                return path
            url = f"https://noaa-nos-ofs-pds.s3.amazonaws.com/lmhofs/netcdf/{d:%Y/%m/%d}/{name}"
            try:
                req = urllib.request.Request(url, method="HEAD")
                urllib.request.urlopen(req, timeout=20)
            except Exception:
                continue
            for old in (CACHE / "lmhofs").glob("*.regulargrid.*.nc"):
                old.unlink()  # keep only the latest file (~146 MB)
            path.write_bytes(_get(url, 600))
            return path
    raise RuntimeError("no recent LMHOFS file found")


def _depth_where(depth_m, temp_c, target_f):
    """First depth (ft) where the profile reaches target_f going down, interpolated; None if it never does."""
    t = [F(c) for c in temp_c]
    if t[0] <= target_f:
        return 0
    for i in range(len(t) - 1):
        if t[i] > target_f >= t[i + 1]:
            dm = depth_m[i] + (depth_m[i + 1] - depth_m[i]) * (t[i] - target_f) / (t[i] - t[i + 1])
            return round(dm * FT)
    return None


def cold_layer(port: str) -> dict:
    """Modeled cold-layer depth offshore of a port, at points in ~80, 120 and 160 ft of water."""
    import xarray as xr
    from water_column import grid_profile
    path = _latest_grid_file()
    cache = path.with_suffix(".coldlayer.json")
    data = json.loads(cache.read_text()) if cache.exists() else {}
    if port not in data:
        ds = xr.open_dataset(path, engine="h5netcdf", decode_times=False)
        t = datetime(2018, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(ds["time"].values[0]))
        pts = []
        for bottom_ft in (80, 120, 160):
            p = grid_profile(ds, *PORT_XY[port], bottom_m=bottom_ft / FT)
            pts.append({"water_ft": round(p["bottom_m"] * FT), "surface_f": round(F(p["temp_c"][0]), 1),
                        "bottom_f": round(F(p["temp_c"][-1]), 1), "first_55f_at_ft": _depth_where(p["depth_m"], p["temp_c"], COLD_F),
                        "band_52_58f_ft": [_depth_where(p["depth_m"], p["temp_c"], BAND_F[1]), _depth_where(p["depth_m"], p["temp_c"], BAND_F[0])]})
        data[port] = {"model_time": t.isoformat(), "points": pts}
        cache.write_text(json.dumps(data))
    return data[port]


def current_conditions_text(port: str) -> str:
    out = [f"Current conditions for {port}:"]
    b = buoy_now(port)
    if b:
        trend = "" if b["change_72h_f"] is None else f", {b['change_72h_f']:+.0f} F over the last 3 days"
        sharp = " (sharp drop: possible upwelling)" if (b["change_72h_f"] or 0) <= -UPWELLING_DROP_C * 9 / 5 else ""
        out.append(f"- Surface water (MEASURED, buoy {b['station']}, {b['time'][:16]}Z): {b['surface_f']:.0f} F{trend}{sharp}; "
                   f"wind {b['wind_mph']} mph, waves {b['waves_ft']} ft")
    else:
        out.append("- Surface water: no live buoy reading near this port (buoy offline or out for the season)")
    try:
        c = cold_layer(port)
        out.append(f"- Cold layer (MODELED by NOAA LMHOFS, {c['model_time'][:16]}Z; approximate, model surface was ~2 C off buoys in testing):")
        for p in c["points"]:
            first = "never reaches 55 F" if p["first_55f_at_ft"] is None else f"first 55 F at ~{p['first_55f_at_ft']} ft down"
            lo, hi = p["band_52_58f_ft"]
            band = "" if lo is None and hi is None else f"; 52-58 F band ~{lo if lo is not None else '?'}-{hi if hi is not None else 'bottom'} ft down"
            out.append(f"  - in ~{p['water_ft']} ft of water: surface {p['surface_f']:.0f} F, bottom {p['bottom_f']:.0f} F, {first}{band}")
    except Exception as e:
        out.append(f"- Cold layer: model data unavailable ({e})")
    try:
        out.append("- Wind forecast (Open-Meteo): " + "; ".join(
            f"{w['day']}: max {w['wind_max_mph']} mph from {w['from']}, gusts {w['gusts_mph']}" for w in wind_forecast(port)))
    except Exception as e:
        out.append(f"- Wind forecast unavailable ({e})")
    return "\n".join(out)


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build()
    elif sys.argv[1] == "now":
        print(current_conditions_text(" ".join(sys.argv[2:])))
