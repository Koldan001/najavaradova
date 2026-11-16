import difflib
import json
import math
import re
import sqlite3
import os
import h3
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from shapely.geometry import shape, LineString, Point, MultiLineString
from shapely.ops import substring, linemerge, nearest_points
from core.database import get_connection

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERBIA_DB_PATH = os.path.join(BASE_DIR, "data", "serbia_streets.db")
# RGZ (Republic Geodetic Authority) cadastral extract — see scripts/build_rgz_index.py. Rows are
# already scoped to the correct naselje/opština with a stable street ID, so wherever it has
# coverage it's tried first and is authoritative; SERBIA_DB_PATH (OSM) + Nominatim remain the
# unchanged fallback for anything outside RGZ's ~38 covered opštine.
RGZ_DB_PATH = os.path.join(BASE_DIR, "data", "rgz.db")

# Hand-drawn boundaries for named areas that have neither an OSM boundary relation nor a
# distinct RGZ naselje — city kvartovi like Берберско / Мала Америка / Градулица, which RGZ
# files under one big 'ZRENJANIN' naselje and Nominatim knows only as a point. A GeoJSON
# FeatureCollection of Polygons; each feature needs properties {"name": "<kvart, any script>",
# "opstina": "<opština, any script>"}. Checked before Nominatim, so a curated shape always wins.
CURATED_AREAS_PATH = os.path.join(BASE_DIR, "data", "kvartovi.geojson")

geolocator = Nominatim(user_agent="ns_outage_notifier_prod_v17")

# Nominatim's public usage policy caps requests at ~1/sec; nothing here ever throttled calls,
# which is exactly what triggers HTTP 429 (GeocoderRateLimited) under any real load — every
# fallback query variant fired back-to-back only makes it worse. geopy's RateLimiter is the
# standard fix: spaces calls out and retries automatically on rate-limit/timeout errors with
# backoff, mirroring the 429 backoff parser.py already does for the Gemini API.
_geocode_with_backoff = RateLimiter(
    geolocator.geocode,
    min_delay_seconds=1.1,
    max_retries=2,
    error_wait_seconds=3.0,
    swallow_exceptions=False,
)

CYR_TO_LAT = {
    'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Ђ': 'Đ', 'Е': 'E', 'Ж': 'Ž',
    'З': 'Z', 'И': 'I', 'Ј': 'J', 'К': 'K', 'Л': 'L', 'Љ': 'LJ', 'М': 'M', 'Н': 'N',
    'Њ': 'NJ', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'Ћ': 'Ć', 'У': 'U',
    'Ф': 'F', 'Х': 'H', 'Ц': 'C', 'Ч': 'Č', 'Џ': 'DŽ', 'Ш': 'Š'
}

def cyrillic_to_latin(text: str) -> str:
    if not text:
        return ""
    return "".join(CYR_TO_LAT.get(c, c) for c in text.upper())

def clean_street(query: str) -> str:
    if not query:
        return ""
    
    query = query.strip()
    patterns_to_remove = [
        r'(?i)^\s*ulica\s+', r'(?i)^\s*ul\.\s*',
        r'(?i)^\s*улица\s+', r'(?i)^\s*ул\.\s*',
        r'(?i)\s+ulica\s*$', r'(?i)\s+улица\s*$',
        r'(?i)\bkraj\b', r'(?i)\bкрај\b',
        r'(?i)\bpocetak\b', r'(?i)\bпочетак\b',
        r'(?i)\bsvi\s+brojevi\b', r'(?i)\bсви\s+бројеви\b',
        r'(?i)\bneparna\s+strana\b', r'(?i)\bнепарна\s+страна\b',
        r'(?i)\bparna\s+strana\b', r'(?i)\bпарна\s+страна\b',
        r'(?i)\bdeo\s+ulice\b', r'(?i)\bдео\s+улице\b',
        r'(?i)\bod\s+broja\b.*', r'(?i)\bод\s+броја\b.*',
        r'(?i)\b(m|e)-\d+\b',  # Strips M-22, E-75 route prefixes
    ]

    for pat in patterns_to_remove:
        query = re.sub(pat, '', query)

    # Fix missing spaces after initials (e.g., "Т.Рајића" -> "Т. Рајића")
    query = re.sub(r'([A-Za-zĐćčžšЂјљњћџА-Яа-я])\.\s*([A-Za-zĐćčžšЂјљњћџА-Яа-я])', r'\1. \2', query)
    return re.sub(r'[\s\.,\-]+$', '', query).strip()

def clean_settlement(query: str) -> str:
    """Strips common Serbian settlement-reference noise (place-type prefixes, abbreviations)
    before a settlement name is geocoded or used as a cache key. Mirrors clean_street()."""
    if not query:
        return ""

    query = query.strip()
    patterns_to_remove = [
        r'(?i)^\s*место\s+', r'(?i)^\s*mesto\s+',
        r'(?i)^\s*насеље\s+', r'(?i)^\s*naselje\s+',
        r'(?i)^\s*сл\.\s*', r'(?i)^\s*sl\.\s*',
        r'(?i)^\s*цела\s+', r'(?i)^\s*цео\s+',
        r'(?i)^\s*cela\s+', r'(?i)^\s*ceo\s+',
        r'(?i)^\s*у\s+насељу\s+', r'(?i)^\s*u\s+naselju\s+',
        r'(?i)^\s*у\s+месту\s+', r'(?i)^\s*u\s+mestu\s+',
    ]
    for pat in patterns_to_remove:
        query = re.sub(pat, '', query)

    return re.sub(r'[\s\.,\-:]+$', '', query).strip()


def meters_to_degrees_lon(meters: float, at_lat: float) -> float:
    """Converts a real-world meter distance to degrees of longitude at a given latitude.
    A degree of longitude shrinks by cos(latitude) moving away from the equator, so a flat
    degree constant is effectively a tighter real-world tolerance further north (e.g. Subotica)
    than further south (e.g. Pančevo) for the same nominal value."""
    meters_per_degree_lon = 111320.0 * max(math.cos(math.radians(at_lat)), 0.01)
    return meters / meters_per_degree_lon


def _segments_length_m(segments: list[list]) -> float:
    """Real-world (haversine) total length of a list of [lon, lat] coordinate lists, summed
    fragment by fragment. Deliberately not the degree-Euclidean approximation meters_to_degrees_lon
    supports elsewhere in this module: that's fine for comparing two nearby points at a similar
    latitude, but RGZ streets are stored as many separate block-sized fragments rather than one
    continuous way — linemerge's consolidated path length can read much *shorter* than the real
    distance covered by summing every fragment's own length independently, so a cap meant to catch
    "this unclipped street is too long to hand back safely" has to measure the same thing it's
    capping (see clip_polyline_by_intersections)."""
    R = 6371000
    total = 0.0
    for seg in segments:
        for (lon1, lat1), (lon2, lat2) in zip(seg, seg[1:]):
            p1, p2 = math.radians(lat1), math.radians(lat2)
            dphi = math.radians(lat2 - lat1)
            dlmb = math.radians(lon2 - lon1)
            a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
            total += 2 * R * math.asin(math.sqrt(a))
    return total


def get_smart_stem(word: str) -> str:
    """Strips a Serbian case-ending suffix, in EITHER script. Cyrillic suffixes are additive and
    can never fire on a Latin-transliterated string or vice versa (disjoint Unicode ranges), so
    this is safe for every existing caller that pre-transliterates - and it enables a new
    Cyrillic-input use in get_intersection_point, which needs to stem raw Cyrillic genitive-case
    cross-street names (e.g. "БАТУТОВЕ" from "од Батутове до...") straight out of the parser,
    with no transliteration step."""
    word = word.strip()
    if len(word) < 4:
        return word
    for suf in ['OVA', 'EVA', 'INA', 'OVE', 'EVE', 'INE', 'OG', 'EG', 'OM', 'IM', 'IH',
                'ОВА', 'ЕВА', 'ИНА', 'ОВЕ', 'ЕВЕ', 'ИНЕ', 'ОГ', 'ЕГ', 'ОМ', 'ИМ', 'ИХ']:
        if word.endswith(suf):
            return word[:-len(suf)]
    for suf in ['A', 'E', 'U', 'I', 'O', 'А', 'Е', 'У', 'И', 'О']:
        if word.endswith(suf):
            return word[:-len(suf)]
    return word

def _validate_geocode_result(location, parent_region: str) -> bool:
    """Cross-checks a Nominatim result's address components against the expected parent
    region (opština/city) before we trust and cache it. Without this, a generic toponym
    (e.g. "Виногради" = "vineyards", reused across many Serbian towns) can silently resolve
    to the wrong place. Returns True if there's nothing to validate against (parent_region
    not supplied) or the result's address plausibly contains it."""
    if not parent_region:
        return True
    address = (location.raw or {}).get('address', {})
    if not address:
        return True  # no addressdetails available to cross-check; accept rather than false-reject

    clean_parent = cyrillic_to_latin(parent_region.strip()).upper()
    haystack = " ".join(
        cyrillic_to_latin(str(v)).upper()
        for v in address.values()
        if isinstance(v, str)
    )
    return clean_parent in haystack


def _inside_parent_area(latlng, clean_settlement_name: str, parent_region: str | None) -> bool:
    """A geocoded settlement must lie inside its parent opština's area (region_envelope).
    _validate_geocode_result can only compare address text, and passes a result that carries no
    address at all - which is how a generic toponym ("Виногради" = vineyards) for Stara Pazova
    resolved to Kragujevac. True when there is nothing to check against (no parent, the name IS
    the parent - the envelope lookup itself goes through here - or the parent can't be placed)."""
    if not parent_region:
        return True
    if _norm_region_lat(parent_region) == _norm_region_lat(clean_settlement_name):
        return True
    env = region_envelope(parent_region)
    return env is None or envelope_distance_km(env, latlng[0], latlng[1]) <= 0


def _get_rgz_settlement_info(clean_settlement_name: str, clean_parent: str, resolution: int) -> tuple[str, int] | tuple[None, None]:
    """RGZ-sourced equivalent of the settlement_cache/Nominatim lookup below — instant, offline,
    and immune to the same-toponym-different-town risk _validate_geocode_result works around,
    for any naselje RGZ actually covers. See scripts/build_rgz_index.py for how centroid/
    radius_steps are derived from RGZ's own street geometry."""
    if not os.path.exists(RGZ_DB_PATH):
        return None, None

    # Same kvart->parent-naselje alias as query_rgz_streets(): "Рибњак" has no rgz_settlements
    # row of its own; its centroid/radius are Sremska Kamenica's. Without this the two #792
    # streets RGZ doesn't carry under Kamenica fall to the OSM path with no settlement hex,
    # and the proximity cutoff (city-sized) reaches Srbobran's same-named streets.
    clean_settlement_name = _dealias_naselje(clean_settlement_name)

    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    cursor = conn.cursor()

    # Same rule as query_rgz_streets(): a naselje name is not unique nationwide, so a given
    # opština is never silently discarded on a miss — that would risk resolving to a
    # same-named naselje under a completely different (and unscoped, not even same-opština)
    # part of the country. Only fall back to an unscoped naselje-only lookup when the caller
    # genuinely gave us no parent region to scope by in the first place.
    if clean_parent:
        cursor.execute(
            "SELECT centroid_lat, centroid_lon, radius_steps FROM rgz_settlements WHERE naselje_lat = ? AND opstina_lat = ?",
            (clean_settlement_name, clean_parent)
        )
        row = cursor.fetchone()
    else:
        cursor.execute(
            "SELECT centroid_lat, centroid_lon, radius_steps FROM rgz_settlements WHERE naselje_lat = ?",
            (clean_settlement_name,)
        )
        row = cursor.fetchone()

    # Same gap as query_rgz_streets(): a bare gradska opština name ("CRVENI KRST") matches
    # neither form above (RGZ's real naselje_lat is the compound "NIŠ (CRVENI KRST)") - without
    # this, the caller falls through to settlement_cache/Nominatim, where the bare name can
    # geocode to a same-named entity in a completely different city (the confirmed Belgrade leak
    # this whole retry chain exists to prevent).
    if not row and clean_settlement_name in _SUBOPSTINA_TO_GRAD:
        grad = _SUBOPSTINA_TO_GRAD[clean_settlement_name]
        cursor.execute(
            "SELECT centroid_lat, centroid_lon, radius_steps FROM rgz_settlements "
            "WHERE naselje_lat = ? AND opstina_lat = ?",
            (f"{grad} ({clean_settlement_name})", clean_settlement_name),
        )
        row = cursor.fetchone()

    conn.close()
    if not row:
        return None, None

    hex_id = h3.latlng_to_cell(row[0], row[1], resolution)
    return hex_id, row[2]


def get_settlement_info(settlement_name: str, parent_region: str = None, resolution: int = 9) -> tuple[str, int] | tuple[None, None]:
    if not settlement_name:
        return None, None

    clean_settlement_name = cyrillic_to_latin(clean_settlement(settlement_name)).upper()
    clean_parent = cyrillic_to_latin(parent_region.strip()).upper() if parent_region else ""

    rgz_hex, rgz_radius = _get_rgz_settlement_info(clean_settlement_name, clean_parent, resolution)
    if rgz_hex:
        return rgz_hex, rgz_radius

    cache_key = f"{clean_settlement_name}_{clean_parent}" if clean_parent else clean_settlement_name

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT hex_id, radius_steps FROM settlement_cache WHERE UPPER(name) = ?", (cache_key,))
    row = cursor.fetchone()
    if row:
        conn.close()
        # Entries cached before the parent-area check below existed can be wrong - confirmed:
        # "VINOGRADI_STARA PAZOVA" was cached at Kragujevac's Vinogradi, ~110 km away.
        if not _inside_parent_area(h3.cell_to_latlng(row['hex_id']), clean_settlement_name, parent_region):
            return None, None
        return row['hex_id'], row['radius_steps']

    # Negative cache: a name that has already failed to geocode as a settlement (e.g. a street
    # name mistakenly tried as a place by clip_polyline_by_intersections' "toward X" fallback)
    # would otherwise be re-queried against live Nominatim on every single call, forever.
    cursor.execute("SELECT 1 FROM settlement_lookup_failures WHERE name = ?", (cache_key,))
    if cursor.fetchone():
        conn.close()
        return None, None

    search_queries = []
    if clean_parent and clean_parent != clean_settlement_name:
        search_queries.append(f"{clean_settlement_name}, {clean_parent}, Serbia")
    search_queries.append(f"{clean_settlement_name}, Serbia")

    # Serbian settlement names get grammatically declined in running text (e.g. "према Сигету"
    # = "towards Siget", dative case) — the extracted name can be the inflected form, which
    # Nominatim won't match even though the nominative form resolves fine. get_smart_stem()
    # (already used for street-name matching) strips common Serbian noun-case endings; try it
    # as a last resort if the direct forms above all fail.
    stemmed = get_smart_stem(clean_settlement_name)
    if stemmed and stemmed != clean_settlement_name:
        if clean_parent:
            search_queries.append(f"{stemmed}, {clean_parent}, Serbia")
        search_queries.append(f"{stemmed}, Serbia")

    location = None
    got_a_real_response = False  # distinguishes "Nominatim answered: no results" from "the
                                  # request itself failed" (timeout/network) — only the former
                                  # is safe to bake into the negative cache below.
    for q in search_queries:
        try:
            candidate = _geocode_with_backoff(q, timeout=5, country_codes='rs', addressdetails=True)
            got_a_real_response = True
            if candidate and _validate_geocode_result(candidate, parent_region) and \
                    _inside_parent_area((candidate.latitude, candidate.longitude),
                                        clean_settlement_name, parent_region):
                location = candidate
                break
        except Exception:
            continue

    if location:
        center_lat, center_lon = location.latitude, location.longitude
        center_hex = h3.latlng_to_cell(center_lat, center_lon, resolution)
        
        box_radius = 20
        if location.raw.get('boundingbox'):
            bbox = location.raw['boundingbox']
            corner_lat, corner_lon = float(bbox[1]), float(bbox[3])
            corner_hex = h3.latlng_to_cell(corner_lat, corner_lon, resolution)
            box_radius = h3.grid_distance(center_hex, corner_hex)
        
        radius_steps = max(25, int(box_radius * 1.5))
        verified_opstina = (location.raw or {}).get('address', {}).get('county') \
            or (location.raw or {}).get('address', {}).get('municipality')

        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settlement_cache (name, lat, lng, hex_id, radius_steps, source, verified_opstina) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cache_key, center_lat, center_lon, center_hex, radius_steps, 'nominatim', verified_opstina)
                )
        except Exception as e:
            print(f"⚠️ Cache write error: {e}")
        finally:
            conn.close()

        return center_hex, radius_steps

    if got_a_real_response:
        try:
            with conn:
                conn.execute("INSERT OR IGNORE INTO settlement_lookup_failures (name) VALUES (?)", (cache_key,))
        except Exception:
            pass
    conn.close()
    return None, None

def geojson_to_h3_cells(geojson_dict: dict, resolution: int = 9) -> set[str]:
    if not geojson_dict:
        return set()
    try:
        return set(h3.polygon_to_cells(h3.GeoJson(geojson_dict), resolution))
    except Exception:
        pass

    hexes = set()
    try:
        geom = shape(geojson_dict)
        polygons = [geom] if geom.geom_type == 'Polygon' else (list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [])
        for poly in polygons:
            outer = [(p[1], p[0]) for p in poly.exterior.coords]
            holes = [[(p[1], p[0]) for p in interior.coords] for interior in poly.interiors]
            h3_poly = h3.LatLngPoly(outer, *holes)
            hexes.update(h3.polygon_to_cells(h3_poly, resolution))
    except Exception:
        pass
    return hexes

def hexes_to_geojson_feature(hex_ids, properties: dict = None) -> dict | None:
    """Inverse of geojson_to_h3_cells: dissolves a set of H3 cells back into a drawable outline.

    Deliberately renders the H3 coverage itself rather than re-fetching the original Nominatim
    boundary: the hexes are exactly what the matcher notifies on, so drawing them keeps the map
    honest about the real notification footprint instead of showing a tidier shape that doesn't
    match who actually gets a message. Used for whole-settlement and "surrounding streets"
    outages, which have no street polyline to draw at all and were previously invisible."""
    cells = list(hex_ids or [])
    if not cells:
        return None

    try:
        shape_obj = h3.cells_to_h3shape(cells)
        geometry = h3.h3shape_to_geo(shape_obj)
    except Exception:
        return None

    if not geometry or not geometry.get('coordinates'):
        return None

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": properties or {}
    }


def _clip_segments_to_cells(segments, cells: set, resolution: int) -> list[list]:
    """Keep only the runs of a polyline whose vertices fall inside `cells`, as separate
    coordinate lists (each >= 2 points). A vertex outside the set breaks the current run."""
    kept = []
    for seg in segments:
        run = []
        for pt in seg:
            if len(pt) >= 2 and h3.latlng_to_cell(pt[1], pt[0], resolution) in cells:
                run.append([pt[0], pt[1]])
            else:
                if len(run) >= 2:
                    kept.append(run)
                run = []
        if len(run) >= 2:
            kept.append(run)
    return kept


def _runs_to_geometry(runs: list[list]) -> dict | None:
    if not runs:
        return None
    if len(runs) == 1:
        return {"type": "LineString", "coordinates": runs[0]}
    return {"type": "MultiLineString", "coordinates": runs}


def get_streets_within_hexes(hex_ids, parent_region: str = None, resolution: int = 9,
                             max_streets: int = 120) -> list[dict]:
    """Every RGZ-registered street with geometry inside the given H3 cells, clipped to the part
    that actually falls inside them, as drawable LineString features.

    This is what lets a whole-settlement or kvart outage draw as the streets it really covers
    instead of one translucent blob stretched over everything inside the administrative boundary
    - farmland, the airfield, the Begej, rail yards. In central Zrenjanin several such blobs
    also stacked on top of each other into an unreadable mess. Matching still runs on the hexes
    (targets are hex-mapped); only the map geometry changes.

    Returns [] when the parent region has no RGZ street table coverage, so callers can fall back
    to hexes_to_geojson_feature."""
    cells = hex_ids if isinstance(hex_ids, (set, frozenset)) else set(hex_ids or [])
    if not cells or not parent_region or not os.path.exists(RGZ_DB_PATH):
        return []

    opstina_lat = cyrillic_to_latin(parent_region.strip()).upper()

    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT name_lat, geometry_json FROM rgz_streets WHERE UPPER(opstina_lat) = ?",
            (opstina_lat,)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    features = []
    for name, geometry_json in rows:
        kept_runs = _clip_segments_to_cells(
            _geometry_rows_to_segments([(geometry_json,)]), cells, resolution
        )
        geometry = _runs_to_geometry(kept_runs)
        if not geometry:
            continue
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {"kind": "street", "street": (name or "").title()}
        })
        if len(features) >= max_streets:
            break

    return features


def get_settlement_polygon_hexes(settlement_name: str, parent_region: str = None, resolution: int = 9) -> set[str]:
    """Hexes only. Callers that need to know whether those hexes came from a real administrative
    boundary or from the crude center-point disk should use get_settlement_coverage instead."""
    return get_settlement_coverage(settlement_name, parent_region, resolution)[0]


def get_curated_area_hexes(settlement_name: str, parent_region: str = None, resolution: int = 9) -> set[str]:
    """H3 cells for a hand-drawn boundary in data/kvartovi.geojson, or an empty set if the file
    is absent or has no feature matching this name (+ opština, when the feature names one).
    Name/opština are compared transliterated-and-cleaned, same as everywhere else, so the
    GeoJSON can carry Cyrillic or Latin."""
    if not settlement_name or not os.path.exists(CURATED_AREAS_PATH):
        return set()

    try:
        with open(CURATED_AREAS_PATH, encoding="utf-8") as f:
            collection = json.load(f)
    except Exception:
        return set()

    want_name = cyrillic_to_latin(clean_settlement(settlement_name)).upper().strip()
    want_opstina = cyrillic_to_latin((parent_region or "").strip()).upper().strip()

    for feature in collection.get("features", []):
        props = feature.get("properties") or {}
        feat_name = cyrillic_to_latin(clean_settlement(str(props.get("name", "")))).upper().strip()
        if feat_name != want_name:
            continue
        feat_opstina = cyrillic_to_latin(str(props.get("opstina", "")).strip()).upper().strip()
        if want_opstina and feat_opstina and feat_opstina != want_opstina:
            continue
        hexes = geojson_to_h3_cells(feature.get("geometry"), resolution)
        if hexes:
            return hexes
    return set()


# Cities whose territory RGZ splits into several gradske opštine, so a street/village named with
# only the city has to be looked for across all of them. RGZ stores city streets under a naselje
# like "NIŠ (MEDIJANA)" and villages under their own name inside one of these opštine.
_GRAD_OPSTINE = {
    "BEOGRAD": [
        "ČUKARICA", "NOVI BEOGRAD", "PALILULA (BEOGRAD)", "RAKOVICA", "SAVSKI VENAC",
        "STARI GRAD", "SURČIN", "VOŽDOVAC", "VRAČAR", "ZEMUN", "ZVEZDARA",
    ],
    "NIŠ": ["CRVENI KRST", "MEDIJANA", "NIŠKA BANJA", "PALILULA (NIŠ)", "PANTELEJ"],
}

# Reverse of _GRAD_OPSTINE: sub-opština name -> the grad umbrella it belongs to. A caller that
# only knows the specific gradska opština (e.g. EPS's own "Opština: Crveni krst" table column -
# it never says "Niš") needs this to land on RGZ's compound naselje_lat ("NIŠ (CRVENI KRST)")
# directly. Without it, both query_rgz_streets() and _get_rgz_settlement_info() below only knew
# how to expand the *umbrella city* name ("NIŠ") across its 5 opštine - a bare sub-opština name
# matched neither an exact naselje_lat nor that retry, fell all the way through to OSM+Nominatim,
# and Nominatim geocoded the bare string ("Crveni Krst, Serbia") to some unrelated same-named
# entity in Belgrade instead of the Niš municipality - a real, confirmed leak (2026-09-04): an
# EPS outage in Niš's Crveni Krst opština got anchored in Belgrade, drawing a real Belgrade
# street's geometry on the public map for a Niš outage.
_SUBOPSTINA_TO_GRAD = {sub: grad for grad, subs in _GRAD_OPSTINE.items() for sub in subs}

# Every RGZ opština a grad umbrella covers - _GRAD_OPSTINE above lists only the urban gradske
# opštine (their streets sit under a compound "BEOGRAD (X)" naselje), but Grad Beograd also
# includes six suburban opštine whose villages are ordinary naselja. BVK serves all of them.
_GRAD_ALL_OPSTINE = {
    "BEOGRAD": _GRAD_OPSTINE["BEOGRAD"] + [
        "BARAJEVO", "GROCKA", "LAZAREVAC", "MLADENOVAC", "OBRENOVAC", "SOPOT",
    ],
    "NIŠ": list(_GRAD_OPSTINE["NIŠ"]),
}

_rgz_opstine_cache: frozenset | None = None


def _rgz_opstine() -> frozenset:
    """Every opštine RGZ carries streets for (58 as of Sep 2026). Read once per process."""
    global _rgz_opstine_cache
    if _rgz_opstine_cache is None:
        if not os.path.exists(RGZ_DB_PATH):
            return frozenset()
        conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
        try:
            _rgz_opstine_cache = frozenset(
                r[0] for r in conn.execute("SELECT DISTINCT opstina_lat FROM rgz_streets") if r[0]
            )
        finally:
            conn.close()
    return _rgz_opstine_cache


# EPS's own spellings of an opština, where they differ from RGZ/serbia_places.
_REGION_ALIASES = {
    "PETROVAC": "PETROVAC NA MLAVI",
}


def _norm_region_lat(name: str | None) -> str:
    """'Град Београд' / 'ОПШТИНА СМЕДЕРЕВО' / 'Палилула - Ниш' -> 'BEOGRAD' / 'SMEDEREVO' /
    'PALILULA (NIŠ)' - the forms RGZ and core/serbia_places.py use."""
    lat = cyrillic_to_latin((name or "").strip()).upper().strip()
    # EPS writes a gradska opština as "Палилула - Ниш"; RGZ as "PALILULA (NIŠ)". Before
    # clean_settlement, which strips a trailing " -".
    lat = re.sub(r"^(.+?)\s+-\s+(NIŠ|BEOGRAD|NOVI SAD|KRAGUJEVAC)$", r"\1 (\2)", lat)
    lat = cyrillic_to_latin(clean_settlement(lat)).upper().strip()
    lat = re.sub(r"^(GRAD|OPŠTINA|OPSTINA|GRADSKA OPŠTINA)\s+", "", lat).strip()
    return _REGION_ALIASES.get(lat, lat)


_serbian_opstine_cache: frozenset | None = None


def is_serbian_opstina(name: str | None) -> bool:
    """True for any Serbian opština / grad / gradska opština name - including the ones RGZ
    doesn't carry yet (core/serbia_places.py lists them all), as opposed to a naselje name."""
    global _serbian_opstine_cache
    if _serbian_opstine_cache is None:
        from core.serbia_places import OPSTINE
        names = {_norm_region_lat(o) for o in OPSTINE}
        names |= set(_rgz_opstine()) | set(_GRAD_ALL_OPSTINE)
        names |= {o.split(" (")[0] for o in names}  # "PALILULA (BEOGRAD)" -> "PALILULA"
        _serbian_opstine_cache = frozenset(n for n in names if n)
    return _norm_region_lat(name) in _serbian_opstine_cache


def naselje_owner_opstine(naselje: str | None) -> set[str]:
    """The RGZ opštine that have a naselje by this name (usually one; 60 names repeat)."""
    n = _dealias_naselje(_norm_region_lat(naselje))
    if not n or not os.path.exists(RGZ_DB_PATH):
        return set()
    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT opstina_lat FROM rgz_settlements WHERE naselje_lat = ?", (n,))}
    finally:
        conn.close()


def rgz_naselje_street_count(naselje: str | None, parent_region: str | None = None) -> int | None:
    """How many RGZ streets a naselje has, within the region's opštine when it names any - a
    village has a few dozen, a town a hundred or more. None when RGZ doesn't carry the place."""
    n = _dealias_naselje(_norm_region_lat(naselje))
    if not n or not os.path.exists(RGZ_DB_PATH):
        return None
    ops = region_opstine(parent_region) if parent_region else None
    sql = "SELECT COUNT(*) FROM rgz_streets WHERE naselje_lat = ?"
    args = [n]
    if ops:
        sql += f" AND opstina_lat IN ({','.join('?' * len(ops))})"
        args += sorted(ops)
    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        count = conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()
    return count or None


def region_opstine(region: str | None) -> set[str] | None:
    """The RGZ opštine an announcement's region name stands for: a grad umbrella -> all of its
    opštine; "PALILULA" -> both Palilulas (Beograd, Niš); any other name -> that opština itself.
    None when RGZ carries none of them - the caller then can't vouch for any RGZ hit at all."""
    r = _norm_region_lat(region)
    if not r:
        return None
    known = _rgz_opstine()
    if r in _GRAD_ALL_OPSTINE:
        cand = set(_GRAD_ALL_OPSTINE[r])
    else:
        cand = {o for o in known if o == r or o.startswith(r + " (")}
    cand &= known
    return cand or None


# ~H3 res9 grid step, rounded up so the envelope errs on the side of containing a real place.
_STEP_KM = 0.35
# Slack around each naselje disk. Generous on purpose: EPS's "Opština" column is sometimes the
# branch's home opština rather than the village's own (a Temerin or Beočin village listed under
# "Нови Сад"), and a gate that blocks a real neighbouring village is a missed alert. 10 km still
# stops the far-away class this gate exists for (#2365: 125 km, Vinogradi: 110 km); a nearer
# same-named place is the scoped lookups' job, not the gate's.
_ENVELOPE_MARGIN_KM = 10.0
# Region RGZ doesn't carry: all we have is the seat town's position (OSM/Nominatim), so allow a
# radius that covers even Serbia's largest rural opštine (Knjaževac, Ivanjica ~35 km from the
# seat to the far villages). It still stops the cross-country class of leak (#2365: 125 km).
_FALLBACK_REGION_RADIUS_KM = 40.0

_envelope_cache: dict = {}


def region_envelope(region: str | None) -> list[tuple[float, float, float]] | None:
    """Where an announcement for `region` can physically be: a list of (lat, lon, radius_km)
    disks. For an opština RGZ carries, one disk per naselje (its own centroid + radius + margin) -
    a tight, data-driven outline of the opština. Otherwise one generous disk around the region's
    geocoded seat. None when the region can't be placed at all (then nothing can be gated)."""
    key = _norm_region_lat(region)
    if not key:
        return None
    if key in _envelope_cache:
        return _envelope_cache[key]

    env = None
    ops = region_opstine(region)
    if not ops and not is_serbian_opstina(region):
        # A village standing in as the region (EPS's inline "Буковац: ..."): the opština RGZ
        # files it under is a far safer outline than a geocode of a name that repeats.
        ops = naselje_owner_opstine(region) or None
    if ops and os.path.exists(RGZ_DB_PATH):
        conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
        try:
            marks = ",".join("?" * len(ops))
            rows = conn.execute(
                f"SELECT centroid_lat, centroid_lon, radius_steps FROM rgz_settlements "
                f"WHERE opstina_lat IN ({marks}) AND centroid_lat IS NOT NULL",
                tuple(ops),
            ).fetchall()
        finally:
            conn.close()
        env = [(la, lo, (rs or 0) * _STEP_KM + _ENVELOPE_MARGIN_KM) for la, lo, rs in rows] or None
    if env is None:
        # Parent = the region itself: an unscoped lookup could land on a same-named village in
        # another opština (naselje names repeat - there are two Vučaks, seven Slatinas).
        center_hex, radius_steps = get_settlement_info(region, region)
        if center_hex:
            la, lo = h3.cell_to_latlng(center_hex)
            env = [(la, lo, max((radius_steps or 0) * _STEP_KM, _FALLBACK_REGION_RADIUS_KM))]

    _envelope_cache[key] = env
    return env


def envelope_distance_km(env, lat: float, lon: float) -> float:
    """How far (lat, lon) lies outside the envelope, in km; <= 0 means inside."""
    best = float("inf")
    for la, lo, r in env:
        d = math.hypot((lat - la) * 111.0, (lon - lo) * 111.0 * math.cos(math.radians(la)))
        best = min(best, d - r)
    return best


# Neighbourhoods / mesne zajednice EPS names directly in an announcement ("Делови насеља
# Рибњак") that RGZ does NOT carry as their own naselje - their streets are filed under a
# parent naselje. Keyed and valued in Latin upper, applied right after
# cyrillic_to_latin(clean_settlement(...)).upper() so every naselje-scoped lookup downstream
# uses the parent. Confirmed: outage #792, all "Рибњак" streets sit under SREMSKA KAMENICA.
_KVART_TO_NASELJE = {
    "RIBNJAK": "SREMSKA KAMENICA",
}


def _dealias_naselje(naselje_lat: str) -> str:
    return _KVART_TO_NASELJE.get(naselje_lat, naselje_lat)


def get_rgz_naselje_streets(settlement_name: str, parent_region: str) -> list[tuple]:
    """RGZ tags every street with the naselje it belongs to. For a settlement RGZ carries as its
    own naselje — every VILLAGE in an opština (Ечка, Арадац, Меленци, Лукићево…), but NOT a
    kvart of the main town, which RGZ lumps under one 'ZRENJANIN'-type naselje — this returns
    that naselje's exact street rows: (name_lat, geometry_json).

    It's an authoritative boundary, unlike the two guesses below it in get_settlement_coverage:
    Ечка's Nominatim polygon bled ~4x its real size across the canal into Muzlja, and a
    center-point disk for a riverside place spills into whatever is on the far bank."""
    if not settlement_name or not parent_region or not os.path.exists(RGZ_DB_PATH):
        return []

    naselje_lat = _dealias_naselje(cyrillic_to_latin(clean_settlement(settlement_name)).upper().strip())
    opstina_lat = _norm_region_lat(parent_region)
    if not naselje_lat or naselje_lat == opstina_lat:
        return []

    # A village given with only its multi-opština city (Суповац/Ниш, Габровац/Ниш) sits under one
    # of that city's gradske opštine, not under an "opstina_lat = NIŠ" row that doesn't exist -
    # and Борча given with "Палилула - Београд" (EPS's Belgrade pages) sits under RGZ's
    # "PALILULA (BEOGRAD)". region_opstine knows every one of those forms.
    opstine = sorted(region_opstine(parent_region) or [opstina_lat])
    placeholders = ",".join("?" * len(opstine))

    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        return conn.execute(
            f"SELECT name_lat, geometry_json FROM rgz_streets "
            f"WHERE UPPER(opstina_lat) IN ({placeholders}) AND UPPER(naselje_lat) = ?",
            (*[o.upper() for o in opstine], naselje_lat)
        ).fetchall()
    finally:
        conn.close()


def closest_rgz_naselje(settlement_name: str, parent_region: str) -> str | None:
    """The real RGZ naselje of this opština a misspelled settlement name means - "ЈЕВРЕГОВАЦ" and
    "ЈЕВРЕЈОВАЦ" for Јевремовац (Šabac, 27.10.2026: the AI misread a title that mixed Latin "J"
    into Cyrillic, and the notice fell back to a 293-hex area). None when the name already is a
    naselje there, or when no single one is clearly closest - never a guess between two."""
    import difflib
    if not settlement_name or not parent_region or not os.path.exists(RGZ_DB_PATH):
        return None
    name = _dealias_naselje(cyrillic_to_latin(clean_settlement(settlement_name)).upper().strip())
    if len(name) < 5:
        return None
    opstine = sorted(region_opstine(parent_region) or [_norm_region_lat(parent_region)])
    placeholders = ",".join("?" * len(opstine))
    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT naselje_lat, naselje FROM rgz_settlements WHERE UPPER(opstina_lat) IN ({placeholders})",
            [o.upper() for o in opstine]).fetchall()
    finally:
        conn.close()
    if any(lat == name for lat, _ in rows):
        return None
    scored = sorted(((difflib.SequenceMatcher(None, name, lat).ratio(), cyr or lat) for lat, cyr in rows),
                    reverse=True)
    if not scored or scored[0][0] < 0.85 or (len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.05):
        return None
    return scored[0][1]


def is_town_part(settlement_name: str, parent_region: str) -> bool:
    """A named kvart of Beograd or Niš (Cerak, Banjica, Pantelej's Ledena stena) rather than a
    settlement of its own. RGZ files every village as its own naselje but lumps a city's kvartovi
    under the city's naselje, so: the region is one of those cities' urban opštine and the name is
    not one of its naselja. Only there - in an ordinary opština a village whose name RGZ spells
    otherwise (Vrelo, Vrbovno, Dragosinci on 30.09.2026) would pass for a kvart. False whenever
    RGZ can't tell (no rgz.db)."""
    ops = region_opstine(parent_region) if settlement_name and parent_region else None
    urban = {o for opstine in _GRAD_OPSTINE.values() for o in opstine}
    if not ops or not (ops & urban):
        return False
    return not get_rgz_naselje_streets(settlement_name, parent_region)


def get_settlement_coverage(settlement_name: str, parent_region: str = None, resolution: int = 9) -> tuple[set[str], str | None]:
    """Returns (hexes, source) where source is:
      'curated'     — a hand-drawn boundary in data/kvartovi.geojson (highest priority);
      'rgz_naselje' — the exact set of streets RGZ files under this naselje (villages only);
      'polygon'     — a real OSM administrative boundary, trustworthy enough to notify on as-is;
      'disk'        — no boundary exists for this name, so a fixed-radius blob around its center
                      point stands in. Common for city kvartovi (Мала Америка, Градулица,
                      Берберско in Zrenjanin all resolve as a bare point, not an area);
      None          — nothing resolved at all.
    A disk is a guess about shape, not a measurement of it, so an outage covered only by a disk
    should not be reported as an EXACT spatial match."""
    if not settlement_name:
        return set(), None

    curated = get_curated_area_hexes(settlement_name, parent_region, resolution)
    if curated:
        return curated, 'curated'

    naselje_rows = get_rgz_naselje_streets(settlement_name, parent_region) if parent_region else []
    if naselje_rows:
        hexes = set()
        for _, geometry_json in naselje_rows:
            for seg in _geometry_rows_to_segments([(geometry_json,)]):
                for pt in seg:
                    if len(pt) >= 2:
                        hexes.add(h3.latlng_to_cell(pt[1], pt[0], resolution))
        if hexes:
            return hexes, 'rgz_naselje'

    clean_settlement_name = cyrillic_to_latin(clean_settlement(settlement_name)).upper()
    clean_parent = cyrillic_to_latin(parent_region.strip()).upper() if parent_region else ""
    cache_key = f"{clean_settlement_name}_{clean_parent}" if clean_parent else clean_settlement_name

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT hex_ids_json FROM settlement_polygon_cache WHERE UPPER(name) = ?", (cache_key,))
    cached = cursor.fetchone()
    if cached:
        conn.close()
        # Only real boundaries are ever written to this cache; disk fallbacks are recomputed.
        return set(json.loads(cached['hex_ids_json'])), 'polygon'

    search_queries = []
    if clean_parent and clean_parent != clean_settlement_name:
        search_queries.append(f"{clean_settlement_name}, {clean_parent}, Serbia")
    search_queries.append(f"{clean_settlement_name}, Serbia")

    for query in search_queries:
        try:
            location = _geocode_with_backoff(query, geometry='geojson', timeout=5, country_codes='rs', addressdetails=True)
            if location and location.raw.get('geojson') and _validate_geocode_result(location, parent_region):
                geojson_geom = location.raw['geojson']
                hexes = geojson_to_h3_cells(geojson_geom, resolution)
                if hexes:
                    cursor.execute(
                        "INSERT OR REPLACE INTO settlement_polygon_cache (name, hex_ids_json) VALUES (?, ?)",
                        (cache_key, json.dumps(list(hexes)))
                    )
                    conn.commit()
                    conn.close()
                    return hexes, 'polygon'
        except Exception:
            continue

    conn.close()
    center_hex, radius_steps = get_settlement_info(settlement_name, parent_region, resolution)
    if center_hex:
        # No real polygon shape available — fall back to a small disk around the center point.
        # radius_steps == 25 is the floor value get_settlement_info writes when Nominatim
        # returned only a point with no measurable bounding box, i.e. "extent unknown" — most
        # city kvartovi (Мала Америка, Берберско, Градулица). Treat that as a tight ~500m disk
        # (k=2 at res9), NOT the old k=3 ≈ 1.1km which pulled ~100 unrelated streets into the
        # map for a kvart only a few blocks across. A genuinely larger place that still lacks a
        # polygon (radius_steps measured above the floor) scales up modestly, capped well under
        # its full extent since this is a "better than nothing" marker, not a real boundary.
        steps = radius_steps or 25
        disk_radius = 1 if steps <= 25 else max(2, min(6, steps // 12))
        return set(h3.grid_disk(center_hex, disk_radius)), 'disk'
    return set(), None


def get_connecting_streets(street_segments: list[list], parent_region: str = None,
                           resolution: int = 9, max_streets: int = 20,
                           clip_cells: set = None, exclude_name: str = None) -> list[dict]:
    """Streets that physically connect to the given street's geometry — the literal reading of
    "и суседним улицама" / "и околне улице". RGZ splits every street into a new fragment at each
    cross street, so a connecting street shares an endpoint coordinate with the named one; a
    ~60m tolerance (one res11 cell plus its ring) absorbs digitization drift at the corner.

    This replaces widening the street's hexes by a geometric ring and re-querying — that pulled
    in every street the fattened corridor grazed (100+ for a long street), when the announcement
    means the handful of side streets hanging off the one that's dug up.

    clip_cells: when given, each connecting street is trimmed to only the part inside those H3
    cells. "и суседним улицама" means the mouths of those side streets, not their full length
    running clear across town — pass the named street's hexes plus one ring here."""
    if not street_segments or not parent_region or not os.path.exists(RGZ_DB_PATH):
        return []

    TOUCH_RES = 11
    anchor_cells = set()
    named_cells = set()
    for seg in street_segments:
        for pt in seg:
            if len(pt) >= 2:
                named_cells.add(h3.latlng_to_cell(pt[1], pt[0], resolution))
                c = h3.latlng_to_cell(pt[1], pt[0], TOUCH_RES)
                anchor_cells.update(h3.grid_disk(c, 1))
    if not anchor_cells:
        return []

    opstina_lat = cyrillic_to_latin(parent_region.strip()).upper()
    exclude_lat = cyrillic_to_latin(clean_street(exclude_name)).upper().strip() if exclude_name else None
    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT name_lat, geometry_json FROM rgz_streets WHERE UPPER(opstina_lat) = ?",
            (opstina_lat,)
        ).fetchall()
    finally:
        conn.close()

    scored = []
    for name, geometry_json in rows:
        if exclude_lat and (name or "").upper().strip() == exclude_lat:
            continue
        segs = _geometry_rows_to_segments([(geometry_json,)])
        touches = False
        self_overlap = 0
        pts = 0
        for seg in segs:
            for pt in seg:
                if len(pt) < 2:
                    continue
                pts += 1
                if h3.latlng_to_cell(pt[1], pt[0], resolution) in named_cells:
                    self_overlap += 1
                if h3.latlng_to_cell(pt[1], pt[0], TOUCH_RES) in anchor_cells:
                    touches = True
        # Skip the named street itself (most of its vertices sit on named_cells).
        if pts and self_overlap / pts > 0.6:
            continue
        if touches:
            scored.append((self_overlap, name, segs))

    # Prefer the streets that actually meet the named one at a shared node (self_overlap > 0)
    # over ones merely near an endpoint, and keep the list short.
    scored.sort(key=lambda t: t[0], reverse=True)
    features = []
    for _, name, segs in scored[:max_streets]:
        if clip_cells:
            lines = _clip_segments_to_cells(segs, clip_cells, resolution)
        else:
            lines = [[list(p) for p in s] for s in segs if len(s) >= 2]
        geometry = _runs_to_geometry(lines)
        if not geometry:
            continue
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {"kind": "street", "street": (name or "").title(), "relation": "surrounding"}
        })
    return features

def _geometry_rows_to_segments(rows) -> list[list]:
    """Shared by query_street_database and query_rgz_streets: decodes each row's geometry_json
    (a GeoJSON dict) into the flat list-of-coordinate-lists shape the rest of this module works
    with."""
    segments = []
    for row in rows:
        try:
            geom = json.loads(row[0])
            if geom.get('type') == 'LineString':
                segments.append(geom.get('coordinates', []))
            elif geom.get('type') == 'MultiLineString':
                segments.extend(geom.get('coordinates', []))
            elif geom.get('type') == 'Point':
                # A single point can't form a line, but it can still contribute a hex for
                # matching purposes — represent it as a degenerate 1-point "segment" rather
                # than silently dropping it. LineString-only consumers (stitch_street_segments,
                # get_street_geojson_feature) already filter segments with len < 2.
                coord = geom.get('coordinates')
                if coord:
                    segments.append([coord])
        except Exception:
            continue

    return segments


# Ordinal-word stems (as produced by get_smart_stem on the Latin-transliterated word) -> digit.
# Covers the small set of ordinals actually seen in Serbian numbered/commemorative street names
# ("Први/Друга/... Виногради", "Осмог Марта", "Седмог Јула").
_ORDINAL_STEM_TO_DIGIT = {
    'PRV': '1', 'DRUG': '2', 'TREĆ': '3', 'ČETVRT': '4', 'PET': '5',
    'ŠEST': '6', 'SEDM': '7', 'OSM': '8', 'DEVET': '9', 'DESET': '10',
}

# A street "name" that is nothing but a street-type classifier - a bare "Trg", "Bulevar",
# "Ulica". It must never drive a "%LIKE%" geometry lookup or a containment street match:
# "%TRG%" unions every square in a town (Trg Carice Milice, Trg Dositeja Obradovića, Trg
# Mladenaca, ...) and scatters that outage's hexes across the whole map. Kept as UPPER Latin,
# the form every caller normalises to before comparing.
_GENERIC_STREET_TOKENS = frozenset({
    "TRG", "ULICA", "UL", "BULEVAR", "BUL", "PUT", "NASELJE", "NAS",
    "KEJ", "ŠETALIŠTE", "SETALISTE", "PROLAZ", "PLATO",
    "SQUARE", "STREET", "ROAD", "BOULEVARD",
})


def is_generic_street_token(name: str | None) -> bool:
    """True when `name`, once cleaned and transliterated, is a single street-type classifier
    word ("Trg", "Bulevar") with nothing to identify which one. A real multi-word name that
    merely starts with such a word ("Trg Marije Trandafil") is not generic and returns False."""
    if not name:
        return False
    n = cyrillic_to_latin(clean_street(name)).upper().strip()
    n = re.sub(r"[.\-,]", "", n).strip()
    return n in _GENERIC_STREET_TOKENS


# Honorific / title words a street name carries in one text and drops in another. EPS wrote
# "Хероја Пинкија" for Bukovac's street that RGZ (and the street sign) calls "Пинкијева".
_STREET_TITLE_WORDS = frozenset({
    "HEROJA", "NARODNOG", "KRALJA", "KRALJICE", "KNEZA", "KNEGINJE", "CARA", "CARICE", "VOJVODE",
    "DR", "DOKTORA", "PROFESORA", "GENERALA", "PUKOVNIKA", "MAJORA", "KAPETANA", "POPA", "PROTE",
})
_PALATAL_ENDINGS = ("J", "Ć", "Č", "Đ", "Š", "Ž", "LJ", "NJ", "DŽ", "C")


def street_name_variants(street_name: str) -> list[str]:
    """Another name the same street is commonly given, in Latin caps: the possessive form of the
    one name left after dropping title words - "ХЕРОЈА ПИНКИЈА" -> ["PINKIJEVA"], "KRALJA MARKA"
    -> ["MARKOVA"]. Empty when no title word is dropped, or more than one word remains (a first
    name + surname has no single possessive to guess).

    A variant is a guess about which street was meant: look it up by exact name only
    (query_rgz_streets exact_only - "MARKOVA" must never widen to "Svetozara Markovića"), and hold
    it to the rest of the announcement (core/matchmaker.py accepts one only next to streets that
    did resolve)."""
    words = [w for w in re.split(r"[\s.]+", cyrillic_to_latin(clean_street(street_name)).upper()) if w]
    rest = [w for w in words if w not in _STREET_TITLE_WORDS]
    if len(rest) != 1 or len(rest) == len(words) or len(rest[0]) < 4 or not rest[0].endswith("A"):
        return []
    stem = rest[0][:-1]  # genitive "PINKIJA" / "MARKA"
    return [stem + ("EVA" if stem.endswith(_PALATAL_ENDINGS) else "OVA")]


def _street_name_like_patterns(cleaned_name: str) -> list[str]:
    """Builds an ordered list of SQL LIKE patterns (matched against name_lat) to try for a street
    name that didn't hit an exact match, most-specific first.

    Stemming each WORD independently — rather than the whole multi-word string as one blob —
    means a case-ending mismatch on *any* word gets bridged, not just the last one ("Генерал
    Кутузова" vs RGZ's "Генерала Кутузова"), and a stray punctuation character between words
    (RGZ's "22. Августа" vs text's "22 Августа") no longer breaks a literal-substring match the
    way stemming the whole string as one unit did. Trying the words in reversed order too bridges
    a personal name's given-name/surname order flip ("Јураја Арнолда" vs RGZ's "Арнолд Јураја")
    and a numbered street's digit-before/after-noun convention ("5 Виногради" vs RGZ's "Виногради
    5") — both are really the same "same words, different order" problem. Resolving a leading
    ordinal word to its digit form ("Осмог" -> "8") handles the one case per-word stemming can't:
    a different lexical form, not just a different case ending or word order.

    A leading initial ("Б.Јовановић") is NOT dropped as noise — it's the one piece of
    information that disambiguates which of several same-surname streets is meant (confirmed
    real case: Pančevo alone has Змај Јове, Браће, Стевице and Паје Јовановића — an initial-
    stripped "%JOVANOVIĆ%" pattern matches all four and silently unions their geometry). Patterns
    that keep the initial as a required first-letter anchor ("B%JOVANOVIĆ%") are tried first, so
    the ambiguous case above resolves to Браће uniquely; the initial-stripped patterns remain as
    a fallback for callers where the initial doesn't actually correspond to the true first word.
    """
    lat_full = cyrillic_to_latin(cleaned_name).upper().strip()
    initial_match = re.match(r'^([A-ZĐĆČŽŠ])\.\s*', lat_full)
    initial_letter = initial_match.group(1) if initial_match else None
    no_initial = re.sub(r'^([A-ZĐĆČŽŠ]\.\s*)+', '', lat_full).strip()
    words = no_initial.split()
    if not words:
        return []

    # A bare classifier ("TRG", "BULEVAR") must not expand to "%TRG%" - that unions every
    # square/boulevard in the naselje. The exact-name lookups the caller runs before this still
    # resolve a street genuinely named just "Trg"; only the wildcard blow-up is suppressed.
    if len(words) == 1 and words[0] in _GENERIC_STREET_TOKENS:
        return []

    stems = [get_smart_stem(w) for w in words]
    word_lists = [stems]

    first_digit = _ORDINAL_STEM_TO_DIGIT.get(stems[0])
    if first_digit and len(stems) > 1:
        word_lists.append([first_digit] + stems[1:])

    anchored, unanchored = [], []
    for words_variant in word_lists:
        if len(words_variant) == 1:
            unanchored.append(f"%{words_variant[0]}%")
            if initial_letter:
                anchored.append(f"{initial_letter}%{words_variant[0]}%")
        else:
            unanchored.append("%" + "%".join(words_variant) + "%")
            unanchored.append("%" + "%".join(reversed(words_variant)) + "%")
            if initial_letter:
                anchored.append(f"{initial_letter}%" + "%".join(words_variant) + "%")

    seen = set()
    ordered = []
    for p in anchored + unanchored:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def _name_words(name: str) -> list[str]:
    return [w for w in re.split(r"[\s.,/\-–]+", cyrillic_to_latin(name).upper()) if w]


def _same_word(a: str, b: str, short: bool = False) -> bool:
    """One name word in two spellings: a case ending ('GENERAL' / 'GENERALA', 'NUŠIĆEVA' /
    'NUŠIĆA') or a Hungarian name's added vowel ('PETEFI' / 'PETEFIJA', 'JO' / 'JOA'). A stem
    under four letters counts only beside another matching name word (`short`: 'MATE JERKOVIĆA' /
    'MATA JERKOVIĆA'); alone it would let get_smart_stem("НОВА") = "N" match anything."""
    if a == b:
        return True
    if a.isdigit() or b.isdigit():
        return False
    sa, sb = get_smart_stem(a), get_smart_stem(b)
    if sa == sb and min(len(sa), len(sb)) >= (3 if short else 4):
        return True
    lo, hi = sorted((a, b), key=len)
    rest = hi[len(lo):]
    return (len(lo) >= (2 if short else 3) and hi.startswith(lo) and len(rest) <= 2
            and set(rest) <= set("AEIOUJ"))


def _is_ordinal_word(word: str) -> bool:
    return not word.isdigit() and get_smart_stem(word) in _ORDINAL_STEM_TO_DIGIT


def like_hit_fits(query: str, hit: str, loose: bool = False) -> bool:
    """True when `hit`, a street a LIKE pattern from _street_name_like_patterns found, really is
    the street `query` names. A LIKE pattern only says the stems occur somewhere in the name, and
    a stem can be a single letter: get_smart_stem("НОВА") is "N", so outage #4264's street "Нова"
    became '%N%' and drew 515 Novi Sad streets on the map (10.10.2026).

    - The hit carries the query's words side by side, in either order (Hungarian names flip:
      'PETEFI ŠANDORA' / 'ŠANDORA PETEFIJA'), each the same word by _same_word. A leading ordinal
      word may stand as its digit ('OSMOG MARTA' / '8. MARTA').
    - A single letter at either end ('П ДРАПШИНА', 'КОСАНЧИЋ И') or before a dot is an initial:
      the first letter of a word of three letters or more ('PETRA I' is not 'PETRA II').
    - A title word may be missing from the query ('DR KASAPINOVIĆA'), and a lone middle initial
      from the hit ('BRANKA V. RADIČEVIĆA').
    - With one name word the hit may gain only street-type words ('OSLOBOĐENJA' -> 'BULEVAR
      OSLOBOĐENJA'), never a number ('NOVA' is not 'NOVA 8'). With `loose` it may also gain the
      words before a surname ('NUŠIĆEVA' -> 'BRANISLAVA NUŠIĆA'); _rows_that_fit takes a loose
      fit only when the lookup found no other street.
    - A date street ('7. ЈУЛИ', 'ПЕТОГ ПУКА') may use short stems and gain a year ('15. AVGUSTA
      1942.').
    - A query of only numbers, ordinals, initials or street-type words fits nothing (the exact
      lookups have already run)."""
    found = list(re.finditer(r"([^\s.,/\-–]+)(\.)?", clean_street(query).upper().strip()))
    tokens = []
    for i, m in enumerate(found):
        w = cyrillic_to_latin(m.group(1))
        # A Latin I, V or X among Cyrillic words is a Roman numeral ('I ВИНОГРАДИ'), not an initial.
        roman = re.fullmatch(r"[IVX]+", m.group(1)) is not None
        initial = (len(w) == 1 and w.isalpha() and not roman
                   and (bool(m.group(2)) or i in (0, len(found) - 1)))
        tokens.append((w, initial))
    named = [t for t in tokens if t[1] or not (t[0].isdigit() or _is_ordinal_word(t[0])
                                                or re.fullmatch(r"[IVX]+", t[0])
                                                or t[0] in _GENERIC_STREET_TOKENS)]
    if not any(not initial for _, initial in named):
        return False
    variants = [tokens]
    untitled = [t for t in tokens if t[0] not in _STREET_TITLE_WORDS]
    if len(untitled) < len(tokens):
        variants.append(untitled)
    digit = _ORDINAL_STEM_TO_DIGIT.get(get_smart_stem(tokens[0][0]))
    if digit and len(tokens) > 1 and not tokens[0][1]:
        variants.append([(digit, False)] + tokens[1:])
    variants += [v[::-1] for v in variants]

    # A date street ('7. ЈУЛИ', 'ПЕТОГ ПУКА') may use short stems ('JUL', 'PET') and gain a year
    # ('15. AVGUSTA 1942.').
    dated = any(w.isdigit() or _is_ordinal_word(w) for w, _ in tokens)
    words = _name_words(hit)
    hits = [words]
    if len(words) > 2 and any(len(w) == 1 for w in words[1:-1]):
        hits.append([words[0]] + [w for w in words[1:-1] if len(w) > 1] + [words[-1]])
    for v in variants:
        names = [t for t in v if t in named]
        if not any(not initial for _, initial in names):
            continue
        single = len(names) == 1
        short = not single or loose or dated
        for hit_words in hits:
            for i in range(len(hit_words) - len(v) + 1):
                span = hit_words[i:i + len(v)]
                if not all((h == w or (len(h) >= 3 and h[0] == w)) if initial else _same_word(w, h, short)
                           for (w, initial), h in zip(v, span)):
                    continue
                before, after = hit_words[:i], hit_words[i + len(v):]
                if single:
                    if any(w.isdigit() and not (dated and len(w) == 4) for w in before + after):
                        continue
                    gained = [w for w in before + after if not w.isdigit() and w not in _GENERIC_STREET_TOKENS]
                    if gained and not (loose and not [w for w in after if w not in _GENERIC_STREET_TOKENS]):
                        continue
                return True
    return False


def _rows_that_fit(street_name: str, rows) -> list[tuple]:
    """The LIKE-found rows (each ending in its street name) that really are `street_name`, with
    the name dropped. A loose fit (a lone surname or possessive gaining a first name) is taken
    only when the pattern found no other street: 'ПОПОВИЋА' next to both Raje Popovića and Save
    Popovića Gembeša names neither."""
    fit = [r[:-1] for r in rows if like_hit_fits(street_name, r[-1])]
    if fit or len({r[-1] for r in rows}) != 1:
        return fit
    return [r[:-1] for r in rows if like_hit_fits(street_name, r[-1], loose=True)]


def rgz_street_naselja(street_name: str, region: str) -> dict[str, str] | None:
    """{naselje_lat: naselje} for every RGZ naselje of `region`'s opštine that has this street:
    by its exact name, else by the first LIKE pattern with a hit like_hit_fits accepts (strict
    only - a loose guess must not decide where a street is). None when RGZ can't answer for the
    region at all."""
    cleaned = clean_street(street_name)
    allowed = region_opstine(region)
    if not cleaned or not allowed or not os.path.exists(RGZ_DB_PATH):
        return None
    cyr, lat = cleaned.upper().strip(), cyrillic_to_latin(cleaned).upper().strip()
    ops = tuple(sorted(allowed))
    scope = f"opstina_lat IN ({','.join('?' * len(ops))})"
    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT naselje_lat, naselje FROM rgz_streets "
            f"WHERE (name IN (?, ?) OR name_lat IN (?, ?)) AND {scope}", (cyr, lat, cyr, lat) + ops
        ).fetchall()
        if not rows:
            for pattern in _street_name_like_patterns(cleaned):
                hits = conn.execute(
                    f"SELECT DISTINCT naselje_lat, naselje, name_lat FROM rgz_streets "
                    f"WHERE name_lat LIKE ? AND {scope}", (pattern,) + ops
                ).fetchall()
                rows = [h[:2] for h in hits if like_hit_fits(cleaned, h[2])]
                if rows:
                    break
        return dict(rows)
    finally:
        conn.close()


def query_rgz_streets(street_name: str, naselje: str = None, opstina: str = None,
                      exact_only: bool = False) -> list[list]:
    """The RGZ street's geometry as segments - see _rgz_street_rows for how it is found."""
    return _geometry_rows_to_segments(_rgz_street_rows(street_name, naselje, opstina, exact_only))


def rgz_house_points(street_name: str, settlement: str = None, parent_region: str = None) -> list[tuple]:
    """(kucni_broj, lat, lon) of every active RGZ house number on the street that
    get_local_street_segments draws for the same arguments. Empty when RGZ doesn't carry that
    street (an OSM-drawn street has no house numbers to place)."""
    ids = sorted({r[1] for r in _rgz_street_rows(street_name, naselje=settlement or parent_region,
                                                 opstina=parent_region)})
    if not ids:
        return []
    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        return conn.execute(
            f"SELECT kucni_broj, lat, lon FROM rgz_housenumbers WHERE is_active = 1 "
            f"AND ulica_maticni_broj IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
    finally:
        conn.close()


def _rgz_street_rows(street_name: str, naselje: str = None, opstina: str = None,
                     exact_only: bool = False) -> list[tuple]:
    """(geometry_json, ulica_maticni_broj) rows of the RGZ street.

    RGZ-sourced equivalent of query_street_database(), scoped directly by naselje instead of
    relying on the H3-grid-distance guessing get_local_street_segments does for OSM data — RGZ
    rows already know which settlement they belong to, so an exact naselje match here is
    authoritative rather than a proximity heuristic. Deliberately does NOT broaden to the whole
    opština on a naselje miss (see the note below).

    `opstina` scopes every lookup to the RGZ opštine it stands for (region_opstine). A naselje
    name alone is NOT unique nationwide - confirmed real leak, outage #2365: a Smederevo
    announcement for "Вучак" matched RGZ's Vučak under Kruševac too (two Vučaks), and Vučačka
    street there - 125 km away - got hexes and would have been notified. When `opstina` names a
    region RGZ doesn't carry at all, no RGZ row can be vouched for: return nothing and let the
    OSM path (which scopes by the region's own position) decide.

    exact_only skips the LIKE and fuzzy passes - for a caller whose name is already a guess
    (street_name_variants)."""
    cleaned = clean_street(street_name)
    if not cleaned or not os.path.exists(RGZ_DB_PATH):
        return []

    allowed_opstine = None
    if opstina:
        allowed_opstine = region_opstine(opstina)
        if not allowed_opstine:
            if is_serbian_opstina(opstina):
                # A real opština RGZ doesn't carry: any RGZ hit is some other opština's naselje.
                return []
            # Not an opština name at all - a registered address whose "city" is its village
            # (Čantavir, Bukovac): usable only if RGZ knows exactly one naselje by that name.
            owners = naselje_owner_opstine(naselje or opstina)
            if len(owners) != 1:
                return []
            allowed_opstine = owners
    opstina_clause = ""
    opstina_args: tuple = ()
    if allowed_opstine:
        opstina_clause = f" AND opstina_lat IN ({','.join('?' * len(allowed_opstine))})"
        opstina_args = tuple(sorted(allowed_opstine))

    cyr_name = cleaned.upper().strip()
    lat_name = cyrillic_to_latin(cleaned).upper().strip()
    naselje_val = _dealias_naselje(cyrillic_to_latin(clean_settlement(naselje)).upper().strip()) if naselje else ""
    # EPS names a gradska opština "Палилула - Ниш" / "Палилула - Београд"; the sub-opština retry
    # below keys on RGZ's "PALILULA (NIŠ)" form. Without this, every street of either Palilula
    # given that way came back empty.
    naselje_val = re.sub(r"^(.+?)\s+-\s+(NIŠ|BEOGRAD)$", r"\1 (\2)", naselje_val)

    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    cursor = conn.cursor()

    def _query_scope(scope_col: str, scope_val: str):
        cursor.execute(
            f"SELECT geometry_json, ulica_maticni_broj FROM rgz_streets WHERE (name IN (?, ?) OR name_lat IN (?, ?)) "
            f"AND {scope_col} = ?{opstina_clause}",
            (cyr_name, lat_name, cyr_name, lat_name, scope_val) + opstina_args
        )
        rows = cursor.fetchall()
        if rows or exact_only:
            return rows

        # See _street_name_like_patterns() for what each candidate bridges (punctuation, word
        # order, ordinal-vs-digit, case endings on any word). Tried in order, first hit wins.
        # Each pattern only narrows the candidates; _rows_that_fit decides which really are this
        # street.
        for pattern in _street_name_like_patterns(cleaned):
            cursor.execute(
                f"SELECT geometry_json, ulica_maticni_broj, name_lat FROM rgz_streets WHERE name_lat LIKE ? "
                f"AND {scope_col} = ?{opstina_clause}",
                (pattern, scope_val) + opstina_args
            )
            rows = _rows_that_fit(cleaned, cursor.fetchall())
            if rows:
                return rows

        # Last resort: a genuine single-letter typo/transcription slip rather than a grammatical
        # or word-order mismatch (confirmed real case: source text "Душана Крнајског" vs RGZ's
        # "Душана Крњајског" — one letter, Н vs Њ, not a case ending or reordering). Fuzzy-match
        # against ONLY this naselje's own street names — never opština- or nationwide, same
        # cross-town-contamination guard as the exact/LIKE lookups above — and accept a match
        # only if it's a clear, close best match (not merely the closest of a mediocre bunch).
        cursor.execute(
            f"SELECT DISTINCT name_lat FROM rgz_streets WHERE {scope_col} = ?{opstina_clause}",
            (scope_val,) + opstina_args
        )
        candidates = [r[0] for r in cursor.fetchall()]
        close = difflib.get_close_matches(lat_name, candidates, n=1, cutoff=0.85)
        if close:
            cursor.execute(
                f"SELECT geometry_json, ulica_maticni_broj FROM rgz_streets WHERE name_lat = ? AND {scope_col} = ?"
                f"{opstina_clause}",
                (close[0], scope_val) + opstina_args
            )
            rows = cursor.fetchall()
            if rows:
                return rows
        return []

    # Deliberately naselje-scoped only, with no opština-wide broadening: a street name is very
    # commonly reused by multiple naselja under the same opština (confirmed case: "Буњевачка"
    # exists in RGZ only under Bajmok, not Pačir — an outage naming Pačir was silently resolving
    # to Bajmok's street, many km away, when this used to broaden to opstina_lat on a miss). No
    # naselje match here means "let this fall through to OSM+H3-distance" (query_street_database,
    # called by get_local_street_segments right after this), which has its own real-world
    # distance cutoff instead of a blind opština-wide name match.
    rows = _query_scope("naselje_lat", naselje_val) if naselje_val else []

    # Belgrade's gradske opštine (Zvezdara, Palilula, Vračar, ...) are a real exception to "a
    # naselje is its own scope": RGZ stores their naselje_lat as "BEOGRAD (<opština>)", not the
    # bare opština name — confirmed via rgz.db (Димитрија Туцовића sits under naselje_lat
    # 'BEOGRAD (ZVEZDARA)', not 'ZVEZDARA'). An outage announcement naming just the opština
    # ("у општини Звездара", BVK's own phrasing) has no way to know that, so retry with the
    # Belgrade-wrapped form before giving up and falling through to the coarser OSM/H3 path.
    if not rows and naselje_val and not naselje_val.startswith("BEOGRAD ("):
        rows = _query_scope("naselje_lat", f"BEOGRAD ({naselje_val})")

    # Caller gave the specific gradska opština directly (e.g. EPS's own "Opština: Crveni krst"
    # column - it never says "Niš"), not the umbrella city name the retry above and the sweep
    # below both expect. Try that one compound naselje directly before the broader sweep -
    # cheaper, and avoids ever falling through to OSM/Nominatim for a name this precise.
    if not rows and naselje_val in _SUBOPSTINA_TO_GRAD:
        grad = _SUBOPSTINA_TO_GRAD[naselje_val]
        sub = naselje_val.replace(f" ({grad})", "")  # "PALILULA (NIŠ)" -> "NIŠ (PALILULA)"
        rows = _query_scope("naselje_lat", f"{grad} ({sub})")

    # A street named with only a multi-opština city (Ниш: streets sit under "NIŠ (MEDIJANA)",
    # "NIŠ (PALILULA)", ...) - try that city's own street naselje in each of its gradske
    # opštine. Scoped per-opština, so still nowhere near a nationwide name match.
    if not rows:
        for grad, opstine in _GRAD_OPSTINE.items():
            if naselje_val != grad and (opstina or "").upper() != grad:
                continue
            for op in opstine:
                rows = _query_scope("naselje_lat", f"{grad} ({op.replace(f' ({grad})', '')})")
                if rows:
                    break
            if rows:
                break

    conn.close()
    return rows


def query_street_database(street_name: str) -> list[list]:
    cleaned = clean_street(street_name)
    if not cleaned or not os.path.exists(SERBIA_DB_PATH):
        return []

    cyr_name = cleaned.upper().strip()
    lat_name = cyrillic_to_latin(cleaned).upper().strip()

    conn = sqlite3.connect(f"file:{SERBIA_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    cursor = conn.cursor()

    cursor.execute("SELECT geometry_json FROM streets WHERE clean_name IN (?, ?)", (cyr_name, lat_name))
    rows = cursor.fetchall()

    if not rows:
        initial_cyr_match = re.match(r'^([А-ЯЂЈЉЊЋЏ])\.\s*', cyr_name)
        initial_lat_match = re.match(r'^([A-ZĐĆČŽŠ])\.\s*', lat_name)
        no_initial_cyr = re.sub(r'^([А-ЯЂЈЉЊЋЏ]\.\s*)+', '', cyr_name).strip()
        no_initial_lat = re.sub(r'^([A-ZĐĆČŽŠ]\.\s*)+', '', lat_name).strip()
        stem_cyr = get_smart_stem(no_initial_cyr)
        stem_lat = get_smart_stem(no_initial_lat)

        if len(stem_lat) >= 3:
            # A leading initial ("Б.Јовановић") is the one thing that disambiguates which of
            # several same-surname streets is meant (real case: Pančevo alone has Змај Јове,
            # Браће, Стевице and Паје Јовановића — an initial-stripped "%JOVANOVIĆ%" pattern
            # matches all four and silently unions their geometry). Try anchoring on the initial
            # as the required first letter before falling back to the initial-stripped pattern.
            # As in the RGZ lookup, a LIKE hit counts only if _rows_that_fit says it is this street.
            if initial_cyr_match or initial_lat_match:
                cursor.execute(
                    "SELECT geometry_json, clean_name FROM streets WHERE clean_name LIKE ? OR clean_name LIKE ?",
                    (
                        f"{initial_cyr_match.group(1)}%{stem_cyr}%" if initial_cyr_match else f"%{stem_cyr}%",
                        f"{initial_lat_match.group(1)}%{stem_lat}%" if initial_lat_match else f"%{stem_lat}%",
                    )
                )
                rows = _rows_that_fit(cleaned, cursor.fetchall())

            if not rows:
                cursor.execute(
                    "SELECT geometry_json, clean_name FROM streets WHERE clean_name LIKE ? OR clean_name LIKE ?",
                    (f"%{stem_cyr}%", f"%{stem_lat}%")
                )
                rows = _rows_that_fit(cleaned, cursor.fetchall())

    conn.close()
    return _geometry_rows_to_segments(rows)

def _naselje_has_rgz_coverage(naselje: str) -> bool:
    """True when RGZ carries ANY streets at all under this exact naselje - lets a genuine 'RGZ
    has never covered this settlement, fall back to OSM+distance' case be told apart from 'RGZ
    covers this settlement but has no row for THIS street', which should mean the street isn't
    there under this name rather than something to guess at via a citywide OSM search.

    Confirmed real case (outage #1866, Bukovac/Novi Sad): EPS's own announcement named a Bukovac
    street "Хероја Пинкија"; RGZ's Bukovac entry for that same street is "Пинкијева" (a shorter
    label for the same hero), so the exact/LIKE/fuzzy match in query_rgz_streets all missed under
    naselje=BUKOVAC and fell through to OSM. OSM's nearest "Heroja Pinkija" by distance was a
    real, unrelated street in central Novi Sad ~7km away - well inside Bukovac's own settlement
    radius, so it passed the proximity cutoff and was reported EXACT, falsely notifying real
    Liman-area registrants for a Bukovac outage they had nothing to do with. Every OTHER street
    in that same announcement resolved correctly through RGZ - this only guards the specific
    gap where RGZ demonstrably knows the settlement but not this one street's RGZ-side name."""
    if not naselje or not os.path.exists(RGZ_DB_PATH):
        return False
    naselje_lat = _dealias_naselje(cyrillic_to_latin(clean_settlement(naselje)).upper().strip())
    if not naselje_lat:
        return False
    conn = sqlite3.connect(f"file:{RGZ_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    try:
        return conn.execute(
            "SELECT 1 FROM rgz_streets WHERE naselje_lat = ? LIMIT 1", (naselje_lat,)
        ).fetchone() is not None
    finally:
        conn.close()


def get_local_street_segments(street_name: str, settlement: str = None, parent_region: str = None, resolution: int = 9) -> list[list]:
    # RGZ-first: rows are already scoped to the right naselje/opština, so a hit here needs none
    # of the H3-grid-distance filtering below (that's purely a "guess which OSM way belongs to
    # this settlement" workaround). Falls through to OSM+H3 only where RGZ has no coverage.
    rgz_segments = query_rgz_streets(street_name, naselje=settlement or parent_region, opstina=parent_region)
    if rgz_segments:
        return rgz_segments

    # Only guard on a settlement the announcement actually NAMED, distinct from its own opština/
    # city - not the silent "settlement or parent_region" default every other caller in this
    # file falls back to when the text names no settlement at all. Confirmed real regression
    # while testing this fix: an outage giving only "Opština: Пирот / Ulice: Рагодеш" (Рагодеш is
    # a real village near Pirot, not a street in Pirot town) has settlement falling back to
    # "ПИРОТ" itself - which DOES have RGZ street coverage (it's the city), so the guard below
    # would have wrongly blocked the OSM fallback that correctly finds the genuinely separate
    # village of Рагодеш today. A real, distinct settlement name (like "БУКОВАЦ" for a Novi Sad
    # outage) never collides with this - it's never equal to its own parent_region.
    if settlement and cyrillic_to_latin(settlement).upper().strip() != cyrillic_to_latin(parent_region or "").upper().strip():
        if _naselje_has_rgz_coverage(settlement):
            # RGZ actively covers this settlement (other streets resolve under it) but has no
            # row for this specific street name - see _naselje_has_rgz_coverage for the confirmed
            # real case this guards. Reporting this one location unresolved is safer than a
            # citywide OSM guess that can land on a same-named street in another neighbourhood.
            return []

    return _get_osm_local_segments(street_name, settlement, parent_region, resolution)


# H3 res9 grid steps (~0.32 km each) past which an OSM street match is treated as a different
# place, not this settlement's own outskirts. ~18 km - comfortably covers any Serbian city's
# real street grid, well under the gap to the nearest other city. See the note on `cutoff`.
_MAX_STREET_MATCH_STEPS = 55


def _get_osm_local_segments(street_name: str, settlement: str = None, parent_region: str = None, resolution: int = 9) -> list[list]:
    """OSM+H3-distance path, factored out of get_local_street_segments() so
    clip_polyline_by_intersections() can retry against OSM's geometry specifically when an
    RGZ-sourced clip degenerates (RGZ registering a shorter stretch of a street than actually
    exists is a real, confirmed case — see the note in clip_polyline_by_intersections)."""
    all_segments = query_street_database(street_name)
    if not all_segments:
        return []

    target_settlement = settlement or parent_region
    if not target_settlement:
        return all_segments

    settlement_hex, radius_steps = get_settlement_info(target_settlement, parent_region, resolution)

    # HARD GUARD: Never leak nationwide streets when a specific city/village was requested
    if not settlement_hex:
        print(f"⚠️ Could not resolve settlement hex for '{target_settlement}'. Blocking nationwide fallback to prevent leakage.")
        return []

    # Use the settlement's own computed radius (village ≈25 steps ≈8km real distance, a city
    # like Subotica ≈104 steps ≈35km — empirically calibrated at H3 res9 in this region) instead
    # of a flat constant. A flat cutoff sized for a village silently drops a bigger city's own
    # legitimate outskirts streets, while being needlessly generous for a small village.
    #
    # But cap it: a grad's Nominatim extent is mostly rural (Grad Sremska Mitrovica reaches
    # ~30km toward Kuzmin; Subotica's ~104 steps is fields, not streets). Left uncapped, that
    # let a same-named street in the NEXT city over satisfy the match as if it were local -
    # confirmed: "Житни трг" in Novi Sad resolved for a Sremska Mitrovica EPS outage (#905) and
    # dragged the whole outage ~30km north, reported EXACT. No real city's street grid runs past
    # ~18km from its centre, so bound the proximity cutoff there however large the settlement
    # geocoded. (RGZ-covered opštine never reach this path - it's the OSM-only fallback.)
    cutoff = min(radius_steps or 25, _MAX_STREET_MATCH_STEPS)

    segment_distances = []
    for seg in all_segments:
        min_d = float('inf')
        for pt in seg:
            if len(pt) >= 2:
                try:
                    h = h3.latlng_to_cell(pt[1], pt[0], resolution)
                    d = h3.grid_distance(h, settlement_hex)
                    if d < min_d:
                        min_d = d
                except Exception:
                    continue
        # HARD STOP: discard any candidate outside this settlement's own real-world extent
        if min_d <= cutoff:
            segment_distances.append((min_d, seg))

    if not segment_distances:
        return []  # Returns empty list instead of bleeding into Belgrade/Zmajevo

    segment_distances.sort(key=lambda x: x[0])
    closest_dist, anchor_segment = segment_distances[0]

    anchor_hexes = set()
    for pt in anchor_segment:
        if len(pt) >= 2:
            try:
                anchor_hexes.add(h3.latlng_to_cell(pt[1], pt[0], resolution))
            except Exception:
                continue

    local_segments = []
    for d, seg in segment_distances:
        if d - closest_dist > 5:
            continue

        is_connected = False
        for pt in seg:
            if len(pt) >= 2:
                try:
                    h = h3.latlng_to_cell(pt[1], pt[0], resolution)
                    for ah in anchor_hexes:
                        if h3.grid_distance(h, ah) <= 3:
                            is_connected = True
                            break
                    if is_connected:
                        break
                except Exception:
                    continue

        if is_connected:
            local_segments.append(seg)

    return local_segments

def stitch_street_segments(segments: list[list]) -> LineString | None:
    """Safely merges street segments into a single LineString using Shapely's topology engine."""
    if not segments:
        return None
    
    valid_lines = [LineString(seg) for seg in segments if isinstance(seg, (list, tuple)) and len(seg) >= 2]
    if not valid_lines:
        return None
    if len(valid_lines) == 1:
        return valid_lines[0]

    try:
        merged = linemerge(valid_lines)
        if merged.geom_type == 'LineString':
            return merged
        elif merged.geom_type == 'MultiLineString':
            return max(merged.geoms, key=lambda l: l.length)
    except Exception:
        pass

    return valid_lines[0]

def get_intersection_point(street_a: str, street_b: str, settlement_hex: str = None) -> Point | None:
    if not street_a or not street_b or not os.path.exists(SERBIA_DB_PATH):
        return None

    clean_a = clean_street(_strip_junction_prefix(street_a))
    clean_b = clean_street(_strip_junction_prefix(street_b))

    cyr_a, lat_a = clean_a.upper().strip(), cyrillic_to_latin(clean_a).upper().strip()
    cyr_b, lat_b = clean_b.upper().strip(), cyrillic_to_latin(clean_b).upper().strip()

    # `intersections` (built by build_intersections.py from OSM data) stores street names in
    # Cyrillic only — a LIKE pattern built from the Latin transliteration can never match it
    # (Cyrillic 'Ј' and Latin 'J' are different code points), so stripping/anchoring initials
    # must be done against the CYRILLIC form to actually have any effect. Latin patterns are kept
    # too, purely defensively, in case the table's format ever changes.
    initial_a_cyr = re.match(r'^([А-ЯЂЈЉЊЋЏ])\.\s*', cyr_a)
    initial_b_cyr = re.match(r'^([А-ЯЂЈЉЊЋЏ])\.\s*', cyr_b)
    initial_a_lat = re.match(r'^([A-ZĐĆČŽŠ])\.\s*', lat_a)
    initial_b_lat = re.match(r'^([A-ZĐĆČŽŠ])\.\s*', lat_b)
    no_init_a_cyr = re.sub(r'^([А-ЯЂЈЉЊЋЏ]\.\s*)+', '', cyr_a).strip()
    no_init_b_cyr = re.sub(r'^([А-ЯЂЈЉЊЋЏ]\.\s*)+', '', cyr_b).strip()
    no_init_a_lat = re.sub(r'^([A-ZĐĆČŽŠ]\.\s*)+', '', lat_a).strip()
    no_init_b_lat = re.sub(r'^([A-ZĐĆČŽŠ]\.\s*)+', '', lat_b).strip()

    conn = sqlite3.connect(f"file:{SERBIA_DB_PATH}?mode=ro", uri=True, timeout=30.0)
    cursor = conn.cursor()

    def _query(patterns_a: list[str], patterns_b: list[str]):
        def _side_clause(column: str, exact_a: str, exact_b: str, patterns: list[str]):
            clause = " OR ".join([f"{column} IN (?, ?)"] + [f"{column} LIKE ?"] * len(patterns))
            return clause, [exact_a, exact_b, *patterns]

        clause_a, params_a = _side_clause("street_a", cyr_a, lat_a, patterns_a)
        clause_b, params_b = _side_clause("street_b", cyr_b, lat_b, patterns_b)
        # Reversed: street_a/street_b swapped (an intersection row can list either street first).
        clause_a2, params_a2 = _side_clause("street_a", cyr_b, lat_b, patterns_b)
        clause_b2, params_b2 = _side_clause("street_b", cyr_a, lat_a, patterns_a)

        cursor.execute(
            f"SELECT lat, lon FROM intersections WHERE (({clause_a}) AND ({clause_b})) OR (({clause_a2}) AND ({clause_b2}))",
            params_a + params_b + params_a2 + params_b2
        )
        return cursor.fetchall()

    def _best_within_settlement(rows):
        # Hard city proximity check (~12km radius from settlement center)
        for lat, lon in rows:
            try:
                cell = h3.latlng_to_cell(lat, lon, 9)
                if h3.grid_distance(cell, settlement_hex) <= 30:
                    return Point(lon, lat)
            except Exception:
                continue
        return None

    # A leading initial ("Б.Јовановић") is the one thing that disambiguates which of several
    # same-surname streets is meant (real case: Pančevo alone has Змај Јове, Браће, Стевице and
    # Паје Јовановића) — an initial-stripped "%ЈОВАНОВИЋ%" pattern can match any of them,
    # including one whose actual crossing with the main street has nothing to do with the real
    # outage. Try anchoring on the initial as a required first letter before falling back to the
    # initial-stripped pattern.
    anchored_a = [f"{initial_a_cyr.group(1)}%{no_init_a_cyr}%" if initial_a_cyr else f"%{no_init_a_cyr}%"]
    anchored_b = [f"{initial_b_cyr.group(1)}%{no_init_b_cyr}%" if initial_b_cyr else f"%{no_init_b_cyr}%"]
    if initial_a_lat:
        anchored_a.append(f"{initial_a_lat.group(1)}%{no_init_a_lat}%")
    if initial_b_lat:
        anchored_b.append(f"{initial_b_lat.group(1)}%{no_init_b_lat}%")

    if initial_a_cyr or initial_b_cyr:
        rows = _query(anchored_a, anchored_b)
        if rows:
            result = _best_within_settlement(rows) if settlement_hex else Point(rows[0][1], rows[0][0])
            if result:
                conn.close()
                return result

    rows = _query([f"%{no_init_a_cyr}%", f"%{no_init_a_lat}%"], [f"%{no_init_b_cyr}%", f"%{no_init_b_lat}%"])

    # Case-ending fallback: cross-streets in source text are frequently genitive ("од Батутове
    # до Чингријине" - "Батутове"/"Чингријине"), while `intersections` stores nominative form
    # ("БАТУТОВА"/"ЧИНГРИЈИНА") - confirmed real case, the exact point exists in the table but a
    # raw substring match never fires since "БАТУТОВЕ" isn't a substring of "БАТУТОВА". Stemming
    # both sides bridges it ("БАТУТ" either way). Requiring BOTH cross-streets to match, plus the
    # settlement-proximity filter below, keeps this from being a loose single-word wildcard risk.
    if not rows:
        stem_a_cyr, stem_a_lat = get_smart_stem(no_init_a_cyr), get_smart_stem(no_init_a_lat)
        stem_b_cyr, stem_b_lat = get_smart_stem(no_init_b_cyr), get_smart_stem(no_init_b_lat)
        if len(stem_a_cyr) >= 3 and len(stem_b_cyr) >= 3:
            rows = _query([f"%{stem_a_cyr}%", f"%{stem_a_lat}%"], [f"%{stem_b_cyr}%", f"%{stem_b_lat}%"])

    conn.close()

    if not rows:
        return None

    if settlement_hex:
        return _best_within_settlement(rows)

    return Point(rows[0][1], rows[0][0])

def _resolve_direction_toward_place(main_line, place_name: str, parent_region: str, total_len: float) -> float | None:
    """If `place_name` resolves as a real settlement (rather than a cross-street), returns the
    along-line distance (0.0 or total_len) of whichever endpoint of `main_line` is geographically
    closer to it — i.e. "clip all the way to the end of the road that heads towards that place."
    Returns None if it doesn't resolve as a settlement either."""
    if not place_name or not place_name.strip():
        return None

    # Cheap, local, zero-network guard before spending a geocode attempt on a guess: Serbian
    # street references are very commonly written as "Х.Презиме" (an initial + period, e.g.
    # "Ј.Безуљевића") — that pattern essentially never occurs in a bare settlement/place name, so
    # skip straight to "not a place" rather than paying for a speculative live lookup (and, under
    # any Nominatim rate-limiting, a full retry-with-backoff cycle) on something this unlikely.
    if re.match(r'^[A-ZĐĆČŽŠ]\.\s*', cyrillic_to_latin(place_name.strip()).upper()):
        return None

    place_hex, _ = get_settlement_info(place_name, parent_region)
    if not place_hex:
        return None

    try:
        place_lat, place_lon = h3.cell_to_latlng(place_hex)
        start_pt, end_pt = main_line.coords[0], main_line.coords[-1]
        start_hex = h3.latlng_to_cell(start_pt[1], start_pt[0], 9)
        end_hex = h3.latlng_to_cell(end_pt[1], end_pt[0], 9)
        dist_to_start = h3.grid_distance(start_hex, place_hex)
        dist_to_end = h3.grid_distance(end_hex, place_hex)
        return 0.0 if dist_to_start <= dist_to_end else total_len
    except Exception:
        return None


# Words an announcement puts in front of the actual cross-street name when giving a vector bound
# ("од раскрснице Светог Саве", "до угла Крунске", "код Дома здравља"). Left on, they stop the
# cross street from being found and the clip collapses to a stub at the other bound - confirmed
# real case: Војводе Степе in Čačak, "од раскрснице Светог Саве" resolved to a 60 m fragment.
_JUNCTION_PREFIX_RE = re.compile(
    r"^\s*(?:од\s+|до\s+)?(?:раскрснице|раскрсница|раскрсници|угла|угао|углу|код|кода)\s+",
    re.IGNORECASE,
)


def _strip_junction_prefix(name: str | None) -> str | None:
    if not name:
        return name
    return _JUNCTION_PREFIX_RE.sub("", name).strip() or name


def clip_polyline_by_intersections(main_segments: list[list], start_street: str, end_street: str, settlement: str, parent_region: str, main_street_name: str = "") -> list[list]:
    start_street = _strip_junction_prefix(start_street)
    end_street = _strip_junction_prefix(end_street)
    if not main_segments or (not start_street and not end_street):
        return main_segments

    is_rgz_main = bool(main_street_name and query_rgz_streets(main_street_name, settlement or parent_region, parent_region))

    result, degenerate = _attempt_clip(main_segments, start_street, end_street, settlement, parent_region, main_street_name, is_rgz_main)

    if degenerate and is_rgz_main:
        # RGZ's own registered entity for this street didn't reach both cross-street bounds
        # cleanly — confirmed case: Стипе Гргића's RGZ geometry stops ~285m short of Прашка
        # (both cross-street bounds ended up projecting to the same endpoint), while OSM's single
        # "way" for the same street actually touches both cross streets with distinct positions.
        # Retry against OSM's geometry for the same street before giving up — same street, a
        # sometimes more complete digitization — rather than reporting unresolved when a good
        # answer was available from the other source.
        osm_segments = _get_osm_local_segments(main_street_name, settlement, parent_region)
        if osm_segments:
            osm_result, osm_degenerate = _attempt_clip(osm_segments, start_street, end_street, settlement, parent_region, main_street_name, is_rgz_main=False)
            if not osm_degenerate:
                return osm_result

    return result


def _attempt_clip(main_segments: list[list], start_street: str, end_street: str, settlement: str, parent_region: str, main_street_name: str, is_rgz_main: bool) -> tuple[list[list], bool]:
    """Does the actual clipping work for one geometry source. Returns (segments, degenerate) —
    degenerate=True whenever no real substring clip could be computed and `segments` is just the
    capped-or-full fallback, so the caller (clip_polyline_by_intersections) knows whether it's
    worth retrying against a different geometry source instead of accepting this result."""
    # Bound to the real closure once total_len is known (see below); stays None if an exception
    # strikes before that point, so the bottom-of-function fallback can tell whether it's safe to
    # apply the length cap or should fall back to the old unconditional "return everything".
    _safe_unclipped_fallback = None

    try:
        valid_lines = [LineString(seg) for seg in main_segments if isinstance(seg, (list, tuple)) and len(seg) >= 2]
        if not valid_lines:
            return main_segments, True

        # A single named street is very often digitized as several DISJOINT chains — RGZ splits
        # its own street entity at every cross street, and OSM ways aren't always contiguous
        # either. stitch_street_segments() used to collapse this down to just the single longest
        # merged chain, which silently discards real parts of the street. Confirmed case:
        # Синђелићева's real crossing with Браће Јовановића sits on RGZ's 3rd-longest fragment
        # chain (of 4), nowhere near the longest one — keeping only the longest chain made
        # "Б.Јовановић" miss it entirely and fall through to an arbitrary 200m buffer landing
        # ~500m from the real intersection. Keep every merged component available for the
        # proximity/intersection search below, and only commit to one specific component once
        # both bounds are confirmed to actually agree on it (see comp_start/comp_end below).
        try:
            merged = linemerge(valid_lines)
        except Exception:
            merged = None

        if merged is None:
            components = valid_lines
        elif merged.geom_type == 'LineString':
            components = [merged]
        elif merged.geom_type == 'MultiLineString':
            components = list(merged.geoms)
        else:
            components = valid_lines

        components = [c for c in components if c.length > 0]
        if not components:
            return main_segments, True

        # Used only as a shared reference for latitude-corrected meter tolerances below, and as
        # the base line for the rarer "no cross-street bounds resolved at all" fallbacks further
        # down — NOT assumed to be the component either bound actually lands on.
        main_line = max(components, key=lambda l: l.length)
        total_len = main_line.length
        ref_lat = main_line.centroid.y

        # RGZ registers a street's FULL length across a whole settlement as one entity — much
        # longer, in general, than OSM's typically block-sized "way" tags (confirmed case:
        # Прашка/Стипе Гргића in Suboticа came out as 700-780m of scattered fragments once their
        # cross-street clip degenerated). Silently returning the *entire* unclipped street when a
        # specific sub-segment was requested was a reasonable fail-safe for a short OSM way; for
        # a long RGZ street entity it means flooding a much wider area than the outage ever
        # described — visibly wrong lines sprawling across town. Once bounds were actually
        # requested (this function only runs at all when they were — see the guard above), cap
        # how much we're willing to hand back on a failed/degenerate clip instead of dumping
        # everything; UNRESOLVED is a more honest outcome than "the whole street, maybe 3km of it".
        #
        # Measured against main_segments' own real length (_segments_length_m), NOT total_len
        # (main_line.length, a degree-Euclidean approximation of the *stitched* line) — RGZ's
        # un-stitched multi-fragment geometry means those two can diverge a lot: linemerge finds
        # a short direct path between touching fragment endpoints, while the raw fragment list
        # (what actually gets returned here) can cover far more real ground than that.
        MAX_UNCLIPPED_FALLBACK_M = 500

        def _safe_unclipped_fallback():
            return [] if _segments_length_m(main_segments) > MAX_UNCLIPPED_FALLBACK_M else main_segments

        # Resolve the settlement anchor ONCE, up front, and use it for the first (and only)
        # get_intersection_point lookup. Previously this ran twice per bound — once without
        # settlement_hex (so it could silently accept an intersection match from a *different*
        # town that happens to share both street names, e.g. two towns both having a "Trg
        # Slobode"/"Glavna" pair) and once with it — but if the second, validated call came back
        # empty, d_start/d_end were left holding the first call's unvalidated result instead of
        # being reset. Doing the validated lookup first avoids that.
        settlement_hex, _ = get_settlement_info(settlement or parent_region) if (settlement or parent_region) else (None, None)

        # Real-meter tolerances, latitude-corrected: a flat degree constant is effectively a
        # tighter real-world tolerance in northern Vojvodina (Subotica) than further south
        # (Pančevo), for the exact same nominal value.
        PROXIMITY_LIMIT = meters_to_degrees_lon(300, ref_lat)

        def _nearest_component(point):
            best = None
            for comp in components:
                d = comp.distance(point)
                if best is None or d < best[0]:
                    best = (d, comp)
            return best[1] if best else None

        def _via_intersection_table(cross_street):
            if not (main_street_name and cross_street):
                return None
            p = get_intersection_point(main_street_name, cross_street, settlement_hex=settlement_hex)
            if not p:
                return None
            comp = _nearest_component(p)
            return (comp, comp.project(p)) if comp else None

        def _via_local_proximity(cross_street):
            if not cross_street:
                return None
            cleaned = clean_street(cross_street)
            if not cleaned:
                return None
            segs = get_local_street_segments(cleaned, settlement, parent_region)
            if not segs:
                return None
            min_d, best = float('inf'), None
            for s in segs:
                if len(s) >= 2:
                    cross_line = LineString(s)
                    for comp in components:
                        p1, p2 = nearest_points(comp, cross_line)
                        dist = p1.distance(p2)
                        if dist < min_d and dist < PROXIMITY_LIMIT:
                            min_d = dist
                            best = (comp, comp.project(p1))
            return best

        # `intersections` is built from OSM geometry (build_intersections.py); when the main
        # street itself resolved via RGZ, an OSM-table intersection point is from a different
        # digitization than main_line and can sit tens of meters off it, projecting to a wrong
        # point along the line. Comparing two RGZ-sourced geometries directly (same digitization)
        # is far more consistent in that case, so try that first and only fall back to the OSM
        # table if RGZ has no geometry for the cross-street either. Pure-OSM streets keep the
        # original order, where the OSM table's exact computed contact point is the better source.
        # (is_rgz_main is a parameter here — the caller already knows it and uses it to decide
        # whether an OSM retry is worth attempting on a degenerate result.)

        def _first_of(*methods):
            for method in methods:
                r = method()
                if r is not None:
                    return r
            return None

        # Each result is (component, along-component distance) rather than a bare distance now,
        # since a bound found on one disjoint chain can't be mixed with a distance measured along
        # a different one.
        if is_rgz_main:
            start_result = _first_of(lambda: _via_local_proximity(start_street), lambda: _via_intersection_table(start_street))
            end_result = _first_of(lambda: _via_local_proximity(end_street), lambda: _via_intersection_table(end_street))
        else:
            start_result = _first_of(lambda: _via_intersection_table(start_street), lambda: _via_local_proximity(start_street))
            end_result = _first_of(lambda: _via_intersection_table(end_street), lambda: _via_local_proximity(end_street))

        # "од X према Y" (from X towards Y) describes a DIRECTION, not a cross-street bound —
        # Y is often a neighboring settlement/place name, not a street, so it will never resolve
        # via get_intersection_point or the local-street proximity check above. If Y resolves as
        # a real settlement instead, clip all the way to whichever end of this street's line is
        # geographically closer to that settlement, rather than falling through to a tiny fixed
        # buffer that has no relationship to the real direction. Anchored to whichever component
        # the OTHER bound already resolved to, if any, so both ends stay on the same chain.
        if start_result is None and start_street:
            ref_line = end_result[0] if end_result else main_line
            d = _resolve_direction_toward_place(ref_line, start_street, parent_region, ref_line.length)
            if d is not None:
                start_result = (ref_line, d)
        if end_result is None and end_street:
            ref_line = start_result[0] if start_result else main_line
            d = _resolve_direction_toward_place(ref_line, end_street, parent_region, ref_line.length)
            if d is not None:
                end_result = (ref_line, d)

        FALLBACK_BUFFER = meters_to_degrees_lon(200, ref_lat)

        if start_result is None and end_result is not None:
            comp, d_end = end_result
            if start_street and start_street.strip():
                start_result = (comp, max(0.0, d_end - FALLBACK_BUFFER))
            else:
                start_result = (comp, 0.0)
        elif end_result is None and start_result is not None:
            comp, d_start = start_result
            if end_street and end_street.strip():
                end_result = (comp, min(comp.length, d_start + FALLBACK_BUFFER))
            else:
                end_result = (comp, comp.length)
        elif start_result is None and end_result is None:
            return _safe_unclipped_fallback(), True

        comp_start, d_start = start_result
        comp_end, d_end = end_result

        if comp_start is not comp_end:
            # Bounds landed on two different disjoint chains of the same-named street — e.g. one
            # cross street only matched a fragment far from where the other one matched. Can't
            # safely combine distances measured along different lines into one clip; same
            # "honest unresolved beats a confident wrong answer" call as the degenerate-point case
            # below.
            return _safe_unclipped_fallback(), True

        main_line_for_clip = comp_start
        p_min = min(d_start, d_end)
        p_max = max(d_start, d_end)

        if abs(p_min - p_max) > 1e-6:
            clipped = substring(main_line_for_clip, p_min, p_max)
            if clipped.geom_type == 'LineString':
                return [list(clipped.coords)], False
            elif clipped.geom_type == 'MultiLineString':
                return [list(g.coords) for g in clipped.geoms], False

    except Exception as e:
        print(f"⚠️ Intersection clipping error: {e}")

    # Reached either after an exception, or a degenerate clip (p_min == p_max) fell through
    # without hitting a return above — apply the same length cap as the explicit fallback calls
    # above when it's available (i.e. we got far enough into the try block to compute it).
    if _safe_unclipped_fallback is not None:
        return _safe_unclipped_fallback(), True
    return main_segments, True

def get_street_hexes(street_name: str, settlement: str = None, parent_region: str = None, intersection_start: str = None, intersection_end: str = None, resolution: int = 9) -> set[str]:
    segments = get_local_street_segments(street_name, settlement, parent_region, resolution)
    if not segments:
        return set()

    if intersection_start or intersection_end:
        segments = clip_polyline_by_intersections(
            segments, intersection_start, intersection_end, settlement, parent_region, main_street_name=street_name
        )

    hexes = set()
    for seg in segments:
        for pt in seg:
            if len(pt) >= 2:
                hexes.add(h3.latlng_to_cell(pt[1], pt[0], resolution))

    return hexes

def _merge_for_display(segments: list[list]) -> list[list]:
    """Stitches touching fragments back into continuous chains purely for how a street draws on
    the map. RGZ splits a street's own registered entity into a new fragment at every cross
    street — a whole-street mention with no clip bounds (e.g. "део Родина", no named cross
    streets) is handed the raw, unmerged fragment list as-is, which can be 100+ separate 2-point
    pieces. Drawn independently, that reads as a dashed/sawtooth line rather than one street,
    even though each individual point is accurate. Keeps every disjoint merged component (unlike
    the old stitch_street_segments(), which kept only the single longest one) since a street can
    legitimately be non-contiguous across town — this only reconnects fragments that actually
    touch, it doesn't invent connections. Not applied to the matching path (get_street_hexes) —
    only here, where the output is purely for human eyes."""
    valid_lines = [LineString(seg) for seg in segments if isinstance(seg, (list, tuple)) and len(seg) >= 2]
    if not valid_lines:
        return segments

    try:
        merged = linemerge(valid_lines)
    except Exception:
        return segments

    if merged.geom_type == 'LineString':
        return [list(merged.coords)]
    elif merged.geom_type == 'MultiLineString':
        return [list(g.coords) for g in merged.geoms]
    return segments


def _simplify_for_display(segments: list[list], tolerance_m: float = 4.0) -> list[list]:
    """Light Douglas-Peucker simplification purely for how a street draws on the map. RGZ's
    survey-grade digitization can pack a vertex every few meters (a real case: 40 points over a
    ~260m clip) — each point is individually accurate, but connecting that many closely-spaced
    points with straight segments reads as a jittery/"crooked" line at normal map zoom, even
    though the real street is a smooth curve. Deliberately not applied to the matching path
    (get_street_hexes / clip_polyline_by_intersections) — only here, where the output is purely
    for human eyes."""
    simplified = []
    for seg in segments:
        if len(seg) < 3:
            simplified.append(seg)
            continue
        ref_lat = seg[len(seg) // 2][1]
        tolerance_deg = meters_to_degrees_lon(tolerance_m, ref_lat)
        line = LineString(seg).simplify(tolerance_deg, preserve_topology=False)
        simplified.append(list(line.coords) if line.geom_type == 'LineString' else seg)
    return simplified


def get_street_geojson_feature(street_name: str, settlement: str = None, parent_region: str = None, intersection_start: str = None, intersection_end: str = None) -> dict | None:
    segments = get_local_street_segments(street_name, settlement, parent_region)
    if not segments:
        return None

    if intersection_start or intersection_end:
        segments = clip_polyline_by_intersections(
            segments, intersection_start, intersection_end, settlement, parent_region, main_street_name=street_name
        )

    # A LineString/MultiLineString needs >=2 coordinates per segment; drop degenerate
    # single-point "segments" (see query_street_database's Point handling) here, since they're
    # still valid for hex-based matching (get_street_hexes) but can't be drawn as a line.
    segments = [seg for seg in segments if len(seg) >= 2]
    if not segments:
        return None

    segments = _merge_for_display(segments)
    segments = _simplify_for_display(segments)

    lat_name = cyrillic_to_latin(clean_street(street_name)).upper()

    if len(segments) == 1:
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": segments[0]
            },
            "properties": {"street": lat_name, "settlement": settlement or parent_region}
        }
    else:
        return {
            "type": "Feature",
            "geometry": {
                "type": "MultiLineString",
                "coordinates": segments
            },
            "properties": {"street": lat_name, "settlement": settlement or parent_region}
        }


# How far a house's RGZ address point may sit from the street's centre line and still be placed
# on it: the point marks the building, set back from the carriageway (10-40 m in a street grid,
# far more on a boulevard through superblocks). At 100 m, Bulevar Zorana Đinđića 54 in Novi
# Beograd (153 m off the line, like 90 of the street's 256 houses) was dropped and the whole
# boulevard drew instead. The points come from RGZ for this street only, so a wider reach can't
# pick up another street's house.
HOUSE_POINT_MAX_OFFSET_M = 250
# Affected houses spanning less than this along the street draw as one pin, not a line.
HOUSE_PIN_SPAN_M = 30
# Added to each end of a drawn stretch, so the first and last house sit inside it, not at its tip.
HOUSE_STRETCH_PAD_M = 15


def house_number_stretch(feature: dict, points) -> dict | None:
    """Map only: the part of a drawn street `feature` (LineString / MultiLineString) that the
    affected houses at `points` [(lat, lon), ...] stand on. A pin (Point) on the houses when they
    lie within HOUSE_PIN_SPAN_M of each other along the street; otherwise, per disjoint piece of
    the street, the stretch from the first house to the last. Houses farther than
    HOUSE_POINT_MAX_OFFSET_M from the line are ignored. None when none is left - the caller then
    keeps the whole street."""
    geom = (feature or {}).get("geometry") or {}
    if geom.get("type") == "LineString":
        parts = [geom.get("coordinates") or []]
    elif geom.get("type") == "MultiLineString":
        parts = geom.get("coordinates") or []
    else:
        return None
    parts = [p for p in parts if len(p) >= 2]
    points = list(points or [])
    if not parts or not points:
        return None

    # Metres on a local flat projection - fine at street scale.
    kx = 111320 * math.cos(math.radians(points[0][0]))
    ky = 111320
    lines = [LineString([(pt[0] * kx, pt[1] * ky) for pt in p]) for p in parts]

    placed = {}   # index of the street piece -> [(metres along it, lat, lon)]
    for lat, lon in points:
        here = Point(lon * kx, lat * ky)
        i = min(range(len(lines)), key=lambda k: lines[k].distance(here))
        if lines[i].distance(here) <= HOUSE_POINT_MAX_OFFSET_M:
            placed.setdefault(i, []).append((lines[i].project(here), lat, lon))
    if not placed:
        return None

    if len(placed) == 1:
        (houses,) = placed.values()
        along = [h[0] for h in houses]
        if max(along) - min(along) < HOUSE_PIN_SPAN_M:
            lat = sum(h[1] for h in houses) / len(houses)
            lon = sum(h[2] for h in houses) / len(houses)
            return {**feature, "geometry": {"type": "Point", "coordinates": [lon, lat]}}

    stretches = []
    for i in sorted(placed):
        line, along = lines[i], [h[0] for h in placed[i]]
        start = max(0.0, min(along) - HOUSE_STRETCH_PAD_M)
        end = min(line.length, max(along) + HOUSE_STRETCH_PAD_M)
        piece = substring(line, start, end)
        coords = [[x / kx, y / ky] for x, y in piece.coords] if piece.geom_type == "LineString" else []
        if len(coords) >= 2:
            stretches.append(coords)
    if not stretches:
        return None
    if len(stretches) == 1:
        return {**feature, "geometry": {"type": "LineString", "coordinates": stretches[0]}}
    return {**feature, "geometry": {"type": "MultiLineString", "coordinates": stretches}}