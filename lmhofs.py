"""Daily water-temperature profiles from NOAA's lake model (LMHOFS), stored in fishing.db.

  python lmhofs.py backfill 2026-03-15 2026-10-18   # one snapshot per day (12z nowcast hour)
  python lmhofs.py update                           # latest model run, if the newest stored one is over 12 h old

Points per port: the water cell nearest the pierhead ("pier") and offshore cells west of the harbor in
~40, 80, 120 and 160 ft of water. Each model file is ~146 MB, but only a few blocks hold these points, so
they are read over HTTP byte ranges (a few MB per file) and nothing is written to disk except the profiles.
Regular-grid files (fixed depth levels) exist from about March 2026; earlier history would need the
native "fields" files (sigma layers).
"""
import io
import json
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import h5py
import numpy as np

from conditions import DB, FT, PORT_XY, F

S3 = "https://noaa-nos-ofs-pds.s3.amazonaws.com/lmhofs/netcdf"
EPOCH = datetime(2018, 1, 1, tzinfo=timezone.utc)
PORTS = ["Muskegon", "Grand Haven", "Holland", "South Haven", "St. Joseph"]
ALIAS = {"Muskegon Lake": "Muskegon", "Black River": "South Haven", "St. Joseph River": "St. Joseph"}
OFFSHORE_FT = (40, 80, 120, 160)
FILL = -1000  # temp fill value is -99999


class HTTPRange(io.RawIOBase):
    """Read-only file over HTTP byte ranges, so h5py fetches only the blocks it needs."""

    def __init__(self, url: str, block: int = 64 << 10):
        self.url, self.pos, self.block, self.cache, self.fetched = url, 0, block, {}, 0
        head = urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30)
        self.size = int(head.headers["Content-Length"])

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def _block(self, i: int) -> bytes:
        if i not in self.cache:
            a = i * self.block
            req = urllib.request.Request(self.url, headers={"Range": f"bytes={a}-{min(a + self.block, self.size) - 1}"})
            self.cache[i] = urllib.request.urlopen(req, timeout=60).read()
            self.fetched += len(self.cache[i])
        return self.cache[i]

    def prefetch(self, ranges: list[tuple[int, int]], workers: int = 16) -> None:
        """Fetch the blocks covering these (start, end) byte ranges in parallel, before h5py asks for them."""
        need = sorted({i for a, b in ranges for i in range(a // self.block, (b - 1) // self.block + 1)} - set(self.cache))
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(self._block, need))

    def readinto(self, buf):
        n, out = min(len(buf), self.size - self.pos), bytearray()
        while len(out) < n:
            i, off = divmod(self.pos + len(out), self.block)
            out += self._block(i)[off:off + n - len(out)]
        buf[:n] = out
        self.pos += n
        return n


def _url(day: date, cycle: str) -> str:
    return f"{S3}/{day:%Y/%m/%d}/lmhofs.t{cycle}z.{day:%Y%m%d}.regulargrid.f000.nc"


def _open(url: str):
    raw = HTTPRange(url)
    return raw, h5py.File(io.BufferedReader(raw, 64 << 10), "r")


def _box_ranges(temp, y0: int, y1: int, x0: int, x1: int) -> list[tuple[int, int]]:
    """Byte ranges of temp[0, :, y0:y1+1, x0:x1+1] in the file (chunks are uncompressed, C order)."""
    _, cz, cy, cx = temp.chunks
    ranges = []
    for z in range(0, temp.shape[1], cz):
        for ty in range(y0 // cy * cy, y1 + 1, cy):
            for tx in range(x0 // cx * cx, x1 + 1, cx):
                start = temp.id.get_chunk_info_by_coord((0, z, ty, tx)).byte_offset
                for dz in range(min(cz, temp.shape[1] - z)):
                    a, b = max(y0, ty) - ty, min(y1, ty + cy - 1) - ty  # rows of this chunk inside the box
                    ranges.append((start + ((dz * cy + a) * cx) * 4, start + ((dz * cy + b + 1) * cx) * 4))
    return ranges


def _conn():
    c = sqlite3.connect(DB)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS lmhofs_points (port TEXT, point TEXT, iy INTEGER, ix INTEGER, lat REAL, lon REAL,
            bottom_ft INTEGER, PRIMARY KEY (port, point));
        CREATE TABLE IF NOT EXISTS lmhofs_profiles (port TEXT, point TEXT, model_time TEXT, depth_ft TEXT, temp_f TEXT,
            PRIMARY KEY (port, point, model_time));""")
    return c


def _points(conn, url: str) -> list:
    """Grid cells per port, chosen once from the (fixed) model grid and stored."""
    rows = conn.execute("SELECT port, point, iy, ix FROM lmhofs_points").fetchall()
    if rows:
        return rows
    raw, h = _open(url)
    try:
        la, lo, depth, water = h["Latitude"][:], h["Longitude"][:], h["h"][:], h["mask"][:] == 1
    finally:
        h.close()
    for port in PORTS:
        lat, lon = PORT_XY[port]
        d2 = np.where(water, (la - lat) ** 2 + ((lo - lon) * np.cos(np.radians(lat))) ** 2, np.inf)
        picks = {"pier": np.unravel_index(np.argmin(d2), d2.shape)}
        km = np.sqrt(d2) * 111
        near = water & (km <= 25) & (np.abs(la - lat) < 0.1) & (lo < lon)  # within 25 km, roughly west of the harbor
        for ft in OFFSHORE_FT:
            cost = np.where(near, np.abs(depth * FT - ft) + km, np.inf)  # depth closest to target; 1 km counts as 1 ft
            picks[str(ft)] = np.unravel_index(np.argmin(cost), cost.shape)
        for point, (iy, ix) in picks.items():
            conn.execute("INSERT INTO lmhofs_points VALUES (?,?,?,?,?,?,?)",
                         (port, point, int(iy), int(ix), round(float(la[iy, ix]), 3), round(float(lo[iy, ix]), 3),
                          round(float(depth[iy, ix]) * FT)))
    conn.commit()
    return conn.execute("SELECT port, point, iy, ix FROM lmhofs_points").fetchall()


def fetch(day: date, cycles=("12", "18", "06", "00")) -> str | None:
    """Store profiles at all points from one model file (first cycle that exists). Returns the model time."""
    conn = _conn()
    for cycle in cycles:
        url = _url(day, cycle)
        try:
            raw, h = _open(url)
        except Exception:
            continue
        try:
            pts = _points(conn, url)
            t = (EPOCH + timedelta(seconds=float(h["time"][0]))).isoformat()
            ys, xs = [p[2] for p in pts], [p[3] for p in pts]
            y0, x0 = min(ys), min(xs)
            raw.prefetch(_box_ranges(h["temp"], y0, max(ys), x0, max(xs)))
            box = h["temp"][0, :, y0:max(ys) + 1, x0:max(xs) + 1]  # one read covering every point
            levels_ft = [round(d * FT) for d in h["Depth"][:]]
        finally:
            h.close()
        for port, point, iy, ix in pts:
            col = box[:, iy - y0, ix - x0]
            keep = col > FILL
            conn.execute("INSERT OR REPLACE INTO lmhofs_profiles VALUES (?,?,?,?,?)",
                         (port, point, t, json.dumps([d for d, k in zip(levels_ft, keep) if k]),
                          json.dumps([round(F(float(c)), 1) for c in col[keep]])))
        conn.commit()
        print(f"{day} {cycle}z: {len(pts)} profiles, {raw.fetched / 1e6:.1f} MB read")
        return t
    print(f"{day}: no regular-grid file")
    return None


def backfill(start: date, end: date) -> None:
    have = {r[0][:10] for r in _conn().execute("SELECT DISTINCT model_time FROM lmhofs_profiles")}
    day = start
    while day <= end:
        if day.isoformat() not in have:
            try:
                fetch(day)
            except Exception as e:  # keep going; a rerun fills the gaps
                print(f"{day}: failed ({e})")
        day += timedelta(days=1)


def update(max_age_h: float = 12) -> None:
    """Fetch the newest model run unless the newest stored one is recent enough."""
    newest = _conn().execute("SELECT MAX(model_time) FROM lmhofs_profiles").fetchone()[0]
    now = datetime.now(timezone.utc)
    if newest and (now - datetime.fromisoformat(newest)).total_seconds() < max_age_h * 3600:
        return
    for back in range(3):
        if fetch((now - timedelta(days=back)).date(), cycles=("18", "12", "06", "00")):
            return


def profiles(port: str, before: str | None = None) -> dict:
    """Newest stored profile per point for a port (optionally at or before an ISO time): {point: {...}}."""
    port = ALIAS.get(port, port)
    q = """SELECT p.point, p.model_time, p.depth_ft, p.temp_f, pt.bottom_ft FROM lmhofs_profiles p
             JOIN lmhofs_points pt USING (port, point)
            WHERE p.port = ? AND p.model_time = (SELECT MAX(model_time) FROM lmhofs_profiles
                                                  WHERE port = ? AND model_time <= ?)"""
    rows = _conn().execute(q, (port, port, before or "9999")).fetchall()
    return {r[0]: {"model_time": r[1], "depth_ft": json.loads(r[2]), "temp_f": json.loads(r[3]), "bottom_ft": r[4]}
            for r in rows}


if __name__ == "__main__":
    if sys.argv[1] == "backfill":
        backfill(date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3]))
    elif sys.argv[1] == "update":
        update(0)
