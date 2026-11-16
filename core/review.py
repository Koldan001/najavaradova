"""The operator's weekly review: about 10-15 minutes on Monday, in Telegram, and the ground truth
every accuracy figure in /stats is measured against.

Why: a second model agreeing (core/alert_check.py) is not the same as being right - both can be
wrong the same way. Only a person checking against the notice knows. A small RANDOM sample every
week gives an honest accuracy figure with its error margin; the cases the system itself flags are
added on top, and kept apart in the figures so they don't skew it.

Every Monday from 09:00 (Belgrade; any later day of that week if the server was down) the
pipeline sends the operator, one message each with ✅ / ❌:
- RANDOM_ALERTS alerts sent last week, picked at random: "was this alert right for this
  address?" - this is the precision figure (of the alerts we send, how many are right);
- every alert the second check DISAGREED with: how often a DISAGREE is really a wrong alert, and
  how often an AGREE is still wrong - how far the second check can be trusted;
- every alert a reader marked 👎 (core/feedback.py);
- NOTICES notices picked at random, with every place our reading names (✅ / ❌ each), and a reply
  for whatever it left out - this is recall (of the places a notice names, how many we found),
  which no alert can show: a missed street sends nothing.
/pregled sends one right away (items not reviewed before).

    review_items: see core/database.py 17l.     review_batches(batch, sent_at)
"""
import json
import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from core.database import get_connection

TZ = ZoneInfo("Europe/Belgrade")
RANDOM_ALERTS = 10
MAX_FLAGGED = 10            # DISAGREEs, and 👎 answers, each
NOTICES = 2
CALLBACK = re.compile(r"^rv:(ok|no|none):(\d+)$")

_PICKED = {"random": "nasumično", "disagree": "druga provera se ne slaže", "feedback": "čitalac kaže 👎"}
_KIND = {"POWER": "struja", "WATER": "voda", "HEATING": "grejanje"}


# ---------- when ----------

def week_id(now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    return now.strftime("%G-W%V")


def due(now: datetime | None = None) -> bool:
    """From Monday 09:00 on, once a week."""
    now = now or datetime.now(TZ)
    if now.weekday() == 0 and now.hour < 9:
        return False
    conn = get_connection()
    try:
        return conn.execute("SELECT 1 FROM review_batches WHERE batch = ?", (week_id(now),)).fetchone() is None
    finally:
        conn.close()


def run_due(now: datetime | None = None) -> int:
    """Called every pipeline cycle (main.py). Items sent, or 0 when it isn't time."""
    if not due(now):
        return 0
    return send_batch(week_id(now))


# ---------- what ----------

def _not_reviewed() -> str:
    return ("NOT EXISTS (SELECT 1 FROM review_items r WHERE r.kind = 'alert' "
            "AND r.outage_id = x.outage_id AND r.target_id = x.target_id)")


def pick(conn) -> tuple[list[dict], list[int]]:
    """(alert items, notice ids) for one review, nothing written."""
    alerts, seen = [], set()

    def add(rows, picked):
        for r in rows:
            key = (r["outage_id"], r["target_id"])
            if key not in seen:
                seen.add(key)
                alerts.append({"outage_id": key[0], "target_id": key[1], "picked": picked})

    add(conn.execute(f"""
        SELECT x.outage_id, x.target_id FROM message_queue x
        WHERE x.status = 'SENT' AND x.sent_at >= datetime('now', '-7 days') AND {_not_reviewed()}
        GROUP BY x.outage_id, x.target_id ORDER BY RANDOM() LIMIT ?""", (RANDOM_ALERTS,)), "random")
    add(conn.execute(f"""
        SELECT x.outage_id, x.target_id FROM alert_checks x
        WHERE x.verdict = 'DISAGREE' AND x.created_at >= datetime('now', '-7 days') AND {_not_reviewed()}
        ORDER BY x.outage_id DESC LIMIT ?""", (MAX_FLAGGED,)), "disagree")
    add(conn.execute(f"""
        SELECT x.outage_id, x.target_id FROM user_feedback x
        WHERE x.verdict = 'WRONG' AND x.updated_at >= datetime('now', '-7 days') AND {_not_reviewed()}
        ORDER BY x.outage_id DESC LIMIT ?""", (MAX_FLAGGED,)), "feedback")
    for a in alerts:
        row = conn.execute("SELECT verdict FROM alert_checks WHERE outage_id = ? AND target_id = ?",
                           (a["outage_id"], a["target_id"])).fetchone()
        a["shadow"] = row["verdict"] if row else None

    notices = [r["id"] for r in conn.execute("""
        SELECT o.id FROM outages o
        WHERE o.created_at >= datetime('now', '-7 days') AND o.parsed_json IS NOT NULL
          AND IFNULL(o.status, '') NOT IN ('CANCELLATION', 'EXPIRED')
          AND NOT EXISTS (SELECT 1 FROM review_items r WHERE r.kind = 'missing' AND r.outage_id = o.id)
        ORDER BY RANDOM() LIMIT ?""", (NOTICES,))]
    return alerts, notices


def places(parsed_json: str) -> list[str]:
    """Every place our reading of a notice names, as the operator reads it (Latin)."""
    from spatial.spatial_engine import cyrillic_to_latin
    try:
        data = json.loads(parsed_json or "{}")
    except ValueError:
        return []
    out = []
    for loc in data.get("affected_locations") or []:
        lat = lambda v: cyrillic_to_latin(v).title() if v else ""
        kind = loc.get("location_type")
        if kind == "ENTIRE_SETTLEMENT":
            scope = {"PART": " (deo)", "SPOT": " (određeno mesto)"}.get(loc.get("scope") or "", "")
            text = f"{lat(loc.get('settlement'))}{scope}" + (f" - {lat(loc.get('landmark'))}" if loc.get("landmark") else "")
        elif kind == "LANDMARK" and not loc.get("main_street"):
            text = lat(loc.get("landmark")) or "?"
        else:
            text = lat(loc.get("main_street")) or lat(loc.get("landmark")) or "?"
            if loc.get("number_range"):
                text += f" {loc['number_range']}"
            if loc.get("explicit_numbers"):
                text += f" br. {loc['explicit_numbers']}"
            if loc.get("parity") in ("PARNA", "NEPARNA"):
                text += " (parni)" if loc["parity"] == "PARNA" else " (neparni)"
            if loc.get("intersection_start") or loc.get("intersection_end"):
                text += f" od {lat(loc.get('intersection_start')) or '?'} do {lat(loc.get('intersection_end')) or '?'}"
            if loc.get("settlement"):
                text += f", {lat(loc['settlement'])}"
        if loc.get("includes_surrounding_streets"):
            text += " + okolne ulice"
        out.append(text)
    return list(dict.fromkeys(out))


# ---------- sending ----------

def _choice(label: str, chosen: bool) -> str:
    return f"• {label} •" if chosen else label


def keyboard(items) -> dict:
    """Buttons for one message's items, the operator's answer marked - tapping again changes it."""
    rows = []
    for i, it in enumerate(items):
        if it["kind"] == "alert":
            rows.append([{"text": _choice("✅ Tačno", it["verdict"] == "RIGHT"), "callback_data": f"rv:ok:{it['id']}"},
                         {"text": _choice("❌ Pogrešno", it["verdict"] == "WRONG"), "callback_data": f"rv:no:{it['id']}"}])
        elif it["kind"] == "street":
            n = sum(1 for x in items[:i + 1] if x["kind"] == "street")
            rows.append([{"text": _choice(f"{n} ✅", it["verdict"] == "RIGHT"), "callback_data": f"rv:ok:{it['id']}"},
                         {"text": _choice(f"{n} ❌", it["verdict"] == "WRONG"), "callback_data": f"rv:no:{it['id']}"}])
        elif it["kind"] == "missing":
            label = ("• Ništa ne fali •" if it["verdict"] == "DONE" and not it["n"]
                     else f"Fali: {it['n']} (iz odgovora)" if it["verdict"] == "DONE" else "Ništa ne fali")
            rows.append([{"text": label, "callback_data": f"rv:none:{it['id']}"}])
    return {"inline_keyboard": rows}


def _head(o) -> str:
    kind = _KIND.get((o["category"] or "").upper(), (o["category"] or "").lower())
    return f"#{o['id']} · {kind} · {o['outage_date'] or ''} {o['time_window'] or ''}".strip()


TEXT_MAX = 1500


def _folded(text: str) -> tuple[str, list[int]]:
    """Latin, upper case, no diacritics - and for every character of that, its index in `text`,
    so a match found in the folded text can be cut out of the original."""
    import unicodedata
    from spatial.spatial_engine import cyrillic_to_latin
    out, where = [], []
    for i, c in enumerate(text):
        for d in unicodedata.normalize("NFKD", cyrillic_to_latin(c).replace("Đ", "DJ")):
            if not unicodedata.combining(d):
                out.append(d)
                where.append(i)
    return "".join(out), where


def notice_excerpt(raw_text: str, street: str | None) -> str:
    """The notice for a review message. A long one (EPS lists 40+ streets) used to be cut at
    1,500 characters, so the reviewed street could fall in the part never shown and a correct
    alert looked wrong (#6758, Kralja Uroša Prvog). Now: the opening, then the part around the
    street, marked »like this«."""
    text = " ".join((raw_text or "").split())
    if len(text) <= TEXT_MAX:
        return text
    folded, where = _folded(text)
    key = _folded(street or "")[0].strip()
    at = folded.find(key) if key else -1
    if at < 0 and key:                                  # e.g. "Kralja Uroša Prvog" vs "Краља Уроша I"
        # the rarest word of the name that does appear: "UROSA", not the "KRALJA" of every king
        words = [w for w in key.split() if len(w) >= 4 and w in folded]
        if words:
            at = folded.find(min(words, key=folded.count))
            key = min(words, key=folded.count)
    if at < 0:
        return text[:TEXT_MAX] + " …"
    start, end = where[at], where[min(at + len(key), len(where)) - 1] + 1
    marked = f"{text[:start]}»{text[start:end]}«{text[end:]}"
    if end < TEXT_MAX - 200:
        return marked[:TEXT_MAX] + " …"
    lo = marked.rfind(" ", 0, max(0, start - 250)) + 1
    return f"{marked[:500].rsplit(' ', 1)[0]} … {marked[lo:end + 600]} …"


def _alert_text(n, total, item, o, t, message) -> str:
    lines = [f"🔎 {n}/{total} · {_PICKED.get(item['picked'], item['picked'])}", _head(o),
             f"Adresa: {t['ulica']} {t['broj'] or ''}, {t['city']}".replace(" ,", ",") if t else "Adresa: (obrisana)",
             f"Poslato: {(message or '').strip()[:500]}"]
    if o["source_url"]:
        lines.append(f"Objava: {o['source_url']}")
    lines += ["", notice_excerpt(o["raw_text"], t["ulica"] if t else None), "",
              "Da li je ovo obaveštenje tačno za ovu adresu (ulica, broj, dan)?"]
    return "\n".join(lines)


def _notice_text(n, total, o, names) -> str:
    lines = [f"📄 {n}/{total} · objava - da li smo sve našli?", _head(o)]
    if o["source_url"]:
        lines.append(f"Objava: {o['source_url']}")
    lines += ["", " ".join((o["raw_text"] or "").split())[:2200], "", "Mi smo pročitali:"]
    lines += [f"{i}. {p}" for i, p in enumerate(names, 1)] or ["(ništa)"]
    lines += ["", "✅ / ❌ za svako mesto. Ako nešto fali: odgovorite (Reply) na ovu poruku, "
                  "jedno mesto po redu. Ako ne fali ništa: dugme ispod."]
    return "\n".join(lines)


def _send(text: str, items: list[dict]) -> int | None:
    from core.send_check import operator_post
    res = operator_post("sendMessage", {"text": text[:4000], "reply_markup": keyboard(items),
                                        "disable_web_page_preview": True})
    time.sleep(1.1)                       # Telegram allows about one message a second per chat
    return ((res or {}).get("result") or {}).get("message_id")


def send_batch(batch: str) -> int:
    """Pick, store and send one review. Returns the number of items sent."""
    from core.send_check import operator_post
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM review_batches WHERE batch = ?", (batch,)).fetchone():
            return 0
        conn.execute("INSERT INTO review_batches (batch) VALUES (?)", (batch,))
        alerts, notice_ids = pick(conn)
        messages = []                     # (text, [item ids])
        total = len(alerts) + len(notice_ids)
        for n, a in enumerate(alerts, 1):
            o = conn.execute("SELECT * FROM outages WHERE id = ?", (a["outage_id"],)).fetchone()
            t = conn.execute("SELECT ulica, broj, city FROM targets WHERE id = ?", (a["target_id"],)).fetchone()
            m = conn.execute("SELECT custom_message FROM message_queue WHERE outage_id = ? AND target_id = ? "
                             "ORDER BY id DESC LIMIT 1", (a["outage_id"], a["target_id"])).fetchone()
            item_id = conn.execute(
                "INSERT INTO review_items (batch, kind, picked, outage_id, target_id, shadow) "
                "VALUES (?, 'alert', ?, ?, ?, ?)",
                (batch, a["picked"], a["outage_id"], a["target_id"], a["shadow"])).lastrowid
            messages.append((_alert_text(n, total, a, o, t, m["custom_message"] if m else ""), [item_id]))
        for n, oid in enumerate(notice_ids, len(alerts) + 1):
            o = conn.execute("SELECT * FROM outages WHERE id = ?", (oid,)).fetchone()
            names = places(o["parsed_json"])
            ids = [conn.execute("INSERT INTO review_items (batch, kind, picked, outage_id, place) "
                                "VALUES (?, 'street', 'notice', ?, ?)", (batch, oid, p)).lastrowid for p in names]
            ids.append(conn.execute("INSERT INTO review_items (batch, kind, picked, outage_id) "
                                    "VALUES (?, 'missing', 'notice', ?)", (batch, oid)).lastrowid)
            messages.append((_notice_text(n, total, o, names), ids))
        conn.commit()
    finally:
        conn.close()

    if not messages:
        operator_post("sendMessage", {"text": f"📋 Nedeljna provera {batch}: ove nedelje nema ničega za proveru."})
        return 0
    operator_post("sendMessage", {"text": (
        f"📋 Nedeljna provera {batch}: {total} stavki, oko 10 minuta.\n"
        f"Za svako obaveštenje: da li je tačno za tu adresu? Za objave: da li smo našli sva mesta?\n"
        f"Rezultat: /stats")})
    for text, ids in messages:
        message_id = _send(text, _items(ids))
        if message_id:
            conn = get_connection()
            conn.executemany("UPDATE review_items SET message_id = ? WHERE id = ?", [(message_id, i) for i in ids])
            conn.commit()
            conn.close()
    return total


def _items(ids) -> list:
    conn = get_connection()
    try:
        rows = conn.execute(f"SELECT * FROM review_items WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
                            list(ids)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- answers (workers/telegram_bot.py) ----------

def decide(item_id: int, action: str) -> tuple[str, dict | None]:
    """The operator's tap. (short answer, the message's new keyboard or None)."""
    conn = get_connection()
    try:
        it = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
        if not it:
            return "Nepoznata stavka.", None
        if action == "none" and it["kind"] == "missing":
            conn.execute("UPDATE review_items SET verdict = 'DONE', n = 0, note = NULL, "
                         "labelled_at = CURRENT_TIMESTAMP WHERE id = ?", (item_id,))
            answer = "Zabeleženo: ništa ne fali."
        elif action in ("ok", "no") and it["kind"] in ("alert", "street"):
            conn.execute("UPDATE review_items SET verdict = ?, labelled_at = CURRENT_TIMESTAMP WHERE id = ?",
                         ("RIGHT" if action == "ok" else "WRONG", item_id))
            answer = "Zabeleženo: tačno." if action == "ok" else "Zabeleženo: pogrešno."
        else:
            return "", None
        conn.commit()
        siblings = [r["id"] for r in conn.execute("SELECT id FROM review_items WHERE message_id = ?",
                                                  (it["message_id"],))] if it["message_id"] else [item_id]
    finally:
        conn.close()
    return answer, keyboard(_items(siblings))


def missing_reply(message_id: int, text: str) -> tuple[int, dict | None] | None:
    """The operator's typed reply to a notice: the places our reading left out, one per line.
    (how many, the message's new keyboard), or None when the reply isn't to a review notice."""
    names = [l.strip(" -•\t") for l in (text or "").splitlines() if l.strip(" -•\t")]
    conn = get_connection()
    try:
        it = conn.execute("SELECT id FROM review_items WHERE message_id = ? AND kind = 'missing'",
                          (message_id,)).fetchone()
        if not it:
            return None
        conn.execute("UPDATE review_items SET verdict = 'DONE', n = ?, note = ?, labelled_at = CURRENT_TIMESTAMP "
                     "WHERE id = ?", (len(names), "\n".join(names), it["id"]))
        conn.commit()
        siblings = [r["id"] for r in conn.execute("SELECT id FROM review_items WHERE message_id = ?", (message_id,))]
    finally:
        conn.close()
    return len(names), keyboard(_items(siblings))


# ---------- the figures (core/admin_stats.py) ----------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% range for a share k/n from a small sample - 9 of 10 right is somewhere around 60-98%,
    not "90%"."""
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def quality(days: int = 90) -> dict:
    window = f"-{int(days)} days"
    conn = get_connection()
    try:
        def count(sql, *args):
            return conn.execute(sql, (window, *args)).fetchone()[0] or 0

        q = {"days": days}
        q["random_right"] = count("SELECT COUNT(*) FROM review_items WHERE kind = 'alert' AND picked = 'random' "
                                  "AND verdict = 'RIGHT' AND labelled_at >= datetime('now', ?)")
        q["random_wrong"] = count("SELECT COUNT(*) FROM review_items WHERE kind = 'alert' AND picked = 'random' "
                                  "AND verdict = 'WRONG' AND labelled_at >= datetime('now', ?)")
        for shadow in ("AGREE", "DISAGREE"):
            for verdict in ("RIGHT", "WRONG"):
                q[f"{shadow}_{verdict}"] = count(
                    "SELECT COUNT(*) FROM review_items WHERE kind = 'alert' "
                    "AND labelled_at >= datetime('now', ?) AND shadow = ? AND verdict = ?", shadow, verdict)
        q["fb_right"] = count("SELECT COUNT(*) FROM user_feedback WHERE verdict = 'RIGHT' AND updated_at >= datetime('now', ?)")
        q["fb_wrong"] = count("SELECT COUNT(*) FROM user_feedback WHERE verdict = 'WRONG' AND updated_at >= datetime('now', ?)")
        q["fb_confirmed"] = count("SELECT COUNT(*) FROM review_items WHERE kind = 'alert' AND picked = 'feedback' "
                                  "AND verdict = 'WRONG' AND labelled_at >= datetime('now', ?)")
        q["fb_reviewed"] = count("SELECT COUNT(*) FROM review_items WHERE kind = 'alert' AND picked = 'feedback' "
                                 "AND verdict IS NOT NULL AND labelled_at >= datetime('now', ?)")
        q["street_right"] = count("SELECT COUNT(*) FROM review_items WHERE kind = 'street' AND verdict = 'RIGHT' "
                                  "AND labelled_at >= datetime('now', ?)")
        q["street_wrong"] = count("SELECT COUNT(*) FROM review_items WHERE kind = 'street' AND verdict = 'WRONG' "
                                  "AND labelled_at >= datetime('now', ?)")
        q["missing"] = count("SELECT SUM(n) FROM review_items WHERE kind = 'missing' AND verdict = 'DONE' "
                             "AND labelled_at >= datetime('now', ?)")
        q["notices"] = count("SELECT COUNT(*) FROM review_items WHERE kind = 'missing' AND verdict = 'DONE' "
                             "AND labelled_at >= datetime('now', ?)")
        q["open"] = conn.execute("SELECT COUNT(*) FROM review_items WHERE verdict IS NULL AND kind != 'missing'"
                                 ).fetchone()[0] or 0
        return q
    finally:
        conn.close()


def _pct(k, n) -> str:
    if not n:
        return "-"
    lo, hi = wilson(k, n)
    return f"{100 * k // n}% ({k}/{n}, verovatno {round(100 * lo)}-{round(100 * hi)}%)"


def format_quality(q: dict) -> list[str]:
    """Lines for /stats: what a person checked, not what a model thinks."""
    lines = [f"Tačnost (ljudska provera, {q['days']} dana):"]
    lines.append(f"  obaveštenja tačna (nasumični uzorak): {_pct(q['random_right'], q['random_right'] + q['random_wrong'])}")
    found = q["street_right"]
    lines.append(f"  mesta iz objave pronađena: {_pct(found, found + q['missing'])} · "
                 f"pročitana tačno: {_pct(found, found + q['street_wrong'])} · objava: {q['notices']}")
    dis = q["DISAGREE_RIGHT"] + q["DISAGREE_WRONG"]
    agr = q["AGREE_RIGHT"] + q["AGREE_WRONG"]
    lines.append(f"  druga provera 'ne slaže se' zaista greška: {_pct(q['DISAGREE_WRONG'], dis)}")
    lines.append(f"  druga provera 'slaže se' a ipak greška: {_pct(q['AGREE_WRONG'], agr)}")
    lines.append(f"  čitaoci: 👍 {q['fb_right']} · 👎 {q['fb_wrong']}"
                 + (f" (potvrđeno pogrešno {q['fb_confirmed']}/{q['fb_reviewed']})" if q["fb_reviewed"] else ""))
    if q["open"]:
        lines.append(f"  bez odgovora u proveri: {q['open']}")
    return lines
