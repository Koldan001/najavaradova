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

# JP "Vodovod" Vranje. WordPress. One post per day in the "Обавештења" category, permalink
# /DD-MM-YYYY-obavestenje-o-prekidu-vodosnabdevanja/, body one <p> per affected street or
# village, phrased "...у улици/селу X ... те улице/села и околних, до завршетка радова."
LIST_URL = "https://vodovodvranje.rs/category/%D0%BE%D0%B1%D0%B0%D0%B2%D0%B5%D1%88%D1%82%D0%B5%D1%9A%D0%B0/"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
REQUEST_DELAY = 1.0
OPSTINA = "ВРАЊЕ"

_SLUG_DATE_RE = re.compile(r"/(\d{1,2})-(\d{1,2})-(\d{4})-obavestenje")
_TITLE_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

# Standing boilerplate paragraphs that carry no location.
_NOISE_MARKERS = (
    "моле се потрошачи", "обавештење потрошачима", "не користе воду за пиће",
    "препоручује корисницима", "руководилац", "службе одржавања", "ситуациони центар",
)


def _extract_date(url: str, title: str) -> str | None:
    m = _SLUG_DATE_RE.search(url)
    if m:
        d, mth, y = m.groups()
        return f"{int(d):02d}.{int(mth):02d}.{y}"
    m = _TITLE_DATE_RE.search(title or "")
    if m:
        d, mth, y = m.groups()
        return f"{int(d):02d}.{int(mth):02d}.{y}"
    return None


def _body_text(content) -> str:
    keep = []
    for p in content.find_all(["p", "li"]):
        t = p.get_text(" ", strip=True)
        if not t or len(t) < 12:
            continue
        low = t.lower()
        if any(m in low for m in _NOISE_MARKERS):
            continue
        keep.append(t)
    return "\n".join(keep).strip()


def run_scraper(dry_run: bool = False):
    print(f"Scraping Vranje water announcements from: {LIST_URL}\n")

    try:
        time.sleep(REQUEST_DELAY)
        soup = BeautifulSoup(requests.get(LIST_URL, headers=HEADERS, timeout=20).text, "html.parser")
    except Exception as e:
        print(f"❌ Could not fetch feed: {e}")
        return

    seen = set()
    for a in soup.select('a[href*="-obavestenje-o-prekidu-vodosnabdevanja"]'):
        article_url = a["href"].split("#")[0]
        if article_url in seen:
            continue
        seen.add(article_url)

        try:
            time.sleep(REQUEST_DELAY)
            art_soup = BeautifulSoup(requests.get(article_url, headers=HEADERS, timeout=15).text, "html.parser")
            content = art_soup.select_one(".entry-content") or art_soup.select_one("main")
        except Exception as e:
            print(f"⚠️ Could not fetch {article_url}: {e}")
            continue
        if not content:
            continue

        title = ""
        h = art_soup.select_one("h1.entry-title, h1")
        if h:
            title = h.get_text(" ", strip=True)

        body = _body_text(content)
        if len(body) < 30 or "вод" not in body.lower():
            continue

        date_str = _extract_date(article_url, title)
        raw_text = f"Врање, обавештење за дан {date_str}.\n{body}" if date_str else f"Врање.\n{body}"

        text_hash = compute_text_hash(raw_text)
        if is_hash_processed(text_hash):
            continue

        print(f"\n🔥 NEW VRANJE WATER ANNOUNCEMENT ({date_str or 'no date'}): {article_url}")
        parsed = parse_announcement(raw_text)
        if not parsed:
            print("   ↳ parser returned nothing, skipping")
            continue

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
                      f"surrounding={loc.includes_surrounding_streets}")
            continue

        process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                                 source_url=article_url)


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
