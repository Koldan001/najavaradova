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

# JKP "Gradska toplana" Zrenjanin — district heating. One long Joomla page, ~11 "Servisna
# informacija" blocks per page, 24 pages of history. The overwhelming majority are notices with
# no outage in them (price changes, surveys, e-invoice forms, "heating season has started"), so
# this scraper reads only page 1 and keeps a block only if it actually describes an interruption.
LIST_URL = "https://www.gradskatoplanazr.rs/servisne-informacije.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
REQUEST_DELAY = 1.0

# The heading is "Servisna informacija" on every block — with a recurring "Sevisna" typo in at
# least one, so match S...visna loosely rather than the exact spelling.
_HEADING_RE = re.compile(r"S\w{0,3}visna\s+informacija", re.IGNORECASE)

# Block is worth parsing only if it reads as a heating interruption, not a general notice.
_OUTAGE_MARKERS = (
    "prekid grejanja", "prekida grejanja", "bez grejanja", "obustav",
    "bez tople vode", "havarij", "kvar", "isključ", "iskljuc",
    "prekid u isporuci", "prekid isporuke", "obustava isporuke",
)
# ...unless it's clearly one of the standing non-outage notices that also contain a stray "kvar".
_SKIP_MARKERS = (
    "anketa", "obrazac prijave", "cenovnik", "cena toplotne energije nije",
    "javne nabavke", "poziva građane da učestvuju", "grejna sezona 20",
)

_DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
_TRAILING_DATE_RE = re.compile(r"Zrenjanin,\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", re.IGNORECASE)


def _iter_blocks(content_div):
    """Yields (block_text, heading_element) for each 'Servisna informacija' item. Each item is an
    <h2> heading followed by <p> paragraphs ending in a 'Zrenjanin, DD.MM.YYYY' line; the page
    also wraps every item in its own <div>, so dedupe on the heading element identity."""
    seen = set()
    for heading in content_div.find_all(["h1", "h2", "h3", "h4"]):
        if not _HEADING_RE.search(heading.get_text(" ", strip=True)):
            continue
        container = heading.find_parent(["div", "article"]) or heading.parent
        key = id(container)
        if key in seen:
            continue
        seen.add(key)
        text = container.get_text(" ", strip=True)
        text = _HEADING_RE.sub("", text, count=1).strip()
        if text:
            yield text


def _extract_date(block_text: str) -> str | None:
    m = _TRAILING_DATE_RE.search(block_text) or _DATE_RE.search(block_text)
    if not m:
        return None
    d, mth, y = m.groups()
    return f"{int(d):02d}.{int(mth):02d}.{y}"


def _is_outage(block_text: str) -> bool:
    low = block_text.lower()
    if any(s in low for s in _SKIP_MARKERS):
        return False
    return any(k in low for k in _OUTAGE_MARKERS)


def run_scraper(dry_run: bool = False):
    print(f"Scraping Zrenjanin heating announcements from: {LIST_URL}\n")

    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(LIST_URL, headers=HEADERS, timeout=20)
        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        print(f"❌ Could not fetch page: {e}")
        return

    content_div = soup.select_one(".blog") or soup.select_one("div.item-page") or soup.body
    if not content_div:
        print("⚠️ No content container found")
        return

    for block_text in _iter_blocks(content_div):
        if len(block_text) < 40 or not _is_outage(block_text):
            continue

        date_str = _extract_date(block_text)
        raw_text = f"Obaveštenje za dan {date_str}.\n{block_text}" if date_str else block_text

        text_hash = compute_text_hash(raw_text)
        if is_hash_processed(text_hash):
            continue

        print(f"\n🔥 ZR HEATING ANNOUNCEMENT ({date_str or 'no date'})")
        print(f"   ▸ {block_text[:90]}...")
        parsed = parse_announcement(raw_text)
        if not parsed:
            print("     ↳ parser returned nothing, skipping")
            continue

        # This scraper only ever reads gradskatoplanazr.rs - JKP "Gradska toplana" Zrenjanin
        # heats Grad Zrenjanin only, so every announcement here IS Zrenjanin by construction.
        parsed.city = "ЗРЕЊАНИН"
        parsed.event_type = parsed.event_type or "HEATING"
        if date_str:
            use_scraper_date(parsed, date_str, raw_text)

        if dry_run:
            from core.matchmaker import calculate_outage_hexes
            hexes, quality = calculate_outage_hexes(parsed, raw_text)
            print(f"     [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
            for loc in parsed.affected_locations:
                print(f"       - {loc.location_type} street={loc.main_street!r} "
                      f"settlement={loc.settlement!r} surrounding={loc.includes_surrounding_streets}")
            continue

        process_and_queue_outage(
            parsed, raw_text, text_hash=text_hash, category="HEATING", source_url=LIST_URL
        )


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
