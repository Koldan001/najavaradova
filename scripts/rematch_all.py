"""Re-run the registration backfill for every active target against every currently-active
outage. Use it after a matching-logic fix to catch people who should already have been notified
but weren't (e.g. the Latin-city vs Cyrillic-region scope bug).

Safe to run any time and more than once: backfill_target_against_active_outages only queues a
(target, channel, outage) combination that isn't already in message_queue, so it can't
double-notify anyone. It does NOT send - the workers drain message_queue on their own 60s loop.

    docker compose run --rm api python -m scripts.rematch_all --dry-run   # what it WOULD queue
    docker compose run --rm api python -m scripts.rematch_all
    docker compose run --rm api python -m scripts.rematch_all --target 42     # just one
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import get_connection
from core.matchmaker import backfill_target_against_active_outages


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=None, help="only this target id")
    ap.add_argument("--dry-run", action="store_true", help="count what would be queued, queue nothing")
    args = ap.parse_args(argv)

    conn = get_connection()
    if args.target:
        ids = [args.target]
    else:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM targets WHERE is_active = 1 ORDER BY id"
        ).fetchall()]
    conn.close()

    print(f"Re-matching {len(ids)} active target(s){' (DRY RUN - nothing is queued)' if args.dry_run else ''}...")
    totals = {"mapped_hexes": 0, "candidate_outages": 0, "queued": 0, "skipped": 0}
    newly_queued = []
    for tid in ids:
        try:
            r = backfill_target_against_active_outages(tid, dry_run=args.dry_run)
        except Exception as e:
            print(f"  target #{tid}: ERROR {e!r}")
            continue
        for k in totals:
            totals[k] += r.get(k, 0)
        if r.get("queued"):
            newly_queued.append((tid, r["queued"]))
            print(f"  target #{tid}: +{r['queued']} {'would be ' if args.dry_run else ''}queued  ({r})")

    print("\nDone.")
    print(f"  targets with new messages: {len(newly_queued)}")
    print(f"  totals: {totals}")
    if newly_queued and args.dry_run:
        print("  -> dry run: nothing was queued. Run again without --dry-run to queue them.")
    elif newly_queued:
        print("  -> the sender / email_sender workers will deliver these within ~60s")


if __name__ == "__main__":
    main()
