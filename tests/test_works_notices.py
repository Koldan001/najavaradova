"""Works notices on the map read what the source says, not what the parser had to fill in:
- no hours in the notice -> "ceo dan", not the parser's 07:00 - 19:00 (parser.py rule 8);
- "Bez tople vode." only when the notice says hot water stops, never next to "Bez prekida";
- the end date is the notice's own, not the day after it (the expiry grace).
Real case: #2863, Vojvođanskih brigada 11 (Novosadska toplana), 28.09. to 21.10.2026.
"""
import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.filters import states_time
from parser import OutageAlert, StreetSegment

NOTICE_2863 = (
    "ЈКП „Новосадска топлана“ Нови Сад у понедељак, 28. септембра 2026. године, почиње радове на "
    "изградњи прикључка топле воде за објекат у Улици војвођанских бригада број 11. За време "
    "извођења радова затвара се једна саобраћајна трака код броја 11, дозвољава се наизменично "
    "одвијање саобраћаја уз ограничење брзине на 30 km/h. Планирани рок за завршетак радова је "
    "21. октобар 2026. године. Наведени радови неће утицати на квалитет испоруке топлотне енергије "
    "током грејне сезоне."
)


@pytest.mark.parametrize("text, stated", [
    ("Радови од 08:00 до 14:00", True),
    ("od 8.00 do 14.00 časova", True),
    ("до 15 часова", True),
    ("prekid od 9h", True),
    ("нормализација у вечерњим часовима", True),
    ("радови ноћу", True),
    (NOTICE_2863, False),                              # dates and 30 km/h are not times of day
    ("Обавештење за дан 2026-09-07. Радови у Лончарској", False),
    ("Kvar u ulici Laze Kostića 22.10.2026.", False),
    ("", False),
    (None, False),
])
def test_states_time(text, stated):
    assert states_time(text) is stated


def test_works_card_does_not_contradict_itself():
    from scripts.build_web_data import summarize
    _, effects = summarize("RADOVI", "RADOVI", NOTICE_2863)
    assert effects[0] == "Bez prekida u snabdevanju."
    assert "Bez tople vode." not in effects
    assert "Izmenjen režim saobraćaja u zoni radova." in effects


def test_a_real_hot_water_cut_is_still_called_out():
    from scripts.build_web_data import summarize
    for text in ("Прекид испоруке топле воде у улици Цанкарева од 08:00 до 12:00.",
                 "Bez grejanja i tople vode u ulici Futoška.",
                 "Isključenje tople vode 26.08.2026."):
        _, effects = summarize("GREJANJE", "KVAR", text)
        # Said once: the first line when the notice names the cut (core.filters.heat_supply_cut).
        assert sum("tople vode" in e for e in effects) == 1, text


def test_parser_fill_in_hours_are_dropped_for_works():
    from core.matchmaker import _drop_invented_windows

    def alert(**kw):
        return OutageAlert(city="НОВИ САД", outage_date="28.09.2026", affected_locations=[
            StreetSegment(location_type="STREET", main_street="ВОЈВОЂАНСКИХ БРИГАДА",
                          time_window="07:00 - 19:00")], **kw)

    works = alert(end_date="21.10.2026")
    _drop_invented_windows(works, NOTICE_2863)
    assert works.affected_locations[0].time_window == ""
    OutageAlert.model_validate_json(works.model_dump_json())   # the stored parse still loads

    closure = alert(road_closure_only=True)
    _drop_invented_windows(closure, NOTICE_2863)
    assert closure.affected_locations[0].time_window == ""

    stated = alert(end_date="21.10.2026")
    _drop_invented_windows(stated, NOTICE_2863 + " Радови се изводе од 07 до 19 часова.")
    assert stated.affected_locations[0].time_window == "07:00 - 19:00"

    one_day_fault = alert()
    _drop_invented_windows(one_day_fault, "Kvar na mreži u ulici Vojvođanskih brigada.")
    assert one_day_fault.affected_locations[0].time_window == "07:00 - 19:00"


_GEO = json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                   "geometry": {"type": "LineString", "coordinates": [[19.84, 45.25], [19.85, 45.26]]}}]})
TODAY = date(2026, 9, 27)


@pytest.fixture()
def feed(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "w.db"))
    database.init_db()
    import scripts.build_web_data as bwd
    monkeypatch.setattr(bwd, "OUTAGES_DB_PATH", database.DB_NAME)
    conn = database.get_connection()

    def add(raw_text, window, start, end, category="WORKS", road_closure_only=True):
        parsed = json.dumps({"road_closure_only": road_closure_only})
        oid = conn.execute(
            "INSERT INTO outages (region, affected_streets, outage_date, time_window, raw_text, "
            "raw_text_hash, geojson_geometry, status, category, expires_at, parsed_json) "
            "VALUES ('НОВИ САД', 'ВОЈВОЂАНСКИХ БРИГАДА', ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
            (start.strftime("%d.%m.%Y"), window, raw_text, f"h{start}{end}{window}", _GEO, category,
             (end + timedelta(days=1)).strftime("%Y-%m-%d 02:59:00"), parsed)).lastrowid
        conn.commit()
        return [o for o in bwd.build(today=TODAY)["outages"] if o["id"] == oid]

    yield add
    conn.close()


def test_works_without_hours_show_all_day_and_their_own_end_date(feed):
    rows = feed(NOTICE_2863, "07:00 - 19:00", date(2026, 9, 28), date(2026, 10, 21))
    assert rows and all(o["time"] == "ceo dan" for o in rows)
    assert rows[0]["date"] == "28.09.–21.10."
    assert "Bez tople vode." not in rows[0]["effects"]


def test_works_that_state_hours_keep_them(feed):
    rows = feed("Радови од 08:00 до 16:00 сваког дана.", "08:00 - 16:00",
                date(2026, 9, 26), date(2026, 10, 3))
    assert rows and all(o["time"] == "08:00 - 16:00" for o in rows)
    assert rows[0]["date"] == "26.09.–03.10."


def test_all_day_works_are_live_today(feed):
    rows = feed(NOTICE_2863, "07:00 - 19:00", date(2026, 9, 20), date(2026, 10, 21))
    today = [o for o in rows if o["d"] == 0]
    assert today and today[0]["live"] is True


def test_toplana_works_that_cut_nothing_show_all_day(feed):
    # Beogradske elektrane's daily list (#3459): the card read 07:00 - 19:00, the notice has no hours.
    text = ("Beograd, opština Voždovac. Planirani radovi na toplovodnoj mreži (28.09.2026).\n"
            "- Radovi na izgradnji toplovodne mreže u Mitrovdanskoj i Ulici braće Jerković.")
    rows = feed(text, "07:00 - 19:00", date(2026, 9, 28), date(2026, 9, 28),
                category="HEATING", road_closure_only=False)
    assert rows and rows[0]["time"] == "ceo dan"
    assert rows[0]["effects"][0] == "Moguć prekid grejanja ili tople vode."


def test_a_toplana_cut_keeps_its_hours(feed):
    rows = feed("Prekid isporuke tople vode u ulici Futoška od 08:00 do 12:00.", "08:00 - 12:00",
                date(2026, 9, 28), date(2026, 9, 28), category="HEATING", road_closure_only=False)
    assert rows and rows[0]["time"] == "08:00 - 12:00"
    assert rows[0]["effects"][0] == "Bez tople vode u navedenom periodu."
