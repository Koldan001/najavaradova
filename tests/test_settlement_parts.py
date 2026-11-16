"""A whole-settlement location the notice says is only part of the place (core/matchmaker.py,
PART_MARKER). Real case #4865, 15.10.2026: "Блок 8 у Петроварадину" drew and would have alerted
the whole of Petrovaradin. Texts below are real notices. No RGZ data needed: the street count
that tells a village from a town is stubbed."""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm
from parser import OutageAlert, StreetSegment


def _loc(settlement, landmark=None):
    return StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement=settlement, landmark=landmark,
                         time_window="08:00 - 14:00")


@pytest.mark.parametrize("settlement, landmark, text, kind", [
    # Spots: a block, a hamlet, a centre, "kod" a building, a weekend zone.
    ("ПЕТРОВАРАДИН", "БЛОК 8", "без воде ће до 15 сати бити Блок 8 у Петроварадину.", "spot"),
    ("ПЕТРОВАРАДИН", None, "без воде ће до 15 сати бити Блок 8 у Петроварадину.", "spot"),
    ("ЗДРАВИЊЕ", None, "без воде ће бити Здравиње - засеок поток.", "spot"),
    ("САМАИЛА", None, "Ulice: Самаила - Центар села", "spot"),
    ("ТРНАВА", None, "Квар на водоводној мрежи у Паковраћу код „Еурошилда“ и у Трнави код поште.", "spot"),
    ("ПЕТРОВАЦ", None, "Ресник, Нови Милановац, Петровац - Викенд насеље, Цветојевац", "spot"),
    # Part of a place, no spot named.
    ("ЛОЗОВИК", None, "Ulice: Део села Лозовик", "part"),
    ("ДОЊИ МАТЕЈЕВАЦ", None, "Ulice: Део Доњег Матејевца", "part"),
    ("ОСТАТОВИЦА", None, "Ulice: Кијевац, део села Остатовица", "part"),
    ("GAGLOVO", None, "Ulice: насељено место Капиџија и део Гаглова", "part"),
    # The whole place.
    ("КИЈЕВАЦ", None, "Ulice: Кијевац, део села Остатовица", None),
    ("БЕГЕЧ", None, "без воде ће од 8.30 до 14 сати бити насеље Бегеч.", None),
    ("ЈАБУКА", None, "Ulice: Јабука цело село", None),
])
def test_what_a_notice_says_about_the_place(settlement, landmark, text, kind):
    assert mm._settlement_part(_loc(settlement, landmark), text)[0] == kind


def test_a_street_location_is_never_a_part():
    loc = StreetSegment(location_type="STREET", main_street="ФУТОШКА", settlement="НОВИ САД",
                        time_window="08:00 - 14:00")
    assert mm._settlement_part(loc, "део Футошке")[0] is None


def test_the_finding_is_written_into_the_location_for_map_message_and_backfill():
    alert = OutageAlert(city="ЈАГОДИНА", outage_date="09.09.2026", message_template="x",
                        affected_locations=[_loc("ЛОЗОВИК"), _loc("ЗДРАВИЊЕ")])
    mm._mark_settlement_parts(alert, "Део села Лозовик. Здравиње - засеок поток.")
    assert [l.landmark for l in alert.affected_locations] == [mm.PART_MARKER, "ZDRAVINJE - ZASEOK POTOK"]
    assert [mm._settlement_part(l)[0] for l in alert.affected_locations] == ["part", "spot"]


@pytest.mark.parametrize("streets, hexes, kind, whole", [
    (12, 18, "part", True),          # Lozovik: a village - everyone there is told
    (65, 81, "part", False),         # Kać: "part of Kać" alerts nobody on the town alone
    (None, 60, "part", True),        # not in RGZ, small
    (None, 400, "part", False),      # not in RGZ, big
    (12, 18, "spot", False),         # a spot is never the whole village
])
def test_village_or_town(monkeypatch, streets, hexes, kind, whole):
    monkeypatch.setattr(mm, "rgz_naselje_street_count", lambda *a, **k: streets)
    monkeypatch.setattr(mm, "is_serbian_opstina", lambda name: False)
    loc = _loc("ЛОЗОВИК", mm.PART_MARKER if kind == "part" else "ЦЕНТАР")
    assert mm._whole_settlement_not_meant(loc, "ЈАГОДИНА", set(range(hexes))) is (not whole)


def test_the_whole_settlement_stands_in_only_when_meant(monkeypatch, tmp_path):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "parts.db"))
    database.init_db()
    monkeypatch.setattr(mm, "rgz_naselje_street_count", lambda *a, **k: 133)
    import h3
    centre = h3.latlng_to_cell(45.2517, 19.8789, 9)                   # Petrovaradin
    town = set(sorted(h3.grid_disk(centre, 7))[:143])
    monkeypatch.setattr(mm, "get_settlement_coverage", lambda *a, **k: (town, "rgz_naselje"))
    monkeypatch.setattr(mm, "get_settlement_info", lambda *a, **k: (centre, 25))
    # CI has no region outlines; the leak gate has its own tests.
    monkeypatch.setattr(mm, "_gate_hexes", lambda hexes, env, label: set(hexes))
    alert = OutageAlert(city="НОВИ САД", outage_date="15.10.2026", message_template="x",
                        affected_locations=[_loc("ПЕТРОВАРАДИН", "БЛОК 8")])
    hexes, quality = mm.calculate_outage_hexes(alert, "Блок 8 у Петроварадину")
    assert len(hexes) <= 7                                            # a marker, not 143 hexes
    assert mm._match_eligible_locations(alert.affected_locations, quality) == []   # alerts nobody

    whole = OutageAlert(city="НОВИ САД", outage_date="16.10.2026", message_template="x",
                        affected_locations=[_loc("ПЕТРОВАРАДИН")])
    hexes, quality = mm.calculate_outage_hexes(whole, "без воде ће бити насеље Петроварадин")
    assert quality == "EXACT" and len(hexes) == 143

    # "deo" of a town is a spot too; the whole villages named beside it still alert.
    both = OutageAlert(city="НОВИ САД", outage_date="16.10.2026", message_template="x",
                       affected_locations=[_loc("БЕГЕЧ"), _loc("ПЕТРОВАРАДИН")])
    hexes, quality = mm.calculate_outage_hexes(both, "Бегеч и део Петроварадина")
    eligible = mm._match_eligible_locations(both.affected_locations, quality)
    assert [l.settlement for l in eligible] == ["БЕГЕЧ"]
    assert both.affected_locations[1].landmark == mm.PART_TOWN_MARKER


def test_the_message_says_part_of_the_village():
    alert = OutageAlert(city="ЈАГОДИНА", outage_date="09.09.2026",
                        message_template="Planirano isključenje struje u ulici *** dana 09.09.2026.",
                        affected_locations=[_loc("ЛОЗОВИК", mm.PART_MARKER)])
    text = mm._render_message(alert, alert.affected_locations[0], "Karađorđeva", "POWER")
    assert "u delu naselja Lozovik" in text
    fallback = OutageAlert(city="ЈАГОДИНА", outage_date="09.09.2026", message_template="",
                           affected_locations=[_loc("ЛОЗОВИК", mm.PART_MARKER)])
    assert "u delu naselja Lozovik" in mm._render_message(
        fallback, fallback.affected_locations[0], "Karađorđeva", "POWER")


def test_part_of_a_municipality_seat_is_a_town(monkeypatch):
    monkeypatch.setattr(mm, "rgz_naselje_street_count", lambda *a, **k: None)   # no RGZ data
    assert mm._whole_settlement_not_meant(_loc("КОСЈЕРИЋ", mm.PART_MARKER), "КОСЈЕРИЋ", set(range(40)))


def test_the_card_says_what_part():
    assert mm._area_label(_loc("ПЕТРОВАРАДИН", "БЛОК 8")) == "ПЕТРОВАРАДИН (deo naselja: БЛОК 8)"
    assert mm._area_label(_loc("ЛОЗОВИК", mm.PART_MARKER)) == "ЛОЗОВИК (deo naselja)"
    assert mm._area_label(_loc("КАЋ", mm.PART_TOWN_MARKER)) == "КАЋ (deo naselja)"
    assert mm._area_label(_loc("БЕГЕЧ")) == "БЕГЕЧ (celo naselje)"
    assert all(mm.AREA_LABEL.search(mm._area_label(_loc(s, l)))
               for s, l in [("ПЕТРОВАРАДИН", "БЛОК 8"), ("ЛОЗОВИК", mm.PART_MARKER), ("БЕГЕЧ", None)])
    from scripts.build_web_data import title_sr
    assert title_sr("ПЕТРОВАРАДИН (deo naselja: БЛОК 8)") == "Petrovaradin (deo naselja: Blok 8)"


@pytest.mark.parametrize("scope, text, kind", [
    ("SPOT", "без воде ће бити Петроварадин", "spot"),      # the model's own word counts
    ("PART", "без воде ће бити Лозовик", "part"),
    ("WHOLE", "Део села Лозовик", "part"),                 # but never wider than the text
    ("WHOLE", "без воде ће бити Лозовик", None),
])
def test_the_model_says_whole_part_or_spot(scope, text, kind):
    loc = StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement="ЛОЗОВИК" if "Лозовик" in text
                        else "ПЕТРОВАРАДИН", scope=scope, time_window="08:00 - 14:00")
    assert mm._settlement_part(loc, text)[0] == kind
