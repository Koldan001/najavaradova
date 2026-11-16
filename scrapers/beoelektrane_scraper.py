import sys
import os
import re
import time

import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed
from scrapers._common import use_scraper_date

# JKP "Beogradske elektrane" — Belgrade district heating. Two evergreen pages, both edited in
# place rather than posting dated articles:
#   PLANNED_WORKS_URL  - ongoing toplovod works, grouped by gradska opština
#   DISTURBANCES_URL   - current unplanned interruptions; usually reads "NEMA POREMEČAJA"
# Neither page ever states an end time; treating an outage as lasting until end of day is a
# downstream lifecycle decision, not this scraper's job - it only records what is published.
PLANNED_WORKS_URL = "https://beoelektrane.co.rs/planirani-radovi/radovi-na-toplovodnoj-mrezi/"
DISTURBANCES_URL = "https://beoelektrane.co.rs/category/poremecaji-u-snabdevanju/"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
REQUEST_DELAY = 1.0

CITY = "БЕОГРАД"

# "28 авг 2026" / "3 септембар 2026" / "3 sep 2026" - day, Serbian month (cyrillic or latin,
# abbreviated or full), year. Keyed on the first three letters of the month.
_MONTHS = {
    "јан": 1, "феб": 2, "мар": 3, "апр": 4, "мај": 5, "јун": 6,
    "јул": 7, "авг": 8, "сеп": 9, "окт": 10, "нов": 11, "дец": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "avg": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+([А-Яа-яA-Za-zЂђЈјЉљЊњЋћЏџ]{3,})\s+(\d{4})")

# "Radovi na toplovodnoj mreži – Voždovac" - the section heading whose opština scopes the
# street lines under it. The dash is an en-dash on the live page; accept a plain hyphen too.
_SECTION_RE = re.compile(r"toplovodnoj\s+mre[žz]i\s*[–—-]\s*(.+)$", re.IGNORECASE)

_NO_DISTURBANCE_RE = re.compile(r"NEMA\s+(POREME[ČC]AJA|PREKIDA)", re.IGNORECASE)


def _fetch(url: str) -> BeautifulSoup | None:
    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"⚠️ Could not fetch {url}: {e}")
        return None


def _content_root(soup: BeautifulSoup):
    art = soup.find("article", id=lambda v: v and v.startswith("post-"))
    return (art.select_one(".entry-content") if art else None) or \
           (art if art else None) or soup.select_one(".entry-content") or soup.select_one("main")


def _parse_page_date(root) -> str | None:
    """The page's own 'DD <month> YYYY' heading -> DD.MM.YYYY. This is the publish/refresh date
    of an evergreen page, used as the outage date since the works lines carry none."""
    head = root.find(["h1", "h2", "h3"])
    m = _DATE_RE.search(head.get_text(" ", strip=True)) if head else None
    if not m:
        m = _DATE_RE.search(root.get_text(" ", strip=True))
    if not m:
        return None
    day, month_word, year = m.groups()
    month = _MONTHS.get(month_word[:3].lower())
    return f"{int(day):02d}.{month:02d}.{year}" if month else None


def _scrape_planned_works(dry_run: bool):
    soup = _fetch(PLANNED_WORKS_URL)
    if not soup:
        return
    root = _content_root(soup)
    if not root:
        print("⚠️ planned works: no content root")
        return

    date_str = _parse_page_date(root)

    # Walk the content in document order, attaching each street <p> to the opština of the last
    # section heading seen.
    sections = {}       # opština -> [line, ...]
    current_opstina = None
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        m = _SECTION_RE.search(text)
        if m:
            current_opstina = m.group(1).strip(" .–—-")
            sections.setdefault(current_opstina, [])
            continue
        if "zahvaljuju se" in text.lower() or text.startswith("*"):
            continue
        if current_opstina and (text.startswith("-") or text.startswith("–") or "radovi" in text.lower()):
            sections[current_opstina].append(text.lstrip("-–— ").strip())

    if not sections:
        print("⚠️ planned works: no opština sections found")
        return

    print(f"\n📋 PLANNED WORKS ({date_str or 'no date'}) - {len(sections)} opština section(s)")
    for opstina, lines in sections.items():
        lines = [ln for ln in lines if len(ln) > 8]
        if not lines:
            continue
        body = "\n".join(f"- {ln}" for ln in lines)
        raw_text = (f"Beograd, opština {opstina}. Planirani radovi na toplovodnoj mreži"
                    + (f" ({date_str})." if date_str else ".") + f"\n{body}")

        text_hash = compute_text_hash(raw_text)
        if is_hash_processed(text_hash):
            continue

        print(f"   ▸ {opstina}: {len(lines)} line(s)")
        _parse_and_queue(raw_text, text_hash, date_str, default_settlement=opstina,
                         dry_run=dry_run, source_url=PLANNED_WORKS_URL)


def _scrape_disturbances(dry_run: bool):
    soup = _fetch(DISTURBANCES_URL)
    if not soup:
        return
    root = _content_root(soup)
    if not root:
        print("⚠️ disturbances: no content root")
        return

    full = root.get_text("\n", strip=True)
    if _NO_DISTURBANCE_RE.search(full):
        print("\n✅ DISTURBANCES: NEMA POREMEČAJA - nothing to record")
        return

    date_str = _parse_page_date(root)
    # Everything except the standing disclaimer line and the date heading.
    lines = [
        ln.strip() for ln in full.splitlines()
        if ln.strip() and not ln.strip().startswith("*") and not _DATE_RE.search(ln)
    ]
    body = "\n".join(lines)
    if len(body) < 30:
        print("\n⚠️ DISTURBANCES: page has no usable text")
        return

    raw_text = (f"Beograd. Trenutni prekid u isporuci toplotne energije"
                + (f" ({date_str})." if date_str else ".") + f"\n{body}")
    text_hash = compute_text_hash(raw_text)
    if is_hash_processed(text_hash):
        print("\n(disturbances: already recorded this text)")
        return

    print(f"\n🔥 DISTURBANCE REPORT ({date_str or 'no date'})")
    _parse_and_queue(raw_text, text_hash, date_str, default_settlement=None,
                     dry_run=dry_run, source_url=DISTURBANCES_URL)


def _parse_and_queue(raw_text, text_hash, date_str, default_settlement, dry_run, source_url):
    parsed = parse_announcement(raw_text)
    if not parsed:
        print("     ↳ parser returned nothing, skipping")
        return

    # This scraper only ever reads beoelektrane.co.rs - JKP "Beogradske elektrane" heats
    # Belgrade only, so every announcement here IS Belgrade by construction.
    parsed.city = CITY
    parsed.event_type = parsed.event_type or "HEATING"
    # Neither source page (see module docstring) EVER states a time - this is a structural fact
    # about the site, not a per-announcement gap parser.py's rule 8 can reliably infer from raw
    # text that has literally nothing to infer from. Left to the model, this came back as a
    # fabricated full-day "00:00 - 24:00" instead of rule 8's own missing-time defaults, which
    # read as a real (alarming, always-"u toku") outage window instead of an honest unknown.
    # Overridden here deterministically, the same way CITY is above - known-true from the
    # source's shape, not worth re-deriving per announcement.
    for loc in parsed.affected_locations:
        loc.time_window = "07:00 - 19:00"
    if date_str:
        use_scraper_date(parsed, date_str, raw_text)
    if default_settlement:
        # The opština comes from the section heading, not the street line, so the parser never
        # sees it per-location. query_rgz_streets already knows Belgrade's opštine are stored as
        # "BEOGRAD (<opština>)" and retries that form, so the bare opština name here is enough.
        for loc in parsed.affected_locations:
            if not loc.settlement:
                loc.settlement = default_settlement

    if dry_run:
        from core.matchmaker import calculate_outage_hexes
        hexes, quality = calculate_outage_hexes(parsed, raw_text)
        print(f"     [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
        for loc in parsed.affected_locations:
            print(f"       - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r} "
                  f"bounds=({loc.intersection_start},{loc.intersection_end}) surrounding={loc.includes_surrounding_streets}")
        return

    process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="HEATING", source_url=source_url)


def run_scraper(dry_run: bool = False):
    print("Scraping Beogradske elektrane (heating): planned works + disturbances\n")
    _scrape_planned_works(dry_run)
    _scrape_disturbances(dry_run)


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
