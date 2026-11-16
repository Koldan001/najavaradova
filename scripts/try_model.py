"""Try a candidate Gemini model as the second reading (core/alert_check.py) before switching to it.

Takes the most recent outages that alerted someone, has MODEL read each notice, and judges every
alerted person exactly like the live check does. Prints how often it agrees, next to the verdicts
the live check already stored for the same alerts. Writes nothing.

Spends one request per outage (more on errors) of MODEL's own daily quota - 10 outages is half of
a 20-a-day free model. Only notice text goes to the model, never an address; the output shows
outage ids and verdict reasons only.

    python scripts/try_model.py gemini-3.8-flash          # 10 recent outages
    python scripts/try_model.py gemini-3.8-flash -n 5
    python scripts/try_model.py --list                    # model names the API offers
"""
import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.alert_check import NEARBY_MARK, judge_outage
from core.database import get_connection


def recent_alerts(n: int) -> dict[int, list[int]]:
    """The n newest outages with alerts on their own street (not "u blizini"), and who got them."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT outage_id, target_id, custom_message FROM message_queue
            WHERE status IN ('QUEUED', 'SENDING', 'SENT', 'HELD')
              AND created_at >= datetime('now', '-14 days')
            ORDER BY outage_id DESC""").fetchall()
    finally:
        conn.close()
    picked: dict[int, list[int]] = {}
    for r in rows:
        if NEARBY_MARK in (r["custom_message"] or ""):
            continue
        if r["outage_id"] not in picked and len(picked) >= n:
            continue
        picked.setdefault(r["outage_id"], []).append(r["target_id"])
    return picked


def stored_verdicts(outage_ids) -> dict[tuple[int, int], tuple[str, str]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT outage_id, target_id, verdict, model FROM alert_checks "
            f"WHERE outage_id IN ({','.join('?' * len(outage_ids))})", list(outage_ids)).fetchall()
    finally:
        conn.close()
    return {(r["outage_id"], r["target_id"]): (r["verdict"], r["model"] or "-") for r in rows}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", nargs="?")
    ap.add_argument("-n", type=int, default=10, help="how many recent outages (default 10)")
    ap.add_argument("--list", action="store_true", help="list the Gemini model names the API offers")
    args = ap.parse_args()

    if args.list:
        from parser import client
        for m in client.models.list():
            print(m.name.removeprefix("models/"))
        return
    if not args.model:
        ap.error("give a model name, or --list")

    picked = recent_alerts(args.n)
    if not picked:
        print("No alerts in the last 14 days.")
        return
    before = stored_verdicts(picked)
    ours, theirs = Counter(), Counter()
    for oid, tids in picked.items():
        t0 = time.monotonic()
        rows = judge_outage(oid, tids, model=args.model, tries=1)
        took = time.monotonic() - t0
        for _, tid, verdict, reason, _ in rows:
            ours[verdict] += 1
            old, old_model = before.get((oid, tid), ("not checked", "-"))
            theirs[old] += 1
            line = f"#{oid} target {tid}: {verdict:<8} (stored: {old} by {old_model})  {took:.1f}s"
            print(line + (f"  - {reason}" if reason and verdict != "AGREE" else ""))

    def share(c):
        judged = c["AGREE"] + c["DISAGREE"]
        return f"{c['AGREE']}/{judged} agree" + (f" ({100 * c['AGREE'] // judged}%)" if judged else "")

    print()
    print(f"{args.model}: {share(ours)}, errors {ours['ERROR']}")
    print(f"stored live check on the same alerts: {share(theirs)}, errors {theirs['ERROR']}, "
          f"not checked {theirs['not checked']}")


if __name__ == "__main__":
    main()
