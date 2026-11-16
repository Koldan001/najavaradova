"""Referral attribution: a partner link credits a NEW account once, never an existing one, and an
unknown ref never credits anyone. Plus the operator paths (bulk_register --ref, partner report)."""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    import core.auth as auth
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "partners.db"))
    database.init_db()
    monkeypatch.setattr(auth, "_send_login_email", lambda *a, **k: None)
    conn = database.get_connection()
    conn.execute("INSERT INTO partners (slug, name, commission_pct) VALUES ('zgradahub', 'ZgradaHub', 25)")
    conn.execute("INSERT INTO partners (slug, name, commission_pct) VALUES ('drugi', 'Drugi', 25)")
    conn.commit()
    conn.close()
    return database


def _referred_by(database, email):
    conn = database.get_connection()
    row = conn.execute("SELECT referred_by FROM subscribers WHERE email = ?", (email,)).fetchone()
    conn.close()
    return row["referred_by"] if row else "NO ACCOUNT"


def test_known_ref_credits_new_account(db):
    from core import auth
    auth.start_login("novi@example.com", payload={"city": "X"}, ref="ZgradaHub ")
    assert _referred_by(db, "novi@example.com") == "zgradahub"


def test_unknown_or_malformed_ref_credits_nobody(db):
    from core import auth
    auth.start_login("a@example.com", payload={"city": "X"}, ref="izmisljen")
    auth.start_login("b@example.com", payload={"city": "X"}, ref="../../etc")
    auth.start_login("c@example.com", payload={"city": "X"}, ref=12345)
    auth.start_login("d@example.com", payload={"city": "X"})
    for email in ("a@example.com", "b@example.com", "c@example.com", "d@example.com"):
        assert _referred_by(db, email) is None


def test_existing_account_is_never_reattributed(db):
    """First touch wins: a direct signup later clicking a partner link, or a partner-A account
    clicking partner B's link, keeps what it had."""
    from core import auth
    auth.start_login("direct@example.com", payload={"city": "X"})
    auth.start_login("direct@example.com", payload={"city": "Y"}, ref="zgradahub")
    assert _referred_by(db, "direct@example.com") is None

    auth.start_login("a-first@example.com", payload={"city": "X"}, ref="zgradahub")
    auth.start_login("a-first@example.com", payload={"city": "Y"}, ref="drugi")
    auth.start_login("a-first@example.com", payload=None)  # plain login
    assert _referred_by(db, "a-first@example.com") == "zgradahub"


def _csv(tmp_path):
    p = tmp_path / "addresses.csv"
    p.write_text("city,ulica,broj\n", encoding="utf-8")  # header only - no street data needed
    return str(p)


def test_bulk_register_ref_credits_account(db, tmp_path):
    from scripts.bulk_register import run
    run(_csv(tmp_path), "email", "upravnik@example.com", ref="zgradahub")
    assert _referred_by(db, "upravnik@example.com") == "zgradahub"

    # A second import with another partner doesn't steal the attribution.
    run(_csv(tmp_path), "email", "upravnik@example.com", ref="drugi")
    assert _referred_by(db, "upravnik@example.com") == "zgradahub"


def test_bulk_register_rejects_bad_ref_usage(db, tmp_path):
    from scripts.bulk_register import run
    run(_csv(tmp_path), "email", "typo@example.com", ref="zgradhub")      # unknown partner
    assert _referred_by(db, "typo@example.com") == "NO ACCOUNT"          # nothing created
    run(_csv(tmp_path), "telegram", "123", ref="zgradahub")              # no account to credit
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) c FROM subscribers").fetchone()["c"] == 0
    conn.close()


def test_report_tiers_and_partner_share(db):
    from core import auth
    from scripts.partners import report_rows
    auth.start_login("mali@example.com", payload={"city": "X"}, ref="zgradahub")
    auth.start_login("veliki@example.com", payload={"city": "X"}, ref="zgradahub")
    conn = db.get_connection()
    big = conn.execute("SELECT id FROM subscribers WHERE email = 'veliki@example.com'").fetchone()["id"]
    for i in range(12):                                   # 12 active -> tier M
        conn.execute("INSERT INTO targets (city, ulica, broj, subscriber_id, is_active) "
                     "VALUES ('X', 'Y', ?, ?, 1)", (str(i), big))
    conn.execute("INSERT INTO targets (city, ulica, broj, subscriber_id, is_active) "
                 "VALUES ('X', 'Y', 'old', ?, 0)", (big,))  # inactive - not counted
    conn.commit()

    rows = {r[2]: r for r in report_rows(conn, "zgradahub")}
    conn.close()
    assert rows["mali@example.com"][4:] == (0, "Free", 0, 0)
    assert rows["veliki@example.com"][4:] == (12, "M", 29, 7.25)


def test_tier_boundaries():
    from core.partners import tier_for
    assert tier_for(2) == ("Free", 0)
    assert tier_for(3) == ("S", 12)
    assert tier_for(10) == ("S", 12)
    assert tier_for(11) == ("M", 29)
    assert tier_for(100) == ("L", 59)
    assert tier_for(101) == ("Custom", None)
