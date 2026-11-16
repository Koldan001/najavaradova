"""A second look before a risky alert goes out - automatic, with the operator only as a fallback.

Two kinds of alert are held (message_queue status HELD) instead of queued straight away:
- area: someone matched through a whole settlement, not through their own street being named.
  #4865 (15.10.2026) would have told all of Petrovaradin about "Блок 8".
- date: the date the model read is nowhere in the notice's own text.

An area hold is checked automatically right after the outage is stored: our own rule
(core/matchmaker._settlement_part) and a second, independent model call
(parser.check_place_scope) each say WHOLE / PART for every place. When they agree the messages go
out (AGREED) and nobody is asked. When they don't, or the second call fails, or it is a date hold,
the operator gets one Telegram message - the place, both answers, the official notice's link and
its text - with ✅ / ❌. Nothing is sent until then: an unanswered hold is simply never sent, and
streets named in the same notice are not held at all.

    send_checks(outage_id UNIQUE, reason area|date, status PENDING|HELD|AGREED|APPROVED|REJECTED)
"""
import json
import re

import requests

import config
from core.database import get_connection

CALLBACK = re.compile(r"^sc:(ok|no):(\d+)$")


def _operator_post(method: str, payload: dict):
    if not (config.TELEGRAM_BOT_TOKEN and config.OPERATOR_CHAT_ID):
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}",
                          json={"chat_id": config.OPERATOR_CHAT_ID, **payload}, timeout=15)
        return r.json() if r.ok else None
    except Exception as e:
        print(f"⚠️ send check: Telegram {method} failed: {e}")
        return None


# The one way a module outside the bot messages the operator (core/feedback.py, core/review.py).
operator_post = _operator_post


def _area_places(parsed_json: str) -> list[tuple[str, str]]:
    """(settlement, what our rule says: WHOLE or PART) for every whole-settlement location that
    can alert people. Spots never alert, so they are not asked about."""
    from core.matchmaker import _settlement_part
    from parser import OutageAlert
    try:
        alert = OutageAlert.model_validate_json(parsed_json or "{}")
    except Exception:
        return []
    out = {}
    for loc in alert.affected_locations:
        if loc.location_type != "ENTIRE_SETTLEMENT" or not (loc.settlement or "").strip():
            continue
        kind, _ = _settlement_part(loc)
        if kind == "spot":
            continue
        out[loc.settlement] = "PART" if kind == "part" else "WHOLE"
    return list(out.items())


def _verdict(rule: list[tuple[str, str]], model: dict | None) -> bool:
    """True when the second opinion says the same as our rule for every place."""
    if model is None or not rule:
        return False
    norm = {re.sub(r"\s+", " ", k).strip().upper(): v for k, v in model.items()}
    return all(norm.get(place.strip().upper()) == ours for place, ours in rule)


def _release(conn, outage_id: int, status: str) -> int:
    """Settle a hold: HELD messages go out (QUEUED) or are dropped (SKIPPED)."""
    if status in ("AGREED", "APPROVED"):
        n = conn.execute("UPDATE message_queue SET status = 'QUEUED' WHERE outage_id = ? "
                         "AND status = 'HELD'", (outage_id,)).rowcount
    else:
        n = conn.execute("UPDATE message_queue SET status = 'SKIPPED' WHERE outage_id = ? "
                         "AND status = 'HELD'", (outage_id,)).rowcount
    conn.execute("UPDATE send_checks SET status = ?, decided_at = CURRENT_TIMESTAMP "
                 "WHERE outage_id = ?", (status, outage_id))
    return n


def check(outage_id: int) -> str | None:
    """Run the check for one outage that has HELD messages. Returns the status it settled on."""
    conn = get_connection()
    try:
        held = conn.execute("SELECT COUNT(DISTINCT target_id) FROM message_queue "
                            "WHERE outage_id = ? AND status = 'HELD'", (outage_id,)).fetchone()[0]
        row = conn.execute("SELECT * FROM send_checks WHERE outage_id = ?", (outage_id,)).fetchone()
        outage = conn.execute("SELECT id, region, outage_date, time_window, raw_text, parsed_json, "
                              "source_url FROM outages WHERE id = ?", (outage_id,)).fetchone()
    finally:
        conn.close()
    if not held or not outage:
        return row["status"] if row else None
    if row and row["status"] in ("AGREED", "APPROVED", "REJECTED"):
        # A decision already made covers messages queued after it (a later signup's backfill).
        conn = get_connection()
        _release(conn, outage_id, row["status"])
        conn.commit()
        conn.close()
        return row["status"]
    if row and row["status"] == "HELD":
        return "HELD"                                     # the operator was already asked

    reason = row["reason"] if row else "area"
    rule = _area_places(outage["parsed_json"])
    model = None
    if reason == "area":
        from parser import check_place_scope
        try:
            model = check_place_scope(outage["raw_text"] or "", [p for p, _ in rule])
        except Exception as e:
            print(f"⚠️ send check #{outage_id}: second opinion failed: {e}")
    agreed = reason == "area" and _verdict(rule, model)
    status = "AGREED" if agreed else "HELD"
    detail = json.dumps({"rule": dict(rule), "model": model}, ensure_ascii=False)

    conn = get_connection()
    try:
        conn.execute("INSERT INTO send_checks (outage_id, reason, status, detail) VALUES (?, ?, 'PENDING', ?) "
                     "ON CONFLICT(outage_id) DO UPDATE SET detail = excluded.detail",
                     (outage_id, reason, detail))
        if agreed:
            n = _release(conn, outage_id, "AGREED")
            print(f"✅ send check #{outage_id}: our rule and the second opinion agree - {n} message(s) out")
        else:
            conn.execute("UPDATE send_checks SET status = 'HELD' WHERE outage_id = ?", (outage_id,))
        conn.commit()
    finally:
        conn.close()
    if not agreed:
        ask_operator(outage, reason, rule, model, held)
    return status


def _scope_word(v) -> str:
    return {"WHOLE": "celo", "PART": "deo", "SPOT": "određeno mesto", "UNCLEAR": "nejasno"}.get(v, "nema odgovora")


def ask_operator(outage, reason: str, rule, model, held: int) -> None:
    lines = [f"🛑 Zadržano: #{outage['id']} · {outage['region']} · {outage['outage_date']} "
             f"{outage['time_window'] or ''}".strip(),
             f"Čeka poruku: {held}"]
    if reason == "date":
        lines.append("Razlog: datum koji je AI pročitao ne piše u objavi.")
    else:
        norm = {re.sub(r"\s+", " ", k).strip().upper(): v for k, v in (model or {}).items()}
        for place, ours in rule:
            lines.append(f"{place}: pravilo kaže {_scope_word(ours)}, druga provera kaže "
                         f"{_scope_word(norm.get(place.strip().upper())) if model is not None else 'nije odgovorila'}")
    if outage["source_url"]:
        lines.append(f"Objava: {outage['source_url']}")
    lines.append("")
    lines.append(" ".join((outage["raw_text"] or "").split())[:1200])
    lines.append("")
    lines.append("Bez odgovora se ništa ne šalje. Ulice imenovane u objavi su već poslate.")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Pošalji", "callback_data": f"sc:ok:{outage['id']}"},
        {"text": "❌ Ne šalji", "callback_data": f"sc:no:{outage['id']}"},
    ]]}
    _operator_post("sendMessage", {"text": "\n".join(lines)[:4000], "reply_markup": keyboard,
                                   "disable_web_page_preview": False})


def decide(outage_id: int, approve: bool) -> int | None:
    """The operator's ✅ / ❌. Number of messages released or dropped; None when already decided."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT status FROM send_checks WHERE outage_id = ?", (outage_id,)).fetchone()
        if not row or row["status"] not in ("PENDING", "HELD"):
            return None
        n = _release(conn, outage_id, "APPROVED" if approve else "REJECTED")
        conn.commit()
        return n
    finally:
        conn.close()


def run_pending() -> int:
    """Every outage with HELD messages not yet settled: the safety net for holds the scrape path
    didn't check (a later signup's backfill, a crash between store and check)."""
    conn = get_connection()
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT m.outage_id FROM message_queue m "
        "LEFT JOIN send_checks c ON c.outage_id = m.outage_id "
        "WHERE m.status = 'HELD' AND (c.status IS NULL OR c.status IN ('PENDING', 'AGREED', 'APPROVED', 'REJECTED'))")]
    conn.close()
    for outage_id in ids:
        try:
            check(outage_id)
        except Exception as e:
            print(f"⚠️ send check #{outage_id} failed: {e}")
    return len(ids)
