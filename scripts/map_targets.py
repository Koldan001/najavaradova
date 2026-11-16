import sys
import os

# Ensure root directory is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.database import get_connection
from spatial import spatial_engine

def map_unmapped_targets():
    print("🎯 Mapping registered target addresses to H3 Spatial Grid...")
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Ensure target_hexes table exists
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS target_hexes (
            target_id INTEGER NOT NULL,
            hex_id TEXT NOT NULL,
            PRIMARY KEY (target_id, hex_id),
            FOREIGN KEY (target_id) REFERENCES targets (id)
        )
    ''')

    # 2. Query targets without hex mapping
    cursor.execute('''
        SELECT t.id, t.city, t.opstina, t.naselje, t.ulica, t.broj
        FROM targets t
        LEFT JOIN target_hexes th ON t.id = th.target_id
        WHERE th.target_id IS NULL AND t.is_active = 1
    ''')
    unmapped = cursor.fetchall()

    if not unmapped:
        print("✅ All active targets are already mapped to the H3 spatial grid.")
        conn.close()
        return

    print(f"📍 Found {len(unmapped)} unmapped target addresses.")

    mapped_count = 0
    for target in unmapped:
        target_id = target['id']
        settlement = target['naselje'] or target['city']
        parent_region = target['opstina'] or target['city']
        street = target['ulica']

        print(f"  Resolving Target #{target_id}: {street}, {settlement}")

        # Reuse the exact same primitives core/matchmaker.py's calculate_outage_hexes() uses
        # for outages — this is the "one consistent logic path" for both sides of matching,
        # at the same H3 resolution (9) outage_hexes is built at, so the two can actually join.
        hexes = spatial_engine.get_street_hexes(
            street,
            settlement=settlement,
            parent_region=parent_region
        )

        if not hexes:
            # Street-level resolution failed (unknown/misspelled street, or settlement not
            # resolvable) — settlement-level coverage beats leaving the target unmapped and
            # silently never notified.
            print(f"  ⚠️ Could not resolve street '{street}' for target #{target_id}; falling back to settlement-level coverage.")
            hexes = spatial_engine.get_settlement_polygon_hexes(settlement, parent_region=parent_region)

        if hexes:
            for hex_id in hexes:
                cursor.execute('''
                    INSERT OR IGNORE INTO target_hexes (target_id, hex_id)
                    VALUES (?, ?)
                ''', (target_id, hex_id))
            mapped_count += 1
            print(f"  ✅ Target #{target_id} mapped -> {len(hexes)} hex(es)")
        else:
            print(f"  ❌ Could not resolve any location for Target #{target_id} — left unmapped.")
            # An address that isn't on the map never gets an alert, and nothing else would say
            # so (#94, 19.10.2026: "Belgrade" sat unmapped and silent). Tell the operator - once
            # a week per address while it stays unmapped. No email in the message.
            try:
                from core.alerts import send_operator_alert
                send_operator_alert(
                    f"unmapped_target:{target_id}",
                    f"📍 Adresa #{target_id} nije na mapi: {target['city']}, {street} {target['broj']}\n"
                    "Ovaj korisnik ne dobija obaveštenja dok se adresa ne ispravi.",
                    cooldown_s=7 * 24 * 3600)
            except Exception as e:
                print(f"  ⚠️ operator alert for target #{target_id} failed: {e}")

    conn.commit()
    conn.close()
    print(f"✅ Successfully mapped {mapped_count} target addresses into target_hexes!")

if __name__ == "__main__":
    map_unmapped_targets()
