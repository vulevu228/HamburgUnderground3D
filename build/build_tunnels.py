"""Build the rail tunnel layer for the map's underground view from OpenStreetMap.

Source: OpenStreetMap contributors via the Overpass API, licence ODbL (attribution required).

  python build_tunnels.py            -> ../tunnels.json (query cached in ../data/osm_tunnels.json)
  python build_tunnels.py --refresh  -> query Overpass again

OSM has no tunnel depth in metres. What it has is the stacking order: `level` (preferred) or
`layer`, -1 = first level below ground ... -5 = deepest (Jungfernstieg). The map turns that
into an estimated depth of ~7 m per level and says so.
"""
import json, pathlib, sys

import numpy as np
import requests

HERE = pathlib.Path(__file__).parent
CACHE = HERE.parent / "data" / "osm_tunnels.json"
OUT = HERE.parent / "tunnels.json"
BBOX = (53.26, 9.50, 53.87, 10.56)      # south, west, north, east - same area as the map
QUERY = f"""[out:json][timeout:150];
way["railway"~"^(subway|rail|light_rail)$"]["tunnel"="yes"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
out tags geom;"""
SIMPLIFY_DEG = 0.00003


def rdp(pts, eps):
    """Ramer-Douglas-Peucker on an (n, 2) array, iterative (same as build_network.py)."""
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        seg = pts[b] - pts[a]
        n = np.hypot(*seg) or 1e-12
        rel = pts[a + 1:b] - pts[a]
        d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / n
        i = int(np.argmax(d))
        if d[i] > eps:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return pts[keep]


def level(tags):
    for k in ("level", "layer"):
        try:
            v = int(float(tags[k].split(";")[0]))
            if v < 0:
                return max(v, -5)
        except (KeyError, ValueError):
            pass
    return -1


def main(refresh):
    if refresh or not CACHE.exists():
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": QUERY}, timeout=200,
                          headers={"User-Agent": "HamburgUnderground3D (non-commercial)"})
        r.raise_for_status()
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_bytes(r.content)
    ways = json.loads(CACHE.read_text("utf-8"))["elements"]
    out = []
    for w in ways:
        pts = np.array([[g["lon"], g["lat"]] for g in w.get("geometry", [])])
        if len(pts) < 2 or w["tags"].get("tunnel") != "yes":     # not station halls (building_passage)
            continue
        pts = rdp(pts, SIMPLIFY_DEG)
        out.append({"k": w["tags"]["railway"], "lv": level(w["tags"]),
                    "c": [round(float(v), 5) for p in pts for v in p]})
    OUT.write_text(json.dumps({"source": "OpenStreetMap contributors (Overpass API), ODbL",
                               "ways": out}, separators=(",", ":")), "utf-8")
    levels = {}
    for w in out:
        levels[w["lv"]] = levels.get(w["lv"], 0) + 1
    print(f"-> {OUT}: {len(out)} tunnel ways, levels {dict(sorted(levels.items()))}, {OUT.stat().st_size / 1e3:.0f} KB")


if __name__ == "__main__":
    main("--refresh" in sys.argv)
