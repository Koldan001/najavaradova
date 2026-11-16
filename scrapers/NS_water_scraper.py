import re
import sys
import os
from datetime import datetime

import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import process_and_queue_outage
from core.database import get_connection
from scrapers.eps_scraper import compute_text_hash, is_hash_processed
from scrapers._common import use_scraper_date

# Confirmed for real: this category page is NOT "latest N posts" - it's the site's entire
# history on one URL (487 unique article links on a single fetch, dated back to 2024). Every
# other scraper's source is naturally bounded (today+a few days, or one page of ~10-20 latest);
# this one has nothing built in. 480 of those 487 links carry their own date right in the slug
# (".../najava-radova-za-cetvrtak-11-6-2026/") - cheap to check before ever fetching the article,
# unlike vodovodkv_scraper.py's equivalent problem where the date only showed up after OCR.
_SLUG_DATE_RE = re.compile(r"-(\d{1,2})-(\d{1,2})-(\d{4})")
MAX_ARTICLE_AGE_DAYS = 21


def _slug_date(url: str) -> datetime | None:
    """The outage date, taken from the slug (".../najava-radova-za-cetvrtak-10-9-2026/").
    None for the ~7 of 487 slugs with no extractable date (numeric-ID permalinks, a typo'd
    year)."""
    m = _SLUG_DATE_RE.search(url)
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _slug_is_stale(url: str) -> bool:
    """A slug with no extractable date is NOT stale by this check - falls through to the normal
    fetch+hash-dedup path instead, same as before this existed."""
    posted = _slug_date(url)
    return posted is not None and (datetime.now() - posted).days > MAX_ARTICLE_AGE_DAYS


# vikns routinely packs SEVERAL unrelated water cuts into one post - both the daily
# "Сервисне информације" digest AND ordinary "najava-radova-za-<dan>" articles. The items sit
# 10+ km apart, each with its own time window; stored as one outage they draw a city-spanning
# map zone and a meaningless merged timeline. Real case: outage #1153 - "Ул. Биће Кесића … у
# Степановићеву" (do 15h) + "ул. Владимира Назора и Новосадска у Сремској Каменици" (8-16h),
# ~30 km apart, one row. Each item is its own <p> containing "без воде" / "bez vode"; the
# trailing "Постоји могућност да ће … блокада … продужена" line is a standing caveat, not an
# item. Detection is content-based (>=2 outage paragraphs), NOT slug-based - the merge happens
# on plain najava-radova URLs too.
_OUTAGE_PARA_MARKERS = (
    "без воде", "неће имати вод", "обустав", "прекид",
    "bez vode", "neće imati vod", "nece imati vod", "obustav", "prekid",
)


def _looks_like_outage_para(text: str) -> bool:
    low = (text or "").lower()
    return len(low) >= 40 and any(m in low for m in _OUTAGE_PARA_MARKERS)


def _source_url_already_stored(article_url: str) -> bool:
    """True if this exact article URL already backs a stored outage. Used only on the split
    path: an article ingested BEFORE content-split shipped is stored as one MERGED row, and
    re-ingesting it now as N split rows (new outage ids) would re-notify everyone the merged row
    already told - the (outage_id, target_id, channel) dedup can't see across a new id. Hard no,
    especially with real subscribers live. The merged row expires on its own; only genuinely new
    multi-item articles (no row yet) get split. link["href"] is byte-identical run to run (same
    page), so an exact match is reliable here."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT 1 FROM outages WHERE source_url = ? LIMIT 1", (article_url,)
        ).fetchone() is not None
    finally:
        conn.close()


def _process_item(body_text: str, article_url: str, slug_dt):
    """Parse one outage body - a whole normal announcement, or a single paragraph of a
    "Сервисне информације" digest - dedup it on its own hash, and queue it.

    Every vikns announcement is Novi Sad's water utility (Novi Sad / Petrovaradin / Sremska
    Kamenica / Sremski Karlovci) by construction, and the slug's date is authoritative - the
    body almost never carries one, so a body-only parse hallucinated it (outage #1007).
    """
    raw_text = body_text.strip()
    if len(raw_text) < 30:
        return

    if slug_dt:
        raw_text = f"Датум: {slug_dt.strftime('%d.%m.%Y')}. {raw_text}"

    # This scraper had NO dedup at all before - every run re-parsed (Gemini call included) every
    # article still on the category page's first page. Guard it the same way as eps_scraper.py.
    # Per-item hashing also means a digest's three paragraphs dedup independently.
    text_hash = compute_text_hash(raw_text)
    if is_hash_processed(text_hash):
        return

    parsed = parse_announcement(raw_text)
    if not parsed:
        return
    parsed.city = "НОВИ САД"
    if slug_dt:
        use_scraper_date(parsed, slug_dt.strftime("%d.%m.%Y"), raw_text)
    process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                             source_url=article_url)


def run_scraper():
    url = "https://www.vikns.rs/category/najava-radova-sr/?pismo=cir"
    print(f"Scraping NS Water announcements from: {url}\n")
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        # vikns.rs is genuinely slow - confirmed a real fetch taking ~11s. timeout=10 here meant
        # this scraper missed its own site's announcements on a normal response, not just a
        # dead/overloaded one. 25s gives real slack without hanging forever on an actually-down site.
        response = requests.get(url, headers=headers, timeout=25)
        soup = BeautifulSoup(response.text, "html.parser")
        processed_urls = set()

        for link in soup.find_all("a", href=True):
            article_url = link["href"]
            if "najava-radova-sr" not in article_url or article_url in processed_urls:
                continue
            if any(k in article_url for k in ["category", "page/", "?pismo="]):
                continue
            if _slug_is_stale(article_url):
                continue

            processed_urls.add(article_url)

            try:
                article_res = requests.get(article_url, headers=headers, timeout=25)
                article_soup = BeautifulSoup(article_res.text, "html.parser")
                paragraphs = [p.get_text(" ", strip=True)
                              for p in article_soup.find_all("p", class_="wp-block-paragraph")
                              if p.get_text(strip=True)]
            except Exception as e:
                print(f"⚠️ Could not fetch {article_url}: {e}")
                continue

            if not paragraphs:
                continue

            slug_dt = _slug_date(article_url)
            items = [p for p in paragraphs if _looks_like_outage_para(p)]

            if len(items) >= 2:
                # Multi-location post: each "…без воде…" paragraph is a separate outage,
                # kilometres apart, with its own window. One row per paragraph so each gets its
                # own map zone and timeline instead of one city-wide blob.
                if _source_url_already_stored(article_url):
                    print(f"   ↳ already ingested as one merged row (pre-split), skipping to "
                          f"avoid re-notify: {article_url}")
                    continue
                print(f"\n🔥 NEW WATER MULTI-ITEM ({len(items)} item(s)): {article_url}")
                for para in items:
                    _process_item(para, article_url, slug_dt)
            else:
                body_text = " ".join(paragraphs).strip()
                if len(body_text) < 30:
                    continue
                print(f"\n🔥 NEW WATER ANNOUNCEMENT: {article_url}")
                _process_item(body_text, article_url, slug_dt)

    except Exception as e:
        print(f"❌ Scraper error: {e}")

if __name__ == "__main__":
    run_scraper()