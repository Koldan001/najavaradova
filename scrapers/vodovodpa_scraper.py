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

OPSTINA = "ПАНЧЕВО"

# JKP "Vodovod i kanalizacija" Pančevo publishes one WordPress post per day in the
# "Кварови и искључења" category, titled DD/M/YYYY, body a single Cyrillic paragraph listing
# the affected streets (often with "на потезу од X до Y" bounds).
LIST_URL = ("https://www.vodovodpa.rs/category/"
            "%d0%ba%d0%b2%d0%b0%d1%80%d0%be%d0%b2%d0%b8-%d0%b8%d1%81%d0%ba%d1%99%d1%83%d1%87%d0%b5%d1%9a%d0%b0/")
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
REQUEST_DELAY = 1.0

# Post permalinks are /YYYY/MM/DD/<slug>/ - the date lives right there, no need to trust the
# body (which never repeats it) or the model.
_SLUG_DATE_RE = re.compile(r"/vodovodpa\.rs/(\d{4})/(\d{2})/(\d{2})/")
_TITLE_DATE_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})")


def _extract_date(article_url: str, title: str) -> str | None:
    m = _SLUG_DATE_RE.search(article_url)
    if m:
        y, mth, d = m.groups()
        return f"{int(d):02d}.{int(mth):02d}.{y}"
    m = _TITLE_DATE_RE.search(title or "")
    if m:
        d, mth, y = m.groups()
        return f"{int(d):02d}.{int(mth):02d}.{y}"
    return None


def run_scraper(dry_run: bool = False):
    print(f"Scraping Pančevo water announcements from: {LIST_URL}\n")

    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(LIST_URL, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        print(f"❌ Could not fetch feed: {e}")
        return

    processed_urls = set()
    for card in soup.find_all("article", id=lambda v: v and v.startswith("post-")):
        link = card.select_one("h1.entry-title a[href], h2.entry-title a[href], .entry-title a[href]")
        if not link:
            continue
        article_url = link["href"]
        title = link.get_text(" ", strip=True)
        if article_url in processed_urls:
            continue
        processed_urls.add(article_url)

        try:
            time.sleep(REQUEST_DELAY)
            article_soup = BeautifulSoup(
                requests.get(article_url, headers=HEADERS, timeout=15).text, "html.parser"
            )
            article_tag = article_soup.find("article", id=lambda v: v and v.startswith("post-"))
            content_div = (article_tag.select_one(".entry-content") if article_tag else None) \
                or article_soup.select_one(".entry-content")
        except Exception as e:
            print(f"⚠️ Could not fetch {article_url}: {e}")
            continue

        if not content_div:
            print(f"⚠️ No content body at {article_url}")
            continue

        body = content_div.get_text(" ", strip=True)
        if len(body) < 40:
            continue

        date_str = _extract_date(article_url, title)
        raw_text = f"Обавештење за дан {date_str}.\n{body}" if date_str else body

        text_hash = compute_text_hash(raw_text)
        if is_hash_processed(text_hash):
            continue

        print(f"\n🔥 NEW PANČEVO WATER ANNOUNCEMENT ({date_str or 'no date'}): {article_url}")
        parsed = parse_announcement(raw_text)
        if not parsed:
            print("   ↳ parser returned nothing, skipping")
            continue

        # This scraper only ever reads vodovodpa.rs - JKP "Vodovod i kanalizacija" Pančevo serves
        # the Pančevo opština only, so every announcement here IS Pančevo by construction.
        parsed.city = OPSTINA
        if date_str:
            use_scraper_date(parsed, date_str, raw_text)
        drop_unknown_settlements(parsed, OPSTINA)

        if dry_run:
            from core.matchmaker import calculate_outage_hexes
            hexes, quality = calculate_outage_hexes(parsed, raw_text)
            print(f"   [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
            for loc in parsed.affected_locations:
                print(f"     - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r} "
                      f"bounds=({loc.intersection_start},{loc.intersection_end})")
            continue

        process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                                 source_url=article_url)


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
