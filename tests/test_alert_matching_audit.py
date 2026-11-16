"""The three alert-matching bugs found in the 2026-09-25 audit, and the re-match path that repairs
their effect on outages already stored:

- a region written "X - Y" (EPS Belgrade's "ПАЛИЛУЛА - БЕОГРАД") matched no target at all;
- a segment carrying both a number range and explicit numbers matched nobody on the street;
- a whole-village outage reached every street of the neighbouring town that touches the village.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import core.matchmaker as mm
from core.filters import is_house_number_affected as affected
from parser import OutageAlert, StreetSegment


# --- 1. dashed region names -------------------------------------------------------------------

def test_dashed_region_reaches_its_gradska_opstina_targets():
    palilula_bg = mm._region_token_set("Beograd (Palilula)", "PALILULA (BEOGRAD)", "BEOGRAD (PALILULA)")
    palilula_nis = mm._region_token_set("Niš (Palilula)", "PALILULA (NIŠ)", "NIŠ (PALILULA)")
    assert mm._region_variants("ПАЛИЛУЛА - БЕОГРАД") & palilula_bg
    assert mm._region_variants("Палилула - Ниш") & palilula_nis
    assert mm._region_variants("БЕОГРАД - ЗВЕЗДАРА") & mm._region_token_set(
        "Beograd (Zvezdara)", "ZVEZDARA", "BEOGRAD (ZVEZDARA)")


def test_dashed_region_still_separates_other_cities():
    assert not (mm._region_variants("ПАЛИЛУЛА - БЕОГРАД") & mm._region_token_set("Novi Sad", "NOVI SAD", "NOVI SAD"))


# --- 2. house numbers -------------------------------------------------------------------------

@pytest.mark.parametrize("num, parity, rng, exp, want", [
    ("145", "ALL", "1-19", None, False),
    ("15", "ALL", "1-19", None, True),
    ("15", "ALL", "19-1", None, True),            # a descending range still covers its span
    ("26", "ALL", "20-30", "5, 7", True),         # range OR explicit (#6 Srbobranska shape)
    ("5", "ALL", "20-30", "5, 7", True),
    ("9", "ALL", "20-30", "5, 7", False),
    ("11", "ALL", "14-32", "11, 13", True),
    ("40", "ALL", "14-32", "11, 13", False),
    ("45", "ALL", "1-19, 2-30", None, False),     # two ranges in one field, read as two
    ("24", "ALL", "1-19, 2-30", None, True),
    ("25", "ALL", "1-19, 2-30", None, False),     # 2-30 is the even side only
    ("14", "ALL", None, "12-16", True),           # a range written into explicit_numbers
    ("41", "PARNA", None, "41, 43", True),        # named outright: parity can't take it back
    ("45", "ALL", None, "41, 43, 50", False),
    ("8", "NEPARNA", "1-19", None, False),
    ("7", "NEPARNA", "1-19", None, True),
    ("30", "ALL", "od 20 do 40", None, True),
    ("50", "ALL", "od 20 do 40", None, False),
    ("3", "ALL", "20", None, True),               # unreadable range: whole street, as before
    ("3", "ALL", "1-19 i 25", None, True),
    # No house number (BB): a stretch or a list of numbers can't place it, so no alert (#6503,
    # 4 houses of 276 alerted a BB address); the whole street still reaches it.
    ("bb", "PARNA", "2-10", None, False),
    ("BB", "ALL", None, "48, 50, 52, 54", False),
    ("BB", "ALL", None, None, True),
    ("12a", "ALL", None, "12", True),
    ("100", "ALL", None, None, True),
    ("5", "ALL", None, "5 (ulazi 1-3)", True),    # a remark in brackets isn't a range
    ("2", "ALL", None, "5 (ulazi 1-3)", False),
    ("14", "ALL", None, "(12, 14)", True),        # brackets holding only numbers still count
    ("30", "ALL", None, "od 20 do 40", True),     # a range in words inside explicit_numbers
    ("30", "ALL", None, "од бр. 20 до 40", True),
    ("20", "ALL", "14‑32", None, True),           # non-breaking hyphen
    ("20", "ALL", "14−32", None, True),           # minus sign
    ("20", "ALL", "14-32 (parni)", None, True),
])
def test_house_number_scope(num, parity, rng, exp, want):
    assert affected(num, parity=parity, number_range=rng, explicit_numbers=exp) is want


# --- 3. whole-village outage vs the neighbouring town ------------------------------------------

@pytest.fixture()
def rgz_naselja(monkeypatch):
    """RGZ's answer to "which opštine have a naselje by this name", without rgz.db."""
    owners = {"VETERNIK": {"NOVI SAD"}, "ZUCE": {"VOŽDOVAC"}, "KLISA": {"SOMEWHERE ELSE"},
              "ČENEJ": {"NOVI SAD"}, "NOVI SAD": {"NOVI SAD"}}
    mm._naselje_opstine_cached.cache_clear()
    monkeypatch.setattr(mm, "_rgz_available", lambda: True)
    monkeypatch.setattr(mm, "naselje_owner_opstine", lambda n: owners.get(n, set()))
    yield owners
    mm._naselje_opstine_cached.cache_clear()


def _village(name):
    return StreetSegment(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement=name,
                         time_window="08:00 - 12:00")


def test_village_outage_skips_a_town_street_touching_it(rgz_naselja):
    loc = _village("ВЕТЕРНИК")
    assert not mm._location_matches_target(loc, "Futoški put", "12", "NOVI SAD", {"NOVI SAD"})
    assert mm._location_matches_target(loc, "Branka Radičevića", "3", "VETERNIK", {"NOVI SAD"})


def test_village_outage_across_a_belgrade_opstina_line(rgz_naselja):
    # Zuce is a naselje of Voždovac; a Zvezdara street touching it is not in Zuce.
    assert not mm._location_matches_target(_village("Зуце"), "Zaplanjska", "1",
                                           "BEOGRAD (ZVEZDARA)", {"VOŽDOVAC", "ZVEZDARA"})


def test_kvart_or_unknown_settlement_keeps_the_geometric_answer(rgz_naselja):
    # "Klisa" is a Novi Sad kvart; a village elsewhere sharing the name must not cut its alerts.
    assert mm._location_matches_target(_village("Клиса"), "Sentandrejski put", "5", "NOVI SAD", {"NOVI SAD"})
    assert mm._location_matches_target(_village("Детелинара"), "Rumenačka", "5", "NOVI SAD", {"NOVI SAD"})
    # no naselje on record for the target: nothing to compare, geometry decides
    assert mm._location_matches_target(_village("ВЕТЕРНИК"), "Futoški put", "12", None, {"NOVI SAD"})


def test_excluded_village_only_excludes_its_own_people(rgz_naselja):
    """An exclusion with no street ("osim naselja Veternik") used to match - and so exclude -
    every target. As an exclusion it now covers only targets known to live there."""
    excl = _village("ВЕТЕРНИК")
    assert not mm._location_matches_target(excl, "Futoški put", "12", "NOVI SAD", {"NOVI SAD"},
                                           exclusion=True)
    assert mm._location_matches_target(excl, "Branka Radičevića", "3", "VETERNIK", {"NOVI SAD"},
                                       exclusion=True)


def test_an_exclusion_in_doubt_keeps_the_alert(rgz_naselja):
    """"ceo Novi Sad osim Detelinare": Detelinara is a kvart, so who lives in it can't be told
    from the place on record - nobody is excluded rather than everybody."""
    assert not mm._location_matches_target(_village("Детелинара"), "Rumenačka", "5", "NOVI SAD",
                                           {"NOVI SAD"}, exclusion=True)
    assert not mm._location_matches_target(_village("ВЕТЕРНИК"), "Futoški put", "12", None,
                                           {"NOVI SAD"}, exclusion=True)
    landmark = StreetSegment(location_type="LANDMARK", main_street=None, settlement=None,
                             time_window="08:00 - 12:00")
    assert not mm._location_matches_target(landmark, "Futoški put", "12", "NOVI SAD",
                                           {"NOVI SAD"}, exclusion=True)


def test_whole_town_notice_still_reaches_its_suburbs(rgz_naselja):
    """A settlement named like its opština ("Нови Сад") is the whole town: geometry decides, as
    before, rather than cutting Veternik or Petrovaradin out of it."""
    assert mm._location_matches_target(_village("НОВИ САД"), "Branka Radičevića", "3", "VETERNIK",
                                       {"NOVI SAD"})


def test_place_typed_without_diacritics_is_still_the_village(rgz_naselja):
    """bulk_register stores a CSV's "Cenej" as typed; the notice says "ЧЕНЕЈ"."""
    assert mm._location_matches_target(_village("ЧЕНЕЈ"), "Salajka", "4", "Cenej", {"NOVI SAD"})
    assert not mm._location_matches_target(_village("ЧЕНЕЈ"), "Futoški put", "12", "Novi Sad",
                                           {"NOVI SAD"})


def test_rgz_not_answering_keeps_the_geometric_answer_and_is_not_cached(rgz_naselja, monkeypatch):
    import sqlite3

    def broken(n):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mm, "naselje_owner_opstine", broken)
    assert mm._location_matches_target(_village("ВЕТЕРНИК"), "Futoški put", "12", "NOVI SAD", {"NOVI SAD"})
    # once RGZ answers again, the real answer comes back - the failure wasn't cached
    monkeypatch.setattr(mm, "naselje_owner_opstine", lambda n: rgz_naselja.get(n, set()))
    assert not mm._location_matches_target(_village("ВЕТЕРНИК"), "Futoški put", "12", "NOVI SAD", {"NOVI SAD"})
    monkeypatch.setattr(mm, "_rgz_available", lambda: False)
    mm._naselje_opstine_cached.cache_clear()
    assert mm._location_matches_target(_village("ВЕТЕРНИК"), "Futoški put", "12", "NOVI SAD", {"NOVI SAD"})


# --- when an announcement is over ------------------------------------------------------------

def test_a_night_window_ends_the_next_morning():
    assert mm._compute_expires_at("26.09.2026", None, "22:00 - 06:00") == "2026-09-27 09:00:00"
    assert mm._compute_expires_at("26.09.2026", None, "08:00 - 14:00") == "2026-09-26 17:00:00"
    assert mm._compute_expires_at("26.09.2026", None, "08:00 - 10:00, 13:00 - 16:00") == "2026-09-26 19:00:00"
    assert mm._compute_expires_at("26.09.2026", None, "08:00 - 00:00") == "2026-09-27 03:00:00"
    # a morning start with an earlier end is a bad reading, not a night window
    assert mm._compute_expires_at("26.09.2026", None, "07:00 - 00:01") == "2026-09-26 03:01:00"


def test_an_announcement_is_over_when_its_last_location_is():
    alert = OutageAlert(city="НОВИ САД", outage_date="26.09.2026", affected_locations=[
        StreetSegment(location_type="STREET", main_street="A", time_window="08:00 - 10:00"),
        StreetSegment(location_type="STREET", main_street="B", time_window="13:00 - 16:00")])
    assert mm._announcement_expires_at(alert, "26.09.2026", "08:00 - 10:00") == "2026-09-26 19:00:00"


def test_window_over_tolerates_a_bad_timestamp():
    assert mm._window_over(None) is False
    assert mm._window_over("not a date") is False
    assert mm._window_over("2000-01-01 10:00:00") is True


# --- the queueing path and the re-match -------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "audit.db"))
    database.init_db()
    return database


def _target(database, ulica, broj, naselje, opstina="NOVI SAD", hexes=("89aaaaaaaaaaaaa",)):
    conn = database.get_connection()
    sid = conn.execute("INSERT INTO subscribers (email) VALUES (?)", (f"{ulica}{broj}@x.test",)).lastrowid
    tid = conn.execute(
        "INSERT INTO targets (subscriber_id, city, opstina, naselje, ulica, broj, email, "
        "telegram_chat_id, unsub_token, is_active, email_active, telegram_active) "
        "VALUES (?, 'Novi Sad', ?, ?, ?, ?, ?, NULL, ?, 1, 1, 1)",
        (sid, opstina, naselje, ulica, broj, f"{ulica}{broj}@x.test", f"u{sid}")).lastrowid
    for h in hexes:
        conn.execute("INSERT INTO target_hexes (target_id, hex_id) VALUES (?, ?)", (tid, h))
    conn.commit()
    conn.close()
    return tid


def _outage(database, alert, expires_at, hexes=("89aaaaaaaaaaaaa",), region="НОВИ САД", category="WATER",
            status="ACTIVE", source_url=None):
    import uuid
    conn = database.get_connection()
    oid = conn.execute(
        "INSERT INTO outages (region, affected_streets, outage_date, time_window, raw_text, "
        "raw_text_hash, status, match_quality, category, expires_at, parsed_json, source_url) "
        "VALUES (?, 'x', ?, '08:00 - 23:59', 'raw', ?, ?, 'EXACT', ?, ?, ?, ?)",
        (region, alert.outage_date, uuid.uuid4().hex, status, category, expires_at,
         alert.model_dump_json(), source_url)).lastrowid
    for h in hexes:
        conn.execute("INSERT INTO outage_hexes (outage_id, hex_id) VALUES (?, ?)", (oid, h))
    conn.commit()
    conn.close()
    return oid


def _queued(database, tid):
    conn = database.get_connection()
    n = conn.execute("SELECT COUNT(*) FROM message_queue WHERE target_id = ?", (tid,)).fetchone()[0]
    conn.close()
    return n


def _later(hours):
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _alert(**loc):
    return OutageAlert(city="НОВИ САД", outage_date=date.today().strftime("%d.%m.%Y"),
                       message_template="Prekid vode u ulici ***.",
                       affected_locations=[StreetSegment(location_type="STREET",
                                                         time_window="08:00 - 23:59", **loc)])


def test_rematch_now_reaches_a_range_plus_explicit_segment(db):
    tid = _target(db, "Srbobranska", "20", "NOVI SAD")
    _outage(db, _alert(main_street="СРБОБРАНСКА", number_range="14-32", explicit_numbers="11, 13"),
            _later(10))
    assert mm.backfill_target_against_active_outages(tid, dry_run=True)["queued"] == 1
    assert _queued(db, tid) == 0          # dry run queued nothing
    assert mm.backfill_target_against_active_outages(tid)["queued"] == 1
    assert _queued(db, tid) == 1
    assert mm.backfill_target_against_active_outages(tid)["queued"] == 0   # never twice


def test_rematch_skips_a_window_that_already_ended(db):
    tid = _target(db, "Srbobranska", "20", "NOVI SAD")
    # expires_at carries the 3 h grace: one hour from now means the window closed 2 h ago.
    _outage(db, _alert(main_street="СРБОБРАНСКА"), _later(1))
    assert mm.backfill_target_against_active_outages(tid)["queued"] == 0


def test_rematch_reaches_tonights_night_outage_during_the_day(db):
    """"22:00 - 06:00" today used to expire at 09:00 this morning, so a signup at noon was
    skipped (and the outage was already off the map)."""
    tid = _target(db, "Srbobranska", "20", "NOVI SAD")
    today = date.today().strftime("%d.%m.%Y")
    alert = OutageAlert(city="НОВИ САД", outage_date=today, message_template="Ispiranje mreže u ulici ***.",
                        affected_locations=[StreetSegment(location_type="STREET", main_street="СРБОБРАНСКА",
                                                          time_window="22:00 - 06:00")])
    _outage(db, alert, mm._compute_expires_at(today, None, "22:00 - 06:00"))
    assert mm.backfill_target_against_active_outages(tid)["queued"] == 1


def test_dry_run_does_not_geocode_an_unmapped_target(db, monkeypatch):
    tid = _target(db, "Srbobranska", "20", "NOVI SAD", hexes=())

    def no_geocoding(*a, **k):
        raise AssertionError("dry run must not map a target")

    monkeypatch.setattr(mm, "ensure_target_hexes", no_geocoding)
    assert mm.backfill_target_against_active_outages(tid, dry_run=True)["mapped_hexes"] == 0


def test_rematch_does_not_send_a_village_outage_to_the_town(db, rgz_naselja):
    town = _target(db, "Futoški put", "12", "NOVI SAD")
    village = _target(db, "Branka Radičevića", "3", "VETERNIK")
    alert = OutageAlert(city="НОВИ САД", outage_date=date.today().strftime("%d.%m.%Y"),
                        message_template="Prekid vode u naselju Veternik.",
                        affected_locations=[_village("ВЕТЕРНИК")])
    _outage(db, alert, _later(10))
    assert mm.backfill_target_against_active_outages(town)["queued"] == 0
    assert mm.backfill_target_against_active_outages(village)["queued"] == 1


# --- the dashboard's "Poslednjih 90 dana" -----------------------------------------------------

def _past(days):
    return (date.today() - timedelta(days=days)).strftime("%d.%m.%Y")


def _alert_on(day, time_window="08:00 - 12:00", **loc):
    return OutageAlert(city="НОВИ САД", outage_date=day, message_template="Prekid vode u ulici ***.",
                       affected_locations=[StreetSegment(location_type="STREET", time_window=time_window, **loc)])


@pytest.mark.parametrize("window, hours", [
    ("08:00 - 14:00", 6), ("22:00 - 06:00", 8), ("08:00 - 10:00, 13:00 - 16:00", 5),
    ("08:00 - 00:00", 16), ("07:00 - 00:01", 0), ("", 0), ("08:00", 0), (None, 0),
])
def test_window_hours(window, hours):
    assert mm._window_hours(window) == hours


def test_history_counts_what_the_alert_rules_would_have_sent(db):
    tid = _target(db, "Srbobranska", "20", "NOVI SAD")
    _outage(db, _alert_on(_past(5), "08:00 - 14:00", main_street="СРБОБРАНСКА", number_range="14-32"), None)
    _outage(db, _alert_on(_past(5), "08:00 - 14:00", main_street="СРБОБРАНСКА"), None)   # the same event, stored again
    _outage(db, _alert_on(_past(30), "22:00 - 06:00", main_street="СРБОБРАНСКА"), None)  # a night: 8 h
    _outage(db, _alert_on(_past(10), main_street="СРБОБРАНСКА", number_range="40-60"), None)   # other house numbers
    _outage(db, _alert_on(_past(10), main_street="ФУТОШКА"), None)                             # other street
    _outage(db, _alert_on(_past(120), main_street="СРБОБРАНСКА"), None)                       # older than 90 days
    _outage(db, _alert_on(_past(-2), main_street="СРБОБРАНСКА"), None)                        # announced, not history
    _outage(db, _alert_on(_past(3), main_street="СРБОБРАНСКА"), None, category="WORKS")       # road works only
    _outage(db, _alert_on(_past(3), main_street="СРБОБРАНСКА"), None, region="БЕОГРАД")       # another city
    assert mm.outage_history([tid]) == {tid: {"count": 2, "hours": 14}}


def test_history_counts_outages_the_cleanup_hid_but_not_older_copies(db):
    """The 2026-09-11 cleanup kept the newest row per source page and marked the rest EXPIRED.
    On an EPS page those were other, real outages; on a single-notice page, older copies."""
    tid = _target(db, "Srbobranska", "20", "NOVI SAD")
    eps = "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/NoviSad_1.htm"
    _outage(db, _alert_on(_past(20), "08:00 - 14:00", main_street="СРБОБРАНСКА"), None,
            category="POWER", status="EXPIRED", source_url=eps)                               # counts
    _outage(db, _alert_on(_past(19), "08:00 - 14:00", main_street="ФУТОШКА"), None,
            category="POWER", source_url=eps)                                                # the kept row: another street
    notice = "https://vikns.rs/obavestenje-17/"
    _outage(db, _alert_on(_past(11), "08:00 - 12:00", main_street="СРБОБРАНСКА"), None,
            status="EXPIRED", source_url=notice)                                             # older copy, date drifted
    _outage(db, _alert_on(_past(10), "08:00 - 12:00", main_street="СРБОБРАНСКА"), None,
            source_url=notice)                                                               # counts
    _outage(db, _alert_on(_past(4), main_street="СРБОБРАНСКА"), None, status="CANCELLED")   # never a real status
    assert mm.outage_history([tid]) == {tid: {"count": 2, "hours": 10}}


def test_history_of_an_unmapped_or_unknown_address_is_zero(db):
    tid = _target(db, "Srbobranska", "20", "NOVI SAD", hexes=())
    assert mm.outage_history([tid, 999999]) == {tid: {"count": 0, "hours": 0}, 999999: {"count": 0, "hours": 0}}
    assert mm.outage_history([]) == {}
