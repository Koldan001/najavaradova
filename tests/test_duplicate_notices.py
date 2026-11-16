"""A re-published notice must update the outage it already made, not put a second copy on the map
- also when the source spells a street differently the second time (#2652/#2626: BVK wrote
Bulevar Franše d'Eperea as "Д Епереа", then "Д Епереја"). Two real, different streets must
never be merged. Geometry and RGZ are stubbed, so this runs in CI."""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm
from parser import OutageAlert, StreetSegment

DAY = (date.today() + timedelta(days=1)).strftime("%d.%m.%Y")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "dup.db"))
    database.init_db()
    monkeypatch.setattr(mm, "calculate_outage_hexes", lambda *a, **k: (["89aaaaaaaaaaaaa"], "EXACT"))
    monkeypatch.setattr(mm, "_build_visual_features", lambda *a, **k: [])
    monkeypatch.setattr(mm, "query_rgz_streets", lambda *a, **k: [])
    return database


def _publish(*streets, category="WATER", day=DAY):
    alert = OutageAlert(city="БЕОГРАД", outage_date=day, message_template="Kvar u ulici ***.",
                        affected_locations=[StreetSegment(location_type="STREET", main_street=s,
                                                          time_window="08:00 - 14:00")
                                            for s in streets])
    raw = f"Kvar ({category}) {day} 08-14h: " + ", ".join(streets)
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category=category)


def _rows(database):
    conn = database.get_connection()
    rows = [r["affected_streets"] for r in conn.execute("SELECT affected_streets FROM outages ORDER BY id")]
    conn.close()
    return rows


def test_a_respelled_notice_updates_its_outage(db):
    _publish("БУЛЕВАР ФРАНШЕ Д ЕПЕРЕА")
    _publish("БУЛЕВАР ФРАНШЕ Д ЕПЕРЕЈА")
    assert _rows(db) == ["БУЛЕВАР ФРАНШЕ Д ЕПЕРЕЈА"]


def test_latin_and_punctuation_are_the_same_spelling(db):
    _publish("БУЛЕВАР ФРАНШЕ Д'ЕПЕРЕА", "ЗАХУМСКА")
    _publish("Bulevar Franse d Eperea", "Zahumska")
    assert len(_rows(db)) == 1


def test_two_real_streets_a_letter_apart_stay_two_outages(db, monkeypatch):
    monkeypatch.setattr(mm, "query_rgz_streets", lambda *a, **k: [[[20.0, 44.0], [20.1, 44.1]]])
    _publish("КРАЉА МИЛУТИНА")
    _publish("КРАЉА МИЛАНА")      # RGZ knows both
    assert len(_rows(db)) == 2


def test_short_names_must_match_exactly(db):
    _publish("ДУНАВСКА")
    _publish("ДУНАВСКИ")
    assert len(_rows(db)) == 2


def test_one_respelled_street_is_not_enough_when_the_list_differs(db):
    _publish("НЕБОЈШИНА", "БУЛЕВАР ФРАНШЕ Д ЕПЕРЕА")
    _publish("РАНКЕОВА", "БУЛЕВАР ФРАНШЕ Д ЕПЕРЕЈА")
    assert len(_rows(db)) == 2


def test_other_day_or_kind_is_another_outage(db):
    _publish("БУЛЕВАР ФРАНШЕ Д ЕПЕРЕА")
    _publish("БУЛЕВАР ФРАНШЕ Д ЕПЕРЕЈА", day=(date.today() + timedelta(days=2)).strftime("%d.%m.%Y"))
    _publish("БУЛЕВАР ФРАНШЕ Д ЕПЕРЕЈА", category="POWER")
    assert len(_rows(db)) == 3


@pytest.mark.parametrize("a, b, same", [
    ("БУЛЕВАР ФРАНШЕ Д ЕПЕРЕА", "БУЛЕВАР ФРАНШЕ Д ЕПЕРЕЈА", True),
    ("Бул. Франше д'Епереа", "Bul. Franše d Eperea", True),
    ("ЂУРЕ ЈАКШИЋА", "DJURE JAKSICA", True),
    ("НЕМАЊИНА", "НЕМАЊИЋА", False),                  # under 10 letters: exact only
    ("КНЕЗА МИЛОША", "КНЕЗА МИХАИЛА", False),          # 3 letters apart
    ("", "НЕБОЈШИНА", False),
])
def test_respelled(a, b, same, monkeypatch):
    monkeypatch.setattr(mm, "query_rgz_streets", lambda *a, **k: [])
    assert mm._respelled(a, b, "БЕОГРАД") is same
