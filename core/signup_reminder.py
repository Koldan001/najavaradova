"""One reminder email for a signup that stopped halfway on Telegram.

Someone who picks Telegram confirms their email and lands on the dashboard, but the address is
saved only when they then open our bot and press Start (pending_registrations, completed by
workers/telegram_bot.complete_registration). On a computer without Telegram, or a phone where
the app didn't open, they stop there: an account with no address, "0 adresa · bez kanala" in
/stats (30.09.2026, a signup from Google via the Svilajnac city page, 23 seconds from form to
confirmed email, never in Telegram).

A few hours later, in the daytime, they get one email with two buttons: "Otvori Telegram" (the
same deep link) and "Šalji mi na email umesto toga" (a confirm link: /auth/verify saves the
address with email and drops the Telegram link). Only for accounts with no active address at all
- someone already getting alerts who left a second Telegram link unfinished sees it on their
dashboard. Once per account; nothing is switched without their click."""
import json
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

import config
from core import auth
from core.database import get_connection

BELGRADE = ZoneInfo("Europe/Belgrade")
REMIND_AFTER_HOURS = 3
# The bot honours a deep link for 24 hours (workers/telegram_bot.PENDING_CODE_LIFETIME_HOURS).
# Past this, too little of that is left for the Telegram button to be worth sending.
REMIND_BEFORE_HOURS = 20
TELEGRAM_LINK_HOURS = 24
# Belgrade hours the reminder may go out in: from 08:00, before 21:00.
DAYTIME = (8, 21)
EMAIL_LINK_TTL_MINUTES = 24 * 60

_CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e", "ж": "ž", "з": "z",
        "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m", "н": "n", "њ": "nj", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "ћ": "ć", "у": "u", "ф": "f", "х": "h", "ц": "c",
        "ч": "č", "џ": "dž", "ш": "š"}

_bot = None


def _bot_username() -> str:
    global _bot
    if _bot is None:
        data = requests.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe",
                            timeout=10).json()
        if not data.get("ok"):
            raise RuntimeError(f"getMe failed: {data}")
        _bot = data["result"]["username"]
    return _bot


def _latin(s: str) -> str:
    """Title-cased Serbian Latin for a stored (often Cyrillic, upper-cased) name."""
    return "".join(_CYR.get(ch, ch) for ch in (s or "").lower()).title()


def _due(now_utc: datetime) -> dict:
    """{subscriber_id: {"email", "addresses": {address key: pending row}}} for everyone due a
    reminder, the newest link per address."""
    stamp = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        rows = conn.execute('''
            SELECT p.id, p.code, p.city, p.ulica, p.broj, p.created_at, p.subscriber_id, s.email
            FROM pending_registrations p JOIN subscribers s ON s.id = p.subscriber_id
            WHERE p.channel = 'telegram' AND p.consumed_at IS NULL AND p.reminded_at IS NULL
              AND p.attach_target_id IS NULL
              AND p.created_at <= datetime(?, ?) AND p.created_at > datetime(?, ?)
              AND NOT EXISTS (SELECT 1 FROM targets t
                              WHERE t.subscriber_id = p.subscriber_id AND t.is_active = 1)
              AND NOT EXISTS (SELECT 1 FROM pending_registrations q
                              WHERE q.subscriber_id = p.subscriber_id AND q.reminded_at IS NOT NULL)
            ORDER BY p.id DESC
        ''', (stamp, f"-{REMIND_AFTER_HOURS} hours", stamp, f"-{REMIND_BEFORE_HOURS} hours")).fetchall()
    finally:
        conn.close()
    people = {}
    for r in rows:
        person = people.setdefault(r["subscriber_id"], {"email": r["email"], "addresses": {}})
        person["addresses"].setdefault((r["city"].upper(), r["ulica"].upper(), r["broj"]), r)
    return people


def _email(to: str, items: list) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Tvoja adresa još nije sačuvana — NajavaRadova.rs"
    msg["To"] = to
    one = len(items) == 1
    intro = ("Prijavio/la si se za obaveštenja o isključenjima, ali Telegram nije povezan, pa "
             + ("adresa još nije sačuvana" if one else "adrese još nisu sačuvane")
             + " i ne možemo da ti javimo za najavljena isključenja.")
    until = min(i["until"] for i in items).strftime("%d.%m. u %H:%M")
    footer = (f"Link za Telegram važi do {until}, a link za email 24 sata. Ovo je jedini podsetnik — "
              "ako ne želiš obaveštenja, samo ignoriši ovaj mejl.")

    text = [intro, ""]
    for i in items:
        text += [i["address"], f"  Otvori Telegram i pritisni Start: {i['telegram']}",
                 f"  Ili primaj obaveštenja na email: {i['email']}", ""]
    msg.set_content("\n".join(text + [footer]))

    esc = auth._html_escape
    blocks = "".join(f"""
  <p style="color:#F2F0EE;font-size:15px;font-weight:600;margin:22px 0 0;">{esc(i['address'])}</p>
  <a href="{esc(i['telegram'])}" style="display:inline-block;margin-top:12px;padding:13px 22px;border-radius:10px;
    background:#F2F0EE;color:#101013;font-size:15px;font-weight:600;text-decoration:none;">Otvori Telegram</a>
  <a href="{esc(i['email'])}" style="display:inline-block;margin-top:12px;padding:12px 20px;border-radius:10px;
    border:1px solid #3A3A42;color:#F2F0EE;font-size:14.5px;font-weight:500;text-decoration:none;">
    Šalji mi na email umesto toga</a>""" for i in items)
    msg.add_alternative(f"""\
<!doctype html><html lang="sr"><body style="margin:0;padding:32px 16px;background:#101013;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" style="max-width:480px;margin:0 auto;border-collapse:collapse;">
<tr><td style="background:#1B1B20;border:1px solid #2B2B31;border-radius:16px;padding:28px 26px;">
  <div style="color:#F2F0EE;font-size:16px;font-weight:700;letter-spacing:-.02em;">
    NajavaRadova<span style="color:#7FA9F5;font-weight:400;">.rs</span>
  </div>
  <p style="color:#F2F0EE;font-size:15px;line-height:1.55;margin:22px 0 0;">{esc(intro)}</p>{blocks}
  <hr style="border:none;border-top:1px solid #2B2B31;margin:24px 0 16px;">
  <p style="margin:0;font-size:12.5px;color:#8C8892;line-height:1.5;">{esc(footer)}</p>
</td></tr>
</table>
</body></html>""", subtype="html")
    return msg


def send_signup_reminders(now: datetime | None = None) -> int:
    """Sends the reminders that are due; returns how many people got one. A failed send leaves
    the account unmarked, so the next cycle tries again (until REMIND_BEFORE_HOURS)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not DAYTIME[0] <= now.astimezone(BELGRADE).hour < DAYTIME[1]:
        return 0
    people = _due(now)
    if not people:
        return 0
    bot = _bot_username()
    sent = 0
    for subscriber_id, person in people.items():
        items = []
        for r in person["addresses"].values():
            made = datetime.strptime(r["created_at"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            items.append({
                "address": f"{_latin(r['city'])}, {_latin(r['ulica'])} {r['broj']}",
                "telegram": f"https://t.me/{bot}?start={r['code']}",
                "email": auth.issue_link(subscriber_id, {
                    "city": r["city"], "ulica": r["ulica"], "broj": r["broj"], "channel": "email",
                    "cancel_telegram_code": r["code"]}, EMAIL_LINK_TTL_MINUTES),
                "until": (made + timedelta(hours=TELEGRAM_LINK_HOURS)).astimezone(BELGRADE),
            })
        try:
            auth.send_account_email(_email(person["email"], items))
        except Exception as e:
            print(f"⚠️ signup reminder for subscriber #{subscriber_id} failed, retried next cycle: {e}")
            continue
        conn = get_connection()
        try:
            conn.execute("UPDATE pending_registrations SET reminded_at = CURRENT_TIMESTAMP "
                         "WHERE subscriber_id = ? AND consumed_at IS NULL", (subscriber_id,))
            conn.commit()
        finally:
            conn.close()
        sent += 1
    return sent


# --- The confirmation link, never clicked -----------------------------------------------------
# A step earlier than the above: the form was sent, the "Potvrdi prijavu" mail went out, and the
# link was never clicked - so nothing is saved, not even the account's way in (14.10.2026: a
# Kragujevac signup from Google, address typed in, gone). An hour on, in the daytime, one more
# mail with a fresh link. Once per account, and only while it has never logged in at all.
CONFIRM_REMIND_AFTER_HOURS = 1
CONFIRM_REMIND_BEFORE_HOURS = 20


def _confirm_due(now_utc: datetime) -> list:
    """The newest unclicked confirmation per account that is due a reminder."""
    stamp = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        return conn.execute('''
            SELECT s.id AS subscriber_id, s.email, t.payload_json
            FROM subscribers s
            JOIN login_tokens t ON t.id = (SELECT MAX(id) FROM login_tokens
                                           WHERE subscriber_id = s.id AND purpose = 'confirm')
            WHERE s.confirm_reminded_at IS NULL
              AND t.used_at IS NULL
              AND t.created_at <= datetime(?, ?) AND t.created_at > datetime(?, ?)
              AND NOT EXISTS (SELECT 1 FROM login_tokens u
                              WHERE u.subscriber_id = s.id AND u.used_at IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM targets g WHERE g.subscriber_id = s.id)
        ''', (stamp, f"-{CONFIRM_REMIND_AFTER_HOURS} hours",
              stamp, f"-{CONFIRM_REMIND_BEFORE_HOURS} hours")).fetchall()
    finally:
        conn.close()


def _confirm_email(to: str, address: str, link: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Potvrdi prijavu — adresa još nije sačuvana"
    msg["To"] = to
    intro = (f"Prijavio/la si se za obaveštenja o isključenjima za adresu {address}, ali prijava "
             "još nije potvrđena. Dok je ne potvrdiš, ne možemo da ti javimo kad nestane vode, "
             "struje ili grejanja.")
    footer = ("Link važi 24 sata i radi samo jednom. Ovo je jedini podsetnik — ako ne želiš "
              "obaveštenja, samo ignoriši ovaj mejl.")
    msg.set_content(f"{intro}\n\nPotvrdi adresu: {link}\n\n{footer}")
    esc = auth._html_escape
    msg.add_alternative(f"""\
<!doctype html><html lang="sr"><body style="margin:0;padding:32px 16px;background:#101013;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" style="max-width:480px;margin:0 auto;border-collapse:collapse;">
<tr><td style="background:#1B1B20;border:1px solid #2B2B31;border-radius:16px;padding:28px 26px;">
  <div style="color:#F2F0EE;font-size:16px;font-weight:700;letter-spacing:-.02em;">
    NajavaRadova<span style="color:#7FA9F5;font-weight:400;">.rs</span>
  </div>
  <p style="color:#F2F0EE;font-size:15px;line-height:1.55;margin:22px 0 0;">{esc(intro)}</p>
  <a href="{esc(link)}" style="display:inline-block;margin-top:20px;padding:13px 22px;border-radius:10px;
    background:#F2F0EE;color:#101013;font-size:15px;font-weight:600;text-decoration:none;">Potvrdi adresu</a>
  <hr style="border:none;border-top:1px solid #2B2B31;margin:24px 0 16px;">
  <p style="margin:0;font-size:12.5px;color:#8C8892;line-height:1.5;">{esc(footer)}</p>
</td></tr>
</table>
</body></html>""", subtype="html")
    return msg


def send_confirm_reminders(now: datetime | None = None) -> int:
    """Sends the due "potvrdi prijavu" reminders; returns how many went out. A failed send leaves
    the account unmarked, so the next cycle tries again (until CONFIRM_REMIND_BEFORE_HOURS)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not DAYTIME[0] <= now.astimezone(BELGRADE).hour < DAYTIME[1]:
        return 0
    sent = 0
    for r in _confirm_due(now):
        payload = json.loads(r["payload_json"] or "{}")
        # A manager's list (core/managers.py) carries its own label, "8 adresa".
        address = payload.get("label") or ", ".join(p for p in (_latin(payload.get("city", "")),
                                        f"{_latin(payload.get('ulica', ''))} {payload.get('broj', '')}".strip()) if p)
        link = auth.issue_link(r["subscriber_id"], payload, auth.CONFIRM_TOKEN_TTL_MINUTES)
        try:
            auth.send_account_email(_confirm_email(r["email"], address, link))
        except Exception as e:
            print(f"⚠️ confirm reminder for subscriber #{r['subscriber_id']} failed, retried next cycle: {e}")
            continue
        conn = get_connection()
        try:
            conn.execute("UPDATE subscribers SET confirm_reminded_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (r["subscriber_id"],))
            conn.commit()
        finally:
            conn.close()
        sent += 1
    return sent
