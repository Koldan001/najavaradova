"""Validate data/kvartovi.geojson (hand-drawn kvart boundaries) before relying on it.

For every feature: checks it has a usable name, a Polygon/MultiPolygon geometry, and reports
how many H3 cells and RGZ streets it covers plus whether get_settlement_coverage() now routes
that name through the curated shape.

Usage: python scripts/check_kvartovi.py
"""
import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from spatial.spatial_engine import (
    CURATED_AREAS_PATH, geojson_to_h3_cells, get_settlement_coverage,
    get_streets_within_hexes,
)

DEFAULT_OPSTINA = "ЗРЕЊАНИН"


def main():
    if not os.path.exists(CURATED_AREAS_PATH):
        print(f"No file at {CURATED_AREAS_PATH} yet.")
        print("Export from geojson.io as GeoJSON and save it there.")
        return

    with open(CURATED_AREAS_PATH, encoding="utf-8") as f:
        collection = json.load(f)

    features = collection.get("features", [])
    if not features:
        print("File parsed, but it has no features.")
        return

    print(f"{len(features)} feature(s) in {CURATED_AREAS_PATH}\n")
    for i, feature in enumerate(features, 1):
        props = feature.get("properties") or {}
        name = str(props.get("name", "")).strip()
        opstina = str(props.get("opstina", "")).strip() or DEFAULT_OPSTINA
        geom_type = (feature.get("geometry") or {}).get("type")

        problems = []
        if not name:
            problems.append("missing properties.name")
        if geom_type not in ("Polygon", "MultiPolygon"):
            problems.append(f"geometry is {geom_type!r}, need Polygon/MultiPolygon")

        hexes = geojson_to_h3_cells(feature.get("geometry")) if not problems else set()
        if not problems and not hexes:
            problems.append("geometry produced 0 H3 cells (too small, or wrong winding)")

        tag = "OK " if not problems else "BAD"
        print(f"[{tag}] #{i} name={name!r} opstina={opstina!r} geom={geom_type}")
        if problems:
            for p in problems:
                print(f"       - {p}")
            continue

        streets = get_streets_within_hexes(hexes, opstina)
        cov_hexes, source = get_settlement_coverage(name, opstina)
        routed = "curated" if source == "curated" else f"NOT curated (got {source!r})"
        print(f"       {len(hexes)} hexes, {len(streets)} RGZ streets inside; "
              f"get_settlement_coverage -> {routed}")

    print("\nRe-run the scraper + visualize to see it on the map:")
    print("  python scripts/reset_vikzr_test.py && python -m scrapers.vikzr_scraper && python -m spatial.visualize")


if __name__ == "__main__":
    main()
