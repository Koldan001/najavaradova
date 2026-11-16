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

# JKP "Vodovod" Čačak. Two sources:
#   OBAVESTENJA_URL - the news feed. Most posts publish the notice ONLY as a scanned JPG - the
#                     post body is empty - so those are read with Gemini vision (parse_announcement
#                     image= path). Posts that do carry body text are read normally.
#   KVAROVI_URL     - an evergreen "works on the network" page, always plain text.
OBAVESTENJA_URL = "https://vodovodca.rs/category/obavestenja/"
KVAROVI_URL = "https://vodovodca.rs/kvarovi-na-mrezi/"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
REQUEST_DELAY = 1.0
OPSTINA = "ЧАЧАК"

# Feed items that aren't outages (vehicle sales, procurement, tariff decisions, generic notices).
_SKIP_MARKERS = (
    "продаја", "возил", "оглас", "конкурс", "јавна набавка", "јавне набавке", "тендер",
    "ценовник", "цена вод", "рестрикциј",  # water-use restriction decree, not a supply cut
    "ограничењу испоруке", "водосистема „рзав",  # region-wide supply limit, no street detail
)
_OUTAGE_MARKERS = (
    "вод", "квар", "радов", "интервенциј", "раскрсниц", "искључ", "без воде", "снабдевањ",
    "хавариј", "цевовод", "прикључ", "редукован",
)

_DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
# The generic stock thumbnail every post carries; the real notice image is a different upload.
_STOCK_IMG_RE = re.compile(r"(pixabay|logo|placeholder|default|thumb)", re.IGNORECASE)
_NOTICE_IMG_RE = re.compile(r"/wp-content/uploads/\d{4}/\d{2}/.+\.(jpe?g|png)$", re.IGNORECASE)


def _fetch(url: str) -> BeautifulSoup | None:
    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.encoding = r.apparent_encoding or "utf-8"
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"⚠️ Could not fetch {url}: {e}")
        return None


def _looks_like_outage(text: str) -> bool:
    low = (text or "").lower()
    if any(s in low for s in _SKIP_MARKERS):
        return False
    return any(k in low for k in _OUTAGE_MARKERS)


def _date_from(article, title: str) -> str | None:
    # The last <time> is the modified date - right for an evergreen page that gets edited; for a
    # normal one-shot post published and modified are the same.
    times = article.find_all("time", attrs={"datetime": True}) if article else []
    if times:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", times[-1]["datetime"])
        if m:
            y, mth, d = m.groups()
            return f"{d}.{mth}.{y}"
    m = _DATE_RE.search(title or "")
    if m:
        d, mth, y = m.groups()
        return f"{int(d):02d}.{int(mth):02d}.{y}"
    return None


def _notice_image_url(article) -> str | None:
    """The scanned-notice image on a post, skipping the generic stock thumbnail."""
    for img in article.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        src = src.split("?")[0]
        if _STOCK_IMG_RE.search(src):
            continue
        # strip WordPress size suffix so we fetch the full image, not the -724x1024 crop
        full = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)
        if _NOTICE_IMG_RE.search(full) or _NOTICE_IMG_RE.search(src):
            return full
    return None


def _queue(raw_text, image, date_str, text_hash, title, source_url, dry_run):
    parsed = parse_announcement(raw_text, image=image)
    if not parsed:
        print("     ↳ parser returned nothing, skipping")
        return
    parsed.city = OPSTINA
    if date_str:
        use_scraper_date(parsed, date_str, raw_text)
    drop_unknown_settlements(parsed, OPSTINA)

    if dry_run:
        from core.matchmaker import calculate_outage_hexes
        hexes, quality = calculate_outage_hexes(parsed, raw_text)
        print(f"     [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
        for loc in parsed.affected_locations:
            print(f"       - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r} "
                  f"bounds=({loc.intersection_start},{loc.intersection_end})")
        return

    process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER", source_url=source_url)


def _scrape_kvarovi(dry_run: bool):
    soup = _fetch(KVAROVI_URL)
    if not soup:
        return
    art = soup.find("article", id=lambda v: v and v.startswith("post-"))
    content = art.select_one(".entry-content") if art else None
    body = content.get_text(" ", strip=True) if content else ""
    if len(body) < 40 or not _looks_like_outage(body):
        return

    title = art.select_one(".entry-title").get_text(" ", strip=True) if art.select_one(".entry-title") else ""
    date_str = _date_from(art, title)
    raw_text = f"Чачак, обавештење за дан {date_str}.\n{title}. {body}" if date_str else f"Чачак. {title}. {body}"
    text_hash = compute_text_hash(raw_text)
    if is_hash_processed(text_hash):
        return
    print(f"\n🔥 ČAČAK WATER (kvarovi page, {date_str or 'no date'})")
    print(f"   ▸ {body[:90]}...")
    _queue(raw_text, None, date_str, text_hash, title, KVAROVI_URL, dry_run)


def _scrape_obavestenja(dry_run: bool):
    soup = _fetch(OBAVESTENJA_URL)
    if not soup:
        return

    seen = set()
    for card in soup.find_all("article", id=lambda v: v and v.startswith("post-")):
        link = card.select_one(".entry-title a[href], h2 a[href], h3 a[href]")
        if not link:
            continue
        article_url = link["href"]
        title = link.get_text(" ", strip=True)
        if article_url in seen or article_url.rstrip("/") == KVAROVI_URL.rstrip("/"):
            continue  # the evergreen works page is handled by _scrape_kvarovi
        seen.add(article_url)
        if not _looks_like_outage(title):
            continue

        post = _fetch(article_url)
        if not post:
            continue
        art = post.find("article", id=lambda v: v and v.startswith("post-"))
        if not art:
            continue
        content = art.select_one(".entry-content")
        body = content.get_text(" ", strip=True) if content else ""
        date_str = _date_from(art, title)

        if len(body) >= 40:
            raw_text = f"Чачак, обавештење за дан {date_str}.\n{title}. {body}" if date_str else f"Чачак. {title}. {body}"
            image = None
        else:
            img_url = _notice_image_url(art)
            if not img_url:
                print(f"   (skip: no body text and no notice image) {article_url}")
                continue
            try:
                time.sleep(REQUEST_DELAY)
                img_bytes = requests.get(img_url, headers=HEADERS, timeout=25).content
            except Exception as e:
                print(f"⚠️ Could not fetch image {img_url}: {e}")
                continue
            mime = "image/png" if img_url.lower().endswith(".png") else "image/jpeg"
            image = (img_bytes, mime)
            raw_text = (f"Чачак, обавештење за дан {date_str}. Наслов: {title}."
                        if date_str else f"Чачак. Наслов: {title}.")

        text_hash = compute_text_hash(raw_text if image is None else f"{article_url}|{title}")
        if is_hash_processed(text_hash):
            continue

        kind = "text" if image is None else "image OCR"
        print(f"\n🔥 ČAČAK WATER ({date_str or 'no date'}, {kind}): {article_url}")
        print(f"   ▸ {title[:90]}")
        _queue(raw_text, image, date_str, text_hash, title, article_url, dry_run)


def run_scraper(dry_run: bool = False):
    print("Scraping Čačak water announcements (vodovodca.rs)\n")
    _scrape_kvarovi(dry_run)
    _scrape_obavestenja(dry_run)


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
