"""Golden alerts: real past notices replayed against test addresses, each with the right answer.

The one question the product must get right - does this notice reach this address, and on which
day - checked on real notices rather than made-up ones. Each case in tests/golden/cases.json names
a stored notice (tests/golden/notices.json: the raw text and the model's stored answer, so no
Gemini call is made) and a few addresses:

    {"street": "Braće Jerkovića", "number": "189", "place": "Beograd (Voždovac)",
     "expect": "alert", "day": "4.11."}

expect "alert" passes when a message is queued or held for the operator; "none" passes only when
nothing is queued or held - a hold is usually released by the second model agreeing, as #6514's
was, so it is no safeguard to count on. "day" is the day the message must
name. A case marked "gap" documents a known wrong answer not fixed yet: it is expected to fail,
and passing it makes the test fail too (strict xfail), so the fix gets the mark removed.

The clock is frozen at the moment the notice was first scraped, so an old notice is not skipped
as past. Telegram, Gemini and the send check are stubbed out: nothing leaves the machine.

A same-day BVK fault message ("u blizini ... otklonjen do 15:00") names no day, so its cases
carry no "day". A "check" on an address is an open question to the operator, not read here.

Addresses are neighbours of real subscribers' (another house number on the same stretch), never
a subscriber's own. Needs data/rgz.db and data/serbia_streets.db, so it skips in CI and runs
wherever both exist (locally, and on the server).
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from spatial.spatial_engine import RGZ_DB_PATH, SERBIA_DB_PATH

GOLDEN = Path(__file__).parent / "golden"
NOTICES = {n["id"]: n for n in json.loads((GOLDEN / "notices.json").read_text(encoding="utf-8"))}
CASES = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))

pytestmark = pytest.mark.skipif(
    not (os.path.exists(RGZ_DB_PATH) and os.path.exists(SERBIA_DB_PATH)),
    reason="data/rgz.db and data/serbia_streets.db not present",
)


@pytest.fixture()
def replay(tmp_path, monkeypatch):
    import core.broadcast as broadcast
    import core.database as database
    import core.matchmaker as mm
    import core.send_check as send_check

    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "golden.db"))
    database.init_db()
    monkeypatch.setattr(mm, "send_operator_alert", lambda *a, **k: None)
    monkeypatch.setattr(mm, "send_operator_note", lambda *a, **k: None)
    monkeypatch.setattr(mm, "parse_announcement", lambda *a, **k: None)
    monkeypatch.setattr(send_check, "check", lambda *a, **k: None)
    monkeypatch.setattr(broadcast, "create_draft", lambda *a, **k: None)

    def run(case):
        notice = NOTICES[case["notice"]]
        frozen = datetime.fromisoformat(notice["created_at"])

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        monkeypatch.setattr(mm, "datetime", Frozen)
        conn = database.get_connection()
        ids = []
        for i, a in enumerate(case["addresses"]):
            sid = conn.execute("INSERT INTO subscribers (email) VALUES (?)", (f"g{i}@example.com",)).lastrowid
            tid = conn.execute(
                "INSERT INTO targets (subscriber_id, city, ulica, broj, email, unsub_token, is_active, "
                "email_active) VALUES (?, ?, ?, ?, ?, ?, 1, 1)",
                (sid, a["place"], a["street"], a["number"], f"g{i}@example.com", f"u{i}"),
            ).lastrowid
            row = conn.execute("SELECT id, city, opstina, naselje, ulica FROM targets WHERE id = ?",
                               (tid,)).fetchone()
            mm.ensure_target_hexes(conn.cursor(), row)
            ids.append(tid)
        conn.commit()
        conn.close()

        nearby = tuple(notice["nearby"]) if notice.get("nearby") else None
        mm.process_and_queue_outage(json.loads(notice["parsed_json"]), notice["raw_text"],
                                    text_hash=f"golden-{notice['id']}", category=notice["category"],
                                    source_url=notice["source_url"],
                                    gate_region=notice.get("gate_region"), nearby=nearby)

        conn = database.get_connection()
        got = []
        for tid in ids:
            rows = conn.execute("SELECT status, custom_message FROM message_queue WHERE target_id = ?",
                                (tid,)).fetchall()
            got.append([(r["status"], r["custom_message"]) for r in rows])
        conn.close()
        return got

    return run


def _names_day(message: str, day: str) -> bool:
    """'4.11.' is named by '04.11.2026', '4.11.' or '4/5.11.' - not by '14.11.'."""
    d, m = (int(x) for x in day.strip(".").split("."))
    for found_d, extra, found_m in re.findall(r"(?<!\d)(\d{1,2})(?:\s*/\s*(\d{1,2}))?\.(\d{1,2})\.", message or ""):
        if int(found_m) == m and d in (int(found_d), int(extra or 0)):
            return True
    return False


def _wrong(case, got) -> list[str]:
    out = []
    for a, rows in zip(case["addresses"], got):
        where = f"{a['street']} {a['number']} ({a['place']})"
        queued = [msg for status, msg in rows if status == "QUEUED"]
        held = [msg for status, msg in rows if status == "HELD"]
        if a["expect"] == "alert":
            if not (queued or held):
                out.append(f"{where}: expected an alert, got none")
            elif a.get("day") and not any(_names_day(m, a["day"]) for m in queued + held):
                out.append(f"{where}: expected day {a['day']}, message: {(queued + held)[0]!r}")
        elif queued or held:
            out.append(f"{where}: expected no alert, got{' (held)' if not queued else ''}: "
                       f"{(queued + held)[0]!r}")
    return out


def _param(case):
    marks = [pytest.mark.xfail(strict=True, reason=case["gap"])] if case.get("gap") else []
    return pytest.param(case, id=f"n{case['notice']}", marks=marks)


@pytest.mark.parametrize("case", [_param(c) for c in CASES])
def test_golden_notice(replay, case):
    wrong = _wrong(case, replay(case))
    assert not wrong, f"#{case['notice']} ({case.get('why', '')}):\n  " + "\n  ".join(wrong)
