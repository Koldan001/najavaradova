# web/ — NajavaRadova.rs front page

Static landing page: header, a live map with a day-filtered outage list
(Danas / Sutra / Prekosutra), a Telegram sign-up section, and a source list.
Single HTML file, no build step, vanilla JS + Leaflet from CDN.

## Run locally

```bash
python -m http.server 8000 --directory web
# open http://localhost:8000   (localhost, not 0.0.0.0)
```

Leaflet and the CARTO dark basemap tiles load from CDN, so the map needs an
internet connection. Opening `index.html` straight from disk works too — there
are no same-origin `fetch` requirements beyond the optional `outages.json`.

## Data

The page loads `web/outages.json` on start. If that file is missing, fails to
load, or contains no upcoming outages, it falls back to the built-in `SAMPLE`
array in `index.html`. That fallback is labelled on screen — the badge reads
"Demo prikaz" and a note says the entries are examples — so visitors are never
shown placeholder outages as if they were real announcements.

`outages.json` is generated from the outages DB:

```bash
python scripts/build_web_data.py     # writes web/outages.json
```

It reads `data/outage_notifier.db` and converts each `ACTIVE` outage dated today
or later into:

| field | meaning |
| --- | --- |
| `kind` / `color` | VODA / STRUJA / GREJANJE / RADOVI, from the DB `category` |
| `street`, `region` | transliterated to Latin, title-cased, capped at 3 names + `+N` |
| `d` | whole days from today (0 = Danas) — drives the day tabs |
| `time` | `time_window`, blanked when it is `N/A` |
| `source`, `sourceUrl` | provider name resolved from the announcement host |
| `summary`, `effects` | plain-language explanation shown when a card is opened |
| `paths` | GeoJSON geometry flipped from `[lng,lat]` to Leaflet `[lat,lng]` |

`summary` and `effects` are derived in `summarize()` from the category, the
parsed `event_type` and a keyword scan of `raw_text` — the notices themselves
are long and inconsistent between providers, so the page gets a headline plus
the concrete consequences ("Bez tople vode.", "Moguć slab pritisak…") instead.

`web/outages.json` is git-ignored — it's a build artifact tied to the local DB.
Regenerate it after each scrape and redeploy `web/`, or wire the command into a
scheduled job / container entrypoint later.

## Page behaviour

- **Day tabs** — Danas / Sutra / Prekosutra, counted from `d`.
- **Viewport filter** — panning or zooming the map narrows the list to outages
  intersecting the visible area; a bar shows "Prikazano X od Y" with a reset.
  Clicking a card moves the map *without* re-filtering, so the rest of the list
  does not vanish underneath you.
- **Geolocation** — the "Moja lokacija" button (and, on repeat visits where
  permission is already granted, the initial load) snaps to the nearest city
  that has outages, or to the visitor's own position if none is within ~60 km.
- **Detail panel** — clicking a card expands `summary` + `effects` + a link to
  the official announcement, and rewrites the Telegram preview lower down the
  page to that outage.
- **Header** — sticky; its button flips between "Prijavi se za notifikacije"
  and "Nazad na mapu" depending on scroll position.

## Deploy (static host)

Any static file server works. The whole site is the `web/` directory.

- **Cloudflare Pages**: connect the repo, build command *(none)*, output
  directory `web`. Attach the custom domain in the Pages project.
- **Container**:

  ```dockerfile
  FROM nginx:alpine
  COPY web/ /usr/share/nginx/html/
  ```

  To ship real data in the image, run `python scripts/build_web_data.py` in the
  build and `COPY web/outages.json` alongside.
