"""No announcement may ever notify or draw a place outside the area it is about.

Two layers, both tested here:
- the RGZ street lookup is scoped to the announced opština (a naselje name alone is not unique -
  outage #2365: Smederevo's "Вучак" also matched the Vučak under Kruševac, 125 km away);
- the leak gate in core/matchmaker.py drops any hex / drawn line outside the announced area,
  whatever resolver produced it, and records the block for the operator.
"""
import math
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from spatial.spatial_engine import (
    RGZ_DB_PATH, envelope_distance_km, h3, query_rgz_streets, region_envelope, region_opstine,
)

requires_rgz = pytest.mark.skipif(not os.path.exists(RGZ_DB_PATH), reason="data/rgz.db not present")

SMEDEREVO = (44.664, 20.928)


def _km_from(point, lat, lon):
    return math.hypot((lat - point[0]) * 111, (lon - point[1]) * 111 * math.cos(math.radians(lat)))


# --- layer 1: the lookup itself -----------------------------------------------------------------

@requires_rgz
def test_same_named_village_resolves_only_in_the_announced_opstina():
    # RGZ has a Vučačka street only in Kruševac's Vučak - the old unscoped lookup returned it
    # for a Smederevo announcement. Scoped, there is none to find in Smederevo.
    assert query_rgz_streets("ВУЧАЧКА", naselje="ВУЧАК", opstina="СМЕДЕРЕВО") == []
    # ...while a street Smederevo's Vučak really has still resolves, and only there.
    segs = query_rgz_streets("КАРАЂОРЂЕВА", naselje="ВУЧАК", opstina="СМЕДЕРЕВО")
    assert segs
    assert all(_km_from(SMEDEREVO, pt[1], pt[0]) < 20 for seg in segs for pt in seg)


@requires_rgz
def test_real_opstina_rgz_doesnt_carry_gets_no_rgz_rows():
    # Aleksandrovac isn't loaded in RGZ; any "Вучак" RGZ knows belongs to another opština.
    assert region_opstine("АЛЕКСАНДРОВАЦ") is None
    assert query_rgz_streets("ВУЧАЧКА", naselje="ВУЧАК", opstina="АЛЕКСАНДРОВАЦ") == []


@requires_rgz
def test_village_named_as_parent_still_resolves_when_unambiguous():
    """Registered addresses often carry the village as their "city" and no opština."""
    assert query_rgz_streets("5 ВИНОГРАДИ", naselje="ЧАНТАВИР", opstina="ЧАНТАВИР")


@requires_rgz
def test_ambiguous_village_named_as_parent_is_not_guessed():
    # Seven opštine have a naselje called Slatina - without an opština there is no telling which.
    assert query_rgz_streets("ГЛАВНА", naselje="СЛАТИНА", opstina="СЛАТИНА") == []


@requires_rgz
def test_region_opstine_forms():
    assert region_opstine("СМЕДЕРЕВО") == {"SMEDEREVO"}
    assert region_opstine("Град Београд") >= {"ZVEZDARA", "BARAJEVO", "NOVI BEOGRAD"}
    assert region_opstine("ПАЛИЛУЛА") == {"PALILULA (BEOGRAD)", "PALILULA (NIŠ)"}


# --- layer 2: the gate ---------------------------------------------------------------------------

@requires_rgz
def test_eps_opstina_with_village_in_text_resolves_and_passes_gate():
    """EPS's Opština column says "Нови Сад", the street text says Буковац - a village RGZ files
    under Novi Sad. Must resolve there and pass the gate (the owner's own worry case)."""
    novi_sad = region_envelope("НОВИ САД")
    segs = query_rgz_streets("22 АВГУСТА", naselje="БУКОВАЦ", opstina="НОВИ САД")
    assert segs
    assert all(envelope_distance_km(novi_sad, pt[1], pt[0]) <= 0 for s in segs for pt in s)


@requires_rgz
def test_eps_inline_village_as_region_is_placed_by_its_rgz_opstina():
    """EPS inline "Буковац: ..." can make the village the region. Its gate outline must come
    from the opština RGZ files it under - never from a geocode of a name that repeats."""
    from spatial.spatial_engine import naselje_owner_opstine
    assert naselje_owner_opstine("БУКОВАЦ") == {"NOVI SAD"}
    bukovac = (45.2033, 19.8961)
    assert envelope_distance_km(region_envelope("БУКОВАЦ"), *bukovac) <= 0


@requires_rgz
def test_gate_region_hint_anchors_a_swapped_region():
    """When EPS swaps the region for an inline place, the table's Opština anchors the gate."""
    from core import matchmaker as mm
    env = mm._announced_envelope("ТЕМЕРИН", gate_region="НОВИ САД")
    assert envelope_distance_km(env, 45.408, 19.887) <= 0     # Temerin itself (an opština)
    assert envelope_distance_km(env, 45.2551, 19.8451) <= 0   # Novi Sad
    assert envelope_distance_km(env, 43.5646, 21.2808) > 0    # Kruševac's Vučak - never


@requires_rgz
def test_palilula_named_with_its_city_resolves_only_in_that_city():
    """EPS writes "Палилула - Београд" (after _belgrade_opstina) and "Палилула - Ниш". Both used
    to return nothing: the sub-opština retry built "BEOGRAD (PALILULA (BEOGRAD))"."""
    from spatial.spatial_engine import get_rgz_naselje_streets
    belgrade, nis = region_envelope("БЕОГРАД"), region_envelope("НИШ")

    segs = query_rgz_streets("ВИШЊИЧКА", naselje="ПАЛИЛУЛА - БЕОГРАД", opstina="ПАЛИЛУЛА - БЕОГРАД")
    assert segs
    assert all(envelope_distance_km(belgrade, pt[1], pt[0]) <= 0 for s in segs for pt in s)

    segs = query_rgz_streets("LJUTICE BOGDANA", naselje="ПАЛИЛУЛА - НИШ", opstina="ПАЛИЛУЛА - НИШ")
    assert segs
    assert all(envelope_distance_km(nis, pt[1], pt[0]) <= 0 for s in segs for pt in s)

    # A village of Belgrade's Palilula, given with that opština (drop_unknown_settlements keeps it).
    assert get_rgz_naselje_streets("БОРЧА", "ПАЛИЛУЛА - БЕОГРАД")
    assert not get_rgz_naselje_streets("БОРЧА", "ПАЛИЛУЛА - НИШ")


@requires_rgz
def test_eps_opstina_spellings_normalise():
    assert region_opstine("Палилула - Ниш") == {"PALILULA (NIŠ)"}


def test_eps_opstina_names_are_known_without_rgz():
    # core/serbia_places.py alone - CI has no rgz.db.
    from spatial.spatial_engine import is_serbian_opstina
    assert is_serbian_opstina("Петровац") and is_serbian_opstina("Ражањ")


@requires_rgz
def test_explicitly_named_neighbour_opstina_is_allowed():
    """A Novi Sad utility announcing works in Sremski Karlovci names that opština outright - an
    explicit opština is not the same-named-village risk, so its area is allowed too."""
    from core import matchmaker as mm
    karlovci = (45.2028, 19.9336)
    env = mm._location_envelope("СРЕМСКИ КАРЛОВЦИ", "НОВИ САД", region_opstine("НОВИ САД"))
    assert envelope_distance_km(env, *karlovci) <= 0
    # A bare village name gets no such widening: it stays within the announced region.
    env_village = mm._location_envelope("ВУЧАК", "СМЕДЕРЕВО", region_opstine("СМЕДЕРЕВО"))
    assert envelope_distance_km(env_village, 43.5646, 21.2808) > 0


@requires_rgz
def test_gate_drops_far_hexes_and_records_it():
    from core import matchmaker as mm
    env = region_envelope("СМЕДЕРЕВО")
    near = h3.latlng_to_cell(*SMEDEREVO, 9)
    far = h3.latlng_to_cell(43.5646, 21.2808, 9)   # the Kruševac-side Vučak, outage #2365
    mm._leak_gate_events.clear()
    kept = mm._gate_hexes({near, far}, env, "test")
    assert kept == {near}
    assert mm._leak_gate_events and "outside the announced area" in mm._leak_gate_events[0]


@requires_rgz
def test_gate_drops_far_lines_from_a_drawn_feature():
    from core import matchmaker as mm
    env = region_envelope("СМЕДЕРЕВО")
    near_line = [[20.928, 44.664], [20.930, 44.665]]
    far_line = [[21.2808, 43.5646], [21.2806, 43.5642]]
    feat = {"type": "Feature", "properties": {},
            "geometry": {"type": "MultiLineString", "coordinates": [near_line, far_line]}}
    gated = mm._gate_feature(feat, env, "test")
    assert gated["geometry"]["coordinates"] == [near_line]
    only_far = {"type": "Feature", "properties": {},
                "geometry": {"type": "LineString", "coordinates": far_line}}
    assert mm._gate_feature(only_far, env, "test") is None


@requires_rgz
def test_smederevo_vucak_outage_never_leaves_smederevo():
    """The whole outage #2365 path - hexes that notify, lines that draw."""
    from core.matchmaker import _build_visual_features, calculate_outage_hexes
    from parser import OutageAlert, StreetSegment

    def alert():
        return OutageAlert(
            city="СМЕДЕРЕВО", outage_date="25.09.2026", message_template="x",
            affected_locations=[
                StreetSegment(location_type="STREET", main_street=s, settlement="ВУЧАК",
                              includes_surrounding_streets=True, time_window="08:00 - 14:00")
                for s in ("ВУЧАЧКА", "КАРАЂОРЂЕВА")])

    env = region_envelope("СМЕДЕРЕВО")
    hexes, _ = calculate_outage_hexes(alert())
    assert hexes
    assert all(envelope_distance_km(env, *h3.cell_to_latlng(h)) <= 0 for h in hexes)

    for f in _build_visual_features(alert(), "СМЕДЕРЕВО"):
        g = f["geometry"]
        lines = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        for line in lines:
            for lon, lat in (pt[:2] for pt in line):
                assert envelope_distance_km(env, lat, lon) <= 0
