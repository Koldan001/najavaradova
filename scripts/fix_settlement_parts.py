"""Apply the "only part of the place" rule (core/matchmaker.py, PART_MARKER) to outages stored
before it: #4865, "Блок 8 у Петроварадину", was drawn and matched as all of Petrovaradin.

For each outage still on the map with a whole-settlement location:
- a spot (block, hamlet, centre, "kod" a building) or a town's "deo": that location alerts
  nobody any more (the other places and streets of the notice still do), and the map draws a
  dot where the place is instead of its whole outline;
- a village's "deo": nothing changes but the stored location, so a later message says
  "u delu naselja X".
No Gemini, no messages, no hexes: who is alerted is decided per location, not by hexes alone.

    docker compose run --rm api python -m scripts.fix_settlement_parts --dry-run
    docker compose run --rm api python -m scripts.fix_settlement_parts
"""
import argparse
import json
import os
import sqlite3
import sys

import h3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import get_connection
from core.matchmaker import (PART_MARKER, PART_TOWN_MARKER, OutageAlert, _area_label, _mark_settlement_parts, _settlement_part,
                             _whole_settlement_not_meant)
from spatial.spatial_engine import (cyrillic_to_latin, get_settlement_info,
                                    get_settlement_polygon_hexes, hexes_to_geojson_feature)


def fix(dry_run: bool) -> int:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, region, raw_text, parsed_json, match_quality, geojson_geometry, affected_streets "
        "FROM outages "
        "WHERE status = 'ACTIVE' AND (expires_at IS NULL OR expires_at > datetime('now')) "
        "AND parsed_json LIKE '%ENTIRE_SETTLEMENT%'").fetchall()
    changed = 0
    for row in rows:
        try:
            alert = OutageAlert.model_validate_json(row["parsed_json"])
        except Exception:
            continue
        _mark_settlement_parts(alert, row["raw_text"] or "")
        region = alert.city or row["region"] or "НОВИ САД"
        narrowed = [l for l in alert.affected_locations
                    if _settlement_part(l)[0] and _whole_settlement_not_meant(
                        l, region, get_settlement_polygon_hexes(l.settlement, parent_region=region))]
        for loc in narrowed:
            if (loc.landmark or "").strip() == PART_MARKER:
                loc.landmark = PART_TOWN_MARKER
        fc = json.loads(row["geojson_geometry"]) if row["geojson_geometry"] else None
        if narrowed:
            names = {cyrillic_to_latin(l.settlement).title() for l in narrowed}
            if fc:
                fc["features"] = [f for f in fc.get("features", [])
                                  if not ((f.get("properties") or {}).get("area_type") == "settlement"
                                          and (f.get("properties") or {}).get("settlement") in names)]
                have = {(f.get("properties") or {}).get("settlement") for f in fc["features"]
                        if (f.get("properties") or {}).get("area_type") == "spot"}
                for loc in narrowed:
                    if cyrillic_to_latin(loc.settlement).title() in have:
                        continue                               # already a dot: safe to run twice
                    centre, _r = get_settlement_info(loc.settlement, region)
                    dot = hexes_to_geojson_feature(set(h3.grid_disk(centre, 1)) if centre else set(), {
                        "kind": "area", "area_type": "spot",
                        "settlement": cyrillic_to_latin(loc.settlement).title()})
                    if dot:
                        fc["features"].append(dot)

        streets = row["affected_streets"]
        if not any(l.main_street for l in alert.affected_locations):
            streets = ", ".join(dict.fromkeys(
                _area_label(l) for l in alert.affected_locations
                if l.location_type == "ENTIRE_SETTLEMENT" and l.settlement)) or streets

        parsed = alert.model_dump_json()
        geometry = json.dumps(fc) if fc else row["geojson_geometry"]
        if (parsed, streets, geometry) == (row["parsed_json"], row["affected_streets"], row["geojson_geometry"]):
            continue

        labels = ", ".join(f"{l.settlement} [{l.landmark}]" for l in alert.affected_locations
                           if l.location_type == "ENTIRE_SETTLEMENT")
        print(f"#{row['id']} {row['region']}: {streets} | {'dot' if narrowed else 'outline'}; {labels}")
        changed += 1
        if not dry_run:
            conn.execute("UPDATE outages SET parsed_json = ?, affected_streets = ?, geojson_geometry = ? "
                         "WHERE id = ?", (parsed, streets, geometry, row["id"]))
    if not dry_run:
        conn.commit()
    conn.close()
    print(f"{'Would change' if dry_run else 'Changed'} {changed} of {len(rows)} outages.")
    return changed


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    fix(ap.parse_args().dry_run)
