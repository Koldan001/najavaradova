"""End-to-end guard for the one thing the product is: an outage announced for a street a
subscriber registered must queue exactly one message per channel, both workers must see it, a
re-scrape must not double-send, and a target on the wrong street or outside the announced house
numbers must get nothing.

Unlike test_matchmaker.py (pure functions, no DB) this drives the real
process_and_queue_outage -> message_queue -> worker-SELECT path against a throwaway DB. Needs
data/serbia_streets.db for the street geometry, so it skips where that isn't present (CI).
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from spatial.spatial_engine import SERBIA_DB_PATH

requires_street_db = pytest.mark.skipif(
    not os.path.exists(SERBIA_DB_PATH),
    reason="data/serbia_streets.db not present locally",
)

CITY = "ПАНЧЕВО"
STREET = "СИНЂЕЛИЋЕВА"
OTHER_STREET = "МИЛОША ОБРЕНОВИЋА"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "e2e.db"))
    database.init_db()
    return database


def _mk_target(database, ulica, broj, email, chat_id, city=CITY, opstina=None, naselje=None):
    conn = database.get_connection()
    sid = conn.execute("INSERT INTO subscribers (email) VALUES (?)", (email,)).lastrowid
    tid = conn.execute(
        """INSERT INTO targets
           (subscriber_id, city, opstina, naselje, ulica, broj, email, telegram_chat_id,
            unsub_token, is_active, email_active, telegram_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1)""",
        (sid, city, opstina, naselje, ulica, broj, email, chat_id, "unsub_%s" % sid),
    ).lastrowid
    conn.commit()
    from core.matchmaker import ensure_target_hexes
    row = conn.execute(
        "SELECT id, city, opstina, naselje, ulica FROM targets WHERE id = ?", (tid,)
    ).fetchone()
    ensure_target_hexes(conn.cursor(), row)
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) c FROM target_hexes WHERE target_id = ?", (tid,)
    ).fetchone()["c"]
    conn.close()
    return tid, n


def _queued(database, tid):
    conn = database.get_connection()
    rows = conn.execute(
        "SELECT channel, status FROM message_queue WHERE target_id = ?", (tid,)
    ).fetchall()
    conn.close()
    return sorted((r["channel"], r["status"]) for r in rows)


def _bare_queue_row(database, channel="telegram", message="msg"):
    """A single QUEUED message_queue row plus the target + outage rows it joins to - no street
    geometry needed, so this doesn't gate on data/serbia_streets.db."""
    conn = database.get_connection()
    sid = conn.execute("INSERT INTO subscribers (email) VALUES ('c@example.com')").lastrowid
    tid = conn.execute(
        "INSERT INTO targets (subscriber_id, city, ulica, broj, email, telegram_chat_id, "
        "unsub_token, is_active, email_active, telegram_active) "
        "VALUES (?, 'X', 'Y', '1', 'c@example.com', 999, 'u', 1, 1, 1)", (sid,)
    ).lastrowid
    oid = conn.execute(
        "INSERT INTO outages (region, raw_text, status) VALUES ('X', 'raw', 'ACTIVE')"
    ).lastrowid
    conn.execute(
        "INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at) "
        "VALUES (?, ?, ?, ?, 'QUEUED', datetime('now'))", (oid, tid, channel, message)
    )
    conn.commit()
    conn.close()
    return tid


def test_claim_is_exclusive_no_double_send(db):
    """Two `sender` processes overlapping - a deploy recreates the container while the previous
    run is mid-send - must not each deliver the same notification. The QUEUED -> SENDING claim
    is atomic, so the second worker's claim comes back empty."""
    from workers.telegram_worker import claim_queued_messages

    _bare_queue_row(db, "telegram")

    first = [j for j in claim_queued_messages() if str(j["telegram_chat_id"]) == "999"]
    assert first, "first claim should get the queued row"
    second = [j for j in claim_queued_messages() if str(j["telegram_chat_id"]) == "999"]
    assert second == [], "a concurrent second claim must not re-surface a claimed row"


def test_stale_sending_row_is_reclaimed(db):
    """A worker that crashed after claiming leaves a row in 'SENDING'. Once it's older than the
    reclaim floor it goes back to 'QUEUED' and can be sent by a later run."""
    from workers.telegram_worker import claim_queued_messages, reclaim_stale_sending

    _bare_queue_row(db, "telegram")
    claim_queued_messages()  # -> SENDING

    conn = db.get_connection()
    conn.execute("UPDATE message_queue SET last_attempt_at = datetime('now', '-10 minutes') "
                 "WHERE status = 'SENDING'")
    conn.commit()
    conn.close()

    reclaim_stale_sending()
    again = [j for j in claim_queued_messages() if str(j["telegram_chat_id"]) == "999"]
    assert again, "a stale SENDING row must return to QUEUED and be claimable again"


def test_fresh_sending_row_is_not_reclaimed(db):
    """The reclaim must NOT touch a row another worker just claimed (last_attempt_at is recent),
    or two workers would both send it - the exact race this fix closes."""
    from workers.telegram_worker import claim_queued_messages, reclaim_stale_sending

    _bare_queue_row(db, "telegram")
    claim_queued_messages()  # -> SENDING, last_attempt_at = now

    reclaim_stale_sending()  # simulates the other worker's run starting
    again = [j for j in claim_queued_messages() if str(j["telegram_chat_id"]) == "999"]
    assert again == [], "a freshly-claimed SENDING row must stay claimed"


def test_email_subject_is_specific_by_category_and_date():
    from workers.email_worker import _subject
    assert _subject("POWER", "09.09.2026") == "Najava isključenja struje — 09.09.2026"
    assert _subject("water", "10.09.2026") == "Najava isključenja vode — 10.09.2026"
    assert _subject("HEATING", "N/A") == "Najava prekida grejanja"      # no date suffix
    assert _subject(None, None) == "Najava planiranih radova"           # unknown category


def _telegram_payloads(monkeypatch):
    """Runs nothing itself - swaps requests.post in the Telegram worker for a recorder that
    answers like a successful sendMessage, and returns the list the payloads land in."""
    import workers.telegram_worker as tw
    sent = []

    class _Ok:
        ok = True
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return _Ok()

    monkeypatch.setattr(tw.config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(tw.requests, "post", fake_post)
    return sent


def _add_addresses(database, tid, n, active=1, manager=0):
    """Give the owner of target `tid` n more addresses (same subscriber and Telegram chat).
    manager=1 = they came in as a building manager's list (targets.manager_list)."""
    conn = database.get_connection()
    sid = conn.execute("SELECT subscriber_id FROM targets WHERE id = ?", (tid,)).fetchone()[0]
    for i in range(n):
        conn.execute(
            "INSERT INTO targets (subscriber_id, city, ulica, broj, email, telegram_chat_id, "
            "unsub_token, is_active, email_active, telegram_active, manager_list) "
            "VALUES (?, 'X', 'Z', ?, 'c@example.com', 999, ?, ?, 1, 1, ?)",
            (sid, str(i + 2), "u%d" % i, active, manager),
        )
    conn.commit()
    conn.close()


def _copy_button(payload):
    return payload["reply_markup"]["inline_keyboard"][0][0]


def test_telegram_single_address_user_gets_neighbour_copy(db, monkeypatch):
    """A regular user gets a copy to pass to neighbours or family, signed with where it came
    from - the service travels with every paste. Many addresses typed in one by one (a family
    with a few flats) is still a regular user, up to 9 of them."""
    import workers.telegram_worker as tw
    sent = _telegram_payloads(monkeypatch)
    tid = _bare_queue_row(db, "telegram", "Nestanak struje u ulici Y dana 01.10.2026.")
    _add_addresses(db, tid, 8)                           # 9 addresses, none from a manager list
    _add_addresses(db, tid, 3, active=0, manager=1)      # a deactivated manager list doesn't count

    tw.run_worker()

    assert len(sent) == 1
    button = _copy_button(sent[0])
    copied = button["copy_text"]["text"]
    assert button["text"] == "📤 Pošalji komšijama"
    assert copied.startswith("Nestanak struje u ulici Y dana 01.10.2026.")
    assert copied.endswith("Obaveštenje preko najavaradova.rs")
    # The in-chat housekeeping stays out of the copy.
    assert "/odjava" not in copied and "dashboard" not in copied


def test_telegram_manager_gets_tenant_copy(db, monkeypatch):
    """Managers (signed up with a list) retype our alert into their tenants' Viber group - the
    copy button hands them the finished tenant notice, signed quietly. Every address of theirs
    gets it, including one added later by hand (the alerted row here has no flag itself)."""
    import workers.telegram_worker as tw
    sent = _telegram_payloads(monkeypatch)
    tid = _bare_queue_row(db, "telegram", "Nestanak struje u ulici Y dana 01.10.2026.")
    _add_addresses(db, tid, 1, manager=1)

    tw.run_worker()

    assert len(sent) == 1
    button = _copy_button(sent[0])
    assert button["text"] == "📋 Kopiraj za stanare"
    assert button["copy_text"]["text"].endswith("\n\n— najavaradova.rs")


def test_telegram_alert_over_copy_limit_is_sent_without_button(db, monkeypatch):
    """Telegram rejects a copy_text over 256 chars - the alert must still go out, just without
    the button, rather than failing or sending a cut-off notice."""
    import workers.telegram_worker as tw
    sent = _telegram_payloads(monkeypatch)
    _bare_queue_row(db, "telegram", "Isključenje struje. " * 20)

    tw.run_worker()

    assert len(sent) == 1
    assert "reply_markup" not in sent[0]


def test_feedback_buttons_only_when_switched_on(db, monkeypatch):
    """👍 / 👎 under an alert is off by default (config.FEEDBACK_BUTTONS); on, it is the only row
    left when the copy button doesn't fit."""
    import config
    import workers.telegram_worker as tw
    monkeypatch.setattr(config, "FEEDBACK_BUTTONS", True)
    sent = _telegram_payloads(monkeypatch)
    _bare_queue_row(db, "telegram", "Isključenje struje. " * 20)

    tw.run_worker()

    rows = sent[0]["reply_markup"]["inline_keyboard"]
    assert not any("copy_text" in b for row in rows for b in row)
    assert [b["callback_data"][:5] for b in rows[0]] == ["fb:y:", "fb:n:"]


def _email_parts(manager):
    from workers.email_worker import build_message
    msg = build_message("a@example.com", "tok", "Nestanak vode u ulici Y.", "https://example.com/x",
                        manager=manager)
    return (msg.get_body(preferencelist=("plain",)).get_content(),
            msg.get_body(preferencelist=("html",)).get_content())


def test_email_regular_user_gets_plain_alert():
    plain, html = _email_parts(False)
    assert plain.startswith("Nestanak vode u ulici Y.\n\n")
    assert plain.count("Nestanak vode u ulici Y.") == 1
    assert "Tekst za stanare" not in plain and "Tekst za stanare" not in html


def test_email_manager_gets_the_tenant_notice_once():
    """Mostly older readers: the manager's email is one ready-to-paste block, not the alert
    followed by a second copy of itself."""
    from workers.email_worker import TENANT_HEADING
    plain, html = _email_parts(True)
    assert plain.startswith(f"{TENANT_HEADING}\n\nNestanak vode u ulici Y.\n\n— najavaradova.rs\n\n")
    assert plain.count("Nestanak vode u ulici Y.") == 1
    assert TENANT_HEADING in html and "— najavaradova.rs" in html


def _d(days_from_today=0) -> str:
    """A YYYY-MM-DD date relative to whenever the suite actually runs, not a fixed calendar day.
    Every alert below used to hardcode "2026-09-10" etc. - fine the day it was written, but
    core/matchmaker.py's own past-date guard (4536f98, this same project) is designed to reject
    exactly that once real time catches up to it: confirmed live, "2026-09-10" started failing
    the moment the wall clock crossed into 2026-09-11, with zero code change - the guard working
    correctly on a fixture that had quietly gone stale. That also silently blocks every deploy
    (ci.yml's deploy job needs: test). None of these tests care which specific day it is, only
    that it isn't in the past, so compute it instead of pinning it."""
    return (datetime.now() + timedelta(days=days_from_today)).strftime("%Y-%m-%d")


def _alert(street, date, number_range=None):
    # 00:00-23:59, not a fixed crew window like "08:00-14:00": the latter made
    # _compute_expires_at land in the past once the suite happened to run later than 14:00,
    # which made test_rescraping_the_same_outage_does_not_double_queue's SECOND
    # process_and_queue_outage call miss the dedup-merge query's `expires_at > datetime('now')`
    # check and insert a fresh row instead of updating the existing one - not a real bug (the
    # merge query correctly refuses to fold into an already-expired row), just another fixture
    # that went stale with wall-clock time, same species as the outage_date issue above.
    seg = {"location_type": "STREET", "settlement": CITY, "main_street": street,
           "time_window": "00:00 - 23:59"}
    if number_range:
        seg["number_range"] = number_range
    return {"event_type": "POWER", "city": CITY, "outage_date": date,
            "message_template": "Nestanak struje u ulici {street}.",
            "affected_locations": [seg]}


@requires_street_db
def test_registered_address_gets_one_queued_row_per_channel(db):
    from core.matchmaker import process_and_queue_outage

    tid, n_hex = _mk_target(db, STREET, "10", "hit@example.com", 111111)
    assert n_hex > 0, "target street resolved to no geometry"

    process_and_queue_outage(_alert(STREET, _d()), "Nestanak struje: %s" % STREET,
                             text_hash="e2e_h1", category="POWER")

    assert _queued(db, tid) == [("email", "QUEUED"), ("telegram", "QUEUED")]


@requires_street_db
def test_outage_whose_window_already_ended_today_is_not_sent(db):
    """First seen after its own window ended (an EPS Dan_0 page read in the evening): kept for the
    map, never messaged."""
    from core.matchmaker import process_and_queue_outage

    tid, _ = _mk_target(db, STREET, "10", "hit@example.com", 111111)
    alert = _alert(STREET, _d())
    alert["affected_locations"][0]["time_window"] = "00:00 - 00:01"
    process_and_queue_outage(alert, "Nestanak struje: %s" % STREET,
                             text_hash="e2e_over", category="POWER")

    assert _queued(db, tid) == []
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) c FROM outages WHERE raw_text_hash = 'e2e_over'").fetchone()["c"] == 1
    conn.close()


@requires_street_db
def test_both_workers_surface_the_queued_row(db):
    from core.matchmaker import process_and_queue_outage
    from workers.telegram_worker import claim_queued_messages as tg_jobs
    from workers.email_worker import claim_queued_messages as em_jobs

    _mk_target(db, STREET, "10", "hit@example.com", 111111)
    process_and_queue_outage(_alert(STREET, _d()), "Nestanak struje: %s" % STREET,
                             text_hash="e2e_h1", category="POWER")

    assert [j for j in tg_jobs() if str(j["telegram_chat_id"]) == "111111"]
    assert [j for j in em_jobs() if j["email"] == "hit@example.com"]


@requires_street_db
def test_rescraping_the_same_outage_does_not_double_queue(db):
    from core.matchmaker import process_and_queue_outage

    tid, _ = _mk_target(db, STREET, "10", "hit@example.com", 111111)
    process_and_queue_outage(_alert(STREET, _d()), "Nestanak struje: %s" % STREET,
                             text_hash="e2e_h1", category="POWER")
    first = _queued(db, tid)
    process_and_queue_outage(_alert(STREET, _d()), "Nestanak struje: %s EDIT" % STREET,
                             text_hash="e2e_h1b", category="POWER")
    assert _queued(db, tid) == first


@requires_street_db
def test_target_on_a_different_street_is_not_notified(db):
    from core.matchmaker import process_and_queue_outage

    tid, n_hex = _mk_target(db, OTHER_STREET, "10", "miss@example.com", 222222)
    assert n_hex > 0, "control street resolved to no geometry - pick another"
    process_and_queue_outage(_alert(STREET, _d()), "Nestanak struje: %s" % STREET,
                             text_hash="e2e_h2", category="POWER")
    assert _queued(db, tid) == []


@requires_street_db
def test_latin_registered_city_matches_a_cyrillic_scraped_outage(db):
    """The real-world common case: the RGZ autocomplete gives the subscriber a Latin city
    ('Pančevo'), every EPS/toplana page names its region in Cyrillic ('ПАНЧЕВО'). The macro-
    scope guard must bridge the two scripts or the target is silently never a candidate."""
    from core.matchmaker import process_and_queue_outage

    tid, n_hex = _mk_target(db, STREET, "10", "latin@example.com", 555000)
    assert n_hex > 0
    conn = db.get_connection()
    conn.execute(
        "UPDATE targets SET city = ?, opstina = NULL, naselje = NULL WHERE id = ?",
        ("Pančevo", tid),   # Latin, exactly as the web form stores it
    )
    conn.commit()
    conn.close()

    process_and_queue_outage(_alert(STREET, _d()),   # _alert() uses Cyrillic CITY
                             "Nestanak struje: %s" % STREET, text_hash="e2e_latin",
                             category="POWER")
    assert _queued(db, tid) == [("email", "QUEUED"), ("telegram", "QUEUED")]


@requires_street_db
def test_road_closure_only_notice_is_mapped_but_never_notified(db):
    """category WORKS = traffic is affected, no supply is cut. It belongs on the map as context
    for the neighbourhood, but nobody signed up to be told their street is closed to cars."""
    from core.matchmaker import process_and_queue_outage

    tid, n_hex = _mk_target(db, STREET, "10", "works@example.com", 777000)
    assert n_hex > 0
    process_and_queue_outage(_alert(STREET, _d()),
                             "Izmena režima saobraćaja: %s" % STREET,
                             text_hash="e2e_works", category="WORKS")
    assert _queued(db, tid) == []

    # ...and it really is stored, just silently
    conn = db.get_connection()
    row = conn.execute("SELECT category FROM outages WHERE raw_text_hash = 'e2e_works'").fetchone()
    conn.close()
    assert row["category"] == "WORKS"


@requires_street_db
def test_notification_text_names_the_scrapers_utility_not_the_models_guess(db):
    """End-to-end version of the EPS incident: the alert's template says water, the scraper
    guarantees POWER, so what actually lands in message_queue must say struje."""
    from core.matchmaker import process_and_queue_outage

    tid, _ = _mk_target(db, STREET, "10", "utility@example.com", 888000)
    alert = _alert(STREET, _d())
    alert["message_template"] = "Planirano isključenje vode u ulici ***."
    process_and_queue_outage(alert, "EPS row", text_hash="e2e_utility", category="POWER")

    conn = db.get_connection()
    msgs = [r["custom_message"] for r in conn.execute(
        "SELECT custom_message FROM message_queue WHERE target_id = ?", (tid,)).fetchall()]
    conn.close()
    assert msgs, "nothing was queued"
    for m in msgs:
        assert "vode" not in m.lower(), m
        assert "struje" in m.lower(), m


@requires_street_db
def test_house_number_outside_announced_range_is_not_notified(db):
    from core.matchmaker import process_and_queue_outage

    out_tid, _ = _mk_target(db, STREET, "500", "range@example.com", 333333)
    process_and_queue_outage(_alert(STREET, _d(), number_range="1-20"),
                             "Nestanak struje: %s 1-20" % STREET, text_hash="e2e_h3",
                             category="POWER")
    assert _queued(db, out_tid) == []

    in_tid, _ = _mk_target(db, STREET, "12", "inrange@example.com", 444444)
    process_and_queue_outage(_alert(STREET, _d(), number_range="1-20"),
                             "Nestanak struje: %s 1-20 b" % STREET, text_hash="e2e_h4",
                             category="POWER")
    assert _queued(db, in_tid) == [("email", "QUEUED"), ("telegram", "QUEUED")]


def test_telegram_ten_addresses_typed_by_hand_is_a_manager(db, monkeypatch):
    """The early Novi Sad managers typed their 10+ buildings in one by one, before /upravnici:
    that many addresses is a manager without the list signup."""
    import workers.telegram_worker as tw
    sent = _telegram_payloads(monkeypatch)
    tid = _bare_queue_row(db, "telegram", "Nestanak struje u ulici Y dana 01.10.2026.")
    _add_addresses(db, tid, 9)

    tw.run_worker()

    assert _copy_button(sent[0])["text"] == "📋 Kopiraj za stanare"
