"""Export the outages DB into web/outages.json for the static front page.

The page (web/index.html) reads this file at load time. Run it after a scrape,
or on a schedule, then redeploy the web/ directory.

    python scripts/build_web_data.py

Output shape:
    {
      "meta": {"generated": "<iso8601>", "city": "Srbija", "count": N},
      "outages": [
        {
          "id": 369,
          "kind": "VODA",            # VODA | STRUJA | GREJANJE | RADOVI
          "color": "#5FAEE3",
          "street": "Vojvode Stepe, Učiteljska, Pigova +5",
          "streets": ["Vojvode Stepe", "Učiteljska (parni brojevi 20-33)", "…"],  # full, untruncated, w/ house numbers when the outage isn't the whole street
          "region": "Čačak",
          "date": "28.07.2026",
          "d": 0,                     # whole days from today (can be negative)
          "time": "08:00 - 16:00",
          "source": "Vodovod Čačak",
          "sourceUrl": "https://…",
          "live": false,
          "summary": "Kvar na vodovodnoj mreži.",
          "effects": ["Bez vode dok traje sanacija."],
          "paths": [[[43.891, 20.337], ...], ...],  # [lat, lng] polylines
          "labels": ["Učiteljska 20-33 (parni)", ...],  # the address each path draws, once each
          "pathLabel": [0, 0, 1, ...],                # per path, its index in `labels`
          "areas": [{"hull": [...]}, {"dot": [lat, lng]}, {"pin": [lat, lng], "label": "Pigova 7"},
                    {"pin": [lat, lng], "label": ..., "ring": 200}],
          "maybe": [3, 4],            # paths that are side streets of "i okolne ulice" (dashed)
          "part": [1, 3]              # one of 3 far-apart places of one notice (same id), own card
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Every scraper's outage_date/time_window is the announcement's own Serbian wall-clock time - the
# container this runs in has no TZ set (defaults to UTC, confirmed real case: "u toku" stayed
# true for an already-finished 08:00-12:00 window and never lit up for the real current
# 12:00-16:00 one, a 2h UTC/CEST offset). All "what time is it right now" comparisons need this
# zone explicitly, never the bare system clock.
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

try:
    from config import OUTAGES_DB_PATH
except Exception:
    OUTAGES_DB_PATH = BASE_DIR / "data" / "outage_notifier.db"

import sqlite3

from core.filters import heat_supply_cut, heat_works_only, heating_start, states_time

OUT_PATH = BASE_DIR / "web" / "outages.json"

# How far ahead to keep. The page shows Danas/Sutra/Prekosutra tabs (d 0..2);
# a little extra head-room is cheap and lets the tabs be widened later.
MAX_DAYS_AHEAD = 10
# Past-dated announcements are dropped; the page's tabs are Danas/Sutra/Prekosutra.
MAX_DAYS_BEHIND = 0

KIND_BY_CATEGORY = {
    "WATER": ("VODA", "#5FAEE3"),
    "POWER": ("STRUJA", "#E0B05C"),
    "HEATING": ("GREJANJE", "#E08468"),
}
FALLBACK_KIND = ("RADOVI", "#9AA0A6")

# host fragment -> friendly provider name
SOURCE_BY_HOST = {
    "vikns.rs": "ViK Novi Sad",
    "vodovodca.rs": "Vodovod Čačak",
    "vodovodpa": "Vodovod Pančevo",
    "vodovodkv": "Vodovod Kraljevo",
    "vodovodle": "Vodovod Leskovac",
    "vodovodvranje": "Vodovod Vranje",
    "vodovodva.co.rs": "Vodovod Valjevo",
    "www.jpkikinda.rs": "JP Kikinda",
    "vodovodsabac.rs": "Vodovod Šabac",
    "vikkg": "ViK Kragujevac",
    "vik-kg": "ViK Kragujevac",
    "vikzr": "ViK Zrenjanin",
    "naissus": "Naissus Niš",
    "bvk.rs": "BVK Beograd",
    "eps": "EPS Distribucija",
    "elektrodistribucija": "EPS Distribucija",
    "beoelektrane": "Beogradske elektrane",
    "energetika-kg": "Energetika Kragujevac",
    "energetika-kragujevac": "Energetika Kragujevac",
    "nitoplana": "Gradska toplana Niš",
    "vodovodks": "Vodovod Kruševac",
    "toplanakrusevac": "Toplana Kruševac",
    "grejanje-zr": "Toplana Zrenjanin",
    "toplanazr": "Toplana Zrenjanin",
    "nstoplana": "Novosadska toplana",
    "novosadska-toplana": "Novosadska toplana",
}


# Sources publish in both scripts; the page's own copy is Latin, so street and
# city names are transliterated to match rather than mixing the two on a card.
CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e", "ж": "ž",
    "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m", "н": "n",
    "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ћ": "ć", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "č", "џ": "dž", "ш": "š",
}


def to_latin(text: str) -> str:
    return "".join(CYR_TO_LAT.get(ch, CYR_TO_LAT.get(ch.lower(), ch)) for ch in text)


def title_sr(text: str) -> str:
    """ALLCAPS Cyrillic/Latin street lists -> readable Latin title case."""
    small = {"i", "na", "u", "od", "do", "kod", "za", "sa"}
    out = []
    words = to_latin(text.lower()).split()
    for i, word in enumerate(words):
        # Last word "I" is a numeral ("Kralja Petra I"), not "and".
        out.append(word if (0 < i < len(words) - 1 and word in small) else word[:1].upper() + word[1:])
    # The matchmaker's own "Bogutovac (celo naselje)" / "Lozovik (deo naselja)" is a note, not
    # part of the name.
    return " ".join(out).replace("(celo Naselje)", "(celo naselje)").replace("(deo Naselja", "(deo naselja")


def street_label(affected: str | None) -> str:
    if not affected:
        return "Više ulica"
    parts = [p.strip() for p in affected.split(",") if p.strip()]
    # affected_streets carries one entry per StreetSegment, not per unique street - rule 9
    # (parser.py) deliberately splits "od br. 1-19 i 18-30" on one street into two segments, so
    # the same name can legitimately repeat 2-3x here. Left undeduped this headline read as
    # "Despota Brankovića, Despota Brankovića, Despota Brankovića +17" - dedupe by name (keeping
    # first-seen casing) so the truncated headline only ever names distinct streets, and the
    # "+N" count matches distinct streets left over, not raw segment count.
    seen_fold = set()
    uniq = []
    for p in parts:
        f = p.casefold()
        if f not in seen_fold:
            seen_fold.add(f)
            uniq.append(p)
    shown = uniq[:3]
    label = ", ".join(title_sr(p) for p in shown)
    if len(uniq) > len(shown):
        label += f" +{len(uniq) - len(shown)}"
    return label


def latin_numbers(text: str) -> str:
    """House numbers in Latin, a house letter upper case the way it's written on the house:
    "151ж" -> "151Ž", "12а" -> "12A"."""
    return re.sub(r"(\d)([a-zčćžšđ])\b", lambda m: m[1] + m[2].upper(), to_latin(text))


def location_number_suffix(loc: dict) -> str:
    """House-number qualifier for one affected_locations entry, e.g. ' (parni brojevi 20-33)',
    ' (brojevi 41, 43, 50)' - empty string when the whole street is affected (parity ALL, no
    range/explicit numbers - the common case, and the one that needs no qualifier at all)."""
    parity = (loc.get("parity") or "ALL").upper()
    parity_word = {"PARNA": "parni", "NEPARNA": "neparni"}.get(parity, "")
    explicit_numbers = loc.get("explicit_numbers")
    number_range = loc.get("number_range")

    if explicit_numbers:
        label = f"brojevi {explicit_numbers}"
    elif number_range:
        label = f"{parity_word + ' ' if parity_word else ''}brojevi {number_range}"
    elif parity_word:
        label = f"{parity_word} brojevi"
    else:
        return ""
    return f" ({latin_numbers(label)})"


def street_list_full(affected: str | None, parsed_json: str | None = None) -> list[str]:
    """Every street in the announcement, title-cased, untruncated, with a house-number qualifier
    when the outage doesn't cover the whole street - street_label() above collapses this to
    '<first 3> +N' for the card headline (no numbers, stays compact); the page's expandable
    detail panel shows this full, number-qualified version instead of the '+N' being a dead end.

    Reads parsed_json (the full model output, see core/matchmaker.py) for the per-street
    parity/number_range/explicit_numbers - the flat affected_streets column never carried them.
    Falls back to that flat column (no numbers) for older rows with no cached parsed_json."""
    if parsed_json:
        try:
            locs = json.loads(parsed_json).get("affected_locations", [])
            out = [title_sr(loc["main_street"]) + location_number_suffix(loc)
                   for loc in locs if loc.get("main_street")]
            if out:
                return out
        except Exception:
            pass
    if not affected:
        return []
    return [title_sr(p.strip()) for p in affected.split(",") if p.strip()]


def source_label(url: str | None, region: str | None) -> str:
    if url:
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        for frag, name in SOURCE_BY_HOST.items():
            if frag in host:
                return name
    if region:
        return title_sr(region)
    return "Zvanični izvor"


# --- plain-language summary -------------------------------------------------
# The announcements themselves are long, bureaucratic and inconsistent between
# providers. The page only needs a headline ("what is happening") plus the
# concrete consequences for a resident, so both are derived here from the
# category, the parsed event_type and a keyword scan of raw_text rather than
# shipping the whole notice to the browser.

PLANNED_WORDS = ("planiran", "planska", "radovi", "izvođenje", "извођење", "радови", "remont")
FAULT_WORDS = ("kvar", "квар", "havarij", "хаварij", "хаварија", "sanacij", "санациј", "puknuć", "пукнућ")

# The notice explicitly says nothing gets cut - only the street/traffic is affected. Backs up
# the model's road_closure_only flag with the phrasings providers (toplana especially) actually
# use, so the card never promises a "moguć prekid u snabdevanju" the announcement rules out.
# Matched against a diacritic-folded, transliterated blob (see _fold), so list these ascii-only.
NO_SUPPLY_WORDS = (
    "nece biti prekida",
    "bez prekida u snabdev",
    "bez prekida u isporuci",
    "bez prekida u vodosnabdev",
    "nece doci do prekida",
    "ne dolazi do prekida",
    "snabdevanje nece biti",
    "isporuka toplotne energije nece",
    "nece uticati na snabdev",
    # Novosadska toplana's works notices: "Наведени радови неће утицати на квалитет испоруке
    # топлотне енергије током грејне сезоне." (#1058, #2863)
    "nece uticati na kvalitet isporuke",
    "nece uticati na isporuku",
    "nema uticaja na snabdev",
    "bez obustave isporuke",
    "uredno snabdevanje",
)

_LAT_DIACRITICS = str.maketrans({"š": "s", "ž": "z", "č": "c", "ć": "c", "đ": "dj"})


def _fold(text: str) -> str:
    """lowercase + Cyrillic-to-Latin + strip Serbian Latin diacritics, so a keyword scan
    catches "neće" / "нeће" / "nece" alike."""
    return to_latin((text or "").lower()).translate(_LAT_DIACRITICS)

SUMMARY_BY_KIND = {
    "VODA": ("Kvar na vodovodnoj mreži.", "Planirani radovi na vodovodnoj mreži."),
    "STRUJA": ("Kvar na elektrodistributivnoj mreži.", "Planirani radovi na elektro mreži."),
    "GREJANJE": ("Kvar na toplovodnoj mreži.", "Planirani radovi na toplovodnoj mreži."),
    "RADOVI": ("Kvar na komunalnoj infrastrukturi.", "Planirani komunalni radovi."),
}

BASE_EFFECT = {
    "VODA": "Bez vode u navedenom periodu.",
    "STRUJA": "Bez struje u navedenom periodu.",
    "GREJANJE": "Bez grejanja u navedenom periodu.",
    "RADOVI": "Moguć prekid u snabdevanju.",
}
# What a toplana notice says it cuts (core.filters.heat_supply_cut). When it doesn't say -
# Beogradske elektrane's "Planirani radovi na toplovodnoj mreži" - the card doesn't claim a cut.
HEAT_SUPPLY_EFFECT = {
    "HOT_WATER": "Bez tople vode u navedenom periodu.",
    "HEATING": "Bez grejanja u navedenom periodu.",
    "BOTH": "Bez grejanja i tople vode u navedenom periodu.",
    None: "Moguć prekid grejanja ili tople vode.",
}

# Hot water only counts as cut when the notice says it stops: "tople vode" alone also appears in
# "priključak tople vode" - a hot-water CONNECTION being built, with nothing cut (outage #2863,
# which read "Bez prekida u snabdevanju." and "Bez tople vode." side by side). Matched on _fold().
_HOT_WATER_CUT = re.compile(r"\b(bez|prekid\w*|obustav\w*|iskljucen\w*|nece biti)\W+(\w+\W+){0,4}?topl\w* vod")

# extra consequence worth calling out on the card: (keywords or a pattern on _fold(), the line,
# whether it is a supply cut - left out when the notice says nothing is cut)
EXTRA_EFFECTS = (
    (("redukovan", "редукован", "smanjen pritisak", "смањен притисак", "slab pritisak"),
     "Moguć slab pritisak i u okolnim ulicama.", True),
    (_HOT_WATER_CUT, "Bez tople vode.", True),
    (("cistern", "цистерн"),
     "Voda će biti dostupna iz cisterni.", True),
    (("saobraćaj", "саобраћај"),
     "Izmenjen režim saobraćaja u zoni radova.", False),
    (("hidrant", "хидрант"),
     "Radovi na hidrantskoj mreži.", False),
)


def summarize(kind: str, event_type: str | None, raw_text: str | None,
              road_closure_only: bool = False) -> tuple[str, list[str]]:
    """(headline, effects) in plain Serbian, for the outage detail panel."""
    blob = f"{event_type or ''} {raw_text or ''}".lower()

    fault = any(w in blob for w in FAULT_WORDS)
    planned = any(w in blob for w in PLANNED_WORDS)
    # A notice mentioning both is almost always a fault being repaired.
    fault_headline, planned_headline = SUMMARY_BY_KIND.get(kind, SUMMARY_BY_KIND["RADOVI"])
    headline = fault_headline if fault or not planned else planned_headline

    # The announcement says nothing is actually cut (model flag, or an explicit "neće biti
    # prekida" phrasing). Don't lead the card with "Moguć prekid u snabdevanju." - state the
    # opposite, which is the one thing the resident wants to know.
    folded = _fold(blob)
    # Flushing the network ("испирање водоводне мреже", BVK's multi-night notices, #1984): water
    # comes and goes and the pressure drops, it isn't simply off - unless the notice says so.
    if kind == "VODA" and "ispiranj" in folded and "bez vode" not in _fold(raw_text or ""):
        effects = ["Povremeni prekidi i slabiji pritisak vode."]
        if "zamucen" in folded:
            effects.append("Moguće kratkotrajno zamućenje vode.")
        return "Ispiranje vodovodne mreže.", effects
    # The heating being switched on - tople probe, the season starting (#4298, Niš 12.10.2026,
    # read "Kvar na toplovodnoj mreži. Moguć prekid grejanja" over the whole city).
    start = heating_start(raw_text) if kind == "GREJANJE" else None
    if start:
        return ("Počinje grejna sezona." if start == "SEASON" else "Tople probe - grejanje se uključuje.",
                ["Ništa se ne isključuje - radijatori se zagrevaju.",
                 "Ako grejanje izostane ili nešto curi, javite se toplani."])
    no_supply = road_closure_only or any(w in folded for w in NO_SUPPLY_WORDS)
    base = BASE_EFFECT.get(kind, BASE_EFFECT["RADOVI"])
    if kind == "GREJANJE":
        # What the toplana notice says is cut: "za pripremu potrošne tople vode" read "Bez
        # grejanja" on the card, in September (#3465).
        base = HEAT_SUPPLY_EFFECT[heat_supply_cut(raw_text)]
    effects = ["Bez prekida u snabdevanju."] if no_supply else [base]
    for words, effect, cuts_supply in EXTRA_EFFECTS:
        if cuts_supply and no_supply:
            continue  # the notice says nothing is cut - don't contradict it on the same card
        if effect == "Bez tople vode." and "tople vode" in base and not no_supply:
            continue  # the first line already says it
        hit = words.search(folded) if hasattr(words, "search") else any(w in blob for w in words)
        if hit and effect not in effects:
            effects.append(effect)
    return headline, effects[:3]


def clean_time(window: str | None) -> str:
    # Not always a clock range: providers also write "do 13:30" or
    # "10:30 - завршетак радова", so the free text is kept (transliterated)
    # rather than parsed away.
    value = to_latin((window or "").strip())
    if value.lower() in ("", "n/a", "na", "-", "nepoznato"):
        return ""
    return value


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%Y.", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_expires(value: str | None) -> datetime | None:
    """outages.expires_at -> a Belgrade-time datetime, or None. Set by
    core.matchmaker._compute_expires_at from a multi-day end_date (or the day's time window),
    plus a few hours' grace - it's the authoritative "stop showing this on the map" moment.
    None means the announcement stated no finish, so the outage is treated as single-day."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).replace(tzinfo=BELGRADE_TZ)
        except ValueError:
            continue
    return None


# 5 decimals is ~1 m - finer than any street line on the map can show. The stored GeoJSON carries
# ~15, which made coordinates the bulk of outages.json (3.1 MB on 23.09.2026, mostly digits).
COORD_DECIMALS = 5


def _feature_paths(geom: dict) -> list[list[list[float]]]:
    """One GeoJSON geometry -> list of [lat, lng] polylines, rounded to COORD_DECIMALS."""
    paths: list[list[list[float]]] = []

    def add_line(coords):
        line: list[list[float]] = []
        for pt in coords:
            if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
                continue
            p = [round(pt[1], COORD_DECIMALS), round(pt[0], COORD_DECIMALS)]
            if not line or line[-1] != p:  # rounding can collapse neighbouring points
                line.append(p)
        if len(line) >= 2:
            paths.append(line)

    gtype, coords = (geom or {}).get("type"), (geom or {}).get("coordinates")
    if not coords:
        return paths
    if gtype == "LineString":
        add_line(coords)
    elif gtype in ("MultiLineString", "Polygon"):
        for part in coords:
            add_line(part)
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                add_line(ring)
    elif gtype == "Point" and len(coords) >= 2:
        p = [round(coords[1], COORD_DECIMALS), round(coords[0], COORD_DECIMALS)]
        paths.append([p, list(p)])
    return paths


# A tiny leftover group (a settlement resolved to just one or two street pieces) still draws as a
# dot rather than an oddly-shaped sliver of a hull - a real village-vs-area distinction (below)
# already handles the common case, this is just a sanity floor for whatever that misses.
DOT_THRESHOLD_KM = 1.5


def _hull_extent_km(hull: list[list[float]]) -> float:
    """Rough bounding-box diagonal in km - good enough to classify "village-scale" vs "area-
    scale", not meant as a precise distance."""
    lats = [p[0] for p in hull]
    lngs = [p[1] for p in hull]
    lat_span_km = (max(lats) - min(lats)) * 111.0
    lng_span_km = (max(lngs) - min(lngs)) * 111.0 * math.cos(math.radians(sum(lats) / len(lats)))
    return math.hypot(lat_span_km, lng_span_km)


def _centroid(hull: list[list[float]]) -> list[float]:
    return [round(sum(p[0] for p in hull) / len(hull), COORD_DECIMALS),
            round(sum(p[1] for p in hull) / len(hull), COORD_DECIMALS)]


def address_label(loc: dict) -> str:
    """One affected location as an address, for the map popup of the piece it draws: "Rankeova
    2, 3", "Nebojšina 27-31", "Skerlićeva 2-12 (parni)", "Skerlićeva (parni brojevi)", or the
    street alone when the whole street is affected."""
    street = title_sr(loc.get("main_street") or "")
    parity = {"PARNA": "parni", "NEPARNA": "neparni"}.get((loc.get("parity") or "ALL").upper(), "")
    number_range = loc.get("number_range")
    numbers = ", ".join(n for n in (loc.get("explicit_numbers"), number_range) if n)
    numbers = latin_numbers(numbers)
    if numbers:
        return f"{street} {numbers}" + (f" ({parity})" if parity and number_range else "")
    return f"{street} ({parity} brojevi)" if parity else street


def _street_key(name: str | None) -> str:
    return re.sub(r"[^0-9A-ZČĆŽŠĐ]", "", to_latin(name or "").upper())


def _parsed_locations(parsed_json: str | None) -> list[dict]:
    try:
        return list(json.loads(parsed_json or "{}").get("affected_locations") or [])
    except (ValueError, TypeError, AttributeError):
        return []


def _feature_labels(features: list, parsed_json: str | None) -> list[str]:
    """Per feature, the address it draws - see _feature_locations."""
    return _feature_locations(features, parsed_json)[0]


def _feature_locations(features: list, parsed_json: str | None) -> tuple[list[str], list[int | None]]:
    """Per feature, (the address it draws - "" for a settlement area, the index in
    affected_locations of the location it draws, or None). A named street's line (or pin) is
    paired with its affected location in order, the way core/matchmaker.py's
    _build_visual_features drew them (and scripts/redraw_house_numbers.py pairs them); without
    a stored parse, or a location to pair with, it is labelled with the street name alone. A side
    street drawn for "i okolne ulice" carries its own name and belongs to the named street."""
    all_locs = _parsed_locations(parsed_json)
    locs = [k for k, loc in enumerate(all_locs) if loc.get("main_street")]
    used, labels, owners = set(), [], []
    for feat in features:
        props = (feat or {}).get("properties") or {}
        street = props.get("street") or ""
        label, owner = "", None
        if street and not props.get("area_type"):
            label = title_sr(street)
            key = _street_key(props.get("street_of") or street)
            free = [] if props.get("street_of") or not key else [k for k in locs if k not in used]
            # Exact name first; the drawn name can be shorter (spatial_engine.clean_street drops
            # "ulica", "od broja ...").
            i = next((k for k in free if _street_key(all_locs[k]["main_street"]) == key),
                     next((k for k in free if key in _street_key(all_locs[k]["main_street"])), None))
            if i is not None:
                used.add(i)
                label, owner = address_label(all_locs[i]), i
            elif props.get("street_of"):
                owner = next((k for k in locs if _street_key(all_locs[k]["main_street"]) == key),
                             next((k for k in locs if key in _street_key(all_locs[k]["main_street"])), None))
        labels.append(label)
        owners.append(owner)
    return labels, owners


def extract_shapes(raw: str | None) -> tuple[list[list[list[float]]], list[dict]]:
    """(line_paths, areas) - see extract_shapes_labelled."""
    lines, areas, _ = extract_shapes_labelled(raw)
    return lines, areas


def _named_areas(parsed_json: str | None) -> tuple[dict[str, str], str, str | None]:
    """({map tag: settlement as parsed} for the notice's named whole-area locations, its region,
    the tag of the stand-in drawn for a whole-area location with no name). The tag is what
    core/matchmaker.py's _build_visual_features writes on an area feature: the settlement, or the
    announcement's city when the location names none. Empty without a stored parse, so rows from
    before parsed_json are drawn as they always were."""
    try:
        parsed = json.loads(parsed_json) if parsed_json else None
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return {}, "", None
    try:
        from spatial.spatial_engine import cyrillic_to_latin  # the tag's own transliteration
    except Exception:
        return {}, "", None
    areas = [loc for loc in parsed.get("affected_locations") or []
             if loc.get("location_type") == "ENTIRE_SETTLEMENT"]
    named = {cyrillic_to_latin(loc["settlement"]).title(): loc["settlement"]
             for loc in areas if (loc.get("settlement") or "").strip()}
    city = parsed.get("city") or ""
    nameless = any(not (loc.get("settlement") or "").strip() for loc in areas)
    return named, city, (cyrillic_to_latin(city).title() if nameless and city else None)


def _outlined(named: dict[str, str], tag: str, region: str) -> bool:
    """Draw this whole area's outline, not a dot: a named part of a town (is_town_part)."""
    if not named or tag not in named:
        return False
    try:
        from spatial.spatial_engine import is_town_part
        return is_town_part(named[tag], region)
    except Exception:  # the map must still build without RGZ
        return False


def extract_shapes_labelled(raw: str | None, parsed_json: str | None = None
                            ) -> tuple[list[list[list[float]]], list[dict], list[str]]:
    """FeatureCollection JSON -> (line_paths, areas, line_labels). Each area is {"hull": [...]}
    for a real filled shape, {"dot": [lat, lng]} for a village-scale settlement (see
    DOT_THRESHOLD_KM), or {"pin": [lat, lng], "label": ...} for a single house named by number -
    plus "ring": metres for a fault house whose surroundings may be cut too (BVK kvarovi).
    line_labels[i] is the address line_paths[i] draws (see _feature_labels), so the map popup
    can name the piece that was clicked instead of the whole announcement.

    Each feature carries its own `properties.area_type`/`settlement` tag (set in
    core/matchmaker.py's _build_visual_features, one call per affected_locations entry) - that's
    the real, existing signal for "this geometry belongs to settlement X's area", already computed
    correctly per-settlement upstream and simply not read here before. A multi-settlement
    announcement (a real, common case since parser.py's multi-settlement splitting) produces one
    such group PER settlement; grouping and hulling by that tag instead of by flattening every
    feature into one point soup keeps geographically separate villages from being convex-hulled
    together into one sprawling blob - confirmed real case: 15 unrelated settlements in one EPS
    announcement rendered as a single hull spanning all of them.

    A feature with no area_type is a standalone street line, unrelated to any settlement blob."""
    ex = _extract(raw, parsed_json)
    return ex["lines"], ex["areas"], ex["labels"]


def _extract(raw: str | None, parsed_json: str | None = None) -> dict:
    """extract_shapes_labelled's lines, areas and labels, plus per line `maybe` (a side street
    drawn for "i okolne ulice": nobody named it, so the page draws it dashed - see
    core/matchmaker._queue_side_streets) and, per line and per area, the set of affected
    locations it draws (`line_locs`, `area_locs`: indices into parsed_json's
    affected_locations) - what split_parts keeps together."""
    ex = {"lines": [], "labels": [], "maybe": [], "line_locs": [], "areas": [], "area_locs": []}
    if not raw:
        return ex
    try:
        fc = json.loads(raw)
    except (TypeError, ValueError):
        return ex

    features = fc.get("features", []) if fc.get("type") == "FeatureCollection" else [{"geometry": fc, "properties": {}}]
    named, region, stand_in = _named_areas(parsed_json)
    tag_locs = _area_tag_locations(parsed_json)

    line_items: list[tuple[list[list[float]], str, bool, int | None]] = []
    pins: dict[tuple, str] = {}
    pin_locs: dict[tuple, set] = {}
    rings: dict[tuple, int] = {}
    groups: dict[tuple, list[list[list[float]]]] = {}
    group_kind: dict[tuple, str] = {}
    group_order: list[tuple] = []

    for feat, label, owner in zip(features, *_feature_locations(features, parsed_json)):
        props = (feat or {}).get("properties") or {}
        geom = (feat or {}).get("geometry") or {}
        area_type = props.get("area_type")
        coords = geom.get("coordinates") or []
        if geom.get("type") == "Point" and not area_type and len(coords) >= 2:
            # One house named by number (core/matchmaker.py's _house_number_feature): a pin.
            pin = (round(coords[1], COORD_DECIMALS), round(coords[0], COORD_DECIMALS))
            pins[pin] = _join_labels(pins.get(pin, ""), props.get("label") or label)
            pin_locs.setdefault(pin, set()).update([owner] if owner is not None else [])
            if props.get("nearby_m"):
                rings[pin] = props["nearby_m"]
            continue
        paths = _feature_paths(geom)
        if not paths:
            continue
        if area_type:
            key = (area_type, props.get("settlement") or "")
            if key not in groups:
                groups[key] = []
                group_order.append(key)
                # "street": core/matchmaker.py's get_streets_within_hexes found real RGZ streets
                # for this settlement - genuine detail worth drawing. "area": no street data at
                # all, this is the raw H3-dissolve fallback (see spatial_engine.hexes_to_geojson_
                # feature) - almost always a small rural place the street registry doesn't cover,
                # and its jagged hex-boundary shape reads as a "honeycomb" outline when drawn as a
                # polygon. That's a more honest, size-independent signal than the shape's own
                # physical extent - a real rural settlement's H3 coverage is often 5-15km across
                # even though it's genuinely just one small place with nothing else nearby.
                group_kind[key] = props.get("kind") or ""
            groups[key].extend(paths)
        else:
            line_items.extend((path, label, bool(props.get("street_of")), owner) for path in paths)

    # The same street piece often arrives several times - one feature per affected location, and
    # overlapping locations share segments (outage #2214: 2,859 lines, 955 distinct). Drawing a
    # line twice changes nothing on the map, so ship each once, carrying every address it drew.
    # A piece both named and a side street is named: solid.
    index: dict[tuple, int] = {}
    lines, labels, maybe, line_locs = ex["lines"], ex["labels"], ex["maybe"], ex["line_locs"]
    for line, label, side, owner in line_items:
        key = tuple(map(tuple, line))
        if key not in index:
            index[key] = len(lines)
            lines.append(line)
            labels.append("")
            maybe.append(side)
            line_locs.append(set())
        i = index[key]
        labels[i] = _join_labels(labels[i], label)
        maybe[i] = maybe[i] and side
        if owner is not None:
            line_locs[i].add(owner)

    areas, area_locs = ex["areas"], ex["area_locs"]
    for key in group_order:
        area_type, tag = key
        if area_type == "settlement" and stand_in and tag == stand_in and tag not in named:
            # A whole-area location with no name of its own, drawn as the announcement's city:
            # all of Beograd at one point, a dot in the river at Ušće (#3347). The matcher skips
            # it (calculate_outage_hexes); rows stored before the map did too still carry it.
            continue
        pts = [pt for line in groups[key] for pt in line]
        hull = _convex_hull(pts)
        owners = tag_locs.get(tag, set())
        if len(hull) < 3:
            # Too few points for a real polygon - fall back to drawing this group as lines
            # rather than silently dropping a settlement with only one or two street pieces.
            lines.extend(groups[key])
            labels.extend("" for _ in groups[key])
            maybe.extend(False for _ in groups[key])
            line_locs.extend(set(owners) for _ in groups[key])
            continue
        if area_type == "block":
            # A block inside a ring of streets (core/block_area.py): its own outline, never a
            # dot or a convex hull - the block is often a long bent strip between two streets.
            areas.append({"hull": pts})
        elif area_type == "settlement" and _outlined(named, tag, region):
            # A part of a town (Cerak): the outline is exactly who is told, and a dot at its
            # centre read as one spot (#3465). Villages keep the dot.
            areas.append({"hull": hull})
        elif group_kind.get(key) == "area" or _hull_extent_km(hull) < DOT_THRESHOLD_KM:
            areas.append({"dot": _centroid(hull)})
        else:
            areas.append({"hull": hull})
        area_locs.append(set(owners))
    for p, label in pins.items():
        areas.append({"pin": list(p), **({"label": label} if label else {}),
                      **({"ring": rings[p]} if p in rings else {})})
        area_locs.append(pin_locs.get(p, set()))

    return ex


def _area_tag_locations(parsed_json: str | None) -> dict[str, set]:
    """{map tag: indices of the whole-area locations drawn under it} - the tag being what
    core/matchmaker.py's _build_visual_features writes on an area feature (see _named_areas)."""
    locs = _parsed_locations(parsed_json)
    if not locs:
        return {}
    try:
        from spatial.spatial_engine import cyrillic_to_latin  # the tag's own transliteration
    except Exception:
        return {}
    out: dict[str, set] = {}
    for k, loc in enumerate(locs):
        if not loc.get("main_street") and (loc.get("settlement") or "").strip():
            out.setdefault(cyrillic_to_latin(loc["settlement"]).title(), set()).add(k)
    return out


# One notice naming places far apart - EPS's villages across an opština, BVK's faults across
# Belgrade - is shown as one card per place, each with its own streets and its own zoom, instead
# of one card whose map frame spans them all (26.10.2026: 407 of 2,498 notices in a month had
# pieces more than a kilometre apart). Pieces closer than this stay one card: a street and the
# side streets around it, a neighbourhood's few streets.
PART_GAP_KM = 1.0


def _piece_points(lines: list, areas: list) -> list[list[list[float]]]:
    pts = [line[::max(1, len(line) // 40)] + [line[-1]] for line in lines]
    for a in areas:
        pts.append(a.get("hull") or [a.get("dot") or a.get("pin")])
    return pts


def split_parts(ex: dict, gap_km: float = PART_GAP_KM) -> list[dict]:
    """The notice's pieces (lines and areas of _extract) grouped into the places it names: pieces
    within gap_km of each other are one place, and so are the pieces of one location (a long road
    is one road). Each part: {"lines", "labels", "maybe", "areas", "locs"}. One part - the whole
    notice - unless every part draws at least one location of its own, so a piece the parse
    can't place never becomes a card with no name."""
    lines, areas = ex["lines"], ex["areas"]
    owners = ex["line_locs"] + ex["area_locs"]
    n = len(owners)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    by_loc: dict[int, int] = {}
    for i, locs in enumerate(owners):
        for k in locs:
            if k in by_loc:
                union(i, by_loc[k])
            else:
                by_loc[k] = i

    pts = _piece_points(lines, areas)
    lat0 = math.radians(44.0)
    cy, cx = gap_km / 111.0, gap_km / (111.0 * math.cos(lat0))
    grid: dict[tuple, dict[int, list]] = {}
    for i, piece in enumerate(pts):
        for p in piece:
            grid.setdefault((int(p[0] // cy), int(p[1] // cx)), {}).setdefault(i, []).append(p)

    def near(a, b) -> bool:
        return any(math.hypot((p[0] - q[0]) * 111.0, (p[1] - q[1]) * 111.0 * math.cos(lat0)) <= gap_km
                   for p in a for q in b)

    for (gy, gx), here in grid.items():
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                there = grid.get((gy + dy, gx + dx))
                if not there:
                    continue
                for i, a in here.items():
                    for j, b in there.items():
                        if find(i) != find(j) and near(a, b):
                            union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    if len(groups) < 2:
        return [_part(ex, range(n))]
    parts = [_part(ex, members) for members in groups.values()]
    if any(not part["locs"] for part in parts):
        return [_part(ex, range(n))]
    return sorted(parts, key=lambda part: min(part["locs"]))


def _part(ex: dict, members) -> dict:
    n_lines = len(ex["lines"])
    part = {"lines": [], "labels": [], "maybe": [], "areas": [], "locs": set()}
    for i in members:
        if i < n_lines:
            part["lines"].append(ex["lines"][i])
            part["labels"].append(ex["labels"][i])
            part["maybe"].append(ex["maybe"][i])
            part["locs"] |= ex["line_locs"][i]
        else:
            part["areas"].append(ex["areas"][i - n_lines])
            part["locs"] |= ex["area_locs"][i - n_lines]
    return part


def part_streets(locs: set, parsed_json: str | None) -> tuple[str, list[str]]:
    """(card headline, full street list) for the locations one part draws - street_label and
    street_list_full over just those."""
    all_locs = _parsed_locations(parsed_json)
    chosen = [all_locs[k] for k in sorted(locs) if k < len(all_locs)]
    names = [loc.get("main_street") or loc.get("settlement") or "" for loc in chosen]
    full = [title_sr(loc["main_street"]) + location_number_suffix(loc)
            for loc in chosen if loc.get("main_street")]
    if not full:
        full = list(dict.fromkeys(title_sr(name) for name in names if name))
    if len(chosen) == 1 and chosen[0].get("main_street"):
        # One address: its numbers in the headline too - BVK's two faults on Kumodraška, km apart,
        # are two cards that would otherwise both read "Kumodraška".
        return address_label(chosen[0]), full
    return street_label(", ".join(name for name in names if name)), full


def _index_labels(path_labels: list[str]) -> tuple[list[str], list[int]]:
    """(each distinct label once, per path the index of its label). Both empty when no path has
    one."""
    if not any(path_labels):
        return [], []
    order: dict[str, int] = {}
    index = [order.setdefault(label, len(order)) for label in path_labels]
    return list(order), index


def _join_labels(have: str, add: str) -> str:
    if not add or add in have.split(" · "):
        return have
    return f"{have} · {add}" if have else add


def _convex_hull(points: list[list[float]]) -> list[list[float]]:
    """Andrew's monotone chain. Input/output points are [lat, lng]."""
    pts = sorted(set((round(p[0], COORD_DECIMALS), round(p[1], COORD_DECIMALS)) for p in points))
    if len(pts) < 3:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def in_progress(d: int, window: str | None, target_day: date) -> bool:
    if d != 0 or not window or target_day != datetime.now(BELGRADE_TZ).date():
        return False
    try:
        start, end = [part.strip() for part in window.split("-", 1)]
        now = datetime.now(BELGRADE_TZ).strftime("%H:%M")
        return start <= now <= end
    except ValueError:
        return False


# The city SEO pages (scripts/build_city_pages.py) run on GitHub's runners with no DB, so the feed
# carries a small per-region record of the past month: a page on a quiet day still says how often
# that place has had outages and where. Only what the map showed counts (a row with a shape).
HISTORY_DAYS = 30
HISTORY_RECENT = 5

# Neighbourhood pages: which kvartovi each outage touches
# (core/kvartovi.py), and the past month per kvart. Every visitor of the map downloads this feed,
# so a past outage is written once ("kv_recent") and each kvart points to it by index.
_kv_failed = False


def kvartovi_of(fn: str, *args) -> list[str]:
    """core.kvartovi.<fn>(*args), or [] - a missing data file or a shapely error must never cost
    the map its feed."""
    global _kv_failed
    if _kv_failed:
        return []
    try:
        from core import kvartovi
        return getattr(kvartovi, fn)(*args)
    except Exception as e:
        _kv_failed = True
        print(f"⚠️ neighbourhoods left out of the feed: {e}")
        return []


# "days": the past month day by day for the city pages' strip, one hex digit per day from
# today - HISTORY_DAYS to today (oldest first), each the kinds cut that day as flags - "0" none,
# "1" power, "3" power and water. ~31 bytes per place.
DAY_BIT = {"STRUJA": 1, "VODA": 2, "GREJANJE": 4}
DAY_OTHER = 8


def _mark_day(entry: dict, today: date, day: date, kind: str) -> None:
    flags = entry.setdefault("_days", [0] * (HISTORY_DAYS + 1))
    flags[HISTORY_DAYS - (today - day).days] |= DAY_BIT.get(kind, DAY_OTHER)


def _finish_days(entry: dict) -> None:
    flags = entry.pop("_days", None)
    if flags:
        entry["days"] = "".join(format(f, "x") for f in flags)


def history(rows, today: date) -> dict:
    regions: dict[str, dict] = {}
    kvartovi: dict[str, dict] = {}
    past: list[tuple] = []  # (date, kind, street, region, (kvart ids)) of each past outage
    seen = set()
    for row in rows:
        day = parse_date(row["outage_date"])
        if day is None or not (today - timedelta(days=HISTORY_DAYS) <= day <= today):
            continue
        if not row["region"] or (row["geojson_geometry"] or "null") == "null":
            continue
        region = title_sr(row["region"])
        kind = KIND_BY_CATEGORY.get(row["category"], FALLBACK_KIND)[0]
        street = street_label(row["affected_streets"])
        # A re-published notice (BVK edits a post, EPS repeats a row) is one outage, not two.
        key = (region, day, kind, street.casefold())
        if key in seen:
            continue
        seen.add(key)
        entry = regions.setdefault(region, {"counts": {}, "recent": []})
        entry["counts"][kind] = entry["counts"].get(kind, 0) + 1
        _mark_day(entry, today, day, kind)
        if day < today:  # today's are on the page's own list already
            entry["recent"].append({"date": day.isoformat(), "kind": kind, "street": street})
        kv = kvartovi_of("for_geojson", row["geojson_geometry"])
        for k in kv:
            kv_entry = kvartovi.setdefault(k, {"counts": {}, "recent": []})
            kv_entry["counts"][kind] = kv_entry["counts"].get(kind, 0) + 1
            _mark_day(kv_entry, today, day, kind)
        if kv and day < today:
            past.append((day.isoformat(), kind, street, region, tuple(kv)))
    for entry in regions.values():
        entry["recent"] = sorted(entry["recent"], key=lambda r: r["date"], reverse=True)[:HISTORY_RECENT]
        _finish_days(entry)
    for entry in kvartovi.values():
        _finish_days(entry)
    # Each kvart's latest few, the outages written once and pointed to.
    kv_recent, index = [], {}
    for item in sorted(past, reverse=True):
        for k in item[4]:
            recent = kvartovi[k]["recent"]
            if len(recent) >= HISTORY_RECENT:
                continue
            if item not in index:
                index[item] = len(kv_recent)
                kv_recent.append({"date": item[0], "kind": item[1], "street": item[2], "region": item[3]})
            recent.append(index[item])
    return {"days": HISTORY_DAYS, "regions": regions, "kvartovi": kvartovi, "kv_recent": kv_recent}


def build(today: date | None = None) -> dict:
    try:
        conn = sqlite3.connect(f"file:{OUTAGES_DB_PATH}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM outages WHERE status = 'ACTIVE' ORDER BY outage_date"
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        # DB busy (a worker holds the write lock). Read an immutable snapshot;
        # safe for an export that only needs a consistent point-in-time view.
        conn = sqlite3.connect(f"file:{OUTAGES_DB_PATH}?immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM outages WHERE status = 'ACTIVE' ORDER BY outage_date"
        ).fetchall()
        conn.close()

    now_dt = datetime.now(BELGRADE_TZ)
    if today is not None:
        # Caller pinned "day zero" (tests, or the --today= CLI override). Anchor "now" to noon
        # of that same day so the expires_at gate below is deterministic instead of drifting
        # with the wall clock - otherwise a fixture that pins `today` still silently depends on
        # when the suite happens to run. The real pipeline passes no `today`, so it is unaffected.
        now_dt = now_dt.replace(year=today.year, month=today.month, day=today.day,
                                hour=12, minute=0, second=0, microsecond=0)
    today = today or now_dt.date()
    outages = []
    for row in rows:
        day = parse_date(row["outage_date"])
        if day is None:
            continue
        d_start = (day - today).days
        expires = parse_expires(row["expires_at"])
        if expires is not None:
            # Multi-day span or an explicit finish date ("radovi do 20.09", "kraj radova
            # planiran za …") - keep it on the map until expires_at actually passes, no matter
            # how many days ago it started. Previously the row vanished the day after its START
            # date because visibility was keyed on outage_date alone; confirmed on a Novi Sad
            # roadworks notice that dropped off a day in despite stating a later end date.
            if now_dt > expires or d_start > MAX_DAYS_AHEAD:
                continue
        elif d_start < -MAX_DAYS_BEHIND or d_start > MAX_DAYS_AHEAD:
            # No stated finish -> single-day: only its own date (plus the existing head-room).
            continue
        # Which day-tabs this outage sits on. A multi-day span shows on EVERY day it covers,
        # not just "Danas" - a roadwork announced for 24.08-20.09 is genuinely happening on
        # sutra and prekosutra too. first_d: its start (0 if it's already running). last_d: the
        # last covered day, from expires_at; without one it's a single-day notice. Clamped to
        # the three tabs the page renders (Danas / Sutra / Prekosutra).
        LAST_TAB_D = 2
        first_d = max(0, d_start)
        if first_d > LAST_TAB_D:
            # Starts after Prekosutra (EPS "Dan 3"+ / nakosutra). It's scraped and stored, but
            # held off the map until it slides into the Danas/Sutra/Prekosutra window. Without
            # this, a day-3 single-day outage clamped to first_d==last_d==2 below and still
            # rendered on the Prekosutra tab.
            continue
        if expires is not None:
            end_day = expires.date()
            # matchmaker._compute_expires_at adds a few hours' grace ("works run late"), which
            # tips a same-day 07:00-22:00 cut past midnight (-> 01:00 next day). That grace is a
            # visibility cushion, not another day of outage - if expires lands in the small
            # hours past the start day, the outage really ended the day before. (A real
            # end_date lands at 02:59 too, so this also recovers the true last day for those.)
            if end_day > day and expires.hour < 6:
                end_day -= timedelta(days=1)
            last_d = (end_day - today).days
        else:
            last_d = d_start
        last_d = min(last_d, LAST_TAB_D)
        first_d = min(first_d, LAST_TAB_D)
        if last_d < first_d:
            continue

        ex = _extract(row["geojson_geometry"], row["parsed_json"])
        if not ex["lines"] and not ex["areas"]:
            continue
        parts = split_parts(ex)
        kind, color = KIND_BY_CATEGORY.get(row["category"], FALLBACK_KIND)
        try:
            rco = bool(json.loads(row["parsed_json"]).get("road_closure_only")) if row["parsed_json"] else False
        except (ValueError, TypeError, AttributeError):
            rco = False
        headline, effects = summarize(kind, row["event_type"], row["raw_text"], road_closure_only=rco)

        multiday = last_d > first_d or d_start < 0
        if multiday and expires is not None:
            # end_day, not expires.date(): expires carries the grace hours, which put a notice
            # ending 21.10. at "22.10." on the card (#2863, and #1058 read 26.10. for 25.10.).
            date_label = "%s–%s" % (day.strftime("%d.%m."), end_day.strftime("%d.%m."))
        else:
            date_label = day.strftime("%d.%m.%Y.")

        # Works that run for days or weeks with no hours in the notice go on around the clock;
        # the "07:00 - 19:00" on them was the parser's fill-in (rule 8), not the source's. Shown
        # as "ceo dan", and "u toku" for the whole day. Same for a toplana's works notice with no
        # stated cut (Beogradske elektrane's daily "Planirani radovi na toplovodnoj mreži").
        works = kind == "GREJANJE" and heat_works_only(row["raw_text"])
        all_day = (kind == "RADOVI" or rco or multiday or works) and not states_time(row["raw_text"])
        time_label = "ceo dan" if all_day else clean_time(row["time_window"])

        base = {
            "id": row["id"],
            "kind": kind,
            "color": color,
            "street": street_label(row["affected_streets"]),
            "streets": street_list_full(row["affected_streets"], row["parsed_json"]),
            "region": title_sr(row["region"]) if row["region"] else "",
            "date": date_label,
            "time": time_label,
            "source": source_label(row["source_url"], row["region"]),
            "sourceUrl": row["source_url"] or "",
            "summary": headline,
            "effects": effects,
        }
        for k, part in enumerate(parts):
            # `paths` is standalone street lines only; `areas` is one shape per DISTINCT
            # settlement, either {"hull": [...]} or {"dot": [lat,lng]} for a village-scale one
            # (see extract_shapes) - kept separate so unrelated settlements in the same
            # announcement never get convex-hulled together into one sprawling blob, and so the
            # page doesn't redraw an area's hull outline a second time as a thin line. The page's
            # bounds/fit maths (web/index.html) reads both fields, not just `paths`.
            piece = dict(base, paths=part["lines"], areas=part["areas"])
            if len(parts) > 1:
                # One place of a notice naming several far apart (split_parts): its own card.
                piece["street"], piece["streets"] = part_streets(part["locs"], row["parsed_json"])
                piece["part"] = [k + 1, len(parts)]
            kv = kvartovi_of("for_shapes", piece["paths"], piece["areas"])
            if kv:
                piece["kv"] = kv
            # A side street drawn for "i okolne ulice" (paths[i] for i in `maybe`): dashed.
            maybe = [i for i, side in enumerate(part["maybe"]) if side]
            if maybe:
                piece["maybe"] = maybe
            # The address each path draws ("Rankeova 2, 3"): the map popup names the piece that
            # was clicked. Each name once in `labels`, `pathLabel[i]` its index for paths[i] - a
            # street drawn in 40 pieces would otherwise carry its name 40 times (+12% feed). Left
            # out when the card is one street: the popup names it already.
            labels, path_label = _index_labels(part["labels"])
            named = [label for label in labels if label]
            if len(named) > 1 or (named and len(piece["streets"]) > 1):
                piece["labels"], piece["pathLabel"] = labels, path_label
            for d in range(first_d, last_d + 1):
                entry = dict(piece)
                entry["d"] = d
                # d==0 covers a same-day outage and an ongoing multi-day span alike - either way
                # the question is "are we inside today's stated hours", so check against today,
                # not the (possibly past) start date. d>0 copies are never "live".
                entry["live"] = (d == 0) if all_day else \
                    in_progress(d, row["time_window"], today if d == 0 else day)
                outages.append(entry)

    outages.sort(key=lambda o: (o["d"], o["street"]))
    regions = {o["region"] for o in outages if o["region"]}
    city = regions.pop() if len(regions) == 1 else "Srbija"

    return {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "city": city,
            "count": len({o["id"] for o in outages}),
        },
        "outages": outages,
        "history": history(rows, today),
    }


def main() -> None:
    # --today=YYYY-MM-DD pins "day zero" instead of using the clock. Serbian
    # utilities publish same-day notices on working days only, so on a weekend
    # a live run legitimately finds nothing; this makes it possible to check the
    # export against a weekday without waiting for one.
    pinned = None
    dry_run = "--dry-run" in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith("--today="):
            pinned = datetime.strptime(arg.split("=", 1)[1], "%Y-%m-%d").date()

    data = build(pinned)
    label = f" (as of {pinned})" if pinned else ""
    if dry_run:
        print(f"[dry-run] {data['meta']['count']} outages{label}, city={data['meta']['city']}")
        for o in data["outages"][:20]:
            print(f"  d={o['d']} {o['kind']:9} {o['time'] or '—':16} {o['street'][:44]}")
        return

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH.relative_to(BASE_DIR)} - {data['meta']['count']} outages{label}, "
          f"city={data['meta']['city']}")


if __name__ == "__main__":
    main()
