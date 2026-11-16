import re
import sys
import os
import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement
from core.matchmaker import STREET_PLACEHOLDER, process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed

# Unplanned/real-time faults (as opposed to bvk_scraper.py's scheduled planirani-radovi).
# Structurally different from planirani-radovi despite sharing the same toggle-accordion theme:
# ONE toggle section per DAY (not per announcement), and inside it one <li> per opština, each a
# short "Општина: адреса1, адреса2 – насеље" fragment - closer in shape to EPS's per-row table
# than to BVK's own planned-works prose, so this follows the EPS pattern: hash/parse per LI, not
# per page or per section.
LIST_URL = "https://www.bvk.rs/kvarovi-na-mrezi/"

# Each day's <li> entries are grouped under "ДО HH:MM" headings ("до 15:00", "до 22:00" - the
# fault's expected restoration time), rendered as <h1> inside a <blockquote>, NOT as <p>/<li> -
# find_all("li") skipped them entirely, so every entry lost its real time window and fell back
# to whatever the model guessed (confirmed real case: "Нови Београд: Савски насип 9а" landed
# under "ДО 15:00" on the page but was stored/shown as 00:00-23:59, all day). Real cutoff is
# right there in the page - deterministic override, not something to leave the model to guess.
# Since late Oct 2026 the heading is a <p> (not <h1>) and is typed "Дo 15:00" - Cyrillic Д, LATIN o
# - so a Cyrillic-only "ДО" matched nothing, the model guessed 07:00-19:00 for every fault, and
# that went out in alerts (26.10.2026). Read the heading from any non-<li> element, either script.
_DO_TIME_RE = re.compile(r"[ДD][ОO]\s+(\d{1,2})[:.](\d{2})", re.IGNORECASE)
_HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p"]

# Each <li> starts with its opština ("Звездара: Руди Чајавеца бб"). That label is the city, set in
# code like every other scraper does - left to the model it came back as "БЕОГРАД", "Звездара" or
# "Београд - Стари град" for the same fault on different reads, so a re-read stored a second
# outage instead of updating the first (26.10.2026). Spelled the way EPS Belgrade spells them;
# Palilula needs its city, Niš has one too.
_OPSTINA_ALIASES = {"ПАЛИЛУЛА": "ПАЛИЛУЛА - БЕОГРАД"}

# The page names the house where a pipe broke and says "Без воде су потрошачи у наведеним и
# околним улицама": subscribers within this many metres of it hear it may reach them too (see
# core/matchmaker._queue_nearby), and the map rings the house.
NEARBY_M = 200


def _fault_template(date: str, cutoff: str | None) -> str:
    """A fault, said as one - the model wrote "Planirano isključenje vode" (26.10.2026) - with the
    end BVK gives. Without a heading the usual window is appended by the matchmaker."""
    msg = f"Kvar na vodovodnoj mreži u ulici {STREET_PLACEHOLDER} dana {date.strip().rstrip('.')}."
    return msg + (f" BVK navodi da će kvar biti otklonjen do {cutoff}." if cutoff else "")


def _opstina_of(entry_text: str) -> str | None:
    label, sep, _ = entry_text.partition(":")
    if not sep or not label.strip() or len(label) > 40:
        return None
    name = " ".join(label.split()).upper()
    return _OPSTINA_ALIASES.get(name, name)


def run_scraper():
    print(f"Scraping BVK current network faults from: {LIST_URL}\n")
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(LIST_URL, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        day_sections = soup.find_all("section", class_="av_toggle_section")

        for day_section in day_sections:
            toggler = day_section.find("p", class_="toggler")
            date = toggler.get("data-title", "").strip() if toggler else ""

            content_div = day_section.find("div", class_="toggle_content")
            if not content_div:
                continue

            # Walk headings and entries together, in document order, so each <li> picks up
            # whichever "ДО HH:MM" heading last appeared above it - the same grouping the page
            # itself uses, just not expressed in a form find_all("li") alone could see.
            cutoff = None
            for el in content_div.find_all(_HEADING_TAGS + ["li"]):
                if el.name != "li":
                    m = _DO_TIME_RE.search(el.get_text(" ", strip=True))
                    if m:
                        cutoff = f"{int(m.group(1)):02d}:{m.group(2)}"
                    continue

                entry_text = el.get_text(" ", strip=True)
                if len(entry_text) < 5:
                    continue

                # Each <li> only has "Општина: адресе" - no date and no explicit mention this is
                # a water fault (unlike bvk_scraper.py's prose, which spells that out). Both need
                # to be stated explicitly rather than left for the model to guess, same lesson as
                # the ns_toplana city-guessing bug.
                cutoff_note = f" (до {cutoff} часова)" if cutoff else ""
                raw_text = f"Дана {date} тренутни квар на водоводној мрежи{cutoff_note}. {entry_text}"

                text_hash = compute_text_hash(raw_text)
                if is_hash_processed(text_hash):
                    continue

                print(f"\n🔥 NEW BVK FAULT ({cutoff or 'no cutoff'}): {entry_text[:70]}")
                parsed = parse_announcement(raw_text)
                if parsed:
                    opstina = _opstina_of(entry_text)
                    if opstina:
                        parsed.city = opstina
                    parsed.message_template = _fault_template(date, cutoff)
                    if cutoff:
                        # Known real cutoff from the page itself - override rather than trust
                        # the model's guess, same pattern used for EPS/vikzr dates.
                        for loc in parsed.affected_locations:
                            loc.time_window = f"00:00 - {cutoff}"
                    process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category="WATER",
                                             source_url=LIST_URL, nearby=(NEARBY_M, "BVK"))

    except Exception as e:
        print(f"❌ Scraper error: {e}")


if __name__ == "__main__":
    run_scraper()
