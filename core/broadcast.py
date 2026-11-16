"""Važno obaveštenje: a rare notice for everyone in one city (heating season starting, tople
probe), sent only after the operator approves it on Telegram.

An ordinary alert goes to the addresses a notice names. This one goes to every address whose
city field is that city, so it never depends on the map - no geometry, no leak. Because it reaches
a whole city at once, nothing goes out until the operator taps ✅ on the draft
(workers/telegram_bot.py). Two ways in:

- core/matchmaker sees a heating-start notice ("tople probe", "početak grejne sezone") and makes
  a draft from it instead of alerting anyone (#4298, Niš toplana's tople probe, 12.10.2026: the
  map had it as "Kvar na toplovodnoj mreži, moguć prekid grejanja" over all of Niš);
- `python -m scripts.broadcast --city "Novi Sad" --text "..."` for what no scraper reads (Novi
  Sad's season start is often only in the news).

One message per person (a Telegram chat, an email address), whatever number of addresses they
have there. Telegram's quiet hours hold an approved notice until 07:00. Anyone can turn these off
(/obavestenja off, or the link in the email) without losing their outage alerts.
"""
import re
from datetime import datetime, timedelta

import requests

import config
from core import delivery
from core.database import get_connection
from core.filters import _fold

# The callback data on the draft's buttons: "bc:ok:<id>" sends, "bc:no:<id>" drops.
CALLBACK = re.compile(r"^bc:(ok|no):(\d+)$")

OPT_OUT_BASE_URL = "https://app.najavaradova.rs/obavestenja"
TELEGRAM_FOOTER = "Retko obaveštenje za ceo grad. Da ih ne dobijate: /obavestenja off"


def city_key(city) -> str:
    """The city an address or a notice is in, as one comparable word: "Niš (medijana)", "НИШ" and
    "Nis" are all "nis"; "Beograd (voždovac)" is "beograd". Veternik stays Veternik - it is not
    Novi Sad's toplana network, and the operator sees the count before anything goes out."""
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", " ", _fold(city or ""))).strip()


def city_label(city) -> str:
    """"НИШ" -> "Niš", for the draft and the subject line."""
    from spatial.spatial_engine import cyrillic_to_latin
    return re.sub(r"\(.*?\)", "", cyrillic_to_latin(city or "")).strip().title()


def recipients(conn, city) -> list[tuple[int, str]]:
    """(target_id, channel), one per person in `city`: per Telegram chat and per email address,
    the oldest of their addresses there. A person who turned these off on any of their addresses
    is left out."""
    key = city_key(city)
    rows = conn.execute(
        "SELECT id, city, telegram_chat_id, email, telegram_active, email_active, broadcasts_off "
        "FROM targets WHERE is_active = 1 ORDER BY id").fetchall()
    rows = [r for r in rows if key and city_key(r["city"]) == key]
    off_chats = {r["telegram_chat_id"] for r in rows if r["broadcasts_off"] and r["telegram_chat_id"]}
    off_mails = {(r["email"] or "").lower() for r in rows if r["broadcasts_off"] and r["email"]}
    out, seen = [], set()
    for r in rows:
        chat, mail = r["telegram_chat_id"], (r["email"] or "").lower()
        if chat and r["telegram_active"] and chat not in off_chats and ("t", chat) not in seen:
            seen.add(("t", chat))
            out.append((r["id"], "telegram"))
        if mail and r["email_active"] and mail not in off_mails and ("e", mail) not in seen:
            seen.add(("e", mail))
            out.append((r["id"], "email"))
    return out


def _count_line(found) -> str:
    tg = sum(1 for _, c in found if c == "telegram")
    return f"{len(found)} (Telegram {tg}, email {len(found) - tg})"


def _operator_post(method: str, payload: dict) -> dict | None:
    token, chat = config.TELEGRAM_BOT_TOKEN, getattr(config, "OPERATOR_CHAT_ID", None)
    if not token or not chat:
        print(f"[broadcast] (no OPERATOR_CHAT_ID/TELEGRAM_BOT_TOKEN) {payload.get('text', '')}")
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json={"chat_id": chat, **payload}, timeout=10)
        return r.json() if r.ok else None
    except Exception as e:
        print(f"[broadcast] {method} failed: {e}")
        return None


def send_preview(broadcast_id: int) -> bool:
    """The draft as the operator sees it: who it would reach, the text, and ✅ / ❌."""
    conn = get_connection()
    b = conn.execute("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
    found = recipients(conn, b["city"]) if b else []
    conn.close()
    if not b:
        return False
    text = (f"📣 Važno obaveštenje #{b['id']} - {city_label(b['city'])}\n"
            f"Primalaca: {_count_line(found)}\n\n"
            f"{telegram_text(b)}\n\n"
            f"Izmena teksta: /izmeni {b['id']} <novi tekst>")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Pošalji", "callback_data": f"bc:ok:{b['id']}"},
        {"text": "❌ Odbaci", "callback_data": f"bc:no:{b['id']}"},
    ]]}
    return bool(_operator_post("sendMessage", {"text": text[:4000], "reply_markup": keyboard,
                                               "disable_web_page_preview": True}))


def create_draft(city: str, body: str, source_url: str | None = None,
                 source_outage_id: int | None = None) -> int | None:
    """Store a draft and show it to the operator. None when this notice already has one (a
    re-scrape of the same post), or when nobody in that city would get it - then the operator only
    hears that it was seen."""
    conn = get_connection()
    if source_outage_id is not None and conn.execute(
            "SELECT 1 FROM broadcasts WHERE source_outage_id = ?", (source_outage_id,)).fetchone():
        conn.close()
        return None
    if not recipients(conn, city):
        conn.close()
        _operator_post("sendMessage", {
            "text": f"📣 Obaveštenje za ceo grad ({city_label(city)}), ali tamo nema prijavljenih "
                    f"adresa - ništa nije poslato.\n\n{body}",
            "disable_web_page_preview": True})
        return None
    cur = conn.execute(
        "INSERT INTO broadcasts (city, body, source_url, source_outage_id) VALUES (?, ?, ?, ?)",
        (city, body.strip(), source_url, source_outage_id))
    conn.commit()
    broadcast_id = cur.lastrowid
    conn.close()
    send_preview(broadcast_id)
    return broadcast_id


def edit(broadcast_id: int, body: str) -> bool:
    """/izmeni: a new text for a draft still waiting; the operator gets the new preview."""
    conn = get_connection()
    changed = conn.execute("UPDATE broadcasts SET body = ? WHERE id = ? AND status = 'DRAFT'",
                           (body.strip(), broadcast_id)).rowcount
    conn.commit()
    conn.close()
    return bool(changed) and send_preview(broadcast_id)


def approve(broadcast_id: int, now: datetime | None = None) -> int | None:
    """✅: queue it for everyone in the city as they are now. In the quiet hours it waits until
    07:00. None when it isn't a waiting draft any more (a second tap, or already dropped) -
    nothing is queued twice."""
    now = now or delivery.serbia_now()
    conn = get_connection()
    until = delivery.quiet_until(now)
    claimed = conn.execute(
        "UPDATE broadcasts SET status = 'APPROVED', decided_at = CURRENT_TIMESTAMP, send_after = ? "
        "WHERE id = ? AND status = 'DRAFT'",
        (delivery.to_utc_sql(until) if until else None, broadcast_id)).rowcount
    if not claimed:
        conn.close()
        return None
    city = conn.execute("SELECT city FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()["city"]
    found = recipients(conn, city)
    conn.executemany(
        "INSERT OR IGNORE INTO broadcast_sends (broadcast_id, target_id, channel) VALUES (?, ?, ?)",
        [(broadcast_id, t, c) for t, c in found])
    conn.commit()
    conn.close()
    return len(found)


def reject(broadcast_id: int) -> bool:
    conn = get_connection()
    done = conn.execute("UPDATE broadcasts SET status = 'REJECTED', decided_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND status = 'DRAFT'", (broadcast_id,)).rowcount
    conn.commit()
    conn.close()
    return bool(done)


def telegram_text(b) -> str:
    link = f"\n\nZvanična objava: {b['source_url']}" if b["source_url"] else ""
    return f"{b['body']}{link}"


def claim(channel: str) -> list:
    """This channel's approved sends that are due, moved QUEUED -> SENDING in one statement (the
    same claim as message_queue's, so two workers can't send one twice), joined to their target
    and broadcast. A send stuck in SENDING for 5 minutes was orphaned by a dead worker and is
    claimed again."""
    conn = get_connection()
    ids = [r["id"] for r in conn.execute(
        "UPDATE broadcast_sends SET status = 'SENDING', claimed_at = CURRENT_TIMESTAMP "
        "WHERE id IN (SELECT s.id FROM broadcast_sends s JOIN broadcasts b ON b.id = s.broadcast_id "
        "  JOIN targets t ON t.id = s.target_id "
        "  WHERE s.channel = ? AND b.status = 'APPROVED' AND t.is_active = 1 "
        "    AND (b.send_after IS NULL OR b.send_after <= datetime('now')) "
        "    AND (s.status = 'QUEUED' OR (s.status = 'SENDING' "
        "         AND s.claimed_at <= datetime('now', '-5 minutes')))) "
        "RETURNING id", (channel,)).fetchall()]
    conn.commit()
    if not ids:
        conn.close()
        return []
    rows = conn.execute(
        f"SELECT s.id AS send_id, s.target_id, b.id AS broadcast_id, b.city, b.body, b.source_url, "
        f"t.telegram_chat_id, t.email, t.unsub_token FROM broadcast_sends s "
        f"JOIN broadcasts b ON b.id = s.broadcast_id JOIN targets t ON t.id = s.target_id "
        f"WHERE s.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    conn.close()
    return rows


def finish(send_id: int, ok: bool) -> None:
    """SENT, or FAILED - a city-wide notice is not retried: a late "grejanje počinje" helps no
    one, and the operator can send it again."""
    conn = get_connection()
    conn.execute("UPDATE broadcast_sends SET status = ?, sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                 ("SENT" if ok else "FAILED", send_id))
    conn.commit()
    conn.close()


def send_telegram() -> int:
    """Called by workers/telegram_worker after its own queue. Returns how many went out."""
    sent = 0
    for r in claim("telegram"):
        ok = False
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": r["telegram_chat_id"],
                      "text": f"📣 Važno obaveštenje\n\n{telegram_text(r)}\n\n— {TELEGRAM_FOOTER}",
                      "disable_web_page_preview": True}, timeout=10)
            ok = bool(res.ok and res.json().get("ok"))
        except Exception as e:
            print(f"⚠️ broadcast #{r['broadcast_id']} to target {r['target_id']} failed: {e}")
        finish(r["send_id"], ok)
        sent += ok
    return sent


def set_opt_out(*, chat_id=None, email=None, off: bool = True) -> int:
    """Turn these notices off (or back on) for every address of one Telegram chat or one email
    address. Outage alerts are untouched. Returns how many addresses changed."""
    conn = get_connection()
    if chat_id is not None:
        n = conn.execute("UPDATE targets SET broadcasts_off = ? WHERE telegram_chat_id = ?",
                         (int(off), str(chat_id))).rowcount
    elif email:
        n = conn.execute("UPDATE targets SET broadcasts_off = ? WHERE lower(email) = lower(?)",
                         (int(off), email)).rowcount
    else:
        n = 0
    conn.commit()
    conn.close()
    return n


# ---- from a scraped notice ----------------------------------------------------------------------

# "почев од 05.00 часова", "od 6h": a clock time the notice gives - never a date ("12.10.2026").
_TIME = re.compile(r"\b([01]?\d|2[0-3])(?:[.:]([0-5]\d))?\s*(?:h\b|час|čas|cas)", re.IGNORECASE)


def heating_start_text(city: str, outage_date: str | None, raw_text: str, season: bool) -> str:
    """The draft for a heating-start notice. Plain and short; the operator edits it if needed."""
    when = (outage_date or "").strip().rstrip(".")
    m = _TIME.search(raw_text or "")
    at = f" od {int(m[1]):02d}:{m[2] or '00'}" if m else ""
    what = "počinje grejna sezona" if season else "počinju tople probe grejanja"
    day = f", {when}.{at}" if when and when.upper() != "N/A" else ""
    return (f"🔥 {city_label(city)}: {what}{day}\n"
            f"Radijatori bi trebalo da se zagreju tokom dana. Ako grejanje izostane ili nešto "
            f"curi, javite se toplani.")
