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

# "Energetika" d.o.o. Kragujevac - district heating. No structured outage feed: interruptions
# are posted as plain news items on vesti.php, mixed in with corporate news (surveys, patron
# saint's day, procurement, price hikes). Title + body are keyword-gated to keep only the
# actual "bez grejanja" posts. Rare outside the heating season.
LIST_URL = "https://www.energetika-kragujevac.com/vesti.php"
POST_URL = "https://www.energetika-kragujevac.com/vest.php?vest={}"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
REQUEST_DELAY = 1.0
OPSTINA = "КРАГУЈЕВАЦ"
MAX_POSTS_SCANNED = 40

_VEST_ID_RE = re.compile(r"vest\.php\?vest=(\d+)")
_DATE_LINE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\.?$")

# A news item is an outage if its title/body says heating is (going) off.
_OUTAGE_MARKERS = (
    "bez grejanja", "bez tople vode", "obustav", "isključ", "iskljuc", "prekid grejanja",
    "prekid u isporuci", "kvar", "havarij", "defekt", "popravk", "popustile", "pukla",
    "toplovod",
)
# ...and NOT one of the recurring corporate pieces that can still contain a stray keyword.
_SKIP_MARKERS = (
    "anketa", "krsna slava", "javne nabavke", "javnih nabavki", "cenovnik", "poskup",
    "cena grejanja", "cene grejanja", "konkurs", "konferencij", "tops 20", "simterm",
    "davalac", "davaoc", "krvi", "praznuje", "venac", "venci", "veliki školski čas",
    "poziv za građane", "razgovarajte sa direktorom", "veće plate", "priznanje",
    "grejna sezona počela", "završena je grejna sezona", "spremni za grejnu sezonu",
    "obnovljiv", "modernizacij", "susretima toplana",
)

_BODY_END_MARKERS = ("Ostale vesti", "MENI", "Naslovna", "Copyright")


def _looks_like_outage(text: str) -> bool:
    low = (text or "").lower()
    if any(s in low for s in _SKIP_MARKERS):
        return False
    return any(k in low for k in _OUTAGE_MARKERS)


def _fetch(url: str) -> BeautifulSoup | None:
    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"⚠️ Could not fetch {url}: {e}")
        return None


def _post_title_date_body(soup: BeautifulSoup):
    """The vest.php page carries the title, a bare DD.MM.YYYY line, then the body paragraphs,
    ending at the 'Ostale vesti' block. Nav/footer share the page, so anchor on the date line."""
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.extract()
    lines = [ln.strip() for ln in soup.get_text("\n", strip=True).splitlines() if ln.strip()]

    idx = next((i for i, ln in enumerate(lines) if _DATE_LINE_RE.match(ln)), None)
    if idx is None:
        return None, None, None

    m = _DATE_LINE_RE.match(lines[idx])
    d, mth, y = m.groups()
    date_str = f"{int(d):02d}.{int(mth):02d}.{y}"
    title = lines[idx - 1] if idx > 0 else ""

    end = len(lines)
    for i in range(idx + 1, len(lines)):
        if any(mark in lines[i] for mark in _BODY_END_MARKERS):
            end = i
            break
    body = " ".join(lines[idx + 1:end]).strip()
    return title, date_str, body


def run_scraper(dry_run: bool = False):
    print(f"Scraping Kragujevac heating (Energetika) from: {LIST_URL}\n")

    soup = _fetch(LIST_URL)
    if not soup:
        return

    # De-duplicate the vest ids in list order (each item links twice - image + headline).
    seen_ids, ordered_ids = set(), []
    for a in soup.select('a[href*="vest.php?vest="]'):
        m = _VEST_ID_RE.search(a["href"])
        if not m:
            continue
        vid = m.group(1)
        if vid not in seen_ids:
            seen_ids.add(vid)
            ordered_ids.append((vid, a.get_text(" ", strip=True)))

    checked = 0
    for vid, list_title in ordered_ids:
        if checked >= MAX_POSTS_SCANNED:
            break
        checked += 1

        # Cheap gate on the headline before fetching the post.
        if list_title and not _looks_like_outage(list_title):
            continue

        post_soup = _fetch(POST_URL.format(vid))
        if not post_soup:
            continue
        title, date_str, body = _post_title_date_body(post_soup)
        if not body or len(body) < 40:
            continue
        if not _looks_like_outage(f"{title} {body}"):
            continue

        raw_text = f"Крагујевац, обавештење за дан {date_str}.\n{title}. {body}" if date_str \
            else f"Крагујевац. {title}. {body}"

        text_hash = compute_text_hash(raw_text)
        if is_hash_processed(text_hash):
            continue

        print(f"\n🔥 KG HEATING ANNOUNCEMENT ({date_str or 'no date'}): vest={vid}")
        print(f"   ▸ {title[:90]}")
        parsed = parse_announcement(raw_text)
        if not parsed:
            print("     ↳ parser returned nothing, skipping")
            continue

        parsed.city = OPSTINA
        parsed.event_type = parsed.event_type or "HEATING"
        if date_str:
            use_scraper_date(parsed, date_str, raw_text)
        drop_unknown_settlements(parsed, OPSTINA)

        if dry_run:
            from core.matchmaker import calculate_outage_hexes
            hexes, quality = calculate_outage_hexes(parsed, raw_text)
            print(f"     [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
            for loc in parsed.affected_locations:
                print(f"       - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r}")
            continue

        process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="HEATING",
                                 source_url=POST_URL.format(vid))


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
