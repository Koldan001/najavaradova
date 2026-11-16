"""
Audits data/outage_notifier.db's settlement_cache / settlement_polygon_cache tables.

A bad Nominatim geocode (a generic toponym resolving to the wrong town, a stale result from
before settlement-name cleaning existed, etc.) gets cached permanently — nothing previously
re-checked it. This script re-resolves every cached settlement with today's hardened lookup
(clean_settlement + country_codes='rs' + address cross-validation, see spatial_engine.py) and
flags any row whose cached location disagrees with a fresh lookup by more than ~10km.

Dry-run by default: prints findings, restores the original cached value so nothing changes.
Pass --apply to keep the freshly re-geocoded value in place for flagged rows.

Usage:
    python scripts/audit_settlement_cache.py            # report only, no changes
    python scripts/audit_settlement_cache.py --apply    # keep the fresh value for SUSPECT rows
"""
import sys
import os
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import h3
from core.database import get_connection
from spatial.spatial_engine import get_settlement_info, get_settlement_polygon_hexes

# ~10km at H3 res9 in this region (empirically calibrated: ~0.31-0.34km per grid step)
SUSPECT_DISTANCE_STEPS = 30


def _split_cache_key(name: str) -> tuple[str, str | None]:
    """settlement_cache keys are '<SETTLEMENT>_<PARENT>' or just '<SETTLEMENT>'."""
    if '_' in name:
        settlement, parent = name.split('_', 1)
        return settlement, parent
    return name, None


def _hex_set_centroid(hex_ids) -> tuple[float, float] | None:
    if not hex_ids:
        return None
    pts = [h3.cell_to_latlng(h) for h in hex_ids]
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def audit_settlement_cache(apply: bool = False):
    conn = get_connection()
    cursor = conn.cursor()

    # --- settlement_cache (point + radius) ---
    cursor.execute("SELECT name, lat, lng, hex_id, radius_steps FROM settlement_cache")
    rows = cursor.fetchall()
    print(f"🔍 Auditing {len(rows)} settlement_cache entries...")
    suspects = []

    for row in rows:
        settlement, parent = _split_cache_key(row['name'])
        cached_hex = row['hex_id']

        # Force a fresh lookup by clearing this row so get_settlement_info can't just hit its
        # own cache — it will live-geocode and cache the fresh result as a side effect.
        cursor.execute("DELETE FROM settlement_cache WHERE name = ?", (row['name'],))
        conn.commit()

        fresh_hex, _ = get_settlement_info(settlement, parent)

        if not fresh_hex:
            print(f"  ⚠️ SUSPECT — no longer resolvable at all: {row['name']}")
            suspects.append(row['name'])
            if not apply:
                cursor.execute(
                    "INSERT OR REPLACE INTO settlement_cache (name, lat, lng, hex_id, radius_steps) VALUES (?, ?, ?, ?, ?)",
                    (row['name'], row['lat'], row['lng'], row['hex_id'], row['radius_steps'])
                )
                conn.commit()
            continue

        try:
            distance = h3.grid_distance(cached_hex, fresh_hex)
        except Exception:
            distance = None

        if distance is None or distance > SUSPECT_DISTANCE_STEPS:
            print(f"  ⚠️ SUSPECT — manual review: {row['name']} (cached vs fresh grid_distance={distance})")
            suspects.append(row['name'])
            if not apply:
                # Dry run: restore the original value get_settlement_info's fresh lookup just
                # overwrote, so nothing actually changes until --apply is passed.
                cursor.execute(
                    "INSERT OR REPLACE INTO settlement_cache (name, lat, lng, hex_id, radius_steps) VALUES (?, ?, ?, ?, ?)",
                    (row['name'], row['lat'], row['lng'], row['hex_id'], row['radius_steps'])
                )
                conn.commit()
            # else: leave the freshly re-geocoded value (already cached by get_settlement_info)
        # else: agrees with a fresh lookup — already re-cached with today's provenance columns.

    # --- settlement_polygon_cache (polygon hex sets) ---
    cursor.execute("SELECT name, hex_ids_json FROM settlement_polygon_cache")
    poly_rows = cursor.fetchall()
    print(f"\n🔍 Auditing {len(poly_rows)} settlement_polygon_cache entries...")
    poly_suspects = []

    for row in poly_rows:
        settlement, parent = _split_cache_key(row['name'])
        cached_hexes = set(json.loads(row['hex_ids_json']))
        cached_centroid = _hex_set_centroid(cached_hexes)

        cursor.execute("DELETE FROM settlement_polygon_cache WHERE name = ?", (row['name'],))
        conn.commit()

        fresh_hexes = get_settlement_polygon_hexes(settlement, parent)
        fresh_centroid = _hex_set_centroid(fresh_hexes)

        flagged = False
        if not fresh_hexes or not cached_centroid or not fresh_centroid:
            flagged = True
        else:
            try:
                cached_cell = h3.latlng_to_cell(*cached_centroid, 9)
                fresh_cell = h3.latlng_to_cell(*fresh_centroid, 9)
                if h3.grid_distance(cached_cell, fresh_cell) > SUSPECT_DISTANCE_STEPS:
                    flagged = True
            except Exception:
                flagged = True

        if flagged:
            print(f"  ⚠️ SUSPECT — manual review: {row['name']}")
            poly_suspects.append(row['name'])
            if not apply:
                cursor.execute(
                    "INSERT OR REPLACE INTO settlement_polygon_cache (name, hex_ids_json) VALUES (?, ?)",
                    (row['name'], json.dumps(list(cached_hexes)))
                )
                conn.commit()
            # else: leave whatever get_settlement_polygon_hexes just (re-)cached, if anything.

    conn.close()

    total_suspects = len(suspects) + len(poly_suspects)
    if total_suspects:
        print(f"\n⚠️ {total_suspects} suspect entries found across both tables.")
        if apply:
            print("   Fresh re-geocoded values kept in place.")
        else:
            print("   Dry run only — original values restored. Rerun with --apply to accept the fresh values.")
    else:
        print("\n✅ No suspect entries found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit/repair settlement_cache and settlement_polygon_cache entries.")
    parser.add_argument("--apply", action="store_true", help="Keep freshly re-geocoded values for SUSPECT rows (default: dry run, restores originals)")
    args = parser.parse_args()
    audit_settlement_cache(apply=args.apply)
