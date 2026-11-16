"""build_web_data.build() visibility rules - specifically that a multi-day outage (one with an
expires_at set from an announced end date) stays on the map until expires_at passes, instead of
vanishing the day after its start date. Regression: a Novi Sad roadworks notice with a stated
later end date dropped off the map one day in.
"""
import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_GEO = json.dumps({
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "properties": {},
                  "geometry": {"type": "LineString", "coordinates": [[20.0, 45.0], [20.01, 45.01]]}}],
})
TODAY = date(2026, 9, 6)


@pytest.fixture()
def build(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "m.db"))
    database.init_db()

    import scripts.build_web_data as bwd
    monkeypatch.setattr(bwd, "OUTAGES_DB_PATH", database.DB_NAME)

    conn = database.get_connection()

    def add(hash_, outage_date, expires_at):
        conn.execute(
            "INSERT INTO outages (region, affected_streets, outage_date, time_window, raw_text, "
            "raw_text_hash, geojson_geometry, status, category, expires_at) "
            "VALUES ('НОВИ САД','Тестулица',?,?, 'radovi', ?, ?, 'ACTIVE','WORKS',?)",
            (outage_date, "08:00 - 16:00", hash_, _GEO, expires_at),
        )
        conn.commit()
        return conn.execute("SELECT id FROM outages WHERE raw_text_hash = ?", (hash_,)).fetchone()[0]

    def days_shown(oid):
        return sorted(o["d"] for o in bwd.build(today=TODAY)["outages"] if o["id"] == oid)

    yield add, days_shown
    conn.close()


def _d(days):
    return (TODAY + timedelta(days=days)).strftime("%d.%m.%Y")


def _ts(days):
    # expires_at as matchmaker stores it for an outage whose real last day is TODAY+days:
    # end-of-that-day + 3h grace = 02:59 the following morning. The day-tab maths has to recover
    # TODAY+days back out of that timestamp.
    return (TODAY + timedelta(days=days + 1, hours=2, minutes=59)).strftime("%Y-%m-%d %H:%M:%S")


def _ts_grace_only(days):
    # A same-day cut whose stated end (e.g. 22:00) + 3h grace tips just past midnight into day X.
    return (TODAY + timedelta(days=days, hours=1)).strftime("%Y-%m-%d %H:%M:%S")


def test_multiday_ongoing_spans_every_covered_tab(build):
    add, days_shown = build
    oid = add("multi_future", _d(-1), _ts(8))    # started yesterday, ends in 8 days
    assert days_shown(oid) == [0, 1, 2]          # Danas + Sutra + Prekosutra, not just Danas


def test_multiday_two_day_span_shows_on_two_tabs(build):
    add, days_shown = build
    oid = add("two_day", _d(0), _ts(1))          # ends end of tomorrow
    assert days_shown(oid) == [0, 1]


def test_same_day_cut_with_grace_past_midnight_stays_on_danas(build):
    add, days_shown = build
    # 07:00-22:00 today; 22:00 + 3h grace = 01:00 tomorrow. Must NOT leak onto the Sutra tab.
    oid = add("bg_water", _d(0), _ts_grace_only(1))
    assert days_shown(oid) == [0]


def test_multiday_outage_disappears_after_expires_at(build):
    add, days_shown = build
    oid = add("multi_over", _d(-3), _ts(-2))     # ended two days ago
    assert days_shown(oid) == []


def test_single_day_past_outage_still_drops(build):
    add, days_shown = build
    oid = add("single_yday", _d(-1), None)       # no end date -> single-day, yesterday
    assert days_shown(oid) == []


def test_single_day_today_outage_shows_only_on_danas(build):
    add, days_shown = build
    oid = add("single_today", _d(0), None)
    assert days_shown(oid) == [0]


def test_upcoming_single_day_shows_on_its_own_tab(build):
    add, days_shown = build
    oid = add("sutra", _d(1), None)              # single-day, tomorrow
    assert days_shown(oid) == [1]


def test_far_future_multiday_still_capped(build):
    add, days_shown = build
    oid = add("far", _d(30), _ts(40))           # starts in 30 days - beyond MAX_DAYS_AHEAD
    assert days_shown(oid) == []


def test_nakosutra_single_day_is_held_off_the_map(build):
    add, days_shown = build
    oid = add("nakosutra", _d(3), None)         # single-day, TODAY+3 (EPS "Dan 3")
    assert days_shown(oid) == []                # held until it slides into the 3-tab window


def test_prekosutra_single_day_still_shows(build):
    add, days_shown = build
    oid = add("prekosutra", _d(2), None)        # single-day, TODAY+2 - last visible tab
    assert days_shown(oid) == [2]


# --- summarize(): a road-closure-only notice must not claim a supply cut ---------------------

def test_summary_road_closure_only_flag_suppresses_supply_line():
    from scripts.build_web_data import summarize
    headline, effects = summarize("RADOVI", "RADOVI",
                                  "Planirani radovi. Izmenjen režim saobraćaja u zoni radova.",
                                  road_closure_only=True)
    assert effects[0] == "Bez prekida u snabdevanju."
    assert "Moguć prekid u snabdevanju." not in effects


def test_summary_no_supply_phrasing_detected_without_flag():
    from scripts.build_web_data import summarize
    _, effects = summarize("GREJANJE", "RADOVI",
                           "Радови на мрежи. Неће бити прекида у испоруци топлотне енергије.")
    assert effects[0] == "Bez prekida u snabdevanju."


def test_summary_real_supply_cut_still_warns():
    from scripts.build_web_data import summarize
    _, effects = summarize("VODA", "KVAR", "Kvar na vodovodnoj mreži, bez vode do 14h")
    assert effects[0] == "Bez vode u navedenom periodu."


def test_shapes_are_rounded_and_duplicate_lines_dropped():
    """The stored GeoJSON carries ~15 decimals and the same street piece once per affected
    location - the feed ships each line once, at ~1 m precision."""
    from scripts.build_web_data import COORD_DECIMALS, extract_shapes
    line = {"type": "LineString",
            "coordinates": [[19.8451234567891, 45.2551234567891], [19.8461234567891, 45.2561234567891]]}
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "geometry": line, "properties": {}}] * 3}
    lines, areas = extract_shapes(json.dumps(fc))
    assert areas == []
    assert lines == [[[45.25512, 19.84512], [45.25612, 19.84612]]]
    assert all(len(str(v).split(".")[1]) <= COORD_DECIMALS for pt in lines[0] for v in pt)


def test_rounding_collapses_repeated_points():
    from scripts.build_web_data import extract_shapes
    # Two points 10 cm apart round to the same 1 m cell; the line must not keep a zero-length hop.
    line = {"type": "LineString",
            "coordinates": [[19.845120001, 45.255120001], [19.845120002, 45.255120002], [19.846, 45.256]]}
    lines, _ = extract_shapes(json.dumps({"type": "FeatureCollection",
                                          "features": [{"geometry": line, "properties": {}}]}))
    assert lines == [[[45.25512, 19.84512], [45.256, 19.846]]]


def test_history_counts_the_past_month_per_region():
    from scripts.build_web_data import history

    def row(region, days_ago, category="WATER", streets="ПАРТИЗАНСКА", geo=_GEO):
        return {"region": region, "outage_date": _d(-days_ago), "category": category,
                "affected_streets": streets, "geojson_geometry": geo}

    rows = [
        row("НОВИ САД", 0),                                   # today: counted, not in "recent"
        row("НОВИ САД", 3), row("НОВИ САД", 3),               # re-published notice: one outage
        row("НОВИ САД", 5, category="POWER", streets="ФУТОШКА"),
        row("НОВИ САД", 31),                                  # outside the window
        row("НОВИ САД", 2, geo=None),                         # never on the map
        row("НОВИ САД", -1),                                  # upcoming, not history
        row("", 1),
        row("ЗВЕЗДАРА", 1),
    ]
    h = history(rows, TODAY)
    assert h["days"] == 30
    ns = h["regions"]["Novi Sad"]
    assert ns["counts"] == {"VODA": 2, "STRUJA": 1}
    assert [(r["date"], r["street"]) for r in ns["recent"]] == [
        ((TODAY - timedelta(days=3)).isoformat(), "Partizanska"),
        ((TODAY - timedelta(days=5)).isoformat(), "Futoška"),
    ]
    assert set(h["regions"]) == {"Novi Sad", "Zvezdara"}
    # Day by day for the city pages' strip, oldest first, today last: water today and 3 days
    # ago (2), power 5 days ago (1).
    days = ns["days"]
    assert len(days) == 31 and days[-1] == "2" and days[-4] == "2" and days[-6] == "1"
    assert days.count("0") == 28


def test_whole_settlement_note_stays_lower_case():
    from scripts.build_web_data import title_sr
    assert title_sr("БОГУТОВАЦ (celo naselje)") == "Bogutovac (celo naselje)"
    assert title_sr("БУЛЕВАР ОСЛОБОЂЕЊА") == "Bulevar Oslobođenja"
    assert title_sr("КРАЉА ПЕТРА И") == "Kralja Petra I"          # a numeral, not "and"
    assert title_sr("ПУТ ЗА ДРЕНОВАЦ") == "Put za Drenovac"
    assert title_sr("БРАНКА И ЈОВАНА") == "Branka i Jovana"


def test_house_letters_are_latin_in_the_street_list():
    from scripts.build_web_data import location_number_suffix
    assert location_number_suffix({"explicit_numbers": "1513, 151ж"}) == " (brojevi 1513, 151Ž)"
    assert location_number_suffix({"parity": "PARNA", "number_range": "2-12а"}) == " (parni brojevi 2-12A)"
