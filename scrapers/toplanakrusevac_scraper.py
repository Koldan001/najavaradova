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

# ЈКП "Градска топлана" Крушевац runs a standard WordPress site with the REST API left on -
# confirmed live, no auth needed. Using it instead of scraping rendered HTML sidesteps a real
# trap: the site's block theme renders a "Новости" (related posts) block on every single-post
# page using the SAME wp-block-post markup as the real article, so a naive HTML scrape of one
# article page pulls in 4-5 unrelated neighbouring posts' text along with it. The REST API
# returns exactly one post's own content per item, no related-posts bleed.
API_URL = "https://toplanakrusevac.rs/wp-json/wp/v2/posts"

# 606 posts total on the site (checked live) - most of the "вести" feed here is routine PR, not
# outage-shaped: director TV interviews, anniversary events, and near-daily "Испорука топлотне
# енергије одвија се без проблема" (supply proceeding without issues) status posts. Bounding to
# recent ones by the API's own structured `date` field (not a regex guess) keeps a cold start
# from parsing the entire history.
MAX_POST_AGE_DAYS = 14

# Cheap pre-filter before ever calling Gemini: skip anything that doesn't even mention a
# technical/works word, so the daily "director gave a TV interview" / "no problems today" /
# anniversary posts never reach the parser at all. Deliberately broad (better to waste a Gemini
# call on an ambiguous "supply is fine" post than to silently skip a real one) - the actual
# HEATING vs WORKS decision happens after parsing, on _is_supply_cut below.
_TECHNICAL_MARKERS = (
    "радов", "интервенциј", "квар", "искључењ", "прекид", "снабдевањ", "оштећењ",
    "санациј", "реконструкциј", "поремећај", "без грејањ", "без топле",
)

# Same reasoning and same phrase set as ns_toplana_scraper.py's _is_supply_cut (ded84e4, earlier
# this session) - identical utility type and language, deliberately kept as a local copy rather
# than a cross-scraper import (see vodovodks_scraper.py's docstring for why: two small
# city-specific scrapers, not worth a shared module for one function).
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


def _looks_technical(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _TECHNICAL_MARKERS)


def _post_date(post: dict) -> datetime | None:
    # WP REST gives "2026-08-31T09:05:36" (site-local, no offset) - date portion only matters.
    raw = (post.get("date") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def run_scraper():
    print(f"Scraping Kruševac Toplana announcements from: {API_URL}\n")
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(
            API_URL,
            params={"per_page": 30, "_fields": "id,date,slug,link,title,content"},
            headers=headers, timeout=25,
        )
        posts = response.json()
    except Exception as e:
        print(f"❌ Scraper error: {e}")
        return

    if not isinstance(posts, list):
        print(f"❌ Unexpected response shape from {API_URL}: {type(posts)}")
        return

    for post in posts:
        post_dt = _post_date(post)
        if post_dt and (datetime.now() - post_dt).days > MAX_POST_AGE_DAYS:
            continue

        title = BeautifulSoup(post.get("title", {}).get("rendered", ""), "html.parser").get_text(" ", strip=True)
        body_html = post.get("content", {}).get("rendered", "")
        body = BeautifulSoup(body_html, "html.parser").get_text(" ", strip=True)
        combined = f"{title}. {body}".strip()

        if len(combined) < 30 or not _looks_technical(combined):
            continue

        source_url = post.get("link") or f"https://toplanakrusevac.rs/?p={post.get('id')}"

        raw_text = combined
        if post_dt:
            raw_text = f"Датум: {post_dt.strftime('%d.%m.%Y')}. {raw_text}"

        text_hash = compute_text_hash(raw_text)
        if is_hash_processed(text_hash):
            continue

        print(f"\n🔥 NEW TOPLANA KRUŠEVAC ANNOUNCEMENT: {source_url}")
        parsed = parse_announcement(raw_text)
        if not parsed:
            continue
        # Every toplanakrusevac.rs announcement is Kruševac's district heating by construction -
        # same reasoning as ns_toplana_scraper.py overriding city to НОВИ САД for its own utility.
        parsed.city = "КРУШЕВАЦ"
        if post_dt:
            use_scraper_date(parsed, post_dt.strftime("%d.%m.%Y"), raw_text)
        category = "HEATING" if _is_supply_cut(combined) else "WORKS"
        process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category=category,
                                 source_url=source_url)


if __name__ == "__main__":
    run_scraper()
