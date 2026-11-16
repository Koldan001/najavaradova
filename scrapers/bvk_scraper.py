import re
import sys
import os
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed

# Unlike nstoplana/vikns (a chronological blog with one URL per article), BVK publishes planned
# works as toggle/accordion sections on a single static page - no article URLs to visit, no
# pagination. Each <section class="av_toggle_section"> is one announcement; the toggler's
# data-title carries "<date>. <opština>" and .toggle_content carries the actual notice.
LIST_URL = "https://www.bvk.rs/planirani-radovi/"


def _opstina_heading(p_tag) -> str | None:
    """BVK's own section divider: a `<p>` whose entire content is one bare `<strong>` run
    ("<p><strong>ЗВЕЗДАРА</strong></p>"). Every section carries at least one - even an ordinary
    single-opština post repeats the same opština the title already names - and a section can
    carry several (confirmed real case, 17.09.2026: one toggle titled "...у општинама Звездара и
    Нови Београд" held a Zvezdara street list AND an unrelated Novi Beograd/Blok 70 notice, each
    under its own heading, both wrapped into one announcement)."""
    strongs = p_tag.find_all("strong", recursive=False)
    if len(strongs) != 1:
        return None
    text = p_tag.get_text(" ", strip=True)
    heading = strongs[0].get_text(" ", strip=True)
    return heading if heading and heading == text else None


# A line that opens with its own night or day: "21/22.09.2026. године – блок оивичен улицама ..."
# or "22.09.2026. – ...". BVK's multi-night flushing notices list one such line per night.
_DATED_LINE = re.compile(r"^\s*(\d{1,2})\s*(?:/\s*(\d{1,2})\s*)?\.\s*(\d{1,2})\s*\.\s*(\d{4})")


def _line_date(text: str) -> str | None:
    """DD.MM.YYYY the line's night starts on ("30/01.10.2026" starts on 30.09.2026), or None."""
    m = _DATED_LINE.match(text)
    if not m:
        return None
    first, second, month, year = int(m[1]), m[2], int(m[3]), int(m[4])
    try:
        if second and first > int(second):         # the night crosses into the next month
            day = (date(year, month, 1) - timedelta(days=1)).replace(day=first)
        else:
            day = date(year, month, first)
    except ValueError:
        return None
    return day.strftime("%d.%m.%Y")


def split_nights(blocks: list[str]) -> list[tuple[str, list[str]]] | None:
    """[(DD.MM.YYYY, the blocks of that one night)] when the notice lists two or more dated lines,
    else None. Each night keeps the notice's opening and closing paragraphs (the hours, "may be
    cloudy") around its own line. Joined, a flushing notice for 21.-26.09. over five blocks was one
    week-long outage on all 32 streets, every night (#1984)."""
    dated = [i for i, text in enumerate(blocks) if _line_date(text)]
    if len(dated) < 2:
        return None
    intro, outro = blocks[:dated[0]], [b for b in blocks[dated[-1] + 1:] if not _line_date(b)]
    return [(_line_date(blocks[i]), intro + [blocks[i]] + outro) for i in dated]


_BLOCK = re.compile(r"блок\w*\s+оивичен\w*\s+улиц", re.IGNORECASE)


def bound_block_streets(parsed, text: str) -> None:
    """"блок оивичен улицама А, Б, В, Г" is the block inside those streets: each one counts only
    along the block's edge, from the street before it in the list to the one after (the list goes
    round the block). Left whole, a boundary street like Аутопут or Булевар ослобођења was drawn
    and notified for kilometres beyond the block (#1984). Set as the same cross-street bounds an
    "од X до Y" notice gives, so the existing clipping - and its whole-street fallback when a
    corner can't be found - applies. Only a line that is nothing but streets: one that also names
    a neighbourhood ("насеље Сењак") doesn't list a ring, and is left as it was. So is a text with
    more than one block: its streets are several rings, not one."""
    if len(_BLOCK.findall(text or "")) != 1:
        return
    locs = parsed.affected_locations
    if len(locs) < 3 or any(loc.location_type != "STREET" or not loc.main_street
                            or loc.intersection_start or loc.intersection_end for loc in locs):
        return
    for i, loc in enumerate(locs):
        loc.intersection_start = locs[i - 1].main_street
        loc.intersection_end = locs[(i + 1) % len(locs)].main_street


def run_scraper():
    print(f"Scraping BVK planned works from: {LIST_URL}\n")
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(LIST_URL, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        sections = soup.find_all("section", class_="av_toggle_section")

        for section in sections:
            toggler = section.find("p", class_="toggler")
            title = toggler.get("data-title", "").strip() if toggler else ""

            content_div = section.find("div", class_="toggle_content")
            if not content_div:
                continue

            # Split on the opština headings instead of flattening the whole section into one
            # blob. Without this, every street here is parsed with settlement=None and, since
            # "Beograd" the grad is not itself a real RGZ opština, that falls through to an
            # OSM search scoped to the whole city (an ~18km radius - see
            # spatial/spatial_engine.py's _get_osm_local_segments) - wide enough for a
            # same-named street elsewhere in Belgrade to leak in as if it were local. Confirmed
            # live: the Zvezdara street list above matched into Voždovac this way and notified
            # two subscribers there for an outage nowhere near them. Splitting per heading and
            # forcing that opština onto every location below it (see the override after parsing)
            # closes this for both the multi-opština case and the ordinary single-opština one,
            # which was carrying the exact same latent gap.
            segments = []  # [(opstina_heading | None, [block_text, ...]), ...]
            current_heading = None
            current_blocks = []
            for el in content_div.find_all(["p", "li"]):
                heading = _opstina_heading(el) if el.name == "p" else None
                if heading:
                    if current_blocks:
                        segments.append((current_heading, current_blocks))
                    current_heading = heading
                    current_blocks = []
                    continue
                text = el.get_text(" ", strip=True)
                if text:
                    current_blocks.append(text)
            if current_blocks:
                segments.append((current_heading, current_blocks))
            if not segments:
                continue

            notices = []  # (opština heading, night date or None, blocks)
            for opstina_heading, blocks in segments:
                nights = split_nights(blocks)
                notices += ([(opstina_heading, night, night_blocks) for night, night_blocks in nights]
                            if nights else [(opstina_heading, None, blocks)])

            for opstina_heading, night, blocks in notices:
                body_text = " ".join(blocks)
                raw_text = (f"{title} {opstina_heading}. {body_text}" if opstina_heading
                            else f"{title} {body_text}").strip()
                if len(raw_text) < 30:
                    continue

                text_hash = compute_text_hash(raw_text)
                if is_hash_processed(text_hash):
                    continue

                print(f"\n🔥 NEW BVK ANNOUNCEMENT ({opstina_heading or title}): {body_text[:70]}")
                parsed = parse_announcement(raw_text)
                if not parsed:
                    continue

                if night:
                    # One night of a multi-night notice: its own line says which. The opening
                    # paragraph still names the whole run ("од 21. до 26. септембра"), so the
                    # model's dates can't be trusted to stop at this night.
                    parsed.outage_date = night
                    parsed.end_date = None
                bound_block_streets(parsed, body_text)

                if opstina_heading:
                    # Deterministic override, not left for the model to carry a heading down
                    # through a long street list on its own (confirmed it doesn't, every street
                    # location here came back with settlement=None). Same reasoning as vikzr's
                    # parsed.city override elsewhere in this codebase: the scraper KNOWS the
                    # opština from the page's own structure, and that must win. Only
                    # street-scoped locations - an ENTIRE_SETTLEMENT already names its own, more
                    # specific place (e.g. "БЛОК 70") and overwriting that would lose precision,
                    # not add it.
                    for loc in parsed.affected_locations:
                        if loc.location_type != "ENTIRE_SETTLEMENT" and not (loc.settlement or "").strip():
                            loc.settlement = opstina_heading

                process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                                         source_url=LIST_URL)

    except Exception as e:
        print(f"❌ Scraper error: {e}")


if __name__ == "__main__":
    run_scraper()
