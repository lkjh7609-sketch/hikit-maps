"""Builds the trail network tiles the apps use for guidance on every mountain (not only 100대명산).

From an OpenStreetMap extract (.osm.pbf) it takes:
  - trails: highway=path/footway/track/steps/bridleway (no sidewalks or crossings)
  - minor roads (unclassified/service) for on/off-trail matching only, so walking a forest road
    is not "off trail"
  - junctions: nodes where three or more trail/road segments meet, at least one of them a trail
  - POIs: guideposts (with destinations), named peaks, springs/drinking water, shelters

Output: <out>/12/<x>/<y>.json, gzip-compressed JSON per z12 tile (upload with
Content-Encoding: gzip). Data © OpenStreetMap contributors, ODbL.

  {"v":1,
   "ways":[[kind, [lat_e6, lon_e6, lat_e6, lon_e6, ...]], ...],     kind 0 = trail, 1 = road
   "junctions":[[lat_e6, lon_e6, degree], ...],
   "pois":[[type, name, lat_e6, lon_e6], ...]}                       type SIGN/PEAK/SPRING/SHELTER

usage: python build_osm_trails.py south-korea-latest.osm.pbf out [--bbox w,s,e,n]
"""
import argparse, gzip, json, math, os, sys
from collections import defaultdict
import osmium

TRAILS = {"path", "footway", "track", "steps", "bridleway"}
ROADS = {"unclassified", "service"}
SKIP_FOOTWAY = {"sidewalk", "crossing", "traffic_island"}
SKIP_SERVICE = {"parking_aisle", "driveway", "drive-through"}
Z = 12


def tile(lat, lon, z=Z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


class Collect(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.bbox = bbox
        self.ways = []        # (kind, [node ids], [(lat, lon)])
        self.pois = []        # (type, name, lat, lon)

    def inside(self, lat, lon):
        w, s, e, n = self.bbox
        return s <= lat <= n and w <= lon <= e

    def way(self, w):
        hw = w.tags.get("highway")
        if hw in TRAILS:
            if hw == "footway" and w.tags.get("footway") in SKIP_FOOTWAY:
                return
            if w.tags.get("area") == "yes" or w.tags.get("access") in ("private", "no"):
                return
            kind = 0
        elif hw in ROADS:
            if w.tags.get("service") in SKIP_SERVICE:
                return
            kind = 1
        else:
            return
        try:
            ids = [n.ref for n in w.nodes]
            pts = [(n.location.lat, n.location.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(pts) < 2 or not any(self.inside(*p) for p in pts):
            return
        self.ways.append((kind, ids, pts))

    def node(self, n):
        t = n.tags
        if not t:
            return
        lat, lon = n.location.lat, n.location.lon
        if not self.inside(lat, lon):
            return
        name = t.get("name:ko") or t.get("name")
        if t.get("tourism") == "information" and t.get("information") == "guidepost":
            dests = t.get("destination") or t.get("destination:ko")
            label = " / ".join(d.strip() for d in dests.split(";")) if dests else (name or "")
            self.pois.append(("SIGN", label, lat, lon))
        elif t.get("natural") == "peak" and name:
            self.pois.append(("PEAK", name, lat, lon))
        elif t.get("natural") == "spring" or t.get("amenity") == "drinking_water":
            self.pois.append(("SPRING", name or "", lat, lon))
        elif t.get("amenity") == "shelter":
            self.pois.append(("SHELTER", name or "", lat, lon))


def simplify(pts, keep, tol=3.0):
    """Douglas-Peucker in meters, always keeping indices in `keep`."""
    if len(pts) <= 2:
        return list(range(len(pts)))
    lat0 = math.radians(pts[0][0])
    kx, ky = 111320 * math.cos(lat0), 110540
    xy = [(p[1] * kx, p[0] * ky) for p in pts]
    out = {0, len(pts) - 1} | set(keep)
    anchors = sorted(out)
    stack = list(zip(anchors, anchors[1:]))
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / L
            if d > best:
                best, bi = d, i
        if best > tol:
            out.add(bi)
            stack += [(a, bi), (bi, b)]
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--bbox", default="124.5,33.0,131.0,38.7")
    args = ap.parse_args()
    bbox = tuple(map(float, args.bbox.split(",")))

    h = Collect(bbox)
    h.apply_file(args.pbf, locations=True, idx="flex_mem")
    print(f"ways {len(h.ways)} (trails {sum(1 for w in h.ways if w[0] == 0)}), pois {len(h.pois)}", file=sys.stderr)

    # Node degree over all kept segments; junctions must touch a trail.
    degree = defaultdict(int)
    on_trail = set()
    loc = {}
    for kind, ids, pts in h.ways:
        for i, nid in enumerate(ids):
            degree[nid] += (i > 0) + (i < len(ids) - 1)
            loc[nid] = pts[i]
            if kind == 0:
                on_trail.add(nid)
    junction_ids = {n for n, d in degree.items() if d >= 3 and n in on_trail}
    print(f"junctions {len(junction_ids)}", file=sys.stderr)

    tiles = defaultdict(lambda: {"v": 1, "ways": [], "junctions": [], "pois": []})
    e6 = lambda v: int(round(v * 1e6))
    for kind, ids, pts in h.ways:
        keep = [i for i, nid in enumerate(ids) if nid in junction_ids]
        idx = simplify(pts, keep)
        coords = []
        for i in idx:
            coords += [e6(pts[i][0]), e6(pts[i][1])]
        for t in {tile(*pts[i]) for i in idx}:
            tiles[t]["ways"].append([kind, coords])
    for nid in junction_ids:
        lat, lon = loc[nid]
        tiles[tile(lat, lon)]["junctions"].append([e6(lat), e6(lon), degree[nid]])
    for typ, name, lat, lon in h.pois:
        tiles[tile(lat, lon)]["pois"].append([typ, name, e6(lat), e6(lon)])

    total = 0
    for (x, y), data in tiles.items():
        d = os.path.join(args.out, str(Z), str(x))
        os.makedirs(d, exist_ok=True)
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        gz = gzip.compress(raw, 9)
        total += len(gz)
        with open(os.path.join(d, f"{y}.json"), "wb") as f:
            f.write(gz)
    print(f"tiles {len(tiles)}, {total / 1e6:.1f} MB gzip", file=sys.stderr)


if __name__ == "__main__":
    main()
