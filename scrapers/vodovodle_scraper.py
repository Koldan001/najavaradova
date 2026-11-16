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

# JKP "Vodovod" Leskovac. One evergreen page, edited in place. Normally reads
# "Водоснабдевање DD.MM.YYYY. - Водоснабдевање града и околине је уредно." and only lists
# streets when something is actually out. Very low volume.
STATUS_URL = "https://www.vodovodle.rs/servisne-informacije.php"
# A short UA ("Mozilla/5.0", with or without a suffix) gets HTTP 403 here - their firewall blocks
# incomplete browser signatures (found 23.09.2026, the scraper had been silently empty). A full
# desktop-browser UA string is served normally. One request per pipeline cycle, as before.
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")}
REQUEST_DELAY = 1.0
OPSTINA = "ЛЕСКОВАЦ"

_DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
# Phrases that mean "nothing is wrong" - skip when present and no street/outage language is.
_ALL_CLEAR_RE = re.compile(r"уредно|нормал|без\s+прекида|нема\s+прекида", re.IGNORECASE)
_OUTAGE_RE = re.compile(r"без\s+воде|прекид|искључ|квар|хавариј|редукц|у\s+улиц", re.IGNORECASE)


def run_scraper(dry_run: bool = False):
    print(f"Scraping Leskovac water status from: {STATUS_URL}\n")

    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(STATUS_URL, headers=HEADERS, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"❌ Could not fetch page: {e}")
        return

    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.extract()

    # The status block mentions "Водоснабдевање" with a date. Take the SMALLEST element that
    # does - the page wrapper matches too, but only its inner status div is the real notice.
    candidates = [
        el.get_text(" ", strip=True)
        for el in soup.find_all(["div", "p", "section"])
        if "Водоснабдевање" in el.get_text(" ", strip=True) and _DATE_RE.search(el.get_text(" ", strip=True))
    ]
    candidates = [c for c in candidates if "НАСЛОВНА" not in c and 20 < len(c) < 1500]
    if not candidates:
        print("⚠️ Could not locate the status block")
        return
    block = min(candidates, key=len)

    m = _DATE_RE.search(block)
    date_str = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{m.group(3)}" if m else None
    raw_text = f"Лесковац, обавештење за дан {date_str}.\n{block}" if date_str else f"Лесковац.\n{block}"
    # Hashed before the all-clear check on purpose: the hash call is how core/source_health.py
    # counts "found a status block this run". An all-clear day is still a healthy page - only a
    # page where the block can't be located at all should read as silent.
    text_hash = compute_text_hash(raw_text)

    if _ALL_CLEAR_RE.search(block) and not _OUTAGE_RE.search(block):
        print(f"✅ Supply normal — nothing to record\n   {block[:120]}")
        return

    if is_hash_processed(text_hash):
        print("(already recorded this status)")
        return

    print(f"\n🔥 LESKOVAC WATER ({date_str or 'no date'})")
    print(f"   ▸ {block[:120]}...")
    parsed = parse_announcement(raw_text)
    if not parsed:
        print("   ↳ parser returned nothing, skipping")
        return

    parsed.city = OPSTINA
    if date_str:
        use_scraper_date(parsed, date_str, raw_text)
    drop_unknown_settlements(parsed, OPSTINA)

    if dry_run:
        from core.matchmaker import calculate_outage_hexes
        hexes, quality = calculate_outage_hexes(parsed, raw_text)
        print(f"   [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
        for loc in parsed.affected_locations:
            print(f"     - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r}")
        return

    process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                             source_url=STATUS_URL)


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
