"""Fetch the neighbourhoods (kvartovi) of Serbia's bigger cities from OpenStreetMap into
spatial/kvartovi.json, for the neighbourhood SEO pages (scripts/build_city_pages.py) and the feed
that tags each outage with the neighbourhoods it touches (core/kvartovi.py).

Run by hand when the list should be refreshed; the output is committed, so nothing depends on
OpenStreetMap at run time. Map data © OpenStreetMap contributors, ODbL.

    python scripts/fetch_kvartovi.py                      # every city below
    python scripts/fetch_kvartovi.py Kragujevac Subotica  # only these; the rest stay as they are

A neighbourhood OSM draws as an area keeps its shape ("poly"); one it has only as a point keeps
the point ("pt") and covers what lies near it (core/kvartovi.POINT_RADIUS_M). A name that already
has its own page - a gradska opština like Zemun or Voždovac - is left out, as are OSM's
"Beograd (Voždovac)"-style town parts, which are those same opštine again.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date

import requests
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_city_pages import GRAD_DIR, slugify  # noqa: E402
from scripts.build_web_data import title_sr  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "spatial", "kvartovi.json")
MIRRORS = ("https://overpass-api.de/api/interpreter",
           "https://overpass.kumi.systems/api/interpreter",
           "https://overpass.private.coffee/api/interpreter")
# south, west, north, east: the built-up city, not the whole grad with its villages. Beyond the
# first three, each box is the extent of the town's own RGZ streets (98% of their points), set
# by hand where RGZ has stray points (Leskovac) or the box came out tighter than the town.
CITIES = {
    "Beograd": (44.68, 20.20, 44.93, 20.65),
    "Novi Sad": (45.20, 19.72, 45.33, 19.93),
    "Niš": (43.28, 21.82, 43.36, 21.97),
    "Kragujevac": (43.977, 20.842, 44.055, 20.975),
    "Subotica": (46.063, 19.607, 46.144, 19.734),
    "Zrenjanin": (45.334, 20.357, 45.407, 20.443),
    "Pančevo": (44.837, 20.625, 44.929, 20.741),
    "Čačak": (43.860, 20.314, 43.917, 20.403),
    "Kraljevo": (43.691, 20.643, 43.740, 20.735),
    "Kruševac": (43.550, 21.280, 43.610, 21.370),
    "Leskovac": (42.970, 21.900, 43.030, 21.990),
    "Smederevo": (44.629, 20.866, 44.669, 20.963),
    "Valjevo": (44.252, 19.847, 44.292, 19.939),
    "Šabac": (44.735, 19.639, 44.776, 19.715),
    "Sombor": (45.705, 19.051, 45.848, 19.213),
    "Vranje": (42.529, 21.880, 42.566, 21.922),
    "Novi Pazar": (43.124, 20.487, 43.159, 20.536),
    "Užice": (43.834, 19.812, 43.872, 19.876),
    "Sremska Mitrovica": (44.962, 19.579, 45.013, 19.669),
    "Kikinda": (45.761, 20.387, 45.866, 20.539),
    "Zaječar": (43.874, 22.227, 43.942, 22.301),
    "Pirot": (43.135, 22.569, 43.190, 22.604),
    "Jagodina": (43.951, 21.238, 43.995, 21.281),
    "Vršac": (45.086, 21.275, 45.145, 21.360),
    "Ruma": (44.992, 19.795, 45.024, 19.845),
}
QUERY = """[out:json][timeout:180];
nwr["place"~"^(suburb|quarter|neighbourhood)$"]({bbox});
out geom;"""
SIMPLIFY_DEG = 0.00012     # ~10 m
TOWN_PART = re.compile(r"^(" + "|".join(map(re.escape, CITIES)) + r")\s*\(")


def overpass(bbox) -> list[dict]:
    q = QUERY.replace("{bbox}", ",".join(str(x) for x in bbox))
    last = None
    for url in MIRRORS:
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": q}, timeout=240,
                                  headers={"User-Agent": "najavaradova.rs kvartovi fetch"})
                if r.status_code == 200:
                    return r.json()["elements"]
                last = f"HTTP {r.status_code}"
            except (requests.RequestException, ValueError) as e:
                last = e
            time.sleep(15)
    raise RuntimeError(f"Overpass unavailable: {last}")


_ROMAN = re.compile(r"\b(Ii|Iii|Iv|Vi|Vii|Viii|Ix)\b")


def nice_name(name: str) -> str:
    """title_sr, plus what it gets wrong in a place name: "Mirijevo Ii" is Mirijevo II,
    "Borča-greda" Borča-Greda, "Naselje „crkva”" Naselje „Crkva”."""
    name = _ROMAN.sub(lambda m: m.group(0).upper(), title_sr(name).strip())
    return re.sub(r"([-„])(\w)", lambda m: m.group(1) + m.group(2).upper(), name)


def name_of(tags: dict) -> str:
    raw = tags.get("name:sr-Latn") or tags.get("name") or ""
    return nice_name(raw) if raw else ""


def shape_of(el: dict):
    """The element's area, or None when OSM has it only as a point / an open line."""
    if el["type"] == "way":
        pts = [(g["lon"], g["lat"]) for g in el.get("geometry") or []]
        return Polygon(pts) if len(pts) >= 4 and pts[0] == pts[-1] else None
    if el["type"] == "relation":
        lines = [LineString([(g["lon"], g["lat"]) for g in m["geometry"]])
                 for m in el.get("members") or [] if m.get("role") in ("outer", "") and m.get("geometry")
                 and len(m["geometry"]) >= 2]
        polys = list(polygonize(unary_union(lines))) if lines else []
        return unary_union(polys) if polys else None
    return None


def rings(geom) -> list[list[list[float]]]:
    """[[lng, lat], ...] outer rings, simplified and rounded - holes are ignored."""
    parts = getattr(geom, "geoms", [geom])
    out = []
    for p in parts:
        s = p.simplify(SIMPLIFY_DEG, preserve_topology=True)
        if s.is_empty or s.area <= 0:
            continue
        out.append([[round(x, 5), round(y, 5)] for x, y in s.exterior.coords])
    return out


def _centre(k: dict) -> tuple[float, float]:
    """(lng, lat) of a kvart entry."""
    if "pt" in k:
        return k["pt"][1], k["pt"][0]
    ring = k["poly"][0]
    return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)


def place_by_rgz(entries: list[dict]) -> list[dict]:
    """Each kvart under the city whose opštine hold the RGZ street nearest to it. The boxes of
    neighbouring cities overlap: Beograd's reaches across the Tamiš, and Pančevo's Kotež 2,
    Sodara and Tri Tiganja had been published as Beograd's (10.10.2026). A kvart nearest to an
    opština of no city here is dropped, as is a second copy of one id."""
    from spatial.spatial_engine import RGZ_DB_PATH, region_opstine
    import sqlite3
    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True)
    points, owner = [], []
    for geometry, opstina in conn.execute("SELECT geometry_json, opstina_lat FROM rgz_streets"):
        try:
            g = json.loads(geometry)
        except ValueError:
            continue
        lines = {"LineString": [g.get("coordinates")], "MultiLineString": g.get("coordinates")}.get(g.get("type")) or []
        for line in lines:
            for x, y in (line[0], line[len(line) // 2], line[-1]) if line else ():
                points.append(Point(x, y))
                owner.append(opstina)
    conn.close()
    if not points:
        raise RuntimeError(f"no RGZ streets in {RGZ_DB_PATH}: can't tell which city a kvart is in")
    tree = STRtree(points)
    city_of = {op: city for city in CITIES for op in (region_opstine(city) or ())}
    out, seen = [], set()
    for k in entries:
        lng, lat = _centre(k)
        city = city_of.get(owner[tree.nearest(Point(lng, lat))])
        if city is None:
            print(f"  dropped {k['id']}: nearest street is in no city here")
            continue
        if city != k["city"]:
            new_id = f"{slugify(city)}/{k['id'].split('/', 1)[1]}"
            print(f"  {k['id']} -> {new_id}")
            k = {**k, "id": new_id, "city": city}
        if k["id"] not in seen:
            seen.add(k["id"])
            out.append(k)
    return out


def main(only: list[str]) -> int:
    unknown = [c for c in only if c not in CITIES]
    if unknown:
        print(f"unknown cities: {unknown}")
        return 1
    own_pages = {d for d in os.listdir(GRAD_DIR) if os.path.isdir(os.path.join(GRAD_DIR, d))}
    result = []
    if only and os.path.exists(OUT):
        # Every other city stays exactly as it was: a re-fetch could rename or drop a kvart whose
        # page is already online.
        with open(OUT, encoding="utf-8") as fh:
            result = [k for k in json.load(fh)["kvartovi"] if k["city"] not in only]
    for city, bbox in CITIES.items():
        if only and city not in only:
            continue
        elements = overpass(bbox)
        areas, points = {}, {}
        for el in elements:
            name = name_of(el.get("tags") or {})
            slug = slugify(name) if name else ""
            if not slug or TOWN_PART.match(name) or slug in own_pages or slug == slugify(city):
                continue
            if el["type"] == "node":
                points.setdefault(slug, (name, (el["lon"], el["lat"])))
                continue
            geom = shape_of(el)
            if geom is not None and not geom.is_empty:
                prev = areas.get(slug)
                areas[slug] = (name, geom if prev is None else unary_union([prev[1], geom]))
        for slug, (name, geom) in sorted(areas.items()):
            poly = rings(geom)
            if poly:
                result.append({"id": f"{slugify(city)}/{slug}", "name": name, "city": city, "poly": poly})
        for slug, (name, (lon, lat)) in sorted(points.items()):
            if slug in areas:
                continue  # the same place, drawn as an area too
            result.append({"id": f"{slugify(city)}/{slug}", "name": name, "city": city,
                           "pt": [round(lat, 5), round(lon, 5)]})
        n_poly = sum(1 for k in result if k["city"] == city and "poly" in k)
        n_pt = sum(1 for k in result if k["city"] == city and "pt" in k)
        print(f"{city}: {n_poly} areas, {n_pt} points")
        time.sleep(10)
    result = place_by_rgz(result)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"source": "© OpenStreetMap contributors, ODbL", "fetched": date.today().isoformat(),
                   "kvartovi": result}, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(result)} neighbourhoods -> {OUT} ({os.path.getsize(OUT) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
