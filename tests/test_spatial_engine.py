"""
Regression suite for spatial/spatial_engine.py, seeded from real outages already sitting in
data/outage_notifier.db (not invented fixtures) — see the plan discussion for how each case was
diagnosed. Requires data/serbia_streets.db (a large, gitignored local artifact); skipped
entirely if it's not present.

Run with: pytest tests/test_spatial_engine.py -v
"""
import math
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from spatial.spatial_engine import (
    SERBIA_DB_PATH,
    clean_settlement,
    meters_to_degrees_lon,
    get_settlement_info,
    get_street_geojson_feature,
    get_local_street_segments,
    get_intersection_point,
    is_generic_street_token,
    _street_name_like_patterns,
)


def test_generic_street_token_recognises_bare_classifiers():
    for w in ("Trg", "ТРГ", "trg", "Bulevar", "Put", "Naselje", "Kej"):
        assert is_generic_street_token(w), w
    for w in ("Trg Marije Trandafil", "Bulevar oslobođenja", "Danila Kiša", "Putnička"):
        assert not is_generic_street_token(w), w


def test_like_patterns_refuses_to_expand_a_bare_classifier():
    # "%TRG%" would union every square in the naselje - no wildcard patterns for a bare "Trg".
    assert _street_name_like_patterns("TRG") == []
    assert _street_name_like_patterns("BULEVAR") == []
    # a real multi-word name that happens to start with a classifier is unaffected
    assert _street_name_like_patterns("TRG MARIJE TRANDAFIL")

requires_street_db = pytest.mark.skipif(
    not os.path.exists(SERBIA_DB_PATH),
    reason="data/serbia_streets.db not present locally",
)


def haversine_m(coords: list[list[float]]) -> float:
    """Total length in meters of a [[lon, lat], ...] coordinate list."""
    R = 6371000
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
        total += 2 * R * math.asin(math.sqrt(a))
    return total


def feature_length_m(feature: dict) -> float:
    coords = feature["geometry"]["coordinates"]
    if feature["geometry"]["type"] == "LineString":
        return haversine_m(coords)
    return sum(haversine_m(c) for c in coords)


# ---------------------------------------------------------------------------
# Unit tests — no DB/network required
# ---------------------------------------------------------------------------

def test_clean_settlement_strips_place_type_prefixes():
    assert clean_settlement("СЛ.Виногради") == "Виногради"
    assert clean_settlement("МЕСТО Виногради") == "Виногради"
    assert clean_settlement("НАСЕЉЕ Чантавир") == "Чантавир"
    assert clean_settlement("Суботица") == "Суботица"  # no-op on an already-clean name


def test_meters_to_degrees_lon_grows_going_north():
    # A degree of longitude covers fewer real meters further from the equator, so representing
    # the *same* real-world distance takes a *larger* degree value at Subotica's latitude
    # (~46.1) than at Pančevo's (~44.9). This is what corrects the original bug: a flat degree
    # constant used everywhere was an effectively tighter real tolerance further north.
    subotica_deg = meters_to_degrees_lon(300, at_lat=46.1)
    pancevo_deg = meters_to_degrees_lon(300, at_lat=44.9)
    assert subotica_deg > pancevo_deg


# ---------------------------------------------------------------------------
# Regression locks — the two known-correct outages must stay correct
# ---------------------------------------------------------------------------

@requires_street_db
def test_pancevo_sindjeliceva_stays_tightly_clipped():
    """
    Outage #13: Синђелићева од С.Милетића до Б.Јовановић.

    Real regressions this locks in, found in sequence against the actual outage:
    1. "Б.Јовановић" is an abbreviated cross-street, and Pančevo has FOUR distinct
       "...Јовановић(а)" streets (Змај Јове, Браће, Стевице, Паје) — stripping the leading
       initial as noise before matching used to make "%JOVANOVIĆ%" match all four and silently
       union their geometry. Only "Браће Јовановића" starts with the initial "Б", so the fix
       anchors on it.
    2. Even after (1), the real crossing with Браће Јовановића sits on RGZ's 3rd-longest
       disjoint fragment chain for Синђелићева (of 4) — stitch_street_segments() used to reduce
       a fragmented street down to only its single longest merged chain, silently discarding the
       chain the real crossing is actually on. That made the proximity search miss it and fall
       through to an arbitrary 200m buffer landing ~500m from the true intersection instead.
    A pure length bound doesn't catch either bug on its own (each still produced an in-range-
    looking length) — assert one endpoint sits at the real, known Синђелићева/Браће Јовановића
    intersection (confirmed independently via OSM's exact geometric intersection of the two
    streets) too.
    """
    feat = get_street_geojson_feature("СИНЂЕЛИЋЕВА", "ПАНЧЕВО", "ПАНЧЕВО", "С.МИЛЕТИЋА", "Б.ЈОВАНОВИЋ")
    assert feat is not None
    length = feature_length_m(feat)
    # Baseline measured before this change: ~214m clipped vs ~1505m for the whole street. Raised
    # lower bound (was 0) since the wrong-street bug above still produced an in-range-looking
    # ~46m result — a length check alone isn't enough to catch it.
    assert 150 < length < 400

    coords = feat["geometry"]["coordinates"]
    braće_jovanovića = (20.6463248, 44.8747046)

    def near(pt, target, tol=1e-4):
        return abs(pt[0] - target[0]) < tol and abs(pt[1] - target[1]) < tol

    assert near(coords[0], braće_jovanovića) or near(coords[-1], braće_jovanovića)


@requires_street_db
def test_intersection_point_initial_disambiguates_same_surname_streets():
    """
    The `intersections` table (built from OSM geometry) stores street names in Cyrillic only.
    get_intersection_point() used to build its LIKE-fallback patterns from the Latin
    transliteration even though the column it matches against is Cyrillic — those literally
    never match (Cyrillic 'Ј' and Latin 'J' are different code points), so an abbreviated cross
    street like "Б.Јовановић" could only ever resolve via an exact full-name match, never via the
    stemmed/initial fallback. Confirms the fallback now actually fires and, per the same
    disambiguation fix as query_rgz_streets, resolves to the one Јовановић street starting with Б.
    """
    settlement_hex, _ = get_settlement_info("ПАНЧЕВО", "ПАНЧЕВО")
    p = get_intersection_point("СИНЂЕЛИЋЕВА", "Б.ЈОВАНОВИЋ", settlement_hex=settlement_hex)
    assert p is not None
    assert abs(p.x - 20.6463248) < 1e-3 and abs(p.y - 44.8747046) < 1e-3


@requires_street_db
def test_zrenjanin_manastirska_known_limitation_does_not_crash():
    """
    Outage #18: Манастирска од Цара Душана до Таковске.

    Before RGZ street data was wired in (see the RGZ integration plan), this clipped tightly to
    ~157m against OSM's ~393m whole-street geometry. RGZ is now preferred for Zrenjanin, and it
    turns out RGZ's own registered "Манастирска" street entity there only spans ~278m in the
    first place — both Цара Душана and Таковска project to the same end of that shorter RGZ
    line, so there's no distinct interior point to clip between and the full RGZ segment is
    returned unclipped instead. Same "known limitation, fails safe" shape as the sibling
    Т.Рајића case below: locks in that this stays bounded and within Zrenjanin rather than
    silently producing something wildly wrong.
    """
    feat = get_street_geojson_feature("МАНАСТИРСКА", "ЗРЕЊАНИН", "ЗРЕЊАНИН", "ЦАРА ДУШАНА", "ТАКОВСКЕ")
    assert feat is not None
    length = feature_length_m(feat)
    assert 0 < length < 400
    coords = feat["geometry"]["coordinates"]
    sample = coords[0] if feat["geometry"]["type"] == "LineString" else coords[0][0]
    lon, lat = sample
    assert 45.3 < lat < 45.5 and 20.3 < lon < 20.5


# ---------------------------------------------------------------------------
# Known limitation — documented, not silently swept under the rug
# ---------------------------------------------------------------------------

@requires_street_db
def test_pancevo_t_rajica_known_limitation_does_not_crash():
    """
    Outage #12: Т.Рајића од Ј.безуљевића до Синђелићеве — one of two street segments in this
    outage clips perfectly (see the sibling case below); this one currently does NOT clip,
    because "Ј.безуљевића" doesn't resolve via get_intersection_point at all (looks like an
    OCR/typo issue in the source announcement, upstream of anything spatial_engine.py controls).
    This test locks in that it fails *safe* (falls back to the full unclipped street, still
    within Pančevo) rather than throwing or producing something wildly wrong.
    """
    feat = get_street_geojson_feature("Т.РАЈИЋА", "ПАНЧЕВО", "ПАНЧЕВО", "Ј.БЕЗУЉЕВИЋА", "СИНЂЕЛИЋЕВЕ")
    assert feat is not None
    # Sanity bound: stays within Pančevo, not off in another town.
    coords = feat["geometry"]["coordinates"]
    sample = coords[0] if feat["geometry"]["type"] == "LineString" else coords[0][0]
    lon, lat = sample
    assert 44.7 < lat < 45.0 and 20.5 < lon < 20.8


@requires_street_db
def test_pancevo_t_rajica_sibling_segment_clips_correctly():
    """The second segment in the same outage #12 — Синђелићева од Т.Рајића до Книћанинове —
    has both cross-streets resolvable and should clip tightly, same as the locked cases above."""
    feat = get_street_geojson_feature("СИНЂЕЛИЋЕВА", "ПАНЧЕВО", "ПАНЧЕВО", "Т.РАЈИЋА", "КНИЋАНИНОВЕ")
    assert feat is not None
    length = feature_length_m(feat)
    assert 0 < length < 500


# ---------------------------------------------------------------------------
# Settlement-name cleaning fix — outage #20
# ---------------------------------------------------------------------------

@requires_street_db
def test_stara_pazova_vinogradi_is_cleaned_but_never_placed_in_another_town():
    """
    Outage #20: 'МЕСТО СЛ.Виногради'. clean_settlement() must strip the "СЛ." prefix (the raw
    string never geocoded at all). This test used to go further and assert the cleaned name
    RESOLVES - it did, but to the Vinogradi in Kragujevac (~110 km away): "Виногради" is a
    generic toponym and the geocoder's answer carried no address to validate. A settlement must
    now land inside its parent opština or not resolve at all.
    """
    from spatial.spatial_engine import clean_settlement, envelope_distance_km, h3, region_envelope
    assert clean_settlement("СЛ.ВИНОГРАДИ") == "ВИНОГРАДИ"
    hex_id, radius = get_settlement_info("СЛ.ВИНОГРАДИ", "СТАРА ПАЗОВА")
    if hex_id is not None:
        env = region_envelope("СТАРА ПАЗОВА")
        assert envelope_distance_km(env, *h3.cell_to_latlng(hex_id)) <= 0


# ---------------------------------------------------------------------------
# Cross-contamination sanity — settlement resolution shouldn't drift to a distant town
# ---------------------------------------------------------------------------

@requires_street_db
def test_novi_knezevac_prema_siget_clips_toward_correct_direction():
    """
    Outage #10: 'Војводе Стевана Књићанина (од Ђуре Јакшића према Сигету)' — "od X prema Y"
    describes a DIRECTION toward a neighboring settlement (Сигет), not a cross-street bound.
    "Сигету" is also the dative-case inflection of "Сигет" and fails to geocode directly.
    Before this fix this fell through to a ~200m arbitrary-direction buffer past Đure Jakšića;
    it should now resolve the real Đure Jakšića intersection as one endpoint and extend toward
    whichever end of the street is geographically closer to Siget (west of Banatsko Aranđelovo).
    """
    feat = get_street_geojson_feature(
        "ВОЈВОДЕ СТЕВАНА КНИЋАНИНА", "БАНАТСКО АРАНЂЕЛОВО", "НОВИ КНЕЖЕВАЦ", "ЂУРЕ ЈАКШИЋА", "СИГЕТУ"
    )
    assert feat is not None
    coords = feat["geometry"]["coordinates"]
    djure_jaksica = (20.2408518, 46.0693527)

    def near(pt, target, tol=1e-4):
        return abs(pt[0] - target[0]) < tol and abs(pt[1] - target[1]) < tol

    start_is_djure = near(coords[0], djure_jaksica)
    end_is_djure = near(coords[-1], djure_jaksica)
    # One endpoint must be the real Đure Jakšića intersection point.
    assert start_is_djure or end_is_djure
    other = coords[-1] if start_is_djure else coords[0]
    # The other endpoint should move *towards* Siget (west, lon ~20.09) from there, not sit
    # within a tiny ~200m arbitrary buffer of Đure Jakšića.
    assert other[0] < djure_jaksica[0]  # moved west, towards Siget


@requires_street_db
def test_cantavir_resolves_near_itself_not_zmajevo():
    """
    Regression lock, not a fix per se — Čantavir and Zmajevo are ~45km apart and share several
    common (socialist-era) street names. Confirms settlement resolution stays anchored to the
    correct town rather than drifting to a same-named-street neighbor.
    """
    hex_id, _ = get_settlement_info("ЧАНТАВИР", "СУБОТИЦА")
    assert hex_id is not None
    import h3
    lat, lon = h3.cell_to_latlng(hex_id)
    # Real Čantavir ~45.92,19.76; real Zmajevo ~45.53,19.62 — assert we're near the former.
    assert 45.8 < lat < 46.0 and 19.6 < lon < 19.9


def test_a_like_hit_must_really_be_the_named_street():
    """Outage #4264 (10.10.2026) named a street "Нова". get_smart_stem cut it to "N", the LIKE
    pattern became '%N%', and the map drew 515 Novi Sad streets. Every pair below is a real
    announced street against a real RGZ name the old LIKE lookup returned for it."""
    from spatial.spatial_engine import like_hit_fits, _rows_that_fit
    for query, hit in (("НОВА", "PREŠERNOVA"), ("НОВА", "ALBERTA AJNŠTAJNA"), ("НОВА", "NOVA 8"),
                       ("НОВА 8", "NOVA 78"), ("МАЛА", "MALINA"), ("КИСАЧ", "KISAČKA"),
                       ("ЈНА", "ALBERTA AJNŠTAJNA"), ("Р.ПАВЛОВИЋА", "JOVANA PAVLOVIĆA"),
                       ("П.СМИЉАНИЋА", "PUKOVNIKA KRSTE SMILJANIĆA"), ("ПРВА", "PRVOMAJSKA"),
                       ("I ВИНОГРАДИ", "ISTOČNI VINOGRADI"), ("КРАЉА ПЕТРА I", "TRG KRALJA PETRA II KARAĐORĐEVIĆA"),
                       ("ПОПОВИЋА", "DUŠANA POPOVIĆA"), ("ПРОЛЕТЕРСКА", "PROLETERSKIH BRIGADA")):
        assert not like_hit_fits(query, hit), (query, hit)
    # what the LIKE lookup is for still works
    for query, hit in (("ТАКОВСКЕ", "TAKOVSKA"), ("22 АВГУСТА", "22. AVGUSTA"),
                       ("5 ВИНОГРАДИ", "VINOGRADI 5"), ("ПРВИ ВИНОГРАДИ", "VINOGRADI 1"),
                       ("ОСМОГ МАРТА", "8. MARTA"), ("ГЕНЕРАЛ КУТУЗОВА", "GENERALA KUTUZOVA"),
                       ("ЈУРАЈА АРНОЛДА", "ARNOLD JURAJA"), ("ПЕТЕФИ ШАНДОРА", "ŠANDORA PETEFIJA"),
                       ("НАЂ ИМРЕ", "IMREA NAĐA"), ("КОСАНЧИЋ И.", "KOSANČIĆ IVANA"),
                       ("П ДРАПШИНА", "PETRA DRAPŠINA"), ("П.СМИЉАНИЋА", "PROTE SMILJANIĆA"),
                       ("МАТЕ ЈЕРКОВИЋА", "ADMIRALA MATA JERKOVIĆA"), ("7. ЈУЛИ", "7.JULA"),
                       ("15. АВГУСТ", "15. AVGUSTA 1942."), ("ПЕТОГ ПУКА", "PETI PUK"),
                       ("БРАНКА РАДИЧЕВИЋА", "BRANKA V. RADIČEVIĆA"), ("ОСЛОБОЂЕЊА", "BULEVAR OSLOBOĐENJA"),
                       ("ДЕСЕТА НОВА", "10. NOVA"), ("МИНЕ К.", "MINE KARADŽIĆ")):
        assert like_hit_fits(query, hit), (query, hit)
    # a lone surname or possessive names a fuller street only when the lookup found no other
    assert _rows_that_fit("НУШИЋЕВА", [("g", "BRANISLAVA NUŠIĆA")]) == [("g",)]
    assert _rows_that_fit("ПИГОВА", [("g", "MAJORA MARINKOVIĆA PIGA")]) == [("g",)]
    assert _rows_that_fit("ПОПОВИЋА", [("a", "RAJE POPOVIĆA"), ("b", "SAVE POPOVIĆA GEMBEŠA")]) == []
