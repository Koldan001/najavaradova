"""Bulk-onboard many addresses to one contact - the tool for a building manager (upravnik) who
wants outage alerts for every address in their portfolio without clicking through the web form
one address at a time. Rows added here are flagged targets.manager_list, so the person gets the
tenant-notice layout (core/notice.py). Reuses exactly the same validation, dedup, and mapping/backfill path the
web form and Telegram bot use, so a bulk-imported target behaves identically to one someone
registered by hand.

CSV format (comma-separated, UTF-8, header required):
    city,ulica,broj
    Novi Sad,Futoska,10
    Novi Sad,Bulevar oslobodjenja,45

Usage:
    python -m scripts.bulk_register --telegram-chat-id 123456789 addresses.csv
    python -m scripts.bulk_register --email upravnik@example.com addresses.csv
    python -m scripts.bulk_register --telegram-chat-id 123456789 addresses.csv --dry-run
    python -m scripts.bulk_register --telegram-chat-id 123456789 addresses.csv --allow-uncovered
    python -m scripts.bulk_register --email upravnik@example.com addresses.csv --ref zgradahub

Every row is checked against the same coverage rule /register enforces (see
scripts/local_web_server.py's check_address): a row for a city with no RGZ street
data is skipped by default (--allow-uncovered inserts it anyway - equivalent to a visitor ticking
the "I understand this isn't covered yet" box on the site), and a row whose street isn't
recognised in a covered city is skipped and reported, never silently guessed.
"""
import argparse
import csv
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import secrets

from core import auth, partners
from core.database import get_connection
from core.matchmaker import ensure_target_hexes, backfill_target_against_active_outages
from scripts.local_web_server import check_address, valid_broj


def _dup_exists(conn, channel, contact, city, ulica, broj):
    if channel == "telegram":
        return conn.execute(
            "SELECT 1 FROM targets WHERE is_active = 1 AND telegram_chat_id = ? "
            "AND UPPER(city) = UPPER(?) AND UPPER(ulica) = UPPER(?) AND broj = ?",
            (contact, city, ulica, broj),
        ).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM targets WHERE is_active = 1 AND LOWER(email) = LOWER(?) "
        "AND UPPER(city) = UPPER(?) AND UPPER(ulica) = UPPER(?) AND broj = ?",
        (contact, city, ulica, broj),
    ).fetchone() is not None


def _insert_target(conn, channel, contact, city, ulica, broj, subscriber_id=None):
    if channel == "telegram":
        cur = conn.execute(
            "INSERT INTO targets (city, ulica, broj, telegram_chat_id, manager_list) VALUES (?, ?, ?, ?, 1)",
            (city, ulica, broj, contact),
        )
    else:
        # subscriber_id, not just the email column, is what the dashboard's /api/me actually
        # filters on (scripts/local_web_server.py) - without it a bulk-imported address sends
        # notifications fine but silently never shows up when the manager logs in. Same
        # find-or-create as a real signup (core.auth._find_or_create_subscriber), so an address
        # added here for an email that already has an account lands in that same account instead
        # of orphaning a second, invisible one.
        cur = conn.execute(
            "INSERT INTO targets (city, ulica, broj, email, unsub_token, subscriber_id, manager_list) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (city, ulica, broj, contact, secrets.token_urlsafe(24), subscriber_id),
        )
    return cur.lastrowid


def _apply_ref(conn, subscriber_id, ref):
    """Operator-assisted signup that came through a partner. Same first-touch rule as the web
    signup (core/auth.py): set only while the account has no partner yet, never overwritten."""
    cur = conn.execute(
        "UPDATE subscribers SET referred_by = ? WHERE id = ? AND referred_by IS NULL",
        (ref, subscriber_id),
    )
    if cur.rowcount:
        print(f"🤝 Nalog pripisan partneru '{ref}'.")
    else:
        current = conn.execute(
            "SELECT referred_by FROM subscribers WHERE id = ?", (subscriber_id,)
        ).fetchone()["referred_by"]
        print(f"ℹ️ Nalog je već pripisan partneru '{current}' - --ref nije promenjen.")


def run(csv_path, channel, contact, allow_uncovered=False, dry_run=False, ref=None):
    if ref is not None:
        # Referrals live on the account (subscribers.referred_by); a Telegram-only bulk import
        # creates no account, so there'd be nothing to attribute.
        if channel != "email":
            print("❌ --ref radi samo uz --email (partner se pripisuje nalogu).")
            return
        check = get_connection()
        known = partners.known_partner(check, ref)
        check.close()
        if not known:
            # Unlike the web form (silently ignores unknown refs), a typo here is the operator's
            # own - fail loudly rather than quietly losing the attribution.
            print(f"❌ Nepoznat partner '{ref}' - dodaj ga sa: python -m scripts.partners add ...")
            return
        ref = known

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = {"city", "ulica", "broj"} - set(h.strip().lower() for h in (reader.fieldnames or []))
        if missing:
            print(f"❌ CSV is missing column(s): {', '.join(sorted(missing))}. "
                  f"Header must be: city,ulica,broj")
            return
        rows = list(reader)

    conn = get_connection()
    counts = {"ok": 0, "duplicate": 0, "uncovered": 0, "bad_street": 0, "bad_broj": 0}

    # Resolved once up front, not inside the row loop or dry-run branch - dry-run must report
    # (not create) accounts, and every email-channel row in the file shares the same contact.
    subscriber_id = None
    if channel == "email" and not dry_run:
        subscriber_id = auth._find_or_create_subscriber(conn, contact, source=("rucno", None))
        if ref:
            _apply_ref(conn, subscriber_id, ref)
        conn.commit()

    for i, row in enumerate(rows, start=2):  # start=2: row 1 is the header, matches a spreadsheet's line number
        city = (row.get("city") or "").strip()
        ulica = (row.get("ulica") or "").strip()
        # Uppercased same as the web form now does - "8a"/"bb" are real addresses (see
        # local_web_server.py's valid_broj), and casing must be consistent for dedup/matching.
        broj = (row.get("broj") or "").strip().upper()
        label = f"[{i}] {city} / {ulica} {broj}"

        if not city or not ulica or not broj:
            print(f"{label}: ❌ prazno polje - preskočeno")
            counts["bad_broj"] += 1
            continue
        if not valid_broj(broj):
            print(f"{label}: ❌ neispravan broj (npr. 12, 12a ili bb) - preskočeno")
            counts["bad_broj"] += 1
            continue

        city, ulica, err = check_address(city, ulica, broj, allow_uncovered)
        if err and err.get("code") == "UNCOVERED":
            print(f"{label}: ⚠️ NEPOKRIVENO (nema ulica u RGZ bazi) - preskočeno "
                  f"(--allow-uncovered da ipak dodaš)")
            counts["uncovered"] += 1
            continue
        if err:
            print(f"{label}: ❌ {err['error']} - preskočeno")
            counts["bad_street"] += 1
            continue

        if _dup_exists(conn, channel, contact, city, ulica, broj):
            print(f"{label}: ⏭️ već registrovano za ovaj kontakt - preskočeno")
            counts["duplicate"] += 1
            continue

        if dry_run:
            tag = "✅ (dry-run, nije upisano)" if tier != "none" else "⚠️ (dry-run, nepokriveno, nije upisano)"
            print(f"{label}: {tag}")
            counts["ok"] += 1
            continue

        target_id = _insert_target(conn, channel, contact, city, ulica, broj, subscriber_id)
        conn.commit()

        # Same inline map + backfill every other registration path uses (workers/telegram_bot.py,
        # scripts/local_web_server.py) - never fatal, a target with no hexes yet still gets
        # picked up by main.py's map_unmapped_targets() on the next scrape cycle.
        summary = {"queued": 0}
        try:
            cursor = conn.cursor()
            row_full = conn.execute(
                "SELECT id, city, opstina, naselje, ulica, broj FROM targets WHERE id = ?",
                (target_id,),
            ).fetchone()
            ensure_target_hexes(cursor, row_full)
            conn.commit()
            summary = backfill_target_against_active_outages(target_id)
        except Exception as e:
            print(f"{label}: ⚠️ mapiranje/backfill nije uspelo ({e}) - dodato je, mapiraće se na sledećem ciklusu")

        print(f"{label}: ✅ dodato (id={target_id}, {summary.get('queued', 0)} postojećih prekida odmah u redu za slanje)")
        counts["ok"] += 1

    conn.close()
    print(
        f"\n🎉 Gotovo. {counts['ok']} uspešno, {counts['duplicate']} duplikat, "
        f"{counts['uncovered']} nepokriveno, {counts['bad_street']} nepoznata ulica, "
        f"{counts['bad_broj']} loš unos."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    contact = parser.add_mutually_exclusive_group(required=True)
    contact.add_argument("--telegram-chat-id", type=str, help="Numeric Telegram chat id (from @userinfobot).")
    contact.add_argument("--email", type=str, help="Email address to attach every address to.")
    parser.add_argument("csv_path", help="CSV file with a city,ulica,broj header.")
    parser.add_argument("--allow-uncovered", action="store_true",
                         help="Insert rows for a city with no RGZ street data instead of skipping them "
                              "(equivalent to the web form's 'I understand this isn't covered yet' checkbox).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Validate and report only - no rows written.")
    parser.add_argument("--ref", type=str,
                         help="Partner slug this manager came through (see scripts/partners.py). "
                              "Email only; never overwrites an existing attribution.")
    args = parser.parse_args()

    channel = "telegram" if args.telegram_chat_id else "email"
    contact_value = args.telegram_chat_id or args.email
    run(args.csv_path, channel, contact_value, allow_uncovered=args.allow_uncovered,
        dry_run=args.dry_run, ref=args.ref)


if __name__ == "__main__":
    main()
