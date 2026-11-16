"""Which neighbourhoods (kvartovi) an outage's drawn shape touches, in the cities of
spatial/kvartovi.json, for the neighbourhood SEO pages (scripts/build_city_pages.py). The file
comes from OpenStreetMap (scripts/fetch_kvartovi.py).

A neighbourhood OSM draws as an area has what lies inside it. One OSM has only as a point has
what lies nearer to it than to any other such point, up to POINT_RADIUS_M - so a street through
Vračar is Čubura's where it passes Čubura and Neimar's where it passes Neimar, not both along its
whole length. A drawn area bigger than MAX_AREA_KM2 is a whole settlement standing in for streets
that didn't resolve, and names no neighbourhood.

Display only: nothing here decides who is alerted."""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.strtree import STRtree

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "spatial", "kvartovi.json")
POINT_RADIUS_M = 800
MAX_AREA_KM2 = 3
SAMPLE_M = 60
# Metres per degree, with longitude at Serbia's middle latitude: the cities lie between 43.3 and
# 45.3 degrees north, so one scale is within 2%.
_M_LAT = 111_320
_M_LON = 111_320 * math.cos(math.radians(44.3))


def _xy(lng: float, lat: float) -> tuple[float, float]:
    return lng * _M_LON, lat * _M_LAT


@lru_cache(maxsize=1)
def _index():
    with open(PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    areas, points, names = [], [], {}
    for k in data.get("kvartovi", []):
        names[k["id"]] = (k["name"], k["city"])
        for ring in k.get("poly") or []:
            areas.append((k["id"], Polygon([_xy(lng, lat) for lng, lat in ring])))
        if k.get("pt"):
            points.append((k["id"], Point(_xy(k["pt"][1], k["pt"][0]))))
    # Where any kvart is, in lon/lat, with the point radius around it: a shape outside this box
    # is skipped before any work (most outages are in other towns).
    bounds = None
    if areas or points:
        x0, y0, x1, y1 = shapely.total_bounds([g for _, g in areas + points])
        bounds = ((x0 - POINT_RADIUS_M) / _M_LON, (y0 - POINT_RADIUS_M) / _M_LAT,
                  (x1 + POINT_RADIUS_M) / _M_LON, (y1 + POINT_RADIUS_M) / _M_LAT)
    return (areas, STRtree([g for _, g in areas]), points, STRtree([g for _, g in points]),
            names, bounds)


def names() -> dict[str, tuple[str, str]]:
    """id -> (name, city)."""
    return _index()[4]


_SCALE = np.array([_M_LON, _M_LAT])
_POINT, _LINE, _RING, _POLYGON = 0, 1, 2, 3


def _parts(g) -> np.ndarray:
    """Every single point, line and polygon in `g`, collections and multi-parts opened."""
    parts = shapely.get_parts(np.asarray([g], dtype=object))
    while len(parts) and (shapely.get_type_id(parts) >= 4).any():
        parts = shapely.get_parts(parts)
    return parts


def _tag(g, lonlat: bool = False) -> list[str]:
    """`g` (any geometry) in metres, or in lon/lat with `lonlat` - projected here, only once it
    is known to be near one of the cities. Most outages are in other towns."""
    areas, area_tree, points, point_tree, _, bounds = _index()
    if g is None or g.is_empty or bounds is None:
        return []
    b = g.bounds if lonlat else (g.bounds[0] / _M_LON, g.bounds[1] / _M_LAT,
                                  g.bounds[2] / _M_LON, g.bounds[3] / _M_LAT)
    if b[2] < bounds[0] or b[0] > bounds[2] or b[3] < bounds[1] or b[1] > bounds[3]:
        return []
    if lonlat:
        g = shapely.transform(g, lambda xy: xy * _SCALE)
    parts = _parts(g)
    kinds = shapely.get_type_id(parts)
    found = set()
    # A broken piece (a one-point "line", a self-crossing ring) makes GEOS reject the whole batch -
    # stored shapes do have them (#2) - so it is left out, as a loop over the pieces would.
    polys = parts[kinds == _POLYGON]
    polys = polys[shapely.is_valid(polys) & (shapely.area(polys) <= MAX_AREA_KM2 * 1_000_000)]
    if len(polys):
        if areas:
            found.update(areas[i][0] for i in area_tree.query(polys, predicate="intersects")[1])
        if points:
            found.update(points[i][0] for i in point_tree.query(polys, predicate="contains")[1])
    # A line is probed every SAMPLE_M along it; an area at a point inside it.
    # A line of one repeated point has no length to walk: its point is probed as it is.
    lines = parts[(kinds == _LINE) | (kinds == _RING)]
    walk = shapely.length(lines) > 0
    coords = np.concatenate([shapely.get_coordinates(shapely.segmentize(lines[walk], SAMPLE_M)),
                             shapely.get_coordinates(lines[~walk])]) if len(lines) else np.empty((0, 2))
    probes = np.concatenate([shapely.points(coords), parts[kinds == _POINT],
                             shapely.point_on_surface(polys)])
    if not len(probes):
        return sorted(found)
    inside = area_tree.query(probes, predicate="within") if areas else np.empty((2, 0), dtype=int)
    found.update(areas[i][0] for i in inside[1])
    outside = np.setdiff1d(np.arange(len(probes)), inside[0])
    if points and len(outside):
        near = point_tree.query_nearest(probes[outside], max_distance=POINT_RADIUS_M)
        found.update(points[i][0] for i in near[1])
    return sorted(found)


def for_geojson(geojson) -> list[str]:
    """The neighbourhoods a stored outage shape (outages.geojson_geometry) touches."""
    try:
        text = geojson if isinstance(geojson, str) else json.dumps(geojson)
        return _tag(shapely.from_geojson(text), lonlat=True)
    except Exception:  # a shape GEOS can't read names no neighbourhood; it must not stop the feed
        try:
            data = json.loads(geojson) if isinstance(geojson, str) else geojson
            geoms = [shape(f["geometry"]) for f in (data or {}).get("features") or [] if f.get("geometry")]
            return _tag(shapely.geometrycollections(geoms), lonlat=True) if geoms else []
        except Exception:
            return []


def for_shapes(paths, areas) -> list[str]:
    """The same for the map's own shapes (build_web_data): paths [[lat, lng], ...], areas
    {"hull": [[lat, lng], ...]} or {"dot": [lat, lng]}."""
    try:
        geoms = [LineString([_xy(lng, lat) for lat, lng in p]) for p in paths or [] if len(p) >= 2]
        geoms += [Point(_xy(p[0][1], p[0][0])) for p in paths or [] if len(p) == 1]
        for a in areas or []:
            if a.get("hull") and len(a["hull"]) >= 3:
                geoms.append(Polygon([_xy(lng, lat) for lat, lng in a["hull"]]))
            elif a.get("dot"):
                geoms.append(Point(_xy(a["dot"][1], a["dot"][0])))
        return _tag(shapely.geometrycollections(geoms)) if geoms else []
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        return []
