"""The area inside a ring of streets: "део рејона оивичен улицама Пеке Павловић, Заплањском и
Браће Јерковић" (BVK, #6458, 31.10.2026) is the block those streets enclose, not the streets
themselves. Read as streets, all three were alerted end to end (5.8 km, people 2 km from the
block) while the houses inside, on streets the notice never names, heard nothing.

The shape: each named street from corner to corner, the streets taken in the order the notice
lists them (it goes round the block). Two streets that don't quite meet (Заплањска and Браће
Јерковић end 183 m apart in RGZ) are joined with a straight line - once per block; a ring with
two such gaps is a list out of order, not a block. Whatever fails a check gives no shape at all,
and the caller alerts nobody: a wrong block is worse than none.

Coordinates are (lon, lat) like the street segments; the geometry is done in local metres."""
import math
from functools import lru_cache

import h3
from shapely.geometry import LineString, MultiLineString, Point, Polygon, mapping
from shapely.ops import nearest_points, transform

from spatial.spatial_engine import get_local_street_segments

CORNER_M = 60         # two streets this close meet at a corner (digitising drift)
GAP_M = 250           # a corner this far apart is bridged with a straight line, once per block
EDGE_M = 5            # a house this close outside the edge counts as inside
BORDER_M = 60         # a house on a boundary street, across the road from the block
MAX_AREA_M2 = 1_000_000
MIN_AREA_M2 = 500
RESOLUTION = 9


class _Local:
    """Equirectangular metres around one point - exact enough across a block."""

    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = 111_320 * math.cos(math.radians(lat0))
        self.ky = 110_540

    def to_m(self, geom):
        return transform(lambda x, y, z=None: ((x - self.lon0) * self.kx, (y - self.lat0) * self.ky), geom)

    def to_deg(self, geom):
        return transform(lambda x, y, z=None: (x / self.kx + self.lon0, y / self.ky + self.lat0), geom)


def _street_lines(street: str, settlement: str | None, region: str | None):
    segs = get_local_street_segments(street, settlement, region)
    lines = [LineString(s) for s in segs or [] if isinstance(s, (list, tuple)) and len(s) >= 2]
    return MultiLineString(lines) if lines else None


def _between(street, a: Point, b: Point) -> list:
    """The street's own course from corner a to corner b: its points that lie between the two
    corners, in order along the way. RGZ cuts a street into a piece per crossing, and the pieces
    don't always touch, so following the line itself broke off at the first gap and the edge fell
    back to a straight line that cut across the next street (#6458). A point counts when it falls
    between the corners along a-b and not far off that line (a street that turns away and back is
    not this edge)."""
    ax, ay, bx, by = a.x, a.y, b.x, b.y
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1:
        return [(ax, ay), (bx, by)]
    off_max = max(150.0, 0.5 * math.sqrt(length2))
    kept = []
    for part in street.geoms:
        for x, y in part.coords:
            t = ((x - ax) * dx + (y - ay) * dy) / length2
            off = abs((x - ax) * dy - (y - ay) * dx) / math.sqrt(length2)
            if 0 < t < 1 and off <= off_max:
                kept.append((t, x, y))
    kept.sort()
    return [(ax, ay)] + [(x, y) for _, x, y in kept] + [(bx, by)]


@lru_cache(maxsize=256)
def block_polygon(streets: tuple, settlement: str | None, region: str | None):
    """(polygon in metres, its _Local) for the block inside `streets` (in the notice's order),
    or None when they don't close round a block."""
    if len(streets) < 3:
        return None
    raw = [_street_lines(s, settlement, region) for s in streets]
    if any(g is None for g in raw):
        return None
    c = raw[0].centroid
    local = _Local(c.x, c.y)
    lines = [local.to_m(g) for g in raw]
    n = len(lines)

    corners, bridged = [], 0
    for i in range(n):
        p, q = nearest_points(lines[i], lines[(i + 1) % n])   # p on street i, q on street i+1
        gap = p.distance(q)
        if gap > GAP_M:
            return None
        if gap > CORNER_M:
            bridged += 1
        corners.append((p, q))
    if bridged > 1:
        return None

    ring = []
    for i in range(n):
        start = corners[i - 1][1]            # where street i meets the one before it
        end = corners[i][0]                  # where it meets the one after it
        ring.extend(_between(lines[i], start, end))
    if len(ring) < 3:
        return None
    poly = Polygon(ring)
    if not poly.is_valid:
        fixed = poly.buffer(0)
        if fixed.geom_type == "MultiPolygon":
            biggest = max(fixed.geoms, key=lambda g: g.area)
            if biggest.area < 0.8 * fixed.area:
                return None
            fixed = biggest
        poly = fixed
    if poly.is_empty or poly.geom_type != "Polygon" or not MIN_AREA_M2 <= poly.area <= MAX_AREA_M2:
        return None
    return poly, local


def where(shape, lat: float, lon: float) -> str | None:
    """'inside' the block, 'border' (just outside it, across a boundary street) or None."""
    poly, local = shape
    pt = local.to_m(Point(lon, lat))
    d = poly.distance(pt)                    # 0 inside
    if d <= EDGE_M:
        return "inside"
    if d <= BORDER_M:
        return "border"
    return None


def block_cells(shape) -> set[str]:
    """H3 cells over the block and its border strip: who may be inside or across the road."""
    poly, local = shape
    area = local.to_deg(poly.buffer(BORDER_M))
    cells = set(h3.geo_to_cells(mapping(area), RESOLUTION))
    cells |= {h3.latlng_to_cell(y, x, RESOLUTION) for x, y in area.exterior.coords}
    return cells


def block_feature(shape, label: str) -> dict:
    """The block drawn on the map: its outline, filled."""
    poly, local = shape
    outline = local.to_deg(poly)
    return {"type": "Feature", "geometry": mapping(outline),
            "properties": {"kind": "area", "area_type": "block", "settlement": label}}


CORNER_NEAR_M = 150   # a house this close to a corner that stands in for a stretch


@lru_cache(maxsize=512)
def corner_point(street: str, cross: str, settlement: str | None, region: str | None):
    """(lat, lon) where `street` meets `cross`, or None when the two don't meet. For a stretch
    "од X до Y" that can't be cut: the corner it names is where the works are, not the whole
    street (#6449, "Крунска од Кичевске до Крунске")."""
    a, b = _street_lines(street, settlement, region), _street_lines(cross, settlement, region)
    if a is None or b is None:
        return None
    local = _Local(a.centroid.x, a.centroid.y)
    p, q = nearest_points(local.to_m(a), local.to_m(b))
    if p.distance(q) > CORNER_M:
        return None
    corner = local.to_deg(p)
    return corner.y, corner.x


def corner_cells(street: str, cross: str, settlement: str | None, region: str | None) -> set[str]:
    """The cells round corner_point, for the map and who is looked at."""
    corner = corner_point(street, cross, settlement, region)
    if not corner:
        return set()
    return set(h3.grid_disk(h3.latlng_to_cell(corner[0], corner[1], RESOLUTION), 1))


def distance_m(segments, lat: float, lon: float) -> float | None:
    """Metres from (lat, lon) to the nearest of `segments` ([[lon, lat], ...] lines)."""
    lines = [LineString(s) for s in segments if isinstance(s, (list, tuple)) and len(s) >= 2]
    if not lines:
        return None
    local = _Local(lon, lat)
    return local.to_m(MultiLineString(lines)).distance(Point(0, 0))


def point_distance_m(a: tuple, b: tuple) -> float:
    """Metres between two (lat, lon) points."""
    local = _Local(a[1], a[0])
    return local.to_m(Point(b[1], b[0])).distance(Point(0, 0))
