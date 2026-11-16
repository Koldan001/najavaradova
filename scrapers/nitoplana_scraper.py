import re
import sys
import os
from datetime import datetime

import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.database import get_connection
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed
from scrapers._common import use_scraper_date

# ЈКП "Градска топлана" Ниш - a custom (non-WordPress) site, no REST API found. Confirmed real
# title examples: "Обавештење о радовима у улици Генерала Боже Јанковића", "Извођење радова на
# топловоду - Зетска" - genuine street-level works notices, not a mixed news feed the way
# nstoplana/toplanakrusevac are, so there is no PR-filler pre-filter needed here.
#
# Site redesign (found 23.09.2026): the old list page /news answers 404 and articles moved from
# numbered permalinks (news/220) to slugs (/posts/obavestenje-o-radovima-u-...). The works/supply
# notices now have their own category page; its first page carries the ~15 newest.
LIST_URL = "https://nitoplana.rs/posts/categories/obavestenja-o-radovima-i-prekidima-u-snabdevanju"
BASE_URL = "https://nitoplana.rs/"

# Article body after the redesign: the only <div class="leading-relaxed ..."> on a post page.
# The pre-redesign <div class="body"> is kept as a fallback in _article_body. Both hold
# Word-pasted, malformed nesting - .get_text() on the whole div picks up every nested paragraph.
_CONTENT_CLASSES = ("leading-relaxed", "body")


def _is_article_link(href: str) -> bool:
    return "/posts/" in href and "/categories/" not in href


def _article_body(soup) -> str:
    for cls in _CONTENT_CLASSES:
        div = soup.find("div", class_=cls)
        if div:
            return div.get_text(" ", strip=True)
    return ""

# Confirmed real: "20.08.2026.год." - numeric date directly in prose, no word-months to parse
# here (unlike ns_toplana.rs). Loose on internal whitespace as a defensive habit (nstoplana's
# prose sprays stray spaces mid-token; not confirmed here, but costs nothing to tolerate).
_DATE_RE = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d(?:[\d ]{0,4})\d|\d{4})")


def _extract_date(text: str) -> str | None:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    d, mth, y = m.groups()
    y = re.sub(r"\s+", "", y)
    if len(y) != 4:
        return None
    try:
        datetime(int(y), int(mth), int(d))
    except ValueError:
        return None
    return f"{int(d):02d}.{int(mth):02d}.{y}"


# Same phrase set and same reasoning as ns_toplana_scraper.py's _is_supply_cut and
# toplanakrusevac_scraper.py's local copy - identical utility type and language.
_SUPPLY_CUT_PHRASES = (
    "без топле воде", "без грејањ", "без греја", "без тпв",
    "неће имати топлу воду", "неће имати грејање", "неће бити топле воде",
    "неће бити грејања", "нема испоруке топле воде", "без редовног снабдевања",
    "нема грејања",
)
_CUT_VERBS = ("прекид", "обустав", "поремећај", "искључењ")
_SUPPLY_OBJECTS = ("испоруц", "испорук", "топле воде", "топлотне енергије", "грејањ", "тпв", "снабдевањ")


def _is_supply_cut(text: str) -> bool:
    low = (text or "").lower()
    if any(p in low for p in _SUPPLY_CUT_PHRASES):
        return True
    for verb in _CUT_VERBS:
        i = low.find(verb)
        while i != -1:
            if any(obj in low[i:i + 60] for obj in _SUPPLY_OBJECTS):
                return True
            i = low.find(verb, i + 1)
    return False


def _source_url_already_stored(article_url: str) -> bool:
    """True if this exact article URL already backs a stored outage. Numbered permalinks here
    look immutable (unlike ns_toplana.rs, which is confirmed to re-touch/re-edit live posts) -
    still guarding on source_url rather than raw_text_hash anyway, same reasoning as
    NS_water_scraper.py and the ns_toplana.rs fix from earlier tonight: it is what makes a
    content-drift-driven duplicate outage_id structurally impossible here, not just unlikely."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT 1 FROM outages WHERE source_url = ? LIMIT 1", (article_url,)
        ).fetchone() is not None
    finally:
        conn.close()


def run_scraper():
    print(f"Scraping Niš Toplana announcements from: {LIST_URL}\n")
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(LIST_URL, headers=headers, timeout=25)
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        print(f"❌ Scraper error: {e}")
        return

    seen_urls = set()
    for a in soup.find_all("a", href=lambda h: h and _is_article_link(h)):
        href = a["href"]
        article_url = href if href.startswith("http") else BASE_URL + href.lstrip("/")
        if article_url in seen_urls:
            continue
        seen_urls.add(article_url)

        if _source_url_already_stored(article_url):
            continue

        try:
            article_res = requests.get(article_url, headers=headers, timeout=25)
            article_soup = BeautifulSoup(article_res.text, "html.parser")
            body_text = _article_body(article_soup)
        except Exception as e:
            print(f"⚠️ Could not fetch {article_url}: {e}")
            continue

        if len(body_text) < 30:
            continue

        date_str = _extract_date(body_text)
        raw_text = body_text
        if date_str:
            raw_text = f"Датум: {date_str}. {raw_text}"

        # A note on images: some real nitoplana.rs posts illustrate the affected zone with a
        # circled map graphic instead of (or in addition to) naming every street in prose. This
        # scraper is text-only - a post whose ONLY location detail is in the image will fail to
        # parse a street (parse_announcement returns nothing usable) and nothing gets queued.
        # Safe failure direction (under-notifies, never mis-notifies), but a real coverage gap
        # worth knowing about, not silently "handled".
        text_hash = compute_text_hash(raw_text)
        if is_hash_processed(text_hash):
            continue

        print(f"\n🔥 NEW NIŠ TOPLANA ANNOUNCEMENT: {article_url}")
        parsed = parse_announcement(raw_text)
        if not parsed:
            continue
        parsed.city = "НИШ"
        if date_str:
            use_scraper_date(parsed, date_str, raw_text)
        category = "HEATING" if _is_supply_cut(raw_text) else "WORKS"
        process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category=category,
                                 source_url=article_url)


if __name__ == "__main__":
    run_scraper()
