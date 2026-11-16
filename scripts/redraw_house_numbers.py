"""Redraw the outages still on the map whose announcement names house numbers: the stored street
line becomes the stretch those houses stand on, or a pin for one house. New outages are drawn that
way when they're scraped (core/matchmaker.py's _house_number_feature); this does the same for the
ones stored before it.

Only the drawing changes. It works on each row's stored lines, so the leak gate they already
passed still holds, and it touches nothing else: no hexes, no messages, no Gemini. Safe to run
more than once - an already redrawn line is left as it is.

    docker compose run --rm api python -m scripts.redraw_house_numbers --dry-run   # what WOULD change
    docker compose run --rm api python -m scripts.redraw_house_numbers
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import get_connection
from core.matchmaker import OutageAlert, _gate_feature, _house_number_feature, _location_envelope
from spatial.spatial_engine import clean_street, cyrillic_to_latin, region_opstine


def _length_km(geom: dict) -> float:
    parts = {"LineString": [geom.get("coordinates")], "MultiLineString": geom.get("coordinates")}.get(
        geom.get("type")) or []
    km = 0.0
    for part in parts:
        for (x1, y1), (x2, y2) in zip(part, part[1:]):
            km += math.hypot((x2 - x1) * 111.32 * math.cos(math.radians(y1)), (y2 - y1) * 110.57)
    return km


def redraw(fc: dict, alert: OutageAlert) -> tuple[dict, list[tuple[str, float, float]]]:
    """(the redrawn FeatureCollection, [(street, km before, km after)] per changed line). A named
    street's line is paired with its location in order - _build_visual_features draws one per
    location, in the announcement's own order."""
    region = alert.city or 'НОВИ САД'
    locs = [loc for loc in alert.affected_locations if loc.main_street]
    used, changes, features = set(), [], []
    for feat in fc.get("features") or []:
        props = feat.get("properties") or {}
        if (props.get("kind") != "street" or props.get("street_of")
                or (feat.get("geometry") or {}).get("type") not in ("LineString", "MultiLineString")):
            features.append(feat)
            continue
        i = next((k for k, loc in enumerate(locs) if k not in used
                  and cyrillic_to_latin(clean_street(loc.main_street)).upper() == props.get("street")), None)
        if i is None:
            features.append(feat)
            continue
        used.add(i)
        loc = locs[i]
        new = feat
        if not getattr(loc, 'includes_surrounding_streets', False):
            new = _house_number_feature(feat, loc, region)
        if new is not feat:
            env = _location_envelope(loc.settlement, region, region_opstine(region))
            new = _gate_feature(new, env, f"{loc.main_street} ({region}, redraw)") or feat
        if (new is not feat and new["geometry"]["type"] == feat["geometry"]["type"]
                and abs(_length_km(new["geometry"]) - _length_km(feat["geometry"])) < 0.005):
            new = feat   # already the houses' stretch (a second run)
        if new is not feat:
            changes.append((props.get("street"), _length_km(feat["geometry"]), _length_km(new["geometry"])))
        features.append(new)
    return {**fc, "features": features}, changes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args(argv)

    conn = get_connection()
    rows = conn.execute(
        "SELECT id, geojson_geometry, parsed_json FROM outages WHERE status = 'ACTIVE' "
        "AND (expires_at IS NULL OR expires_at > datetime('now')) "
        "AND geojson_geometry IS NOT NULL AND parsed_json IS NOT NULL ORDER BY id"
    ).fetchall()
    print(f"{len(rows)} outage(s) on the map{' (DRY RUN - nothing is written)' if args.dry_run else ''}")
    redrawn = lines = pins = 0
    before = after = 0.0
    for row in rows:
        try:
            fc = json.loads(row["geojson_geometry"])
            alert = OutageAlert.model_validate_json(row["parsed_json"])
        except Exception:
            continue
        if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
            continue
        new_fc, changes = redraw(fc, alert)
        if not changes:
            continue
        redrawn += 1
        for street, km_before, km_after in changes:
            before += km_before
            after += km_after
            pins += km_after == 0
            lines += km_after > 0
            print(f"  #{row['id']} {street}: {km_before:.2f} km -> "
                  f"{'pin' if km_after == 0 else f'{km_after:.2f} km'}")
        if not args.dry_run:
            conn.execute("UPDATE outages SET geojson_geometry = ? WHERE id = ?",
                         (json.dumps(new_fc), row["id"]))
    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"\n{redrawn} outage(s) {'would be ' if args.dry_run else ''}redrawn: {lines} stretch(es), "
          f"{pins} pin(s); drawn street length {before:.1f} km -> {after:.1f} km")
    return 0


if __name__ == "__main__":
    sys.exit(main())
