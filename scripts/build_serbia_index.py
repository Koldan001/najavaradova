import json
import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEOJSON_PATH = os.path.join(BASE_DIR, "data", "serbia_roads.geojsonseq")
DB_PATH = os.path.join(BASE_DIR, "data", "serbia_streets.db")

def build_index():
    if not os.path.exists(GEOJSON_PATH):
        print(f"❌ Could not find: {GEOJSON_PATH}")
        print("Please run Step 2 (osmium command) first!")
        return

    print("⏳ Processing Serbia road geometries into SQLite...")

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('DROP TABLE IF EXISTS streets')
    cursor.execute('''
        CREATE TABLE streets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            clean_name TEXT,
            geometry_json TEXT
        )
    ''')
    cursor.execute('CREATE INDEX idx_clean_name ON streets(clean_name)')

    records = []
    count = 0

    with open(GEOJSON_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('\x1e'):
                line = line.lstrip('\x1e').strip()
            if not line:
                continue

            try:
                feature = json.loads(line)
                props = feature.get('properties', {})
                raw_name = props.get('name')

                if raw_name:
                    clean = str(raw_name).strip().upper()
                    geom = json.dumps(feature.get('geometry', {}))
                    records.append((raw_name, clean, geom))
                    count += 1

                if len(records) >= 10000:
                    cursor.executemany('INSERT INTO streets (name, clean_name, geometry_json) VALUES (?, ?, ?)', records)
                    conn.commit()
                    records = []

            except json.JSONDecodeError:
                continue

    if records:
        cursor.executemany('INSERT INTO streets (name, clean_name, geometry_json) VALUES (?, ?, ?)', records)
        conn.commit()

    conn.close()
    print(f"🎉 Success! Indexed {count} named street segments into '{DB_PATH}'.")

if __name__ == "__main__":
    build_index()