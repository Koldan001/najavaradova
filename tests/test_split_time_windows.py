"""One notice that lists separate events at different times is stored as separate outages, for
every scraper (core/matchmaker.process_and_queue_outage). 20.10.2026, ViK Novi Sad: "Kovilj low
pressure 9-10.30" and "Slavka Rodića no water 8-12" were one outage - one shape on the map, one
popup, both at 09:00 - 10:30. Geometry and RGZ are stubbed, so this runs in CI."""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm  # noqa: E402
from parser import OutageAlert, StreetSegment  # noqa: E402

DAY = (date.today() + timedelta(days=1)).strftime("%d.%m.%Y")
RAW = (f"Датум: {DAY}. Због радова Електродистрибуције Нови Сад Ковиљ ће од 9 до 10.30 сати "
       "имати смањен притисак у водоводној мрежи. Због планираних радова на водоводној мрежи без "
       "воде ће од 8 до 12 сати бити Улица Славка Родића у Сремској Каменици.")


def _alert(template=f"Planirano isključenje vode u ulici *** dana {DAY}. u periodu 08:00 - 12:00."):
    return OutageAlert(city="НОВИ САД", outage_date=DAY, message_template=template, affected_locations=[
        StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement="КОВИЉ", time_window="09:00 - 10:30"),
        StreetSegment(location_type="STREET", main_street="СЛАВКА РОДИЋА", settlement="СРЕМСКА КАМЕНИЦА",
                      time_window="08:00 - 12:00"),
    ])


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "split.db"))
    database.init_db()
    monkeypatch.setattr(mm, "calculate_outage_hexes", lambda *a, **k: (["89aaaaaaaaaaaaa"], "EXACT"))
    monkeypatch.setattr(mm, "_build_visual_features", lambda *a, **k: [])
    monkeypatch.setattr(mm, "query_rgz_streets", lambda *a, **k: [])
    monkeypatch.setattr(mm, "send_operator_note", lambda *a, **k: None)
    return database


def _rows(database):
    conn = database.get_connection()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, time_window, affected_streets, raw_text_hash FROM outages ORDER BY id")]
    conn.close()
    return rows


def test_two_events_two_outages_each_with_its_own_time(db):
    mm.process_and_queue_outage(_alert(), RAW, text_hash="h", category="WATER")
    rows = _rows(db)
    assert [(r["time_window"], r["raw_text_hash"]) for r in rows] == [
        ("09:00 - 10:30", "h"), ("08:00 - 12:00", "h#2")]
    assert "СЛАВКА РОДИЋА" not in rows[0]["affected_streets"]
    assert "СЛАВКА РОДИЋА" in rows[1]["affected_streets"]


def test_rescrape_of_the_same_notice_adds_nothing(db):
    from scrapers.eps_scraper import is_hash_processed
    mm.process_and_queue_outage(_alert(), RAW, text_hash="h", category="WATER")
    assert is_hash_processed("h")              # what every scraper checks before parsing again
    mm.process_and_queue_outage(_alert(), RAW, text_hash="h", category="WATER")
    assert len(_rows(db)) == 2


def test_one_time_for_everything_stays_one_outage(db):
    alert = _alert()
    for loc in alert.affected_locations:
        loc.time_window = "08:00 - 12:00"
    mm.process_and_queue_outage(alert, RAW, text_hash="h", category="WATER")
    assert [r["raw_text_hash"] for r in _rows(db)] == ["h"]


def test_template_with_the_other_events_hours_is_dropped_own_kept():
    kovilj, slavka = mm._split_by_time_window(_alert(), RAW)
    assert kovilj.message_template is None                      # it quoted 08:00 - 12:00
    assert slavka.message_template.endswith("08:00 - 12:00.")   # its own hours: kept
    assert [loc.settlement for loc in kovilj.affected_locations] == ["КОВИЉ"]


def test_a_location_without_hours_goes_with_the_first_event():
    alert = _alert()
    alert.affected_locations.append(StreetSegment(location_type="STREET", main_street="ФУТОШКА",
                                                  time_window="N/A"))
    first, second = mm._split_by_time_window(alert, RAW)
    assert [loc.main_street for loc in first.affected_locations] == [None, "ФУТОШКА"]
    assert len(second.affected_locations) == 1
