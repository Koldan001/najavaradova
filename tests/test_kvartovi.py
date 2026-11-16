"""Neighbourhood pages: which kvartovi an outage touches (core/kvartovi.py), the feed's per-kvart
history (build_web_data.history) and the pages built from it (build_city_pages). A small made-up
Novi Sad stands in for spatial/kvartovi.json: Liman drawn as an area, Telep and Detelinara as
points 2.6 km apart."""
import json
import os
import sys
from datetime import date

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.kvartovi as kv
from scripts import build_city_pages as cp
from scripts import build_web_data as bwd

LIMAN = [[19.840, 45.240], [19.852, 45.240], [19.852, 45.248], [19.840, 45.248], [19.840, 45.240]]
TELEP = [45.245, 19.800]
DETELINARA = [45.262, 19.820]
DATA = {"kvartovi": [
    {"id": "novi-sad/liman", "name": "Liman", "city": "Novi Sad", "poly": [LIMAN]},
    {"id": "novi-sad/telep", "name": "Telep", "city": "Novi Sad", "pt": TELEP},
    {"id": "novi-sad/detelinara", "name": "Detelinara", "city": "Novi Sad", "pt": DETELINARA},
]}


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    path = tmp_path / "kvartovi.json"
    path.write_text(json.dumps(DATA), encoding="utf-8")
    monkeypatch.setattr(kv, "PATH", str(path))
    monkeypatch.setattr(cp, "KVARTOVI_PATH", str(path))
    monkeypatch.setattr(bwd, "_kv_failed", False)
    kv._index.cache_clear()
    yield
    kv._index.cache_clear()


def _geo(*geoms):
    return json.dumps({"type": "FeatureCollection",
                       "features": [{"type": "Feature", "properties": {}, "geometry": g} for g in geoms]})


def _line(*lnglat):
    return {"type": "LineString", "coordinates": [list(p) for p in lnglat]}


def _square(lng, lat, half):
    return {"type": "Polygon", "coordinates": [[[lng - half, lat - half], [lng + half, lat - half],
                                                [lng + half, lat + half], [lng - half, lat + half],
                                                [lng - half, lat - half]]]}


def test_a_street_inside_an_area_is_that_area():
    assert kv.for_geojson(_geo(_line((19.843, 45.242), (19.849, 45.246)))) == ["novi-sad/liman"]


def test_a_place_known_as_a_point_covers_what_is_nearest_to_it():
    near_telep = _line((19.803, 45.2455), (19.804, 45.246))              # ~300 m from Telep
    assert kv.for_geojson(_geo(near_telep)) == ["novi-sad/telep"]
    far = _line((19.760, 45.200), (19.761, 45.201))                      # ~6 km from anything
    assert kv.for_geojson(_geo(far)) == []


def test_a_street_between_two_points_is_in_both_where_it_passes():
    assert kv.for_geojson(_geo(_line((19.800, 45.245), (19.820, 45.262)))) == \
        ["novi-sad/detelinara", "novi-sad/telep"]


def test_a_whole_settlement_standing_in_names_no_neighbourhood():
    assert kv.for_geojson(_geo(_square(19.81, 45.25, 0.02))) == []      # ~3 x 4.5 km
    assert kv.for_geojson(_geo(_square(TELEP[1], TELEP[0], 0.001))) == ["novi-sad/telep"]


def test_broken_pieces_of_a_stored_shape_do_not_lose_the_rest():
    # #2: a zero-length piece made the whole shape name nothing.
    shape = _geo(_line((19.803, 45.2455), (19.803, 45.2455)),
                 {"type": "MultiLineString", "coordinates": [[[19.843, 45.242], [19.849, 45.246]]]})
    assert kv.for_geojson(shape) == ["novi-sad/liman", "novi-sad/telep"]


def test_other_towns_and_bad_input_name_nothing():
    assert kv.for_geojson(_geo(_line((20.68, 43.72), (20.69, 43.73)))) == []   # Kraljevo
    assert kv.for_geojson("not json") == [] and kv.for_geojson(None) == []


def test_the_maps_own_shapes_give_the_same_answer():
    assert kv.for_shapes([[[45.242, 19.843], [45.246, 19.849]]], []) == ["novi-sad/liman"]
    assert kv.for_shapes([], [{"dot": [45.2455, 19.803]}]) == ["novi-sad/telep"]


def _row(day, street, geo, category="WATER", region="НОВИ САД"):
    return {"outage_date": day.strftime("%d.%m.%Y"), "region": region, "category": category,
            "affected_streets": street, "geojson_geometry": geo}


def test_the_feed_keeps_each_past_outage_once_and_each_kvart_points_to_it():
    today = date(2026, 9, 30)
    both = _geo(_line((19.843, 45.242), (19.849, 45.246)), _line((19.803, 45.2455), (19.804, 45.246)))
    rows = [_row(date(2026, 9, 28), "ФУТОШКА", both),
            _row(date(2026, 9, 29), "БУЛЕВАР ОСЛОБОЂЕЊА", _geo(_line((19.843, 45.242), (19.849, 45.246)))),
            _row(today, "ЖЕЛЕЗНИЧКА", _geo(_line((19.843, 45.242), (19.849, 45.246))))]
    h = bwd.history(rows, today)
    assert h["kvartovi"]["novi-sad/liman"]["counts"] == {"VODA": 3}          # today's counts ...
    assert [h["kv_recent"][i]["street"] for i in h["kvartovi"]["novi-sad/liman"]["recent"]] == \
        ["Bulevar Oslobođenja", "Futoška"]                                    # ... but isn't "past"
    assert h["kvartovi"]["novi-sad/telep"]["recent"] == [1]
    assert len(h["kv_recent"]) == 2                                          # Futoška written once


def _hist(**counts):
    return {"kvartovi": {k: {"counts": {"VODA": n}, "recent": []} for k, n in counts.items()}, "kv_recent": []}


def test_a_kvart_needs_three_outages_for_a_page_and_keeps_it_while_it_has_any():
    names = cp.kvart_names()
    pages = cp.kvart_pages([], _hist(**{"novi-sad/liman": 3, "novi-sad/telep": 2}), names, {})
    assert list(pages) == ["novi-sad/liman"] and pages["novi-sad/liman"]["indexed"]
    published = {"novi-sad/liman": True, "novi-sad/telep": True}
    pages = cp.kvart_pages([], _hist(**{"novi-sad/liman": 1}), names, published)
    assert pages["novi-sad/liman"]["indexed"] and not pages["novi-sad/telep"]["indexed"]   # quiet: noindex


def _outage(id_, region="Novi Sad", kvs=("novi-sad/liman",)):
    return {"id": id_, "d": 0, "kind": "VODA", "region": region, "street": "Futoška", "streets": ["Futoška"],
            "date": "30.09.2026.", "time": "08:00 - 12:00", "summary": "Bez vode.", "kv": list(kvs)}


def test_a_kvart_page_reads_as_the_neighbourhood_of_its_city():
    html = cp.page_html("Liman", "novi-sad/liman", {0: [_outage(7)]}, "2026-09-30T10:00:00+00:00",
                        [("Telep", "novi-sad/telep")], ({"VODA": 4}, []), parent=("Novi Sad", "novi-sad"))
    assert "<title>Liman bez struje, vode ili grejanja? Najave danas — Novi Sad</title>" in html
    assert '<link rel="canonical" href="https://najavaradova.rs/grad/novi-sad/liman/">' in html
    assert '<a href="/grad/novi-sad/">Novi Sad</a>' in html and "OpenStreetMap" in html
    assert "Novi Sad · 30.09.2026." not in html                                # its own city isn't repeated
    assert "noindex" not in html
    quiet = cp.page_html("Telep", "novi-sad/telep", {}, "2026-09-30T10:00:00+00:00", [], ({}, []),
                         parent=("Novi Sad", "novi-sad"), noindex=True)
    assert cp._NOINDEX in quiet and "za ovo naselje" in quiet


def test_the_city_page_links_its_neighbourhoods():
    html = cp.page_html("Novi Sad", "novi-sad", {}, "2026-09-30T10:00:00+00:00", [], None,
                        naselja=[("Liman", "novi-sad/liman", 2)])
    assert '<a href="/grad/novi-sad/liman/">Liman <b>2</b></a>' in html


def _run(tmp_path, monkeypatch, feed):
    monkeypatch.setattr(cp, "WEB_DIR", str(tmp_path))
    monkeypatch.setattr(cp, "GRAD_DIR", str(tmp_path / "grad"))
    monkeypatch.setattr(cp, "load_feed", lambda args: feed)
    monkeypatch.setattr(sys, "argv", ["build_city_pages.py", "--from-feed", "https://x.test/outages.json"])
    assert cp.main() == 0
    return (tmp_path / "sitemap.xml").read_text(encoding="utf-8")


def test_pages_are_written_linked_and_listed(tmp_path, monkeypatch):
    feed = {"outages": [_outage(7)],
            "history": {"regions": {}, **_hist(**{"novi-sad/liman": 4, "novi-sad/telep": 1})}}
    sitemap = _run(tmp_path, monkeypatch, feed)
    page = tmp_path / "grad" / "novi-sad" / "liman" / "index.html"
    assert page.exists() and not (tmp_path / "grad" / "novi-sad" / "telep").exists()
    assert "https://najavaradova.rs/grad/novi-sad/liman/" in sitemap
    assert '<a href="/grad/novi-sad/liman/">Liman <b>1</b></a>' in \
        (tmp_path / "grad" / "novi-sad" / "index.html").read_text(encoding="utf-8")

    # A feed from before the VM tagged kvartovi leaves the pages and the sitemap entry alone.
    before = page.read_text(encoding="utf-8")
    sitemap = _run(tmp_path, monkeypatch, {"outages": [], "history": {"regions": {}}})
    assert page.read_text(encoding="utf-8") == before
    assert "https://najavaradova.rs/grad/novi-sad/liman/" in sitemap

    # Liman falls quiet: the page stays online, noindex, out of the sitemap.
    sitemap = _run(tmp_path, monkeypatch, {"outages": [], "history": {"regions": {}, **_hist()}})
    assert cp._NOINDEX in page.read_text(encoding="utf-8")
    assert "novi-sad/liman" not in sitemap


@pytest.mark.skipif(not os.path.exists(os.path.join(os.path.dirname(__file__), "..", "data", "rgz.db")),
                    reason="data/rgz.db not present")
def test_a_kvart_belongs_to_the_city_its_streets_are_in():
    """Beograd's box reaches across the Tamiš: Pančevo's Kotež 2 and Sodara were published as
    Beograd's neighbourhoods (10.10.2026)."""
    from scripts.fetch_kvartovi import place_by_rgz
    placed = place_by_rgz([
        {"id": "beograd/kotez-2", "name": "Kotež 2", "city": "Beograd", "pt": [44.882, 20.646]},
        {"id": "beograd/sodara", "name": "Sodara", "city": "Beograd", "pt": [44.863, 20.644]},
        {"id": "beograd/bolecka-mehana", "name": "Bolečka Mehana", "city": "Beograd", "pt": [44.736, 20.611]},
        {"id": "beograd/zvezdara-x", "name": "X", "city": "Beograd", "pt": [44.80, 20.49]},
    ])
    assert [(k["id"], k["city"]) for k in placed] == [
        ("pancevo/kotez-2", "Pančevo"), ("pancevo/sodara", "Pančevo"),
        ("beograd/bolecka-mehana", "Beograd"), ("beograd/zvezdara-x", "Beograd")]
