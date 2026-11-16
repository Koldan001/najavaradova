"""
Tests for scripts/build_rgz_index.py's reprojection helper, and for the RGZ-first resolution
paths it feeds in spatial/spatial_engine.py (query_rgz_streets, get_settlement_info). See the
RGZ integration plan for background on why RGZ is preferred over OSM+Nominatim where it has
coverage.

Run with: pytest tests/test_rgz_index.py -v
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import spatial.spatial_engine as spatial_engine
from scripts.build_rgz_index import reproject_wkt
from spatial.spatial_engine import (
    RGZ_DB_PATH,
    get_settlement_info,
    get_street_geojson_feature,
    get_street_hexes,
    query_rgz_streets,
)

requires_rgz_db = pytest.mark.skipif(
    not os.path.exists(RGZ_DB_PATH),
    reason="data/rgz.db not present locally (run scripts/build_rgz_index.py)",
)


# ---------------------------------------------------------------------------
# reproject_wkt — no DB required
# ---------------------------------------------------------------------------

def test_a_gauss_kruger_part_is_read_as_such_not_as_utm():
    # Užičke Republike, Kragujevac (RGZ 718980010364): one part in UTM 34N, one in Gauss-Krüger
    # zone 7. Read as UTM, the second part landed in India (#4362, 12.10.2026).
    geom = reproject_wkt("MULTILINESTRING((497131.07 4870828.7,497127.8 4870826.87),"
                         "(7498169.92035 4871716.86365,7498216.54035 4871741.64365))")
    assert geom["type"] == "MultiLineString" and len(geom["coordinates"]) == 2
    for line in geom["coordinates"]:
        for lon, lat in line:
            assert 20.9 < lon < 21.0 and 43.9 < lat < 44.1


def test_a_geometry_outside_serbia_is_dropped():
    assert reproject_wkt("POINT(9000000 4870828.7)") is None  # no such zone
    assert reproject_wkt("LINESTRING(10 10, 20 20)") is None  # UTM origin area, far from Serbia


def test_reproject_wkt_matches_known_futog_location():
    # Sample point straight from an RGZ housenumbers CSV row (Futog, near Novi Sad) — this is
    # the check that originally confirmed the source CRS is EPSG:32634 (WGS84 / UTM zone 34N).
    geom = reproject_wkt("POINT(392297.84 5019453.39)")
    assert geom["type"] == "Point"
    lon, lat = geom["coordinates"]
    assert 45.25 < lat < 45.40 and 19.55 < lon < 19.70


def test_reproject_wkt_handles_empty_and_garbage_input():
    assert reproject_wkt("") is None
    assert reproject_wkt(None) is None
    assert reproject_wkt("NOT WKT") is None


# ---------------------------------------------------------------------------
# query_rgz_streets — requires data/rgz.db
# ---------------------------------------------------------------------------

@requires_rgz_db
def test_query_rgz_streets_finds_known_street_in_naselje():
    segs = query_rgz_streets("СИНЂЕЛИЋЕВА", naselje="ПАНЧЕВО", opstina="ПАНЧЕВО")
    assert segs


@requires_rgz_db
def test_query_rgz_streets_stemming_bridges_declined_form():
    # "Таковске" (genitive, as it appears in real outage text) vs RGZ's canonical nominative
    # "Таковска" — without get_smart_stem() applied on the RGZ side too, this returns nothing.
    segs = query_rgz_streets("ТАКОВСКЕ", naselje="ЗРЕЊАНИН", opstina="ЗРЕЊАНИН")
    assert segs


@requires_rgz_db
def test_query_rgz_streets_bridges_punctuation_and_ordinal_forms():
    """
    Regression lock for a batch of real, confirmed name-form mismatches found by comparing real
    cached outage text against RGZ's canonical spelling — see the per-word matching in
    _street_name_like_patterns(). Each of these returned nothing before that fix existed.
    """
    # Missing period: text "22 Августа" vs RGZ's "22. Августа".
    assert query_rgz_streets("22 АВГУСТА", naselje="БУКОВАЦ", opstina="НОВИ САД")
    # Ordinal word vs RGZ's numbered form ("Виногради 1"): both a digit-first and a
    # word-ordinal-first phrasing should resolve.
    assert query_rgz_streets("5 ВИНОГРАДИ", naselje="ЧАНТАВИР", opstina="СУБОТИЦА")
    assert query_rgz_streets("ПРВИ ВИНОГРАДИ", naselje="ЧАНТАВИР", opstina="СУБОТИЦА")
    # Ordinal-genitive day + month word vs RGZ's "N. Month" numeral form.
    assert query_rgz_streets("ОСМОГ МАРТА", naselje="ЧАНТАВИР", opstina="СУБОТИЦА")
    assert query_rgz_streets("СЕДМОГ ЈУЛА", naselje="ЧАНТАВИР", opstina="СУБОТИЦА")
    # Case ending on the FIRST word only ("Генерал" vs RGZ's genitive "Генерала").
    assert query_rgz_streets("ГЕНЕРАЛ КУТУЗОВА", naselje="СУБОТИЦА", opstina="СУБОТИЦА")
    # Given-name/surname word order reversed ("Јураја Арнолда" vs RGZ's "Арнолд Јураја").
    assert query_rgz_streets("ЈУРАЈА АРНОЛДА", naselje="СУБОТИЦА", opstina="СУБОТИЦА")


@requires_rgz_db
def test_query_rgz_streets_bridges_single_letter_typo():
    """
    Outage #23's real, confirmed source-text typo: 'Душана Крнајског' (with plain Н) vs RGZ's
    canonical 'Душана Крњајског' (with Њ) — a single-letter transcription slip, not a
    grammatical case ending or word-order flip, so the stemming/pattern tiers above don't bridge
    it. Fuzzy-matches as a last resort, but ONLY against this naselje's own street names — must
    not accidentally match some unrelated street.
    """
    assert query_rgz_streets("ДУШАНА КРНАЈСКОГ", naselje="СУБОТИЦА", opstina="СУБОТИЦА")


@requires_rgz_db
def test_query_rgz_streets_does_not_broaden_to_wrong_naselje_in_same_opstina():
    """
    Regression lock for a real, confirmed mislocation: "Буњевачка" exists in RGZ only under
    naselje=Bajmok, not under Пачир — both in Subotica opština. An earlier version of
    query_rgz_streets() broadened an unmatched naselje to the whole opština, which silently
    resolved a Пачir outage's street to Bajmok's geometry (many km away) instead of reporting no
    match. Must return [] here, not Bajmok's segments, so the caller falls through to the OSM
    path instead of accepting a same-opština-wrong-naselje result.
    """
    segs = query_rgz_streets("БУЊЕВАЧКА", naselje="ПАЧИР", opstina="СУБОТИЦА")
    assert segs == []


@requires_rgz_db
def test_query_rgz_streets_initial_disambiguates_same_surname_streets():
    """
    Regression lock for outage #13's real "drawing on wrong side" bug: Pančevo has four distinct
    "...Јовановић(а)" streets (Змај Јове, Браће, Стевице, Паје). An initial-stripped
    "%JOVANOVIĆ%" pattern matches all four and returns their combined geometry as if it were one
    street. "Б.Јовановић" should resolve ONLY to Браће Јовановића (the one starting with Б) —
    not the other three, and not a bigger unioned set.
    """
    only_braće = query_rgz_streets("БРАЋЕ ЈОВАНОВИЋА", naselje="ПАНЧЕВО", opstina="ПАНЧЕВО")
    abbreviated = query_rgz_streets("Б.ЈОВАНОВИЋ", naselje="ПАНЧЕВО", opstina="ПАНЧЕВО")
    assert abbreviated
    assert len(abbreviated) == len(only_braće)


@requires_rgz_db
def test_query_rgz_streets_returns_empty_for_unknown_street():
    segs = query_rgz_streets("ЈЕДНАУЛИЦАКОЈАНЕПОСТОЈИ", naselje="ПАНЧЕВО", opstina="ПАНЧЕВО")
    assert segs == []


# ---------------------------------------------------------------------------
# get_settlement_info — RGZ-first, no Nominatim call for covered settlements
# ---------------------------------------------------------------------------

@requires_rgz_db
def test_get_settlement_info_resolves_rgz_settlement_without_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should not hit Nominatim for an RGZ-covered settlement")

    monkeypatch.setattr(spatial_engine, "_geocode_with_backoff", _boom)

    hex_id, radius = get_settlement_info("ПАНЧЕВО", "ПАНЧЕВО")
    assert hex_id is not None
    assert radius is not None


@requires_rgz_db
def test_rgz_settlement_lookup_does_not_ignore_a_mismatched_parent():
    """
    Regression lock, same bug class as the street-side one above: Пачir is a real RGZ-covered
    naselje, but under opština Subotica, not Novi Sad. An earlier version of
    _get_rgz_settlement_info() fell back to a nationwide, opština-unscoped naselje lookup
    whenever the given parent didn't match — silently ignoring a caller-supplied (wrong) parent
    instead of reporting no match. Tested directly against the private helper (rather than
    get_settlement_info()) so a genuine miss here doesn't fall through to a live Nominatim call.
    """
    hex_id, radius = spatial_engine._get_rgz_settlement_info("PAČIR", "NOVI SAD", 9)
    assert hex_id is None
    assert radius is None


@requires_rgz_db
def test_get_settlement_info_subotica_is_not_village_scale():
    # Regression lock for a real miscalibration this integration introduced along the way: an
    # RGZ-derived radius_steps computed only from the "SUBOTICA" naselje's own streets (~51) came
    # out under core/matchmaker.py's SMALL_SETTLEMENT_RADIUS_THRESHOLD (65), misclassifying a real
    # city as village-scale. build_settlements() fixed this by using the whole opština's bbox
    # when naselje_lat == opstina_lat.
    _, radius = get_settlement_info("СУБОТИЦА", "СУБОТИЦА")
    assert radius is not None and radius > 65


# ---------------------------------------------------------------------------
# clip_polyline_by_intersections — degenerate RGZ clip must not sprawl, and
# should retry against OSM before giving up
# ---------------------------------------------------------------------------

@requires_rgz_db
def test_degenerate_rgz_clip_retries_against_osm_and_resolves_tightly():
    """
    Regression lock for outage #22's real "lines going everywhere" map bug, and its fix: Стипе
    Гргића's cross-street bounds (Травничка/Прашка) both project to the exact same point on its
    RGZ geometry (RGZ's registered entity stops well short of actually reaching Прашка), so an
    RGZ-only clip degenerates — and before the length cap existed, that fell back to returning
    the entire ~770m of scattered RGZ fragments. OSM's own "way" for the same street turns out to
    reach both cross streets cleanly, so clip_polyline_by_intersections() now retries against OSM
    geometry on a degenerate RGZ clip instead of giving up — this should resolve to a tight,
    single-digit-hex clip, not zero hexes and not a sprawl across a big chunk of Suboticа.
    """
    hexes = get_street_hexes(
        "СТИПЕ ГРГИЋА", settlement="СУБОТИЦА", parent_region="СУБОТИЦА",
        intersection_start="ТРАВНИЧКА", intersection_end="ПРАШКА",
    )
    assert 0 < len(hexes) < 15


# ---------------------------------------------------------------------------
# get_street_geojson_feature — display simplification
# ---------------------------------------------------------------------------

@requires_rgz_db
def test_geojson_feature_reconnects_fragments_with_no_clip_bounds():
    """
    Outage #17 (Бездан/Сомбор): "део Родина", "Малта" — whole-street/part-of-street mentions
    with no named cross streets, so no clip ever runs. RGZ splits its own street entity into a
    new fragment at every side-street junction (real case: Родина came back as 143 separate,
    individually-accurate 2-point fragments, most of which share an exact touching endpoint with
    a neighbor) — handed to the map as-is, that draws as a dashed/sawtooth line instead of one
    street. get_street_geojson_feature() should reconnect touching fragments into far fewer,
    longer chains before display.
    """
    raw_segments = query_rgz_streets("РОДИНА", naselje="БЕЗДАН", opstina="СОМБОР")
    raw_fragment_count = len(raw_segments)
    assert raw_fragment_count > 20  # sanity: this street really is fragment-heavy in RGZ

    feat = get_street_geojson_feature("РОДИНА", "БЕЗДАН", "СОМБОР")
    assert feat is not None
    coords = feat["geometry"]["coordinates"]
    merged_component_count = 1 if feat["geometry"]["type"] == "LineString" else len(coords)
    assert merged_component_count < raw_fragment_count / 5


@requires_rgz_db
def test_geojson_feature_is_simplified_for_display():
    """
    RGZ's survey-grade digitization packs a vertex every few meters - each one individually
    accurate, but dense enough to read as a jittery/"crooked" line at normal map zoom.
    get_street_geojson_feature() should hand back a visibly lighter point count than the raw
    geometry, without collapsing to a straight 2-point line (the real street does curve).

    Was Прашка/Suboticа (a real case: its clipped ~260m segment carried 40 points) - RGZ's own
    digitization for that specific street fragmented further at some point after this test was
    written (93 disjoint 2-point pieces now, none reaching within tolerance of either named
    cross street) and OSM has zero coverage for it to fall back on, so the clip now correctly
    resolves UNRESOLVED rather than a wrong guess - by design, see clip_polyline_by_intersections.
    Not a live bug: Subotica has no active scraper, this street is never hit in production.
    Swapped to Kneza Miloša/Beograd (an actively-scraped city) - real, stable, and still resolves
    cleanly against current data."""
    raw_segments = query_rgz_streets("КНЕЗА МИЛОША", naselje="БЕОГРАД", opstina="БЕОГРАД")
    raw_points = sum(len(seg) for seg in raw_segments)
    assert raw_points > 20  # sanity: this street really is densely digitized

    feat = get_street_geojson_feature(
        "КНЕЗА МИЛОША", "БЕОГРАД", "БЕОГРАД",
        intersection_start="НЕМАЊИНА", intersection_end="ПОП ЛУКИНА",
    )
    assert feat is not None
    coords = feat["geometry"]["coordinates"]
    simplified_points = len(coords) if feat["geometry"]["type"] == "LineString" else sum(len(c) for c in coords)
    assert 2 < simplified_points < raw_points / 2
