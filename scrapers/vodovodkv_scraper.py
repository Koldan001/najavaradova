import sys
import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed
from scrapers._common import drop_unknown_settlements, use_scraper_date

# JKP "Vodovod" Kraljevo. Three sources:
#   RADOVI_URL   - a structured "planirani radovi" table: Od datuma | Do datuma | Datum
#                  početka | Opis radova | Lokacija. The "Do datuma" column is exactly the
#                  outage's end date, so these rows carry a real expiry.
#   KVAROVI_URL  - a "kvarovi na mreži" table: Datum | Ulica.
#   AKTUELNOSTI  - news posts whose notice is a scanned JPG (read with Gemini vision).
RADOVI_URL = "https://www.vodovodkv.co.rs/radovi.php"
KVAROVI_URL = "https://www.vodovodkv.co.rs/kvarovi_mreza.php"
AKTUELNOSTI_URL = "https://www.vodovodkv.co.rs/aktuelnosti.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
REQUEST_DELAY = 1.0
OPSTINA = "КРАЉЕВО"

# AKTUELNOSTI is one static page listing this site's full news history, unpaginated - unlike
# every other scraper's "today + a few days ahead" source, there's nothing here that bounds how
# far back it goes. Confirmed for real: a bare run OCR'd and Gemini-parsed notices back to 2024.
# An outage whose own date is this stale is already over - no one to notify, pure wasted Gemini
# spend (OCR + parse per item). 21 days is generous slack for the site's own posting lag, not a
# tight "must be exactly current" cutoff.
MAX_AKTUELNOSTI_AGE_DAYS = 21

_MONTHS = {
    "januar": 1, "februar": 2, "mart": 3, "april": 4, "maj": 5, "jun": 6,
    "jul": 7, "avgust": 8, "septembar": 9, "oktobar": 10, "novembar": 11, "decembar": 12,
}
_ISO_DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
_NEWS_DATE_RE = re.compile(r"-\s*([A-Za-zČĆŽŠĐčćžšđ]+)\s+(\d{1,2})\.?\s*,?\s*(\d{4})")
# Confirmed for real: titles carry their date in several inconsistent shapes - "Avgust 24.-25.
# 2025." (day RANGE, breaks _NEWS_DATE_RE's single \d{1,2}), "26. Septembar 2024." (day-before-
# month, the reverse of _NEWS_DATE_RE's assumed order), and probably others not seen yet. Getting
# every shape exactly right is a losing game; a bare 4-digit year is unambiguous regardless of
# what surrounds it, so it's the actual staleness signal used below - the precise day/month from
# _norm_date/_news_date is only for display, staleness no longer depends on them parsing at all.
_ANY_YEAR_RE = re.compile(r"\b(20\d{2})\b")

_OUTAGE_TITLE_RE = re.compile(
    r"prekid|radov|kvar|vodosnabdev|priključ|isključ|havarij|obustav", re.IGNORECASE)
_SKIP_TITLE_RE = re.compile(
    r"racionaln|apeluje|cena|cene|e-korisnik|servis|registrac|nagrad|konkurs", re.IGNORECASE)
_STOCK_IMG_RE = re.compile(r"(radovi_1|radovi_2|voda\.jpg|water_|logo|ekorisnik)", re.IGNORECASE)


def _fetch(url: str) -> BeautifulSoup | None:
    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.encoding = r.apparent_encoding or "utf-8"
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"⚠️ Could not fetch {url}: {e}")
        return None


def _norm_date(value: str) -> str | None:
    m = _ISO_DATE_RE.search(value or "")
    if not m:
        return None
    d, mth, y = m.groups()
    return f"{int(d):02d}.{int(mth):02d}.{y}"


def _news_date(text: str) -> str | None:
    m = _NEWS_DATE_RE.search(text or "")
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    return f"{int(m.group(2)):02d}.{month:02d}.{m.group(3)}"


def _is_stale(date_str: str | None, title_text: str = "") -> bool:
    """Two checks, in order of reliability:

    1. Any bare 4-digit year in the title that isn't this year is stale, full stop - a "planned
       works" notice from a prior year is over regardless of which day/month format the site
       used (and it uses several - see _ANY_YEAR_RE's comment). This alone would have caught
       every 2023/2024/2025 case confirmed in testing, including ones _news_date couldn't parse.
    2. If date_str DID parse (typically the day.month.year-this-year case), also apply the
       21-day window within the current year - catches e.g. an unrelated March notice still
       sitting on the page in September.

    A post with neither a stale year nor a parseable date is NOT stale by default - dateless
    posts are rare and usually just-posted, so letting them through beats a false positive here.
    """
    this_year = datetime.now().year
    year_match = _ANY_YEAR_RE.search(title_text)
    if year_match and int(year_match.group(1)) != this_year:
        return True
    if not date_str:
        return False
    try:
        posted = datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        return False
    return (datetime.now() - posted).days > MAX_AKTUELNOSTI_AGE_DAYS


def _queue(raw_text, image, date_str, end_date, text_hash, source_url, dry_run):
    parsed = parse_announcement(raw_text, image=image)
    if not parsed:
        print("     ↳ parser returned nothing, skipping")
        return
    parsed.city = OPSTINA
    if date_str:
        use_scraper_date(parsed, date_str, raw_text)
    if end_date and not getattr(parsed, "end_date", None):
        parsed.end_date = end_date
    drop_unknown_settlements(parsed, OPSTINA)

    if dry_run:
        from core.matchmaker import calculate_outage_hexes, _compute_expires_at
        hexes, quality = calculate_outage_hexes(parsed, raw_text)
        tw = next((l.time_window for l in parsed.affected_locations if l.time_window), "N/A")
        exp = _compute_expires_at(parsed.outage_date, getattr(parsed, "end_date", None), tw)
        print(f"     [dry-run] date={parsed.outage_date!r} end={getattr(parsed,'end_date',None)!r} "
              f"expires={exp} hexes={len(hexes)} quality={quality}")
        for loc in parsed.affected_locations:
            print(f"       - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r}")
        return

    process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER", source_url=source_url)


def _scrape_table(url: str, kind: str, section_marker: str, dry_run: bool):
    soup = _fetch(url)
    if not soup:
        return
    heading = soup.find(lambda t: t.name in ("h1", "h2", "h3") and section_marker in t.get_text(" ", strip=True).upper())
    table = heading.find_next("table") if heading else soup.find("table")
    if not table:
        print(f"   ({kind}: no table)")
        return

    scheduled = 0
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if kind == "radovi" and len(cells) >= 5:
            od, do, pocetak, opis, lokacija = cells[0], cells[1], cells[2], cells[3], cells[4]
            date_str = _norm_date(pocetak) or _norm_date(od)
            end_date = _norm_date(do)
            raw_text = (f"Краљево. Планирани радови на водоводној мрежи. Локација: {lokacija}. "
                        f"{opis}." + (f" Радови трају до {do}." if do else ""))
        elif kind == "kvarovi" and len(cells) >= 2:
            # Columns are Datum | Od | Do | Ulica (older layout was just Datum | Ulica). The
            # street is the last cell; the first date-looking cell is the start, a later one Do.
            ulica = cells[-1]
            date_cells = [c for c in cells[:-1] if _norm_date(c)]
            date_str = _norm_date(date_cells[0]) if date_cells else None
            end_date = _norm_date(date_cells[-1]) if len(date_cells) > 1 else None
            raw_text = f"Краљево. Квар на водоводној мрежи у улици {ulica}." + (
                f" Радови трају до {date_cells[-1]}." if end_date else "")
        else:
            continue

        # A row with no parseable date is the header row (or a stray) - skip it.
        if not date_str:
            continue
        scheduled += 1

        if len(raw_text) < 30:
            continue
        text_hash = compute_text_hash(raw_text)
        if is_hash_processed(text_hash):
            continue
        print(f"\n🔥 KRALJEVO WATER ({kind}, {date_str})\n   ▸ {raw_text[:110]}...")
        _queue(raw_text, None, date_str, end_date, text_hash, url, dry_run)

    if not scheduled:
        print(f"   ({kind}: table has no dated rows — nothing scheduled)")


def _scrape_aktuelnosti(dry_run: bool):
    soup = _fetch(AKTUELNOSTI_URL)
    if not soup:
        return

    for h4 in soup.find_all("h4"):
        title = h4.get_text(" ", strip=True)
        if not _OUTAGE_TITLE_RE.search(title) or _SKIP_TITLE_RE.search(title):
            continue

        # The scanned notice image is the next non-stock <img> after this heading.
        img_tag = None
        for sib in h4.find_all_next("img", limit=6):
            src = (sib.get("src") or sib.get("data-src") or "")
            if src and not _STOCK_IMG_RE.search(src):
                img_tag = sib
                break
        img_url = None
        if img_tag:
            src = img_tag["src"] if img_tag.get("src") else img_tag.get("data-src")
            img_url = src if src.startswith("http") else "https://www.vodovodkv.co.rs/" + src.lstrip("/")

        date_str = _norm_date(title) or _news_date(title)
        if _is_stale(date_str, title):
            continue
        raw_text = f"Краљево, обавештење за дан {date_str}. {title}" if date_str else f"Краљево. {title}"
        text_hash = compute_text_hash(f"{img_url or ''}|{title}")
        if is_hash_processed(text_hash):
            continue

        image = None
        if img_url:
            try:
                time.sleep(REQUEST_DELAY)
                data = requests.get(img_url, headers=HEADERS, timeout=25).content
                image = (data, "image/png" if img_url.lower().endswith(".png") else "image/jpeg")
            except Exception as e:
                print(f"⚠️ Could not fetch image {img_url}: {e}")

        print(f"\n🔥 KRALJEVO WATER (aktuelnosti, {date_str or 'no date'}, {'image OCR' if image else 'title only'})")
        print(f"   ▸ {title[:110]}")
        _queue(raw_text, image, date_str, None, text_hash, AKTUELNOSTI_URL, dry_run)


def run_scraper(dry_run: bool = False):
    print("Scraping Kraljevo water announcements (vodovodkv.co.rs)\n")
    _scrape_table(RADOVI_URL, "radovi", "PLANIRANI RADOVI", dry_run)
    _scrape_table(KVAROVI_URL, "kvarovi", "KVAROVI NA MRE", dry_run)
    _scrape_aktuelnosti(dry_run)


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
