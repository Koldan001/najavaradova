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
from spatial.spatial_engine import get_settlement_coverage

# JKP "Vodovod i kanalizacija" Kragujevac. The /iskljucenja/ feed carries two post types:
#   havarijska-iskljucenja-DD-MM-YYYY   - a daily <li> list, one street outage per item
#   havarija-na-magistralnom-cevovodu-* - a single prose main-pipe fault
LIST_URL = "https://jkpvik-kg.com/iskljucenja/"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}
REQUEST_DELAY = 1.0
OPSTINA = "КРАГУЈЕВАЦ"

_LIST_SLUG = "havarijska-iskljucenja"
_MAIN_PIPE_SLUG = "havarija-na-magistralnom"

_SLUG_DATE_RE = re.compile(r"-(\d{1,2})-(\d{1,2})-(\d{4})/?$")
_TITLE_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

# "...потрошачи у следећим насељима: Шумарице, Вишњак, део Станова, део Корићана и део Ердеча."
# The model keeps the segment count but repeatedly drops these names, so pull them directly.
_NASELJA_LIST_RE = re.compile(r"насе[љл]има\s*:?\s*(.+?)(?:\.|\bНормализациј|\bЦистерн)", re.IGNORECASE)


def _nominative_candidates(name: str) -> list[str]:
    """The prose lists areas in the genitive ('без воде у ... Станова, Корићана, Ердеча'), but
    geocoding and the RGZ naselje table are keyed on the nominative. Serbian genitive->nominative
    for place names has no single rule, so return the plausible reversals, most-specific first,
    and let the caller keep whichever one actually resolves."""
    n = name.strip().upper()
    cands = [n]
    if n.endswith("ОВА"):
        cands.append(n[:-3] + "ОВО")      # Станова -> Станово
    if n.endswith("ЕВА"):
        cands.append(n[:-3] + "ЕВО")
    if n.endswith("А") and len(n) > 3:
        cands.append(n[:-1])              # Ердеча -> Ердеч
        cands.append(n[:-1] + "И")        # Корићана -> Корићани
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _resolve_settlement_name(name: str, opstina: str) -> str:
    """Pick the candidate spelling that resolves best. An authoritative source (RGZ naselje /
    real boundary) always wins over a center-point disk - otherwise 'Ердеча' fuzzy-geocodes to
    some disk and shadows 'Ердеч', which RGZ has exactly."""
    cands = _nominative_candidates(name)
    disk_hit = None
    for cand in cands:
        hexes, src = get_settlement_coverage(cand, parent_region=opstina)
        if hexes and src in ("curated", "polygon", "rgz_naselje"):
            return cand
        if hexes and disk_hit is None and cand != cands[0]:
            disk_hit = cand      # prefer a transformed spelling over the raw genitive
    if disk_hit:
        return disk_hit
    for cand in cands:
        if get_settlement_coverage(cand, parent_region=opstina)[0]:
            return cand
    return cands[0]


def _settlements_from_prose(text: str) -> list[str]:
    m = _NASELJA_LIST_RE.search(text)
    if not m:
        return []
    names = []
    for part in re.split(r"\s*(?:,|\bи\b)\s*", m.group(1)):
        name = re.sub(r"^\s*(део|дела|делови)\s+", "", part.strip(), flags=re.IGNORECASE).strip(" .")
        if len(name) > 2:
            names.append(name.upper())
    return names

# Boilerplate lines carried on every post that add nothing for extraction.
_NOISE_MARKERS = (
    "план искључења је подлежан", "приликом пуштања воде", "може доћи до благог замућења",
    "напомена :",
)


def _extract_date(article_url: str, title: str) -> str | None:
    m = _SLUG_DATE_RE.search(article_url)
    if m:
        d, mth, y = m.groups()
        return f"{int(d):02d}.{int(mth):02d}.{y}"
    m = _TITLE_DATE_RE.search(title or "")
    if m:
        d, mth, y = m.groups()
        return f"{int(d):02d}.{int(mth):02d}.{y}"
    return None


def _clean(text: str) -> str:
    low = text.lower()
    return "" if any(m in low for m in _NOISE_MARKERS) else text.strip()


def _entries_for(article_url: str, content_div) -> list[str]:
    """One raw block per outage. The daily list splits into its <li> items; the main-pipe post
    is a single prose block."""
    if _LIST_SLUG in article_url:
        return [t for t in (_clean(li.get_text(" ", strip=True)) for li in content_div.find_all("li")) if len(t) > 15]

    parts = [t for t in (_clean(p.get_text(" ", strip=True)) for p in content_div.find_all("p")) if len(t) > 15]
    body = " ".join(parts).strip()
    return [body] if len(body) > 30 else []


def run_scraper(dry_run: bool = False):
    print(f"Scraping Kragujevac water announcements from: {LIST_URL}\n")

    try:
        time.sleep(REQUEST_DELAY)
        soup = BeautifulSoup(requests.get(LIST_URL, headers=HEADERS, timeout=20).text, "html.parser")
    except Exception as e:
        print(f"❌ Could not fetch feed: {e}")
        return

    seen = set()
    for link in soup.select("article .entry-title a[href], article h2 a[href], article h3 a[href]"):
        article_url = link["href"]
        title = link.get_text(" ", strip=True)
        if article_url in seen:
            continue
        if _LIST_SLUG not in article_url and _MAIN_PIPE_SLUG not in article_url:
            continue
        seen.add(article_url)

        try:
            time.sleep(REQUEST_DELAY)
            art_soup = BeautifulSoup(requests.get(article_url, headers=HEADERS, timeout=15).text, "html.parser")
            content_div = art_soup.select_one(".entry-content")
        except Exception as e:
            print(f"⚠️ Could not fetch {article_url}: {e}")
            continue
        if not content_div:
            print(f"⚠️ No content body at {article_url}")
            continue

        date_str = _extract_date(article_url, title)
        entries = _entries_for(article_url, content_div)
        if not entries:
            continue

        print(f"\n🔥 KG WATER ANNOUNCEMENT ({date_str or 'no date'}): {article_url}")
        print(f"   {len(entries)} outage block(s)")

        texts = [f"Крагујевац, обавештење за дан {date_str}.\n{entry}" if date_str else f"Крагујевац. {entry}"
                 for entry in entries]
        # One post can list a street twice at different hours (Стојана Протића, 14.10.2026:
        # 08-12 and 12-16). Both are still published, so neither is the other's new version.
        live_hashes = {compute_text_hash(t) for t in texts}

        for entry, raw_text in zip(entries, texts):
            text_hash = compute_text_hash(raw_text)
            if is_hash_processed(text_hash):
                continue

            print(f"   ▸ {entry[:90]}...")
            parsed = parse_announcement(raw_text)
            if not parsed:
                print("     ↳ parser returned nothing, skipping")
                continue

            # This scraper only ever reads jkpvik-kg.com - JKP Vodovod Kragujevac serves the
            # Kragujevac opština only, so every announcement here IS Kragujevac by construction.
            parsed.city = OPSTINA
            if date_str:
                use_scraper_date(parsed, date_str, raw_text)

            # Main-pipe posts list affected areas as "...у насељима: A, B, део C...". The model
            # keeps the right number of ENTIRE_SETTLEMENT segments but blanks their names, so
            # re-attach them from the prose. Extra segments beyond the list are dropped.
            if _MAIN_PIPE_SLUG in article_url:
                names = _settlements_from_prose(entry)
                if names:
                    es = [l for l in parsed.affected_locations if l.location_type == "ENTIRE_SETTLEMENT"]
                    other = [l for l in parsed.affected_locations if l.location_type != "ENTIRE_SETTLEMENT"]
                    template = es[0] if es else None
                    rebuilt = []
                    for name in names:
                        loc = template.model_copy(deep=True) if template else None
                        if loc is None:
                            from parser import StreetSegment
                            loc = StreetSegment(location_type="ENTIRE_SETTLEMENT", time_window="")
                        loc.location_type = "ENTIRE_SETTLEMENT"
                        loc.settlement = _resolve_settlement_name(name, OPSTINA)
                        loc.main_street = None
                        rebuilt.append(loc)
                    parsed.affected_locations = other + rebuilt

            drop_unknown_settlements(parsed, OPSTINA)

            if dry_run:
                from core.matchmaker import calculate_outage_hexes
                hexes, quality = calculate_outage_hexes(parsed, raw_text)
                print(f"     [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
                for loc in parsed.affected_locations:
                    print(f"       - {loc.location_type} street={loc.main_street!r} settlement={loc.settlement!r} "
                          f"bounds=({loc.intersection_start},{loc.intersection_end})")
                continue

            process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                                     source_url=article_url, live_hashes=live_hashes)


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
