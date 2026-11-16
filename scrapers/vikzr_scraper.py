import sys
import os
import re
import time

import requests
from bs4 import BeautifulSoup

# Ensure root directory is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed
from scrapers._common import use_scraper_date

# JKP "Vodovod i kanalizacija" Zrenjanin publishes one post per day titled
# "РАДОВИ НА ВОДОВОДНОЈ МРЕЖИ НА ДАН DD.MM.YYYY. ГОДИНЕ" plus the occasional ad-hoc
# "ПРЕКИД ВОДОСНАБДЕВАЊА ..." emergency notice. Both land in this feed.
LIST_URL = "https://www.vikzr.rs/category/vesti/najnovije-vesti/"
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-notifier)"}

# vikzr.rs robots.txt sets no Crawl-delay; keep a modest self-imposed gap anyway.
REQUEST_DELAY = 1.0

# Slugs worth reading. Everything else in this feed (price lists, interviews, "kvar
# successfully repaired" all-clears, consumption appeals) is not an outage.
RELEVANT_SLUG_KEYWORDS = ("radovi-na-vodovodnoj-mrezi", "prekid-vodosnabdevanja")

# An all-clear / completion post can still match the slug filter (e.g.
# "prekid-vodosnabdevanja-..." followed by "voda je puštena"). Skip when the body
# reads as a past-tense resolution rather than an upcoming interruption.
_ALL_CLEAR_MARKERS = (
    "успешно санир", "успешно је санир", "вода је пуштена", "вода пуштена у мрежу",
    "нормализације водоснабдевања", "радови су завршени", "квар је санир",
)

# The daily post's works list sits between this heading and the site furniture that
# follows it (related-posts block, YouTube plug, department signature).
_WORKS_HEADING_RE = re.compile(r"РАДОВИ\s+НА\s+МРЕЖИ", re.IGNORECASE)
_BODY_END_MARKERS = (
    "Видео прилоге", "Прочитајте још", "Служба информисања", "БОНУС ВИДЕО",
    "погледајте на нашем YouTube",
)

_DATE_IN_TITLE_RE = re.compile(r"НА\s+ДАН\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.IGNORECASE)
_DATE_IN_SLUG_RE = re.compile(r"na-dan-(\d{1,2})-(\d{1,2})-(\d{4})")

# Each works entry opens with a bolded, fully upper-case place heading on its own line
# ("МЕЛЕНЦИ", "ТОШЕ ЈОВАНОВИЋА (ЗРЕЊАНИН)", "РЕЈОН ОКО ПОЉОПРИВРЕДНЕ ШКОЛЕ (ЗРЕЊАНИН)"),
# followed by body text in normal case. Longest real heading seen is 40 characters; the cap
# keeps an all-caps sentence inside a body from being mistaken for a new entry.
_MAX_HEADING_LEN = 80


def _is_entry_heading(line: str) -> bool:
    letters = [ch for ch in line if ch.isalpha()]
    if len(letters) < 2 or len(line) > _MAX_HEADING_LEN:
        return False
    return all(ch.isupper() for ch in letters)


def _split_works_entries(works_block: str) -> list[str]:
    """One announcement page covers several unrelated places at once (a village 15km out, a city
    kvart, and a single street all in the same post). Splitting them into separate outages rather
    than storing the post as one row is what lets a recipient's notification and the map popup
    show only the entry that concerns them, and keeps a village's whole-settlement hexes from
    being lumped into the same row as a downtown street's.

    Delimiting on the upper-case heading rather than on "line does not start with a dash":
    WordPress emits each bolded run as its own text node, so a single entry can arrive as a bare
    "–" line plus several unindented continuation lines (the 22.08 Јунака Милана Тепића entry,
    with its intraday 12:30/15:30 updates, splits into six such lines). Case is what actually
    separates a heading from body text - headings are occasionally typed with a Latin 'T'/'M' in
    an otherwise Cyrillic word, so script detection would not be reliable either."""
    lines = [ln.strip() for ln in works_block.splitlines() if ln.strip()]
    entries: list[list[str]] = []

    for line in lines:
        if _WORKS_HEADING_RE.search(line):
            continue
        if _is_entry_heading(line) or not entries:
            entries.append([line])
        else:
            entries[-1].append(line)

    return [" ".join(parts).strip() for parts in entries if parts]


def _extract_date(title: str, article_url: str, soup: BeautifulSoup) -> str | None:
    """DD.MM.YYYY for the outage day. The post title is authoritative; the URL slug is a
    fallback (they occasionally disagree - a 20.08 post has lived at a ...19-08-2026 slug).
    Ad-hoc emergency posts carry no date in either, so fall back to the publish date."""
    for pattern, source in ((_DATE_IN_TITLE_RE, title), (_DATE_IN_SLUG_RE, article_url)):
        m = pattern.search(source or "")
        if m:
            d, mth, y = m.groups()
            return f"{int(d):02d}.{int(mth):02d}.{y}"

    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", time_tag["datetime"])
        if m:
            y, mth, d = m.groups()
            return f"{d}.{mth}.{y}"
    return None


def _extract_works_block(content_div) -> tuple[str, bool] | tuple[None, bool]:
    """The affected-locations text, trimmed of the standing intro paragraphs and the trailing
    site furniture. Returns (block, structured); structured is True only for the daily
    "РАДОВИ НА МРЕЖИ:" list, whose per-place headings can be split into separate outages. An
    ad-hoc emergency notice is free prose about one event and must stay whole - splitting it by
    line shape would shred a single incident into a dozen sentence fragments. Returns
    (None, False) when the post describes no interruption at all."""
    full_text = content_div.get_text("\n", strip=True)
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]

    structured = True
    start = next((i for i, ln in enumerate(lines) if _WORKS_HEADING_RE.search(ln)), None)
    if start is None:
        structured = False
        lowered = full_text.lower()
        if not any(k in lowered for k in ("прекид водоснабдевања", "без воде", "доћи ће до прекида")):
            return None, False
        start = 0

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if any(marker in lines[i] for marker in _BODY_END_MARKERS):
            end = i
            break

    block = "\n".join(lines[start:end]).strip()
    return (block or None), structured


def run_scraper(dry_run: bool = False):
    print(f"Scraping Zrenjanin water announcements from: {LIST_URL}\n")

    try:
        response = requests.get(LIST_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        print(f"❌ Could not fetch feed: {e}")
        return

    processed_urls = set()
    articles = soup.find_all("article", id=lambda v: v and v.startswith("post-"))

    # Oldest first so DB ids follow chronological order on a first bulk run.
    for card in reversed(articles):
        title_link = card.select_one("h2.entry-title a[href], h1.entry-title a[href], .entry-title a[href]")
        if not title_link:
            continue

        article_url = title_link["href"]
        title = title_link.get_text(" ", strip=True)

        if article_url in processed_urls:
            continue
        if not any(kw in article_url for kw in RELEVANT_SLUG_KEYWORDS):
            continue
        processed_urls.add(article_url)

        try:
            time.sleep(REQUEST_DELAY)
            article_res = requests.get(article_url, headers=HEADERS, timeout=15)
            article_soup = BeautifulSoup(article_res.text, "html.parser")
            article_tag = article_soup.find("article", id=lambda v: v and v.startswith("post-"))
            # vikzr.rs runs the Customizr theme: the post body is div.czr-wp-the-content
            # (aka div.post-entry), not the usual WordPress div.entry-content.
            content_div = None
            for scope in (article_tag, article_soup):
                if not scope:
                    continue
                content_div = (scope.find("div", class_="czr-wp-the-content")
                               or scope.find("div", class_="post-entry")
                               or scope.find("div", class_="entry-content"))
                if content_div:
                    break
        except Exception as e:
            print(f"⚠️ Could not fetch {article_url}: {e}")
            continue

        if not content_div:
            print(f"⚠️ No content body found at {article_url}")
            continue

        block_text = content_div.get_text("\n", strip=True)
        if any(marker in block_text for marker in _ALL_CLEAR_MARKERS) and not _WORKS_HEADING_RE.search(block_text):
            continue

        works_block, structured = _extract_works_block(content_div)
        if not works_block or len(works_block) < 30:
            continue

        date_str = _extract_date(title, article_url, article_soup)
        entries = _split_works_entries(works_block) if structured else [works_block]
        if not entries:
            continue

        print(f"\n🔥 ZR WATER ANNOUNCEMENT ({date_str or 'no date'}): {article_url}")
        print(f"   {len(entries)} works entr{'y' if len(entries) == 1 else 'ies'} on this page")

        for entry_text in entries:
            if len(entry_text) < 30:
                continue

            # Prepend the day so the parser has a date to extract - the works entries
            # never repeat it, it only lives in the post title.
            raw_text = f"Обавештење за дан {date_str}.\n{entry_text}" if date_str else entry_text

            text_hash = compute_text_hash(raw_text)
            if is_hash_processed(text_hash):
                continue

            print(f"   ▸ {entry_text[:70]}...")
            parsed = parse_announcement(raw_text)
            if not parsed:
                print("     ↳ parser returned nothing, skipping")
                continue

            # This scraper only ever reads vikzr.rs - JKP "Vodovod i kanalizacija" Zrenjanin
            # serves Grad Zrenjanin only, so every announcement here IS Zrenjanin by
            # construction. Same override the NS scrapers use: a short entry that names no
            # city gives Gemini nothing to ground a guess on, and it can guess wrong.
            parsed.city = "ЗРЕЊАНИН"
            # Override rather than fill-if-empty: the post title states the day outright, while
            # the model has to infer it and gets it wrong in ways that look plausible. Confirmed
            # real cases from these same pages: the 25.08 entry came back as "2025-08-25" and the
            # 27.08 entry as "2027-08-27" - the day number read as a year. It also returns three
            # different formats across one run ("26.08.2026.", "2026-08-26", "24.08.2026"), which
            # nothing downstream can compare or expire on.
            if date_str:
                use_scraper_date(parsed, date_str, raw_text)

            if dry_run:
                from core.matchmaker import calculate_outage_hexes
                hexes, quality = calculate_outage_hexes(parsed, raw_text)
                print(f"     [dry-run] date={parsed.outage_date!r} hexes={len(hexes)} quality={quality}")
                for loc in parsed.affected_locations:
                    print(f"       - {loc.location_type} street={loc.main_street!r} "
                          f"settlement={loc.settlement!r} time={loc.time_window!r} "
                          f"surrounding={loc.includes_surrounding_streets}")
                continue

            process_and_queue_outage(
                parsed, raw_text, text_hash=text_hash, category="WATER", source_url=article_url
            )


if __name__ == "__main__":
    run_scraper(dry_run="--dry-run" in sys.argv)
