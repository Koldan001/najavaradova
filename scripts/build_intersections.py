import os
import json
import sqlite3
from shapely.geometry import shape, Point
from shapely.strtree import STRtree
from shapely.ops import nearest_points

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERBIA_DB_PATH = os.path.join(BASE_DIR, "data", "serbia_streets.db")

def build_intersection_index():
    if not os.path.exists(SERBIA_DB_PATH):
        print(f"❌ Database not found at {SERBIA_DB_PATH}")
        return

    conn = sqlite3.connect(SERBIA_DB_PATH)
    cursor = conn.cursor()

    # 1. Prepare Schema
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS intersections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            street_a TEXT NOT NULL,
            street_b TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_street_pair ON intersections(street_a, street_b)")
    
    cursor.execute("DELETE FROM intersections")
    conn.commit()

    # 2. Load Geometries
    print("📦 Loading street geometries from database...")
    cursor.execute("SELECT clean_name, geometry_json FROM streets WHERE geometry_json IS NOT NULL")
    rows = cursor.fetchall()

    geometries = []
    street_names = []

    for name, geom_json in rows:
        if not name or not geom_json:
            continue
        try:
            g = shape(json.loads(geom_json))
            if g.is_valid and not g.is_empty:
                geometries.append(g)
                street_names.append(name.upper().strip())
        except Exception:
            continue

    print(f"🌲 Building STRtree spatial index for {len(geometries)} streets...")
    tree = STRtree(geometries)

    # 3. Compute Exact Intersections (~25m limit)
    DISTANCE_LIMIT = 0.00025  # ~25 meters in degrees
    intersections_to_insert = []
    processed_pairs = set()

    print("⚡ Computing exact contact points via STRtree...")
    
    idx_a, idx_b = tree.query(geometries, predicate="dwithin", distance=DISTANCE_LIMIT)

    for i_a, i_b in zip(idx_a, idx_b):
        if i_a >= i_b:
            continue

        name_a = street_names[i_a]
        name_b = street_names[i_b]

        if name_a == name_b:
            continue

        pair_key = (name_a, name_b)
        if pair_key in processed_pairs:
            continue

        geom_a = geometries[i_a]
        geom_b = geometries[i_b]

        # FIX: Calculate exact contact point between the two street lines
        p_a, p_b = nearest_points(geom_a, geom_b)
        exact_point = Point((p_a.x + p_b.x) / 2.0, (p_a.y + p_b.y) / 2.0)

        processed_pairs.add(pair_key)
        
        intersections_to_insert.append((name_a, name_b, exact_point.y, exact_point.x))
        intersections_to_insert.append((name_b, name_a, exact_point.y, exact_point.x))

    # 4. Save to SQLite
    print(f"💾 Saving {len(intersections_to_insert)} accurate intersection pairs to database...")
    cursor.executemany(
        "INSERT INTO intersections (street_a, street_b, lat, lon) VALUES (?, ?, ?, ?)",
        intersections_to_insert
    )

    conn.commit()
    conn.close()
    print("✅ Pre-computation complete!")

if __name__ == "__main__":
    build_intersection_index()