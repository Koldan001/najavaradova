import re
import sys
import os
import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from parser import parse_announcement, StreetSegment
from core.database import get_connection
from core.matchmaker import process_and_queue_outage
from scrapers.eps_scraper import compute_text_hash, is_hash_processed, refresh_source_url
from spatial.spatial_engine import cyrillic_to_latin
from scrapers._common import use_scraper_date

LIST_URL = "https://nstoplana.rs/category/vesti/"

# The "Вести" category mixes outage/works announcements with unrelated posts (meter-reading
# notices, procurement, etc.). Slugs are consistent Latin ASCII, so filter on those rather than
# the Cyrillic title. Covers every outage-shaped announcement we've seen so far:
#   prekid    -> "ПРЕКИД(У) У ИСПОРУЦИ..." (interruption)
#   radova    -> "ПОЧЕТКУ/НАСТАВКУ РАДОВА" (works started/continued)
#   poremecaj -> "ПОРЕМЕЋАЈУ ИСПОРУКЕ..." (supply disruption)
RELEVANT_SLUG_KEYWORDS = ["prekid", "radova", "poremecaj"]

# A "почетак/наставак/завршетак радова" post is a works-lifecycle notice: crews are starting /
# resuming / finishing works on the network. It only belongs on the map as grey context
# (category="WORKS", no notifications) UNLESS its body actually announces a hot-water/heating
# cut. A "prekid"/"poremecaj" slug always names a cut and stays HEATING. Without this a bare
# "obavestenje-o-pocetku-radova" was stored coloured and notifiable.
_WORKS_LIFECYCLE_SLUG = (
    "pocetku-radova", "pocetak-radova", "nastavku-radova", "nastavak-radova",
    "zavrsetku-radova", "zavrsetak-radova", "zavrsetku-radova", "okoncanju-radova",
)
# A hot-water / heating cut, stated plainly.
_SUPPLY_CUT_PHRASES = (
    "без топле воде", "без грејањ", "без греја", "без тпв",
    "неће имати топлу воду", "неће имати грејање", "неће бити топле воде",
    "неће бити грејања", "нема испоруке топле воде",
)
# "прекид / обустав / поремећај / искључењ" only counts as a supply cut when the SUPPLY is the
# object nearby - not "обуставља се саобраћај", "привремена измена режима саобраћаја", "улица
# постаје слепа". Confirmed: nstoplana's "почетак радова на изградњи вреловодног прикључка ...
# потпуно се обуставља саобраћај" cuts no hot water, it's a road closure.
_CUT_VERBS = ("прекид", "обустав", "поремећај", "искључењ")
_SUPPLY_OBJECTS = ("испоруц", "испорук", "топле воде", "топлотне енергије", "грејањ", "тпв", "снабдевањ")


def _is_supply_cut(raw_text: str) -> bool:
    low = (raw_text or "").lower()
    if any(p in low for p in _SUPPLY_CUT_PHRASES):
        return True
    for verb in _CUT_VERBS:
        i = low.find(verb)
        while i != -1:
            if any(obj in low[i:i + 60] for obj in _SUPPLY_OBJECTS):
                return True
            i = low.find(verb, i + 1)
    return False

# nstoplana slugs carry no date (prekid/radova/poremecaj only), so the outage date has to come
# from the prose. It is almost always spelled out ("...у среду, 26. августа 2026. године...");
# a bare numeric "26.08.2026" is the exception. The post's publish/modified <time> stamp is
# NOT usable as a fallback: nstoplana re-touches old posts (bumping that stamp weeks later),
# and a run that trusted it re-ingested an August outage as a fresh row dated "today" and
# mailed live users (outage #1063). No date in the prose -> return None and let the parser read
# it from the words itself, rather than substitute a wrong one.
# nstoplana prose sprays stray spaces mid-token ("202 6", "20 26", "26 . августа") - the year
# group is deliberately loose and whitespace is squeezed out before validation.
_BODY_DATE_RE = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d(?:[\d ]{0,4})\d|\d{4})")

# Genitive month forms as they appear in "дана 26. августа 2026". Serbian has a fleeting 'a'
# (септембар -> септембра, октобар -> октобра), so match on the stem that survives declension
# and let a trailing \w* absorb the ending. Cyrillic and Latin both occur on the site.
_MONTH_STEMS = [
    ("јануар", 1), ("фебруар", 2), ("март", 3), ("април", 4), ("мај", 5), ("јун", 6),
    ("јул", 7), ("август", 8), ("септемб", 9), ("октоб", 10), ("новемб", 11), ("децемб", 12),
    ("januar", 1), ("februar", 2), ("mart", 3), ("april", 4), ("maj", 5), ("jun", 6),
    ("jul", 7), ("avgust", 8), ("septemb", 9), ("oktob", 10), ("novemb", 11), ("decemb", 12),
]
_WORD_DATE_RE = re.compile(
    r"(\d{1,2})\s*\.?\s*(" + "|".join(s for s, _ in _MONTH_STEMS) + r")\w*\.?\s+(\d(?:[\d ]{0,4})\d|\d{4})",
    re.IGNORECASE,
)
_MONTH_BY_STEM = {s.lower(): n for s, n in _MONTH_STEMS}


def _valid_dmy(d, mth, y) -> str | None:
    y = re.sub(r"\s+", "", str(y or ""))
    if len(y) != 4:
        return None
    try:
        from datetime import datetime as _dt
        _dt(int(y), int(mth), int(d))
        return f"{int(d):02d}.{int(mth):02d}.{y}"
    except (ValueError, TypeError):
        return None


def _source_url_already_stored(article_url: str) -> bool:
    """True if this exact article URL already backs a stored outage. nstoplana re-fetches the
    same live article every scrape, and _extract_date below isn't guaranteed to return the same
    date every run on it (the numeric-date regex can pick up our own previously-injected
    "Обавештење за дан ..." prefix as well as a real date in the prose - confirmed real case:
    one "obavestenje-o-pocetku-radova-25" article (Гундулићева, objekat 29) produced THREE
    separate outage rows - 173, 1087, 1061 - because the extracted date drifted between scrapes,
    which changed the hash, which looked like a brand-new announcement each time). Those three
    happened to be category=WORKS, which never queues notifications (matchmaker.py's
    `category != 'WORKS'` gate) - harmless this time, but the same drift on a real "прекид у
    испоруци" HEATING post would double-notify subscribers already told once, since
    (outage_id, target_id, channel) dedup can't see across a new outage_id. Guard on source_url
    instead, same pattern as NS_water_scraper.py's _source_url_already_stored: once this exact
    URL backs a stored row, never ingest it again."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT 1 FROM outages WHERE source_url = ? LIMIT 1", (article_url,)
        ).fetchone() is not None
    finally:
        conn.close()


def _extract_date(raw_text: str) -> str | None:
    t = raw_text or ""
    m = _BODY_DATE_RE.search(t)
    if m:
        iso = _valid_dmy(*m.groups())
        if iso:
            return iso
    m = _WORD_DATE_RE.search(t)
    if m:
        iso = _valid_dmy(m.group(1), _MONTH_BY_STEM.get(m.group(2).lower()), m.group(3))
        if iso:
            return iso
    return None

# Novi Sad's district heating grid is split into 7 named zones ("ТО" = toplotna oblast/toplana).
# Some announcements ("... за објекте на конзумном подручју ТО „Запад“.") name a whole zone
# instead of listing any streets in the article body — the actual street list lives on a
# per-zone page. Without expanding these, the parser has nothing to extract and the outage is
# silently dropped despite covering dozens of streets.
TO_ZONE_SLUGS = {
    "ЗАПАД": "to-zapad",
    "ИСТОК": "to-istok",
    "СЕВЕР": "to-sever",
    "ЈУГ": "to-jug",
    "ПЕТРОВАРАДИН": "to-petrovaradin",
    "ДУДАРА": "to-dudara",
    "МАЈЕВИЦА": "to-majevica",
}

# Matches "... подручју [ТО/Топлане] „Запад“ ..." and captures the quoted zone name. The zone
# reference is always right after "подручј..." (consumption/service area), so anchor there
# rather than trying to enumerate every "ТО"/"Топлана" phrasing directly.
TO_ZONE_PATTERN = re.compile(r'подручј\w*.{0,25}?[„"]([^„"“”]+)[“"]', re.IGNORECASE)

_zone_streets_cache: dict[str, list[str]] = {}


# Zone-table cells are terser than announcement prose and carry junk main_street was never
# meant to hold: parenthetical/guillemet asides ("Хајдук Вељкова осим броја 11 („Север“)" - a
# note that house #11 actually belongs to a different zone), "осим/изузев" exception clauses,
# and embedded house-number detail ("Руменачка: 39 – 53, 148 – 148а", "Футошка 68-74 Јодна
# бања..."). Left as-is these fail street-registry lookup and drag match_quality down to
# UNRESOLVED. We only need the bare street name here - stripping down to that (rather than
# preserving the number detail) errs toward covering the whole street, which is the safer
# failure mode than silently dropping it to unresolved.
_ZONE_TRAILING_NOTE_RE = re.compile(r'[„"(].*$')
_ZONE_EXCEPT_CLAUSE_RE = re.compile(r'\s+(?:осим|изузев)\b.*$', re.IGNORECASE)
_ZONE_NUMBER_SUFFIX_RE = re.compile(r'[:\s]+\d.*$')


def _clean_zone_street_name(raw: str) -> str:
    text = _ZONE_TRAILING_NOTE_RE.sub('', raw)
    text = _ZONE_EXCEPT_CLAUSE_RE.sub('', text)
    text = _ZONE_NUMBER_SUFFIX_RE.sub('', text)
    return text.strip(' :;,').strip()


def _fetch_zone_streets(zone_slug: str, headers: dict) -> list[str]:
    """Scrapes the street table off a https://nstoplana.rs/to-<zone>/ page. Cached per-process
    since these zone boundaries change rarely, and one scraper run may hit the same zone
    multiple times across announcements."""
    if zone_slug in _zone_streets_cache:
        return _zone_streets_cache[zone_slug]

    streets: list[str] = []
    try:
        res = requests.get(f"https://nstoplana.rs/{zone_slug}/", headers=headers, timeout=25)
        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table", class_="table")
        if table:
            for td in table.find_all("td"):
                name = _clean_zone_street_name(td.get_text(" ", strip=True).replace("\xa0", " "))
                if name:
                    streets.append(name)
    except Exception as e:
        print(f"⚠️ Could not fetch zone streets for {zone_slug}: {e}")

    _zone_streets_cache[zone_slug] = streets
    return streets


def _build_zone_alert(raw_text: str, headers: dict):
    """If the announcement names a whole TO zone instead of listing streets, build the outage
    directly from the zone's known street list instead of routing it through Gemini.

    We already have all ~77 street names verbatim from the zone page - there's nothing for the
    LLM to extract there. Only outage_date/time_window/event_type need parsing, and those come
    from the short intro sentence alone. Earlier this appended the full street list into
    raw_text and asked Gemini to emit a StreetSegment (11 fields + 3 message variations) per
    street: for TO "Zapad" that's an 80-object structured response and took ~150s for one
    announcement. Parsing the intro (~2s) and building the 77 StreetSegments locally is
    near-instant and gives identical results, since those fields were never AI-derived data
    anyway - they're read straight off the zone table plus a template.

    Returns an OutageAlert, or None if this announcement doesn't reference a known zone (caller
    should fall back to a normal parse_announcement call in that case).
    """
    match = TO_ZONE_PATTERN.search(raw_text)
    if not match:
        return None

    zone_name = match.group(1).strip().upper()
    zone_slug = TO_ZONE_SLUGS.get(zone_name)
    if not zone_slug:
        return None

    streets = _fetch_zone_streets(zone_slug, headers)
    if not streets:
        return None

    base = parse_announcement(raw_text)
    if not base:
        return None

    time_window = base.affected_locations[0].time_window if base.affected_locations else "00:00 - 24:00"
    zone_title = cyrillic_to_latin(zone_name).title()

    base.affected_locations = [
        StreetSegment(
            location_type="STREET",
            main_street=street.upper(),
            parity="ALL",
            time_window=time_window,
        )
        for street in streets
    ]

    # One template for the whole zone; matchmaker substitutes each recipient's own street into
    # the *** slot at queue time.
    base.message_template = (
        f"Prekid u isporuci tople vode u ulici *** dana {base.outage_date} "
        f"(radovi na TO \"{zone_title}\")."
    )

    print(f"   ↳ built ТО „{zone_name}“ outage locally from {len(streets)} streets ({zone_slug}), skipped per-street Gemini calls")
    return base


def run_scraper():
    print(f"Scraping NS Toplana announcements from: {LIST_URL}\n")
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(LIST_URL, headers=headers, timeout=25)
        soup = BeautifulSoup(response.text, "html.parser")
        processed_urls = set()

        for card in soup.find_all("article", class_="entry-card"):
            title_link = card.select_one("h2.entry-title a[href]")
            if not title_link:
                continue

            article_url = title_link["href"]
            if article_url in processed_urls:
                continue
            if not any(kw in article_url for kw in RELEVANT_SLUG_KEYWORDS):
                continue

            processed_urls.add(article_url)

            try:
                # nstoplana.rs is genuinely slow (confirmed real fetches taking >5s) - a tight
                # timeout here meant missed announcements on a normal response, not a dead site.
                article_res = requests.get(article_url, headers=headers, timeout=25)
                article_soup = BeautifulSoup(article_res.text, "html.parser")
                # The real post body lives in the <article> with an id like "post-25304";
                # the header/sidebar also carry stray ".entry-content" blocks (address box,
                # "recent posts" widget) that this id-scoped lookup skips.
                article_tag = article_soup.find("article", id=lambda v: v and v.startswith("post-"))
                content_div = article_tag.find("div", class_="entry-content") if article_tag else None
                # Street lists are frequently given as <ul><li> blocks, not prose inside <p> tags
                # (e.g. "...за објекте у следећим улицама:" followed by a bare <ul>) - grabbing
                # only <p> silently drops every street name in those announcements.
                blocks = content_div.find_all(["p", "li"]) if content_div else []
                body_text = " ".join(b.get_text(" ", strip=True) for b in blocks if b.get_text(strip=True))
                raw_text = body_text.strip()
            except Exception as e:
                print(f"⚠️ Could not fetch {article_url}: {e}")
                continue

            if len(raw_text) < 30:
                continue

            if _source_url_already_stored(article_url):
                # Already have a row for this exact article - skip before even extracting a date,
                # so a drifted re-extraction can't manufacture a duplicate outage_id. See
                # _source_url_already_stored's docstring for the real case that caught this.
                continue

            # Put the prose date in front of the parser (numeric or spelled-out), so both the
            # zone path and the plain parse read a real date instead of guessing. None when the
            # prose has none - we do NOT fall back to the post's publish time (see _extract_date).
            date_str = _extract_date(raw_text)
            if date_str and not raw_text.lstrip().startswith(("Датум", "Обавештење за дан")):
                raw_text = f"Обавештење за дан {date_str}. {raw_text}"

            text_hash = compute_text_hash(raw_text)
            if is_hash_processed(text_hash):
                # Already stored. Rows saved before this scraper started passing source_url
                # (2026-09-02) have none, so the map popup's "Zvanična objava" link never shows
                # for them - fill it in from the article URL we already have in hand.
                refresh_source_url(text_hash, article_url)
                continue

            print(f"\n🔥 NEW TOPLANA ANNOUNCEMENT: {article_url}")
            parsed = _build_zone_alert(raw_text, headers) or parse_announcement(raw_text)
            if parsed:
                # This scraper only ever reads nstoplana.rs - every announcement here IS Novi Sad
                # by construction. Confirmed real case: a zone-only announcement (e.g. "...ТО
                # „Запад“...") never names a city at all, so Gemini has nothing to ground a guess
                # on and can get it wrong (a real one landed as "БЕОГРАД" and got mapped there on
                # the site's own map). Overriding with the known-true value removes the guess
                # entirely rather than hoping the model gets lucky.
                parsed.city = "НОВИ САД"
                if date_str:
                    use_scraper_date(parsed, date_str, raw_text)
                is_works_lifecycle = any(k in article_url for k in _WORKS_LIFECYCLE_SLUG)
                category = "WORKS" if (is_works_lifecycle and not _is_supply_cut(raw_text)) else "HEATING"
                process_and_queue_outage(parsed, raw_text, text_hash=text_hash, category=category,
                                         source_url=article_url)

    except Exception as e:
        print(f"❌ Scraper error: {e}")


if __name__ == "__main__":
    run_scraper()
