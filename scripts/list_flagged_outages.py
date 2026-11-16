"""
Lists outages whose spatial matching needed a guess or failed outright — the review queue that
comes out of core/matchmaker.py's match_quality tracking (see the plan discussion). Run this
after scripts/reprocess_spatial.py to see what's worth a manual look.

Usage:
    python scripts/list_flagged_outages.py             # FALLBACK + UNRESOLVED (default)
    python scripts/list_flagged_outages.py --all        # include EXACT too, for a full picture
    python scripts/list_flagged_outages.py --quality UNRESOLVED
"""
import sys
import os
import argparse
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.database import get_connection

QUALITY_ORDER = {'UNRESOLVED': 0, 'FALLBACK': 1, 'EXACT': 2}


def list_flagged_outages(qualities: set[str]):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, region, affected_streets, raw_text, parsed_json, match_quality FROM outages")
    rows = cursor.fetchall()
    conn.close()

    matches = [r for r in rows if r['match_quality'] in qualities]
    matches.sort(key=lambda r: (QUALITY_ORDER.get(r['match_quality'], 9), r['id']))

    if not matches:
        print(f"✅ Nothing matching {sorted(qualities)} — nothing to review.")
        return

    print(f"Found {len(matches)} outage(s) with match_quality in {sorted(qualities)}:\n")
    for r in matches:
        print(f"--- #{r['id']} [{r['match_quality']}] {r['region']} ---")
        print(f"  Streets: {r['affected_streets']}")

        # Pinpoint which specific location(s) actually failed, not just "something in here did".
        if r['match_quality'] != 'EXACT' and r['parsed_json']:
            try:
                locations = json.loads(r['parsed_json']).get('affected_locations', [])
                names = [loc.get('main_street') or f"[{loc.get('location_type')}]" for loc in locations]
                print(f"  Locations in this outage: {', '.join(n for n in names if n)}")
            except Exception:
                pass

        print(f"  Raw text: {r['raw_text'][:150]}{'...' if len(r['raw_text']) > 150 else ''}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List outages flagged by match_quality for manual review.")
    parser.add_argument("--all", action="store_true", help="Include EXACT outages too (full picture)")
    parser.add_argument("--quality", choices=["EXACT", "FALLBACK", "UNRESOLVED"], help="Show only this one quality tier")
    args = parser.parse_args()

    if args.quality:
        qualities = {args.quality}
    elif args.all:
        qualities = {"EXACT", "FALLBACK", "UNRESOLVED"}
    else:
        qualities = {"FALLBACK", "UNRESOLVED"}

    list_flagged_outages(qualities)
