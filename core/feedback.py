"""A reader's own 👍 / 👎 on an alert: was it right for their address?

Telegram: two buttons under an alert about one outage (workers/telegram_worker.py), handled in
workers/telegram_bot.py. Email: two links under each outage (workers/email_worker.py) to
app.najavaradova.rs/tacno, which only asks; the answer is written on the confirm button's POST, so
a mail scanner opening the link records nothing (scripts/local_web_server.py).

Who may answer: only someone the alert was really sent to. On Telegram that is the chat that
tapped (from the tap itself, never from the button's data); by email, the address's unsubscribe
token from the link. The button carries only the outage id.

One answer per person and outage (user_feedback), and a second tap changes it. A 👎 is also sent
to the operator straight away - a reader saying an alert is wrong is the rarest and most useful
signal there is - and it is put in the next weekly review (core/review.py) to be confirmed.
These answers are biased (people mostly answer when something is wrong), so /stats shows them
next to the weekly random sample, never instead of it.
"""
import re

from core.database import get_connection

CALLBACK = re.compile(r"^fb:(y|n):(\d+)$")
VERDICTS = {"y": "RIGHT", "n": "WRONG"}


def buttons(outage_id: int) -> list[dict]:
    return [{"text": "👍 Tačno", "callback_data": f"fb:y:{outage_id}"},
            {"text": "👎 Nije tačno", "callback_data": f"fb:n:{outage_id}"}]


def _targets(conn, outage_id: int, channel: str, who: str) -> list:
    """The addresses this outage's alert was really sent to, over `channel`, for this chat id
    (telegram) or unsubscribe token (email)."""
    match = "t.telegram_chat_id = ?" if channel == "telegram" else "t.unsub_token = ?"
    return conn.execute(
        f"SELECT DISTINCT t.id, t.ulica, t.broj, t.city FROM message_queue m "
        f"JOIN targets t ON t.id = m.target_id "
        f"WHERE m.outage_id = ? AND m.channel = ? AND m.status = 'SENT' AND {match}",
        (outage_id, channel, str(who))).fetchall()


def record(outage_id: int, channel: str, who, verdict: str) -> int:
    """Store a reader's answer (RIGHT / WRONG) for every address of theirs the alert went to.
    Returns how many were stored; 0 when this person never got this alert."""
    if verdict not in ("RIGHT", "WRONG") or who in (None, ""):
        return 0
    conn = get_connection()
    try:
        targets = _targets(conn, outage_id, channel, who)
        newly_wrong = []
        for t in targets:
            before = conn.execute("SELECT verdict FROM user_feedback WHERE outage_id = ? AND target_id = ?",
                                  (outage_id, t["id"])).fetchone()
            conn.execute(
                "INSERT INTO user_feedback (outage_id, target_id, channel, verdict) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(outage_id, target_id) DO UPDATE SET verdict = excluded.verdict, "
                "channel = excluded.channel, updated_at = CURRENT_TIMESTAMP",
                (outage_id, t["id"], channel, verdict))
            if verdict == "WRONG" and (not before or before["verdict"] != "WRONG"):
                newly_wrong.append(t)
        conn.commit()
        outage = conn.execute("SELECT id, region, category, outage_date, source_url FROM outages WHERE id = ?",
                              (outage_id,)).fetchone() if newly_wrong else None
    finally:
        conn.close()
    if outage:
        _tell_operator(outage, newly_wrong)
    return len(targets)


def _tell_operator(outage, targets) -> None:
    from core.send_check import operator_post
    places = "; ".join(f"{t['ulica']} {t['broj'] or ''}, {t['city']}".replace(" ,", ",") for t in targets)
    lines = [f"👎 Čitalac kaže da obaveštenje nije tačno: #{outage['id']} · {outage['category'] or ''} "
             f"· {outage['outage_date'] or ''}",
             f"Adresa: {places}"]
    if outage["source_url"]:
        lines.append(f"Objava: {outage['source_url']}")
    lines.append("Biće u ponedeljnoj proveri.")
    operator_post("sendMessage", {"text": "\n".join(lines)})
