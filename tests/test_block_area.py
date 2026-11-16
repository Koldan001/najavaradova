"""A block inside a ring of streets (core/block_area.py) and a stretch "од X до Y" that only covers
part of its street (core/matchmaker.py's _outside_stretch). Synthetic streets, so it runs in CI
without the street registers."""
import pytest

from core import block_area as ba
from core import matchmaker as mm
from parser import OutageAlert

LAT0, LON0 = 44.78, 20.47
DLAT = 1 / 110_540                      # one metre north
DLON = 1 / (111_320 * 0.7101)           # one metre east at 44.78°


def at(x_m, y_m):
    """[lon, lat] x metres east, y metres north of the origin."""
    return [LON0 + x_m * DLON, LAT0 + y_m * DLAT]


def ll(x_m, y_m):
    """(lat, lon) of the same point."""
    lon, lat = at(x_m, y_m)
    return lat, lon


# A 200 x 200 m block; each street runs on past its corners, as real streets do.
STREETS = {
    "СЕВЕРНА": [[at(-300, 200), at(500, 200)]],
    "ИСТОЧНА": [[at(200, 500), at(200, -300)]],
    "ЈУЖНА": [[at(500, 0), at(-300, 0)]],
    "ЗАПАДНА": [[at(0, -300), at(0, 500)]],
}


@pytest.fixture
def streets(monkeypatch):
    found = dict(STREETS)
    monkeypatch.setattr(ba, "get_local_street_segments", lambda name, *a, **k: found.get(name))
    ba.block_polygon.cache_clear()
    ba.corner_point.cache_clear()
    yield found
    ba.block_polygon.cache_clear()
    ba.corner_point.cache_clear()


def test_square_block_inside_border_and_far(streets):
    shape = ba.block_polygon(("СЕВЕРНА", "ИСТОЧНА", "ЈУЖНА", "ЗАПАДНА"), None, "ВОЖДОВАЦ")
    assert shape is not None
    assert abs(shape[0].area - 40_000) < 500
    assert ba.where(shape, *ll(100, 100)) == "inside"
    assert ba.where(shape, *ll(100, 197)) == "inside"          # a house facing the block
    assert ba.where(shape, *ll(100, 230)) == "border"          # across the road
    assert ba.where(shape, *ll(450, 200)) is None              # same street, past the corner
    assert ba.block_cells(shape)


def test_one_open_corner_is_bridged(streets):
    streets["ИСТОЧНА"] = [[at(200, 500), at(200, 150)]]      # stops 150 m short of ЈУЖНА
    shape = ba.block_polygon(("СЕВЕРНА", "ИСТОЧНА", "ЈУЖНА", "ЗАПАДНА"), None, "ВОЖДОВАЦ")
    assert shape is not None
    assert ba.where(shape, *ll(100, 100)) == "inside"


def test_streets_out_of_order_make_no_block(streets):
    # СЕВЕРНА and ЈУЖНА are parallel: two "corners" 200 m apart is a list, not a ring.
    assert ba.block_polygon(("СЕВЕРНА", "ЈУЖНА", "ИСТОЧНА", "ЗАПАДНА"), None, "ВОЖДОВАЦ") is None


def test_unknown_street_makes_no_block(streets):
    assert ba.block_polygon(("СЕВЕРНА", "ИСТОЧНА", "ПУТ НИГДЕ"), None, "ВОЖДОВАЦ") is None


def test_far_corner_makes_no_block(streets):
    streets["ИСТОЧНА"] = [[at(600, 500), at(600, -300)]]     # 400 m off СЕВЕРНА's end
    streets["СЕВЕРНА"] = [[at(-300, 200), at(200, 200)]]
    assert ba.block_polygon(("СЕВЕРНА", "ИСТОЧНА", "ЈУЖНА", "ЗАПАДНА"), None, "ВОЖДОВАЦ") is None


def _alert(locations, city="ВОЖДОВАЦ"):
    return OutageAlert.model_validate({
        "event_type": "voda", "city": city, "outage_date": "04.11.2026",
        "message_template": "Isključenje vode u ulici *** zbog ispiranja vodovodne mreže.",
        "affected_locations": locations, "excluded_locations": []})


def _street(name, start=None, end=None, kind="STREET", numbers=None):
    return {"location_type": kind, "main_street": name, "intersection_start": start,
            "intersection_end": end, "explicit_numbers": numbers, "time_window": "22:00 - 06:00"}


RING_TEXT = "4/5.11.2026. – део рејона оивичен улицама Северна, Источна, Јужна и Западна."


def test_ring_is_marked_and_matched_by_where_the_house_stands(streets, monkeypatch):
    alert = _alert([_street("СЕВЕРНА", "ЗАПАДНА", "ИСТОЧНА"), _street("ИСТОЧНА"),
                    _street("ЈУЖНА"), _street(None, "ЗАПАДНА", kind="VECTOR")])
    mm._mark_block_ring(alert, RING_TEXT)
    ring = mm._block_ring(alert.affected_locations)
    assert [loc.main_street for loc in ring] == ["СЕВЕРНА", "ИСТОЧНА", "ЈУЖНА", "ЗАПАДНА"]
    monkeypatch.setattr(mm, "_block_shape", lambda r, region: ba.block_polygon(
        tuple(loc.main_street for loc in r), None, region))

    def target(street, point):
        return {"ulica": street, "broj": "1", "city": "Beograd", "lat": point[0], "lng": point[1]}

    inner = target("УНУТРАШЊА", ll(100, 100))
    assert mm._target_match(alert, alert.affected_locations, inner, "EXACT") is ring[0]
    # On a boundary street but far past the block: the street's name no longer matches it.
    assert mm._target_match(alert, alert.affected_locations, target("СЕВЕРНА", ll(450, 210)), "EXACT") is None
    assert mm._block_where(alert, ring, target("СЕВЕРНА", ll(100, 230))) == "border"

    told = mm._block_message(alert, ring, "WATER")
    assert "u bloku između ulica Severna, Istočna, Južna i Zapadna" in told
    assert "zbog ispiranja" in told and "04.11.2026" in told
    maybe = mm._block_message(alert, ring, "WATER", border_street="СЕВЕРНА")
    assert maybe.startswith("💧 Moguć prekid vode u blizini.")
    assert "Tvoja adresa je u ulici Severna, preko puta tog bloka" in maybe


@pytest.mark.parametrize("text, locations", [
    ("улица Северна, Источна и Јужна", [_street("СЕВЕРНА"), _street("ИСТОЧНА"), _street("ЈУЖНА")]),
    (RING_TEXT + " " + RING_TEXT, [_street("СЕВЕРНА"), _street("ИСТОЧНА"), _street("ЈУЖНА")]),
    (RING_TEXT, [_street("СЕВЕРНА"), _street("ИСТОЧНА")]),
    (RING_TEXT, [_street("СЕВЕРНА", numbers="2, 4"), _street("ИСТОЧНА"), _street("ЈУЖНА")]),
    (RING_TEXT, [_street("СЕВЕРНА"), _street("ИСТОЧНА"), _street("ЈУЖНА"),
                 {"location_type": "ENTIRE_SETTLEMENT", "settlement": "ВОЖДОВАЦ", "time_window": ""}]),
])
def test_not_a_ring(text, locations):
    alert = _alert(locations)
    mm._mark_block_ring(alert, text)
    assert mm._block_ring(alert.affected_locations) == []


def test_house_clear_of_its_stretch_is_not_alerted(monkeypatch):
    # Крунска runs 1 km east; the works are from 600 m to 800 m.
    monkeypatch.setattr(mm, "_clipped_segments", lambda *a: ([at(600, 0), at(800, 0)],))
    alert = _alert([_street("КРУНСКА", "МОЛЕРОВА", "КНЕГИЊЕ ЗОРКЕ")], city="БЕОГРАД")
    loc = alert.affected_locations[0]

    def house(x):
        return {"ulica": "КРУНСКА", "broj": "1", "lat": ll(x, 20)[0], "lng": ll(x, 20)[1]}

    assert mm._outside_stretch(alert, loc, house(50)) is True
    assert mm._outside_stretch(alert, loc, house(700)) is False
    assert mm._outside_stretch(alert, loc, house(840)) is False   # the corner house
    # Can't be placed: the name match stands.
    assert mm._outside_stretch(alert, loc, {"ulica": "КРУНСКА", "broj": "бб"}) is False


def test_stretch_that_cannot_be_cut_falls_back_to_its_corner(streets, monkeypatch):
    monkeypatch.setattr(mm, "_clipped_segments", lambda *a: ())
    # "од Западне до Северне" on Јужна: Северна never meets it, Западна does at (0, 0).
    alert = _alert([_street("ЈУЖНА", "ЗАПАДНА", "СЕВЕРНА")])
    loc = alert.affected_locations[0]

    def house(x):
        return {"ulica": "ЈУЖНА", "broj": "1", "lat": ll(x, -20)[0], "lng": ll(x, -20)[1]}

    assert mm._outside_stretch(alert, loc, house(60)) is False
    assert mm._outside_stretch(alert, loc, house(400)) is True
