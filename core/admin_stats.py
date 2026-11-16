"""Operator-only analytics: who signed up, how many addresses they registered, on which
channels, and whether delivery is healthy.

This module reads subscriber emails and street addresses - the most sensitive data in the whole
system. It is therefore deliberately NOT reachable over HTTP. The only two ways to see this
output are:

  1. `/stats` sent to the Telegram bot from config.OPERATOR_CHAT_ID (exact match, fail-closed) -
     workers/telegram_bot.py. Auth is the operator's own Telegram account; the bot never answers
     that command from any other chat, and never even acknowledges it exists.
  2. `python -m scripts.admin_stats` on the VM, i.e. behind SSH.

Adding a web page for this would put every subscriber's email + home address behind a single
HTTP auth check exposed to the internet 24/7 - the highest-value target in the project, for a
page that gets looked at a few times a day. Telegram + SSH give the same information with no new
attack surface at all. See scripts/admin_stats.py for the CLI wrapper.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.database import get_connection

# created_at columns are SQLite's CURRENT_TIMESTAMP, i.e. UTC. Everything shown to the operator
# is rendered in Serbian local time instead - a bare UTC stamp read two hours off.
BELGRADE = ZoneInfo("Europe/Belgrade")


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return (row[0] if row and row[0] is not None else 0)


def collect(days: int = 7, recent_limit: int = 10) -> dict:
    """Every number the operator views are built from. `days` scopes the "new lately" and
    delivery figures; totals are always all-time."""
    window = f"-{int(days)} days"
    conn = get_connection()
    try:
        s = {"days": int(days)}

        s["subscribers_total"] = _scalar(conn, "SELECT COUNT(*) FROM subscribers")
        s["subscribers_new"] = _scalar(
            conn, "SELECT COUNT(*) FROM subscribers WHERE created_at >= datetime('now', ?)",
            (window,))
        s["targets_active"] = _scalar(conn, "SELECT COUNT(*) FROM targets WHERE is_active = 1")
        s["targets_paused"] = _scalar(conn, "SELECT COUNT(*) FROM targets WHERE is_active = 0")
        # Active only, so it reads consistently against targets_active on the same line - a
        # "+6 novih" next to "5 adresa" (one of them since paused) just looks like a bug.
        s["targets_new"] = _scalar(
            conn, "SELECT COUNT(*) FROM targets WHERE is_active = 1 "
                  "AND created_at >= datetime('now', ?)", (window,))

        # Channel mix across live addresses. "both" is the one worth watching - it's the
        # multi-channel feature actually being used.
        mix = conn.execute("""
            SELECT
              SUM(CASE WHEN email IS NOT NULL AND telegram_chat_id IS NOT NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN email IS NOT NULL AND telegram_chat_id IS NULL     THEN 1 ELSE 0 END),
              SUM(CASE WHEN email IS NULL     AND telegram_chat_id IS NOT NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN email IS NULL     AND telegram_chat_id IS NULL     THEN 1 ELSE 0 END)
            FROM targets WHERE is_active = 1
        """).fetchone()
        s["ch_both"], s["ch_email_only"], s["ch_tg_only"], s["ch_none"] = [m or 0 for m in mix]

        # Channels a user switched off from the dashboard while keeping the address.
        off = conn.execute("""
            SELECT
              SUM(CASE WHEN email IS NOT NULL AND email_active = 0 THEN 1 ELSE 0 END),
              SUM(CASE WHEN telegram_chat_id IS NOT NULL AND telegram_active = 0 THEN 1 ELSE 0 END)
            FROM targets WHERE is_active = 1
        """).fetchone()
        s["email_muted"], s["tg_muted"] = [o or 0 for o in off]

        # How many addresses each subscriber keeps - 1 is a resident, 3+ is a building manager.
        s["addr_per_sub"] = [
            (r[0], r[1]) for r in conn.execute("""
                SELECT n, COUNT(*) FROM (
                  SELECT subscriber_id, COUNT(*) AS n FROM targets
                  WHERE is_active = 1 AND subscriber_id IS NOT NULL GROUP BY subscriber_id
                ) GROUP BY n ORDER BY n
            """).fetchall()
        ]

        s["top_cities"] = [
            (r[0], r[1]) for r in conn.execute("""
                SELECT city, COUNT(*) c FROM targets WHERE is_active = 1
                GROUP BY UPPER(city) ORDER BY c DESC, city LIMIT 8
            """).fetchall()
        ]

        # Where the window's new accounts found us (core/auth.clean_source), and how many of them
        # got as far as a first address - a source whose people never confirm is worth knowing.
        s["sources"] = [
            (r[0], r[1], r[2]) for r in conn.execute("""
                SELECT IFNULL(s.signup_source, 'nepoznato'), COUNT(*),
                       SUM(CASE WHEN EXISTS (SELECT 1 FROM targets t WHERE t.subscriber_id = s.id)
                                THEN 1 ELSE 0 END)
                FROM subscribers s WHERE s.created_at >= datetime('now', ?)
                GROUP BY 1 ORDER BY 2 DESC, 1
            """, (window,)).fetchall()
        ]
        s["source_pages"] = [
            (r[0], r[1]) for r in conn.execute("""
                SELECT signup_page, COUNT(*) FROM subscribers
                WHERE signup_page IS NOT NULL AND created_at >= datetime('now', ?)
                GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 8
            """, (window,)).fetchall()
        ]

        # The signup form (web/signup.js -> /api/ev): opened and sent, and the pages it was opened
        # on most. The table is new on 12.10.2026; a DB without it has no line for it.
        try:
            s["form"] = dict(conn.execute(
                "SELECT event, SUM(n) FROM signup_events WHERE day >= date('now', ?) GROUP BY event",
                (window,)).fetchall())
            s["form_pages"] = conn.execute(
                "SELECT page, SUM(n) FROM signup_events WHERE event = 'open' AND day >= date('now', ?) "
                "GROUP BY page ORDER BY 2 DESC, 1 LIMIT 6", (window,)).fetchall()
        except Exception:
            s["form"], s["form_pages"] = {}, []

        s["recent"] = [dict(r) for r in conn.execute("""
            SELECT s.id, s.email, s.created_at, s.signup_source,
                   COUNT(t.id) AS addr,
                   SUM(CASE WHEN t.email IS NOT NULL THEN 1 ELSE 0 END) AS with_email,
                   SUM(CASE WHEN t.telegram_chat_id IS NOT NULL THEN 1 ELSE 0 END) AS with_tg
            FROM subscribers s
            LEFT JOIN targets t ON t.subscriber_id = s.id AND t.is_active = 1
            GROUP BY s.id ORDER BY s.id DESC LIMIT ?
        """, (int(recent_limit),)).fetchall()]

        s["delivery"] = [
            (r[0], r[1], r[2]) for r in conn.execute("""
                SELECT status, IFNULL(channel, '?'), COUNT(*) FROM message_queue
                WHERE created_at >= datetime('now', ?)
                GROUP BY status, channel ORDER BY status, channel
            """, (window,)).fetchall()
        ]
        # The second check (core/alert_check.py): how often another model's reading of the notice
        # reaches the same person on the same day. The table is new on 31.10.2026.
        try:
            s["checks"] = dict(conn.execute(
                "SELECT verdict, COUNT(*) FROM alert_checks WHERE created_at >= datetime('now', ?) "
                "GROUP BY verdict", (window,)).fetchall())
            s["check_disagree"] = [r[0] for r in conn.execute(
                "SELECT DISTINCT outage_id FROM alert_checks WHERE verdict = 'DISAGREE' "
                "AND created_at >= datetime('now', ?) ORDER BY outage_id DESC LIMIT 8", (window,))]
        except Exception:
            s["checks"], s["check_disagree"] = {}, []
        # What a person checked (core/review.py's weekly review, core/feedback.py's 👍 / 👎) -
        # the figures the second check above is measured against.
        try:
            from core.review import quality
            s["quality"] = quality(days=90)
        except Exception:
            s["quality"] = None
        s["queue_backlog"] = _scalar(
            conn, "SELECT COUNT(*) FROM message_queue WHERE status = 'QUEUED'")
        s["pending_links"] = _scalar(conn, """
            SELECT COUNT(*) FROM pending_registrations
            WHERE consumed_at IS NULL AND created_at >= datetime('now', '-24 hours')
        """)
        return s
    finally:
        conn.close()


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Serbian numeric agreement: 1/21/31 -> `one`, 2-4/22-24 -> `few`, everything else
    (0, 5-20, the 11-14 teens) -> `many`."""
    t, u = n % 100, n % 10
    if u == 1 and t != 11:
        return one
    if 2 <= u <= 4 and not (12 <= t <= 14):
        return few
    return many


def _adrese(n: int) -> str:
    return f"{n} " + _plural(n, "adresa", "adrese", "adresa")


def _when(ts: str | None) -> str:
    """SQLite CURRENT_TIMESTAMP is UTC; the operator reads this in Belgrade time. Renders as
    'danas 19:32' / 'juče 14:20' / '04.09. u 09:15' - a bare '2026-09-06 15:32' was both two
    hours off and harder to place at a glance."""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(str(ts).strip()[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(ts)[:16]
    local = dt.replace(tzinfo=timezone.utc).astimezone(BELGRADE)
    today = datetime.now(BELGRADE).date()
    delta = (today - local.date()).days
    if delta == 0:
        return f"danas {local:%H:%M}"
    if delta == 1:
        return f"juče {local:%H:%M}"
    return f"{local:%d.%m.} u {local:%H:%M}"


def _channels_label(with_email: int, with_tg: int) -> str:
    if with_email and with_tg:
        return "📧 email + 💬 telegram"
    if with_tg:
        return "💬 telegram"
    if with_email:
        return "📧 email"
    return "⚠️ bez kanala"


def format_summary(s: dict, html: bool = False) -> str:
    """The operator's report. Sectioned with a heading per question rather than one flat block -
    the first version was a wall of counters nobody could read on a phone.

    `html=True` adds Telegram <b> headings (workers/telegram_bot.py sends it with parse_mode
    HTML and falls back to plain on any API error); the CLI takes the plain form.
    """
    def h(text):
        return f"<b>{text}</b>" if html else text.upper()

    def esc(text):
        t = str(text)
        return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) if html else t

    out = [h(f"📊 Pregled · poslednjih {s['days']} dana"), ""]

    # --- people ---------------------------------------------------------------------------
    subs, subs_new = s["subscribers_total"], s["subscribers_new"]
    act, act_new = s["targets_active"], s["targets_new"]
    out.append(h("👥 Ljudi"))
    if subs:
        word = _plural(subs, "pretplatnik", "pretplatnika", "pretplatnika")
        out.append(f"• {subs} {word}" + (f"  (+{subs_new} novih)" if subs_new else ""))
        out.append(f"• {_adrese(act)} prati obaveštenja"
                   + (f"  (+{act_new} novih)" if act_new else ""))
        if s["targets_paused"]:
            p = s["targets_paused"]
            out.append(f"• {_adrese(p)} {_plural(p, 'pauzirana', 'pauzirane', 'pauzirano')}")
    else:
        out.append("• još nema nijedne prijave")
    out.append("")

    # --- channels -------------------------------------------------------------------------
    if act:
        out.append(h("📡 Kako ih obaveštavamo"))
        for count, label in ((s["ch_both"], "📧 email + 💬 telegram"),
                             (s["ch_email_only"], "📧 samo email"),
                             (s["ch_tg_only"], "💬 samo telegram")):
            if count:
                out.append(f"• {_adrese(count)} — {label}")
        if s["ch_none"]:
            out.append(f"• ⚠️ {_adrese(s['ch_none'])} bez ijednog kanala — ovo ne treba da postoji")
        if s["email_muted"]:
            out.append(f"• 🔕 {_adrese(s['email_muted'])} — ugašen email")
        if s["tg_muted"]:
            out.append(f"• 🔕 {_adrese(s['tg_muted'])} — ugašen telegram")
        out.append("")

    # --- where ----------------------------------------------------------------------------
    if s["top_cities"]:
        out.append(h("🏙 Gradovi"))
        out += [f"• {esc(city)} — {_adrese(c)}" for city, c in s["top_cities"]]
        out.append("")

    # --- how many addresses each holds ----------------------------------------------------
    if s["addr_per_sub"]:
        out.append(h("👤 Koliko adresa drže"))
        for n, people in s["addr_per_sub"]:
            who = f"{people} " + _plural(people, "korisnik", "korisnika", "korisnika")
            out.append(f"• {_adrese(n)} — {who}")
        out.append("")

    # --- where they come from -------------------------------------------------------------
    if s.get("sources"):
        out.append(h(f"🧭 Odakle dolaze · {s['days']} dana"))
        for source, n, finished in s["sources"]:
            left = n - (finished or 0)
            out.append(f"• {esc(source)} — {n}" + (f"  ({left} bez adrese)" if left else ""))
        if s.get("source_pages"):
            out.append("• preko stranica: " + ", ".join(f"{esc(p)} {n}" for p, n in s["source_pages"]))
        out.append("")

    # --- the signup form ------------------------------------------------------------------
    if s.get("form"):
        opened, sent = s["form"].get("open", 0), s["form"].get("submit", 0)
        out.append(h(f"📝 Forma za prijavu · {s['days']} dana"))
        out.append(f"• otvorena {opened}×, poslata {sent}×")
        if s.get("form_pages"):
            out.append("• otvarana na: " + ", ".join(f"{esc(p)} {n}" for p, n in s["form_pages"]))
        out.append("")

    # --- recent signups -------------------------------------------------------------------
    if s["recent"]:
        out.append(h("🆕 Poslednje prijave"))
        for r in s["recent"]:
            ch = _channels_label(r["with_email"] or 0, r["with_tg"] or 0)
            src = f" · {esc(r['signup_source'])}" if r.get("signup_source") else ""
            out.append(f"• {esc(r['email'])}")
            out.append(f"   {_adrese(r['addr'] or 0)} · {ch}{src} · {_when(r['created_at'])}")
        out.append("")

    # --- delivery -------------------------------------------------------------------------
    # Collapsed from raw (status, channel) rows into the three outcomes that mean something,
    # with the channel split kept in brackets so a one-sided failure is still visible.
    by_status: dict[str, dict[str, int]] = {}
    for status, channel, n in s["delivery"]:
        by_status.setdefault(status, {})[channel] = by_status.setdefault(status, {}).get(channel, 0) + n

    def _line(icon, status, one, few, many):
        chans = by_status.get(status)
        if not chans:
            return None
        total = sum(chans.values())
        detail = ", ".join(f"{v} {k}" for k, v in sorted(chans.items()))
        word = _plural(total, "poruka", "poruke", "poruka")
        return f"• {icon} {total} {word} {_plural(total, one, few, many)}  ({detail})"

    out.append(h(f"📬 Isporuka · {s['days']} dana"))
    rows = [r for r in (_line("✅", "SENT", "poslata", "poslate", "poslato"),
                        _line("⏳", "QUEUED", "čeka", "čekaju", "čeka"),
                        _line("⏭️", "SKIPPED", "preskočena (zakasnila)", "preskočene (zakasnile)",
                              "preskočeno (zakasnilo)"),
                        _line("❌", "FAILED", "nije uspela", "nisu uspele", "nije uspelo")) if r]
    out += rows or ["• ništa nije slato u ovom periodu"]

    checks = s.get("checks") or {}
    agree, disagree = checks.get("AGREE", 0), checks.get("DISAGREE", 0)
    if agree + disagree:
        rate = round(100 * agree / (agree + disagree))
        line = f"• 🔎 druga provera: {agree} od {agree + disagree} se slaže ({rate}%)"
        if s.get("check_disagree"):
            line += " · ne slaže se: " + ", ".join(f"#{i}" for i in s["check_disagree"])
        out.append(line)
    q = s.get("quality")
    if q and any(q[k] for k in ("random_right", "random_wrong", "street_right", "street_wrong",
                                "notices", "fb_right", "fb_wrong", "open")):
        from core.review import format_quality
        first, *rest = format_quality(q)
        out.append(f"• 🧑‍⚖️ {first}")
        out += rest
    if s["queue_backlog"]:
        out.append(f"• ⏳ trenutno u redu: {s['queue_backlog']}")
    if s["pending_links"]:
        out.append(f"• 🔗 nepotvrđenih telegram linkova (24h): {s['pending_links']}")
    return "\n".join(out).rstrip()


def new_since(last_subscriber_id: int, last_target_id: int) -> dict:
    """Subscribers and addresses created since the given ids - the feed behind the watchdog's
    "someone just signed up" DM. Returns the rows plus the new high-water marks."""
    conn = get_connection()
    try:
        subs = [dict(r) for r in conn.execute(
            "SELECT id, email, created_at, signup_source, signup_page FROM subscribers "
            "WHERE id > ? ORDER BY id",
            (int(last_subscriber_id),)).fetchall()]
        targets = [dict(r) for r in conn.execute("""
            SELECT t.id, t.city, t.ulica, t.broj, t.created_at,
                   (t.email IS NOT NULL) AS has_email,
                   (t.telegram_chat_id IS NOT NULL) AS has_tg,
                   s.email AS owner
            FROM targets t LEFT JOIN subscribers s ON s.id = t.subscriber_id
            WHERE t.id > ? ORDER BY t.id
        """, (int(last_target_id),)).fetchall()]
        max_sub = _scalar(conn, "SELECT IFNULL(MAX(id), 0) FROM subscribers")
        max_tgt = _scalar(conn, "SELECT IFNULL(MAX(id), 0) FROM targets")
        return {"subscribers": subs, "targets": targets,
                "max_subscriber_id": max_sub, "max_target_id": max_tgt}
    finally:
        conn.close()


def format_new(feed: dict) -> str:
    """The instant "someone signed up" DM. Grouped by person, because a new subscriber almost
    always arrives together with their first address - listing the two separately read as two
    unrelated events. Empty string when there is nothing new to say."""
    subs, targets = feed["subscribers"], feed["targets"]
    if not subs and not targets:
        return ""

    new_emails = {sc["email"] for sc in subs if sc["email"]}
    # "izvor: google, stranica kraljevo" on a new account's line.
    came = {}
    for sc in subs:
        where = ", ".join(filter(None, (sc.get("signup_source"),
                                        sc.get("signup_page") and f"stranica {sc['signup_page']}")))
        if where:
            came[sc["email"]] = f" · izvor: {where}"
    by_owner: dict[str, list] = {}
    for t in targets:
        by_owner.setdefault(t["owner"] or "?", []).append(t)

    total = len(targets)
    head = "🆕 " + ("Nova prijava" if total == 1 and len(by_owner) == 1
                    else f"Nove prijave · {_adrese(total)}" if total
                    else "Nova prijava")
    lines = [head, ""]

    for owner, rows in by_owner.items():
        tag = " (nov)" if owner in new_emails else ""
        lines.append(f"👤 {owner}{tag}{came.get(owner, '') if tag else ''}")
        for t in rows:
            ch = _channels_label(t["has_email"], t["has_tg"])
            lines.append(f"📍 {t['city']}, {t['ulica']} {t['broj']} — {ch}")
        lines.append("")

    # Someone who confirmed their email but has no address yet - worth seeing, it usually means
    # they dropped out of the flow halfway.
    orphans = [sc for sc in subs if sc["email"] not in by_owner]
    for sc in orphans:
        lines.append(f"👤 {sc['email']} (nov){came.get(sc['email'], '')} — još bez adrese")

    return "\n".join(lines).rstrip()
