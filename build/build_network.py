"""Build the static rail network for the map from HVV's open GTFS timetable.

Source: hvv Fahrplandaten (GTFS), Transparenzportal Hamburg, licence dl-de/by-2-0
(open data - may be published with attribution, unlike the GTI live data).

  python build_network.py PATH/TO/gtfs.zip     -> ../network.json

Keeps U-Bahn, S-Bahn, regional trains / AKN, ferries and the rail replacement buses ("S1-SEV").
For every line and destination (trip_headsign) the most frequent shape is one "variant", with
its track geometry (simplified, clipped to the map area) and its stop sequence with the
scheduled minutes from the first stop. Re-run when HVV publishes a new timetable.
"""
import datetime as dt, io, json, pathlib, re, sys, zipfile

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).parent
OUT = HERE.parent / "network.json"
# map area (index.html MAP_BOUNDS) + margin, so lines run off the edge instead of stopping at it
CLIP = (9.40, 53.16, 10.66, 53.97)
SIMPLIFY_DEG = 0.00004          # ~3-4 m: invisible at street zoom, ~85 % fewer points
TYPES = {"402": "U_BAHN", "109": "S_BAHN", "2": "R_BAHN", "1200": "SCHIFF"}


def read(z, name, **kw):
    return pd.read_csv(io.BytesIO(z.read(name)), encoding="utf-8-sig", dtype=str, keep_default_na=False, **kw)


def rdp(pts, eps):
    """Ramer-Douglas-Peucker on an (n, 2) array, iterative."""
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
        d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / n   # distance to the chord
        i = int(np.argmax(d))
        if d[i] > eps:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return pts[keep]


def clip_parts(pts):
    """Split a line into the runs that lie inside CLIP (keeping one point beyond each edge)."""
    x0, y0, x1, y1 = CLIP
    inside = (pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1)
    parts, cur = [], []
    for i, p in enumerate(pts):
        if inside[i] or (i + 1 < len(pts) and inside[i + 1]) or (i > 0 and inside[i - 1]):
            cur.append(p)
        elif cur:
            parts.append(cur)
            cur = []
    if cur:
        parts.append(cur)
    return [[[round(float(x), 5), round(float(y), 5)] for x, y in part] for part in parts if len(part) > 1]


def minutes(t):
    h, m, s = (int(v) for v in t.split(":"))
    return h * 60 + m + s / 60


def main(zip_path):
    z = zipfile.ZipFile(zip_path)
    routes = read(z, "routes.txt")
    sev = (routes.route_type == "3") & routes.route_short_name.str.endswith("-SEV")
    routes = routes[routes.route_type.isin(TYPES) | sev].copy()
    routes["mode"] = np.where(sev[routes.index], "SEV", routes.route_type.map(TYPES))
    routes.loc[routes.route_short_name.str.match(r"^A\d") & (routes["mode"] == "R_BAHN"), "mode"] = "A_BAHN"
    print(f"{len(routes)} routes:", ", ".join(sorted(routes.route_short_name.unique())))
    # official line colours (route_color; AKN carries white with its orange as text colour)
    colors = {}
    for r in routes.itertuples():
        c = r.route_color.strip().upper()
        if c == "FFFFFF":
            c = r.route_text_color.strip().upper()
        if re.fullmatch(r"[0-9A-F]{6}", c) and c not in ("FFFFFF", "000000"):
            colors.setdefault(r.route_short_name, "#" + c)

    trips = read(z, "trips.txt", usecols=["route_id", "trip_id", "trip_headsign", "direction_id", "shape_id"])
    trips = trips.merge(routes[["route_id", "route_short_name", "mode"]], on="route_id")
    # one variant per line + destination: its most frequent shape, represented by one trip
    counts = trips.groupby(["route_short_name", "mode", "trip_headsign", "direction_id", "shape_id"]).agg(
        trips=("trip_id", "size"), trip_id=("trip_id", "first")).reset_index()
    variants = counts.sort_values("trips", ascending=False).drop_duplicates(["route_short_name", "trip_headsign"])
    print(f"{len(variants)} variants from {len(trips):,} trips")

    st = read(z, "stop_times.txt", usecols=["trip_id", "stop_id", "stop_sequence", "arrival_time"])
    st = st[st.trip_id.isin(set(variants.trip_id))].copy()
    st["seq"] = st.stop_sequence.astype(int)
    st = st.sort_values(["trip_id", "seq"])

    stops = read(z, "stops.txt", usecols=["stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station"])
    sinfo = stops.set_index("stop_id")
    station_of = {r.stop_id: (r.parent_station if r.parent_station in sinfo.index else r.stop_id) for r in stops.itertuples()}
    stations, station_idx = [], {}

    def station(stop_id):
        sid = station_of.get(stop_id, stop_id)
        if sid not in station_idx:
            r = sinfo.loc[sid]
            station_idx[sid] = len(stations)
            # GTFS numbers some duplicate platforms "Barmbek(2)"; the rider-facing name has no suffix
            stations.append({"name": re.sub(r"\s*\(\d+\)$", "", r.stop_name), "lon": round(float(r.stop_lon), 5), "lat": round(float(r.stop_lat), 5), "lines": set()})
        return station_idx[sid]

    need = set(variants.shape_id)
    shapes = read(z, "shapes.txt")
    shapes = shapes[shapes.shape_id.isin(need)].copy()
    shapes["seq"] = shapes.shape_pt_sequence.astype(int)
    geom = {}
    for sid, g in shapes.sort_values(["shape_id", "seq"]).groupby("shape_id"):
        pts = g[["shape_pt_lon", "shape_pt_lat"]].astype(float).to_numpy()
        geom[sid] = clip_parts(rdp(pts, SIMPLIFY_DEG))

    lines = {}
    by_trip = dict(tuple(st.groupby("trip_id")))
    for v in variants.itertuples():
        seq = by_trip.get(v.trip_id)
        if seq is None or not geom.get(v.shape_id):
            continue
        t0 = minutes(seq.arrival_time.iloc[0])
        stop_list = []
        for r in seq.itertuples():
            i = station(r.stop_id)
            if not stop_list or stop_list[-1][0] != i:
                stop_list.append([i, round(minutes(r.arrival_time) - t0)])
            stations[i]["lines"].add(v.route_short_name)
        line = lines.setdefault(v.route_short_name, {"name": v.route_short_name, "mode": v.mode, "variants": []})
        line["variants"].append({"headsign": v.trip_headsign, "dir": int(v.direction_id or 0), "trips": int(v.trips),
                                 "shape": geom[v.shape_id], "stops": stop_list})
    for s in stations:
        s["lines"] = sorted(s["lines"])
    order = {"U_BAHN": 0, "S_BAHN": 1, "A_BAHN": 2, "R_BAHN": 3, "SCHIFF": 4, "SEV": 5}
    out = {"source": "hvv Fahrplandaten (GTFS), Transparenzportal Hamburg, dl-de/by-2-0",
           "feed": pathlib.Path(zip_path).parent.name, "built": dt.date.today().isoformat(),
           "lines": sorted(lines.values(), key=lambda l: (order[l["mode"]], len(l["name"]), l["name"])),
           "stations": stations, "colors": colors}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), "utf-8")
    pts = sum(len(p) for l in out["lines"] for v in l["variants"] for p in v["shape"])
    print(f"-> {OUT}: {len(out['lines'])} lines, {sum(len(l['variants']) for l in out['lines'])} variants, "
          f"{len(stations)} stations, {pts:,} points, {OUT.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1])
