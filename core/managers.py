"""A building manager's list of addresses, from /upravnici (web/upravnici/index.html).

Flow: the manager pastes a list, the page checks every line (scripts/local_web_server.py
check_address_lines) and sends only the ones the manager confirmed. create_request() saves them
and the login link goes out; when the link is clicked, /auth/verify loads them onto the account
(the server's _load_manager_request) and record_load() notes which addresses that created.
notify_operator() then tells the operator on Telegram, with one ❌ that remove() undoes the load
with - deleting only the addresses this request created, never ones the account already had.
"""
import json
import re

import requests

import config
from core.database import get_connection

MAX_ADDRESSES = 200
CALLBACK = re.compile(r"^mg:rm:(\d+)$")


def create_request(email: str, name: str | None, channels: list, addresses: list) -> int:
    conn = get_connection()
    try:
        rid = conn.execute(
            "INSERT INTO manager_requests (email, name, channels, addresses_json) VALUES (?, ?, ?, ?)",
            (email, (name or "").strip()[:120] or None, ",".join(channels),
             json.dumps(addresses, ensure_ascii=False)),
        ).lastrowid
        conn.commit()
        return rid
    finally:
        conn.close()


def get_request(request_id: int):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM manager_requests WHERE id = ?", (request_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def claim_for_load(request_id: int, subscriber_id: int) -> dict | None:
    """The request, once: PENDING -> LOADING for this subscriber. A second click on the same
    link (or a replayed one) gets None and loads nothing twice."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE manager_requests SET status = 'LOADING', subscriber_id = ? "
            "WHERE id = ? AND status = 'PENDING'", (subscriber_id, request_id))
        conn.commit()
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM manager_requests WHERE id = ?", (request_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def record_load(request_id: int, new_target_ids: list) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE manager_requests SET status = 'LOADED', target_ids_json = ?, "
            "loaded_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps(new_target_ids), request_id))
        conn.commit()
    finally:
        conn.close()


def remove(request_id: int) -> tuple | None:
    """The operator's ❌ on a spam list: deletes the addresses this request created (as the
    dashboard's "Obriši" does), and the account's unused "connect Telegram for all" link. An
    address the account already had before the list stays. Returns (deleted, kept), or None when
    there is nothing left to delete. A list removed before deleting existed (it only paused its
    addresses) is finished off by running this again."""
    from core import auth
    conn = get_connection()
    try:
        row = conn.execute("SELECT status, addresses_json, target_ids_json, subscriber_id "
                           "FROM manager_requests WHERE id = ?", (request_id,)).fetchone()
        if not row or row["status"] not in ("LOADED", "REMOVED"):
            return None
        ids = json.loads(row["target_ids_json"] or "[]")
        deleted = sum(auth.delete_target(conn, row["subscriber_id"], tid) for tid in ids)
        if row["status"] == "REMOVED" and not deleted:
            return None
        # A spam list: the addresses the account already had go back to regular alerts.
        conn.execute("UPDATE targets SET manager_list = 0 WHERE subscriber_id = ?", (row["subscriber_id"],))
        conn.execute("DELETE FROM pending_registrations WHERE subscriber_id = ? AND attach_all = 1 "
                     "AND consumed_at IS NULL", (row["subscriber_id"],))
        conn.execute("UPDATE manager_requests SET status = 'REMOVED', removed_at = CURRENT_TIMESTAMP "
                     "WHERE id = ?", (request_id,))
        conn.commit()
        return deleted, max(0, len(json.loads(row["addresses_json"])) - len(ids))
    finally:
        conn.close()


def removal_answer(result) -> str:
    """The operator's answer after ❌."""
    if result is None:
        return "Već uklonjeno."
    deleted, kept = result
    return f"Obrisano: {deleted} novih adresa." + (
        f" {kept} je već bila na nalogu i ostaje." if kept == 1 else
        f" {kept} su već bile na nalogu i ostaju." if kept else "")


def operator_text(req: dict, loaded: int) -> str:
    """The operator's note. It carries the manager's email - the operator chat is the one place
    that already gets subscriber emails (/stats), and OPERATOR_CHAT_ID is a credential."""
    addresses = json.loads(req["addresses_json"])
    cities = sorted({a["city"] for a in addresses})
    new = len(json.loads(req.get("target_ids_json") or "[]"))
    split = f" ({new} novih, {loaded - new} već na nalogu)" if new != loaded else ""
    lines = [f"🏢 Upravnik #{req['id']}: {loaded} adresa uneto{split}",
             f"{req['name'] + ' · ' if req.get('name') else ''}{req['email']} · {req['channels']}",
             "Gradovi: " + ", ".join(cities[:6]) + (" …" if len(cities) > 6 else "")]
    lines += [f"• {a['ulica']} {a['broj']}, {a['city']}" for a in addresses[:5]]
    if len(addresses) > 5:
        lines.append(f"… i još {len(addresses) - 5}")
    return "\n".join(lines)


def notify_operator(request_id: int, loaded: int) -> None:
    """Best effort, like core/alerts.py: a failed note never undoes the load."""
    req = get_request(request_id)
    if not req or not (config.TELEGRAM_BOT_TOKEN and getattr(config, "OPERATOR_CHAT_ID", None)):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.OPERATOR_CHAT_ID, "text": operator_text(req, loaded)[:4000],
                  "disable_web_page_preview": True,
                  "reply_markup": {"inline_keyboard": [[
                      {"text": "❌ Ukloni (spam)", "callback_data": f"mg:rm:{request_id}"}]]}},
            timeout=15)
    except Exception as e:
        print(f"⚠️ manager request #{request_id}: operator note failed: {e}")
