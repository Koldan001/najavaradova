import sys
import os
from datetime import datetime

import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed
from scrapers._common import use_scraper_date

# ЈКП "Водовод-Крушевац" runs its info-servis page as a client-rendered Next.js app - the HTML
# response carries no announcement text at all, it's fetched by the browser's own JS after load.
# Found the real data source by reading the page's own JS bundle: a plain JSON API, no auth,
# same origin. Confirmed live: GET returns [{_id, id, date, content, ...}, ...], newest last?
# (id increases monotonically; not assumed sorted either way - see MAX_DOC_AGE_DAYS below).
# The utility moved to vodovodks.rs around 31.10.2026: the old www.vodovodks.co.rs host now
# 301s every path to the new home page, so the API URL no longer returned JSON. Same app, same
# /api/info-servis path, same JSON shape.
API_URL = "https://vodovodks.rs/api/info-servis"
PAGE_URL = "https://vodovodks.rs/sr/informations/info-servis"
# The new host's firewall answers a bare "Mozilla/5.0" with 406 Not Acceptable; a full browser
# User-Agent gets the JSON.
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")}

# The API has no date filter and returns the entire history (267 docs as of 2026-09-11, going
# back well over a year) - processing all of it on a cold start would fire a Gemini parse call
# per outage-shaped paragraph across the whole archive, easily blowing the 500/day free-tier
# budget in one run. Every doc carries a real, structured `date` field (ISO "YYYY-MM-DD", not a
# regex guess off Cyrillic prose like ns_toplana needs) - use it to skip anything old outright.
MAX_DOC_AGE_DAYS = 14

# Confirmed real case (2026-09-11, doc id 275): one day's digest packs FIVE separate, unrelated
# street-level cuts into one doc's `content` (HTML, one <p> per cut) - same shape as vikns.rs's
# daily digest. Split by paragraph, not by doc, so each cut gets its own map zone/timeline
# instead of one city-wide blob. Markers match NS_water_scraper.py's _looks_like_outage_para -
# same language, same domain, deliberately kept in sync rather than imported cross-scraper (two
# small city-specific scrapers, not worth a shared module for one constant tuple).
_OUTAGE_PARA_MARKERS = (
    "без воде", "неће имати вод", "обустав", "прекид",
    "bez vode", "neće imati vod", "nece imati vod", "obustav", "prekid",
)


def _looks_like_outage_para(text: str) -> bool:
    low = (text or "").lower()
    return len(low) >= 40 and any(m in low for m in _OUTAGE_PARA_MARKERS)


def _doc_date(doc: dict) -> datetime | None:
    try:
        return datetime.strptime((doc.get("date") or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _process_item(body_text: str, source_url: str, doc_dt: datetime | None):
    """Parse one outage body - a whole digest doc, or a single paragraph split out of one - dedup
    it on its own hash, and queue it. Unlike ns_toplana.rs, the date here is never regex-guessed
    from prose: it comes straight from the API's own structured `date` field, so there's no drift
    source to manufacture a duplicate outage_id the way ns_toplana's extracted date once did
    (see that scraper's _source_url_already_stored for the incident this avoided by construction:
    each paragraph's hash is stable run to run because the prefix it's built from never changes)."""
    raw_text = body_text.strip()
    if len(raw_text) < 30:
        return

    if doc_dt:
        raw_text = f"Датум: {doc_dt.strftime('%d.%m.%Y')}. {raw_text}"

    text_hash = compute_text_hash(raw_text)
    if is_hash_processed(text_hash):
        return

    parsed = parse_announcement(raw_text)
    if not parsed:
        return
    # Every vodovodks.rs announcement is Kruševac's water utility by construction - same
    # reasoning as NS_water_scraper.py overriding city to НОВИ САД.
    parsed.city = "КРУШЕВАЦ"
    if doc_dt:
        use_scraper_date(parsed, doc_dt.strftime("%d.%m.%Y"), raw_text)
    process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                             source_url=source_url)


def run_scraper():
    print(f"Scraping Kruševac Vodovod announcements from: {API_URL}\n")
    try:
        response = requests.get(API_URL, headers=HEADERS, timeout=25)
        docs = response.json()
    except Exception as e:
        print(f"❌ Scraper error: {e}")
        return

    if not isinstance(docs, list):
        print(f"❌ Unexpected response shape from {API_URL}: {type(docs)}")
        return

    for doc in docs:
        doc_dt = _doc_date(doc)
        if doc_dt and (datetime.now() - doc_dt).days > MAX_DOC_AGE_DAYS:
            continue

        content = (doc.get("content") or "").strip()
        if len(content) < 30:
            continue

        doc_id = doc.get("id") if doc.get("id") is not None else doc.get("_id")
        # Synthetic per-doc anchor into the real page - there's no individual per-announcement
        # URL on this site (everything lives in one client-rendered list), so this is the closest
        # real "Zvanična objava" link a user can follow; it lands on the right page either way.
        source_url = f"{PAGE_URL}#{doc_id}"

        soup = BeautifulSoup(content, "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")
                      if p.get_text(strip=True)]
        if not paragraphs:
            # Some older docs may not wrap content in <p> tags at all - fall back to the whole
            # block rather than silently dropping it.
            whole = soup.get_text(" ", strip=True)
            if whole:
                paragraphs = [whole]

        if not paragraphs:
            continue

        items = [p for p in paragraphs if _looks_like_outage_para(p)]

        if len(items) >= 2:
            print(f"\n🔥 KRUŠEVAC WATER MULTI-ITEM ({len(items)} item(s)): {source_url}")
            for para in items:
                _process_item(para, source_url, doc_dt)
        else:
            body_text = " ".join(paragraphs).strip()
            if len(body_text) < 30:
                continue
            print(f"\n🔥 KRUŠEVAC WATER ANNOUNCEMENT: {source_url}")
            _process_item(body_text, source_url, doc_dt)


if __name__ == "__main__":
    run_scraper()
