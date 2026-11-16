import argparse
import sys
import os
import sqlite3
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.database import get_connection
from core.matchmaker import calculate_outage_hexes
from parser import parse_announcement
from spatial.spatial_engine import get_street_geojson_feature
from spatial.visualize import visualize_database_outages

def reprocess_existing_geometries(only_id=None, active_only=False, allow_parse=False):
    # PHASE 1: Fetch all outage records into memory and close connection immediately
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if only_id is not None:
        cursor.execute("SELECT * FROM outages WHERE id = ?", (only_id,))
    elif active_only:
        cursor.execute("SELECT * FROM outages WHERE status = 'ACTIVE'")
    else:
        cursor.execute("SELECT * FROM outages")
    raw_outages = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if only_id is not None and not raw_outages:
        print(f"⚠️ No outage with id {only_id} — nothing to do.")
        return

    # The parse fallback below is the only thing here that calls the LLM parser (Gemini free
    # tier, 500 req/day). It is OFF unless --allow-parse is passed, so a routine geometry
    # reprocess can never exhaust the daily quota — outages with no cached parsed_json are
    # skipped instead.
    skipped_unparsed = 0

    print(f"🔄 Re-processing geometry for {len(raw_outages)} cached outages"
          f"{' (parse fallback ENABLED)' if allow_parse else ''}...")

    processed_results = []
    updated_count = 0

    # PHASE 2: Compute all geometries and parsing in memory
    # spatial_engine can now write to settlement_cache without DB lock conflicts
    for outage in raw_outages:
        outage_id = outage['id']
        region = outage['region']
        raw_text = outage['raw_text']
        
        parsed_data = {}
        if outage.get('parsed_json'):
            try:
                parsed_data = json.loads(outage['parsed_json'])
            except Exception:
                pass

        if not parsed_data and raw_text and allow_parse:
            parsed_alert = parse_announcement(raw_text)
            # parse_announcement returns a validated OutageAlert (pydantic model), not a dict —
            # normalize here since everything below (and the parsed_json column itself) expects
            # a plain dict. This path was previously dead code (every existing outage already
            # had cached parsed_json), so the mismatch never surfaced until now.
            parsed_data = parsed_alert.model_dump() if parsed_alert else {}

        if not parsed_data:
            if not allow_parse and raw_text:
                skipped_unparsed += 1
            continue

        affected_locations = parsed_data.get('affected_locations', [])
        features = []
        active_settlement = region

        # Visual line features for the map — street-level locations only. An ENTIRE_SETTLEMENT
        # location has no line geometry to draw (visualize.py already documents this: whole-
        # settlement outages intentionally render with no street line), but it still counts for
        # matching/hex coverage below via calculate_outage_hexes.
        for loc in affected_locations:
            if not isinstance(loc, dict):
                continue

            if loc.get('settlement'):
                active_settlement = loc.get('settlement')

            main_st = loc.get('main_street')
            start_st = loc.get('intersection_start')
            end_st = loc.get('intersection_end')
            settlement = active_settlement

            if main_st:
                feat = get_street_geojson_feature(main_st, settlement, region, start_st, end_st)
                if feat:
                    features.append(feat)

        # Reuse the exact same hex-computation core/matchmaker.py's process_and_queue_outage()
        # uses at live-scrape time — this is what actually handles ENTIRE_SETTLEMENT coverage,
        # the small-settlement fallback, and match_quality tracking. Previously this script had
        # its own separate, drifted, incomplete copy of this logic (no ENTIRE_SETTLEMENT handling
        # at all), so what you saw here on the map could disagree with what real notifications
        # would actually do.
        all_hexes, match_quality = calculate_outage_hexes(parsed_data)

        geojson_str = json.dumps({"type": "FeatureCollection", "features": features}) if features else None

        if features or all_hexes:
            updated_count += 1

        processed_results.append({
            'id': outage_id,
            'parsed_json': json.dumps(parsed_data),
            'geojson_geometry': geojson_str,
            'match_quality': match_quality,
            'hexes': all_hexes
        })

    # PHASE 3: Open database once and batch update all changes in a single transaction
    conn = get_connection()
    try:
        with conn:
            for item in processed_results:
                outage_id = item['id']
                
                conn.execute("UPDATE outages SET parsed_json = ?, geojson_geometry = ?, match_quality = ? WHERE id = ?",
                             (item['parsed_json'], item['geojson_geometry'], item['match_quality'], outage_id))
                
                conn.execute("DELETE FROM outage_hexes WHERE outage_id = ?", (outage_id,))
                
                for h in item['hexes']:
                    conn.execute("INSERT OR IGNORE INTO outage_hexes (outage_id, hex_id) VALUES (?, ?)", (outage_id, h))
    finally:
        conn.close()

    print(f"✅ Reprocessing complete! {updated_count}/{len(raw_outages)} outages generated valid map geometries.")
    if skipped_unparsed:
        print(f"   skipped {skipped_unparsed} outage(s) with no cached parsed_json "
              f"(re-run with --allow-parse to LLM-parse them; costs Gemini quota).")

    quality_counts = {}
    for item in processed_results:
        quality_counts[item['match_quality']] = quality_counts.get(item['match_quality'], 0) + 1
    if quality_counts.get('FALLBACK') or quality_counts.get('UNRESOLVED'):
        print(f"   match_quality: {quality_counts} — FALLBACK/UNRESOLVED outages are worth a manual look.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Recompute map geometry / hexes / match_quality for stored outages. "
                    "Read/compute only — never sends notifications or touches message_queue."
    )
    ap.add_argument("--id", type=int, default=None, help="only reprocess this outage id")
    ap.add_argument("--active", action="store_true",
                    help="only outages with status = 'ACTIVE' (skip expired history)")
    ap.add_argument("--allow-parse", action="store_true",
                    help="LLM-parse outages missing a cached parsed_json (costs Gemini "
                         "free-tier quota, 500 req/day). Off by default; unparsed rows are skipped.")
    args = ap.parse_args()
    reprocess_existing_geometries(only_id=args.id, active_only=args.active,
                                  allow_parse=args.allow_parse)
    visualize_database_outages()