"""A building manager's list from /upravnici: every pasted line is checked and shown before
anything is saved, only confirmed addresses are sent, the server checks them again, the email
link loads them once, one Telegram link connects them all, and the operator's ❌ removes exactly
what that list created. Fake registry + temporary database, so this runs in CI too."""
import json
import os
import sqlite3
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

from scripts import local_web_server as lws  # noqa: E402

# (street id, name, naselje, opština, house numbers)
STREETS = [
    ("1", "BULEVAR OSLOBOĐENJA", "NOVI SAD", "NOVI SAD", ["46", "72"]),
    ("2", "NARODNOG FRONTA", "NOVI SAD", "NOVI SAD", ["12А"]),
    ("3", "FUTOŠKA", "NOVI SAD", "NOVI SAD", ["15"]),
    ("4", "ŽARKA ZRENJANINA", "NOVI SAD", "NOVI SAD", ["1"]),
    ("5", "BULEVAR CARA LAZARA", "NOVI SAD", "NOVI SAD", ["50"]),
    ("6", "CARA DUŠANA", "FUTOG", "NOVI SAD", ["8"]),
    ("7", "KAMENJAR 2", "NOVI SAD", "NOVI SAD", ["13"]),
    ("8", "RUDNIČKA", "BEOGRAD (VRAČAR)", "VRAČAR", ["8"]),
    ("9", "RUDNIČKA", "BARAJEVO", "BARAJEVO", ["8"]),
]


@pytest.fixture(autouse=True)
def fake_world(tmp_path, monkeypatch):
    db = tmp_path / "rgz.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rgz_streets (ulica_maticni_broj TEXT PRIMARY KEY, name TEXT, name_lat TEXT, "
                 "naselje TEXT, naselje_lat TEXT, opstina TEXT, opstina_lat TEXT, geometry_json TEXT)")
    conn.execute("CREATE TABLE rgz_housenumbers (kucni_broj_id TEXT PRIMARY KEY, ulica_maticni_broj TEXT, "
                 "kucni_broj TEXT, lat REAL, lon REAL, naselje_lat TEXT, opstina_lat TEXT, is_active INTEGER)")
    for sid, name, naselje, opstina, numbers in STREETS:
        conn.execute("INSERT INTO rgz_streets (ulica_maticni_broj, name_lat, naselje_lat, opstina_lat) "
                     "VALUES (?, ?, ?, ?)", (sid, name, naselje, opstina))
        for n in numbers:
            conn.execute("INSERT INTO rgz_housenumbers (kucni_broj_id, ulica_maticni_broj, kucni_broj) "
                         "VALUES (?, ?, ?)", (f"{sid}-{n}", sid, n))
    conn.commit()
    conn.close()
    monkeypatch.setattr(lws, "RGZ_DB_PATH", str(db))
    for cache in ("_naselje_cache", "_naselje_opstina_cache", "_uncovered_opstine_cache"):
        monkeypatch.setattr(lws, cache, None)

    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "outages.db"))
    database.init_db()


def _by_line(text, city="Novi Sad"):
    return {r["line"]: r for r in lws.check_address_lines(city, text)}


def test_pasted_lines_are_read_and_checked():
    rows = _by_line("Ulica\tBroj\n"                      # a spreadsheet header: skipped
                    "Bulevar oslobođenja\t46\n"           # Excel columns
                    "Futoska 15\n"                        # no diacritics
                    "Bul. Cara Lazara 50\n"               # shorthand
                    "Narodnog fronta 12 a\n"              # spaced letter
                    "Kamenjar 2 13\n"                     # a street named with a number
                    "Cara Dušana 8, Futog\n"              # its own place
                    "Bulevar oslobođenja\t72\tNovi Sad")  # place as a column
    assert "Ulica\tBroj" not in rows
    ok = {line: r["label"] for line, r in rows.items() if r["status"] == "ok"}
    assert ok == {
        "Bulevar oslobođenja\t46": "Bulevar oslobođenja 46, Novi Sad",
        "Futoska 15": "Futoška 15, Novi Sad",
        "Bul. Cara Lazara 50": "Bulevar Cara Lazara 50, Novi Sad",
        "Narodnog fronta 12 a": "Narodnog fronta 12A, Novi Sad",
        "Kamenjar 2 13": "Kamenjar 2 13, Novi Sad",
        "Cara Dušana 8, Futog": "Cara Dušana 8, Futog",
        "Bulevar oslobođenja\t72\tNovi Sad": "Bulevar oslobođenja 72, Novi Sad",
    }


def test_doubtful_lines_are_shown_never_guessed():
    rows = _by_line("Narodnog frotna 12a\nŽarka Zrenjanina 140\nNova ulica 12\nFutoška\nRudnička 8")
    typo = rows["Narodnog frotna 12a"]
    assert typo["status"] == "maybe" and [o["label"] for o in typo["options"]] == ["Narodnog Fronta 12A, Novi Sad"]
    assert rows["Žarka Zrenjanina 140"]["status"] == "num"          # street real, number not
    assert rows["Nova ulica 12"]["status"] == "bad"
    assert rows["Futoška"]["message"].startswith("Nedostaje kućni broj")
    # "Novi Sad" has no Rudnička - and nothing from Belgrade is offered for a Novi Sad list.
    assert rows["Rudnička 8"]["status"] == "bad"
    # "Beograd": the street is in a city district and in an outer village - the manager picks.
    pick = _by_line("Rudnička 8", city="Beograd")["Rudnička 8"]
    assert pick["status"] == "maybe" and pick["message"].startswith("Ova ulica postoji na više mesta")
    assert {o["city"] for o in pick["options"]} == {"Beograd (Vračar)", "Barajevo"}


class FakeHandler:
    """Just enough of lws.Handler to call its manager methods directly."""
    _api_manager_submit = lws.Handler._api_manager_submit
    _load_manager_request = lws.Handler._load_manager_request
    _create_email_target = lws.Handler._create_email_target

    def __init__(self, body):
        self.body, self.out = body, None

    def _read_json(self):
        return self.body

    def _client_ip(self):
        return "203.0.113.9"

    def _json(self, status, payload, extra_headers=None):
        self.out = (status, payload)

    def _backfill_target(self, target_id):
        pass


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(lws.auth, "start_login", lambda email, payload=None, **kw: calls.append((email, payload)))
    monkeypatch.setattr(lws, "rate_limited", lambda ip: False)
    monkeypatch.setattr(lws, "get_bot_username", lambda: "TestBot")
    notes = []
    monkeypatch.setattr(lws.managers, "notify_operator", lambda rid, n: notes.append((rid, n)))
    return calls, notes


ADDRESSES = [{"city": "Novi Sad", "ulica": "Futoška", "broj": "15"},
             {"city": "Futog", "ulica": "Cara Dušana", "broj": "8"},
             {"city": "Novi Sad", "ulica": "futoska", "broj": "15"}]   # same as the first


def test_submit_rechecks_every_address_and_sends_one_link(sent):
    calls, _ = sent
    h = FakeHandler({"email": "Uprava@Firma.rs", "name": "Firma", "channels": ["email"], "addresses": ADDRESSES})
    h._api_manager_submit()
    assert h.out == (200, {"ok": True, "count": 2})
    (email, payload), = calls
    assert email == "uprava@firma.rs" and payload["label"] == "2 adresa"
    saved = json.loads(lws.managers.get_request(payload["manager_request"])["addresses_json"])
    assert saved == [{"city": "Novi Sad", "ulica": "Futoška", "broj": "15"},
                     {"city": "Futog", "ulica": "Cara Dušana", "broj": "8"}]

    # An address the page never confirmed (or a tampered request) is refused, nothing is saved.
    bad = FakeHandler({"email": "a@b.rs", "channels": ["email"],
                       "addresses": [{"city": "Novi Sad", "ulica": "Nova ulica", "broj": "12"}]})
    bad._api_manager_submit()
    assert bad.out[0] == 400 and len(calls) == 1

    # The honeypot: answered like a success, nothing saved, no email.
    bot = FakeHandler({"email": "a@b.rs", "channels": ["email"], "addresses": ADDRESSES, "website": "x"})
    bot._api_manager_submit()
    assert bot.out == (200, {"ok": True, "count": 0}) and len(calls) == 1

    too_many = FakeHandler({"email": "a@b.rs", "channels": ["email"], "addresses": ADDRESSES * 70})
    too_many._api_manager_submit()
    assert too_many.out[0] == 400


def _subscriber(email="uprava@firma.rs"):
    from core.database import get_connection
    conn = get_connection()
    sid = conn.execute("INSERT INTO subscribers (email) VALUES (?)", (email,)).lastrowid
    conn.commit()
    conn.close()
    return sid


def _active(sid):
    from core.database import get_connection
    conn = get_connection()
    rows = conn.execute("SELECT id, city, ulica, broj, email_active, telegram_chat_id FROM targets "
                        "WHERE subscriber_id = ? AND is_active = 1 ORDER BY id", (sid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _manager_flags(sid):
    from core.database import get_connection
    conn = get_connection()
    flags = [r[0] for r in conn.execute(
        "SELECT manager_list FROM targets WHERE subscriber_id = ? AND is_active = 1 ORDER BY ulica", (sid,))]
    conn.close()
    return flags


def test_link_loads_the_list_once_and_the_operator_can_undo_it(sent):
    _, notes = sent
    sid = _subscriber()
    h = FakeHandler(None)
    h._create_email_target(sid, "uprava@firma.rs", "Novi Sad", "Bulevar oslobođenja", "46")   # already had it
    rid = lws.managers.create_request("uprava@firma.rs", None, ["email"],
                                      [{"city": "Novi Sad", "ulica": "Futoška", "broj": "15"},
                                       {"city": "Novi Sad", "ulica": "Bulevar oslobođenja", "broj": "46"}])
    assert h._load_manager_request(sid, rid) == (2, None)
    assert [t["ulica"] for t in _active(sid)] == ["Bulevar oslobođenja", "Futoška"]
    assert _manager_flags(sid) == [1, 1]                         # signed up as a manager
    assert notes == [(rid, 2)]
    assert h._load_manager_request(sid, rid) == (0, None)        # a second click loads nothing
    assert len(_active(sid)) == 2

    text = lws.managers.operator_text(lws.managers.get_request(rid), 2)
    assert "2 adresa uneto (1 novih, 1 već na nalogu)" in text

    result = lws.managers.remove(rid)                            # only what the list created
    assert result == (1, 1)
    assert lws.managers.removal_answer(result) == "Obrisano: 1 novih adresa. 1 je već bila na nalogu i ostaje."
    assert [t["ulica"] for t in _active(sid)] == ["Bulevar oslobođenja"]
    assert _manager_flags(sid) == [0]                            # a spam list: back to regular
    from core.database import get_connection
    conn = get_connection()
    gone = conn.execute("SELECT COUNT(*) FROM targets WHERE subscriber_id = ? AND ulica = 'Futoška'",
                        (sid,)).fetchone()[0]
    conn.close()
    assert gone == 0                                             # deleted, not left paused
    assert lws.managers.remove(rid) is None
    assert lws.managers.removal_answer(None) == "Već uklonjeno."


def test_a_list_removed_by_pausing_is_finished_off_by_deleting(sent):
    # Lists removed before ❌ deleted (27.10.2026: the operator's own test) only paused theirs.
    sid = _subscriber()
    rid = lws.managers.create_request("uprava@firma.rs", None, ["email"],
                                      [{"city": "Novi Sad", "ulica": "Futoška", "broj": "15"}])
    FakeHandler(None)._load_manager_request(sid, rid)
    from core.database import get_connection
    conn = get_connection()
    conn.execute("UPDATE targets SET is_active = 0 WHERE subscriber_id = ?", (sid,))
    conn.execute("UPDATE manager_requests SET status = 'REMOVED' WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    assert lws.managers.remove(rid) == (1, 0)
    assert lws.managers.remove(rid) is None


def test_telegram_choice_gives_one_link_for_every_address(sent, monkeypatch):
    sid = _subscriber()
    rid = lws.managers.create_request("uprava@firma.rs", None, ["telegram"],
                                      [{"city": "Novi Sad", "ulica": "Futoška", "broj": "15"},
                                       {"city": "Futog", "ulica": "Cara Dušana", "broj": "8"}])
    loaded, link = FakeHandler(None)._load_manager_request(sid, rid)
    assert loaded == 2 and link.startswith("https://t.me/TestBot?start=")
    assert all(t["email_active"] == 0 for t in _active(sid))     # Telegram only, as chosen

    from workers import telegram_bot as bot
    monkeypatch.setattr("core.matchmaker.backfill_target_against_active_outages", lambda tid: None)
    pending = bot.find_pending(link.split("start=")[1])
    assert pending["attach_all"] == 1 and pending["subscriber_id"] == sid
    assert bot.complete_attach_all(pending["id"], 4242, "upravnik", sid) == 2
    assert {t["telegram_chat_id"] for t in _active(sid)} == {"4242"}
    assert bot.complete_attach_all(pending["id"], 4242, "upravnik", sid) == 0   # used once


class VerifyHandler(FakeHandler):
    """The real /auth/verify POST, with the HTTP plumbing captured."""
    _auth_verify_consume = lws.Handler._auth_verify_consume
    _card_page = lws.Handler._card_page
    _session_cookie_header = lws.Handler._session_cookie_header

    def __init__(self, path):
        super().__init__(None)
        self.path, self.status, self.headers_out = path, None, {}
        import io
        self.wfile = io.BytesIO()

    def _drain_body(self):
        pass

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers_out[name] = value

    def end_headers(self):
        pass


@pytest.mark.parametrize("channels", [["email"], ["email", "telegram"]])
def test_email_link_loads_the_list_and_opens_the_account(monkeypatch, channels):
    links = []
    monkeypatch.setattr(lws.auth, "_send_login_email",
                        lambda to, link, is_signup, payload=None: links.append((link, payload)))
    monkeypatch.setattr(lws.auth, "_login_email_hits", {})
    monkeypatch.setattr(lws, "rate_limited", lambda ip: False)
    monkeypatch.setattr(lws, "get_bot_username", lambda: "TestBot")
    monkeypatch.setattr(lws.managers, "notify_operator", lambda rid, n: None)
    h = FakeHandler({"email": "uprava@firma.rs", "channels": channels, "addresses": ADDRESSES[:2]})
    h._api_manager_submit()
    assert h.out == (200, {"ok": True, "count": 2})
    (link, payload), = links
    assert payload["label"] == "2 adresa"

    v = VerifyHandler("/auth/verify?" + link.split("?", 1)[1])
    v._auth_verify_consume()
    assert "session=" in v.headers_out["Set-Cookie"]
    from core.database import get_connection
    conn = get_connection()
    sid = conn.execute("SELECT id FROM subscribers WHERE email = 'uprava@firma.rs'").fetchone()[0]
    conn.close()
    assert [t["ulica"] for t in _active(sid)] == ["Futoška", "Cara Dušana"]
    if channels == ["email"]:
        assert v.status == 302 and v.headers_out["Location"] == "/dashboard"
    else:
        page = v.wfile.getvalue().decode()
        assert v.status == 200 and "Poveži Telegram" in page and "https://t.me/TestBot?start=" in page


def test_operator_note_names_the_list_and_offers_undo():
    rid = lws.managers.create_request("uprava@firma.rs", "Firma", ["email"],
                                      [{"city": "Novi Sad", "ulica": "Futoška", "broj": "15"}])
    text = lws.managers.operator_text(lws.managers.get_request(rid), 1)
    assert f"Upravnik #{rid}: 1 adresa uneto" in text and "Futoška 15, Novi Sad" in text
    assert lws.managers.CALLBACK.match(f"mg:rm:{rid}")
