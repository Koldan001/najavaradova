"""End-to-end notification test - no waiting for a real scrape or for "tomorrow".

Two things it proves, both against the REAL database and the REAL Telegram send path:

  1. A signup for an address in an outage that is ALREADY stored + ACTIVE gets notified
     immediately (the registration backfill), not only on some future re-scrape.
  2. The next day's re-scrape of that same outage does NOT ping the same person again
     (dedup-on-update), simulated by re-running process_and_queue_outage() with the stored
     parsed alert and a fresh text hash.

Usage (run inside the container so it sees the same DB + .env):

    docker compose run --rm api python scripts/test_notification_e2e.py \
        --chat-id 123456789 --send

    # then, to simulate tomorrow's re-scrape and confirm no second ping:
    docker compose run --rm api python scripts/test_notification_e2e.py \
        --chat-id 123456789 --dedup-test --outage-id 512

    # remove the test target and everything it queued:
    docker compose run --rm api python scripts/test_notification_e2e.py \
        --chat-id 123456789 --cleanup

--chat-id is your own Telegram numeric id (message @userinfobot to get it). Without --send the
message is only queued (the `sender` container would deliver it within ~60s); with --send this
script calls the worker inline so it arrives now.
"""
import argparse
import hashlib
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import get_connection
from core.matchmaker import backfill_target_against_active_outages, process_and_queue_outage
from parser import OutageAlert, parse_announcement

# Real VIK Sremska Mitrovica announcement address - "Ул. Деспота Бранковића од бр. 1-19 ...".
DEFAULT_CITY = "СРЕМСКА МИТРОВИЦА"
DEFAULT_STREET = "Деспота Бранковића"
DEFAULT_BROJ = "5"  # odd, inside the announced 1-19 range


def get_or_create_target(chat_id: str, city: str, street: str, broj: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM targets WHERE telegram_chat_id = ?", (str(chat_id),)
    ).fetchone()
    if row:
        target_id = row["id"]
        cur.execute(
            "UPDATE targets SET city = ?, ulica = ?, broj = ?, is_active = 1 WHERE id = ?",
            (city, street, broj, target_id),
        )
        print(f"↻ Reusing target #{target_id} (chat_id={chat_id}), address reset to test values.")
    else:
        cur.execute(
            "INSERT INTO targets (telegram_chat_id, telegram_username, city, ulica, broj) "
            "VALUES (?, 'e2e-test', ?, ?, ?)",
            (str(chat_id), city, street, broj),
        )
        target_id = cur.lastrowid
        print(f"＋ Created target #{target_id} (chat_id={chat_id}) for {street} {broj}, {city}.")
    conn.commit()
    conn.close()
    return target_id


def show_queue_for_target(target_id: int):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT mq.id, mq.outage_id, mq.status, mq.custom_message, o.raw_text, o.time_window
        FROM message_queue mq JOIN outages o ON o.id = mq.outage_id
        WHERE mq.target_id = ? ORDER BY mq.id
        """,
        (target_id,),
    ).fetchall()
    conn.close()
    print(f"\n📬 message_queue rows for target #{target_id}: {len(rows)}")
    for r in rows:
        msg = r["custom_message"] or f"(raw) {r['raw_text'][:120]}... [{r['time_window']}]"
        print(f"   queue#{r['id']} outage#{r['outage_id']} [{r['status']}] {msg}")
    return rows


def run_backfill(target_id: int):
    print("\n=== 1. Backfill new target against ACTIVE outages ===")
    summary = backfill_target_against_active_outages(target_id)
    if summary["queued"] == 0 and summary["candidate_outages"] == 0:
        print(
            "\n⚠️  No ACTIVE outage overlaps this address. Either no matching outage is currently "
            "stored, or the street didn't resolve. Pick an address that IS in a live outage, or "
            "check spatial resolution for this street."
        )
    return summary


def send_now():
    print("\n=== 2. Deliver queued Telegram messages now (inline worker run) ===")
    from workers.telegram_worker import run_worker
    run_worker()


def dedup_test(target_id: int, outage_id: int):
    print(f"\n=== Dedup-on-update test against outage #{outage_id} ===")
    conn = get_connection()
    o = conn.execute(
        "SELECT id, raw_text, parsed_json, category, source_url FROM outages WHERE id = ?",
        (outage_id,),
    ).fetchone()
    conn.close()
    if not o:
        print(f"❌ outage #{outage_id} not found.")
        return
    if not o["parsed_json"]:
        print(
            f"❌ outage #{outage_id} has no parsed_json (scraped before that column existed). "
            "Use an outage scraped after this deploy, or trigger a re-scrape of its source first."
        )
        return

    def count_rows():
        c = get_connection()
        n = c.execute(
            "SELECT COUNT(*) AS n FROM message_queue WHERE target_id = ? AND outage_id = ?",
            (target_id, outage_id),
        ).fetchone()["n"]
        c.close()
        return n

    before = count_rows()
    print(f"message_queue rows for (target #{target_id}, outage #{outage_id}) before: {before}")
    if before == 0:
        print("   (target isn't queued for this outage yet - run without --dedup-test first.)")

    alert = OutageAlert.model_validate_json(o["parsed_json"])
    # Simulate the utility editing the post overnight: same event, different bytes -> new hash,
    # so raw_text_hash dedup can't catch it and the (region, date, category, primary-street)
    # correlation path is what has to recognise it as the same outage.
    edited_text = (o["raw_text"] or "") + "  ​[re-scrape simul/ same event]"
    new_hash = hashlib.sha256(edited_text.encode("utf-8")).hexdigest()
    print("Re-running process_and_queue_outage() with the stored alert + a fresh text hash...")
    process_and_queue_outage(
        alert, edited_text, text_hash=new_hash,
        category=o["category"], source_url=o["source_url"],
    )

    after = count_rows()
    print(f"message_queue rows for (target #{target_id}, outage #{outage_id}) after:  {after}")
    if after == before:
        print("✅ PASS - no second notification queued for the already-notified target.")
    else:
        print(f"❌ FAIL - {after - before} extra message(s) queued. Dedup-on-update did not hold.")


def reparse(outage_id: int):
    """Re-parse one outage's raw_text via Gemini and re-run process_and_queue_outage so the row
    gains parsed_json (and thus the clean templated message path). One real Gemini call. Used to
    upgrade a pre-parsed_json row in place without depending on the live source page still
    carrying it - EPS's day-rotating pages in particular won't for long."""
    conn = get_connection()
    o = conn.execute(
        "SELECT id, raw_text, category, source_url, parsed_json FROM outages WHERE id = ?",
        (outage_id,),
    ).fetchone()
    conn.close()
    if not o:
        print(f"❌ outage #{outage_id} not found.")
        return
    if o["parsed_json"]:
        print(f"ℹ️ outage #{outage_id} already has parsed_json - nothing to do.")
        return
    print(f"Re-parsing outage #{outage_id} raw_text via Gemini...")
    alert = parse_announcement(o["raw_text"])
    if not alert:
        print("❌ parse returned nothing (quota or transient). Try again shortly.")
        return
    new_hash = hashlib.sha256((o["raw_text"] or "").encode("utf-8")).hexdigest() + "-reparse"
    process_and_queue_outage(
        alert, o["raw_text"], text_hash=new_hash,
        category=o["category"], source_url=o["source_url"],
    )
    print(f"✅ outage #{outage_id} reprocessed - parsed_json stored. Now run --cleanup then --send.")


def cleanup(chat_id: str):
    conn = get_connection()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM targets WHERE telegram_chat_id = ?", (str(chat_id),)
    ).fetchone()
    if not row:
        print("Nothing to clean up - no target with that chat_id.")
        conn.close()
        return
    tid = row["id"]
    cur.execute("DELETE FROM message_queue WHERE target_id = ?", (tid,))
    cur.execute("DELETE FROM target_hexes WHERE target_id = ?", (tid,))
    cur.execute("DELETE FROM targets WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    print(f"🧹 Removed test target #{tid}, its hexes, and its queued messages.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chat-id", required=True, help="your Telegram numeric chat id")
    ap.add_argument("--city", default=DEFAULT_CITY)
    ap.add_argument("--street", default=DEFAULT_STREET)
    ap.add_argument("--broj", default=DEFAULT_BROJ)
    ap.add_argument("--send", action="store_true", help="call the Telegram worker inline so the ping arrives now")
    ap.add_argument("--dedup-test", action="store_true", help="simulate tomorrow's re-scrape and assert no second ping")
    ap.add_argument("--reparse", action="store_true", help="re-parse --outage-id via Gemini so it gains parsed_json")
    ap.add_argument("--outage-id", type=int, help="outage id for --dedup-test / --reparse")
    ap.add_argument("--cleanup", action="store_true", help="delete the test target and exit")
    args = ap.parse_args()

    if args.cleanup:
        cleanup(args.chat_id)
        return

    if args.reparse:
        if not args.outage_id:
            ap.error("--reparse needs --outage-id")
        reparse(args.outage_id)
        return

    target_id = get_or_create_target(args.chat_id, args.city, args.street, args.broj)

    if args.dedup_test:
        if not args.outage_id:
            ap.error("--dedup-test needs --outage-id")
        dedup_test(target_id, args.outage_id)
        return

    run_backfill(target_id)
    rows = show_queue_for_target(target_id)
    if args.send and rows:
        send_now()
        show_queue_for_target(target_id)
    elif rows:
        print("\n(Queued only. The `sender` container delivers within ~60s, or re-run with --send.)")


if __name__ == "__main__":
    main()
