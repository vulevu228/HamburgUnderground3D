"""Turn Hamburg's official 3D building model (LoD2-DE) into vector map tiles for the 3D view.

Source: 3D-Gebäudemodell LoD2-DE Hamburg, Freie und Hansestadt Hamburg, Landesbetrieb
Geoinformation und Vermessung (LGV), licence dl-de/by-2-0 - via Transparenzportal Hamburg.

  python build_buildings.py data/lod2/LoD2-DE_HH_2026-04-28.zip   -> ../bld/{z}/{x}/{y}.pbf

Every building part becomes its measured ground outline with two heights: `h` = average roof
height above ground (flat roofs exact, pitched roofs at half the slope, which reads best as a
block) and `r` = ridge height. MapLibre extrudes them; roofs stay flat (option 1). The tiles
(z13 + z14, MapLibre over-zooms beyond) are served by livemap.py under /bld/.
"""
import collections, math, pathlib, sys, time, zipfile

import mapbox_vector_tile
import numpy as np
from lxml import etree
from pyproj import Transformer
from shapely import clip_by_rect, make_valid, transform
from shapely.geometry import Polygon
from shapely.ops import unary_union

HERE = pathlib.Path(__file__).parent
OUT = HERE.parent / "bld"
ZOOMS = (13, 14)
EXTENT = 4096
LIMIT = None                           # --test: only a few files
NS = {"bldg": "http://www.opengis.net/citygml/building/1.0", "gml": "http://www.opengis.net/gml"}
B = "{%s}" % NS["bldg"]
TO_MERC = Transformer.from_crs("EPSG:25832", "EPSG:3857", always_xy=True)
R = 6378137.0
HALF = math.pi * R                     # web mercator half width, metres


def poslists(el, surface):
    out = []
    for s in el.iterfind(f".//bldg:{surface}", NS):
        for pl in s.iterfind(".//gml:posList", NS):
            v = np.array(pl.text.split(), float)
            if len(v) >= 9:
                out.append(v.reshape(-1, 3))
    return out


def unit(el):
    """One building or building part -> (outline in UTM, avg roof height, ridge height) or None."""
    ground, roofs = poslists(el, "GroundSurface"), poslists(el, "RoofSurface")
    if not roofs:
        return None
    rz = np.concatenate([r[:, 2] for r in roofs])
    if ground:
        gz = float(min(g[:, 2].min() for g in ground))
        polys = [Polygon(g[:, :2]) for g in ground]
    else:                                  # no ground surface: the roof seen from above
        gz = float(rz.min()) - 3
        polys = [Polygon(r[:, :2]) for r in roofs]
    shape = make_valid(unary_union([make_valid(p) for p in polys if len(p.exterior.coords) >= 4]))
    if shape.is_empty or shape.area < 4:
        return None
    return shape.simplify(0.3), round(max(2.5, float(rz.mean()) - gz), 1), round(max(2.5, float(rz.max()) - gz), 1)


def buildings(zip_path):
    z = zipfile.ZipFile(zip_path)
    files = [n for n in z.namelist() if n.lower().endswith((".gml", ".xml"))][:LIMIT]
    print(f"{len(files)} CityGML files")
    for k, name in enumerate(files):
        with z.open(name) as f:
            for _, el in etree.iterparse(f, events=("end",), tag=B + "Building", huge_tree=True):
                parts = el.findall(f".//{B}BuildingPart")
                for u in (parts or [el]):
                    r = unit(u)
                    if r:
                        yield r
                el.clear()
                while el.getprevious() is not None:
                    del el.getparent()[0]
        if k % 20 == 0:
            print(f"  {k + 1}/{len(files)} files", flush=True)


def tile_bounds(zm, x, y):
    size = 2 * HALF / 2 ** zm
    return -HALF + x * size, HALF - (y + 1) * size, -HALF + (x + 1) * size, HALF - y * size


def main(zip_path):
    t0 = time.time()
    tiles = {zm: collections.defaultdict(list) for zm in ZOOMS}
    n = 0
    for shape, h, r in buildings(zip_path):
        merc = transform(shape, lambda c: np.column_stack(TO_MERC.transform(c[:, 0], c[:, 1])))
        minx, miny, maxx, maxy = merc.bounds
        for zm in ZOOMS:
            size = 2 * HALF / 2 ** zm
            for x in range(int((minx + HALF) // size), int((maxx + HALF) // size) + 1):
                for y in range(int((HALF - maxy) // size), int((HALF - miny) // size) + 1):
                    tiles[zm][(x, y)].append((merc, h, r))
        n += 1
        if n % 50000 == 0:
            print(f"  {n:,} building parts, {time.time() - t0:.0f} s", flush=True)
    print(f"{n:,} building parts read in {time.time() - t0:.0f} s")
    total = 0
    for zm in ZOOMS:
        for (x, y), items in tiles[zm].items():
            b = tile_bounds(zm, x, y)
            pad = (b[2] - b[0]) * 8 / EXTENT
            feats = []
            for merc, h, r in items:
                g = clip_by_rect(merc, b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
                if not g.is_empty and g.geom_type in ("Polygon", "MultiPolygon"):
                    feats.append({"geometry": g, "properties": {"h": h, "r": r}})
            if not feats:
                continue
            data = mapbox_vector_tile.encode([{"name": "b", "features": feats}],
                                             default_options={"quantize_bounds": b, "extents": EXTENT})
            p = OUT / str(zm) / str(x) / f"{y}.pbf"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            total += len(data)
        print(f"z{zm}: {len(tiles[zm])} tiles")
    print(f"-> {OUT}: {total / 1e6:.0f} MB, {time.time() - t0:.0f} s")


if __name__ == "__main__":
    if "--test" in sys.argv:
        LIMIT, OUT = 3, OUT.parent / "lod2_tiles_test"
    main(sys.argv[1])
