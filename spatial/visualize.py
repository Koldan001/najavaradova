import sys
import os
import sqlite3
import json
import folium
import webbrowser

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.database import get_connection

# `category` (WATER/POWER/HEATING) is a clean enum each scraper sets explicitly, knowing its own
# utility deterministically - see the note on the category column in core/database.py. Real bug
# this replaces: keying color off event_type's free Gemini text ("vodosnabdevanje", "kvar",
# "POWER", "toplotna energija", ...) missed cases where the model didn't happen to include a
# matching keyword (confirmed: event_type="kvar" alone rendered orange instead of blue).
WATER_COLOR = '#1E88E5'    # blue
POWER_COLOR = '#E53935'    # red
HEATING_COLOR = '#FB8C00'  # orange
WORKS_COLOR = '#8E24AA'    # purple - a works / road-closure notice with no utility supply cut.
                           # Distinct from all three and doesn't wash out on the light basemap
                           # the way a caution-yellow would; "purple = other infrastructure" is
                           # the usual dashboard convention. These are shown for context only -
                           # matchmaker never queues notifications for a WORKS row.
UNKNOWN_COLOR = '#9E9E9E'  # gray - genuinely uncategorized, not a "not yet assigned" placeholder

_CATEGORY_COLORS = {'WATER': WATER_COLOR, 'POWER': POWER_COLOR, 'HEATING': HEATING_COLOR,
                    'WORKS': WORKS_COLOR}


def _color_for_outage(category: str, event_type: str) -> str:
    if category in _CATEGORY_COLORS:
        return _CATEGORY_COLORS[category]

    # Rows scraped before the category column existed have category=NULL - fall back to the old
    # keyword guess on event_type rather than dumping them all into UNKNOWN_COLOR.
    et = (event_type or '').lower()
    if 'vod' in et:
        return WATER_COLOR
    if 'struj' in et or 'power' in et:
        return POWER_COLOR
    if 'topl' in et or 'grejanj' in et:
        return HEATING_COLOR
    return UNKNOWN_COLOR


def visualize_database_outages():
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # An outage drops off the map once its own stated end has passed (plus the grace matchmaker
    # already baked into expires_at). Rows with no datable end (expires_at IS NULL) stay until
    # cleared by hand. A NULL-end row older than a week is almost certainly stale, so hide it too.
    cursor.execute("""
        SELECT o.id, o.region, o.affected_streets, o.outage_date, o.time_window, o.raw_text,
               o.geojson_geometry, o.event_type, o.category, o.source_url, o.expires_at
        FROM outages o
        WHERE o.status = 'ACTIVE'
          AND (o.expires_at IS NULL OR datetime(o.expires_at) > datetime('now', 'localtime'))
          AND (
                o.expires_at IS NOT NULL
                OR o.created_at IS NULL
                OR datetime(o.created_at) > datetime('now', '-7 days')
              )
        ORDER BY o.id DESC
    """)
    outages = cursor.fetchall()

    if not outages:
        print("⚠️ No active outages found in SQLite database.")
        conn.close()
        return

    # OSM's own standard raster tiles: genuinely keyless. Carto's light_all CDN was used here
    # before and still answers 200, but it now serves an "API KEY REQUIRED" watermark image
    # instead of map imagery for unregistered use, which rendered that text across every tile of
    # the generated map rather than failing outright.
    m = folium.Map(
        location=[45.2517, 19.8369],
        zoom_start=12,
        tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    )
    rendered_count = 0

    for outage in outages:
        outage_id = outage['id']
        streets = outage['affected_streets'] or "Nije navedeno"
        raw_text = outage['raw_text']
        geojson_raw = outage['geojson_geometry']
        color = _color_for_outage(outage['category'], outage['event_type'])

        time_window = outage['time_window'] if outage['time_window'] not in (None, '', 'N/A') else None
        source_url = outage['source_url']
        source_link = (
            f'<div style="margin-top:8px;"><a href="{source_url}" target="_blank" '
            f'rel="noopener" style="color:{color};">Otvori originalnu objavu ↗</a></div>'
            if source_url else ''
        )

        popup_html = f"""
        <div style="font-family: Arial, sans-serif; min-width: 240px; max-width: 320px;">
            <h4 style="margin: 0 0 6px 0; color: {color};">Outage #{outage_id} ({outage['region']})</h4>
            <b>Pogođene ulice:</b> {streets}<br>
            <b>Datum:</b> {outage['outage_date']}
            {f'<br><b>Vreme:</b> {time_window}' if time_window else ''}
            <hr style="margin: 8px 0; border: 0; border-top: 1px solid #ccc;">
            <small style="color: #444;">{raw_text}</small>
            {source_link}
        </div>
        """

        if geojson_raw:
            try:
                geojson_data = json.loads(geojson_raw)
                features = geojson_data.get("features", [])

                # Areas draw first so a street line stays visible on top of the corridor or
                # settlement outline it sits inside.
                area_features = [
                    f for f in features
                    if f.get("geometry", {}).get("type") in ("Polygon", "MultiPolygon")
                ]
                line_features = [
                    f for f in features
                    if f.get("geometry", {}).get("type") in ("LineString", "MultiLineString")
                ]

                # Whole-settlement and "surrounding streets" outages have no street polyline at
                # all - their only geometry is the outline of the H3 cells actually being
                # notified. Those used to be filtered out here, so an entire class of outage
                # (every "прекид у целом насељу") rendered as a blank map.
                if area_features:
                    folium.GeoJson(
                        {"type": "FeatureCollection", "features": area_features},
                        name=f"Outage #{outage_id} (area)",
                        style_function=lambda x, color=color: {
                            'color': color,
                            'weight': 1.5,
                            'opacity': 0.7,
                            'fillColor': color,
                            'fillOpacity': 0.18
                        },
                        popup=folium.Popup(popup_html, max_width=340)
                    ).add_to(m)

                if line_features:
                    folium.GeoJson(
                        {"type": "FeatureCollection", "features": line_features},
                        name=f"Outage #{outage_id}",
                        # color=color as a default arg, not a bare closure over the loop
                        # variable - otherwise every feature would render with whichever
                        # color the LAST outage in the loop happened to have.
                        style_function=lambda x, color=color: {
                            'color': color,
                            'weight': 5,
                            'opacity': 0.9,
                            'lineCap': 'round'
                        },
                        popup=folium.Popup(popup_html, max_width=340)
                    ).add_to(m)

                if area_features or line_features:
                    rendered_count += 1
            except Exception as e:
                print(f"⚠️ Failed to parse GeoJSON for outage #{outage_id}: {e}")

    conn.close()

    output_dir = os.path.abspath("data")
    os.makedirs(output_dir, exist_ok=True)
    map_filename = os.path.join(output_dir, "map.html")
    
    m.save(map_filename)
    print(f"✅ Clean vector map generated for {rendered_count} outages at: {map_filename}")
    
    file_path = f"file://{map_filename}"
    print(f"🌐 Opening map: {file_path}")
    webbrowser.open(file_path)

if __name__ == "__main__":
    visualize_database_outages()