import sys
import os
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed
from scrapers._common import drop_unknown_settlements

# JKP "Vodovod Valjevo". WordPress with the REST API open (no auth), one post per notice in its
# only category, "Vesti" (https://vodovodva.co.rs/category/vesti/). Read through the API, like
# toplanakrusevac_scraper.py, so each item is exactly one post's own text. Busy source: ~70
# posts Jul-Oct 2026, nearly all "Obaveštenje potrošačima" water cuts in the town and the
# villages around it (Gorić, Popučke, Petnica, ... - real RGZ naselja under ВАЉЕВО).
API_URL = "https://vodovodva.co.rs/wp-json/wp/v2/posts"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
OPSTINA = "ВАЉЕВО"

# A notice is about today or the next day or two; older posts are history. Bounded by the API's
# own `date` so a cold start doesn't parse the whole archive (534 posts).
MAX_POST_AGE_DAYS = 7

# Only posts that say someone is without water reach the parser - "nabavili novu cisternu" news
# and "apel za racionalnu potrošnju" appeals don't. Latin script, as the site writes.
_CUT_PHRASES = (
    "bez vodosnabdevanj", "bez vode", "bez snabdevanja vodom", "prekid vodosnabdevanj",
    "prekid u vodosnabdevanj", "isključenje vode", "smanjen pritisak", "slab pritisak",
    # 03.10.2026: "problemi sa vodosnabdevanjem u smislu nižeg pritiska" in parts of the town.
    "nižeg pritiska", "niži pritisak", "problemi sa vodosnabdevanjem",
)
_SKIP_TITLES = ("apel",)
# Trailing captions under the post photo, no location in them.
_NOISE = ("Fotografija predstavlja ilustraciju",)


def _post_date(post: dict) -> datetime | None:
    # "2026-10-12T14:03:11", site-local - only the date matters.
    try:
        return datetime.strptime((post.get("date") or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _text(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    for noise in _NOISE:
        text = text.split(noise)[0]
    return " ".join(text.split())


def _is_cut(title: str, body: str) -> bool:
    if any(s in title.lower() for s in _SKIP_TITLES):
        return False
    low = body.lower()
    return any(p in low for p in _CUT_PHRASES)


def _raw_text(title: str, body: str, posted: datetime | None) -> str:
    # The publish date goes in front: "danas", "večeras" and "sutra" in the body are relative to
    # it, and a notice that names no date ("bez vodosnabdevanja su potrošači...") is about it.
    head = f"Valjevo, objavljeno {posted.strftime('%d.%m.%Y')}." if posted else "Valjevo."
    return f"{head}\n{title}\n{body}"


def run_scraper(dry_run: bool = False, days: int = MAX_POST_AGE_DAYS):
    print(f"Scraping Valjevo water announcements from: {API_URL}\n")
    after = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    try:
        posts = requests.get(
            API_URL, params={"per_page": 50, "after": after, "_fields": "id,date,link,title,content"},
            headers=HEADERS, timeout=25,
        ).json()
    except Exception as e:
        print(f"❌ Scraper error: {e}")
        return
    if not isinstance(posts, list):
        print(f"❌ Unexpected response shape from {API_URL}: {type(posts)}")
        return

    notices = []
    for post in posts:
        title = _text(post.get("title", {}).get("rendered", ""))
        body = _text(post.get("content", {}).get("rendered", ""))
        if len(body) < 30 or not _is_cut(title, body):
            continue
        raw_text = _raw_text(title, body, _post_date(post))
        url = post.get("link") or f"https://vodovodva.co.rs/?p={post.get('id')}"
        notices.append((raw_text, compute_text_hash(raw_text), url))

    # Every notice still on the feed: two of them about the same place on the same day are
    # siblings, never an update of each other (core/matchmaker.py's dedup).
    live_hashes = {h for _, h, _ in notices}

    for raw_text, text_hash, url in notices:
        if is_hash_processed(text_hash):
            continue
        print(f"\n🔥 NEW VALJEVO WATER ANNOUNCEMENT: {url}")
        parsed = parse_announcement(raw_text)
        if not parsed:
            print("   ↳ parser returned nothing, skipping")
            continue
        parsed.city = OPSTINA
        drop_unknown_settlements(parsed, OPSTINA)

        if dry_run:
            from core.matchmaker import calculate_outage_hexes
            hexes, quality = calculate_outage_hexes(parsed, raw_text)
            print(f"   [dry-run] date={parsed.outage_date!r} end={parsed.end_date!r} "
                  f"hexes={len(hexes)} quality={quality}")
            for loc in parsed.affected_locations:
                print(f"     - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r} "
                      f"scope={loc.scope!r} landmark={loc.landmark!r} time={loc.time_window!r}")
            continue

        process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                                 source_url=url, live_hashes=live_hashes)


if __name__ == "__main__":
    # --dry-run [--days N]: parse and print, write nothing; --days reaches further back to test.
    _days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else MAX_POST_AGE_DAYS
    run_scraper(dry_run="--dry-run" in sys.argv, days=_days)
