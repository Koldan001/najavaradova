"""Streets with no place of their own are placed where the rest of their notice is
(core/matchmaker._place_streets_by_their_group). Outage #4264, 12.10.2026."""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core import matchmaker as mm
from parser import OutageAlert, StreetSegment
from spatial.spatial_engine import RGZ_DB_PATH

requires_rgz = pytest.mark.skipif(not os.path.exists(RGZ_DB_PATH), reason="data/rgz.db not present")

VETERNIK_NOTICE = ["ОЛГЕ ИЛИЋ", "ДР. ДУШАНА ПОПОВИЋА", "МАРКА СЕРВИЦКОГ", "ДАНИЛА ЛАЗОВИЋА", "НОВА",
                   "ДОБРЕ ЈОВАНОВИЋА", "ДРАГОСЛАВА СРЕЈОВИЋА"]


def _alert(*streets, city="НОВИ САД"):
    return OutageAlert(
        city=city, outage_date="12.10.2026", message_template="x",
        affected_locations=[
            StreetSegment(location_type="STREET", main_street=s, includes_surrounding_streets=False,
                          time_window="09:00 - 10:30")
            for s in streets])


def _fake_homes(monkeypatch, homes):
    monkeypatch.setattr(mm, "_rgz_available", lambda: True)
    monkeypatch.setattr(mm, "rgz_street_naselja", lambda street, region: homes[street])


def test_a_street_goes_where_the_rest_of_its_notice_is(monkeypatch):
    _fake_homes(monkeypatch, {
        "A": {"VETERNIK": "ВЕТЕРНИК"}, "B": {"VETERNIK": "ВЕТЕРНИК"},
        "TWIN": {"VETERNIK": "ВЕТЕРНИК", "NOVI SAD": "НОВИ САД"},  # same name in both
        "NEW": {},                                                # in neither
    })
    alert = _alert("A", "B", "TWIN", "NEW")
    assert mm._place_streets_by_their_group(alert) == [
        "TWIN -> ВЕТЕРНИК", "NEW -> ВЕТЕРНИК (not in RGZ there: left unresolved)"]
    assert [l.settlement for l in alert.affected_locations] == ["ВЕТЕРНИК"] * 4


def test_no_group_no_placement(monkeypatch):
    # streets pointing to two places (one street only in Futog makes it a mixed notice), one lone
    # pointer, or the town itself: nothing moves
    for homes in ({"A": {"VETERNIK": "ВЕТЕРНИК"}, "B": {"VETERNIK": "ВЕТЕРНИК"}, "TWIN": {"FUTOG": "ФУТОГ"}},
                  {"A": {"VETERNIK": "ВЕТЕРНИК"}, "B": {"FUTOG": "ФУТОГ"}, "TWIN": {"VETERNIK": "ВЕТЕРНИК", "FUTOG": "ФУТОГ"}},
                  {"A": {"VETERNIK": "ВЕТЕРНИК"}, "B": {}, "TWIN": {"VETERNIK": "ВЕТЕРНИК", "FUTOG": "ФУТОГ"}},
                  {"A": {"NOVI SAD": "НОВИ САД"}, "B": {"NOVI SAD": "НОВИ САД"}, "TWIN": {"VETERNIK": "ВЕТЕРНИК", "NOVI SAD": "НОВИ САД"}}):
        _fake_homes(monkeypatch, homes)
        alert = _alert("A", "B", "TWIN")
        assert mm._place_streets_by_their_group(alert) == []
        assert all(l.settlement is None for l in alert.affected_locations), homes


def test_a_street_with_its_own_place_is_left_alone(monkeypatch):
    _fake_homes(monkeypatch, {"A": {"VETERNIK": "ВЕТЕРНИК"}, "B": {"VETERNIK": "ВЕТЕРНИК"},
                              "TWIN": {"VETERNIK": "ВЕТЕРНИК", "NOVI SAD": "НОВИ САД"}})
    alert = _alert("A", "B", "TWIN")
    alert.affected_locations[2].settlement = "НОВИ САД"
    mm._place_streets_by_their_group(alert)
    assert alert.affected_locations[2].settlement == "НОВИ САД"


@requires_rgz
def test_outage_4264_is_drawn_in_veternik_only():
    alert = _alert(*VETERNIK_NOTICE)
    mm._place_streets_by_their_group(alert)
    placed = {l.main_street: l.settlement for l in alert.affected_locations}
    assert placed["ДОБРЕ ЈОВАНОВИЋА"] == "ВЕТЕРНИК"
    assert placed["НОВА"] == "ВЕТЕРНИК"
    hexes, quality = mm.calculate_outage_hexes(alert)
    assert hexes and quality == "UNRESOLVED"  # "Нова" and two new streets aren't in RGZ yet
    # every drawn hex lies within ~2.5 km of Veternik's centre (45.2545, 19.7650)
    import h3
    centre = h3.latlng_to_cell(45.2545, 19.7650, 9)
    assert max(h3.grid_distance(h, centre) for h in hexes) <= 8


@requires_rgz
def test_a_novi_sad_notice_stays_in_novi_sad():
    alert = _alert("БУЛЕВАР ОСЛОБОЂЕЊА", "ЖЕЛЕЗНИЧКА", "ДОБРЕ ЈОВАНОВИЋА")
    mm._place_streets_by_their_group(alert)
    assert all(l.settlement is None for l in alert.affected_locations)
