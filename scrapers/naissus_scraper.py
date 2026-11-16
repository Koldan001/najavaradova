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
from scrapers._common import drop_unknown_settlements, use_scraper_date

# JKP "Naissus" Niš. The /servisne-informacije/ page lists links to individual posts (kvarovi,
# planirani radovi, havarije). Post bodies are plain text; a "kvarovi" roundup carries several
# unrelated jobs under ALL-CAPS sub-headings, split into one outage each.
LIST_URL = "https://jkpnaissus.co.rs/servisne-informacije/"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
# robots.txt: "Crawl-delay:3" — honour it.
REQUEST_DELAY = 3.5
OPSTINA = "НИШ"

# Post slugs worth reading (the decoded Cyrillic slug always contains one of these stems).
_RELEVANT = ("квар", "радов", "хавариј", "прекид", "планиран", "интервенциј", "поправк")

_DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
_MAX_HEADING_LEN = 90
_TAIL_MARKERS = ("ЈКП „Наиссус", "Наиссус се захваљује", "Служба за односе", "захваљујемо се")


def _fetch(url: str) -> BeautifulSoup | None:
    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.encoding = r.apparent_encoding or "utf-8"
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"⚠️ Could not fetch {url}: {e}")
        return None


def _is_heading(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3 or len(line) > _MAX_HEADING_LEN:
        return False
    return all(c.isupper() for c in letters)


def _split_sections(body_text: str) -> list[str]:
    """A kvarovi roundup runs several jobs together, each opening with an ALL-CAPS heading
    ('ПОПРАВКА КВАРА У СЕЛУ СУПОВАЦ', 'ИНТЕРВЕНЦИЈА У СЕЛУ МРАМОРСКО БРДО'). Split on those so
    each village/street job becomes its own outage. A body with no such heading stays whole."""
    lines = [ln.strip() for ln in re.split(r"(?<=[.!?])\s+|\n", body_text) if ln.strip()]
    sections, current = [], []
    for line in lines:
        if any(m in line for m in _TAIL_MARKERS):
            break
        if _is_heading(line):
            if current:
                sections.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(" ".join(current))
    return sections or ([body_text] if body_text else [])


def run_scraper(dry_run: bool = False):
    print(f"Scraping Niš water announcements from: {LIST_URL}\n")

    soup = _fetch(LIST_URL)
    if not soup:
        return

    seen = set()
    for a in soup.select("article a[href], .entry-content a[href], main a[href]"):
        href = a["href"]
        if "jkpnaissus.co.rs/" not in href:
            continue
        slug = requests.utils.unquote(href).lower()
        if not any(k in slug for k in _RELEVANT):
            continue
        if any(x in href for x in ("/detekcija-kvarova", "/prijava-kvara", "/servisne-informacije", "/vesti/")):
            continue
        if href in seen:
            continue
        seen.add(href)

        post = _fetch(href)
        if not post:
            continue
        art = post.find("article") or post
        content = art.select_one(".entry-content") or art
        full = content.get_text("\n", strip=True)

        m = _DATE_RE.search(full) or _DATE_RE.search(a.get_text(" ", strip=True))
        date_str = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{m.group(3)}" if m else None

        for section in _split_sections(full):
            if len(section) < 40 or "вод" not in section.lower():
                continue

            raw_text = f"Ниш, обавештење за дан {date_str}.\n{section}" if date_str else f"Ниш.\n{section}"
            text_hash = compute_text_hash(raw_text)
            if is_hash_processed(text_hash):
                continue

            print(f"\n🔥 NIŠ WATER ({date_str or 'no date'}): {href[:80]}")
            print(f"   ▸ {section[:90]}...")
            parsed = parse_announcement(raw_text)
            if not parsed:
                print("     ↳ parser returned nothing, skipping")
                continue

            parsed.city = OPSTINA
            if date_str:
                use_scraper_date(parsed, date_str, raw_text)
            drop_unknown_settlements(parsed, OPSTINA)

            if dry_run:
                from core.matchmaker import calculate_outage_hexes
                hexes, quality = calculate_outage_hexes(parsed, raw_text)
                print(f"     [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
                for loc in parsed.affected_locations:
                    print(f"       - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r}")
                continue

            process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                                     source_url=href)


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
