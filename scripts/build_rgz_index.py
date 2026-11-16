"""
Builds data/rgz.db from the RGZ (Republic Geodetic Authority) cadastral extract sitting in
data/rgz_raw/ — rgz_streets_<opstina>[_<part>].csv and rgz_housenumbers_<opstina>[_<part>].csv.

Unlike data/serbia_streets.db (OSM road geometry, named ways only, no addresses), RGZ rows are
already scoped to the correct naselje/opština with a stable street ID (ulica_maticni_broj) and
give real house-number address points — see PLAN.md / the RGZ integration plan for why this
matters for spatial_engine.py's street and settlement resolution.

Geometry in the source CSVs is WKT (POINT / LINESTRING / MULTILINESTRING) in EPSG:32634 (WGS84 /
UTM zone 34N) — confirmed by reprojecting a known Futog sample point (392297.84, 5019453.39),
which lands at 45.320N, 19.626E, matching Futog's real location. Everything is reprojected to
EPSG:4326 (lon/lat) here so it's directly usable by spatial_engine.py (h3, shapely, GeoJSON all
expect WGS84 lon/lat, same as data/serbia_streets.db).

Usage:
    python scripts/build_rgz_index.py [source_dir]   # default: data/rgz_raw/
"""
import csv
import glob
import json
import os
import sqlite3
import sys

import h3
from pyproj import Transformer
from shapely import wkt
from shapely.ops import transform as shapely_transform

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RAW_DIR = os.path.join(BASE_DIR, "data", "rgz_raw")
RGZ_DB_PATH = os.path.join(BASE_DIR, "data", "rgz.db")

_to_wgs84 = Transformer.from_crs("EPSG:32634", "EPSG:4326", always_xy=True).transform
# A few line parts in the extract are in the old Serbian Gauss-Krüger grid instead (MGI 1901 /
# Balkans zone 6 or 7: the zone number leads the easting, "7498169.92"). Read as UTM they landed in
# India - 7 points of Užičke Republike in Kragujevac and two whole streets in Pljačkovica (Vranje).
# They stretched those three settlements to the largest envelope there is, and Kragujevac's leak
# gate had to block them on #4362 (12.10.2026).
_GAUSS_KRUGER = {
    zone: Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True).transform
    for zone, epsg in ((6, 3908), (7, 3909))
}
# Serbia with a margin. Anything reprojected outside it is wrong data, whatever the reason.
SERBIA_BOUNDS = (18.0, 41.8, 23.1, 46.3)  # lon_min, lat_min, lon_max, lat_max


def _part_to_wgs84(x, y):
    """One part's coordinates: UTM 34N, or Gauss-Krüger when the easting carries a zone digit.
    Shapely calls this once per part of a multi-geometry, so each part is read on its own."""
    first = x[0] if hasattr(x, "__len__") else x
    if first >= 1_000_000:
        return _GAUSS_KRUGER[int(first // 1_000_000)](x, y)
    return _to_wgs84(x, y)


def _in_serbia(lon: float, lat: float) -> bool:
    lon_min, lat_min, lon_max, lat_max = SERBIA_BOUNDS
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


def _keep_in_serbia(geom: dict) -> dict | None:
    """The geometry without any point or line part that lies outside Serbia; None if nothing is
    left."""
    gtype, coords = geom.get("type"), geom.get("coordinates")
    if gtype == "Point":
        return geom if _in_serbia(*coords[:2]) else None
    if gtype == "LineString":
        return geom if all(_in_serbia(*c[:2]) for c in coords) else None
    if gtype == "MultiLineString":
        parts = [line for line in coords if all(_in_serbia(*c[:2]) for c in line)]
        return {"type": gtype, "coordinates": parts} if parts else None
    return geom


def reproject_wkt(wkt_str: str) -> dict | None:
    """WKT (EPSG:32634, or Gauss-Krüger zone 6/7 for a stray part) -> GeoJSON geometry dict
    (EPSG:4326), or None if unparseable, empty, or wholly outside Serbia."""
    if not wkt_str or not wkt_str.strip():
        return None
    try:
        geom = wkt.loads(wkt_str)
        if geom.is_empty:
            return None
        geom_wgs84 = shapely_transform(_part_to_wgs84, geom)
        # __geo_interface__ coordinates come back as tuples; round-trip through json to get
        # plain lists, matching the geometry_json shape query_street_database() already expects.
        return _keep_in_serbia(json.loads(json.dumps(geom_wgs84.__geo_interface__)))
    except Exception:
        return None


def iter_csv_rows(path: str):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        yield from csv.DictReader(f, delimiter=delimiter)


def _clean(value: str | None) -> str:
    return (value or "").strip().upper()


def build_streets(cursor, raw_dir: str, settlement_acc: dict, opstina_acc: dict) -> int:
    cursor.execute("DROP TABLE IF EXISTS rgz_streets")
    cursor.execute('''
        CREATE TABLE rgz_streets (
            ulica_maticni_broj TEXT PRIMARY KEY,
            name TEXT, name_lat TEXT,
            naselje TEXT, naselje_lat TEXT,
            opstina TEXT, opstina_lat TEXT,
            geometry_json TEXT
        )
    ''')

    count = 0
    files = sorted(glob.glob(os.path.join(raw_dir, "rgz_streets_*.csv")))
    for path in files:
        print(f"  📄 {os.path.basename(path)}")
        records = []
        for row in iter_csv_rows(path):
            street_id = (row.get("ulica_maticni_broj") or "").strip()
            if not street_id:
                continue

            geom = reproject_wkt(row.get("geometry"))
            geom_json = json.dumps(geom) if geom else None

            name = _clean(row.get("ulica_ime"))
            name_lat = _clean(row.get("ulica_ime_lat"))
            naselje = _clean(row.get("naselje_ime"))
            naselje_lat = _clean(row.get("naselje_ime_lat"))
            opstina = _clean(row.get("opstina_ime"))
            opstina_lat = _clean(row.get("opstina_ime_lat"))

            records.append((street_id, name, name_lat, naselje, naselje_lat, opstina, opstina_lat, geom_json))

            # Accumulate settlement centroid/extent data in the same pass (avoids re-reading
            # every street's geometry a second time just to derive rgz_settlements below).
            # acc: [sum_lat, sum_lon, count, naselje, opstina, min_lat, max_lat, min_lon, max_lon]
            if geom and naselje_lat:
                key = (naselje_lat, opstina_lat)
                acc = settlement_acc.setdefault(
                    key, [0.0, 0.0, 0, naselje, opstina, 90.0, -90.0, 180.0, -180.0]
                )
                # Separate opština-wide bbox (no centroid needed): a naselje whose name matches
                # its own opština (e.g. naselje=SUBOTICA, opstina=SUBOTICA — the city itself, as
                # opposed to a peripheral village like Čantavir under the same opština) is really
                # the whole city, not just whatever RGZ happens to tag with that exact naselje
                # name — see build_settlements() for why this matters for radius_steps.
                obbox = opstina_acc.setdefault(opstina_lat, [90.0, -90.0, 180.0, -180.0])
                for lon, lat in _flatten_coords(geom):
                    acc[0] += lat
                    acc[1] += lon
                    acc[2] += 1
                    acc[5] = min(acc[5], lat)
                    acc[6] = max(acc[6], lat)
                    acc[7] = min(acc[7], lon)
                    acc[8] = max(acc[8], lon)
                    obbox[0] = min(obbox[0], lat)
                    obbox[1] = max(obbox[1], lat)
                    obbox[2] = min(obbox[2], lon)
                    obbox[3] = max(obbox[3], lon)

            count += 1

        cursor.executemany('''
            INSERT OR REPLACE INTO rgz_streets
                (ulica_maticni_broj, name, name_lat, naselje, naselje_lat, opstina, opstina_lat, geometry_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', records)

    cursor.execute("CREATE INDEX idx_rgz_streets_lookup ON rgz_streets(name_lat, opstina_lat)")
    cursor.execute("CREATE INDEX idx_rgz_streets_naselje ON rgz_streets(naselje_lat)")
    return count


def _flatten_coords(geom: dict):
    """Yields every [lon, lat] pair out of a GeoJSON Point/LineString/MultiLineString dict."""
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        yield coords
    elif gtype == "LineString":
        yield from coords
    elif gtype == "MultiLineString":
        for line in coords:
            yield from line


def build_housenumbers(cursor, raw_dir: str) -> int:
    cursor.execute("DROP TABLE IF EXISTS rgz_housenumbers")
    cursor.execute('''
        CREATE TABLE rgz_housenumbers (
            kucni_broj_id TEXT PRIMARY KEY,
            ulica_maticni_broj TEXT,
            kucni_broj TEXT,
            lat REAL, lon REAL,
            naselje_lat TEXT, opstina_lat TEXT,
            is_active INTEGER
        )
    ''')

    count = 0
    files = sorted(glob.glob(os.path.join(raw_dir, "rgz_housenumbers_*.csv")))
    for path in files:
        print(f"  📄 {os.path.basename(path)}")
        records = []
        for row in iter_csv_rows(path):
            hn_id = (row.get("kucni_broj_id") or "").strip()
            if not hn_id:
                continue

            geom = reproject_wkt(row.get("geometry"))
            if not geom or geom.get("type") != "Point":
                continue
            lon, lat = geom["coordinates"]

            is_active = 1 if (
                _clean(row.get("vrsta_stanja_lat")) == "AKTIVAN" and not (row.get("retired") or "").strip()
            ) else 0

            records.append((
                hn_id,
                (row.get("ulica_maticni_broj") or "").strip(),
                (row.get("kucni_broj") or "").strip(),
                lat, lon,
                _clean(row.get("naselje_ime_lat")),
                _clean(row.get("opstina_ime_lat")),
                is_active,
            ))
            count += 1

            if len(records) >= 20000:
                cursor.executemany('''
                    INSERT OR REPLACE INTO rgz_housenumbers
                        (kucni_broj_id, ulica_maticni_broj, kucni_broj, lat, lon, naselje_lat, opstina_lat, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', records)
                records = []

        if records:
            cursor.executemany('''
                INSERT OR REPLACE INTO rgz_housenumbers
                    (kucni_broj_id, ulica_maticni_broj, kucni_broj, lat, lon, naselje_lat, opstina_lat, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', records)

    cursor.execute("CREATE INDEX idx_rgz_housenumbers_street ON rgz_housenumbers(ulica_maticni_broj)")
    return count


def build_settlements(cursor, settlement_acc: dict, opstina_acc: dict, resolution: int = 9) -> int:
    cursor.execute("DROP TABLE IF EXISTS rgz_settlements")
    cursor.execute('''
        CREATE TABLE rgz_settlements (
            naselje_lat TEXT, opstina_lat TEXT,
            naselje TEXT, opstina TEXT,
            centroid_lat REAL, centroid_lon REAL,
            radius_steps INTEGER,
            PRIMARY KEY (naselje_lat, opstina_lat)
        )
    ''')

    records = []
    for (naselje_lat, opstina_lat), acc in settlement_acc.items():
        sum_lat, sum_lon, n, naselje, opstina, min_lat, max_lat, min_lon, max_lon = acc
        if n == 0:
            continue
        centroid_lat, centroid_lon = sum_lat / n, sum_lon / n

        # A naselje that shares its opština's own name (e.g. naselje=SUBOTICA, opstina=SUBOTICA)
        # *is* the city itself, as opposed to a peripheral village under the same opština (e.g.
        # Čantavir) — use the whole opština's street bbox for it rather than just this one
        # naselje tag's own extent, which can be a much narrower slice of the real urban area.
        if naselje_lat == opstina_lat and opstina_lat in opstina_acc:
            _, o_max_lat, _, o_max_lon = opstina_acc[opstina_lat]
            max_lat, max_lon = max(max_lat, o_max_lat), max(max_lon, o_max_lon)

        # Same method (not just the same shape) as the Nominatim bounding-box path in
        # get_settlement_info() — h3.grid_distance() between the centroid cell and a bbox-corner
        # cell, not an independent km-based estimate — so radius_steps stays on the exact scale
        # SMALL_SETTLEMENT_RADIUS_THRESHOLD (core/matchmaker.py) was calibrated against. A
        # from-scratch approximation here previously came out ~2x too small (e.g. Subotica
        # computed as 49, vs. its real ~104), misclassifying real cities as "village scale".
        center_hex = h3.latlng_to_cell(centroid_lat, centroid_lon, resolution)
        corner_hex = h3.latlng_to_cell(max_lat, max_lon, resolution)
        try:
            box_radius = h3.grid_distance(center_hex, corner_hex)
        except Exception:
            # h3.grid_distance() can fail outright (H3FailedError) for cells far enough apart to
            # cross icosahedron faces awkwardly — rare, but seen on a few of these ~38 opštine.
            # A large fixed radius is a safe fallback here: it only ever makes a settlement look
            # *bigger* than the village-scale cutoff, never smaller, so it can't cause the
            # wrong-direction failure (misclassifying a real city as village-scale) this whole
            # h3-based approach exists to avoid.
            box_radius = 200
        radius_steps = max(25, int(box_radius * 1.5))
        records.append((naselje_lat, opstina_lat, naselje, opstina, centroid_lat, centroid_lon, radius_steps))

    cursor.executemany('''
        INSERT OR REPLACE INTO rgz_settlements
            (naselje_lat, opstina_lat, naselje, opstina, centroid_lat, centroid_lon, radius_steps)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', records)
    return len(records)


def build_index(raw_dir: str = DEFAULT_RAW_DIR):
    if not os.path.isdir(raw_dir):
        print(f"❌ Source directory not found: {raw_dir}")
        return

    os.makedirs(os.path.dirname(RGZ_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(RGZ_DB_PATH)
    # Fresh derived DB, fully rebuilt each run — safe to trade durability for build speed.
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = MEMORY")
    cursor = conn.cursor()

    print("⏳ Importing RGZ streets...")
    settlement_acc: dict = {}
    opstina_acc: dict = {}
    street_count = build_streets(cursor, raw_dir, settlement_acc, opstina_acc)
    conn.commit()
    print(f"🎉 Indexed {street_count} streets.")

    print("⏳ Importing RGZ house numbers...")
    hn_count = build_housenumbers(cursor, raw_dir)
    conn.commit()
    print(f"🎉 Indexed {hn_count} house numbers.")

    print("⏳ Deriving settlement centroids...")
    settlement_count = build_settlements(cursor, settlement_acc, opstina_acc)
    conn.commit()
    print(f"🎉 Indexed {settlement_count} settlements.")

    conn.close()
    print(f"✅ Success! Built '{RGZ_DB_PATH}'.")


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RAW_DIR
    build_index(source)
