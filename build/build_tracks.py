"""Build the real track geometry of the U-Bahn, S-Bahn and AKN lines from OpenStreetMap route relations.

Source: OpenStreetMap contributors via the Overpass API, licence ODbL (attribution required).

  python build_tracks.py            -> ../tracks.json (query cached in ../data/osm_routes.json)
  python build_tracks.py --refresh  -> query Overpass again

Every line direction is a route relation (PTv2) listing the railway ways it runs on, in order. Each
physical way is stored once with its tunnel level, and each relation as the ordered list of its ways,
so the map can draw a track once for all lines using it and let trains ride exactly that track.

Tunnel level: `level` (preferred) or `layer`, -1 = first level below ground ... -5 deepest, ~7 m each.
`est` marks tunnels with only `layer` (or no level at all): their depth is a weaker guess.
"""
import json, math, pathlib, sys

import numpy as np
import requests

from build_tunnels import rdp

HERE = pathlib.Path(__file__).parent
CACHE = HERE.parent / "data" / "osm_routes.json"
OUT = HERE.parent / "tracks.json"
BBOX = (53.26, 9.50, 53.87, 10.56)
QUERY = f"""[out:json][timeout:240];
relation["type"="route"]["route"~"^(subway|light_rail|train)$"]["ref"~"^(U[1-4]|S[0-9]+|A[1-3])$"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]})->.r;
.r out geom;
way(r.r)["railway"]; out tags;"""
MODE = {"U": "U_BAHN", "S": "S_BAHN", "A": "A_BAHN"}
TRACK_ROLES = ("", "forward", "backward")
SIMPLIFY_DEG = 0.000006          # ~0.5 m: keeps the curves, drops collinear points


def level(tags):
    """(level, estimated): 0 = surface; tunnels -1 ... -5"""
    if tags.get("tunnel") not in ("yes", "building_passage") and tags.get("covered") != "yes":
        return 0, False
    for k, est in (("level", False), ("layer", True)):
        try:
            v = int(float(tags[k].split(";")[0]))
            if v < 0:
                return max(v, -5), est
        except (KeyError, ValueError):
            pass
    return -1, True


def km(a, b):
    return math.hypot((a[0] - b[0]) * 66.2, (a[1] - b[1]) * 111.2)


def main(refresh):
    if refresh or not CACHE.exists():
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": QUERY}, timeout=300,
                          headers={"User-Agent": "HamburgUnderground3D (non-commercial)"})
        r.raise_for_status()
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_bytes(r.content)
    els = json.loads(CACHE.read_text("utf-8"))["elements"]
    tags = {e["id"]: e.get("tags", {}) for e in els if e["type"] == "way"}
    ways, rels = {}, []
    for e in sorted((e for e in els if e["type"] == "relation"), key=lambda e: (e["tags"]["ref"], e["tags"].get("from", ""))):
        t = e["tags"]
        seq, prev_end = [], None
        for m in e["members"]:
            if m["type"] != "way" or m["role"] not in TRACK_ROLES or not m.get("geometry") or m["ref"] not in tags:
                continue
            g = [(p["lon"], p["lat"]) for p in m["geometry"]]
            if m["ref"] not in ways:
                lv, est = level(tags[m["ref"]])
                pts = rdp(np.array(g), SIMPLIFY_DEG)
                ways[m["ref"]] = {"c": [round(float(v), 6) for p in pts for v in p], "lv": lv, "est": est, "lines": []}
            if t["ref"] not in ways[m["ref"]]["lines"]:
                ways[m["ref"]]["lines"].append(t["ref"])
            # orientation: the way's end that touches the previous way comes first
            if prev_end is None:
                nxt = next((x for x in e["members"][e["members"].index(m) + 1:] if x["type"] == "way" and x["role"] in TRACK_ROLES and x.get("geometry")), None)
                rev = bool(nxt) and min(km(g[0], (q["lon"], q["lat"])) for q in (nxt["geometry"][0], nxt["geometry"][-1])) < \
                                    min(km(g[-1], (q["lon"], q["lat"])) for q in (nxt["geometry"][0], nxt["geometry"][-1]))
            else:
                rev = km(g[-1], prev_end) < km(g[0], prev_end)
                if min(km(g[-1], prev_end), km(g[0], prev_end)) > 0.02:
                    seq.append(None)                   # a gap: the next way starts a new part
            seq.append([m["ref"], int(rev)])
            prev_end = g[0] if rev else g[-1]
        stops = [[round(m["lon"], 6), round(m["lat"], 6)] for m in e["members"]
                 if m["type"] == "node" and m["role"].startswith("stop") and "lon" in m]
        rels.append({"line": t["ref"], "mode": MODE[t["ref"][0]], "from": t.get("from", ""), "to": t.get("to", ""),
                     "seq": seq, "stops": stops})
    OUT.write_text(json.dumps({"source": "OpenStreetMap contributors (Overpass API), ODbL",
                               "ways": {str(k): v for k, v in ways.items()}, "rels": rels}, separators=(",", ":"), ensure_ascii=False), "utf-8")
    tun = [w for w in ways.values() if w["lv"] < 0]
    print(f"-> {OUT}: {len(rels)} routes, {len(ways)} ways ({len(tun)} in tunnels, {sum(w['est'] for w in tun)} estimated), "
          f"{OUT.stat().st_size / 1e3:.0f} KB")


if __name__ == "__main__":
    main("--refresh" in sys.argv)
