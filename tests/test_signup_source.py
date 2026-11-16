"""Where signups come from: the form sends the referring site's host (or our own link's ?src=)
and the city page it came through; a NEW account keeps only the site's name, once, and the
operator's /stats and new-signup DM show it."""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    import core.auth as auth
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "source.db"))
    database.init_db()
    monkeypatch.setattr(auth, "_send_login_email", lambda *a, **k: None)
    # The per-address mail throttle is module state: without a fresh one, a second login for the
    # same address inside 90 s is skipped before it reaches the account at all.
    monkeypatch.setattr(auth, "_login_email_hits", {})
    return database


def _source(database, email):
    conn = database.get_connection()
    row = conn.execute("SELECT signup_source, signup_page FROM subscribers WHERE email = ?",
                       (email,)).fetchone()
    conn.close()
    return tuple(row) if row else "NO ACCOUNT"


@pytest.mark.parametrize("raw, expected", [
    ({"src": "www.google.com", "from": "kraljevo"}, ("google", "kraljevo")),
    ({"src": "com.google.android.googlequicksearchbox"}, ("google", None)),     # the Google app
    ({"src": "l.facebook.com"}, ("facebook", None)),
    ({"src": "lm.facebook.com"}, ("facebook", None)),
    ({"src": "mail.google.com"}, ("gmail", None)),
    ({"src": "t.co"}, ("x", None)),
    ({"src": "https://www.021.rs/story/Novi-Sad/123?x=1"}, ("021.rs", None)),  # only the site's name
    ({"src": "pks-mejl"}, ("pks-mejl", None)),                               # our own ?src= tag
    ({"src": "", "from": "novi-sad"}, ("direct", "novi-sad")),
    ({"src": "najavaradova.rs"}, ("direct", None)),                          # our own pages
    ({"src": "app.najavaradova.rs"}, ("direct", None)),
    ({"src": "<script>", "from": "../../etc/passwd"}, ("script", "etcpasswd")),
    ({}, ("direct", None)),
    (None, (None, None)),                                                  # an old page sent nothing
    ("google", (None, None)),
])
def test_only_a_sites_name_is_kept(raw, expected):
    from core.auth import clean_source
    assert clean_source(raw) == expected


def test_a_new_account_keeps_where_it_came_from_once(db):
    from core import auth
    auth.start_login("novi@example.com", payload={"city": "X"},
                     source={"src": "www.google.com", "from": "kraljevo"})
    assert _source(db, "novi@example.com") == ("google", "kraljevo")

    # coming back later through another channel doesn't rewrite it
    auth._login_email_hits.clear()
    auth.start_login("novi@example.com", payload={"city": "Y"}, source={"src": "l.facebook.com"})
    auth._login_email_hits.clear()
    auth.start_login("novi@example.com")
    assert _source(db, "novi@example.com") == ("google", "kraljevo")

    auth.start_login("stari-klijent@example.com", payload={"city": "X"})   # page without the field
    assert _source(db, "stari-klijent@example.com") == (None, None)


def test_bulk_registered_accounts_are_marked_manual(db, tmp_path):
    from scripts.bulk_register import run
    csv = tmp_path / "a.csv"
    csv.write_text("city,ulica,broj\n", encoding="utf-8")
    run(str(csv), "email", "upravnik@example.com")
    assert _source(db, "upravnik@example.com") == ("rucno", None)


def test_stats_and_signup_message_say_where_people_came_from(db):
    from core import auth
    from core.admin_stats import collect, format_summary, new_since, format_new
    auth.start_login("a@example.com", payload={"city": "X"}, source={"src": "www.google.com", "from": "kraljevo"})
    auth.start_login("b@example.com", payload={"city": "X"}, source={"src": "www.google.rs", "from": "nis"})
    auth.start_login("c@example.com", payload={"city": "X"}, source={"src": ""})
    conn = db.get_connection()
    a = conn.execute("SELECT id FROM subscribers WHERE email = 'a@example.com'").fetchone()[0]
    conn.execute("INSERT INTO targets (subscriber_id, city, ulica, broj, email, is_active) "
                 "VALUES (?, 'Kraljevo', 'Jug Bogdanova', '1', 'a@example.com', 1)", (a,))
    conn.commit()
    conn.close()

    s = collect(days=7)
    assert s["sources"] == [("google", 2, 1), ("direct", 1, 0)]
    text = format_summary(s)
    assert "• google — 2  (1 bez adrese)" in text
    assert "• direct — 1  (1 bez adrese)" in text
    assert "• preko stranica: kraljevo 1, nis 1" in text

    body = format_new(new_since(0, 0))
    assert "👤 a@example.com (nov) · izvor: google, stranica kraljevo" in body
    assert "👤 c@example.com (nov) · izvor: direct — još bez adrese" in body


def test_city_pages_carry_the_source_to_the_signup_form():
    from scripts import build_city_pages as cp
    html = cp.page_html("Kraljevo", "kraljevo", {}, "2026-09-28T09:00:00+00:00", [])
    assert 'var page = "kraljevo"' in html
    assert 'u.searchParams.set("from", page)' in html
    # The one signup link opens the home page's form with the place set; the click handler above
    # adds src and from to it like to every other link to our own pages.
    assert '<a class="btn" data-signup href="/?prijava=1&amp;grad=Kraljevo">🔔 Prijavi svoju adresu</a>' in html
    # opened in place, the form gets the same two tags from the page itself
    assert 'source: { src: src, from: page }' in html
    assert 'u.searchParams.set("src", src)' in html
