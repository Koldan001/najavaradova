"""Važno obaveštenje (core/broadcast.py): a city-wide notice reaches a city only after the
operator's ✅, once per person, never while the operator hasn't decided, and never to someone who
turned these off. And a heating-start notice is told apart from a heating cut."""
import os
import sys
from datetime import datetime

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.filters import heating_start

NIS_PROBE = ("Датум: 12.10.2026. Обавештавамо вас да ће ЈКП „Градска топлана“ Ниш, почев од "
             "понедељка 12.10.2026. године започети са топлим пробама на територији целог града. "
             "Топле пробе ће почети у предвиђеном термину почев од 05.00 часова до постизања потпуне "
             "функционалности система. Уколико у преподневним часовима изостане испорука топлотне "
             "енергије или дође до евентуалног цурења на инсталацијама, корисници могу пријавити квар.")
KRUSEVAC_PROBE_WITH_CUT = ("ЈКП „Градска топлана“ Крушевац наставља са применом функционалних топлих "
                           "проба. Данас ће без грејања бити вртић „Колибри“ као и улице Хиландарска "
                           "бр. 7,9 и 11.")


def test_a_heating_start_is_told_apart_from_a_cut():
    assert heating_start(NIS_PROBE) == "PROBE"
    assert heating_start("Počinje grejna sezona u Novom Sadu 15. oktobra.") == "SEASON"
    assert heating_start("Почетак грејне сезоне је 15.10.") == "SEASON"
    # tople probe that leave named buildings without heating: an ordinary outage
    assert heating_start(KRUSEVAC_PROBE_WITH_CUT) is None
    assert heating_start("Planirani radovi na toplovodnoj mreži u Ulici braće Jerković") is None
    assert heating_start("Prekid isporuke toplotne energije zbog kvara") is None
    assert heating_start(None) is None
    # what might go wrong, said in an "if" sentence, is not a cut (#4298's last paragraph)
    assert heating_start(NIS_PROBE + " Апелујемо на стрпљење грађана уколико дође до евентуалних "
                                     "поремећаја у испоруци топлотне енергије.") == "PROBE"
    # works done before the season are not the season starting (#1329)
    assert heating_start("Почиње пуњење мреже, како би систем био спреман пред почетак грејне сезоне.") is None


def test_the_map_card_says_the_heating_is_switched_on():
    from scripts.build_web_data import summarize
    headline, effects = summarize("GREJANJE", "tople probe", NIS_PROBE)
    assert headline.startswith("Tople probe")
    assert not any("prekid" in e.lower() or "bez grejanja" in e.lower() for e in effects)
    assert summarize("GREJANJE", None, KRUSEVAC_PROBE_WITH_CUT)[1][0].startswith("Bez grejanja")


def test_the_draft_takes_the_clock_time_not_the_date():
    from core.broadcast import heating_start_text
    text = heating_start_text("НИШ", "12.10.2026", NIS_PROBE, season=False)
    assert "Niš" in text and "12.10.2026" in text and "od 05:00" in text
    assert "12:10" not in text


def test_city_key():
    from core.broadcast import city_key
    assert city_key("Niš (medijana)") == city_key("НИШ") == city_key("Nis") == "nis"
    assert city_key("Beograd (voždovac)") == "beograd"
    assert city_key("Veternik") != city_key("Novi Sad")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    import core.broadcast as broadcast
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "bc.db"))
    database.init_db()
    sent_to_operator = []
    monkeypatch.setattr(broadcast, "_operator_post",
                        lambda method, payload: sent_to_operator.append(payload) or {"ok": True})
    database.operator_messages = sent_to_operator
    return database


def _target(database, city, email=None, chat=None, active=1, off=0):
    conn = database.get_connection()
    tid = conn.execute(
        "INSERT INTO targets (city, ulica, broj, email, telegram_chat_id, unsub_token, is_active, "
        "email_active, telegram_active, broadcasts_off) VALUES (?, 'X', '1', ?, ?, ?, ?, 1, 1, ?)",
        (city, email, chat, f"tok{city}{email}{chat}", active, off)).lastrowid
    conn.commit()
    conn.close()
    return tid


def test_one_message_per_person_in_that_city_only(db):
    from core.broadcast import recipients
    a1 = _target(db, "Niš (medijana)", email="a@x.rs", chat="100")
    _target(db, "Niš (palilula)", email="A@x.rs", chat="100")  # the same person, second address
    b = _target(db, "Niš", email="b@x.rs")
    _target(db, "Novi Sad", email="c@x.rs", chat="300")
    _target(db, "Niš", email="d@x.rs", active=0)
    _target(db, "Niš", chat="500", off=1)
    conn = db.get_connection()
    found = recipients(conn, "НИШ")
    conn.close()
    assert sorted(found) == sorted([(a1, "telegram"), (a1, "email"), (b, "email")])


def test_nothing_goes_out_until_the_operator_approves_and_only_once(db, monkeypatch):
    import core.broadcast as broadcast
    _target(db, "Niš", email="a@x.rs", chat="100")
    bid = broadcast.create_draft("НИШ", "🔥 Niš: tople probe", source_outage_id=4298)
    assert bid and db.operator_messages, "the operator is shown the draft"
    assert broadcast.create_draft("НИШ", "again", source_outage_id=4298) is None  # a re-scrape
    assert broadcast.claim("telegram") == [] and broadcast.claim("email") == []

    noon = datetime(2026, 10, 11, 12, 0)
    assert broadcast.approve(bid, now=noon) == 2
    assert broadcast.approve(bid, now=noon) is None  # a second tap queues nothing
    assert broadcast.reject(bid) is False

    posted = []

    class Ok:
        ok = True

        @staticmethod
        def json():
            return {"ok": True}

    monkeypatch.setattr(broadcast.requests, "post", lambda url, json, timeout: posted.append(json) or Ok)
    assert broadcast.send_telegram() == 1
    assert "/obavestenja off" in posted[0]["text"] and posted[0]["chat_id"] == "100"
    assert broadcast.send_telegram() == 0  # already sent


def test_a_rejected_draft_sends_nothing(db):
    import core.broadcast as broadcast
    _target(db, "Niš", chat="100")
    bid = broadcast.create_draft("Niš", "text")
    assert broadcast.reject(bid)
    assert broadcast.approve(bid) is None
    assert broadcast.claim("telegram") == []


def test_approved_at_night_waits_for_the_morning(db):
    import core.broadcast as broadcast
    _target(db, "Niš", chat="100")
    bid = broadcast.create_draft("Niš", "text")
    assert broadcast.approve(bid, now=datetime(2099, 10, 11, 23, 30)) == 1
    assert broadcast.claim("telegram") == []


def test_a_city_with_nobody_gets_no_draft(db):
    import core.broadcast as broadcast
    assert broadcast.create_draft("Kikinda", "text") is None
    assert "nema prijavljenih" in db.operator_messages[-1]["text"]


def test_only_the_operator_can_approve(db, monkeypatch):
    import core.broadcast as broadcast
    import workers.telegram_bot as bot
    _target(db, "Niš", chat="100")
    bid = broadcast.create_draft("Niš", "text")
    monkeypatch.setattr(bot.config, "OPERATOR_CHAT_ID", "42", raising=False)
    monkeypatch.setattr(bot.requests, "post", lambda *a, **k: None)
    monkeypatch.setattr(bot, "send_message", lambda *a, **k: True)
    bot.handle_callback({"id": "1", "data": f"bc:ok:{bid}", "from": {"id": 4242}})
    assert broadcast.claim("telegram") == []
    bot.handle_callback({"id": "2", "data": f"bc:ok:{bid}", "from": {"id": 42}})
    assert len(broadcast.claim("telegram")) == 1


def test_opting_out_keeps_outage_alerts(db):
    import core.broadcast as broadcast
    tid = _target(db, "Niš", email="a@x.rs", chat="100")
    assert broadcast.set_opt_out(chat_id="100") == 1
    conn = db.get_connection()
    row = conn.execute("SELECT is_active, broadcasts_off FROM targets WHERE id = ?", (tid,)).fetchone()
    assert broadcast.recipients(conn, "Niš") == []
    conn.close()
    assert row["is_active"] == 1 and row["broadcasts_off"] == 1


def test_the_email_carries_its_own_opt_out_link():
    from workers.email_worker import build_message
    msg = build_message("a@x.rs", "tok", "🔥 Niš: tople probe", subject="Važno obaveštenje — Niš",
                        opt_out_link="https://app.najavaradova.rs/obavestenja?token=tok")
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "obavestenja?token=tok" in plain and "obavestenja?token=tok" in html
    assert "unsubscribe?token=tok" in plain
