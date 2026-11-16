"""How a whole-area location is drawn on the map (30.09.2026, Toplana Cerak's remont):
- a whole-area line with no name of its own is not drawn at all. #3347 put "all of Beograd" as
  one dot at the city's centre point, in the river at Ušće;
- a named kvart of Beograd or Niš (Cerak) is drawn as its outline, which is exactly who is told,
  while villages keep their dot."""
import json
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

from scripts import build_web_data as bwd
from spatial.spatial_engine import RGZ_DB_PATH

requires_rgz = pytest.mark.skipif(not os.path.exists(RGZ_DB_PATH), reason="data/rgz.db not present")


def _square(lat, lng, half=0.012):
    return [[[lng - half, lat - half], [lng + half, lat - half], [lng + half, lat + half],
             [lng - half, lat + half], [lng - half, lat - half]]]


def _area(tag, lat, lng, half=0.012):
    return {"type": "Feature", "properties": {"kind": "area", "area_type": "settlement", "settlement": tag},
            "geometry": {"type": "Polygon", "coordinates": _square(lat, lng, half)}}


PIN = {"type": "Feature", "properties": {"street": "PORUČNIKA SPASIĆA I MAŠERE", "kind": "street"},
       "geometry": {"type": "Point", "coordinates": [20.41305, 44.75953]}}


def _parsed(city, *locations):
    return json.dumps({"city": city, "affected_locations": list(locations)})


def _kinds(areas):
    return [k for a in areas for k in a if k in ("dot", "hull", "pin")]


def test_a_whole_area_without_a_name_is_not_drawn():
    geo = json.dumps({"type": "FeatureCollection",
                      "features": [PIN, _area("Beograd", 44.8248, 20.4285, half=0.2)]})
    parsed = _parsed("БЕОГРАД",
                     {"location_type": "STREET", "main_street": "ПОРУЧНИКА СПАСИЋА И МАШЕРЕ"},
                     {"location_type": "ENTIRE_SETTLEMENT", "settlement": None, "landmark": "ТОПЛАНА \"ЦЕРАК\""})
    _, areas, _ = bwd.extract_shapes_labelled(geo, parsed)
    assert _kinds(areas) == ["pin"]


def test_rows_without_a_stored_parse_are_drawn_as_before():
    geo = json.dumps({"type": "FeatureCollection", "features": [_area("Beograd", 44.8248, 20.4285, half=0.2)]})
    _, areas, _ = bwd.extract_shapes_labelled(geo, None)
    assert _kinds(areas) == ["dot"]


def test_the_map_never_builds_a_nameless_whole_area(monkeypatch):
    import core.matchmaker as mm
    from parser import OutageAlert, StreetSegment
    drawn = []
    monkeypatch.setattr(mm, "_location_envelope", lambda *a, **k: None)
    monkeypatch.setattr(mm, "get_settlement_polygon_hexes", lambda *a, **k: drawn.append(a) or {"89"})
    alert = OutageAlert(city="БЕОГРАД", outage_date="30.09.2026", message_template="x ***",
                        affected_locations=[StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement=None,
                                                          time_window="")])
    assert mm._build_visual_features(alert, "БЕОГРАД") == [] and drawn == []


@requires_rgz
def test_a_kvart_of_beograd_is_drawn_as_its_outline():
    geo = json.dumps({"type": "FeatureCollection", "features": [PIN, _area("Cerak", 44.7527, 20.4223)]})
    parsed = _parsed("БЕОГРАД", {"location_type": "ENTIRE_SETTLEMENT", "settlement": "ЦЕРАК"})
    _, areas, _ = bwd.extract_shapes_labelled(geo, parsed)
    assert sorted(_kinds(areas)) == ["hull", "pin"]


@requires_rgz
def test_a_village_keeps_its_dot():
    geo = json.dumps({"type": "FeatureCollection", "features": [_area("Padinska Skela", 44.95, 20.43)]})
    parsed = _parsed("ПАЛИЛУЛА - БЕОГРАД", {"location_type": "ENTIRE_SETTLEMENT", "settlement": "ПАДИНСКА СКЕЛА"})
    _, areas, _ = bwd.extract_shapes_labelled(geo, parsed)
    assert _kinds(areas) == ["dot"]
    village = _parsed("БАРАЈЕВО", {"location_type": "ENTIRE_SETTLEMENT", "settlement": "ВРБОВНО"})
    _, areas, _ = bwd.extract_shapes_labelled(
        json.dumps({"type": "FeatureCollection", "features": [_area("Vrbovno", 44.52, 20.40)]}), village)
    assert _kinds(areas) == ["dot"]
