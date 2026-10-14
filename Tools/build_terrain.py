#!/usr/bin/env python3
"""DEM GeoTIFFs -> terrain (terrarium raster-dem) and contour (vector) tiles for a bbox.

Usage (inside .tools/venv):
  python -I Tools/build_terrain.py --bbox 126.90,37.58,127.06,37.74 --out MapData/pilot MapData/dem/*.tif

Writes terrain.mbtiles and contours.mbtiles; convert each with `pmtiles convert X.mbtiles X.pmtiles`.
Input: north-up EPSG:4326 GeoTIFFs (Copernicus GLO-30 now, NGII DEM later).
"""
import argparse, gzip, io, json, math, os, sqlite3, sys

import contourpy
import mapbox_vector_tile
import numpy as np
import tifffile
from PIL import Image

TERRAIN_ZOOMS = range(8, 13)     # 30 m data ~ z12 native; MapLibre overzooms beyond
CONTOUR_ZOOMS = range(12, 16)
INTERVAL = 20                    # contour interval (m); 30 m source data does not support finer
INDEX = 100                      # index contour every 100 m
EXTENT = 4096
BUFFER = 64


# ---------- DEM mosaic ----------

def load_mosaic(paths, bbox):
    w, s, e, n = bbox
    tiles = []
    for p in paths:
        with tifffile.TiffFile(p) as t:
            pg = t.pages[0]
            tie = pg.tags["ModelTiepointTag"].value
            sc = pg.tags["ModelPixelScaleTag"].value
            tiles.append((tie[3], tie[4], sc[0], sc[1], pg.asarray().astype(np.float32)))
    dx = min(t[2] for t in tiles)
    dy = min(t[3] for t in tiles)
    pad = 4 * dx
    W, S, E, N = w - pad, s - pad, e + pad, n + pad
    cols = int(math.ceil((E - W) / dx))
    rows = int(math.ceil((N - S) / dy))
    out = np.full((rows, cols), np.nan, np.float32)
    for x0, y0, sx, sy, a in tiles:
        h, wd = a.shape
        c0 = int(round((W - x0) / sx))
        r0 = int(round((y0 - N) / sy))
        rs, re = max(0, r0), min(h, r0 + rows)
        cs, ce = max(0, c0), min(wd, c0 + cols)
        if rs >= re or cs >= ce:
            continue
        out[rs - r0:re - r0, cs - c0:ce - c0] = a[rs:re, cs:ce]
    if np.isnan(out).any():
        sys.exit("DEM tiles do not cover the bbox")
    # pixel-is-area: cell (r, c) center
    return out, (W + dx / 2, N - dy / 2, dx, dy)


def sample(dem, geo, lon, lat):
    """Bilinear sample at arrays of lon/lat."""
    x0, y0, dx, dy = geo
    fx = (lon - x0) / dx
    fy = (y0 - lat) / dy
    h, w = dem.shape
    fx = np.clip(fx, 0, w - 1.001)
    fy = np.clip(fy, 0, h - 1.001)
    ix, iy = np.floor(fx).astype(int), np.floor(fy).astype(int)
    tx, ty = fx - ix, fy - iy
    a = dem[iy, ix] * (1 - tx) + dem[iy, ix + 1] * tx
    b = dem[iy + 1, ix] * (1 - tx) + dem[iy + 1, ix + 1] * tx
    return a * (1 - ty) + b * ty


class LazyDEM:
    """Samples a set of 1x1 degree GeoTIFFs without building one big mosaic (country-scale runs).
    Missing cells (sea) return NaN. Bilinear inside each file; edges are clamped to the file."""

    def __init__(self, paths, cache=6):
        self.files = {}
        for p in paths:
            with tifffile.TiffFile(p) as t:
                pg = t.pages[0]
                tie, sc = pg.tags["ModelTiepointTag"].value, pg.tags["ModelPixelScaleTag"].value
                self.files[(int(math.floor(tie[4] - 1e-9)) , int(round(tie[3])))] = (p, tie[3], tie[4], sc[0], sc[1])
        self.cache, self.order, self.limit = {}, [], cache

    def _load(self, key):
        if key not in self.cache:
            p, x0, y0, sx, sy = self.files[key]
            self.cache[key] = (tifffile.imread(p).astype(np.float32), (x0 + sx / 2, y0 - sy / 2, sx, sy))
            self.order.append(key)
            if len(self.order) > self.limit:
                self.cache.pop(self.order.pop(0))
        return self.cache[key]

    def __call__(self, lon, lat):
        out = np.full(lon.shape, np.nan, np.float32)
        cell_lat = np.floor(lat).astype(int)
        cell_lon = np.floor(lon).astype(int)
        for key in set(zip(cell_lat.ravel().tolist(), cell_lon.ravel().tolist())):
            if key not in self.files:
                continue
            m = (cell_lat == key[0]) & (cell_lon == key[1])
            dem, geo = self._load(key)
            out[m] = sample(dem, geo, lon[m], lat[m])
        return out


# ---------- tiling helpers ----------

def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def tile_range(bbox, z):
    w, s, e, n = bbox
    x0, y1 = lonlat_to_tile(w, s, z)
    x1, y0 = lonlat_to_tile(e, n, z)
    return range(int(x0), int(x1) + 1), range(int(y0), int(y1) + 1)


def pixel_lonlat(z, x, y, size):
    n = 2 ** z
    px = (x + (np.arange(size) + 0.5) / size) / n
    py = (y + (np.arange(size) + 0.5) / size) / n
    lon = px * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * py))))
    return np.meshgrid(lon, lat)


def mbtiles(path, meta):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    db.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    db.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    db.executemany("INSERT INTO metadata VALUES (?, ?)", list(meta.items()))
    return db


def put(db, z, x, y, data):
    db.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, x, (2 ** z - 1) - y, data))  # TMS row


# ---------- terrain ----------

def build_terrain(sampler, bbox, out):
    meta = {"name": "terrain", "format": "png", "type": "baselayer", "encoding": "terrarium",
            "minzoom": str(min(TERRAIN_ZOOMS)), "maxzoom": str(max(TERRAIN_ZOOMS)),
            "bounds": ",".join(map(str, bbox)), "attribution": "Copernicus DEM GLO-30 © DLR e.V. 2010-2014 and © Airbus 2014-2018"}
    db = mbtiles(os.path.join(out, "terrain.mbtiles"), meta)
    count = 0
    for z in TERRAIN_ZOOMS:
        xs, ys = tile_range(bbox, z)
        for x in xs:
            for y in ys:
                lon, lat = pixel_lonlat(z, x, y, 256)
                h = sampler(lon, lat).astype(np.float64)
                if np.isnan(h).all():
                    continue  # open sea: no tile
                h = np.nan_to_num(h, nan=0.0) + 32768
                rgb = np.stack([np.floor(h / 256), np.floor(h) % 256, np.floor((h % 1) * 256)], -1).astype(np.uint8)
                buf = io.BytesIO()
                Image.fromarray(rgb, "RGB").save(buf, "PNG", optimize=True)
                put(db, z, x, y, buf.getvalue())
                count += 1
    db.commit()
    db.close()
    return count


# ---------- contours ----------

def smooth(a):
    """3x3 box blur: removes single-cell DSM noise without moving lines much."""
    p = np.pad(a, 1, mode="edge")
    return sum(p[1 + dy:1 + dy + a.shape[0], 1 + dx:1 + dx + a.shape[1]] for dy in (-1, 0, 1) for dx in (-1, 0, 1)) / 9


def make_contours(dem, geo):
    x0, y0, dx, dy = geo
    z = smooth(dem)
    gen = contourpy.contour_generator(z=z, line_type=contourpy.LineType.Separate)
    lo = int(math.ceil(np.min(z) / INTERVAL) * INTERVAL)
    hi = int(np.max(z))
    lines = []
    for level in range(max(lo, INTERVAL), hi + 1, INTERVAL):
        for seg in gen.lines(level):
            if len(seg) < 3:
                continue
            lon = x0 + seg[:, 0] * dx
            lat = y0 - seg[:, 1] * dy
            lines.append((level, np.column_stack([lon, lat])))
    return lines


def clip_polyline(pts, lo, hi):
    """Clip a polyline (Nx2, tile units) to [lo, hi] square; returns list of runs."""
    runs, cur = [], []

    def inside(p):
        return lo <= p[0] <= hi and lo <= p[1] <= hi

    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        # Liang-Barsky
        t0, t1 = 0.0, 1.0
        d = b - a
        ok = True
        for p, q in ((-d[0], a[0] - lo), (d[0], hi - a[0]), (-d[1], a[1] - lo), (d[1], hi - a[1])):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                r = q / p
                if p < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
        if not ok or t0 > t1:
            if len(cur) > 1:
                runs.append(cur)
            cur = []
            continue
        pa, pb = a + t0 * d, a + t1 * d
        if not cur:
            cur = [pa]
        cur.append(pb)
        if t1 < 1.0:
            runs.append(cur)
            cur = []
    if len(cur) > 1:
        runs.append(cur)
    return runs


def dp(pts, tol):
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    ab = b - a
    n = np.hypot(*ab)
    rel = pts - a
    d = np.abs(ab[0] * rel[:, 1] - ab[1] * rel[:, 0]) / n if n > 0 else np.hypot(rel[:, 0], rel[:, 1])
    i = int(np.argmax(d))
    if d[i] > tol:
        return np.vstack([dp(pts[:i + 1], tol)[:-1], dp(pts[i:], tol)])
    return np.array([a, b])


def build_contours(lines, bbox, out):
    meta = {"name": "contours", "format": "pbf", "minzoom": str(min(CONTOUR_ZOOMS)), "maxzoom": str(max(CONTOUR_ZOOMS)),
            "bounds": ",".join(map(str, bbox)),
            "attribution": "Contours from Copernicus DEM GLO-30 © DLR e.V. 2010-2014 and © Airbus 2014-2018",
            "json": json.dumps({"vector_layers": [{"id": "contours", "fields": {"ele": "Number", "idx": "Number"}}]})}
    db = mbtiles(os.path.join(out, "contours.mbtiles"), meta)
    count = 0
    for z in CONTOUR_ZOOMS:
        n = 2 ** z
        xs, ys = tile_range(bbox, z)
        # project all lines once to world tile coordinates at this zoom
        proj = []
        for level, ll in lines:
            if z == 12 and level % INDEX:
                continue  # only index lines at the lowest zoom
            px = (ll[:, 0] + 180) / 360 * n
            py = (1 - np.arcsinh(np.tan(np.radians(ll[:, 1]))) / np.pi) / 2 * n
            proj.append((level, np.column_stack([px, py])))
        for x in xs:
            for y in ys:
                feats = []
                lo, hi = -BUFFER, EXTENT + BUFFER
                for level, wp in proj:
                    if wp[:, 0].max() < x - 0.02 or wp[:, 0].min() > x + 1.02 or wp[:, 1].max() < y - 0.02 or wp[:, 1].min() > y + 1.02:
                        continue
                    tp = (wp - [x, y]) * EXTENT
                    for run in clip_polyline(tp, lo, hi):
                        run = dp(np.array(run), 2.0)
                        if len(run) < 2:
                            continue
                        coords = [[int(round(px_)), int(round(EXTENT - py_))] for px_, py_ in run]  # y-up for the encoder
                        feats.append({"geometry": {"type": "LineString", "coordinates": coords},
                                      "properties": {"ele": level, "idx": 1 if level % INDEX == 0 else 0}})
                if not feats:
                    continue
                data = mapbox_vector_tile.encode([{"name": "contours", "features": feats}],
                                                 default_options={"extents": EXTENT})
                put(db, z, x, y, gzip.compress(data))
                count += 1
    db.commit()
    db.close()
    return count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True, help="west,south,east,north")
    ap.add_argument("--out", required=True)
    ap.add_argument("--terrain-only", action="store_true",
                    help="country-scale: terrain tiles from per-file sampling; build contours with gdal_contour + tippecanoe")
    ap.add_argument("dems", nargs="+")
    a = ap.parse_args()
    bbox = tuple(float(v) for v in a.bbox.split(","))
    os.makedirs(a.out, exist_ok=True)
    if a.terrain_only:
        print("terrain tiles", build_terrain(LazyDEM(a.dems), bbox, a.out))
        return
    dem, geo = load_mosaic(a.dems, bbox)
    print(f"DEM {dem.shape}, {float(dem.min()):.0f}..{float(dem.max()):.0f} m")
    print("terrain tiles", build_terrain(lambda lo, la: sample(dem, geo, lo, la), bbox, a.out))
    lines = make_contours(dem, geo)
    print("contour lines", len(lines))
    print("contour tiles", build_contours(lines, bbox, a.out))
    # Elevation probes for checking against known summits.
    for name, lon, lat, truth in (("백운대", 126.9776, 37.6588, 836), ("도봉산 자운봉", 127.0153, 37.6986, 740)):
        v = float(sample(dem, geo, np.array([lon]), np.array([lat]))[0])
        print(f"probe {name}: DEM {v:.0f} m vs published {truth} m")


if __name__ == "__main__":
    main()
