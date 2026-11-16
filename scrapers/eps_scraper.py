import sys
import os
import re
import hashlib
import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

# Ensure root directory is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.database import get_connection
from parser import parse_announcement
from core.matchmaker import _compute_expires_at, process_and_queue_outage
from scrapers._common import drop_unknown_settlements
from spatial.spatial_engine import is_serbian_opstina, naselje_owner_opstine, region_opstine

# Fallback only: Dan_0 = today, Dan_1 = sutra, Dan_2 = prekosutra, Dan_3 = +3 days. The page
# itself DOES state its real date in a heading above the table ("НОВИ САД - Планирана искључења
# за датум: 03.09.2026.") - confirmed real, use that first. This offset is only a backstop for
# the rare page that's missing the heading, since trusting our own clock over the site's own
# stated date is backwards (a delayed/skipped publish would silently mislabel every row on it).
DAY_OFFSETS = {"Dan_0": 0, "Dan_1": 1, "Dan_2": 2, "Dan_3": 3}

_PAGE_DATE_RE = re.compile(r"датум[:\s]*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.IGNORECASE)
# The Belgrade pages write the same heading with an ISO date ("за датум: 2026-09-25").
_PAGE_DATE_ISO_RE = re.compile(r"датум[:\s]*(\d{4})-(\d{1,2})-(\d{1,2})", re.IGNORECASE)

# A STREET-type location's settlement is usually left for Gemini to fill in from context, but a
# long comma-separated street list under one place mention is exactly where it drops that context
# (confirmed real case: the "Novi Sad" branch page ran a row prefixed "Место Темерин" - Temerin,
# a separate town - over 9 named streets, and every one came back with settlement=None). Without
# a settlement, a street search falls back to the table's own Opština column as its search scope -
# fine for the *ordinary* case this column is right about, but Temerin has no streets of its own
# in that scope, so the fallback widened to Novi Sad's much bigger city radius and picked up
# unrelated Novi Sad streets that happened to share a name.
#
# Anchored at the very start of the Ulice text on purpose: "СТАРИ СЛАНКАМЕН: место Стари
# Сланкамен ..." and "Опово: цело насељено место осим следећих улица ..." both put other text
# before "место" and must NOT match here - those are the opština's own name repeated as a
# heading, not a different place.
_INLINE_PLACE_RE = re.compile(r"^(?:цело\s+)?(?:насељено\s+)?место\s+([^.:,]{2,30}?)(?:\s+улица\b|[.:,]|$)",
                               re.IGNORECASE)


def _inline_place(row_opstina: str, ulice: str) -> str | None:
    """The place named at the very start of the Ulice text, if any and if it names somewhere
    other than the opština itself - None otherwise. Deliberately NOT checked against RGZ here:
    RGZ coverage has real gaps (confirmed: 4 of 9 real EPS villages tested came back with no RGZ
    naselje data at all, for opština RGZ simply hasn't been imported for yet, not because the
    village isn't real) - "RGZ doesn't know this place" is not evidence it's a different
    municipality. Left for get_local_street_segments' own RGZ-then-OSM fallback to resolve
    either way once it has the real name to search for, instead of guessing here."""
    m = _INLINE_PLACE_RE.match((ulice or "").strip())
    if not m:
        return None
    candidate = m.group(1).strip()
    if not candidate or candidate.upper() == row_opstina.strip().upper():
        return None
    return candidate


# A branch page sometimes carries a row for a neighbouring opština and says so at the very start
# of the Ulice text, while the Opština column still names the branch's own (confirmed on the
# Lazarevac rows: "Општина Горњи Милановац: насеље Бољковци ...", "Општина Љиг: насеље ...").
_INLINE_OPSTINA_RE = re.compile(r"^општина\s+([^:]{2,40}):", re.IGNORECASE)


def _inline_opstina(ulice: str) -> str | None:
    """The opština a row names outright at the start of its Ulice text, if it is a real one."""
    m = _INLINE_OPSTINA_RE.match((ulice or "").strip())
    if m and is_serbian_opstina(m.group(1)):
        return m.group(1).strip()
    return None


def _extract_page_date(html_content: str) -> str | None:
    m = _PAGE_DATE_RE.search(html_content)
    if m:
        d, mth, y = m.groups()
    else:
        m = _PAGE_DATE_ISO_RE.search(html_content)
        if not m:
            return None
        y, mth, d = m.groups()
    try:
        datetime(int(y), int(mth), int(d))
    except ValueError:
        return None
    return f"{int(d):02d}.{int(mth):02d}.{y}"

EPS_ENDPOINTS = {
    "NoviSad": {
        "Dan_0": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/NoviSad_Dan_0_Iskljucenja.htm",
        "Dan_1": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/NoviSad_Dan_1_Iskljucenja.htm",
        "Dan_2": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/NoviSad_Dan_2_Iskljucenja.htm",
        "Dan_3": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/NoviSad_Dan_3_Iskljucenja.htm",
    },
    "Beograd": {
        "Dan_0": "https://elektrodistribucija.rs/planirana-iskljucenja-beograd/Dan_0_Iskljucenja.htm",
        "Dan_1": "https://elektrodistribucija.rs/planirana-iskljucenja-beograd/Dan_1_Iskljucenja.htm",
        "Dan_2": "https://elektrodistribucija.rs/planirana-iskljucenja-beograd/Dan_2_Iskljucenja.htm",
        "Dan_3": "https://elektrodistribucija.rs/planirana-iskljucenja-beograd/Dan_3_Iskljucenja.htm",
    },
    "Kraljevo": {
        "Dan_0": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kraljevo_Dan_0_Iskljucenja.htm",
        "Dan_1": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kraljevo_Dan_1_Iskljucenja.htm",
        "Dan_2": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kraljevo_Dan_2_Iskljucenja.htm",
        "Dan_3": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kraljevo_Dan_3_Iskljucenja.htm",
    },
    "Kragujevac": {
        "Dan_0": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kragujevac_Dan_0_Iskljucenja.htm",
        "Dan_1": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kragujevac_Dan_1_Iskljucenja.htm",
        "Dan_2": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kragujevac_Dan_2_Iskljucenja.htm",
        "Dan_3": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kragujevac_Dan_3_Iskljucenja.htm",
    },
    "Nis": {
        "Dan_0": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Nis_Dan_0_Iskljucenja.htm",
        "Dan_1": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Nis_Dan_1_Iskljucenja.htm",
        "Dan_2": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Nis_Dan_2_Iskljucenja.htm",
        "Dan_3": "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Nis_Dan_3_Iskljucenja.htm",
    },
}


def compute_text_hash(text: str) -> str:
    normalized = " ".join(text.strip().split()).upper()
    return hashlib.md5(normalized.encode('utf-8')).hexdigest()


def is_hash_processed(text_hash: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM outages WHERE raw_text_hash = ?", (text_hash,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def refresh_source_url(text_hash: str, url: str) -> None:
    """Point an already-stored outage's source_url at `url` when it differs. For a byte-identical
    re-scrape the scraper short-circuits on the hash before process_and_queue_outage ever runs,
    so this is the only place a stale/missing link gets corrected. Two callers:
      - eps_scraper: an unchanged row slides Dan_3 -> Dan_2 -> ... -> Dan_0, and EPS reuses the
        old URL for a different day, so the "Zvanična objava" link must follow the row.
      - ns_toplana_scraper: rows saved before it started passing source_url (2026-09-02) have
        none - fill it from the article URL on the next pass.
    Idempotent: the IFNULL/!= guard means it writes at most once per changed value."""
    conn = get_connection()
    conn.execute(
        "UPDATE outages SET source_url = ? WHERE raw_text_hash = ? AND IFNULL(source_url, '') != ?",
        (url, text_hash, url),
    )
    conn.commit()
    conn.close()


# Table header text -> row key. Columns are mapped by their header, not by position: the regional
# pages have four columns [Огранак, Општина, Време, Улице], the Belgrade pages only three
# [Општина, Време, Улице]. The old positional "at least 4 columns" rule dropped every Belgrade row
# without a trace (2026-08-23 to 2026-09-24: no Belgrade power cut was ever scraped).
_COLUMN_KEYS = {"огранак": "ogranak", "општина": "opstina", "време": "vreme", "улице": "ulice"}


def extract_table_rows(html_content: str) -> list[dict]:
    """Parses EPS HTML table rows into individual structured dictionaries."""
    soup = BeautifulSoup(html_content, "html.parser")

    parsed_rows = []
    columns = None
    for row in soup.find_all("tr"):
        cols = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        keys = [_COLUMN_KEYS.get(c.strip().lower()) for c in cols]
        if "opstina" in keys and "ulice" in keys:
            columns = keys
            continue
        if columns is None or len(cols) < len(columns):
            continue
        record = {key: value for key, value in zip(columns, cols) if key}
        if record.get("ulice", "").strip():
            record.setdefault("ogranak", "")
            parsed_rows.append(record)
    return parsed_rows


def _belgrade_opstina(name: str) -> str:
    """A Belgrade page names only the gradska opština, and "Палилула" is also one of Niš's. Pin
    such a name to Belgrade so the street lookup and the leak gate never search Niš for it."""
    opstine = region_opstine(name)
    if opstine and len(opstine) > 1:
        return f"{name.strip()} - Београд"
    return name


# One cycle's share of time for NEW rows. EPS can post ~130 at once (24 Sep 2026), each one a
# Gemini call; with no cap the other sources and the map publish behind it waited for hours.
# Rows past the budget aren't stored, so the next cycle (15 min later) picks them up.
CYCLE_BUDGET_S = int(os.environ.get("EPS_CYCLE_BUDGET_S", "900"))


def _long_over(target_date: str, vreme: str, now: datetime | None = None) -> bool:
    """The row's window plus the map's grace period is already behind us: it would never show on
    the map or reach anyone, so it isn't worth a Gemini call."""
    expires = _compute_expires_at(target_date, None, vreme)
    return bool(expires) and datetime.fromisoformat(expires) < (now or datetime.now())


def _process_new_row(url: str, target_date: str, row: dict, row_text: str, row_hash: str,
                     live_hashes: set[str] | None = None) -> bool:
    """Parse one not-yet-stored EPS row with Gemini and hand it to the matcher. True when stored."""
    print(f"🔥 NEW EPS ROW ({target_date}): [{row['opstina']}] {row['vreme']} -> {row['ulice'][:40]}...")

    # Pass ONLY this row's context to Gemini - but prefixed with what the row
    # itself never says. An EPS table row is bare "Opština | Vreme | Ulice":
    # the words struja/električna energija appear nowhere in it, so the model
    # had nothing to ground event_type or message_template on and could
    # describe a power cut as "planirano isključenje vode" (confirmed, real).
    # The prefix is NOT part of row_text, which is hashed for dedup and has to
    # stay byte-identical or every stored EPS row re-processes as new.
    parsed = parse_announcement(
        "Planirano isključenje ELEKTRIČNE ENERGIJE (EPS Distribucija - "
        "elektrodistribucija.rs). Ovo je prekid struje, nije voda ni grejanje.\n"
        + row_text
    )
    if not parsed:
        return False

    # Override rather than trust the model's guess - the page state's day
    # (Dan_0/1/2/3) is a known fact, not something to infer from a row that
    # doesn't spell the date out itself.
    parsed.outage_date = target_date
    # Same override every single-city scraper already does for its own fixed
    # OPSTINA, applied per-row here since EPS spans the whole country. The
    # Opština column is ground truth - parsed straight from the table before
    # Gemini ever sees the text - so trust it over the model's guess.
    # Confirmed real bug 2026-09-04: a Crveni Krst (Niš) row got mislabeled
    # Beograd by the model (ambiguous/colliding street names across cities),
    # putting it on the wrong map tab with the wrong city's street geometry
    # drawn under it. Never overridden before this - every EPS row trusted
    # the model's city guess outright.
    # A row that names another opština outright belongs to that one (see
    # _INLINE_OPSTINA_RE). Applied here, after row_text was hashed, so a row
    # already stored never hashes differently and re-sends.
    row_opstina = _inline_opstina(row["ulice"]) or row["opstina"]
    parsed.city = row_opstina.strip().upper()
    drop_unknown_settlements(parsed, parsed.city)

    # A place named inline in the Ulice text (see _inline_place) is more
    # specific than the Opština column and belongs on every street location
    # that came back with no settlement of its own - fills in exactly the gap
    # that let the Temerin leak happen.
    inline_place = _inline_place(row_opstina, row["ulice"])
    gate_region = None
    if inline_place:
        applied = False
        for loc in parsed.affected_locations:
            if (loc.location_type != "ENTIRE_SETTLEMENT" and loc.main_street
                    and not (loc.settlement or "").strip()):
                loc.settlement = inline_place
                applied = True
        # A village RGZ files under this very opština ("Нови Сад" + "Буковац:
        # ...") needs no swap: the RGZ lookup resolves it exactly under the
        # opština the table states, and the geocoder never gets involved.
        eps_ops = region_opstine(row_opstina)
        in_opstina = bool(eps_ops and naselje_owner_opstine(inline_place) & eps_ops)
        if applied and not in_opstina:
            # get_street_hexes' parent_region comes from parsed.city/region,
            # not from a location's own settlement - it has to move too, or
            # the geocoder correctly REJECTS the inline place as inconsistent
            # with the (now wrong) parent it's still being validated against.
            # Confirmed live: "ТЕМЕРИН" alone resolves via Nominatim in one
            # call, but resolves to nothing once parent_region="НОВИ САД" is
            # still attached. Deliberately NOT done for the settlement-only
            # case above (an ENTIRE_SETTLEMENT row never reaches `applied`) -
            # there, settlement already equalling city_default is read as a
            # parse artifact and the location gets dropped (see
            # process_and_queue_outage's ENTIRE_SETTLEMENT guard), so
            # overriding parsed.city there would break Насељено место rows
            # that already resolve correctly today.
            parsed.city = inline_place.strip().upper()
            # The table's own Opština still anchors the leak gate - the
            # swapped-in place is only as trustworthy as its geocode.
            gate_region = row_opstina.strip().upper()
    # This was never passed at all - every EPS outage stored source_url=NULL,
    # so the map popup's "Zvanična objava" link silently never showed for any
    # of them. `url` is the exact page (region + day) this row came from.
    process_and_queue_outage(parsed, row_text, text_hash=row_hash,
                             category="POWER", source_url=url,
                             gate_region=gate_region, live_hashes=live_hashes)
    return True


def _interleave(queues: list[list]) -> list:
    """[[a1, a2], [b1], [c1, c2]] -> [a1, b1, c1, a2, c2]: each region's rows take turns."""
    out = []
    for i in range(max((len(q) for q in queues), default=0)):
        out.extend(q[i] for q in queues if i < len(q))
    return out


def run_eps_scraper():
    print("⚡ Starting EPS Scraper with Individual Row Processing...\n")
    headers = {"User-Agent": "Mozilla/5.0"}
    total_new = 0
    skipped_over = deferred = 0
    started = time.monotonic()

    # Pass 1: read every page, keep the rows not stored yet - per day, one list per region.
    new_rows: dict[str, list[list]] = {}
    # Every row on every page this cycle, stored or not: a stored row among them is still
    # published, so a new row may not overwrite it as its "new version" (see live_hashes).
    live_hashes: set[str] = set()
    for region, days in EPS_ENDPOINTS.items():
        for day_label, url in days.items():
            try:
                res = requests.get(url, headers=headers, timeout=10)
                res.encoding = 'utf-8'

                if res.status_code != 200:
                    continue

                target_date = _extract_page_date(res.text) or (
                    datetime.now() + timedelta(days=DAY_OFFSETS[day_label])
                ).strftime("%d.%m.%Y")

                rows = extract_table_rows(res.text)
                if region == "Beograd":
                    for row in rows:
                        row["opstina"] = _belgrade_opstina(row["opstina"])

                fresh = []
                for row in rows:
                    # Date goes into the hashed string too, not just the parser prompt - without
                    # it, the same street/time slot repeated on Dan_1 and Dan_2 (a multi-day
                    # schedule, common on this site) hashed identically and Dan_2's row got
                    # silently deduped as "already processed" against Dan_1's. Confirmed real
                    # case: a real multi-day outage only ever showed up as day 1 ("sutra"),
                    # never day 2/3 ("prekosutra" etc).
                    row_text = f"Datum: {target_date} | Opština: {row['opstina']} | Vreme: {row['vreme']} | Ulice: {row['ulice']}"

                    # Compute hash per ROW, not per PAGE
                    row_hash = compute_text_hash(row_text)
                    live_hashes.add(row_hash)
                    if is_hash_processed(row_hash):
                        # Same row, now on a closer Dan_N page - repoint its source link there.
                        refresh_source_url(row_hash, url)
                        continue
                    if _long_over(target_date, row["vreme"]):
                        skipped_over += 1
                        continue
                    fresh.append((url, target_date, row, row_text, row_hash))
                new_rows.setdefault(day_label, []).append(fresh)

            except Exception as e:
                print(f"❌ Error scraping {region} {day_label}: {e}")

    # Pass 2: soonest day first, and within a day the regions take turns. Region by region, the
    # Novi Sad branch's ~44 new rows (24 Sep 2026) used up every cycle's budget before Belgrade,
    # next in line, got a single row in.
    for day_label in DAY_OFFSETS:
        for url, target_date, row, row_text, row_hash in _interleave(new_rows.get(day_label, [])):
            if time.monotonic() - started > CYCLE_BUDGET_S:
                deferred += 1
                continue
            if is_hash_processed(row_hash):   # the same row twice on one page
                continue
            try:
                if _process_new_row(url, target_date, row, row_text, row_hash, live_hashes):
                    total_new += 1
            except Exception as e:
                print(f"❌ Error processing EPS row [{row['opstina']}] {target_date}: {e}")

    print(f"\n✅ EPS Scraper complete. Processed {total_new} new row entries.")
    if skipped_over:
        print(f"   {skipped_over} new row(s) skipped - their window ended hours ago.")
    if deferred:
        print(f"⏱️ EPS time budget ({CYCLE_BUDGET_S}s) reached - {deferred} new row(s) left for the next cycle.")


if __name__ == "__main__":
    run_eps_scraper()