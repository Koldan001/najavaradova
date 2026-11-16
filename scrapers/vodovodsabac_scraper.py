import re
import sys
import os
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed
from scrapers._common import drop_unknown_settlements, published_line

# JKP "Vodovod Šabac" - water for Šabac and its villages (Jevremovac, Majur, Varna, Mala and Velika
# Vranjska, ...). WordPress with the REST API open, Cyrillic, every post in one category next to
# holiday hours and investment news, so the cuts are picked out by their wording. About 1-3 a
# month (2025-2026), each one post: a street or a stretch of it, a date or "сутра", hours.
# Added 27.10.2026 for a new subscriber in Šabac. Heating (JKP "Toplana Šabac") posts only prices
# and the season start, never an interruption, so it isn't scraped.
API_URL = "https://vodovodsabac.rs/wp-json/wp/v2/posts"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
OPSTINA = "ШАБАЦ"

# A notice is about today or the next day or two; older posts are history.
MAX_POST_AGE_DAYS = 7

# Only posts that say water stops reach the parser, in all the ways this utility has said it:
# "искључимо воду", "без воде" (also typed "без вооде"), "прекид (у) водоснабдевању/а",
# "обустава воде", "затворена вода", "неће имати воду", "без снабдевања пијаћом водом".
# A drop in pressure, a power cut at the source with water still running ("Престанка снабдевања
# водом неће бити"), holiday hours and investments mention water without anyone losing it.
_CUT = re.compile(r"без\s+во+де|искључ\w*\s+вод\w*|(прекид\w*|обустав\w*)\s+(у\s+)?(водоснабдевањ\w*|"
                  r"испоруц\w*\s+воде|воде)|затворен\w*\s+вод\w*|неће\s+имати\s+вод\w*|"
                  r"без\s+снабдевањ\w*\s+(пијаћ\w*\s+)?вод\w*", re.IGNORECASE)
# The page builder's leftovers in the post body: [vc_row ...] shortcodes and a gallery's CSS.
_BUILDER = re.compile(r"\[/?vc_[^\]]*\]|\.gallery-carousel.*", re.DOTALL)


def _post_date(post: dict) -> datetime | None:
    try:
        return datetime.strptime((post.get("date") or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _text(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return " ".join(_BUILDER.sub(" ", text).split())


def _is_cut(title: str, body: str) -> bool:
    return bool(_CUT.search(f"{title} {body}"))


def _raw_text(title: str, body: str, posted: datetime | None) -> str:
    # "данас", "сутра" and "у току сутрашњег дана" in the body are relative to the publish date.
    return f"{published_line('Шабац', posted)}\n{title}\n{body}"


def run_scraper(dry_run: bool = False, days: int = MAX_POST_AGE_DAYS):
    print(f"Scraping Šabac water announcements from: {API_URL}\n")
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
        url = post.get("link") or f"https://vodovodsabac.rs/?p={post.get('id')}"
        notices.append((raw_text, compute_text_hash(raw_text), url))

    # Two notices about the same street on the same day are siblings, never one's update.
    live_hashes = {h for _, h, _ in notices}

    for raw_text, text_hash, url in notices:
        if is_hash_processed(text_hash):
            continue
        print(f"\n🔥 NEW ŠABAC WATER ANNOUNCEMENT: {url}")
        parsed = parse_announcement(raw_text)
        if not parsed:
            print("   ↳ parser returned nothing, skipping")
            continue
        parsed.city = OPSTINA
        drop_unknown_settlements(parsed, OPSTINA)

        if dry_run:
            from core.matchmaker import calculate_outage_hexes
            hexes, quality = calculate_outage_hexes(parsed, raw_text)
            print(f"   [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
            for loc in parsed.affected_locations:
                print(f"     - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r} "
                      f"numbers={loc.explicit_numbers or loc.number_range!r} time={loc.time_window!r}")
            continue

        process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                                 source_url=url, live_hashes=live_hashes)


if __name__ == "__main__":
    # --dry-run [--days N]: parse and print, write nothing; --days reaches further back to test.
    _days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else MAX_POST_AGE_DAYS
    run_scraper(dry_run="--dry-run" in sys.argv, days=_days)
