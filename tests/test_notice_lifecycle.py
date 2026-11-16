"""What happens to a notice after its first alert (29.09.2026 review of the alert path):
- the same notice republished with its streets in another order is not a second outage (#3);
- everything one person has queued together goes out as one message (#4);
- new hours on the same notice reach the people already told (#1);
- "radovi su odloženi / otkazani" takes the outage off the map and tells them (#2);
- Telegram holds planned notices through the night (quiet hours);
- nothing is sent about an outage that is already over (#6).
Geometry, RGZ and the operator's Telegram are stubbed, so this runs in CI."""
import os
import sys
from datetime import date, datetime, time, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm
from core import delivery
from core.notice import CHANGED_PREFIX
from parser import OutageAlert, StreetSegment

TOMORROW = date.today() + timedelta(days=1)
DAY = TOMORROW.strftime("%d.%m.%Y")
HEX = "89aaaaaaaaaaaaa"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "life.db"))
    database.init_db()
    monkeypatch.setattr(mm, "calculate_outage_hexes", lambda *a, **k: ([HEX], "EXACT"))
    monkeypatch.setattr(mm, "_build_visual_features", lambda *a, **k: [])
    monkeypatch.setattr(mm, "query_rgz_streets", lambda *a, **k: [])
    notes = []
    monkeypatch.setattr(mm, "send_operator_note", lambda text, *a, **k: notes.append(text))
    database.notes = notes
    return database


def _target(database, street="ЗАХУМСКА", email="stanar@example.com"):
    conn = database.get_connection()
    tid = conn.execute(
        "INSERT INTO targets (city, ulica, broj, email, unsub_token, is_active, email_active, telegram_active) "
        "VALUES ('БЕОГРАД', ?, '1', ?, ?, 1, 1, 1)", (street, email, f"tok-{street}")).lastrowid
    conn.execute("INSERT INTO target_hexes (target_id, hex_id) VALUES (?, ?)", (tid, HEX))
    conn.commit()
    conn.close()
    return tid


def _publish(*streets, window="08:00 - 14:00", raw=None, day=DAY, category="WATER"):
    alert = OutageAlert(city="БЕОГРАД", outage_date=day, message_template="Bez vode u ulici ***.",
                        affected_locations=[StreetSegment(location_type="STREET", main_street=s,
                                                          time_window=window) for s in streets])
    raw = raw or f"Radovi {day} {window}: " + ", ".join(streets)
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category=category)


def _outages(database):
    conn = database.get_connection()
    rows = [tuple(r) for r in conn.execute("SELECT id, status, time_window FROM outages ORDER BY id")]
    conn.close()
    return rows


def _queue(database):
    conn = database.get_connection()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, outage_id, status, custom_message, scheduled_for FROM message_queue ORDER BY id")]
    conn.close()
    return rows


def _mark_all_sent(database):
    conn = database.get_connection()
    conn.execute("UPDATE message_queue SET status = 'SENT', sent_at = CURRENT_TIMESTAMP WHERE status = 'QUEUED'")
    conn.commit()
    conn.close()


# --- #3: the same streets in another order ---------------------------------------------------

def test_same_streets_in_another_order_are_the_same_notice(db):
    _publish("ЗАХУМСКА", "НЕБОЈШИНА", "РАНКЕОВА")
    _publish("РАНКЕОВА", "ЗАХУМСКА", "НЕБОЈШИНА")
    assert len(_outages(db)) == 1


def test_a_street_added_at_the_same_hours_is_the_same_notice(db):
    _publish("НЕБОЈШИНА", "РАНКЕОВА")
    _publish("ЗАХУМСКА", "НЕБОЈШИНА", "РАНКЕОВА")                    # one more, put first
    assert len(_outages(db)) == 1


def test_the_same_area_at_other_hours_is_another_outage(db):
    _publish("НЕБОЈШИНА", "РАНКЕОВА", window="08:00 - 10:00")
    _publish("ЗАХУМСКА", "НЕБОЈШИНА", "РАНКЕОВА", window="12:00 - 14:00")
    assert len(_outages(db)) == 2


def _publish_live(street, window, live):
    alert = OutageAlert(city="БЕОГРАД", outage_date=DAY, message_template="Bez struje u ***.",
                        affected_locations=[StreetSegment(location_type="STREET", main_street=street,
                                                          time_window=window)])
    raw = f"Datum: {DAY} | Vreme: {window} | Ulice: {street}"
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category="POWER", live_hashes=live)
    return raw


def test_one_place_at_several_hours_still_on_the_page_is_several_outages(db):
    # EPS, Буљане 14.10.2026: three slots on one page. Merged, they flipped the stored hours
    # every cycle and cost a Gemini call each time.
    windows = ["08:00 - 09:00", "09:00 - 10:30", "11:00 - 12:00"]
    live = {f"Datum: {DAY} | Vreme: {w} | Ulice: ЗАХУМСКА" for w in windows}
    _target(db)
    for w in windows:
        _publish_live("ЗАХУМСКА", w, live)

    # Each row keeps its own text hash, so the next cycle skips all three before Gemini.
    assert sorted(o[2] for o in _outages(db)) == sorted(windows)
    assert not any((q["custom_message"] or "").startswith(CHANGED_PREFIX) for q in _queue(db))


def test_a_row_whose_old_text_is_gone_is_still_new_hours(db):
    _target(db)
    old = _publish_live("ЗАХУМСКА", "08:00 - 09:00", None)
    _mark_all_sent(db)
    new = f"Datum: {DAY} | Vreme: 10:00 - 12:00 | Ulice: ЗАХУМСКА"
    _publish_live("ЗАХУМСКА", "10:00 - 12:00", {new})                  # EPS edited the row
    assert old != new and len(_outages(db)) == 1
    assert _queue(db)[-1]["custom_message"].startswith(CHANGED_PREFIX)


# --- #1: new hours reach the people already told ----------------------------------------------

def test_new_hours_are_sent_to_people_already_told(db):
    _target(db)
    _publish("ЗАХУМСКА", "НЕБОЈШИНА")
    _mark_all_sent(db)
    _publish("НЕБОЈШИНА", "ЗАХУМСКА", window="10:00 - 16:00")

    queue = _queue(db)
    assert [q["status"] for q in queue] == ["SENT", "QUEUED"]
    assert queue[1]["custom_message"].startswith(CHANGED_PREFIX) and "10:00" in queue[1]["custom_message"]
    assert _outages(db)[0][2] == "10:00 - 16:00"

    _publish("ЗАХУМСКА", "НЕБОЈШИНА", window="10:00 - 16:00", raw="the same notice, reworded")
    assert len(_queue(db)) == 2                                        # no change, nothing new


def test_an_unsent_message_is_just_rewritten(db):
    _target(db)
    _publish("ЗАХУМСКА")
    _publish("ЗАХУМСКА", window="10:00 - 16:00")
    queue = _queue(db)
    assert len(queue) == 1 and "10:00" in queue[0]["custom_message"]
    assert not queue[0]["custom_message"].startswith(CHANGED_PREFIX)


# --- #2: postponed / cancelled ----------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("ЗВЕЗДАРА – ОДЛОЖЕНИ РАДОВИ Планирани радови ... одложени су до даљег.", "odloženo"),
    ("Radovi najavljeni za sredu se odlažu.", "odloženo"),
    ("Najavljeni radovi su otkazani.", "otkazano"),
    ("због неодложне санације квара", None),                              # urgent, not postponed
    ("У случају лоших временских услова радови се одлажу.", None),        # a condition
    ("Ukoliko kiša bude padala, radovi će biti odloženi.", None),
    ("Воде неће бити до завршетка радова.", None),
])
def test_cancellation_kind(text, kind):
    assert mm._cancellation_kind(text) == kind


def test_a_postponed_notice_takes_the_outage_off_and_tells_them(db):
    _target(db)
    _publish("ЗАХУМСКА", "НЕБОЈШИНА")
    _mark_all_sent(db)
    _publish("ЗАХУМСКА", "НЕБОЈШИНА",
             raw=f"ОДЛОЖЕНИ РАДОВИ. Радови најављени за {DAY} од 8 до 14 одложени су до даљег. "
                 "Уредно водоснабдевање имаће: Захумска, Небојшина.")

    assert [(s) for _, s, _ in _outages(db)] == ["CANCELLED", "CANCELLATION"]
    told = _queue(db)[-1]
    assert told["status"] == "QUEUED" and told["outage_id"] == _outages(db)[1][0]
    assert told["custom_message"].startswith("✅ Radovi su odloženi.")
    assert "Bez vode u ulici" in told["custom_message"]                    # quotes what they were told
    assert any("odloženo" in n for n in db.notes)


def test_a_cancelled_outage_drops_what_was_not_sent_yet(db):
    _target(db)
    _publish("ЗАХУМСКА")
    _publish("ЗАХУМСКА", raw="Najavljeni radovi u Zahumskoj su otkazani.")
    assert [q["status"] for q in _queue(db)] == ["SKIPPED"]                # nobody was told, nobody is


def test_a_cancellation_matching_nothing_is_an_ordinary_notice(db):
    _publish("ЗАХУМСКА", raw="Radovi u Zahumskoj su otkazani.")
    assert [s for _, s, _ in _outages(db)] == ["ACTIVE"]
    assert any("matches 0 stored outages" in n for n in db.notes)


def test_boilerplate_in_an_update_does_not_cancel(db):
    _publish("ЗАХУМСКА", raw="Radovi u Zahumskoj. U slučaju kiše radovi se odlažu.")
    _publish("ЗАХУМСКА", raw="Radovi u Zahumskoj od 8h. U slučaju kiše radovi se odlažu.")
    assert [s for _, s, _ in _outages(db)] == ["ACTIVE"]


# --- quiet hours and late messages (core/delivery) --------------------------------------------

NIGHT = datetime.combine(date.today(), time(23, 30))


@pytest.mark.parametrize("now, outage_date, window, held", [
    (NIGHT, DAY, "10:00 - 14:00", True),                 # tomorrow mid-morning: wait
    (NIGHT, DAY, "07:30 - 14:00", False),                # starts too soon after 07:00
    (NIGHT, date.today().strftime("%d.%m.%Y"), "", False),   # today, maybe under way: now
    (NIGHT, DAY, "", True),                              # tomorrow, whole day
    (datetime.combine(TOMORROW, time(3, 0)), DAY, "", False),    # it is already that day
    (datetime.combine(date.today(), time(12, 0)), DAY, "10:00 - 14:00", False),   # daytime
    (NIGHT, "N/A", "10:00", False),
])
def test_hold_until(now, outage_date, window, held):
    until = delivery.hold_until(outage_date, window, now)
    assert (until is not None) is held
    if held:
        assert until == datetime.combine(TOMORROW, time(7, 0))


def test_telegram_holds_a_planned_notice_through_the_night(db, monkeypatch):
    import workers.telegram_worker as tw
    sent = []

    class Ok:
        ok, status_code = True, 200
        def json(self): return {"ok": True}

    monkeypatch.setattr(tw.config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(tw.requests, "post", lambda url, json=None, timeout=None: sent.append(json) or Ok())
    monkeypatch.setattr(delivery, "serbia_now", lambda: NIGHT)
    tid = _target(db)
    conn = db.get_connection()
    conn.execute("UPDATE targets SET telegram_chat_id = 5 WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    _publish("ЗАХУМСКА", window="10:00 - 14:00")

    tw.run_worker()
    tg = [q for q in _queue(db) if q["scheduled_for"]]
    assert sent == [] and len(tg) == 1
    assert tg[0]["scheduled_for"] == delivery.to_utc_sql(datetime.combine(TOMORROW, time(7, 0)))

    # the morning comes
    conn = db.get_connection()
    conn.execute("UPDATE message_queue SET scheduled_for = datetime('now', '-1 minute')")
    conn.commit()
    conn.close()
    tw.run_worker()
    assert len(sent) == 1


def test_nothing_is_sent_about_an_outage_already_over(db, monkeypatch):
    import workers.email_worker as ew
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)
        def quit(self): pass

    monkeypatch.setattr(ew.smtplib, "SMTP", FakeSMTP)
    for name in ("BREVO_SMTP_LOGIN", "BREVO_SMTP_KEY", "EMAIL_FROM_ADDRESS"):
        monkeypatch.setattr(ew.config, name, "x")
    _target(db)
    _publish("ЗАХУМСКА")
    # a day and a half later the row is still waiting (the provider's daily limit, say)
    monkeypatch.setattr(delivery, "serbia_now", lambda: datetime.combine(TOMORROW + timedelta(days=1), time(9, 0)))
    ew.run_worker()
    assert sent == [] and [q["status"] for q in _queue(db)] == ["SKIPPED"]


def test_a_fresh_row_waits_for_the_rest_of_its_notice(db, monkeypatch):
    from workers.email_worker import claim_queued_messages
    monkeypatch.setattr(delivery, "SETTLE_SECONDS", 90)
    _target(db)
    _publish("ЗАХУМСКА")
    assert claim_queued_messages() == []
    conn = db.get_connection()
    conn.execute("UPDATE message_queue SET created_at = datetime('now', '-2 minutes')")
    conn.commit()
    conn.close()
    assert len(claim_queued_messages()) == 1


def test_a_notice_split_into_nights_is_one_email(db, monkeypatch):
    import workers.email_worker as ew
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)
        def quit(self): pass

    monkeypatch.setattr(ew.smtplib, "SMTP", FakeSMTP)
    for name in ("BREVO_SMTP_LOGIN", "BREVO_SMTP_KEY", "EMAIL_FROM_ADDRESS"):
        monkeypatch.setattr(ew.config, name, "x")
    _target(db)
    for n in range(3):
        _publish("ЗАХУМСКА", window="22:00 - 06:00",
                 day=(TOMORROW + timedelta(days=n)).strftime("%d.%m.%Y"))
    ew.run_worker()
    assert len(sent) == 1
    assert sent[0]["Subject"] == "Najava isključenja vode — 3 najave"
    assert all(q["status"] == "SENT" for q in _queue(db))


def test_a_held_row_is_not_reported_as_stuck(db, monkeypatch):
    import workers.watchdog as wd
    alerts = []
    monkeypatch.setattr(wd, "send_operator_alert", lambda key, *a, **k: alerts.append(key))
    monkeypatch.setattr(wd, "clear_alert", lambda *a, **k: None)
    _target(db)
    _publish("ЗАХУМСКА")
    conn = db.get_connection()
    conn.execute("UPDATE message_queue SET created_at = datetime('now', '-3 hours'), "
                 "scheduled_for = datetime('now', '+4 hours')")
    conn.commit()
    conn.close()
    wd.check_queue()
    assert "queue_stuck" not in alerts


def test_a_postponement_mentioned_further_down_is_not_about_this_notice():
    long_notice = "Radovi u Zahumskoj ulici od 8 do 14 časova. " + "Detalji radova. " * 30 + \
                  "Radovi u Nebojšinoj najavljeni za prošlu nedelju su odloženi."
    assert mm._cancellation_kind(long_notice) is None


# --- #3465: a whole heating area, several days, no stated hours -------------------------------

def test_a_whole_area_notice_over_several_days_reads_right_and_the_note_names_the_area(db):
    """Toplana Cerak's remont (30.09.2026): a street plus "celo grejno područje toplane Cerak",
    no hours, until Thursday. The two people in Cerak were told "u ulici Cerak dana 30.09.2026.
    u periodu 07:00 - 19:00", and the operator's note named only the street."""
    _target(db, street="РЕПИШКА")
    end = (TOMORROW + timedelta(days=1)).strftime("%d.%m.%Y")
    alert = OutageAlert(
        city="БЕОГРАД", outage_date=DAY, end_date=end,
        message_template=f"Planirano isključenje toplotne energije u ulici *** dana {DAY}. u periodu 07:00 - 19:00.",
        affected_locations=[
            StreetSegment(location_type="STREET", main_street="ПОРУЧНИКА СПАСИЋА И МАШЕРЕ",
                          explicit_numbers="4", time_window="07:00 - 19:00"),
            StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement="ЦЕРАК", time_window="07:00 - 19:00")])
    raw = ("Trenutni prekid u isporuci toplotne energije za pripremu potrošne tople vode: Poručnika "
           "Spasića i Mašere 4; celo grejno područje toplane Cerak, remont do četvrtka.")
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category="HEATING")

    [row] = _queue(db)
    message = row["custom_message"]
    assert message.startswith("Bez tople vode u naselju Cerak") and "u ulici" not in message
    assert "07:00" not in message and "19:00" not in message
    assert message.endswith(f" do {end}.")
    assert any("via the whole area ЦЕРАК" in note for note in db.notes)


# --- a multi-day notice re-posted every day ----------------------------------------------------
# Beogradske elektrane post "Izveštaj o trenutnom prekidu" each day while works go on: Toplana
# Cerak's remont to 01.10.2026 came in as #3347 (29.09.) and #3465 (30.09.).

def _report(start, end, *areas, streets=("ПОРУЧНИКА СПАСИЋА И МАШЕРЕ",), window=""):
    locations = [StreetSegment(location_type="STREET", main_street=s, time_window=window) for s in streets]
    locations += [StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement=a, time_window=window) for a in areas]
    alert = OutageAlert(city="БЕОГРАД", outage_date=start.strftime("%d.%m.%Y"),
                        end_date=end.strftime("%d.%m.%Y") if end else None,
                        message_template="Prekid u isporuci tople vode u ulici ***.", affected_locations=locations)
    raw = (f"Izveštaj o trenutnom prekidu u isporuci tople vode {window}: " + ", ".join(streets + areas)
           + f". Objavljeno {start:%d.%m.%Y}.")
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category="HEATING")


def test_the_next_days_copy_of_running_works_is_the_same_outage_and_mails_nobody_again(db):
    _target(db, street="РЕПИШКА")
    day2, last = TOMORROW + timedelta(days=1), TOMORROW + timedelta(days=2)
    _report(TOMORROW, last, "ЦЕРАК")
    assert len(_queue(db)) == 1
    _mark_all_sent(db)
    _report(day2, last, "ЦЕРАК")
    _report(last, None, "ЦЕРАК")                                      # the last day's copy, no end date
    assert len(_outages(db)) == 1
    assert len(_queue(db)) == 1


def test_an_area_the_next_copy_adds_is_told_and_the_rest_are_not_told_again(db):
    street = _target(db, street="ПОРУЧНИКА СПАСИЋА И МАШЕРЕ", email="ulica@example.com")
    cerak = _target(db, street="РЕПИШКА", email="cerak@example.com")
    last = TOMORROW + timedelta(days=2)
    _report(TOMORROW, last)                                           # #3347: Cerak not read
    _mark_all_sent(db)
    _report(TOMORROW + timedelta(days=1), last, "ЦЕРАК")              # #3465: Cerak read
    conn = db.get_connection()
    told = [r[0] for r in conn.execute("SELECT target_id FROM message_queue ORDER BY id")]
    conn.close()
    assert len(_outages(db)) == 1 and told == [street, cerak]


def test_a_one_day_fault_inside_the_works_is_its_own_outage(db):
    _target(db, street="ПОРУЧНИКА СПАСИЋА И МАШЕРЕ")
    _report(TOMORROW, TOMORROW + timedelta(days=2))
    _mark_all_sent(db)
    _report(TOMORROW + timedelta(days=1), None, window="08:00 - 12:00")
    assert len(_outages(db)) == 2 and len(_queue(db)) == 2

