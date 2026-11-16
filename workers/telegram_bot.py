"""Real registration bot - consumes the deep-link code a web form hands out (see
pending_registrations in core/database.py) and turns it into a real targets row.

Not the web form itself - that's a separate piece, not built yet. This is the half that proves
and later runs the "tap Start once, done" flow: a pending row already exists (for now, made by
scripts/create_test_pending.py while there's no web form to make one), the user opens
https://t.me/<bot>?start=<code>, taps Start, Telegram sends "/start <code>" automatically (no
typing), this bot reads that, matches the pending row, writes targets, done.

Usage: python -m workers.telegram_bot
"""
import sys
import os
import time
import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from core.database import get_connection

API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


PENDING_CODE_LIFETIME_HOURS = 24


def find_pending(code):
    """A code older than PENDING_CODE_LIFETIME_HOURS is treated as gone, same as never having
    existed - without this, a leaked or old deep-link (screenshot, forwarded message, browser
    history) stays valid forever. subscriber_id is included but deliberately NOT filtered on
    here - a code can exist before its email is confirmed (see scripts/local_web_server.py's
    /auth/start), and handle_start needs to tell those two "not found" cases apart to give a
    useful message instead of a flat "invalid link" for someone who just hasn't checked their
    email yet."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, city, ulica, broj, subscriber_id, attach_target_id, attach_all FROM pending_registrations
        WHERE code = ? AND consumed_at IS NULL
          AND created_at >= datetime('now', ?)
    ''', (code, f'-{PENDING_CODE_LIFETIME_HOURS} hours'))
    row = cursor.fetchone()
    conn.close()
    return row


def complete_registration(pending_id, chat_id, username, city, ulica, broj, subscriber_id):
    """Writes the real target row and marks the pending row consumed, as one transaction - a
    crash between the two would otherwise let the same code be replayed. After it commits, maps
    the new target onto the H3 grid and backfills it against outages already scraped and ACTIVE,
    so a signup for a known upcoming outage is notified now instead of only on some future
    re-scrape."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO targets (telegram_chat_id, telegram_username, city, ulica, broj, subscriber_id)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (str(chat_id), username, city, ulica, broj, subscriber_id))
        new_target_id = cursor.lastrowid
        cursor.execute('''
            UPDATE pending_registrations SET consumed_at = CURRENT_TIMESTAMP WHERE id = ?
        ''', (pending_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        from core.matchmaker import backfill_target_against_active_outages
        backfill_target_against_active_outages(new_target_id)
    except Exception as e:
        # Registration itself already succeeded and committed - a backfill failure must not
        # undo it. main.py's map_unmapped_targets() pass picks the target up next cycle anyway;
        # only the "notified immediately" part is lost, not the subscription.
        print(f"⚠️ backfill for target #{new_target_id} failed (registration still OK): {e}")


def complete_channel_attach(pending_id, target_id, chat_id, username, subscriber_id):
    """The "add Telegram to an address that already has email" twin of complete_registration:
    UPDATEs the existing target row instead of INSERTing a new one, so the two channels end up on
    one address instead of a duplicate. subscriber_id is checked again in the WHERE clause itself
    (not just trusted from the pending row) - belt and suspenders against ever attaching one
    account's Telegram onto another account's address. telegram_active=1 in the same UPDATE so a
    row that was previously fully deactivated-then-reused... can't happen (attach only ever runs
    against a target with telegram_chat_id IS NULL, checked by both the /api/targets/add-telegram
    endpoint and again here), but costs nothing to set explicitly rather than leave whatever
    default the column had.

    Runs a fresh backfill afterward, same as a brand-new registration - this address may already
    have active outages queued for its email channel, and the new Telegram channel deserves the
    same "notified now, not just on the next re-scrape" treatment a new signup gets."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE targets SET telegram_chat_id = ?, telegram_username = ?, telegram_active = 1
            WHERE id = ? AND subscriber_id = ? AND telegram_chat_id IS NULL
        ''', (str(chat_id), username, target_id, subscriber_id))
        if cursor.rowcount == 0:
            # Already attached (double-click, two tabs) or a subscriber_id mismatch that should
            # never happen by construction - either way, nothing to commit, and the caller
            # branches on this to give an accurate message instead of a false "success".
            conn.rollback()
            return False
        cursor.execute('''
            UPDATE pending_registrations SET consumed_at = CURRENT_TIMESTAMP WHERE id = ?
        ''', (pending_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        from core.matchmaker import backfill_target_against_active_outages
        backfill_target_against_active_outages(target_id)
    except Exception as e:
        print(f"⚠️ backfill for target #{target_id} failed (Telegram attach still OK): {e}")
    return True


def send_message(chat_id, text, parse_mode=None):
    """Returns True only if Telegram accepted the message - callers that format with HTML use
    that to fall back to plain text rather than silently sending nothing."""
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(f"{API}/sendMessage", json=payload, timeout=10)
        return bool(r.ok and r.json().get("ok"))
    except Exception as e:
        print(f"⚠️ sendMessage failed: {e}")
        return False


# ---------- operator-only ----------

def is_operator(chat_id) -> bool:
    """Exact match against config.OPERATOR_CHAT_ID, and fail-closed when it is unset. Never a
    prefix/substring test: chat ids are plain integers, so a `startswith` check would let
    12345678 in on an operator id of 1234. This is the ONLY gate on subscriber emails and home
    addresses leaving the system, so it stays this dumb on purpose."""
    want = str(getattr(config, "OPERATOR_CHAT_ID", "") or "").strip()
    return bool(want) and str(chat_id).strip() == want


def handle_stats(chat_id, arg=None):
    """/stats [dana] - the operator's overview. Caller MUST have checked is_operator() first;
    this never sends anywhere but the chat it was asked from, which is that same operator."""
    try:
        days = max(1, min(int(arg), 365)) if arg and arg.strip().isdigit() else 7
    except (TypeError, ValueError):
        days = 7
    try:
        from core.admin_stats import collect, format_summary
        stats = collect(days=days)
    except Exception as e:
        print(f"⚠️ /stats failed: {e}")
        send_message(chat_id, "Greška pri čitanju statistike - vidi logove `bot` kontejnera.")
        return

    def _capped(body):
        # Telegram hard-caps a message at 4096 chars. The summary is far shorter, but a big
        # install (many cities / recent rows) could grow past it - truncate rather than 400.
        if len(body) <= 3900:
            return body
        return body[:3900] + "\n… (skraćeno — `python -m scripts.admin_stats` na VM-u za sve)"

    # HTML gives the section headings their weight; if Telegram rejects it for any reason the
    # operator still gets the report, just flat, instead of nothing at all.
    if not send_message(chat_id, _capped(format_summary(stats, html=True)), parse_mode="HTML"):
        print("⚠️ /stats HTML send rejected - retrying as plain text")
        send_message(chat_id, _capped(format_summary(stats, html=False)))


def list_active_targets(chat_id):
    """Active target rows for one chat, oldest first - the stable order /odjava <broj> counts
    against, so the number a user sees in /lista is the number they pass back."""
    conn = get_connection()
    rows = conn.execute('''
        SELECT id, city, ulica, broj FROM targets
        WHERE telegram_chat_id = ? AND is_active = 1
        ORDER BY id
    ''', (str(chat_id),)).fetchall()
    conn.close()
    return rows


def _format_list(rows):
    return "\n".join(f"{i}. {r['ulica']} {r['broj']}, {r['city']}" for i, r in enumerate(rows, 1))


def handle_lista(chat_id):
    rows = list_active_targets(chat_id)
    if not rows:
        send_message(chat_id, "Nisi prijavljen ni na jednu adresu.\n\nPrijava: najavaradova.rs")
        return
    send_message(
        chat_id,
        f"📍 Tvoje prijavljene adrese ({len(rows)}):\n{_format_list(rows)}\n\n"
        f"Za odjavu jedne: /odjava <broj>\nZa odjavu svih: /odjava sve",
    )


def _deactivate(target_ids):
    """Soft-delete: is_active=0, not DELETE - keeps the row + its history, and matchmaker only
    matches WHERE is_active=1, so an opted-out address stops getting new outages next cycle."""
    if not target_ids:
        return 0
    conn = get_connection()
    q = ",".join("?" * len(target_ids))
    cur = conn.execute(f"UPDATE targets SET is_active = 0 WHERE id IN ({q})", target_ids)
    conn.commit()
    conn.close()
    return cur.rowcount


def handle_stop(chat_id, arg=None):
    """/odjava            - 0 addresses: say so. 1: stop it. >1: show the numbered list.
       /odjava <broj>     - stop that one address from the /lista ordering.
       /odjava sve|all    - stop every address for this chat."""
    rows = list_active_targets(chat_id)
    arg = (arg or "").strip().lower()

    if not rows:
        send_message(chat_id, "Nisi trenutno prijavljen ni na jednu adresu.")
        return

    if arg in ("sve", "sva", "all"):
        n = _deactivate([r["id"] for r in rows])
        send_message(chat_id, f"✅ Odjavljeno {n} adresa. Više nećeš dobijati obaveštenja.\n\nPredomislio si se? Ponovo se prijavi na najavaradova.rs.")
        print(f"🛑 chat_id={chat_id} unsubscribed ALL ({n})")
        return

    if arg.isdigit():
        idx = int(arg)
        if not (1 <= idx <= len(rows)):
            send_message(chat_id, f"Nemaš adresu pod brojem {idx}. Pošalji /lista da vidiš spisak.")
            return
        row = rows[idx - 1]
        _deactivate([row["id"]])
        send_message(chat_id, f"✅ Odjavljena adresa: {row['ulica']} {row['broj']}, {row['city']}.")
        print(f"🛑 chat_id={chat_id} unsubscribed target #{row['id']}")
        return

    if len(rows) == 1:
        _deactivate([rows[0]["id"]])
        send_message(chat_id, f"✅ Odjavljen si sa adrese {rows[0]['ulica']} {rows[0]['broj']}, {rows[0]['city']}.\n\nPredomislio si se? Ponovo se prijavi na najavaradova.rs.")
        print(f"🛑 chat_id={chat_id} unsubscribed target #{rows[0]['id']}")
        return

    # More than one address and no argument - make them choose.
    send_message(
        chat_id,
        f"Imaš {len(rows)} prijavljene adrese:\n{_format_list(rows)}\n\n"
        f"Odjava jedne: /odjava <broj>\nOdjava svih: /odjava sve",
    )


def complete_attach_all(pending_id, chat_id, username, subscriber_id):
    """One Telegram link for a building manager's whole list (/upravnici,
    scripts/local_web_server.py _load_manager_request): this chat goes onto every active address
    of the account that has no Telegram yet. Returns how many - 0 if the link was already used."""
    conn = get_connection()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM targets WHERE subscriber_id = ? AND is_active = 1 AND telegram_chat_id IS NULL",
            (subscriber_id,))]
        claimed = conn.execute(
            "UPDATE pending_registrations SET consumed_at = CURRENT_TIMESTAMP WHERE id = ? AND consumed_at IS NULL",
            (pending_id,)).rowcount
        if not claimed:
            conn.rollback()
            return 0
        conn.executemany(
            "UPDATE targets SET telegram_chat_id = ?, telegram_username = ?, telegram_active = 1 "
            "WHERE id = ? AND subscriber_id = ? AND telegram_chat_id IS NULL",
            [(str(chat_id), username, tid, subscriber_id) for tid in ids])
        conn.commit()
    finally:
        conn.close()
    from core.matchmaker import backfill_target_against_active_outages
    for tid in ids:
        try:
            backfill_target_against_active_outages(tid)
        except Exception as e:
            print(f"⚠️ backfill for target #{tid} failed (Telegram attach still OK): {e}")
    return len(ids)


def handle_start(chat_id, username, code):
    if not code:
        send_message(
            chat_id,
            "👋 Zdravo! Registracija ide preko sajta - najavaradova.rs - ne direktno kroz bot.\n\n"
            "Za odjavu u bilo kom trenutku, pošalji /odjava.",
        )
        return

    pending = find_pending(code)
    if not pending:
        send_message(
            chat_id,
            "⚠️ Ovaj link je nevažeći ili je već iskorišćen. Vrati se na sajt i pokušaj ponovo.",
        )
        return
    if not pending['subscriber_id']:
        # The address was submitted but the email confirmation link hasn't been clicked yet -
        # login is mandatory now (see PLAN.md's subscriber design), so this Telegram link can't
        # finish a registration on its own. Distinguishable from "invalid" on purpose: this
        # person already holds a real, live code from their own signup, not someone fishing for
        # other people's - telling them exactly what's missing is helpful, not a leak.
        send_message(
            chat_id,
            "📧 Skoro gotovo! Prvo potvrdi svoju adresu preko linka koji smo ti poslali na mejl, "
            "pa se vrati i klikni ponovo na ovaj Telegram link.",
        )
        return

    if pending['attach_all']:
        n = complete_attach_all(pending['id'], chat_id, username, pending['subscriber_id'])
        if not n:
            send_message(chat_id, "Ovaj link je već iskorišćen. Spisak adresa: /lista")
            return
        send_message(chat_id, f"✅ Telegram povezan za {n} adresa. Obaveštenja za njih stižu i ovde.\n\n"
                              f"Spisak adresa: /lista")
        print(f"✅ chat_id={chat_id} attached Telegram to {n} addresses (manager list)")
        return

    if pending['attach_target_id']:
        # "+ Poveži Telegram" on a dashboard address that already has email - attach to that
        # EXISTING target instead of the normal new-address path below.
        try:
            ok = complete_channel_attach(
                pending['id'], pending['attach_target_id'], chat_id, username, pending['subscriber_id']
            )
        except Exception as e:
            send_message(chat_id, "⚠️ Greška pri povezivanju, pokušaj ponovo sa svoje stranice.")
            print(f"⚠️ Telegram attach failed for chat {chat_id}: {e}")
            return
        if not ok:
            send_message(chat_id, f"Već si povezan/a za: {pending['ulica']} {pending['broj']}, {pending['city']}.\n\nSpisak svih adresa: /lista")
            return
        send_message(
            chat_id,
            f"✅ Telegram povezan za adresu {pending['ulica']} {pending['broj']}, {pending['city']}!\n\n"
            f"Sad dobijaš obaveštenja i ovde, pored emaila.",
        )
        print(f"✅ chat_id={chat_id} username=@{username} attached Telegram to target #{pending['attach_target_id']}")
        return

    # One chat can watch many addresses now - only block re-adding the SAME one.
    conn = get_connection()
    dup = conn.execute('''
        SELECT 1 FROM targets
        WHERE telegram_chat_id = ? AND is_active = 1
          AND UPPER(city) = UPPER(?) AND UPPER(ulica) = UPPER(?) AND broj = ?
    ''', (str(chat_id), pending['city'], pending['ulica'], pending['broj'])).fetchone()
    conn.close()
    if dup:
        conn = get_connection()
        conn.execute("UPDATE pending_registrations SET consumed_at = CURRENT_TIMESTAMP WHERE id = ?", (pending['id'],))
        conn.commit()
        conn.close()
        send_message(chat_id, f"Već si prijavljen za: {pending['ulica']} {pending['broj']}, {pending['city']}.\n\nSpisak svih adresa: /lista")
        return

    try:
        complete_registration(pending['id'], chat_id, username, pending['city'], pending['ulica'],
                               pending['broj'], pending['subscriber_id'])
    except Exception as e:
        send_message(chat_id, "⚠️ Greška pri registraciji, pokušaj ponovo sa sajta.")
        print(f"⚠️ Registration failed for chat {chat_id}: {e}")
        return

    count = len(list_active_targets(chat_id))
    extra = f"\n\nUkupno prijavljenih adresa: {count} (spisak: /lista)" if count > 1 else ""
    send_message(
        chat_id,
        f"✅ Registrovan si za obaveštenja!\n"
        f"{pending['ulica']} {pending['broj']}, {pending['city']}\n\n"
        f"Javićemo ti ovde kad bude najave radova za prijavljenu adresu."
        f"{extra}",
    )
    print(f"✅ chat_id={chat_id} username=@{username} registered via code={code}")


# ---------- Važno obaveštenje (core/broadcast.py) ----------

def _set_keyboard(cb, keyboard) -> None:
    """Replace the buttons under the message that was tapped."""
    msg = cb.get("message") or {}
    if not msg.get("message_id") or keyboard is None:
        return
    try:
        requests.post(f"{API}/editMessageReplyMarkup", json={
            "chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
            "reply_markup": keyboard}, timeout=10)
    except Exception:
        pass


def _feedback_keyboard(cb, outage_id: int, verdict: str) -> dict:
    """The alert's own buttons with the reader's 👍 / 👎 row marked, everything else (the copy
    button) left as it was. Tapping again changes the answer."""
    rows = [r for r in ((cb.get("message") or {}).get("reply_markup") or {}).get("inline_keyboard", [])
            if not any(str(b.get("callback_data", "")).startswith("fb:") for b in r)]
    from core.feedback import buttons
    up, down = buttons(outage_id)
    if verdict == "RIGHT":
        up = {**up, "text": "• 👍 Tačno •"}
    else:
        down = {**down, "text": "• 👎 Nije tačno •"}
    return {"inline_keyboard": rows + [[up, down]]}


def handle_callback(cb):
    """Every button tap. ✅ / ❌ on a city-wide draft, a held alert, a manager's list or the weekly
    review count only from the operator - the check is on who tapped, not on which chat the
    message is in; anyone else gets a silent answer and nothing happens. 👍 / 👎 under an alert
    counts only from a chat that alert was sent to (core/feedback.py)."""
    from core import broadcast, feedback, managers, review, send_check
    data, who = cb.get("data") or "", (cb.get("from") or {}).get("id")
    m = broadcast.CALLBACK.match(data)
    answer = ""
    held = send_check.CALLBACK.match(data)
    mgr = managers.CALLBACK.match(data)
    fb = feedback.CALLBACK.match(data)
    rv = review.CALLBACK.match(data)
    if fb:
        outage_id, verdict = int(fb[2]), feedback.VERDICTS[fb[1]]
        if feedback.record(outage_id, "telegram", who, verdict):
            answer = "Hvala! Zabeleženo." if verdict == "RIGHT" else "Hvala - proverićemo ovo obaveštenje."
            _set_keyboard(cb, _feedback_keyboard(cb, outage_id, verdict))
            print(f"👍👎 feedback on outage #{outage_id}: {verdict}")
    elif rv and is_operator(who):
        answer, keyboard = review.decide(int(rv[2]), rv[1])
        _set_keyboard(cb, keyboard)
    elif mgr and is_operator(who):
        # ❌ on a manager's loaded list (core/managers.py): deactivate what it created.
        request_id = int(mgr[1])
        answer = managers.removal_answer(managers.remove(request_id))
        print(f"🏢 manager request #{request_id}: {answer}")
        msg = cb.get("message") or {}
        if msg.get("message_id"):
            try:
                requests.post(f"{API}/editMessageReplyMarkup", json={
                    "chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                    "reply_markup": {"inline_keyboard": []}}, timeout=10)
            except Exception:
                pass
            send_message(msg["chat"]["id"], f"Upravnik #{request_id}: {answer}")
    elif held and is_operator(who):
        # ✅ / ❌ on a held alert (core/send_check.py).
        outage_id = int(held[2])
        n = send_check.decide(outage_id, held[1] == "ok")
        answer = ("Već odlučeno - ništa nije promenjeno." if n is None
                  else f"Šalje se: {n} poruka." if held[1] == "ok" else f"Ne šalje se ({n} poruka).")
        print(f"🛑 held outage #{outage_id} {held[1]}: {answer}")
        msg = cb.get("message") or {}
        if msg.get("message_id"):
            try:
                requests.post(f"{API}/editMessageReplyMarkup", json={
                    "chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                    "reply_markup": {"inline_keyboard": []}}, timeout=10)
            except Exception:
                pass
            send_message(msg["chat"]["id"], f"#{outage_id}: {answer}")
    elif m and is_operator(who):
        action, broadcast_id = m[1], int(m[2])
        if action == "ok":
            n = broadcast.approve(broadcast_id)
            answer = f"Šalje se na {n}." if n is not None else "Već odlučeno - ništa nije poslato."
        else:
            answer = "Odbačeno." if broadcast.reject(broadcast_id) else "Već odlučeno."
        print(f"📣 broadcast #{broadcast_id} {action}: {answer}")
        msg = cb.get("message") or {}
        if msg.get("message_id"):
            try:
                requests.post(f"{API}/editMessageReplyMarkup", json={
                    "chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                    "reply_markup": {"inline_keyboard": []}}, timeout=10)
            except Exception:
                pass
            send_message(msg["chat"]["id"], f"#{broadcast_id}: {answer}")
    try:
        requests.post(f"{API}/answerCallbackQuery",
                      json={"callback_query_id": cb.get("id"), "text": answer}, timeout=10)
    except Exception:
        pass


def handle_izmeni(chat_id, text):
    """/izmeni <id> <novi tekst> - a new text for a waiting draft; the new preview follows."""
    from core import broadcast
    parts = text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        send_message(chat_id, "Upotreba: /izmeni <broj> <novi tekst>")
        return
    if not broadcast.edit(int(parts[1]), parts[2]):
        send_message(chat_id, f"#{parts[1]} nije nacrt koji čeka - ništa nije izmenjeno.")


def handle_obavestenja(chat_id, arg):
    """/obavestenja off|on - city-wide notices (heating season and the like) for every address of
    this chat. Outage alerts are not affected."""
    from core import broadcast
    a = (arg or "").lower()
    if a in ("off", "iskljuci", "isključi", "ne"):
        broadcast.set_opt_out(chat_id=chat_id, off=True)
        send_message(chat_id, "U redu - nećete dobijati obaveštenja za ceo grad. Obaveštenja o "
                              "radovima na vašoj adresi stižu i dalje. Ponovo: /obavestenja on")
    elif a in ("on", "ukljuci", "uključi", "da"):
        broadcast.set_opt_out(chat_id=chat_id, off=False)
        send_message(chat_id, "Uključeno - javljamo i retka obaveštenja za ceo grad (npr. početak "
                              "grejne sezone).")
    else:
        send_message(chat_id, "Retka obaveštenja za ceo grad (npr. početak grejne sezone).\n"
                              "Isključi: /obavestenja off\nUključi: /obavestenja on")


def run_bot():
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set - see .env.example. Aborting.")
        return

    print("🤖 Bot listening for /start... (Ctrl+C to stop)")
    offset = None

    while True:
        params = {"timeout": 30}
        if offset is not None:
            params["offset"] = offset

        res = requests.get(f"{API}/getUpdates", params=params, timeout=35)
        updates = res.json().get("result", [])

        for update in updates:
            offset = update["update_id"] + 1
            if update.get("callback_query"):
                handle_callback(update["callback_query"])
                continue
            message = update.get("message")
            if not message:
                continue

            chat_id = message["chat"]["id"]
            username = message["chat"].get("username", "")
            text = message.get("text", "")

            reply_to = (message.get("reply_to_message") or {}).get("message_id")
            if reply_to and text and not text.startswith("/") and is_operator(chat_id):
                # The operator answering a weekly-review notice: the places our reading missed.
                from core import review
                done = review.missing_reply(reply_to, text)
                if done is not None:
                    n, keyboard = done
                    _set_keyboard({"message": {"chat": {"id": chat_id}, "message_id": reply_to}}, keyboard)
                    send_message(chat_id, f"Zabeleženo: fali {n}." if n else "Zabeleženo: ništa ne fali.")
                    continue

            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                code = parts[1].strip() if len(parts) > 1 else None
                print(f"📩 /start from chat_id={chat_id} username=@{username} code={code!r}")
                handle_start(chat_id, username, code)
            elif text.startswith("/lista") or text.startswith("/adrese"):
                print(f"📩 /lista from chat_id={chat_id}")
                handle_lista(chat_id)
            elif text.startswith("/stop") or text.startswith("/odjava"):
                parts = text.split(maxsplit=1)
                arg = parts[1].strip() if len(parts) > 1 else None
                print(f"📩 /odjava from chat_id={chat_id} arg={arg!r}")
                handle_stop(chat_id, arg)
            elif text.startswith("/obavestenja"):
                parts = text.split(maxsplit=1)
                handle_obavestenja(chat_id, parts[1].strip() if len(parts) > 1 else None)
            elif text.startswith("/pregled"):
                # Operator-only and silent for anyone else: the weekly review, right now.
                if is_operator(chat_id):
                    from datetime import datetime
                    from core import review
                    batch = datetime.now(review.TZ).strftime("%Y-%m-%d %H:%M ručno")
                    review.send_batch(batch)        # says so itself when there is nothing
            elif text.startswith("/izmeni"):
                # Operator-only and silent for anyone else, like /stats.
                if is_operator(chat_id):
                    handle_izmeni(chat_id, text)
            elif text.startswith("/stats"):
                # Deliberately silent for everyone else: no reply, no "unknown command", nothing
                # that confirms the command exists. A stranger poking at the bot learns nothing.
                if is_operator(chat_id):
                    parts = text.split(maxsplit=1)
                    arg = parts[1].strip() if len(parts) > 1 else None
                    print(f"📩 /stats from OPERATOR chat_id={chat_id} arg={arg!r}")
                    handle_stats(chat_id, arg)
                else:
                    print(f"🚫 /stats refused for chat_id={chat_id} (not the operator)")


if __name__ == "__main__":
    run_bot()
