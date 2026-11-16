"""Imports an RGZ street-name CSV export into street_registry."""

import sys
import os
import csv
import sqlite3

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.database import get_connection


def import_rgz_csv(csv_path):
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return

    print(f"Reading RGZ street data from {csv_path}")

    conn = get_connection()
    cursor = conn.cursor()
    inserted_count = 0

    try:
        with open(csv_path, mode='r', encoding='utf-8-sig') as f:
            sample = f.read(2048)
            f.seek(0)
            delimiter = ';' if ';' in sample else ','

            reader = csv.DictReader(f, delimiter=delimiter)

            for row in reader:
                city = (
                    row.get('opstina_ime_latinica') or
                    row.get('opstina_ime') or
                    row.get('naselje_ime_latinica') or
                    ""
                ).strip().upper()

                naselje = (
                    row.get('naselje_ime_latinica') or
                    row.get('naselje_ime') or
                    city
                ).strip().upper()

                street_name = (
                    row.get('ulica_ime_latinica') or
                    row.get('ulica_ime') or
                    ""
                ).strip().upper()

                if not street_name or len(street_name) < 2:
                    continue

                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO street_registry
                        (city, opstina, street_name, primary_neighborhood)
                        VALUES (?, ?, ?, ?)
                    ''', (city, city, street_name, naselje))
                    if cursor.rowcount > 0:
                        inserted_count += 1
                except sqlite3.Error:
                    pass

        conn.commit()
        conn.close()
        print(f"Imported {inserted_count} streets into street_registry")

    except Exception as e:
        print(f"Error processing CSV file: {e}")


if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "data/rgz_streets.csv"
    import_rgz_csv(csv_file)
