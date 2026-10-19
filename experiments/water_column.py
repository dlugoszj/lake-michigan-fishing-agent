"""Water temperature at depth from NOAA's Lake Michigan-Huron model (LMHOFS), plus a check against buoys.

Two LMHOFS products are used:
  - regulargrid files: temperature on fixed depth levels over a lat/lon grid (current files; not in the 2025 archive)
  - stations files:    hourly profiles on 20 even sigma layers at fixed points, including the NDBC buoys (small, full archive)

Feasibility prototype: extracts profiles off each port for one date, and compares the model's
surface temperature with what the buoys measured at the same hour.
"""
import gzip
import io
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import xarray as xr

S3 = "https://noaa-nos-ofs-pds.s3.amazonaws.com/lmhofs/netcdf"
EPOCH = datetime(2018, 1, 1, tzinfo=timezone.utc)
FT = 3.28084

# Approximate harbor-mouth coordinates (for picking offshore grid points) and the NDBC buoy for each port.
PORTS = {
    "Muskegon":    {"lat": 43.227, "lon": -86.343, "buoy": "45161"},
    "Grand Haven": {"lat": 43.058, "lon": -86.256, "buoy": None},
    "Holland":     {"lat": 42.773, "lon": -86.213, "buoy": "45029"},
    "South Haven": {"lat": 42.402, "lon": -86.287, "buoy": "45168"},
    "St. Joseph":  {"lat": 42.116, "lon": -86.488, "buoy": "45026"},
}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def stations_profiles(path: str) -> dict:
    """Last time step of a stations nowcast file -> {station: {"time", "depth_m", "temp_c", "bottom_m"}}."""
    ds = xr.open_dataset(path, engine="h5netcdf", decode_times=False)
    names = [n.decode().strip() if isinstance(n, bytes) else str(n).strip() for n in ds["name_station"].values]
    t = EPOCH + timedelta(seconds=float(ds["time"].values[-1]))
    out = {}
    for i, n in enumerate(names):
        h = float(ds["h"].values[i]) + float(ds["zeta"].values[-1, i])
        depth = -ds["siglay"].values[:, i] * h
        lon = float(ds["lon"].values[i])
        out[n] = {"time": t.isoformat(), "lat": round(float(ds["lat"].values[i]), 3), "lon": round(lon - 360 if lon > 180 else lon, 3),
                  "depth_m": depth.round(2).tolist(),
                  "temp_c": ds["temp"].values[-1, :, i].round(2).tolist(), "bottom_m": round(h, 1)}
    return out


def grid_profile(ds, lat: float, lon: float, bottom_m: float | None = None) -> dict:
    """Profile at the water cell nearest (lat, lon); with bottom_m, the cell west of the port whose depth is closest to it."""
    la, lo, h = ds["Latitude"].values, ds["Longitude"].values, ds["h"].values
    water = np.isfinite(ds["temp"].values[0, 0])
    if bottom_m is None:
        d2 = (la - lat) ** 2 + ((lo - lon) * np.cos(np.radians(lat))) ** 2
        d2[~water] = np.inf
    else:  # offshore: same latitude band, west of the harbor, depth closest to target
        band = water & (np.abs(la - lat) < 0.03) & (lo < lon)
        d2 = np.where(band, np.abs(h - bottom_m) + 0.01 * np.abs(la - lat) * 111, np.inf)
    iy, ix = np.unravel_index(np.argmin(d2), d2.shape)
    temps = ds["temp"].values[0, :, iy, ix]
    depths = ds["Depth"].values
    keep = np.isfinite(temps)
    return {"lat": round(float(la[iy, ix]), 3), "lon": round(float(lo[iy, ix]), 3), "bottom_m": round(float(h[iy, ix]), 1),
            "depth_m": depths[keep].round(1).tolist(), "temp_c": temps[keep].round(2).tolist()}


def buoy_water_temp(station: str, when: datetime) -> float | None:
    """Measured surface water temperature (NDBC WTMP) closest to `when`, within 1 hour."""
    now = datetime.now(timezone.utc)
    if (now - when).days < 40:
        text = fetch(f"https://www.ndbc.noaa.gov/data/realtime2/{station}.txt").decode()
    else:
        raw = fetch(f"https://www.ndbc.noaa.gov/view_text_file.php?filename={station}h{when.year}.txt.gz"
                    f"&dir=data/historical/stdmet/")
        text = gzip.decompress(raw).decode() if raw[:2] == b"\x1f\x8b" else raw.decode()
    best = None
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        p = line.split()
        t = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), tzinfo=timezone.utc)
        if p[14] in ("MM", "999.0"):
            continue
        dt = abs((t - when).total_seconds())
        if dt <= 3600 and (best is None or dt < best[0]):
            best = (dt, float(p[14]))
    return best[1] if best else None


def thermocline(depth_m, temp_c) -> dict | None:
    """Depth of the steepest temperature drop (deg C per m) between adjacent levels."""
    d, t = np.array(depth_m), np.array(temp_c)
    if len(d) < 3:
        return None
    g = -np.diff(t) / np.diff(d)
    i = int(np.argmax(g))
    return {"depth_m": round(float((d[i] + d[i + 1]) / 2), 1), "drop_c_per_m": round(float(g[i]), 2)}


if __name__ == "__main__":
    grid_file, st_now, st_summer, out = sys.argv[1:5]
    ds = xr.open_dataset(grid_file, engine="h5netcdf", decode_times=False)
    t_now = EPOCH + timedelta(seconds=float(ds["time"].values[0]))
    now_st, summer_st = stations_profiles(st_now), stations_profiles(st_summer)
    t_summer = datetime.fromisoformat(next(iter(summer_st.values()))["time"])
    result = {"now": t_now.isoformat(), "summer": t_summer.isoformat(), "ports": {}, "validation": []}
    for port, p in PORTS.items():
        r = {"offshore_now": grid_profile(ds, p["lat"], p["lon"], bottom_m=45.7)}  # ~150 ft of water
        if p["buoy"]:
            r["buoy_now"] = now_st[p["buoy"]]
            r["buoy_summer"] = summer_st[p["buoy"]]
            b = now_st[p["buoy"]]
            g = grid_profile(ds, b["lat"], b["lon"])  # grid profile at the buoy, to cross-check the two model products
            for label, when, model_surface in (("now", t_now, now_st[p["buoy"]]["temp_c"][0]),
                                               ("summer", t_summer, summer_st[p["buoy"]]["temp_c"][0])):
                result["validation"].append({"port": port, "buoy": p["buoy"], "when": when.isoformat(),
                                             "model_surface_c": model_surface, "buoy_measured_c": buoy_water_temp(p["buoy"], when),
                                             "grid_surface_c": g["temp_c"][0] if label == "now" else None})
        for k, v in r.items():
            v["thermocline"] = thermocline(v["depth_m"], v["temp_c"])
        result["ports"][port] = r
    json.dump(result, open(out, "w"), indent=1)
    print(json.dumps(result["validation"], indent=1))
    for port, r in result["ports"].items():
        print(port, {k: (v["bottom_m"], v["temp_c"][0], v["temp_c"][-1], v["thermocline"]) for k, v in r.items()})
