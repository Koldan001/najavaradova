import functools
import json
import math
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
import h3
import config
from core.database import get_connection
from core.alerts import send_operator_alert, send_operator_note
from core.filters import heat_supply_cut, heat_works_only, heating_start, is_house_number_affected, states_time
from core.api_budget import remaining
from core import block_area
from core.notice import CHANGED_PREFIX
from parser import OutageAlert, StreetSegment, parse_announcement
from spatial.spatial_engine import (
    get_street_hexes,
    get_local_street_segments,
    _clip_segments_to_cells,
    clip_polyline_by_intersections,
    get_settlement_polygon_hexes,
    get_settlement_coverage,
    get_street_geojson_feature,
    get_settlement_info,
    hexes_to_geojson_feature,
    get_streets_within_hexes,
    get_connecting_streets,
    clean_street,
    cyrillic_to_latin,
    get_smart_stem,
    is_generic_street_token,
    region_envelope,
    region_opstine,
    is_serbian_opstina,
    envelope_distance_km,
    street_name_variants,
    query_rgz_streets,
    rgz_house_points,
    rgz_naselje_street_count,
    house_number_stretch,
    naselje_owner_opstine,
    _dealias_naselje,
    rgz_street_naselja,
    _norm_region_lat,
    RGZ_DB_PATH,
    h3
)

# H3 res9 grid steps below which a settlement counts as "village scale". Calibrated against the
# real cached settlements in this project's data: every genuine village clusters at 25-64
# (Bukovac 25, Čantavir 37, Ljutovo 41, Bajmok 56, Bezdan 64, ...), every genuine city/town sits
# at 66+ (Zrenjanin 66, Pančevo 79, Sombor 90, Subotica 94) — 65 sits cleanly in the gap. Above
# this, a resolved settlement is big enough that broadcasting to its whole polygon as a fallback
# for one unresolved street risks over-notifying people nowhere near the real outage.
SMALL_SETTLEMENT_RADIUS_THRESHOLD = 65

_QUALITY_RANK = {'EXACT': 0, 'FALLBACK': 1, 'UNRESOLVED': 2}


def _surrounding_streets(main_street: str, settlement: str, region: str,
                         int_start: str = None, int_end: str = None) -> tuple[set[str], list[dict]]:
    """The unnamed side streets an "и суседним улицама" / "и околне улице" / "у ширем рејону око"
    phrase pulls in: streets that physically connect to the named one (shared endpoint in the
    RGZ geometry, not a geometric ring), each trimmed to just the stretch within one H3 ring of
    the named street. Both halves matter — connection picks the right few streets, the trim
    keeps a connecting street that happens to run clear across town from being drawn/notified
    along its whole length.

    The zone is built from the named street CLIPPED to the announcement's own cross-street
    bounds ("на потезу од Коче Коларова до Београдске"), not the whole street — otherwise a long
    street makes a long corridor and every street running alongside it for a block lands inside.
    Returns (hexes for matching, line features for the map)."""
    feat = get_street_geojson_feature(
        main_street, settlement=settlement, parent_region=region,
        intersection_start=int_start, intersection_end=int_end
    )
    if not feat and (int_start or int_end):
        feat = get_street_geojson_feature(main_street, settlement=settlement, parent_region=region)
    if not feat:
        return set(), []

    geom = feat["geometry"]
    segments = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]

    named_cells = {
        h3.latlng_to_cell(pt[1], pt[0], 9)
        for seg in segments for pt in seg if len(pt) >= 2
    }
    zone = set()
    for c in named_cells:
        zone.update(h3.grid_disk(c, 1))

    feats = get_connecting_streets(segments, region, clip_cells=zone, exclude_name=main_street)
    return zone, feats


def _worse_quality(a: str, b: str) -> str:
    return a if _QUALITY_RANK[a] >= _QUALITY_RANK[b] else b


# ---------------------------------------------------------------------------------------------
# Leak gate: the last line of defence against notifying (or drawing) a place the announcement
# is not about. Every resolver above it scopes by settlement/opština on its own, and each of
# those scopes has been beaten at least once by a same-named place elsewhere: Žitni trg (Sremska
# Mitrovica -> Novi Sad), Heroja Pinkija (Bukovac -> Liman), a Zvezdara street list landing in
# Voždovac, and outage #2365 - Smederevo's "Вучак" also matching the Vučak under Kruševac, 125 km
# away. Rather than trust every present and future lookup path to get scoping right, every hex
# and every drawn line is checked against WHERE the announcement can physically be (the
# announced opština's outline, spatial_engine.region_envelope) and anything outside is dropped.
# A drop is always a bug somewhere upstream, so each one is also reported to the operator.
# ---------------------------------------------------------------------------------------------

# What the gate blocked during the current process_and_queue_outage call (drained there).
_leak_gate_events: list[str] = []
# Street names calculate_outage_hexes read as another spelling during the current call ("Хероја
# Пинкија -> PINKIJEVA"), for the operator note (drained in process_and_queue_outage).
_name_corrections: list[str] = []

# How close a re-spelled street must lie to the announcement's resolved streets to be accepted.
VARIANT_MAX_KM = 1.5


def _min_distance_km(hexes, points) -> float:
    best = math.inf
    for h in hexes:
        lat, lon = h3.cell_to_latlng(h)
        k = math.cos(math.radians(lat))
        for plat, plon in points:
            best = min(best, math.hypot((lat - plat) * 111.0, (lon - plon) * 111.0 * k))
    return best


def _variant_near(street: str, settlement: str, region: str, anchor: set) -> tuple[str, set] | None:
    """(variant, hexes) for the first other spelling of `street` (spatial_engine
    .street_name_variants) that RGZ has under exactly that name in this settlement, within
    VARIANT_MAX_KM of `anchor` - the hexes the same announcement's other streets resolved to. None
    otherwise: a re-spelled name is a guess, and a guess is only as good as the evidence around
    it. RGZ only - an OSM near-match on a guessed name is one guess too many."""
    points = [h3.cell_to_latlng(h) for h in anchor]
    for variant in street_name_variants(street):
        segments = query_rgz_streets(variant, naselje=settlement, opstina=region, exact_only=True)
        hexes = {h3.latlng_to_cell(pt[1], pt[0], 9) for seg in segments for pt in seg if len(pt) >= 2}
        if hexes and _min_distance_km(hexes, points) <= VARIANT_MAX_KM:
            return variant, hexes
    return None


def _announced_envelope(region: str, gate_region: str | None = None):
    """Where the announcement as a whole can be. `gate_region` is the source's own opština when
    the scraper had to swap `region` for a more specific place (EPS's inline "Темерин: ..." -
    see eps_scraper) - that column is ground truth, a geocode of the swapped-in place is not, so
    it anchors the gate. The swapped-in place is added only when it is itself an opština."""
    if not gate_region:
        return region_envelope(region)
    env = list(region_envelope(gate_region) or [])
    if is_serbian_opstina(region):
        env += region_envelope(region) or []
    return env or region_envelope(region)


def _location_envelope(settlement: str | None, region: str, region_ops,
                       gate_region: str | None = None):
    """The tightest area one location can lie in. A location scoped to a named opština inside
    the announced region (BVK's "ЗВЕЗДАРА" heading under region Београд) gets that opština's
    outline. A location naming a different, neighbouring opština outright (a Novi Sad utility's
    notice for Сремски Карловци) may lie in either. Anything else - a village, a kvart, nothing -
    gets the announced region: a village name is exactly what repeats across Serbia."""
    region_env = _announced_envelope(region, gate_region)
    if settlement:
        ops = region_opstine(settlement)
        if ops:
            env = region_envelope(settlement)
            if env and (region_ops is None or ops <= region_ops):
                return env
            if env:
                return (region_env or []) + env
    return region_env


def _gate_hexes(hexes, env, label: str) -> set:
    hexes = set(hexes or ())
    if not env or not hexes:
        return hexes
    kept, worst = set(), 0.0
    for h in hexes:
        d = envelope_distance_km(env, *h3.cell_to_latlng(h))
        if d <= 0:
            kept.add(h)
        else:
            worst = max(worst, d)
    if len(kept) < len(hexes):
        msg = (f"{label}: {len(hexes) - len(kept)} of {len(hexes)} hexes outside the announced "
               f"area, up to {worst:.0f} km beyond it")
        print(f"🛑 LEAK BLOCKED - {msg}")
        _leak_gate_events.append(msg)
    return kept


def _gate_feature(feat: dict, env, label: str) -> dict | None:
    """Same gate for a drawn GeoJSON feature: keeps only the lines/rings lying wholly inside the
    envelope. Returns None when nothing is left."""
    if not env or not feat:
        return feat
    geom = feat.get("geometry") or {}
    gtype, coords = geom.get("type"), geom.get("coordinates")

    def inside(pt):
        return envelope_distance_km(env, pt[1], pt[0]) <= 0

    if gtype == "Point":
        parts = [[coords]] if coords else []
    elif gtype == "LineString":
        parts = [coords]
    elif gtype in ("MultiLineString", "Polygon"):
        parts = coords or []
    elif gtype == "MultiPolygon":
        parts = [ring for poly in (coords or []) for ring in poly]
    else:
        return feat

    kept = [p for p in parts if p and all(inside(pt) for pt in p)]
    if len(kept) == len(parts):
        return feat
    msg = f"{label}: {len(parts) - len(kept)} of {len(parts)} drawn line(s) outside the announced area"
    print(f"🛑 LEAK BLOCKED - {msg}")
    _leak_gate_events.append(msg)
    if not kept or gtype not in ("LineString", "MultiLineString"):
        # Nothing left, or a polygon with a ring outside - dropped whole, there is no honest
        # partial shape.
        return None
    return {**feat, "geometry": {"type": "MultiLineString", "coordinates": kept}}

def _normalize_alert(parsed_data: OutageAlert | dict) -> OutageAlert | None:
    """Ensures input is always a validated OutageAlert instance."""
    if not parsed_data:
        return None
    if isinstance(parsed_data, dict):
        return OutageAlert.model_validate(parsed_data)
    return parsed_data


_DEDUP_FIELDS = (
    "location_type", "settlement", "main_street", "parity", "number_range",
    "explicit_numbers", "intersection_start", "intersection_end", "landmark",
    "includes_surrounding_streets",
)


def _dedup_locations(locations: list) -> list:
    """Drops exact-duplicate StreetSegments, keeping first-seen order. Confirmed real case: one
    messy multi-clause EPS row ("Бечањ...Мрчајевци до Бечћа...Корићани и Густа међа") came back
    with the SAME settlement ("Mrčajevci") repeated as 48 separate ENTIRE_SETTLEMENT segments -
    each one drove its own real settlement/street lookup in calculate_outage_hexes and
    _build_visual_features, purely wasted work (the map already collapses same-settlement
    features into one shape, so the duplicates changed nothing about what got drawn - just
    burned 47 redundant DB lookups for it). time_window is excluded from the identity - it's
    shared across a whole announcement in practice, never what makes two segments distinct."""
    seen = set()
    out = []
    for loc in locations:
        key = tuple(getattr(loc, f, None) for f in _DEDUP_FIELDS)
        if key in seen:
            continue
        seen.add(key)
        out.append(loc)
    return out


def _text_words(text: str | None) -> list[str]:
    """Every 4+ letter word, Latin caps."""
    return re.findall(r"[A-ZĐĆČŽŠ]{4,}", cyrillic_to_latin(text or "").upper())


def _place_named_before(raw_text: str, place: str, street: str) -> bool:
    """Does the announcement text itself name `place` ahead of `street` - a heading the street
    sits under ("Буковац: Кестењара, Косовска, Хероја Пинкија")? Words compared by their first
    5 letters, so case endings and abbreviations ("М.МОКРИ ЛУГ" vs "Мали Мокри Луг") still line
    up. When the street's own spelling can't be found in the text, the place just has to appear
    in it somewhere."""
    text = cyrillic_to_latin(raw_text).upper()
    place_keys = [w[:5] for w in _text_words(place)]
    if not place_keys:
        return False
    street_words = sorted(_text_words(street), key=len, reverse=True)
    street_pos = text.find(street_words[0][:5]) if street_words else -1
    scope = text[:street_pos] if street_pos >= 0 else text
    return any(k in scope for k in place_keys)


def _inherit_settlement_context(locations: list, raw_text: str = "") -> list:
    """EPS groups an announcement as "Делови насеља X; Делови улица: A; B; C" - those streets
    are parts of settlement X, but the model routinely tags only the leading ENTIRE_SETTLEMENT
    segment with settlement=X and leaves the following STREET segments' settlement empty. Each
    street then resolves against the whole opština and can land on a same-named street in the
    city proper. Confirmed real case: outage #792, "Делови насеља Рибњак; Делови улица: ...
    Цара Лазара ..." - Рибњак / Sremska Kamenica streets, but "Цара Лазара" matched Novi Sad's
    "Bulevar cara Lazara". Carry the most recent named settlement forward onto any later street
    segment that doesn't name its own; a street that DOES name one, or one that appears before
    any settlement is mentioned, is left untouched.

    Only when the text itself names that place ahead of the street (_place_named_before). A
    street the text gives no place for is that street as written, in the announced opština -
    never a place borrowed from its neighbours (owner's rule, 2026-09-24). With no raw_text to
    check (unit tests, re-matching a stored outage), the plain carry-forward."""
    ctx = None
    out = []
    for loc in locations:
        if loc.settlement:
            ctx = loc.settlement
            out.append(loc)
        elif loc.main_street and ctx and (
                not raw_text or _place_named_before(raw_text, ctx, loc.main_street)):
            out.append(loc.model_copy(update={"settlement": ctx}))
        else:
            out.append(loc)
    return out


def _norm_region(name: str | None) -> str:
    """Normalise a municipality/settlement name for the macro-scope match. Transliterates first,
    so a target registered in Latin ('Novi Sad', from the RGZ autocomplete) matches an outage
    whose region was scraped in Cyrillic ('НОВИ САД', what every EPS/toplana page uses) - plain
    UPPER() in SQLite never bridged the two scripts, which silently dropped every cross-script
    target from the candidate set."""
    return cyrillic_to_latin(name or "").strip().upper()


def _strip_leading_initials(name: str) -> str:
    """'T. RAJIĆA' -> 'RAJIĆA'. Same pattern spatial_engine.get_intersection_point already uses
    to bridge abbreviated ('Т.Рајића') vs full ('Тозе Рајића') street-name forms."""
    return re.sub(r'^([A-ZĐĆČŽŠ]\.\s*)+', '', name).strip()


def _region_variants(name: str | None) -> set:
    """Normalised macro-scope tokens for one region name. A target's city can carry a
    Belgrade/Niš gradska-opština suffix ('Beograd (Voždovac)') while a scraped outage names
    only the grad ('БЕОГРАД'), so also emit the bare prefix and the parenthesised part -
    otherwise the two never compare equal and a real, hex-overlapping outage is dropped from
    the candidate set (target #40 Braće Jerkovića / Voždovac vs outage region 'БЕОГРАД')."""
    norm = _norm_region(name)
    if not norm:
        return set()
    out = {norm}
    m = re.match(r'^(.*?)\s*\(([^)]*)\)\s*$', norm)
    if m:
        if m.group(1).strip():
            out.add(m.group(1).strip())
        if m.group(2).strip():
            out.add(m.group(2).strip())
    # The same pair joined by a dash: EPS's Belgrade rows are stored as "ПАЛИЛУЛА - БЕОГРАД"
    # (eps_scraper._belgrade_opstina), Niš's as "Палилула - Ниш", and the model has written
    # "БЕОГРАД - ЗВЕЗДАРА". Unsplit, that token matched no target at all, so no Palilula
    # subscriber could get an EPS alert (outage #2785, 26.09.2026).
    parts = [p.strip() for p in re.split(r'\s+[-–—]\s+', norm)]
    if len(parts) > 1:
        out.update(p for p in parts if p)
    return out


def _region_token_set(*names) -> set:
    """Union of _region_variants over several name fields (city / opstina / naselje)."""
    out = set()
    for n in names:
        out |= _region_variants(n)
    return out


# An ordinal in a Serbian street name shows up in three interchangeable spellings: a Roman
# numeral ("Kralja Petra I"), a digit ("Kralja Petra 1"), or a declined ordinal word ("Kralja
# Petra Prvog", "Osmog Marta", "Sedmog Jula"). RGZ and a utility's free text routinely disagree
# on which one they use for the same street, so every form is folded to a common "#N" token
# before two names are compared. Word stems are keyed as get_smart_stem produces them (same set
# spatial_engine._ORDINAL_STEM_TO_DIGIT uses).
_ROMAN_ORDINAL = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
    "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
}
_ORDINAL_WORD_STEM = {
    "PRV": 1, "DRUG": 2, "TREĆ": 3, "ČETVRT": 4, "PET": 5,
    "ŠEST": 6, "SEDM": 7, "OSM": 8, "DEVET": 9, "DESET": 10,
}


def _fold_ordinals(name: str) -> str:
    """Rewrite every ordinal token (Roman numeral, digit 1-10, or declined ordinal word) to a
    common '#N' marker, so 'PETRA I', 'PETRA 1' and 'PETRA PRVOG' all compare equal. Any other
    token is left untouched."""
    out = []
    for tok in name.split():
        bare = tok.strip(".")
        if bare in _ROMAN_ORDINAL:
            out.append("#%d" % _ROMAN_ORDINAL[bare])
        elif bare.isdigit() and 1 <= int(bare) <= 10:
            out.append("#%d" % int(bare))
        else:
            n = _ORDINAL_WORD_STEM.get(get_smart_stem(bare))
            out.append("#%d" % n if n else tok)
    return " ".join(out)


def _street_words(name: str) -> list[str]:
    return [w for w in re.split(r"[\s.,/\-–]+", name) if w]


def _words_within(short: str, long: str) -> bool:
    """True when `short`'s words appear, whole and in order, inside `long`: "RAJIĆA" in "TOZE
    RAJIĆA", "OSLOBOĐENJA" in "BULEVAR OSLOBOĐENJA". Never part of a word. Plain substring
    containment matched the street "НОВА" to every name ending in -nova: outage #4264
    (09.10.2026) e-mailed two building managers on Prešernova and Puškinova about a power cut
    "u ulici Nova"."""
    a, b = _street_words(short), _street_words(long)
    return bool(a) and any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))


_INITIALS_RE = re.compile(r'^((?:[A-ZĐĆČŽŠ]\.\s*)+)')


def _split_initials(name: str) -> tuple[list[str], str]:
    """'I.L.RIBARA' -> (['I', 'L'], 'RIBARA'). The initials are kept, not thrown away: 'Ј.КОЧИЋА'
    is not 'Petra Kočića', and 'Ж. ЈОВАНОВИЋА' is not 'Zdravka Jovanovića'."""
    m = _INITIALS_RE.match(name)
    if not m:
        return [], name.strip()
    return re.findall(r'[A-ZĐĆČŽŠ]', m.group(1)), name[m.end():].strip()


def _is_name_word(word: str) -> bool:
    """A word that says which street it is: not a number or ordinal ('1', '#1') and not a
    street-type classifier ('BULEVAR', 'ULICA')."""
    return not (word.startswith('#') or word.isdigit() or is_generic_street_token(word))


def _name_within(short: str, initials: list[str], long: str) -> bool:
    """`short` (with its leading `initials`) is an abbreviated form of `long`:
    - its words appear whole and in order inside `long`;
    - each initial is the first letter of the word right before them ('B.BUHE' in 'BOŠKA BUHE');
    - it has a word that names the street, so '1.', 'ULICA 1' and 'PRVA' match nothing but
      themselves;
    - a single name word may only gain street-type words ('OSLOBOĐENJA' in 'BULEVAR
      OSLOBOĐENJA'), never another name: a lone 'JOVANOVIĆA' is not 'ZDRAVKA JOVANOVIĆA'."""
    a, b = _street_words(short), _street_words(long)
    names = [w for w in a if _is_name_word(w)]
    if not names:
        return False
    for i in range(len(b) - len(a) + 1):
        if b[i:i + len(a)] != a:
            continue
        before, after = b[:i], b[i + len(a):]
        if initials:
            if len(before) < len(initials) or [w[0] for w in before[-len(initials):]] != initials:
                continue
            before = before[:-len(initials)]
        if len(names) == 1 and any(_is_name_word(w) for w in before + after):
            continue
        return True
    return False


def _streets_match(a: str | None, b: str | None) -> bool:
    """Loose equality for comparing a registered target's street against an outage's extracted
    street name: cleaned and transliterated, then compared as an abbreviated form either way
    round (_name_within: whole words, initials that fit), by an ordinal-spelling-insensitive
    pass ("Petra I" == "Petra Prvog"), and by stemmed equality as a fallback (handles Serbian
    case-ending differences). Checked over every subscriber street against every street ever
    announced (09.10.2026): the looser rules before this paired 'Ј.КОЧИЋА' with Petra Kočića,
    'Р.ПАВЛОВИЋА' with Teodora Pavlovića and '1.' with Kralja Petra I."""
    if not a or not b:
        return False
    la = cyrillic_to_latin(clean_street(a)).upper()
    lb = cyrillic_to_latin(clean_street(b)).upper()
    return _latin_streets_match(la, lb) or _typed_without_diacritics_match(la, lb)


_SR_DIACRITICS = frozenset("ČĆŽŠĐ")


def _typed_without_diacritics_match(la: str, lb: str) -> bool:
    """A street typed with no č/ć/ž/š/đ at all ("KNEZA MILOSA", Selevac, 01.11.2026) is the same
    street as the notice's "КНЕЗА МИЛОША". Only when one side has no diacritics anywhere (someone
    who types them is taken at their word), and only the whole name: after dropping diacritics
    the two must be equal word for word, or equal once ordinals are folded ("Petra I"). No
    abbreviations or partial names on this path."""
    if _SR_DIACRITICS & set(la) and _SR_DIACRITICS & set(lb):
        return False
    fa = " ".join(_street_words(_fold_name(la)))
    fb = " ".join(_street_words(_fold_name(lb)))
    if not fa or not fb or (fa, fb) == (" ".join(_street_words(la)), " ".join(_street_words(lb))):
        return False
    if is_generic_street_token(fa) or is_generic_street_token(fb):
        return False
    return fa == fb or _fold_ordinals(fa) == _fold_ordinals(fb)


def _latin_streets_match(la: str, lb: str) -> bool:
    ia, na = _split_initials(la)
    ib, nb = _split_initials(lb)
    if not na or not nb:
        return False
    # Both abbreviated, differently: 'Ž. JOVANOVIĆA' and 'B. JOVANOVIĆA' are two streets.
    if ia and ib and ia != ib:
        return False
    if na == nb:
        return True
    # A bare street-type classifier ("TRG", "BULEVAR") must not match by containment - it would
    # pair a target on "Trg Slobode" with an outage that only said "Trg", and vice versa. An
    # exact-equal "Trg" == "Trg" is already handled above.
    if is_generic_street_token(na) or is_generic_street_token(nb):
        return False
    if _name_within(na, ia, nb) or _name_within(nb, ib, na):
        return True
    fa, fb = _fold_ordinals(na), _fold_ordinals(nb)
    if (fa, fb) != (na, nb) and (fa == fb or _name_within(fa, ia, fb) or _name_within(fb, ib, fa)):
        return True
    return get_smart_stem(na) == get_smart_stem(nb)


def _place_streets_by_their_group(alert: OutageAlert) -> list[str]:
    """Streets the text gives no place for, placed where the rest of the notice is. EPS lists
    one transformer's streets, so they lie together; but it names only the opština, and a name
    can repeat inside it. Outage #4264 (12.10.2026, "Opština: Нови Сад"): Olge Ilić, Danila
    Lazovića and Dragoslava Srejovića exist only in Veternik, and the lookup drew Dobre
    Jovanovića in Novi Sad proper (~6 km off; Veternik has one too) and a "Nova" near Kisač
    (~11 km; Veternik has none).

    When at least two such streets each exist in one RGZ naselje only, and it is the same one,
    every street of the notice that also exists there, or exists nowhere in the opština, is put
    in that naselje. The name is never changed - this only picks which of the same-named
    streets is meant - and a street missing from that naselje then stays unresolved (see
    get_local_street_segments) instead of borrowing a same-named street elsewhere. Nothing
    changes when the group is the town the opština is named after (lookups already start
    there), when any street exists only in some other naselje (a mixed notice), or when RGZ
    can't answer. Returns one line per street placed by the group alone.

    This narrows the owner's rule of 24.09.2026 (a street with no place is the street as written
    in the opština): on the owner's request of 10.10.2026 the notice's own streets now choose
    between same-named streets."""
    region = alert.city
    open_locs = [loc for loc in alert.affected_locations
                 if loc.main_street and not (loc.settlement or "").strip()]
    if len(open_locs) < 2 or not _rgz_available():
        return []
    homes = []
    try:
        for loc in open_locs:
            found = rgz_street_naselja(loc.main_street, region)
            if found is None:
                return []
            homes.append(found)
    except sqlite3.Error:
        return []
    only = [next(iter(h.items())) for h in homes if len(h) == 1]
    places = {lat for lat, _ in only}
    if len(only) < 2 or len(places) != 1:
        return []
    home_lat, home = only[0]
    if "(" in home_lat or _fold_name(home_lat) == _fold_name(_norm_region_lat(region)):
        return []
    placed = []
    by_id = {id(loc): h for loc, h in zip(open_locs, homes)}
    new_locs = []
    for loc in alert.affected_locations:
        h = by_id.get(id(loc))
        if h is not None and (not h or home_lat in h):
            new_locs.append(loc.model_copy(update={"settlement": home}))
            if len(h) != 1:
                placed.append(f"{loc.main_street} -> {home}"
                              + ("" if h else " (not in RGZ there: left unresolved)"))
        else:
            new_locs.append(loc)
    alert.affected_locations = new_locs
    return placed


def _rgz_available() -> bool:
    return os.path.exists(RGZ_DB_PATH)


@functools.lru_cache(maxsize=1024)
def _naselje_opstine_cached(naselje: str) -> frozenset:
    return frozenset(naselje_owner_opstine(naselje))


def _naselje_opstine(naselje: str) -> frozenset | None:
    """The RGZ opštine that have a naselje by this name. None when RGZ can't be asked (no rgz.db,
    or it errors mid-rebuild) - never cached, and never allowed to take a whole outage down with
    it: the caller then keeps the geometric answer."""
    if not _rgz_available():
        return None
    try:
        return _naselje_opstine_cached(naselje)
    except sqlite3.Error:
        return None


@functools.lru_cache(maxsize=256)
def _region_opstine_cached(region: str) -> frozenset:
    return frozenset(region_opstine(region) or ())


def _region_opstine(region: str | None) -> frozenset:
    """region_opstine(), once per region name rather than once per candidate target."""
    if not region:
        return frozenset()
    try:
        return _region_opstine_cached(region)
    except sqlite3.Error:
        return frozenset()


def _fold_name(name: str | None) -> str:
    """Upper case with diacritics dropped, so "Cenej" typed without the č (a CSV import) and RGZ's
    "ČENEJ" compare equal."""
    s = unicodedata.normalize("NFKD", (name or "").replace("Đ", "DJ").replace("đ", "dj"))
    return "".join(c for c in s if not unicodedata.combining(c)).upper().strip()


def _row_get(row, key):
    """row[key] for a sqlite3.Row or a dict, None when the row doesn't carry the key."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _place_keys(settlement: str | None, target_naselje: str | None) -> tuple[str, str]:
    """(the location's settlement, the target's) as comparable RGZ-style naselje names."""
    return (_dealias_naselje(_norm_region_lat(settlement)),
            _dealias_naselje(_norm_region_lat(target_naselje)))


def _settlement_holds_target(settlement: str | None, target_naselje: str | None,
                             opstine=()) -> bool:
    """Is the target inside the settlement a no-street location names? A target is mapped to its
    whole street's hexes, and a street that runs up to a village's edge shares a border hex with
    it - so on geometry alone, "celo naselje Veternik" reached 40 Novi Sad streets (Bulevar Jovana
    Dučića, Futoški put...) and Aradac 8 Zrenjanin ones, far past the village.

    When the named settlement is an RGZ naselje of one of `opstine` (the outage's own, and the
    target's) and the target lives in a different naselje, the target is not in it. Anything
    less certain keeps the geometric answer: the town an opština is named after ("Нови Сад" -
    a whole-town notice reaches its suburbs as before), a kvart name (not an RGZ naselje there,
    even if some village elsewhere is called that), a target with no place on record, or RGZ
    not answering."""
    if not settlement or not target_naselje:
        return True
    want, have = _place_keys(settlement, target_naselje)
    if not want or _fold_name(want) == _fold_name(have):
        return True
    scope = set(opstine or ())
    if _fold_name(want) in {_fold_name(o.split(" (")[0]) for o in scope}:
        return True
    owners = _naselje_opstine(want)
    if owners is None:
        return True
    return not (owners & scope)


def _settlement_surely_holds_target(settlement: str | None, target_naselje: str | None) -> bool:
    """The exclusion side ("osim naselja Veternik"): only a target known to live in that very
    settlement is left out. Unsure - a kvart name, no place on record - the target keeps the
    alert, since a missed alert is worse than an extra one."""
    if not settlement or not target_naselje:
        return False
    want, have = _place_keys(settlement, target_naselje)
    return bool(want) and _fold_name(want) == _fold_name(have)


def _location_matches_target(loc: StreetSegment, target_street: str | None, target_number: str,
                             target_naselje: str | None = None, opstine=(),
                             exclusion: bool = False) -> bool:
    """A location's parity/number-range filter only applies to a target if the location actually
    names a specific street and that street is the one the target lives on. A location with no
    main_street (ENTIRE_SETTLEMENT) covers everyone the hex match found inside that settlement
    (_settlement_holds_target). As an exclusion (`exclusion=True`) a no-street location only
    covers targets known to be inside it (_settlement_surely_holds_target)."""
    if loc.main_street and not _streets_match(loc.main_street, target_street):
        return False
    if not loc.main_street:
        inside = (_settlement_surely_holds_target(loc.settlement, target_naselje) if exclusion
                  else _settlement_holds_target(loc.settlement, target_naselje, opstine))
        if not inside:
            return False
    return is_house_number_affected(
        user_number_str=target_number,
        parity=loc.parity or 'ALL',
        number_range=loc.number_range,
        explicit_numbers=loc.explicit_numbers
    )

STREET_PLACEHOLDER = '***'

# Works nearly always overrun the announced time; keep a row on the map a bit past its stated
# end rather than dropping it while crews are still finishing.
_EXPIRY_GRACE = timedelta(hours=3)
_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%Y.", "%d/%m/%Y")


def _same_region(a: str | None, b: str | None) -> bool:
    """Same city/opština, ignoring case and spacing - compared here, not in SQL: SQLite's UPPER()
    only folds ASCII, so "Београд" and "БЕОГРАД" never matched and a re-read notice was stored as
    a second outage instead of updating the first (BVK, 26.10.2026)."""
    norm = lambda s: " ".join(unicodedata.normalize("NFC", s or "").casefold().split())
    return norm(a) == norm(b)


def _parse_date(value: str):
    v = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _outage_last_day(alert) -> datetime | None:
    """The last calendar day this outage can still be happening: the source's multi-day end_date
    if it gave one, otherwise the single outage_date. None when neither parses (outage_date
    'N/A' or an unrecognised format)."""
    return _parse_date(getattr(alert, 'end_date', None) or '') or _parse_date(alert.outage_date or '')


_MIDNIGHT_STATED_RE = re.compile(
    r"поноћ|ponoć|00[:.]00|24[:.]00", re.IGNORECASE
)


def _normalize_time_window(time_window: str, raw_text: str) -> str:
    """Safety net behind parser.py rule 8 - the model is instructed to never default a missing
    start to '00:00', but an LLM's compliance isn't guaranteed. If it slips anyway and the source
    text itself never actually says midnight, swap a fabricated '00:00' start for '07:00' (when
    crews actually start) - a bare '00:00' on a fault report reads as "started at midnight",
    which is false and worse than an approximation. Deterministic, no model call - this is
    purely a backstop, not the primary fix."""
    if not time_window or time_window == 'N/A':
        return time_window
    m = re.match(r"^\s*00[:.]00\s*-\s*(.+)$", time_window)
    if not m:
        return time_window
    if _MIDNIGHT_STATED_RE.search(raw_text or ""):
        return time_window  # the source really did say midnight - trust it
    return f"07:00 - {m.group(1).strip()}"


def _compute_expires_at(outage_date: str, end_date: str | None, time_window: str) -> str | None:
    """When this outage should stop showing on the map:
      - an explicit multi-day end date  -> end of that day
      - otherwise the end time in the day's time window ('08:00 - 14:00' -> 14:00 that day)
      - otherwise the end of the outage day
      - nothing datable -> None (shows until cleared by hand)
    A few hours' grace is added because works run late."""
    end_day = _parse_date(end_date) if end_date else None
    start_day = _parse_date(outage_date)

    if end_day:
        return (end_day.replace(hour=23, minute=59) + _EXPIRY_GRACE).isoformat(sep=" ")
    if not start_day:
        return None

    hhmm = re.findall(r"(\d{1,2})[:.](\d{2})", time_window or "")
    bare_hour = re.findall(r"(?:до|do)\s*(\d{1,2})\s*(?:час|čas|cas|h\b|sat)", time_window or "", re.IGNORECASE)
    if hhmm:
        last_h, last_m = hhmm[-1]
        end_dt = start_day.replace(hour=min(int(last_h), 23), minute=min(int(last_m), 59))
        # A night window ("22:00 - 06:00", BVK's night flushing) ends the next morning. Read as
        # the same day, it "ended" at 06:00 before it began at 22:00: the outage dropped off the
        # map that morning, and a send or signup later that day was skipped as already over.
        # Likewise "08:00 - 00:00" runs until midnight, not until the start of its own day. Only
        # a window that starts in the afternoon or evening counts as a night one: a morning start
        # with an earlier end ("07:00 - 00:01") is a bad reading, not a night shift.
        first = (int(hhmm[0][0]), int(hhmm[0][1]))
        last = (int(last_h), int(last_m))
        if len(hhmm) > 1 and first > (0, 0) and (last == (0, 0) or (last < first and first >= (12, 0))):
            end_dt += timedelta(days=1)
    elif bare_hour:
        end_dt = start_day.replace(hour=min(int(bare_hour[-1]), 23), minute=0)
    else:
        end_dt = start_day.replace(hour=23, minute=59)
    return (end_dt + _EXPIRY_GRACE).isoformat(sep=" ")


def _drop_invented_windows(alert, raw_text: str, works: bool = False) -> None:
    """Works that run for days or weeks (an end date), or a road closure, with no time of day in
    the notice go on around the clock. The parser still fills one in (parser.py rule 8: 07:00 -
    19:00), and it read as "only during working hours" (#2863, Vojvođanskih brigada, 28.09. to
    21.10.). Dropped here, so expires_at runs to the end of the day and nothing claims hours the
    source never gave. build_web_data does the same on the map for rows stored before this.
    `works` (a toplana's works notice, core.filters.heat_works_only) is the same: Beogradske
    elektrane's daily "Planirani radovi na toplovodnoj mreži" never give hours, and #3459 went out
    as "u periodu 07:00 - 19:00"."""
    if (getattr(alert, 'end_date', None) or getattr(alert, 'road_closure_only', False) or works) \
            and not states_time(raw_text):
        for loc in alert.affected_locations:
            loc.time_window = ""  # "" not None: the stored parse must still validate (str field)


# parser.py rule 6's phrases, either alphabet: "и околне улице", "и суседним улицама", "у ширем
# рејону око", "у околини", "у рејону око".
_SURROUNDINGS_RE = re.compile(r"околн|суседн|ширем\s+рејон|у\s+рејону\s+око|"
                              r"okoln|susedn|širem\s+rejon|u\s+rejonu\s+oko", re.IGNORECASE)


def _drop_invented_surroundings(alert, raw_text: str) -> None:
    """includes_surrounding_streets only when the notice says so. The model set it on EPS rows
    that list streets and nothing else (#926, #927, #932 - Subotica, 11.09.2026), which drew 29
    side streets for 5 named ones and would tell their people a cut may reach them."""
    if raw_text and not _SURROUNDINGS_RE.search(raw_text):
        for loc in alert.affected_locations:
            loc.includes_surrounding_streets = False


def _geocode_landmark(query: str):
    """One Nominatim lookup, through spatial_engine's rate limiter (the public API allows ~1/s)."""
    from spatial.spatial_engine import _geocode_with_backoff
    return _geocode_with_backoff(query, country_codes="rs", addressdetails=True, timeout=10)


def _metres(lat1, lon1, lat2, lon2) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * 111_320, (lon2 - lon1) * 111_320 * k)


def _landmark_house_number(loc, region) -> str | None:
    """The house number of a building named on its street. OSM (Nominatim) must know the building
    on that very street, and RGZ must have that number on that street next to it - RGZ is scoped
    to the notice's own place, so a same-named street in another town never gets through."""
    place = loc.settlement or region
    query = ", ".join(cyrillic_to_latin(x).title() for x in (loc.landmark, loc.main_street, place) if x)
    try:
        found = _geocode_landmark(query)
    except Exception as e:
        print(f"   ↳ landmark lookup failed ({query}): {e}")
        return None
    if not found or found.raw.get("class") in ("highway", "boundary", "landuse", "place"):
        return None  # nothing, or only the street / the town: the building itself wasn't found
    address = found.raw.get("address") or {}
    number = str(address.get("house_number") or "").strip()
    if not number or not _streets_match(address.get("road"), loc.main_street):
        return None
    for kucni_broj, lat, lon in rgz_house_points(loc.main_street, place, region):
        if str(kucni_broj).strip().upper() == number.upper() \
                and _metres(lat, lon, found.latitude, found.longitude) < 250:
            return str(kucni_broj).strip()
    return None


_NUMBERS = re.compile(r"\d+[^\W\d_]?")


def _place_landmarks(alert, category) -> list[str]:
    """A power notice that names one building on a street - "ЛИДЛ на Футошком путу" - cuts that
    building, not the street. The parser keeps the street as main_street, so all of Futoški put
    was drawn and everyone on it would have been alerted (#3442, 30.09.2026). EPS lists single
    customers this way (a Maxi, a school, a petrol station, a clinic); water and heating use
    landmarks for areas (a zone, a corner, a turnaround) and are left as they are.

    - A "landmark" that is the street with house numbers ("ШУЛЕЈИЋЕВА 2"): those numbers.
    - A building OSM finds on that street, at a number RGZ has there: that number.
    - Otherwise the street is dropped and the location is a landmark with no street, like the
      hundreds EPS lists by name alone: not drawn, nobody alerted.
    Returns the dropped ones, for the operator - except a building whose street the same notice
    also lists on a line of its own ("Др. Милана Костића 68-120, 9 и б.б., Атлетски стадион Др.
    Милана Костића б.б.", Sremska Mitrovica, 07.10.2026): the homes there are alerted through that
    line, so the dropped building leaves no gap and the note would only be noise."""
    if category != 'POWER':
        return []
    region = alert.city or 'НОВИ САД'
    listed = [(l.main_street, _place_key(l.settlement)) for l in alert.affected_locations
              if l.location_type == 'STREET' and l.main_street]
    dropped = []
    for loc in alert.affected_locations:
        if (loc.location_type != 'LANDMARK' or not loc.main_street or not (loc.landmark or '').strip()
                or loc.number_range or loc.explicit_numbers):
            continue
        numbers = _NUMBERS.findall(loc.landmark)
        name = _NUMBERS.sub(" ", loc.landmark).strip(" ,.-")
        if numbers and name and _streets_match(name, loc.main_street):
            number = ", ".join(numbers)
        else:
            number = _landmark_house_number(loc, region)
        if number:
            print(f"   ↳ landmark placed: {loc.landmark} = {loc.main_street} {number}")
            loc.location_type, loc.explicit_numbers = 'STREET', number
        else:
            label = f"{loc.landmark}, {loc.main_street}"
            if any(_streets_match(loc.main_street, street) and place == _place_key(loc.settlement)
                   for street, place in listed):
                print(f"   ↳ landmark dropped, its street is listed on its own: {label}")
            else:
                dropped.append(label)
            loc.landmark, loc.main_street = label, None
    return dropped


_BB_AFTER = r"\s*[,.]?\s*(?:B\.?\s?B\b\.?|BEZ BROJA\b)"
_BB_LABEL = re.compile(r" бб(?: \(|$)")


def _drop_bb_streets(alert, raw_text: str) -> list[str]:
    """A street the notice gives only as "бб" (bez broja) is one unnumbered building on it, not
    the street: "Корнелија Станковића бб (ЗГОП)" is ZGOP's building. The parser drops the "бб",
    so the line read as the whole street and a building manager at Kornelija Stankovića 5 got
    the heating cut (#6654, nstoplana, 02.11.2026).

    For every category: a STREET location with no numbers whose street the text names only with
    "бб" after it - every time it names it - loses its street, like a landmark we can't place:
    not drawn, nobody alerted, its text kept for the card. A street the notice also names
    without "бб" ("Улица X; Улица X бб") is left as it is. Returns the dropped ones."""
    text = _folded_text(raw_text)
    dropped = []
    for loc in alert.affected_locations:
        if (loc.location_type != 'STREET' or not loc.main_street
                or loc.number_range or loc.explicit_numbers):
            continue
        name = re.sub(r"\s+", " ", _fold_name(cyrillic_to_latin(loc.main_street)))
        named = list(re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text))
        if not named or not all(re.match(_BB_AFTER, text[m.end():]) for m in named):
            continue
        label = f"{loc.main_street} бб" + (f" ({loc.landmark})" if (loc.landmark or "").strip() else "")
        print(f"   ↳ one building without a number, not the street: {label}")
        loc.location_type, loc.landmark, loc.main_street = 'LANDMARK', label, None
        dropped.append(label)
    return dropped


def _place_key(settlement: str | None) -> str:
    return cyrillic_to_latin(settlement or "").upper().strip()


def _announcement_expires_at(alert, outage_date: str, time_window: str) -> str | None:
    """expires_at for a whole announcement. Each location can carry its own window ("08:00-10:00"
    on one street, "13:00-16:00" on another); the outage is over only when the last one is, not
    when the first location's window ends."""
    end_date = getattr(alert, 'end_date', None)
    ends = [_compute_expires_at(outage_date, end_date, time_window)]
    ends += [_compute_expires_at(outage_date, end_date, loc.time_window)
             for loc in alert.affected_locations if loc.time_window]
    ends = [e for e in ends if e]
    return max(ends) if ends else None


def _window_over(expires_at: str | None) -> bool:
    """The announced window (expires_at less the map's grace) is behind us. The scrape path and the
    registration backfill both ask this, so they can't disagree about it."""
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) - _EXPIRY_GRACE < datetime.now()
    except ValueError:
        return False


def _location_display_name(loc: StreetSegment, target_street: str | None) -> str:
    """The human-readable street label substituted into the alert's message template for one
    recipient. Falls back through settlement then the target's own street so an
    ENTIRE_SETTLEMENT location (which has no main_street at all) still renders a sensible
    sentence rather than an empty slot."""
    base = loc.main_street or loc.settlement or target_street or ''
    display = cyrillic_to_latin(base).title() if base else ''

    # Carry the house-number detail through when the announcement scoped one, so the recipient
    # can see the outage is limited to part of the street rather than all of it.
    detail = loc.number_range or loc.explicit_numbers
    if display and detail:
        display = f"{display} {detail}"
    return display


# Words that prove a message template is actually talking about its own utility. The scraper
# knows the category deterministically (EPS is always POWER, a vodovod always WATER, a toplana
# always HEATING); the template is free text the model wrote, so it gets checked against that
# ground truth before it is ever sent. Matched against the transliterated, upper-cased text.
_CATEGORY_KEYWORDS = {
    "WATER": ("VOD",),                       # voda, vode, vodosnabdevanje
    "POWER": ("STRUJ", "ELEKTRI", "NAPAJAN"),
    "HEATING": ("GREJAN", "TOPL"),           # grejanje, toplotna energija, topla voda
}
_CATEGORY_FALLBACK = {
    "WATER": "Planirano isključenje vode",
    "POWER": "Planirano isključenje struje",
    # A toplana supplies BOTH heating and hot water, and a given notice may cut either or both.
    # Saying "grejanja" when it is actually the hot water (or vice versa) is the same class of
    # error this whole check exists to stop, so the fallback names the supply, not the symptom -
    # which is also the wording toplane themselves use. When the model's own template correctly
    # says "topla voda" or "grejanje", that template is kept and this never runs.
    "HEATING": "Planirani prekid u isporuci toplotne energije (grejanje i topla voda)",
}
# What people lose, when a toplana notice says it (core.filters.heat_supply_cut). "Toplotna
# energija" is the utility's word for it, not the reader's.
_HEAT_SUPPLY_BASE = {
    "HOT_WATER": "Bez tople vode",
    "HEATING": "Bez grejanja",
    "BOTH": "Bez grejanja i tople vode",
}


def _template_matches_category(template: str, category: str | None) -> bool:
    """True unless we can prove the template describes a different utility than the one the
    scraper guaranteed. The test is deliberately "does it mention its OWN utility at all",
    not "does it mention a foreign one" - a toplana notice legitimately says 'topla voda',
    which any foreign-word test would flag."""
    words = _CATEGORY_KEYWORDS.get((category or "").upper())
    if not words or not template:
        return True  # unknown category, or nothing to check - leave the model's text alone
    blob = cyrillic_to_latin(template).upper()
    return any(w in blob for w in words)


def _append_time_window(msg: str, window: str | None) -> str:
    """A model-written message_template sometimes carries the street and date but omits the
    outage's hours even when we parsed them - real case: toplana outage #915 had
    time_window '08:00 - 10:00' and the template rendered "Prekid u isporuci tople vode ...
    dana 09.09.2026" with no time at all. Append the window unless it's already in the text."""
    w = (window or '').strip()
    if not w or w.upper() == 'N/A':
        return msg
    nums = re.findall(r'\d{1,2}[:.]\d{2}', w)
    if w in msg or (nums and all(n in msg for n in nums)):
        return msg
    if nums and re.search(rf"\bdo\s+{re.escape(nums[-1])}", msg):
        return msg  # "otklonjen do 15:00" already says when (a BVK fault states only its end)
    return f"{msg.rstrip().rstrip('.')}, u periodu {w}."


def _date_span(alert) -> str | None:
    """"od 30.09. do 01.10.2026." for a notice that runs over several days, else None. The year
    is written once when both days fall in the same year."""
    start = (alert.outage_date or '').strip()
    end = (getattr(alert, 'end_date', None) or '').strip()
    if not start or not end or start == end:
        return None
    s, e = _parse_date(start), _parse_date(end)
    if not s or not e:
        return f"od {start} do {end}"
    if e.date() <= s.date():
        return None
    first = s.strftime('%d.%m.') if s.year == e.year else s.strftime('%d.%m.%Y.')
    return f"od {first} do {e.strftime('%d.%m.%Y.')}"


# A date written in a message: "05.11.2026", a night "05/06.11.2026", or the first day of a range
# that writes the year only once ("02.11 - 07.11.2026", "30.09. do 01.10.2026"). Clock times
# ("22.00 - 06.00") have no year, so they never match.
_MESSAGE_DATE = re.compile(
    r"(?<![\d.])(\d{1,2})(?:\s*/\s*(\d{1,2}))?\.(\d{1,2})\.?"
    r"(?:\s*(\d{4})|(?=\s*(?:-|–|do)\s*\d{1,2}\.\d{1,2}\.\s*(\d{4})))")


def _crosses_midnight(window: str | None) -> bool:
    """"22:00 - 06:00": the window ends the next morning."""
    hhmm = [(int(h), int(m)) for h, m in re.findall(r"(\d{1,2})[:.](\d{2})", window or "")]
    return len(hhmm) > 1 and hhmm[-1] < hhmm[0]


def _message_dates_are_its_own(text: str | None, alert, loc=None) -> bool:
    """False when the message names a day outside the outage's own days: its date to its end
    date, plus the next morning for a night window. BVK's week of night flushing in Voždovac
    (02.-07.11.2026) is split into one outage per night, but the model wrote every night's
    template from the notice's opening paragraph, and four alerts for 05.11. and 06.11. went out
    as "dana 02.11.2026" (#6459, #6460, 30.10.2026). Same idea as the hours check in
    _split_by_time_window: the stored date is the fact, a sentence that disagrees is not sent."""
    start, last = _parse_date(alert.outage_date or ''), _outage_last_day(alert)
    if not start or not text:
        return True
    if not last or last < start:
        last = start
    if _crosses_midnight(loc.time_window if loc is not None else None) \
            or _crosses_midnight(getattr(alert, 'time_window', None)):
        last += timedelta(days=1)
    for m in _MESSAGE_DATE.finditer(text):
        year, month = int(m[4] or m[5]), int(m[3])
        for i, d in enumerate(filter(None, (m[1], m[2]))):
            mon, yr = month, year
            if i == 0 and m[2] and int(m[1]) > int(m[2]):     # "30/01.10": the night starts in September
                mon, yr = (12, year - 1) if month == 1 else (month - 1, year)
            try:
                day = datetime(yr, mon, int(d))
            except ValueError:
                continue
            if not start.date() <= day.date() <= last.date():
                return False
    return True


def _hours_unknown(alert) -> bool:
    """A notice running over several days with no hours on any location: the source gave none,
    and _drop_invented_windows took out the parser's stand-in (rule 8: 07:00 - 19:00). The model
    wrote message_template before that, so the template still states the made-up hours and only
    the first day - Toplana Cerak's remont to 01.10. went out as "dana 30.09.2026. u periodu
    07:00 - 19:00" (#3465). The sentence is built from the facts instead."""
    if not _date_span(alert):
        return False
    return not any((loc.time_window or '').strip().upper() not in ('', 'N/A')
                   for loc in alert.affected_locations)


# The phrase the model puts in front of the placeholder, written for a street. A whole-area
# match (ENTIRE_SETTLEMENT) puts the area's name in that slot: "u ulici Cerak" went out for
# "celo grejno područje toplane Cerak" (#3465).
_STREET_SLOT = (
    (re.compile(r"\b([uU]) ulic(?:i|ama)\s+\*\*\*"), r"\1 naselju ***"),
    (re.compile(r"\bna adresi\s+\*\*\*"), "u naselju ***"),
    (re.compile(r"\b([zZ])a ulic[ue]\s+\*\*\*"), r"\1a naselje ***"),
)


def _render_message(alert: OutageAlert, loc: StreetSegment, target_street: str | None,
                    category: str | None = None, heat_supply: str | None = None) -> str:
    """Substitutes the recipient's street into the alert's single message template. One template
    per announcement replaces the old 3-variations-per-street scheme: identical text to many
    users is normal for an official Telegram bot (unlike the previous Appium-driven personal
    Viber account, where repetition looked bot-like), so the redundancy bought nothing and cost
    a large multiple of generated tokens per outage.

    `category` is the scraper's own guaranteed utility type. When the model's template doesn't
    describe that utility the template is discarded, not sent: confirmed real case - an EPS row
    is just "Opština | Vreme | Ulice" with no utility word anywhere in it, so the model had
    nothing to ground on and announced a power cut as "planirano isključenje vode". Wrong
    utility is worse than plain, so the fallback below is built from facts we hold, not text.
    The same goes for a multi-day notice with no stated hours (_hours_unknown), and for a toplana
    notice whose template doesn't say what is cut the way the notice does (`heat_supply`, from
    core.filters.heat_supply_cut on the raw text). A location with no street that matched on its
    settlement names an area: "u naselju X", never "u ulici X"."""
    template = (alert.message_template or '').strip()
    display = _location_display_name(loc, target_street)
    area = not loc.main_street and bool(loc.settlement)
    # A village the notice says is cut only in part (PART_MARKER): the message says so.
    area_word = "delu naselja" if area and (loc.landmark or '').strip() == PART_MARKER else "naselju"
    span = _date_span(alert) if _hours_unknown(alert) else None
    heat_base = _HEAT_SUPPLY_BASE.get(heat_supply) if (category or "").upper() == "HEATING" else None

    if template and not _template_matches_category(template, category):
        print(f"⚠️ message_template contradicts category={category}, rebuilding: {template!r}")
        template = ''
    elif template and heat_base and heat_supply_cut(template) != heat_supply:
        print(f"ℹ️ template doesn't say what the toplana cuts ({heat_supply}), rebuilding: {template!r}")
        template = ''
    elif template and span:
        print(f"ℹ️ multi-day notice with no stated hours, message built from facts: {template!r}")
        template = ''
    elif template and not _message_dates_are_its_own(template, alert, loc):
        print(f"⚠️ message_template names a date that isn't the outage's own "
              f"({alert.outage_date}), rebuilding: {template!r}")
        template = ''

    if not template:
        base = heat_base or _CATEGORY_FALLBACK.get((category or "").upper(), "Planirano isključenje")
        where = f"u {area_word} {display}" if area else f"u ulici {display}"
        parts = [f"{base} {where}" if display else base]
        when = (alert.outage_date or '').strip()
        if span:
            parts.append(span)
        elif when and when.upper() != 'N/A':
            parts.append(f"dana {when}")
        window = (loc.time_window or '').strip()
        if window and window.upper() != 'N/A':
            parts.append(f"u periodu {window}")
        return " ".join(parts).rstrip('.') + "."

    if area:
        for pattern, replacement in _STREET_SLOT:
            template = pattern.sub(replacement.replace("naselju", area_word), template)

    if STREET_PLACEHOLDER in template:
        return _append_time_window(template.replace(STREET_PLACEHOLDER, display), loc.time_window)

    # The model occasionally drops the placeholder. Rather than send a message that never names
    # the street, append it instead of silently losing the personalization.
    body = f"{template} ({display})" if display else template
    return _append_time_window(body, loc.time_window)


# Works on a toplana's network that say nothing is cut (core.filters.heat_works_only). Beogradske
# elektrane list them again every working day, with no hours and no end: one address on Braće
# Jerković was mailed 14 times from 08.09. to 30.09.2026, each time in the model's words
# ("Planirano isključenje toplovoda ... u periodu 07:00 - 19:00", #3459). Now the message says
# what the notice says. The first two days of a run are told day by day; the third says the works
# go on "do daljnjeg", and after that the run is quiet unless the notice says something new: an
# end, or a different end (works extended). A cut is not works and always goes out.
_WORKS_BASE = "Radovi na toplovodnoj mreži"
# Days between two posts of the same works that still make one run (a weekend, a holiday).
_WORKS_GAP_DAYS = 7
# The post of a run that turns the daily messages into one "do daljnjeg".
_WORKS_REPEATS = 3
# A works message read back: the place, its day (or first day), and "daljnjeg" or its last day.
_WORKS_TOLD = re.compile(re.escape(_WORKS_BASE) + r"(?: u (?:ulici|naselju) (.+?))? (?:dana|traju od|od) "
                         r"(\d\d\.\d\d\.\d{4})(?: do (daljnjeg|\d\d\.\d\d\.\d{4}))?")


def _works_run(cursor, alert, loc, region, category) -> list:
    """The days of the run of works posts this notice belongs to, first to last: walking back from
    its own day through the stored works posts that name the same street, each at most
    _WORKS_GAP_DAYS before the next. A location with no street of its own is a run of one day."""
    day = (_parse_date(alert.outage_date or '') or datetime.now()).date()
    days = {day}
    if loc.main_street:
        cursor.execute("SELECT region, outage_date, affected_streets, raw_text FROM outages "
                       "WHERE category IS ? AND created_at > datetime('now', '-180 days')", (category,))
        want = _region_variants(region)
        for row in cursor.fetchall():
            posted = _parse_date(row['outage_date'] or '')
            if not posted or posted.date() > day:
                continue
            if want and not (want & _region_variants(row['region'])):
                continue
            if any(_streets_match(s, loc.main_street) for s in (row['affected_streets'] or '').split(',')) \
                    and heat_works_only(row['raw_text']):
                days.add(posted.date())
    run = [day]
    for d in sorted(days, reverse=True)[1:]:
        if (run[-1] - d).days > _WORKS_GAP_DAYS:
            break
        run.append(d)
    return run[::-1]


def _works_message(cursor, alert, loc, target, region, category) -> str | None:
    """The works message for one person, or None when the run has nothing new for them. Their
    earlier works messages for this place are read back (_WORKS_TOLD) and the place compared like
    a street match: the parser wrote "БРАЋЕ ЈЕРКОВИЋ" one day and "БРАЋЕ ЈЕРКОВИЋА" the next. A
    message in the model's words from before this doesn't count."""
    display = _location_display_name(loc, target['ulica'])
    where = f"u naselju {display}" if not loc.main_street and loc.settlement else f"u ulici {display}"
    head = f"{_WORKS_BASE} {where}" if display else _WORKS_BASE
    run = _works_run(cursor, alert, loc, region, category)
    since, day = run[0], run[-1]
    cursor.execute('''
        SELECT o.outage_date, m.custom_message FROM message_queue m JOIN outages o ON o.id = m.outage_id
        WHERE m.target_id = ? AND o.category IS ? AND m.status IN ('QUEUED', 'SENDING', 'SENT')
          AND m.custom_message LIKE ?
    ''', (target['target_id'], category, _WORKS_BASE + '%'))
    told_days, told_ends = set(), set()
    for row in cursor.fetchall():
        posted = _parse_date(row['outage_date'] or '')
        told = _WORKS_TOLD.match(row['custom_message'] or '')
        if posted and posted.date() >= since and told and (
                (told.group(1) or '') == display or _streets_match(told.group(1), display)):
            told_days.add(posted.date())
            told_ends.add(told.group(3))
    effect = "Moguć prekid grejanja ili tople vode."
    # Niš toplana give the end ("radovi će trajati do 13.09."); Beogradske elektrane never do.
    end = _parse_date(getattr(alert, 'end_date', None) or '')
    if end and end.date() > day:
        last = f"{end:%d.%m.%Y}"
        return None if last in told_ends else f"{head} od {since:%d.%m.%Y} do {last}. {effect}"
    if 'daljnjeg' in told_ends or day in told_days:
        return None
    if len(run) >= _WORKS_REPEATS:
        return (f"{head} traju od {since:%d.%m.%Y} do daljnjeg. {effect} Za ove radove više ne "
                "javljamo, osim ako najave prekid ili kraj.")
    return f"{head} dana {day:%d.%m.%Y}. {effect}"


def _target_channels(target) -> list[str]:
    """Which channel(s) a target row is actually reachable on - a target can now legally carry
    both a telegram_chat_id and an email at once (see the dashboard's "add a second channel to
    this address"), so this is a list, not a single value."""
    # Bracket access, not .get() - a sqlite3.Row (the real candidate_targets/target_row shape from
    # every actual caller) supports __getitem__ but has no .get() method at all; every dict this
    # is ever called with (sqlite3.Row or a plain dict literal) is built to always carry both keys.
    channels = []
    if target['telegram_chat_id']:
        channels.append('telegram')
    if target['email'] and _channel_active(target, 'email_active'):
        channels.append('email')
    return channels


def _channel_active(target, key) -> bool:
    """True unless the row explicitly carries `key` set to 0. The send-time _ELIGIBLE check in
    each worker is the real gate; this just stops us QUEUEING a row a worker will always refuse
    (e.g. an email row for an address whose email_active=0 after an unsubscribe) - such a row
    strands as QUEUED forever and trips the "queue not draining" watchdog every hour. A caller
    whose SELECT doesn't include the column is treated as active."""
    val = _row_get(target, key)
    return val is None or val == 1


def _match_eligible_locations(affected_locations, match_quality):
    """Which of an outage's locations may match a target.

    On an EXACT outage an ENTIRE_SETTLEMENT location (no main_street) is trusted to cover
    everyone the H3 hex match already found - it is the one no-street type calculate_outage_hexes
    actually resolves to coverage of its own. A LANDMARK (or any other no-street, non-settlement)
    location contributes no hexes at all, so it has nothing to vouch for and must not act as a
    blanket matcher. Confirmed real case: outage #2178 (Novi Sad, 22.09.2026) named two streets
    plus four bank buildings (Vojvođanska banka, Narodna banka, Société Générale, DDOR) as
    LANDMARK entries. The two streets resolved to 4 real hexes; a resident of Danila Kiša, whose
    own hex overlapped one of them, matched through a bank LANDMARK wildcard instead of the
    street-name and house-number check that would have excluded them, and the message then read
    "u ulici Danila Kiša" because a location with no street falls back to the recipient's own
    street for display. Two real users were emailed for an outage nowhere near their address.

    On a FALLBACK / UNRESOLVED outage the hexes are an approximation - a street didn't resolve
    so a whole-settlement polygon stood in - and that polygon bleeds a hex or two into the
    neighbouring area when rasterised. Confirmed real case: outage #792, "Делови насеља Рибњак"
    fell back to the whole Рибњак suburb polygon, which clipped one H3 cell of central Novi Sad
    and mis-notified addresses on Futoška (nowhere near Ribnjak, not in the announcement's
    street list). So on an approximate outage, a no-street location cannot blanket-notify -
    only the explicitly named STREET locations may match."""
    if match_quality in ('FALLBACK', 'UNRESOLVED'):
        return [loc for loc in affected_locations if loc.main_street]
    # A whole-settlement location that is only a spot in the place (PART_MARKER) never stands
    # for everyone living there.
    return [loc for loc in affected_locations
            if loc.main_street or (loc.location_type == 'ENTIRE_SETTLEMENT'
                                   and _settlement_part(loc)[0] != 'spot')]


def _target_match(alert, affected_locations, target, match_quality):
    """The location of this outage that covers this target - street and house number, or the
    settlement for a no-street one - unless an exclusion takes the target back out; else None.
    The one rule the alerts, the registration backfill and the dashboard's history all use.
    `target` needs ulica and broj; city/naselje/opstina when it has them."""
    target_number = target['broj']
    target_street = target['ulica']
    # The settlement the target lives in, as _resolve_target_hexes maps it. Registration stores
    # the place in `city` (an RGZ naselje picked from the autocomplete); `naselje`/`opstina` are
    # set only on some rows. And the opštine a whole-settlement location is checked against: the
    # outage's, plus the target's own when it's on record.
    target_naselje = _row_get(target, 'naselje') or _row_get(target, 'city')
    opstine = set(_region_opstine(alert.city))
    if _row_get(target, 'opstina'):
        opstine.add(_norm_region_lat(target['opstina']))

    # The streets round a block are its edge, not who is cut (_mark_block_ring): someone matches
    # by where their house stands, inside the block, never by living on one of those streets.
    matched_loc = next(
        (loc for loc in _match_eligible_locations(affected_locations, match_quality)
         if not _in_block_ring(loc)
         and _location_matches_target(loc, target_street, target_number, target_naselje, opstine)
         and not _outside_stretch(alert, loc, target)),
        None
    )
    if matched_loc is None:
        ring = _block_ring(affected_locations)
        if ring and _block_where(alert, ring, target) == 'inside':
            matched_loc = ring[0]
    if matched_loc is None:
        return None

    if alert.excluded_locations and any(
        _location_matches_target(excl, target_street, target_number, target_naselje, opstine,
                                 exclusion=True)
        for excl in alert.excluded_locations
    ):
        return None
    return matched_loc


def _queue_time_change(cursor, outage_id, target, previous, message) -> int:
    """Tell an already-notified target that the outage's hours changed (dedup-on-update found the
    same notice with a new time). A message not yet sent is just rewritten; a sent one gets a
    follow-up "Izmena najave". Before this, the map quietly showed the new hours while everyone
    already told kept the old ones. Returns how many channels were told."""
    told = 0
    for channel in _target_channels(target):
        prev = previous.get((target['target_id'], channel))
        if not prev or prev['status'] in ('FAILED', 'SKIPPED'):
            continue
        if prev['status'] in ('QUEUED', 'HELD'):
            cursor.execute("UPDATE message_queue SET custom_message = ? WHERE id = ?", (message, prev['id']))
        else:
            cursor.execute('''
                INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at)
                VALUES (?, ?, ?, ?, 'QUEUED', datetime('now'))
            ''', (outage_id, target['target_id'], channel, CHANGED_PREFIX + message))
        told += 1
    return told


def _match_and_queue_target(cursor, outage_id, alert, affected_locations, target,
                            already_notified, category=None, match_quality=None,
                            previous=None, heat_supply=None, works=False,
                            region=None) -> tuple[int, int, int]:
    """Per-target half of the match: given a hex-overlapping candidate `target` (needs keys
    target_id, ulica, broj, telegram_chat_id, email), run the street-scoped parity/number-range
    verification, honour excluded_locations, and insert one QUEUED message_queue row per channel
    the target actually has that hasn't already been queued/sent for this outage. Returns
    (queued_delta, skipped_delta, wildcard_delta) - queued_delta counts the TARGET as 1 if
    anything was queued for it (any number of channels), matching the existing "N queued" log
    semantics. wildcard_delta is 1 when the match came through a location with no main_street of
    its own (an ENTIRE_SETTLEMENT match, trusted only because calculate_outage_hexes actually
    resolved that settlement's own coverage) rather than the target's own street being explicitly
    named - the caller uses this to flag an outage for operator review even when match_quality
    says EXACT, since EXACT only means the geocoding resolved cleanly, not that every individual
    match was against a literal street name (confirmed real case: outage #2178's LANDMARK
    wildcard reported EXACT while wrongly matching two Danila Kiša residents - see
    _match_eligible_locations for the fix; this alert is the operator-visibility half of it).

    `already_notified` is a set of (target_id, channel) tuples, not just target_id - a target that
    was already notified by email for this outage but has since connected Telegram too must still
    get a fresh telegram row queued, not be skipped outright as "already notified".

    Extracted so the registration backfill (backfill_target_against_active_outages) runs the
    EXACT same verification as the live scrape path in process_and_queue_outage, instead of a
    second, subtly-different copy.

    `previous` ({(target_id, channel): latest message_queue row}) is given only when the
    dedup-on-update found this notice with new hours: an already-notified target that still
    matches is then told about the change (_queue_time_change) instead of being skipped.

    `works` marks a toplana works notice with no stated cut (_works_message): someone already
    told about the same works in `region` is skipped, like an already-notified target."""
    last_day = _outage_last_day(alert)
    if last_day and last_day.date() < datetime.now().date():
        # A past-dated outage must never generate a notification, however it reached this point:
        # a source re-publishing an old post with a fresh timestamp (new raw_text_hash, so it
        # re-ingests as a "new" outage), a date-parse regression, or a late signup backfilled
        # against a long-expired row whose expires_at was NULL. The outage still lives in the DB
        # for the map and history - this only suppresses the message. Real incident: nstoplana
        # bumped the <time> stamp on August posts, one re-ingested as outage #1063 dated in the
        # past and mailed three live users before this guard existed.
        return 0, 0, 0
    target_id = target['target_id']
    target_street = target['ulica']

    matched_loc = _target_match(alert, affected_locations, target, match_quality)
    if matched_loc is None:
        return 0, 0, 0

    to_queue = [c for c in _target_channels(target) if (target_id, c) not in already_notified]
    if _in_block_ring(matched_loc):
        custom_message = _block_message(alert, _block_ring(affected_locations), category)
    elif works:
        custom_message = to_queue and _works_message(cursor, alert, matched_loc, target, region, category)
        if not custom_message:
            return 0, 1, 0
    else:
        custom_message = _render_message(alert, matched_loc, target_street, category, heat_supply)
    if not works and not _message_dates_are_its_own(custom_message, alert, matched_loc):
        # The last line of defence: a message that names another day than the outage's own is
        # never queued (_render_message already rebuilds a model template that does; this
        # catches a regression there). The operator hears of it instead. A works message names
        # the day its run began on purpose ("traju od 21.10.2026 do daljnjeg") and is built
        # from stored dates, never from the model's text.
        print(f"📅 Outage #{outage_id}: message names another day than {alert.outage_date} - "
              f"not sent: {custom_message!r}")
        send_operator_alert(f"message_date:{outage_id}",
                            f"Outage #{outage_id}: alert NOT sent, its message names another day "
                            f"than the outage's own ({alert.outage_date}):\n{custom_message}",
                            cooldown_s=86400)
        return 0, 1, 0
    if previous and _queue_time_change(cursor, outage_id, target, previous, custom_message):
        # The hours changed: the channels told before get the change, a new channel (connected
        # since) still gets its first message below.
        if not to_queue:
            return 1, 0, 0
    if not to_queue:
        # Every channel this target has was already queued/sent for this outage - re-queueing is
        # the exact bug the dedup-on-update path exists to prevent, and the backfill must respect
        # it too.
        return 0, 1, 0

    status = _send_status(cursor, outage_id, area=not matched_loc.main_street)
    if status is None:
        return 0, 1, 0                      # the operator said no to this outage's area alert
    for channel in to_queue:
        cursor.execute('''
            INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        ''', (outage_id, target_id, channel, custom_message, status))
    return 1, 0, (0 if matched_loc.main_street else 1)


def _send_status(cursor, outage_id, area: bool) -> str | None:
    """QUEUED, HELD (core/send_check.py looks first) or None (not sent). A match through a whole
    settlement is held until checked; a date hold holds everything of the outage. A decision
    already made covers later matches of the same outage."""
    row = cursor.execute("SELECT status, reason FROM send_checks WHERE outage_id = ?",
                         (outage_id,)).fetchone()
    if row is None:
        return 'HELD' if area else 'QUEUED'
    status, reason = row[0], row[1]
    applies = area or reason == 'date'
    if status in ('AGREED', 'APPROVED') or not applies:
        return 'QUEUED'
    return None if status == 'REJECTED' else 'HELD'


# Dates the notice itself states: "15.10.2026", "15.10.", and ranges "21.-26.09.2026".
_DAY_MONTH = re.compile(r"(?<!\d)(\d{1,2})\s*\.\s*(\d{1,2})\s*\.")
_DAY_RANGE = re.compile(r"(?<!\d)(\d{1,2})\s*\.?\s*[-–]\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.")


# A day with its month spelled out: "у суботу 19. септембра", "28- ог октобра", "8 – ог октобра",
# "5. novembra".
_MONTHS = ("јануар", "фебруар", "март", "април", "мај", "јун", "јул", "август", "септемб", "октоб",
           "новемб", "децемб")
_MONTHS_LAT = ("januar", "februar", "mart", "april", "maj", "jun", "jul", "avgust", "septemb", "oktob",
               "novemb", "decemb")
_DAY_MONTH_NAME = re.compile(r"(?<!\d)(\d{1,2})\s*\.?\s*(?:[-–]\s*(?:ог|og)\s+)?(" + "|".join(_MONTHS + _MONTHS_LAT)
                             + r")", re.IGNORECASE)


def notice_days(raw_text: str) -> set[tuple[int, int]]:
    """Every (day, month) the notice's text states, in figures or with the month spelled out."""
    text = raw_text or ''
    stated = {(int(d), int(m)) for d, m in _DAY_MONTH.findall(text)}
    for a, b, m in _DAY_RANGE.findall(text):
        stated |= {(d, int(m)) for d in range(int(a), int(b) + 1)}
    for d, name in _DAY_MONTH_NAME.findall(text):
        name = name.lower()
        month = next(i for i, (c, l) in enumerate(zip(_MONTHS, _MONTHS_LAT), 1)
                     if name.startswith(c) or name.startswith(l))
        stated.add((int(d), month))
    return stated


def _date_not_in_notice(outage_date: str | None, raw_text: str) -> bool:
    """True when the notice states dates and the one the model read is not among them - the
    model's date is then not trusted to send on (core/send_check.py, reason 'date'). A notice
    with no dates of its own (the scraper gave the date) can't disagree."""
    parsed = _parse_date(outage_date or '')
    if not parsed:
        return False
    stated = notice_days(raw_text)
    return bool(stated) and (parsed.day, parsed.month) not in stated


# A whole-settlement location the notice itself says is only PART of the place. The model
# returns "Блок 8 у Петроварадину" as ENTIRE_SETTLEMENT Петроварадин with the block as its
# landmark, and the whole of Petrovaradin went on the map and would have been alerted (#4865,
# 15.10.2026). 375 stored outages had the same shape: "Део села Лозовик", "Здравиње - засеок
# поток", "Самаила - центар села", "у Паковраћу код Еурошилда". Two kinds, owner's rule:
# - a spot (a block, a hamlet, a centre, "kod" a building, a weekend zone): never the whole
#   place - a small marker on the map, and that location alerts nobody (the other places and
#   streets of the same notice still do: "Роћевац, Ђуринац и део села Бобово");
# - "deo sela X" with no spot named: in a village everyone there is told, and the message says
#   "u delu naselja X"; in a town (Kać, Veternik) it is a spot again - "part of Veternik" is not
#   a reason to alert 17,000 people.
PART_MARKER = "ДЕО НАСЕЉА"
# The same "deo" in a town: from here on it is a spot.
PART_TOWN_MARKER = "ДЕО МЕСТА"
# A village: at most this many RGZ streets (Begeč 23, Samaila 36; Kać 65, Petrovaradin 133), or,
# where RGZ doesn't carry the place, at most this many hexes.
PART_VILLAGE_MAX_STREETS = 40
PART_VILLAGE_MAX_HEXES = 150
_PART_WORDS = r"(?:DEO|DELA|DELU|DELOM|DELOVI|DELOVA)"
_SPOT_WORDS = r"(?:BLOK\w*|ZASEOK|ZASEOC\w*|ZASELAK|VIKEND|CENTAR|KOD)"


def _folded_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", _fold_name(cyrillic_to_latin(text or "")))


def _settlement_stem(name: str | None) -> str | None:
    """The settlement's last word without its case ending, as the notice may decline it:
    ПЕТРОВАРАДИН -> PETROVARAD (matches "Петроварадину"), ЛОЗОВИК -> LOZOV."""
    words = re.findall(r"[A-Z0-9]+", _fold_name(cyrillic_to_latin(name or "")))
    if not words:
        return None
    word = words[-1]
    return word[:-2] if len(word) > 5 else word


def _settlement_part(loc, raw_text: str = "") -> tuple[str | None, str | None]:
    """('spot' | 'part' | None, the words that say so) for one location. Only an
    ENTIRE_SETTLEMENT location with a name of its own can be either."""
    if loc.location_type != 'ENTIRE_SETTLEMENT' or not (loc.settlement or '').strip():
        return None, None
    landmark = (loc.landmark or '').strip()
    if landmark == PART_MARKER:
        return 'part', None
    if landmark:
        return 'spot', landmark
    # What the model itself said (parser.StreetSegment.scope); the narrower of it and the text wins.
    scope = getattr(loc, 'scope', None)
    if scope == 'SPOT':
        return 'spot', None
    stem, text = _settlement_stem(loc.settlement), _folded_text(raw_text)
    if not stem or not text:
        return ('part', None) if scope == 'PART' else (None, None)
    name = rf"\b{re.escape(stem)}\w*"
    for pattern in (rf"\b{_SPOT_WORDS}\b(?:\s+\S+){{0,4}}?\s+{name}",
                    rf"{name}\s*[-–(,:]?\s*{_SPOT_WORDS}\b(?:\s+\S+){{0,2}}"):
        m = re.search(pattern, text)
        if m:
            return 'spot', m.group(0).strip(" ,.-–(:")
    if scope == 'PART' or re.search(rf"\b{_PART_WORDS}\b(?:\s+\S+){{0,3}}?\s+{name}", text):
        return 'part', None
    return None, None


def _mark_settlement_parts(alert: OutageAlert, raw_text: str) -> None:
    """Writes what _settlement_part found into the locations themselves (the spot as the
    landmark, a plain part as PART_MARKER), so the stored parsed_json carries it to the map, the
    message and the registration backfill, none of which have the notice's text."""
    for loc in alert.affected_locations:
        kind, words = _settlement_part(loc, raw_text)
        if kind == 'spot' and not (loc.landmark or '').strip():
            loc.landmark = words or PART_TOWN_MARKER
        elif kind == 'part' and not (loc.landmark or '').strip():
            loc.landmark = PART_MARKER


# How a no-street location is named in the outage's street list (and on the map card):
# "Begeč (celo naselje)", "Lozovik (deo naselja)", "Petrovaradin (deo naselja: Blok 8)".
AREA_LABEL = re.compile(r"\((?:celo naselje|deo naselja)(?::[^)]*)?\)$")


def _area_label(loc) -> str:
    kind, _ = _settlement_part(loc)
    landmark = (loc.landmark or '').strip()
    if kind == 'spot' and landmark != PART_TOWN_MARKER:
        return f"{loc.settlement} (deo naselja: {landmark})"
    if kind:
        return f"{loc.settlement} (deo naselja)"
    return f"{loc.settlement} (celo naselje)"


def _part_is_whole_village(settlement: str, region: str, village_hexes) -> bool:
    # A municipality's seat (Kosjerić, Gadžin Han) is a town even where RGZ has no streets for it.
    if is_serbian_opstina(settlement):
        return False
    count = rgz_naselje_street_count(settlement, region)
    if count is not None:
        return count <= PART_VILLAGE_MAX_STREETS
    return len(village_hexes or ()) <= PART_VILLAGE_MAX_HEXES


def _whole_settlement_not_meant(loc, region: str, village_hexes) -> bool:
    """True when this whole-settlement location must not stand for the whole place."""
    kind, _ = _settlement_part(loc)
    return kind == 'spot' or (kind == 'part' and not _part_is_whole_village(
        loc.settlement, region, village_hexes))


def calculate_outage_hexes(parsed_data: OutageAlert | dict, raw_text: str = "",
                           gate_region: str | None = None) -> tuple[list[str], str]:
    """Returns (hexes, match_quality). match_quality is 'EXACT' if every location resolved via
    its primary path, 'FALLBACK' if a small settlement's whole-polygon coverage had to stand in
    for an unresolved street, 'UNRESOLVED' if something couldn't be resolved at all (see
    SMALL_SETTLEMENT_RADIUS_THRESHOLD for why a big settlement doesn't get the same fallback)."""
    alert = _normalize_alert(parsed_data)
    if not alert:
        return [], 'UNRESOLVED'
    alert.affected_locations = _inherit_settlement_context(alert.affected_locations, raw_text)
    _mark_settlement_parts(alert, raw_text)

    all_affected_hexes = set()
    match_quality = 'EXACT'
    city_default = alert.city or 'НОВИ САД'
    # Settlements of streets that couldn't be resolved at all, held back for the deferred
    # whole-settlement decision after the loop (see the note where it's consumed).
    unresolved_settlements = []
    # The same streets with what the variant pass after the loop needs to place them.
    unresolved_streets = []
    region_ops = region_opstine(city_default)

    def _add(hexes, env, label):
        """Every hex enters the result through the leak gate (see _gate_hexes). A location whose
        hexes ALL fall outside its announced area resolved to the wrong place entirely - that is
        an unresolved location, not a match."""
        nonlocal match_quality
        kept = _gate_hexes(hexes, env, label)
        if hexes and not kept:
            match_quality = _worse_quality(match_quality, 'UNRESOLVED')
        all_affected_hexes.update(kept)

    # A block inside a ring of streets is its own area, not the streets (_mark_block_ring).
    _mark_block_ring(alert, raw_text)
    ring = _block_ring(alert.affected_locations)
    if ring:
        shape = _block_shape(ring, city_default)
        names = ", ".join(loc.main_street for loc in ring)
        if shape:
            _add(block_area.block_cells(shape),
                 _location_envelope(ring[0].settlement, city_default, region_ops, gate_region),
                 f"block {names} ({city_default})")
        else:
            # Nobody alerted, and no retry with another model: the parse is fine, the streets
            # just don't close round a block on the map.
            match_quality = _worse_quality(match_quality, 'FALLBACK')
            print(f"   ↳ block streets don't close round a block - nobody alerted: {names}")
            send_operator_note(f"A block notice whose streets don't close round a block on the map "
                               f"- nobody alerted:\n{city_default}, {alert.outage_date or 'N/A'}\n{names}")

    for location in alert.affected_locations:
        if _in_block_ring(location) and ring:
            continue
        loc_type = location.location_type
        raw_street = location.main_street
        settlement = location.settlement or city_default

        int_start = location.intersection_start
        int_end = location.intersection_end
        widen = getattr(location, 'includes_surrounding_streets', False)
        loc_env = _location_envelope(location.settlement, city_default, region_ops, gate_region)
        loc_label = f"{raw_street or settlement} ({city_default})"

        # CASE 1: Specific Street / Vector Sub-segment Mapping
        if raw_street:
            main_street = clean_street(raw_street)
            street_hexes = set()
            if main_street:
                street_hexes = get_street_hexes(
                    main_street,
                    settlement=settlement,
                    parent_region=city_default,
                    intersection_start=int_start,
                    intersection_end=int_end
                )

            # TIER 2: the street itself is known, but clipping it to the announced sub-segment
            # produced nothing. Confirmed real case: Прешевска (Звездара) is digitized as two
            # disjoint chains and its two cross-street bounds land on different ones, so
            # clip_polyline_by_intersections deliberately refuses to combine them (see its
            # comp_start/comp_end guard) and returns empty. Falling straight through to the
            # whole-settlement tier below would trade one unmapped street segment for an entire
            # municipality of false notifications. The whole street is a far tighter and more
            # honest substitute: it over-covers by the rest of ONE street, not by a city.
            if not street_hexes and main_street and (int_start or int_end):
                kind, corner = _stretch_stand_in(location, alert, main_street, settlement,
                                                 city_default)
                if kind == 'corner':
                    _add(corner, loc_env, loc_label)
                    match_quality = _worse_quality(match_quality, 'FALLBACK')
                    continue
                if kind == 'sibling':
                    match_quality = _worse_quality(match_quality, 'FALLBACK')
                    continue
                whole_street_hexes = get_street_hexes(
                    main_street, settlement=settlement, parent_region=city_default
                )
                if whole_street_hexes:
                    if widen:
                        whole_street_hexes = whole_street_hexes | _surrounding_streets(
                            main_street, settlement, city_default)[0]
                    _add(whole_street_hexes, loc_env, loc_label)
                    match_quality = _worse_quality(match_quality, 'FALLBACK')
                    continue

            if street_hexes:
                if widen:
                    # The connecting side streets are streets the announcement never named, so
                    # the result is no longer an EXACT match even though the named street itself
                    # resolved precisely.
                    street_hexes = street_hexes | _surrounding_streets(
                        main_street, settlement, city_default, int_start, int_end)[0]
                    match_quality = _worse_quality(match_quality, 'FALLBACK')
                _add(street_hexes, loc_env, loc_label)
            else:
                # The street isn't in the local street database under any form — not a distance
                # or spelling issue, a genuine coverage gap. Deferred rather than resolved here:
                # whether a whole-settlement substitute is acceptable depends on what the REST of
                # this announcement managed to resolve, which isn't known until the loop ends.
                unresolved_settlements.append(settlement)
                # Re-spelling is for a street the text placed somewhere; one with no place of
                # its own is taken exactly as written (owner's rule, see _inherit_settlement_context).
                if main_street and (location.settlement or "").strip():
                    unresolved_streets.append((location, main_street, settlement, loc_env, loc_label))

        # CASE 2: Strict Whole Settlement Polyfill ONLY
        elif loc_type == "ENTIRE_SETTLEMENT":
            # An ENTIRE_SETTLEMENT with no settlement of its own (or one that's just the
            # opština/city again) is a parse artifact, not a genuine whole-city outage —
            # confirmed real case: a Kragujevac main-pipe post naming "Шумарице, Вишњак, део
            # Станова…" came back as five ENTIRE_SETTLEMENT rows with the names dropped, and
            # defaulting each to city_default blanketed all of Kragujevac (746 hexes). Skip it;
            # if the outage really is city-wide the announcement will have named streets too.
            if not (location.settlement or "").strip() or \
               cyrillic_to_latin(location.settlement).upper().strip() == cyrillic_to_latin(city_default).upper().strip():
                match_quality = _worse_quality(match_quality, 'UNRESOLVED')
                continue

            village_hexes, coverage_source = get_settlement_coverage(settlement, parent_region=city_default)
            if village_hexes:
                _shex, _radius = get_settlement_info(settlement, city_default)
                # A city sub-district (a Zrenjanin mesna zajednica like "Д2" or "Багљаш") has no
                # real boundary of its own. get_settlement_coverage answers the ambiguous name
                # with a city-sized OSM polygon, so drawing it makes the whole grad look cut off
                # and a no-street ENTIRE_SETTLEMENT would blanket-notify everyone in town.
                # vikzr's daily-works headings name the area a spot job sits in, not a
                # whole-district cut. Confirmed: outage #1106, heading "Д2", drew a polygon over
                # all of Zrenjanin and telegrammed a test address far from the works. When the
                # resolved area is town/city scale (radius above the village threshold, or an
                # implausible hex count for a named sub-locality), shrink it to a ~1km marker
                # disk around the centre and force FALLBACK so _match_eligible_locations never
                # notifies on this location. Real villages (Меленци, Орловат, Чента) stay under
                # the threshold and keep their true polygon + EXACT whole-settlement notify.
                oversized = ((_radius or 0) > SMALL_SETTLEMENT_RADIUS_THRESHOLD) or len(village_hexes) > 500
                if oversized:
                    village_hexes = set(h3.grid_disk(_shex, 3)) if _shex else set()
                    match_quality = _worse_quality(match_quality, 'FALLBACK')
                elif coverage_source not in ('curated', 'polygon', 'rgz_naselje'):
                    # A center-point disk is a guess at the settlement's shape, not its real
                    # boundary — common for city kvartovi that OSM only has as a point (Мала
                    # Америка, Градулица, Берберско). Reporting that as EXACT would hide a real
                    # over/under-coverage risk behind the project's most confident label.
                    match_quality = _worse_quality(match_quality, 'FALLBACK')

                if village_hexes and _whole_settlement_not_meant(location, city_default, village_hexes):
                    # Only part of the place (see PART_MARKER): a marker where the place is, and
                    # the location becomes a spot, which _match_eligible_locations never matches.
                    if (location.landmark or '').strip() == PART_MARKER:
                        location.landmark = PART_TOWN_MARKER
                    village_hexes = set(h3.grid_disk(_shex, 1)) if _shex else set()

                if village_hexes:
                    _add(village_hexes, loc_env, loc_label)
                else:
                    match_quality = _worse_quality(match_quality, 'UNRESOLVED')
            else:
                match_quality = _worse_quality(match_quality, 'UNRESOLVED')

    # TIER 2b (deferred): a street no form of its name resolved may be the same street written
    # another way - EPS's "Хероја Пинкија" in Bukovac is RGZ's "Пинкијева", right next to the other
    # streets of that announcement. Accepted only near streets of the SAME announcement that did
    # resolve (see _variant_near); the location is renamed so users on the real street match by
    # name, and the outage goes FALLBACK so any send is flagged to the operator with the change.
    anchor = set(all_affected_hexes)
    for location, main_street, settlement, loc_env, loc_label in (unresolved_streets if anchor else []):
        fixed = _variant_near(main_street, settlement, city_default, anchor)
        if not fixed:
            continue
        variant, variant_hexes = fixed
        _name_corrections.append(f"{main_street} -> {variant} ({settlement})")
        print(f"   ↳ street name read as another spelling: {main_street} -> {variant} ({settlement})")
        location.main_street = variant
        _add(variant_hexes, loc_env, loc_label)
        match_quality = _worse_quality(match_quality, 'FALLBACK')
        unresolved_settlements.remove(settlement)

    # TIER 3 (deferred): whole-settlement coverage for streets nothing else could resolve.
    #
    # Only when the announcement produced NO precise coverage at all. An announcement naming 12
    # streets where 11 resolved exactly does not become more accurate by also blanketing the
    # whole municipality on account of the 12th — that drowns 11 precise streets in tens of
    # thousands of unrelated addresses (confirmed: one unmapped Звездара street was pulling in
    # 279 settlement hexes alongside ~24 real ones). With real coverage in hand, the honest
    # outcome for the straggler is to flag the announcement UNRESOLVED for review and notify
    # only the addresses actually confirmed to be affected.
    #
    # With nothing resolved, a settlement-wide substitute is the only coverage available at all,
    # and stays restricted to village-scale places for the reason below.
    if unresolved_settlements:
        if all_affected_hexes:
            match_quality = _worse_quality(match_quality, 'UNRESOLVED')
        else:
            for unresolved_settlement in unresolved_settlements:
                # Whole-settlement coverage is only a safe substitute for a small place (a
                # village): for an actual city it would mean notifying people many km from the
                # real outage, which is worse than not covering it. Big-settlement misses get
                # flagged UNRESOLVED for manual review instead of guessed at broadly.
                settlement_hex, radius_steps = get_settlement_info(unresolved_settlement, city_default)
                is_small_settlement = settlement_hex and (radius_steps or 0) <= SMALL_SETTLEMENT_RADIUS_THRESHOLD
                fallback_hexes = get_settlement_polygon_hexes(
                    unresolved_settlement, parent_region=city_default
                ) if is_small_settlement else set()

                if fallback_hexes:
                    _add(fallback_hexes,
                         _location_envelope(unresolved_settlement, city_default, region_ops,
                                            gate_region),
                         f"{unresolved_settlement} ({city_default})")
                    match_quality = _worse_quality(match_quality, 'FALLBACK')
                else:
                    match_quality = _worse_quality(match_quality, 'UNRESOLVED')

    return list(all_affected_hexes), match_quality


def _area_features(hexes, region: str, props: dict) -> list[dict]:
    """A whole-settlement / kvart / widened-corridor footprint drawn as the RGZ streets inside
    it, falling back to the dissolved H3 outline only where there's no street data. The blob was
    both dishonest (covered farmland, rivers, the airfield) and, in central Zrenjanin where
    several overlapped, unreadable. The streets are what a recipient actually cares about."""
    hexes = set(hexes or [])
    if not hexes:
        return []

    street_feats = get_streets_within_hexes(hexes, parent_region=region)
    if street_feats:
        for f in street_feats:
            f["properties"].update({k: v for k, v in props.items() if k != "kind"})
        return street_feats

    blob = hexes_to_geojson_feature(hexes, props)
    return [blob] if blob else []


def _affected_house_points(loc, region: str) -> list[tuple[float, float]]:
    """(lat, lon) of every RGZ house on loc's street that its numbers cover."""
    def points(settlement):
        return [(lat, lon) for number, lat, lon in
                rgz_house_points(loc.main_street, settlement=settlement, parent_region=region)
                if re.search(r"\d", number or "")
                and is_house_number_affected(number, loc.parity, loc.number_range, loc.explicit_numbers)]

    found = points(loc.settlement)
    if not found and loc.settlement:
        # A kvart label is not an RGZ naselje: "Јурија Гагарина 253, Омладинских бригада 96,
        # Пеђе Милосављевића 70а – Бежанијска коса" (BVK, 26.10.2026) put all three under
        # Bežanijska kosa, RGZ files them under Beograd, no house was found, and the map drew
        # each whole street with every block driveway that carries its name. Same opština,
        # without the label, finds them.
        found = points(None)
    return found


# --- a fault at one house (BVK's kvarovi list) and the streets around it ---------------------
# BVK names the house where a pipe broke and says "the listed AND SURROUNDING streets" are without
# water. Who is around it isn't listed, so they are told it MAY reach them: a circle of
# `radius_m` round the house, on the map and in who hears about it (26.10.2026).
NEARBY_PREFIX = "💧 Moguć prekid vode u blizini."
# The circle round a house named "и околне улице" by any other source (BVK passes its own).
NEARBY_M = 200


def _metres(lat1, lon1, lat2, lon2) -> float:
    k = math.pi / 180
    a = (math.sin((lat2 - lat1) * k / 2) ** 2
         + math.cos(lat1 * k) * math.cos(lat2 * k) * math.sin((lon2 - lon1) * k / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def _house_centre(loc, region: str) -> tuple[str, float, float] | None:
    """(address, lat, lon) of a location naming houses by number - the middle of the RGZ address
    points it covers (one number can be several entrances: 96М, 96Р, 96Ш) - or None."""
    if loc.location_type != "STREET" or not loc.main_street or not loc.explicit_numbers:
        return None
    try:
        pts = _affected_house_points(loc, region)
    except Exception as e:
        print(f"⚠️ fault house not placed for {loc.main_street}: {e!r}")
        return None
    if not pts:
        return None
    return (_location_display_name(loc, None),
            sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _widened(loc) -> bool:
    return bool(getattr(loc, 'includes_surrounding_streets', False))


def _fault_centres(alert, region: str, widened_only: bool = False) -> list[tuple[str, float, float]]:
    """(address, lat, lon) for each named house of a fault notice. widened_only: just the houses
    named with "и околне улице" ("Јурија Гагарина 253 и околне улице") - any source's notice
    whose surroundings may be cut too, drawn and told the same way as BVK's faults."""
    return [c for loc in alert.affected_locations
            if not widened_only or _widened(loc)
            for c in [_house_centre(loc, region)] if c]


def _nearby_features(centres, radius_m: int, prefix: str = "Kvar: ") -> list[dict]:
    """One point per fault, carrying its circle (scripts/build_web_data.py draws it as a ring)."""
    return [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
             "properties": {"nearby_m": radius_m, "label": f"{prefix}{label} i okolne ulice"}}
            for label, lat, lon in centres]


def _target_point(target) -> tuple[float, float] | None:
    """Where a subscriber's own house stands: its stored pin, else its RGZ address points.
    `target` is a row or a dict; a pin or place it doesn't carry is looked up."""
    if _row_get(target, 'lat') and _row_get(target, 'lng'):
        return target['lat'], target['lng']
    m = re.match(r"\d+", (target['broj'] or '').strip())
    if not m:
        return None
    pts = []
    naselje = _row_get(target, 'naselje')
    for settlement in ([naselje, None] if naselje else [None]):
        pts = [(la, lo) for number, la, lo in
               rgz_house_points(target['ulica'], settlement=settlement,
                                parent_region=_row_get(target, 'opstina') or _row_get(target, 'city'))
               if re.match(rf"{m.group()}(?!\d)", (number or '').strip())]
        if pts:
            break
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _nearby_message(label: str, metres: float, source: str, end: str | None) -> str:
    near = max(50, int(round(metres / 50.0)) * 50)
    msg = (f"{NEARBY_PREFIX} Kvar na vodovodnoj mreži u ulici {label}, oko {near} m od tvoje adrese. "
           f"{source} navodi da su bez vode navedene i okolne ulice, pa je moguće da i tvoja zgrada "
           f"ostane bez vode.")
    return msg + (f" Kvar bi trebalo da bude otklonjen do {end}." if end else "")


def _queue_nearby(cursor, outage_id, alert, centres, radius_m: int, source: str,
                  message=None) -> int:
    """Tell subscribers within radius_m of a fault house - and not already told about this outage
    for their own address - that it may reach them. One message per person; a change of hours
    later goes only to the listed addresses (they never match _target_match, so never reach
    _queue_time_change). An address whose own house can't be placed is left out: no distance,
    no "nearby". message(label, metres) words it; BVK's fault wording when not given."""
    cells = set()
    for _, lat, lon in centres:
        cells |= set(h3.grid_disk(h3.latlng_to_cell(lat, lon, 9), 2))
    told = {r[0] for r in cursor.execute(
        "SELECT DISTINCT target_id FROM message_queue WHERE outage_id = ?", (outage_id,)).fetchall()}
    placeholders = ','.join(['?'] * len(cells))
    candidates = cursor.execute(f'''
        SELECT DISTINCT t.id AS target_id, t.ulica, t.broj, t.telegram_chat_id, t.email,
                        t.email_active, t.city, t.opstina, t.naselje, t.lat, t.lng
        FROM target_hexes th JOIN targets t ON th.target_id = t.id
        WHERE th.hex_id IN ({placeholders}) AND t.is_active = 1
    ''', list(cells)).fetchall()
    windows = [loc.time_window for loc in alert.affected_locations if loc.time_window]
    times = _window_times(windows[0]) if windows else None
    end = times[-1] if times else None
    queued = 0
    for target in candidates:
        if target['target_id'] in told:
            continue
        try:
            home = _target_point(target)
        except Exception as e:
            print(f"⚠️ nearby: address #{target['target_id']} not placed: {e!r}")
            continue
        if not home:
            continue
        label, metres = min(((lbl, _metres(lat, lon, *home)) for lbl, lat, lon in centres),
                            key=lambda x: x[1])
        if metres > radius_m:
            continue
        status = _send_status(cursor, outage_id, area=False)
        if status is None:
            continue
        text = message(label, metres) if message else _nearby_message(label, metres, source, end)
        for channel in _target_channels(target):
            cursor.execute('''
                INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            ''', (outage_id, target['target_id'], channel, text, status))
        told.add(target['target_id'])
        queued += 1
    return queued


# --- "i okolne ulice" from any source -----------------------------------------------------------
# A notice naming a street "и околне улице" draws the named street solid and the side streets
# that connect to it dashed (scripts/build_web_data.py: street_of) - nobody listed them, so
# they're "maybe". People on those side streets are told it MAY reach them, like the circle round
# a named house. Before this the side streets drew as certain and their people heard nothing:
# the matcher only ever takes the named street (26.10.2026).
_POSSIBLE_TEXT = {
    # (headline, what was announced, what the reader may lose)
    "WATER": ("💧 Moguć prekid vode u blizini.", "Najavljen je prekid vode", "bez vode"),
    "POWER": ("⚡ Moguć nestanak struje u blizini.", "Najavljeno je isključenje struje", "bez struje"),
    "HEATING": ("🔥 Moguć prekid grejanja u blizini.", "Najavljen je prekid grejanja", "bez grejanja"),
}


def _possible_message(alert, loc, category: str | None, why: str) -> str:
    """'Moguć prekid vode u blizini': the named place, the day and hours, and `why` - how the
    reader's address relates to it ("Tvoja ulica X je jedna od okolnih", "To je oko 150 m od
    tvoje adrese")."""
    head, announced, lose = _POSSIBLE_TEXT.get((category or "").upper(), _POSSIBLE_TEXT["WATER"])
    parts = [f"{announced} u ulici {_location_display_name(loc, None)} i okolnim ulicama"]
    when = (alert.outage_date or '').strip()
    if when and when.upper() != 'N/A':
        parts.append(f"dana {when}")
    window = (loc.time_window or '').strip()
    if window and window.upper() != 'N/A':
        parts.append(f"u periodu {window}")
    return f"{head} {', '.join(parts)}. {why}, pa je moguće da i ti ostaneš {lose}."


def _ring_message(alert, category: str | None):
    """message(label, metres) for _queue_nearby: a house named "и околне улице" by any source."""
    by_label = {_location_display_name(loc, None): loc for loc in alert.affected_locations
                if _widened(loc) and loc.main_street}

    def message(label: str, metres: float) -> str:
        near = max(50, int(round(metres / 50.0)) * 50)
        return _possible_message(alert, by_label[label], category, f"To je oko {near} m od tvoje adrese")
    return message


def _side_street_locations(alert, region: str) -> list:
    """The widened locations drawn as a street plus its side streets: a named street, or a named
    house that can't be placed (no ring without a centre)."""
    return [loc for loc in alert.affected_locations
            if _widened(loc) and loc.main_street and not _house_centre(loc, region)]


def _queue_side_streets(cursor, outage_id, alert, region: str, category: str | None) -> int:
    """Tell subscribers on the side streets of an "и околне улице" notice that it may reach them:
    a street that connects to the named one (_surrounding_streets), at an address inside the
    stretch drawn on the map and inside the outage's own (leak-gated) hexes. Not someone already
    told about this outage, nor anyone on the named street - _target_match decides those. One
    message per person, like _queue_nearby."""
    told = {r[0] for r in cursor.execute(
        "SELECT DISTINCT target_id FROM message_queue WHERE outage_id = ?", (outage_id,)).fetchall()}
    queued = 0
    for loc in _side_street_locations(alert, region):
        settlement = loc.settlement or region
        zone, feats = _surrounding_streets(clean_street(loc.main_street), settlement, region,
                                           loc.intersection_start, loc.intersection_end)
        sides = [f["properties"].get("street") for f in feats if f["properties"].get("street")]
        if not zone or not sides:
            continue
        placeholders = ','.join(['?'] * len(zone))
        candidates = cursor.execute(f'''
            SELECT DISTINCT t.id AS target_id, t.ulica, t.broj, t.telegram_chat_id, t.email,
                            t.email_active, t.city, t.opstina, t.naselje
            FROM target_hexes th JOIN targets t ON th.target_id = t.id
            JOIN outage_hexes oh ON oh.hex_id = th.hex_id AND oh.outage_id = ?
            WHERE th.hex_id IN ({placeholders}) AND t.is_active = 1
        ''', [outage_id, *zone]).fetchall()
        for target in candidates:
            if target['target_id'] in told or _streets_match(loc.main_street, target['ulica']):
                continue
            side = next((s for s in sides if _streets_match(s, target['ulica'])), None)
            if side is None:
                continue
            naselje = _row_get(target, 'naselje') or _row_get(target, 'city')
            if alert.excluded_locations and any(
                _location_matches_target(excl, target['ulica'], target['broj'], naselje, (),
                                         exclusion=True)
                for excl in alert.excluded_locations
            ):
                continue
            status = _send_status(cursor, outage_id, area=False)
            if status is None:
                continue
            why = f"Tvoja ulica {cyrillic_to_latin(side).title()} je jedna od okolnih"
            text = _possible_message(alert, loc, category, why)
            for channel in _target_channels(target):
                cursor.execute('''
                    INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                ''', (outage_id, target['target_id'], channel, text, status))
            told.add(target['target_id'])
            queued += 1
    return queued


# --- a block inside a ring of streets ------------------------------------------------------------
# "део рејона оивичен улицама Пеке Павловић, Заплањском и Браће Јерковић" is the block those
# streets enclose (core/block_area.py). Read as streets, all three were alerted end to end - people
# 2 km away - while the houses inside, on streets never named, heard nothing (#6458, 31.10.2026).
# Now: inside the block is told, a house just across a boundary street hears it may reach them,
# the rest of those streets nothing. When the streets don't close round a block, nobody is told.
# The streets are marked in the location itself, so the stored parsed_json carries it to the
# backfill, the history and the map, none of which have the notice's text.
BLOCK_MARKER = "БЛОК ОИВИЧЕН УЛИЦАМА"
_BLOCK_RING = re.compile(r"(?:оивичен|oivičen|oivicen)\w*", re.IGNORECASE)


def _mark_block_ring(alert, raw_text: str) -> None:
    """Marks the streets of a notice that is one block "оивичен" by them: one such phrase, three
    or more streets and nothing else, no house numbers."""
    if len(_BLOCK_RING.findall(raw_text or "")) != 1:
        return
    locs = alert.affected_locations
    # "оивичен ауто-путем од Аутокоманде до Душановачке пијаце" came back with the road as the
    # start bound and no street of its own (#6459): the road is the edge. A street "од X до Y"
    # may come back as a VECTOR.
    names = [loc.main_street or loc.intersection_start for loc in locs]
    if len(locs) < 3 or any(loc.location_type not in ('STREET', 'VECTOR') or not name or loc.number_range
                            or loc.explicit_numbers for loc, name in zip(locs, names)):
        return
    if len({_spelling_key(n) for n in names}) < 3:
        return
    for loc, name in zip(locs, names):
        loc.main_street = name
        loc.landmark = BLOCK_MARKER


def _in_block_ring(loc) -> bool:
    return (loc.landmark or '').strip() == BLOCK_MARKER


def _block_ring(locations) -> list:
    ring = [loc for loc in locations if _in_block_ring(loc) and loc.main_street]
    return ring if len(ring) >= 3 else []


def _block_shape(ring, region: str):
    settlement = next((loc.settlement for loc in ring if loc.settlement), None) or region
    try:
        return block_area.block_polygon(tuple(clean_street(loc.main_street) for loc in ring),
                                         settlement, region)
    except Exception as e:
        print(f"⚠️ block shape failed: {e!r}")
        return None


def _block_where(alert, ring, target) -> str | None:
    """'inside', 'border' or None for one subscriber's house; None when it can't be placed or
    the streets make no block."""
    shape = _block_shape(ring, alert.city or 'НОВИ САД')
    if not shape:
        return None
    try:
        home = _target_point(target)
    except Exception as e:
        print(f"⚠️ block: address #{_row_get(target, 'target_id')} not placed: {e!r}")
        return None
    return block_area.where(shape, *home) if home else None


def _block_names(ring) -> str:
    names = [cyrillic_to_latin(loc.main_street).title() for loc in ring]
    return ", ".join(names[:-1]) + " i " + names[-1]


def _block_message(alert, ring, category: str | None, border_street: str | None = None) -> str:
    """The alert for a house inside the block, or - with `border_street` - the "moguć" one for a
    house across the road from it. Built from facts: the model's template says "u ulici ***"."""
    cat = (category or "").upper()
    where = f"u bloku između ulica {_block_names(ring)}"
    parts = []
    span = _date_span(alert) if _hours_unknown(alert) else None
    when = (alert.outage_date or '').strip()
    if span:
        parts.append(span)
    elif when and when.upper() != 'N/A':
        parts.append(f"dana {when}")
    window = (ring[0].time_window or '').strip()
    if window and window.upper() != 'N/A':
        parts.append(f"u periodu {window}")
    why = " zbog ispiranja vodovodne mreže" if "ispiranj" in cyrillic_to_latin(
        alert.message_template or '').lower() else ""
    if border_street is None:
        base = _CATEGORY_FALLBACK.get(cat, "Planirano isključenje")
        return f"{base} {where}{why}, {', '.join(parts)}." if parts else f"{base} {where}{why}."
    head, announced, lose = _POSSIBLE_TEXT.get(cat, _POSSIBLE_TEXT["WATER"])
    told = f"{announced} {where}{why}" + (f", {', '.join(parts)}" if parts else "")
    street = cyrillic_to_latin(border_street).title()
    return (f"{head} {told}. Tvoja adresa je u ulici {street}, preko puta tog bloka, "
            f"pa je moguće da i ti ostaneš {lose}.")


def _queue_block_border(cursor, outage_id, alert, region: str, category: str | None) -> int:
    """Tell subscribers on a boundary street, across the road from the block, that it may reach
    them (owner's call, 01.11.2026: their pipe may run under the same street). Not someone already
    told about this outage; one message per person, like _queue_side_streets."""
    ring = _block_ring(alert.affected_locations)
    if not ring or not _block_shape(ring, region):
        return 0
    told = {r[0] for r in cursor.execute(
        "SELECT DISTINCT target_id FROM message_queue WHERE outage_id = ?", (outage_id,)).fetchall()}
    candidates = cursor.execute('''
        SELECT DISTINCT t.id AS target_id, t.ulica, t.broj, t.telegram_chat_id, t.email,
                        t.email_active, t.city, t.opstina, t.naselje, t.lat, t.lng
        FROM target_hexes th JOIN targets t ON th.target_id = t.id
        JOIN outage_hexes oh ON oh.hex_id = th.hex_id AND oh.outage_id = ?
        WHERE t.is_active = 1
    ''', (outage_id,)).fetchall()
    want_region = _region_variants(region)
    queued = 0
    for target in candidates:
        if target['target_id'] in told:
            continue
        if not want_region & _region_token_set(target['city'], target['opstina'], target['naselje']):
            continue
        edge = next((loc for loc in ring if _streets_match(loc.main_street, target['ulica'])), None)
        if edge is None or _block_where(alert, ring, target) != 'border':
            continue
        naselje = _row_get(target, 'naselje') or _row_get(target, 'city')
        if alert.excluded_locations and any(
            _location_matches_target(excl, target['ulica'], target['broj'], naselje, (),
                                     exclusion=True)
            for excl in alert.excluded_locations
        ):
            continue
        status = _send_status(cursor, outage_id, area=False)
        if status is None:
            continue
        text = _block_message(alert, ring, category, border_street=edge.main_street)
        for channel in _target_channels(target):
            cursor.execute('''
                INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            ''', (outage_id, target['target_id'], channel, text, status))
        told.add(target['target_id'])
        queued += 1
    return queued


# --- a stretch "од X до Y" that can't be cut ------------------------------------------------------
def _stretch_stand_in(location, alert, main_street: str, settlement: str, region: str):
    """What stands in for a stretch whose cross streets can't cut the street: ('corner', cells)
    round a named corner the street does reach; ('sibling', ∅) when another line of the notice
    cuts the same street fine - this one is noise next to it; else ('whole', ∅), the whole street
    as before. "Крунска (од Молерове до Кнегиње Зорке и од Кичевске до Крунске)" (#6449,
    30.10.2026): the second stretch ends at the street itself, so it fell back to all of Krunska
    and alerted Krunska 6, a kilometre from the works."""
    cells = set()
    for cross in (location.intersection_start, location.intersection_end):
        if cross and not _streets_match(cross, location.main_street):
            try:
                cells |= block_area.corner_cells(main_street, clean_street(cross), settlement, region)
            except Exception as e:
                print(f"⚠️ corner {main_street} x {cross} failed: {e!r}")
    if cells:
        return 'corner', cells
    for other in alert.affected_locations:
        if other is location or not other.main_street or _in_block_ring(other):
            continue
        if not (other.intersection_start or other.intersection_end):
            continue
        if not _streets_match(other.main_street, location.main_street):
            continue
        if get_street_hexes(clean_street(other.main_street), settlement=other.settlement or region,
                            parent_region=region, intersection_start=other.intersection_start,
                            intersection_end=other.intersection_end):
            return 'sibling', set()
    return 'whole', set()


# A house this far from its stretch's centre line is clear of it: house points sit 10-40 m off
# the carriageway, and a corner house belongs to the cross street too.
STRETCH_M = 60


@functools.lru_cache(maxsize=512)
def _clipped_segments(street: str, settlement: str, region: str, start: str | None,
                      end: str | None) -> tuple:
    segs = get_local_street_segments(street, settlement, region)
    if not segs:
        return ()
    return tuple(clip_polyline_by_intersections(segs, start, end, settlement, region,
                                                main_street_name=street) or ())


def _outside_stretch(alert, loc, target) -> bool:
    """True when `loc` is a stretch "од X до Y" and this subscriber's house stands clear of it.
    The name match alone took the whole street: the hexes only chose who was looked at, and any
    other line of the notice reaching the house's cell let the rest of the street in. Krunska 6
    was alerted for works from Molerova on (#6449, 30.10.2026) because Svetozara Markovića 14,
    in the same notice, crosses Krunska right by it. A house that can't be placed, and a stretch
    that stands in for the whole street, keep the name match."""
    if not loc.main_street or not (loc.intersection_start or loc.intersection_end):
        return False
    region = alert.city or 'НОВИ САД'
    settlement = loc.settlement or region
    street = clean_street(loc.main_street)
    if not street:
        return False
    try:
        segs = _clipped_segments(street, settlement, region, loc.intersection_start,
                                 loc.intersection_end)
        corners = []
        if not segs:
            kind, _ = _stretch_stand_in(loc, alert, street, settlement, region)
            if kind == 'sibling':
                return True                 # the other line of the same street decides
            if kind == 'whole':
                return False
            corners = [c for c in (block_area.corner_point(street, clean_street(x), settlement, region)
                                   for x in (loc.intersection_start, loc.intersection_end)
                                   if x and not _streets_match(x, loc.main_street)) if c]
        home = _target_point(target)
        if not home:
            return False
        if segs:
            d = block_area.distance_m(segs, *home)
            return d is not None and d > STRETCH_M
        return all(block_area.point_distance_m(c, home) > block_area.CORNER_NEAR_M for c in corners)
    except Exception as e:
        print(f"⚠️ stretch check for {loc.main_street} skipped: {e!r}")
        return False


def _house_number_feature(feat: dict, loc, region: str) -> dict:
    """A street whose announcement names house numbers draws only where those houses stand: the
    stretch from the first to the last, or a pin when it's one house (see
    spatial_engine.house_number_stretch), placed with RGZ's address points. The map only - who
    gets alerted is decided on the whole street's hexes plus is_house_number_affected, so a
    wrong or missing address point can never cost an alert. The whole street stays whenever the
    houses can't be placed: no numbers, an OSM-drawn street, or numbers RGZ doesn't have. A
    house without a number ("bb") is never placed."""
    if not feat or not (loc.number_range or loc.explicit_numbers):
        return feat
    try:
        return house_number_stretch(feat, _affected_house_points(loc, region)) or feat
    except Exception as e:
        print(f"⚠️ house-number drawing skipped for {loc.main_street}: {e!r}")
        return feat


def _build_visual_features(alert: OutageAlert, region: str,
                           gate_region: str | None = None) -> list[dict]:
    """Map geometry for one outage. Streets draw as lines; a whole-settlement or "surrounding
    streets" outage has no single polyline, so it draws as the streets inside its H3 coverage
    (see _area_features). Before this, an ENTIRE_SETTLEMENT announcement stored no geometry at
    all and rendered as nothing - the whole Зелено Поље class of outage was invisible."""
    features = []
    region_ops = region_opstine(region)
    # A block inside a ring of streets draws as the block, filled; its streets not at all. One
    # whose streets make no block draws nothing - nobody was told (calculate_outage_hexes).
    ring = _block_ring(alert.affected_locations)
    if ring:
        shape = _block_shape(ring, region)
        if shape:
            env = _location_envelope(ring[0].settlement, region, region_ops, gate_region)
            feat = _gate_feature(block_area.block_feature(shape, f"Blok {_block_names(ring)}"),
                                 env, f"block ({region}, map)")
            if feat:
                features.append(feat)
    for loc in alert.affected_locations:
        if ring and _in_block_ring(loc):
            continue
        settlement = loc.settlement or region
        widen = getattr(loc, 'includes_surrounding_streets', False)
        # Same leak gate as the matcher (see _gate_hexes): the map must never draw what the
        # matcher refused to notify.
        loc_env = _location_envelope(loc.settlement, region, region_ops, gate_region)
        loc_label = f"{loc.main_street or settlement} ({region}, map)"

        if loc.main_street:
            feat = get_street_geojson_feature(
                loc.main_street,
                settlement=loc.settlement,
                parent_region=region,
                intersection_start=loc.intersection_start,
                intersection_end=loc.intersection_end
            )
            if not feat and (loc.intersection_start or loc.intersection_end):
                # Mirror the matcher's TIER 2 fallback: when clipping a street to its announced
                # cross-street bounds yields nothing, the matcher still notifies on the whole
                # street. Without the same fallback here the outage had hexes but no geometry and
                # drew as nothing (confirmed: Жарка Зрењанина, 7 hexes, empty feature list).
                # Same stand-in as the matcher: the corner it names, or nothing next to a
                # sibling line of the same street (_stretch_stand_in).
                kind, corner = _stretch_stand_in(loc, alert, clean_street(loc.main_street),
                                                 settlement, region)
                if kind == 'corner':
                    segs = _clip_segments_to_cells(
                        get_local_street_segments(clean_street(loc.main_street), settlement, region)
                        or [], corner, 9)
                    if segs:
                        feat = {"type": "Feature",
                                "geometry": {"type": "MultiLineString", "coordinates": segs},
                                "properties": {"street": cyrillic_to_latin(
                                    clean_street(loc.main_street)).upper(), "settlement": settlement}}
                elif kind == 'whole':
                    feat = get_street_geojson_feature(
                        loc.main_street, settlement=loc.settlement, parent_region=region
                    )
            if widen and _house_centre(loc, region):
                # A house named "и околне улице" (Јурија Гагарина 253): its pin, and the circle
                # round it the caller adds (_fault_centres). Its whole street and every street
                # touching it drew before - 20 streets for one house.
                widen = False
            if not widen:
                # A widened location's side streets are drawn around the whole named street.
                feat = _house_number_feature(feat, loc, region)
            feat = _gate_feature(feat, loc_env, loc_label)
            if feat:
                feat.setdefault("properties", {})["kind"] = "street"
                features.append(feat)

            if widen:
                # Draw the connecting side streets too, so the map shows the same footprint the
                # matcher notifies on rather than just the one street that was named.
                _, side_feats = _surrounding_streets(
                    clean_street(loc.main_street), settlement, region,
                    loc.intersection_start, loc.intersection_end
                )
                for f in side_feats:
                    f = _gate_feature(f, loc_env, loc_label)
                    if not f:
                        continue
                    f["properties"].update({
                        "street_of": cyrillic_to_latin(loc.main_street).title(),
                        "settlement": cyrillic_to_latin(settlement).title(),
                    })
                    features.append(f)

        elif loc.location_type == "ENTIRE_SETTLEMENT":
            if not (loc.settlement or "").strip():
                # No name of its own: calculate_outage_hexes skips it as a parse artifact, and
                # drawn as the announcement's city it became all of Beograd at one point - a dot
                # in the river at Ušće (#3347, "celo grejno područje toplane Cerak").
                continue
            polygon = get_settlement_polygon_hexes(settlement, parent_region=region)
            if _whole_settlement_not_meant(loc, region, polygon):
                # Only part of the place: a dot where it is, never its whole outline (#4865).
                centre, _r = get_settlement_info(settlement, region)
                dot = hexes_to_geojson_feature(
                    _gate_hexes(set(h3.grid_disk(centre, 1)) if centre else set(), loc_env, loc_label),
                    {"kind": "area", "area_type": "spot",
                     "settlement": cyrillic_to_latin(settlement).title()})
                if dot:
                    features.append(dot)
                continue
            features.extend(_area_features(
                _gate_hexes(polygon, loc_env, loc_label), region,
                {"kind": "area", "area_type": "settlement",
                 "settlement": cyrillic_to_latin(settlement).title()}
            ))

    return features


def _spelling_key(name: str | None) -> str:
    """A street name as a re-typed notice keeps it: Latin, no diacritics, letters and digits only
    ("Бул. Франше д'Епереа" and "Bul. Franse d Eperea" are one key)."""
    return re.sub(r"[^0-9A-Z]", "", _fold_name(cyrillic_to_latin(name or "")))


def _edit_distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def _respelled(a: str, b: str, region: str) -> bool:
    """One street written two ways in two versions of a notice - BVK typed Bulevar Franše
    d'Eperea as "Д Епереа" and then "Д Епереја", and the same failure went on the map twice
    (#2652/#2626). Equal once folded, or a letter apart per 10 letters (names of 10+ letters
    only) - unless RGZ knows both spellings as streets of this region: then they are two streets."""
    ka, kb = _spelling_key(a), _spelling_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    shorter = min(len(ka), len(kb))
    if shorter < 10 or _edit_distance(ka, kb) > shorter // 10:
        return False
    both_real = all(query_rgz_streets(n, naselje=region, opstina=region, exact_only=True) for n in (a, b))
    return not both_real


def _each_has_a_partner(names: list[str], others: list[str], region: str) -> bool:
    """Every name in `names` pairs with its own name in `others` - the same street, or the same
    street re-spelled (_respelled). Exact spellings are paired first, so a near-spelling never
    takes a partner another name needed exactly."""
    left = list(others)
    keys = [_spelling_key(o) for o in left]
    pending = []
    for name in names:
        key = _spelling_key(name)
        if key and key in keys:
            i = keys.index(key)
            del left[i], keys[i]
        else:
            pending.append(name)
    for name in pending:
        for i, other in enumerate(left):
            if _respelled(name, other, region):
                del left[i], keys[i]
                break
        else:
            return False
    return True


# "Одложени радови", "радови су одложени до даљег", "radovi se odlažu", "otkazani", "stornira
# se". Not "неодложне" (urgent), and not the conditional boilerplate of ordinary notices ("u
# slučaju loših vremenskih uslova radovi se odlažu") - see _cancellation_kind.
_CANCEL_WORDS = re.compile(
    r"(?<![нН][еЕ])одлож[еи]н|(?<![нН][еЕ])одлаж|(?<![nN][eE])odlo[žz][ei]n|(?<![nN][eE])odla[žz]"
    r"|(?P<cancel>отказан|otkazan|сторнира|stornira|неће се изводити|neće se izvoditi)",
    re.IGNORECASE,
)
_CONDITIONAL = re.compile(r"у случају|u slu[čc]aju|уколико|ukoliko|\bако\b|\bako\b|евентуалн|eventualn",
                          re.IGNORECASE)


_CANCEL_LEAD = 300


def _cancellation_kind(raw_text: str | None) -> str | None:
    """'odloženo' or 'otkazano' when a notice says announced works won't go ahead, else None.
    Only in its title and lead (the first _CANCEL_LEAD characters - "27.8.2026. Одложени
    планирани радови ..."): a longer text that mentions some other postponement further down
    must not call off the outage it announces. A match with "u slučaju / ukoliko / ako" in the
    80 characters before it is a condition, not news, and doesn't count."""
    text = (raw_text or "")[:_CANCEL_LEAD]
    for m in _CANCEL_WORDS.finditer(text):
        if _CONDITIONAL.search(text[max(0, m.start() - 80):m.start()]):
            continue
        return "otkazano" if m.group("cancel") else "odloženo"
    return None


def _cancel_message(kind: str, told: str) -> str:
    told = (told or "").removeprefix(CHANGED_PREFIX).strip()
    if kind == "odloženo":
        return ("✅ Radovi su odloženi. Isključenje o kom smo javili neće biti u najavljenom "
                f"terminu: „{told}“ Novi termin javljamo čim bude objavljen.")
    return f"✅ Radovi su otkazani. Isključenje o kom smo javili neće biti: „{told}“"


def _cancel_announced_outage(cursor, raw_text, text_hash, region, category, names, parsed_date,
                             time_window, source_url, event_type, parsed_json) -> bool:
    """A notice saying announced works are postponed or cancelled (_cancellation_kind), matched
    to the ACTIVE outage it is about: same place, utility and day, the same hours when both give
    them, and at least one of its streets when it names any (BVK's "одложени радови" lists the
    streets that WILL have water, 27.08.2026, Zvezdara). That outage becomes CANCELLED - off the
    map, out of the history - its unsent messages are dropped, and everyone already told gets a
    message quoting what they were told. The notice is stored as a CANCELLATION row so it isn't
    parsed again. Returns False, changing nothing, unless exactly one outage matches: then the
    notice goes through as an ordinary one, as it always did, and the operator hears about it."""
    kind = _cancellation_kind(raw_text)
    if not kind or not parsed_date:
        return False
    want_times = _window_times(time_window)
    real_names = [n for n in names if not AREA_LABEL.search(n)]
    if not real_names and not want_times:
        send_operator_note(f"Notice reads as {kind} but names neither streets nor hours - kept as "
                           f"an ordinary notice: {region}, {parsed_date:%d.%m.%Y}")
        return False
    cursor.execute('''
        SELECT id, region, affected_streets, outage_date, time_window, expires_at, raw_text, match_quality
        FROM outages
        WHERE category IS ? AND status = 'ACTIVE'
          AND (expires_at IS NULL OR expires_at > datetime('now'))
          AND created_at > datetime('now', '-14 days')
        ORDER BY id DESC
    ''', (category,))
    candidates = []
    for row in cursor.fetchall():
        if not _same_region(row['region'], region):
            continue
        if _parse_date(row['outage_date']) != parsed_date or _cancellation_kind(row['raw_text']):
            continue            # another day, or the stored notice already says it: boilerplate
        have_times = _window_times(row['time_window'])
        if want_times and have_times and want_times != have_times:
            continue
        have = [s.strip() for s in (row['affected_streets'] or '').split(',') if s.strip()]
        if real_names and not any(_each_has_a_partner([n], have, region) for n in real_names):
            continue
        candidates.append(row)
    if len(candidates) != 1:
        send_operator_note(
            f"Notice reads as {kind} but matches {len(candidates)} stored outages "
            f"{[c['id'] for c in candidates]} - kept as an ordinary notice: {region}, "
            f"{parsed_date:%d.%m.%Y}, {', '.join(names) or 'no streets'}")
        return False

    old = candidates[0]
    cursor.execute("UPDATE outages SET status = 'CANCELLED' WHERE id = ?", (old['id'],))
    cursor.execute("UPDATE message_queue SET status = 'SKIPPED' WHERE outage_id = ? AND status = 'QUEUED'",
                   (old['id'],))
    cursor.execute('''
        INSERT INTO outages (region, affected_streets, outage_date, time_window, raw_text, raw_text_hash,
                             status, match_quality, event_type, category, source_url, expires_at, parsed_json)
        VALUES (?, ?, ?, ?, ?, ?, 'CANCELLATION', ?, ?, ?, ?, ?, ?)
    ''', (region, old['affected_streets'], old['outage_date'], old['time_window'], raw_text, text_hash,
          old['match_quality'], event_type, category, source_url, old['expires_at'], parsed_json))
    notice_id = cursor.lastrowid
    cursor.execute('''
        SELECT target_id, channel, custom_message FROM message_queue
        WHERE outage_id = ? AND status IN ('SENT', 'SENDING') ORDER BY id
    ''', (old['id'],))
    told = {(r['target_id'], r['channel']): r['custom_message'] for r in cursor.fetchall()}
    for (target_id, channel), message in told.items():
        cursor.execute('''
            INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at)
            VALUES (?, ?, ?, ?, 'QUEUED', datetime('now'))
        ''', (notice_id, target_id, channel, _cancel_message(kind, message)))
    print(f"🚫 Outage #{old['id']} {kind} (notice #{notice_id}); telling {len(told)} already notified.")
    send_operator_note(f"Outage #{old['id']} {kind} by notice #{notice_id} ({region}, {old['outage_date']}): "
                       f"off the map, {len(told)} message(s) queued to people already told.")
    return True


def _window_times(time_window: str | None) -> tuple[str, ...]:
    """The clock times in a window, normalised ("8.00 - 14.00" -> ("08:00", "14:00"))."""
    return tuple(f"{int(h):02d}:{m}" for h, m in re.findall(r"(\d{1,2})[:.](\d{2})", time_window or ""))


def _same_notice_respelled(existing_streets: str | None, names: list[str], region: str,
                           same_window: bool = False) -> bool:
    """A stored outage's streets are the new version's: the same streets in any order, each
    allowing for spelling (_respelled) - a single differently spelled name is not enough on its
    own. Order doesn't matter: a republished notice that listed the same streets in another
    order, or put another one first, went on the map and to people a second time. With the same
    hours (`same_window`), a version that only adds or drops streets is the same notice too;
    at other hours it may be the same area's next shift, so it stays separate."""
    have = [s.strip() for s in (existing_streets or "").split(",") if s.strip()]
    names = [n.strip() for n in names if n and n.strip()]
    if not have or not names:
        return False
    if len(have) == len(names):
        return _each_has_a_partner(names, have, region)
    if not same_window:
        return False
    fewer, more = (have, names) if len(have) < len(names) else (names, have)
    return _each_has_a_partner(fewer, more, region)


def _continues_stored_run(row, row_start, new_start, new_last, new_window) -> bool:
    """A new copy of a multi-day notice the stored outage already holds. Beogradske elektrane
    re-post "Izveštaj o trenutnom prekidu" every day while works go on: Toplana Cerak's remont to
    01.10. came in as #3347 (29.09.) and #3465 (30.09.) - two map cards, and each day's copy would
    have mailed the same people again. The same event when the stored outage runs over several
    days, the new copy starts inside them and ends on the same last day, at the same hours. A
    one-day notice that falls inside works (a fault on the same street, with its own hours) does
    not qualify, so it still goes out."""
    try:
        stored = json.loads(row['parsed_json'] or '{}')
    except ValueError:
        return False
    row_last = _parse_date(stored.get('end_date') or '')
    if not (row_start and row_last and new_start and new_last) or row_last <= row_start:
        return False
    return (row_start <= new_start <= row_last and new_last == row_last
            and _window_times(row['time_window']) == _window_times(new_window))


# A clock time (08:00, 8.30) - not the "20.10" of a date "20.10.2026.".
_CLOCK = re.compile(r"(?<![\d.])\d{1,2}[:.]\d{2}(?!\.?\d)")


def _clock(t: str) -> str:
    return t.replace('.', ':').zfill(5)


def _split_by_time_window(alert: OutageAlert, raw_text: str) -> list[OutageAlert]:
    """One notice that lists separate events at different times becomes one alert per time
    window. 20.10.2026, ViK Novi Sad: "Kovilj low pressure 9-10.30" and "Slavka Rodića no water
    8-12" in one post were stored as one outage, drawn and clicked as one, and both carried the
    first location's 09:00 - 10:30 (11 such outages in 30 days, from vodovodks, vikns and
    vodovodle). The model already gives each location its own time_window; this keeps them apart.

    Locations with no window of their own go with the first window. A part whose model-written
    message_template states hours that aren't its own drops the template, so the message is built
    from that part's facts (_render_message) instead of quoting the other event's hours."""
    groups: dict[str, list] = {}
    for loc in alert.affected_locations:
        w = (_normalize_time_window(loc.time_window, raw_text) or '').strip() if loc.time_window else ''
        groups.setdefault('' if w.upper() in ('', 'N/A') else w, []).append(loc)
    timed = [k for k in groups if k]
    if len(timed) < 2:
        return [alert]
    untimed = groups.get('', [])
    parts = []
    for i, key in enumerate(timed):
        part = alert.model_copy(deep=True)
        part.affected_locations = [loc.model_copy(deep=True)
                                   for loc in groups[key] + (untimed if i == 0 else [])]
        stated = {_clock(t) for t in _CLOCK.findall(part.message_template or '')}
        if stated and not stated <= {_clock(t) for t in _CLOCK.findall(key)}:
            part.message_template = None
        parts.append(part)
    return parts


def process_and_queue_outage(parsed_data: OutageAlert | dict, raw_text: str, text_hash: str = None, category: str = None, source_url: str = None,
                             gate_region: str | None = None, live_hashes: set[str] | None = None,
                             nearby: tuple[int, str] | None = None):
    """Stores one notice - as several outages when it lists separate events at different times
    (_split_by_time_window). Each part gets its own text hash (the notice's, then "<hash>#2",
    "#3" - raw_text_hash is UNIQUE), and all parts are live siblings of each other, so the
    dedup never merges one into another. Arguments as _process_one_outage."""
    alert = _normalize_alert(parsed_data)
    if not alert:
        return
    parts = _split_by_time_window(alert, raw_text)
    if len(parts) == 1:
        _process_one_outage(alert, raw_text, text_hash, category, source_url, gate_region, live_hashes,
                            nearby)
        return
    print(f"   ↳ one notice, {len(parts)} separate times - stored as {len(parts)} outages")
    hashes = ([text_hash] + [f"{text_hash}#{i}" for i in range(2, len(parts) + 1)]
              if text_hash else [None] * len(parts))
    siblings = set(live_hashes or ()) | {h for h in hashes if h}
    for part, part_hash in zip(parts, hashes):
        _process_one_outage(part, raw_text, part_hash, category, source_url, gate_region, siblings,
                            nearby)


def _process_one_outage(parsed_data: OutageAlert | dict, raw_text: str, text_hash: str = None, category: str = None, source_url: str = None,
                        gate_region: str | None = None, live_hashes: set[str] | None = None,
                        nearby: tuple[int, str] | None = None):
    """nearby: (radius_m, source name) for a notice of faults at named houses whose surroundings
    may be cut too (BVK's kvarovi) - see _queue_nearby.

    category: WATER/POWER/HEATING, provided by the calling scraper when it knows its own
    utility deterministically (e.g. bvk_scraper is always water) rather than left for
    event_type's free text to be guessed - see the note on the category column in
    core/database.py for the real bug this replaces.

    live_hashes: the text hashes of every notice still on the source right now (EPS passes its
    rows). A stored outage whose text is among them is a sibling still being published, never an
    older version of this one, so the dedup below leaves it alone."""
    alert = _normalize_alert(parsed_data)
    if not alert:
        return
    _leak_gate_events.clear()
    _name_corrections.clear()

    alert.affected_locations = _inherit_settlement_context(_dedup_locations(alert.affected_locations),
                                                           raw_text)
    if nearby:
        # A list of faults names houses: a place there says where the house is ("Милоша
        # Глигоријевића бб (код школе) – Велики Мокри Луг"), never that the place is cut. Read
        # as the whole place it alerted all of Veliki Mokri Lug for one broken pipe at a school
        # (#6514, 31.10.2026). A spot alerts nobody; the fault's own house still gets its ring.
        for _loc in alert.affected_locations:
            if _loc.location_type == 'ENTIRE_SETTLEMENT' and not _loc.main_street:
                _loc.scope = 'SPOT'
    else:
        # A fault list's "Улица бб" is where the fault is, and gets its ring (_queue_nearby);
        # anywhere else it is one building, not the street.
        _drop_bb_streets(alert, raw_text)
    for line in _place_streets_by_their_group(alert):
        print(f"   ↳ placed with the rest of the notice: {line}")
    unplaced = _place_landmarks(alert, category)
    if unplaced:
        send_operator_note(
            "A power notice for single buildings, not found on the map - not drawn, nobody "
            f"alerted:\n{alert.city or 'N/A'}, {alert.outage_date or 'N/A'}\n" + "\n".join(unplaced))

    # _normalize_time_window was only ever applied to the one alert-level time_window used for
    # expires_at (see below) - never to each location's OWN time_window, which is what
    # _render_message actually appends to the message per recipient. Confirmed real case (outage
    # #1909, Preševska): the model wrote "07:00 - 22:00" straight into its own free-text
    # message_template (correctly not defaulting a missing start to midnight, per parser.py rule
    # 8) but left every location's structured time_window as the raw "00:00 - 22:00" it was
    # never told to fix - so the recipient's email read "...u periodu 07:00 - 22:00, u periodu
    # 00:00 - 22:00.": the same window, appended twice, because _append_time_window's own
    # duplicate check only catches an EXACT re-appearance of the numbers being appended, not the
    # same window under an unnormalized start. Normalizing here, once, up front, means every
    # downstream reader of a location's time_window (message rendering, expires_at, the map) sees
    # the same already-safe value instead of each needing its own copy of this backstop.
    for _loc in alert.affected_locations:
        _loc.time_window = _normalize_time_window(_loc.time_window, raw_text)

    works = category == 'HEATING' and heat_works_only(raw_text)
    _drop_invented_windows(alert, raw_text, works)
    _drop_invented_surroundings(alert, raw_text)

    hexes, match_quality = calculate_outage_hexes(alert, raw_text, gate_region)

    # Retry with a stronger model, but ONLY when the first attempt resolved nothing at all -
    # confirmed real case: "Церова" came back as "ЦРОВА" (one dropped letter) and that single
    # street's outage had two OTHER streets that resolved fine (7 real hexes) alongside it. A
    # blind re-parse-and-replace there risks losing those two correct streets to a different,
    # not-necessarily-better sample on retry - Gemini's non-determinism cuts both ways. With
    # nothing resolved there's nothing to lose, so a second attempt on a stronger tier is pure
    # upside; a partial failure instead stays UNRESOLVED for scripts/list_flagged_outages.py.
    if match_quality == 'UNRESOLVED' and not hexes:
        retry_budget = (f"{config.GEMINI_FALLBACK_MODEL_NAME}:retry", config.GEMINI_FALLBACK_RETRY_SHARE)
        if remaining(*retry_budget) > 0:
            # Every request counts against the budget (core/api_budget.py), so 2 tries, not 4.
            retry_alert = parse_announcement(raw_text, max_retries=2,
                                             model=config.GEMINI_FALLBACK_MODEL_NAME, budget=retry_budget)
            if retry_alert:
                if not nearby:
                    _drop_bb_streets(retry_alert, raw_text)
                _place_landmarks(retry_alert, category)
                _drop_invented_surroundings(retry_alert, raw_text)
                retry_hexes, retry_quality = calculate_outage_hexes(retry_alert, raw_text, gate_region)
                if retry_hexes:
                    print(f"   ↳ retry with {config.GEMINI_FALLBACK_MODEL_NAME} resolved what {config.MODEL_NAME} couldn't")
                    alert, hexes, match_quality = retry_alert, retry_hexes, retry_quality
        else:
            print(f"   ↳ {config.GEMINI_FALLBACK_MODEL_NAME} retry budget ({config.GEMINI_FALLBACK_RETRY_SHARE}) spent - staying UNRESOLVED, no retry")

    conn = get_connection()
    cursor = conn.cursor()

    region = alert.city or 'НОВИ САД'
    event_type = alert.event_type or 'POWER'
    affected_locations = alert.affected_locations
    
    # Named streets first; a whole-settlement outage has none, and labelling those "Nije
    # navedeno" hid the one fact that actually describes them - the settlement is the affected
    # area, not a missing detail (every "прекид у целом насељу" row read as unlabelled).
    extracted_names = [
        str(loc.main_street) for loc in affected_locations if loc.main_street
    ]
    if not extracted_names:
        extracted_names = list(dict.fromkeys(
            _area_label(loc)
            for loc in affected_locations
            if loc.location_type == "ENTIRE_SETTLEMENT" and loc.settlement
        ))
    affected_streets_str = ", ".join(extracted_names) if extracted_names else "Nije navedeno"
    outage_date = alert.outage_date or 'N/A'

    # Build visual GeoJSON FeatureCollection for frontend rendering
    visual_features = _build_visual_features(alert, region, gate_region)
    fault_centres = _fault_centres(alert, region) if nearby else []
    if fault_centres:
        visual_features = (visual_features or []) + _nearby_features(fault_centres, nearby[0])
    # Any other source: a house named "и околне улице" gets the same circle.
    widened_houses = [] if nearby else _fault_centres(alert, region, widened_only=True)
    if widened_houses:
        visual_features = (visual_features or []) + _nearby_features(widened_houses, NEARBY_M, prefix="")

    geojson_payload = json.dumps({
        "type": "FeatureCollection",
        "features": visual_features
    }) if visual_features else None

    # The gate already kept the wrong place out of both the hexes and the map; this tells the
    # operator it had to, because each block means some resolver upstream picked a same-named
    # place elsewhere and needs fixing at its source.
    if _leak_gate_events:
        send_operator_alert(
            f"leak_blocked:{text_hash or raw_text[:80]}",
            "Leak blocked (nothing wrong was sent or drawn):\n"
            f"{region}, {alert.outage_date or 'N/A'}\n" + "\n".join(_leak_gate_events[:8]),
            cooldown_s=86400,
        )

    # The announcement's own time window, taken from the first location that carries one - after
    # _split_by_time_window every location of this outage shares it, and this is what the map popup
    # and outage expiry read instead of the hardcoded 'N/A' this column used to store.
    time_window = next((loc.time_window for loc in affected_locations if loc.time_window), 'N/A')
    time_window = _normalize_time_window(time_window, raw_text)

    expires_at = _announcement_expires_at(alert, outage_date, time_window)

    # A notice that is only about closed/diverted traffic, with no utility supply cut, still
    # belongs on the map (context for the neighbourhood) but must not generate notifications -
    # nobody signed up to be told their street is closed to cars. The scraper can pass
    # category="WORKS" for these, or the model flags road_closure_only on a mixed feed.
    if getattr(alert, 'road_closure_only', False) and category in (None, 'WATER', 'HEATING'):
        category = 'WORKS'

    # Dedup-on-update: a re-scrape of the SAME real event (site edited the post, or an EPS
    # notice slid from Dan_2 to Dan_1 as its date got closer) must update the existing row, not
    # insert a new one that re-notifies everyone already told. raw_text_hash dedup alone only
    # catches an exact re-scrape of unchanged text - any wording/detail change defeats it, which
    # is exactly the case this is for.
    #
    # Correlated on (region, outage_date, category, primary affected location) - not source_url,
    # since some sites publish a fresh URL per day for what's still the same real-world event
    # (EPS's Dan_N pattern). Deliberately does NOT correlate when the primary location is the
    # "Nije navedeno" placeholder or outage_date is 'N/A' - two genuinely unrelated outages could
    # both lack a named street/date, and merging those on nothing but region+category would be a
    # false-positive silently swallowing a real second event. Also bounded to still-current
    # (expires_at in the future, or unset within the last 14 days) - never merge into a stale row.
    #
    # outage_date is compared as a PARSED date, not a raw string: a re-scrape of the same notice
    # can have the LLM return the same date in a different format than last time (16.09.2026 vs
    # 2026-09-16), and an exact string match would miss that, insert a second row, and re-notify
    # everyone already told (this happened live 2026-09-15 - two duplicate emails, different-
    # looking subjects, one outage). SQL can't apply _parse_date, so it's checked in the same
    # Python loop as the primary-location comparison below instead of the WHERE clause.
    primary_location = extracted_names[0] if extracted_names else None
    parsed_outage_date = _parse_date(outage_date)

    # "Radovi ... odloženi su do daljeg" / "otkazani": the announced outage won't happen. It
    # comes off the map and everyone already told hears so; the notice itself is kept only so
    # it isn't parsed again. Only when it matches an outage we hold - otherwise it goes through
    # as an ordinary notice, as before.
    if _cancellation_kind(raw_text) and _cancel_announced_outage(
            cursor, raw_text, text_hash, region, category, extracted_names, parsed_outage_date,
            time_window, source_url, event_type, parsed_json=alert.model_dump_json()):
        conn.commit()
        conn.close()
        return

    existing = None
    if primary_location and outage_date != 'N/A' and parsed_outage_date:
        cursor.execute('''
            SELECT id, region, affected_streets, outage_date, time_window, parsed_json, raw_text_hash FROM outages
            WHERE category IS ?
              AND status NOT IN ('CANCELLED', 'CANCELLATION', 'DUPLICATE')
              AND (expires_at IS NULL OR expires_at > datetime('now'))
              AND created_at > datetime('now', '-14 days')
            ORDER BY id DESC
        ''', (category,))
        new_times = _window_times(time_window)
        new_last = _parse_date(getattr(alert, 'end_date', None) or '') or parsed_outage_date
        for row in cursor.fetchall():
            if not _same_region(row['region'], region):
                continue
            # EPS lists one village at several hours on one day (Буљане 14.10.2026: 08-09,
            # 09-10:30, 11-12). Those are separate cuts, all still on the page. Merged, each
            # cycle re-read the other two as new (their hash was overwritten), called Gemini and
            # flipped the stored hours: 286 flips and ~900 Gemini calls in 30 h, and a time-change
            # message every cycle to anyone already told.
            # Its own row (same hash: this very notice processed again) is not a sibling - that
            # one is updated in place, never inserted twice (raw_text_hash is UNIQUE).
            if live_hashes and row['raw_text_hash'] in live_hashes and row['raw_text_hash'] != text_hash:
                continue
            row_start = _parse_date(row['outage_date'])
            if row_start != parsed_outage_date and not _continues_stored_run(
                    row, row_start, parsed_outage_date, new_last, time_window):
                continue
            existing_first = (row['affected_streets'] or '').split(',')[0].strip()
            same_window = bool(new_times) and _window_times(row['time_window']) == new_times
            if ((existing_first and existing_first.upper() == primary_location.strip().upper())
                    or _same_notice_respelled(row['affected_streets'], extracted_names, region,
                                              same_window)):
                existing = row
                break

    # Full parsed alert, stored so the registration backfill can re-verify this outage against a
    # later signup without re-calling Gemini (see core/database.py migration 1g).
    parsed_json = alert.model_dump_json()

    already_notified = set()
    time_changed = None
    if existing:
        outage_id = existing['id']
        cursor.execute('''
            UPDATE outages
            SET affected_streets = ?, time_window = ?, raw_text = ?, raw_text_hash = ?,
                geojson_geometry = ?, match_quality = ?, event_type = ?, category = ?,
                source_url = ?, expires_at = ?, parsed_json = ?
            WHERE id = ?
        ''', (affected_streets_str, time_window, raw_text, text_hash, geojson_payload,
              match_quality, event_type, category, source_url, expires_at, parsed_json, outage_id))
        # The notice changed: an automatic "agreed" was about the old text, so look again.
        cursor.execute("DELETE FROM send_checks WHERE outage_id = ? AND status = 'AGREED'", (outage_id,))
        cursor.execute(
            "SELECT id, target_id, channel, status, custom_message FROM message_queue "
            "WHERE outage_id = ? ORDER BY id", (outage_id,)
        )
        # The latest row per (target, channel): a change already told supersedes the first.
        latest = {(r['target_id'], r['channel']): r for r in cursor.fetchall()}
        already_notified = set(latest)
        # The same notice at new hours: the people already told must hear it (see
        # _queue_time_change). Compared on the structured window, not the message wording, which
        # the model rewrites on every parse.
        if _window_times(time_window) and _window_times(existing['time_window']) != _window_times(time_window):
            time_changed = latest
            print(f"🔁 Outage #{outage_id}: hours changed {existing['time_window']} -> {time_window}")
    else:
        cursor.execute('''
            INSERT INTO outages (region, affected_streets, outage_date, time_window, raw_text, raw_text_hash, geojson_geometry, status, match_quality, event_type, category, source_url, expires_at, parsed_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)
        ''', (region, affected_streets_str, outage_date, time_window, raw_text, text_hash, geojson_payload, match_quality, event_type, category, source_url, expires_at, parsed_json))
        outage_id = cursor.lastrowid

    if hexes:
        for hex_id in hexes:
            cursor.execute('''
                INSERT OR IGNORE INTO outage_hexes (outage_id, hex_id)
                VALUES (?, ?)
            ''', (outage_id, hex_id))

    # An outage first seen after its own window has ended (EPS's Dan_0 page read in the evening -
    # e.g. the first run over the Belgrade pages, 2026-09-24) is map history, not news: telling
    # someone at 21:00 about a 08:30-14:00 cut helps nobody. Same-day only - past days are
    # already suppressed in _match_and_queue_target. The container clock runs UTC, behind Serbia,
    # so this errs towards still sending.
    already_over = _window_over(expires_at)
    # The heating being switched on (tople probe, the season starting) cuts nothing: no address
    # alert. A whole city hears it once, as a Važno obaveštenje the operator approves (below).
    start_notice = heating_start(raw_text) if category == 'HEATING' else None

    if hexes and category != 'WORKS' and not already_over and not start_notice:
        if _date_not_in_notice(outage_date, raw_text):
            cursor.execute("INSERT OR IGNORE INTO send_checks (outage_id, reason, status) "
                           "VALUES (?, 'date', 'PENDING')", (outage_id,))
            print(f"📅 Outage #{outage_id}: date {outage_date} is not in the notice - alerts held")
        placeholders = ','.join(['?'] * len(hexes))

        # TWO-TIER GUARD: Micro H3 Spatial Index (SQL) + Macro Municipality Scope (Python).
        # The scope check runs in Python, not the WHERE clause, because it has to be
        # script-insensitive (see _norm_region) - a Latin-registered target vs a Cyrillic-
        # scraped outage region is the common case, and SQLite's UPPER() does not transliterate.
        cursor.execute(f'''
            SELECT DISTINCT t.id as target_id, t.ulica, t.broj, t.telegram_chat_id, t.email,
                            t.email_active, t.city, t.opstina, t.naselje
            FROM target_hexes th
            JOIN targets t ON th.target_id = t.id
            WHERE th.hex_id IN ({placeholders})
              AND t.is_active = 1
        ''', list(hexes))

        want_region = _region_variants(region)
        candidate_targets = [
            t for t in cursor.fetchall()
            if want_region & _region_token_set(t['city'], t['opstina'], t['naselje'])
        ]
        queued_count, already_notified_skipped, wildcard_count = 0, 0, 0
        heat_supply = heat_supply_cut(raw_text) if category == 'HEATING' else None

        # Per-target street-scoped verification (parity/number-range, exclusions, already-notified
        # skip) - shared with the registration backfill via _match_and_queue_target so both run
        # identical logic.
        for target in candidate_targets:
            q, s, w = _match_and_queue_target(
                cursor, outage_id, alert, affected_locations, target, already_notified,
                category, match_quality, previous=time_changed, heat_supply=heat_supply,
                works=works, region=region
            )
            queued_count += q
            already_notified_skipped += s
            wildcard_count += w

        if fault_centres:
            near = _queue_nearby(cursor, outage_id, alert, fault_centres, *nearby)
            if near:
                print(f"💧 Outage #{outage_id}: {near} address(es) within {nearby[0]} m told it may reach them")
        # A past day never sends (the same guard as _match_and_queue_target's).
        last_day = _outage_last_day(alert)
        if not (last_day and last_day.date() < datetime.now().date()):
            if widened_houses:
                near = _queue_nearby(cursor, outage_id, alert, widened_houses, NEARBY_M, "",
                                     message=_ring_message(alert, category))
                if near:
                    print(f"💧 Outage #{outage_id}: {near} address(es) within {NEARBY_M} m told it may reach them")
            side = _queue_side_streets(cursor, outage_id, alert, region, category)
            if side:
                print(f"💧 Outage #{outage_id}: {side} address(es) on side streets told it may reach them")
            border = _queue_block_border(cursor, outage_id, alert, region, category)
            if border:
                print(f"💧 Outage #{outage_id}: {border} address(es) across the road from the block told it may reach them")

        quality_flag = "" if match_quality == 'EXACT' else f" ⚠️ match_quality={match_quality}"
        action = "Updated" if existing else "Saved"
        skip_note = f", {already_notified_skipped} already notified (skipped)" if already_notified_skipped else ""
        print(f"💾 [{event_type}] {action} Outage #{outage_id}. Queued {queued_count} verified messages{skip_note}!{quality_flag}")

        # Real-time operator visibility for anything a human should eyeball before a user
        # complains: an approximate outage that still queued messages, or any target that
        # matched via a settlement wildcard rather than its own street being named. Confirmed
        # real gap: outage #2178's LANDMARK leak (see _match_and_queue_target) only ever showed
        # up as a print() inside the scrapers container - nobody watches that log live, so it
        # went unnoticed until a user reported it days later. This reuses the same Telegram DM
        # infra watchdog.py already uses for pipeline health, so no new channel to check.
        if queued_count and (match_quality != 'EXACT' or wildcard_count):
            reasons = []
            if match_quality != 'EXACT':
                reasons.append(f"match_quality={match_quality}")
            if wildcard_count:
                # Name the area: the last line lists only the streets, and #3465 ("ПОРУЧНИКА
                # СПАСИЋА И МАШЕРЕ") never said the two people matched through ЦЕРАК.
                areas = sorted({loc.settlement for loc in affected_locations
                                if not loc.main_street and loc.settlement})
                reasons.append(f"{wildcard_count} matched via the whole area {', '.join(areas)}, "
                               "not their own street" if areas
                               else f"{wildcard_count} matched via settlement wildcard, not own street")
            if _name_corrections:
                reasons.append("street name read as: " + ", ".join(_name_corrections))
            send_operator_note(
                f"Outage #{outage_id} queued {queued_count} messages worth a glance: "
                + "; ".join(reasons) + f"\n{region}, {outage_date}, {affected_streets_str}"
            )
    elif category == 'WORKS':
        action = "Updated" if existing else "Saved"
        print(f"🚧 [WORKS] {action} Outage #{outage_id} (map only, no notifications). match_quality={match_quality}")
    elif hexes:
        action = "Updated" if existing else "Saved"
        print(f"💾 [{event_type}] {action} Outage #{outage_id}. Window already over ({expires_at} "
              f"incl. grace) - map only, no notifications.")
    else:
        action = "Updated" if existing else "Saved"
        print(f"💾 [{event_type}] {action} Outage #{outage_id}. No spatial hexes generated. match_quality={match_quality}")

    conn.commit()
    conn.close()

    # After the commit, outside the write: anything held for this outage gets its second look now
    # rather than at the next cycle (core/send_check.py).
    try:
        from core import send_check
        send_check.check(outage_id)
    except Exception as e:
        print(f"⚠️ Outage #{outage_id}: send check failed, retried next cycle: {e}")

    # After the commit: the draft is written on its own connection.
    if start_notice and not existing and not already_over:
        try:
            from core.broadcast import create_draft, heating_start_text
            draft = create_draft(region, heating_start_text(region, outage_date, raw_text,
                                                            start_notice == "SEASON"),
                                 source_url=source_url, source_outage_id=outage_id)
            print(f"📣 Outage #{outage_id} is the heating starting - "
                  + (f"draft #{draft} sent to the operator" if draft else "no draft (nobody there)"))
        except Exception as e:
            print(f"⚠️ Outage #{outage_id}: heating-start draft failed: {e}")


def _resolve_target_hexes(target_row) -> list[str]:
    """Street-level H3 cells for a registered address, settlement-level coverage as fallback -
    the same primitives and order scripts/map_targets.py uses, so a target mapped here and one
    mapped by the batch script land on the identical grid."""
    settlement = target_row['naselje'] or target_row['city']
    parent_region = target_row['opstina'] or target_row['city']
    street = target_row['ulica']

    hexes = get_street_hexes(street, settlement=settlement, parent_region=parent_region)
    if not hexes:
        hexes = get_settlement_polygon_hexes(settlement, parent_region=parent_region)
    return list(hexes or [])


def ensure_target_hexes(cursor, target_row) -> list[str]:
    """Return the target's target_hexes rows, resolving + inserting them first if it has none.
    Lets registration map a brand-new target inline instead of waiting for the next
    map_unmapped_targets() pass in main.py."""
    cursor.execute("SELECT hex_id FROM target_hexes WHERE target_id = ?", (target_row['id'],))
    existing = [r['hex_id'] for r in cursor.fetchall()]
    if existing:
        return existing

    hexes = _resolve_target_hexes(target_row)
    for hex_id in hexes:
        cursor.execute(
            "INSERT OR IGNORE INTO target_hexes (target_id, hex_id) VALUES (?, ?)",
            (target_row['id'], hex_id),
        )
    return hexes


def backfill_target_against_active_outages(target_id: int, dry_run: bool = False) -> dict:
    """Match ONE freshly-registered target against outages that were already scraped and stored
    before it existed. Without this, a signup only ever matches announcements scraped AFTER it
    (or an edit to an existing one) - someone registering for a known upcoming outage would get
    nothing. Maps the target to hexes first if needed. Safe to call more than once: every insert
    path here skips a target already queued for that outage. dry_run rolls everything back, so
    the counts say what a real run would queue; it also leaves an unmapped target unmapped rather
    than geocoding it (that would spend geocoder quota and write caches for nothing).

    Returns {'mapped_hexes': int, 'candidate_outages': int, 'queued': int, 'skipped': int}."""
    conn = get_connection()
    cursor = conn.cursor()
    summary = {'mapped_hexes': 0, 'candidate_outages': 0, 'queued': 0, 'skipped': 0}
    try:
        cursor.execute('''
            SELECT id, city, opstina, naselje, ulica, broj, is_active, telegram_chat_id,
                   email, email_active
            FROM targets WHERE id = ?
        ''', (target_id,))
        target_row = cursor.fetchone()
        if not target_row or not target_row['is_active']:
            return summary

        if dry_run:
            cursor.execute("SELECT hex_id FROM target_hexes WHERE target_id = ?", (target_row['id'],))
            hexes = [r['hex_id'] for r in cursor.fetchall()]
        else:
            hexes = ensure_target_hexes(cursor, target_row)
        summary['mapped_hexes'] = len(hexes)
        if not hexes:
            conn.rollback() if dry_run else conn.commit()
            return summary

        region_scope = _region_token_set(
            target_row['city'], target_row['opstina'], target_row['naselje']
        )

        placeholders = ','.join(['?'] * len(hexes))
        cursor.execute(f'''
            SELECT DISTINCT o.id, o.region, o.raw_text, o.time_window, o.outage_date,
                            o.affected_streets, o.category, o.match_quality, o.parsed_json,
                            o.expires_at
            FROM outage_hexes oh
            JOIN outages o ON o.id = oh.outage_id
            WHERE oh.hex_id IN ({placeholders})
              AND o.status = 'ACTIVE'
              AND (o.expires_at IS NULL OR o.expires_at > datetime('now'))
              AND (o.category IS NULL OR o.category != 'WORKS')
        ''', list(hexes))
        candidates = cursor.fetchall()

        # Same DISTINCT-target shape _match_and_queue_target expects from the scrape path.
        target_for_match = {
            'target_id': target_row['id'],
            'ulica': target_row['ulica'],
            'broj': target_row['broj'],
            'telegram_chat_id': target_row['telegram_chat_id'],
            'email': target_row['email'],
            'email_active': target_row['email_active'],
            'city': target_row['city'],
            'opstina': target_row['opstina'],
            'naselje': target_row['naselje'],
        }

        for o in candidates:
            if region_scope and not (_region_variants(o['region']) & region_scope):
                continue
            # Same rule as the scrape path: once the announced window is over, a message only
            # tells someone about a cut that's behind them.
            if _window_over(o['expires_at']):
                continue
            # An outage with expires_at IS NULL slips past the recency filter in the SQL above
            # (that clause only bounds rows that HAVE an expiry). If such a row is also dated in
            # the past it is stale - never backfill it onto a new signup. A row with a real
            # future expires_at is genuinely still active (e.g. a multi-day works order) and is
            # left alone even when its start date has already passed.
            if not o['expires_at']:
                _last = _parse_date(o['outage_date'] or '')
                if _last and _last.date() < datetime.now().date():
                    continue
            if o['category'] == 'HEATING' and heating_start(o['raw_text']):
                continue  # the heating starting: a city notice, never an address alert
            summary['candidate_outages'] += 1

            cursor.execute(
                "SELECT target_id, channel FROM message_queue WHERE outage_id = ?", (o['id'],)
            )
            already = {(r['target_id'], r['channel']) for r in cursor.fetchall()}

            if o['parsed_json']:
                try:
                    alert = OutageAlert.model_validate_json(o['parsed_json'])
                except Exception:
                    alert = None
                if alert:
                    locs = _inherit_settlement_context(_dedup_locations(alert.affected_locations))
                    heat = o['category'] == 'HEATING'
                    q, s, _w = _match_and_queue_target(
                        cursor, o['id'], alert, locs, target_for_match, already,
                        o['category'], o['match_quality'],
                        heat_supply=heat_supply_cut(o['raw_text']) if heat else None,
                        works=heat and heat_works_only(o['raw_text']), region=o['region']
                    )
                    summary['queued'] += q
                    summary['skipped'] += s
                    continue

            # Fallback for rows scraped before parsed_json existed (or unparseable): the target
            # already hex-overlaps this outage; accept it if the target's street appears in the
            # flattened affected_streets, or the outage is whole-settlement ("Nije navedeno").
            # No parity/number-range precision available here - a rare, bounded over-notify on
            # legacy rows only, never on anything scraped from now on.
            to_queue = [c for c in _target_channels(target_row) if (target_row['id'], c) not in already]
            if not to_queue:
                summary['skipped'] += 1
                continue
            streets = o['affected_streets'] or ''
            loose_match = streets.strip().upper() in ('', 'NIJE NAVEDENO') or any(
                _streets_match(tok, target_row['ulica']) for tok in streets.split(',')
            )
            if loose_match:
                # No message_template on a pre-parsed_json row, but a plain sentence built from
                # the target's own street + the outage's date/window still beats dumping the raw
                # Cyrillic article. Latin, one line - the same shape _render_message produces.
                street_disp = cyrillic_to_latin(target_row['ulica']).title()
                when = (o['outage_date'] or '').strip()
                tw = (o['time_window'] or '').strip()
                loose_msg = f"Planirano isključenje na adresi {street_disp}"
                if when and when != 'N/A':
                    loose_msg += f" dana {when}"
                if tw and tw != 'N/A':
                    loose_msg += f" u periodu {tw}"
                loose_msg += "."
                for channel in to_queue:
                    cursor.execute('''
                        INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at)
                        VALUES (?, ?, ?, ?, 'QUEUED', datetime('now'))
                    ''', (o['id'], target_row['id'], channel, loose_msg))
                summary['queued'] += 1

        conn.rollback() if dry_run else conn.commit()
    finally:
        conn.close()

    print(f"🎯 Backfill target #{target_id}: {summary['mapped_hexes']} hexes, "
          f"{summary['candidate_outages']} candidate outage(s), "
          f"{summary['queued']} {'would be queued (dry run)' if dry_run else 'queued'}, "
          f"{summary['skipped']} skipped (already notified).")
    return summary


# The dashboard's "Poslednjih 90 dana" line (app/dashboard.html): per address, how many outages
# hit it and roughly how many hours they announced.
HISTORY_DAYS = 90


def _window_hours(window: str | None) -> float:
    """Hours in an announced window: "08:00 - 14:00" is 6, "22:00 - 06:00" is 8, two slots add
    up. A pair that can't be read as a span (a morning start with an earlier end) adds nothing."""
    times = [int(h) * 60 + int(m) for h, m in re.findall(r"(\d{1,2})[:.](\d{2})", window or "")]
    if len(times) < 2 or len(times) % 2:
        return 0.0
    minutes = 0
    for start, end in zip(times[::2], times[1::2]):
        if end > start:
            minutes += end - start
        elif end == 0 or start >= 12 * 60:  # until midnight, or a night window (_compute_expires_at)
            minutes += end + 24 * 60 - start
    return minutes / 60


def _older_copy(o, hits) -> bool:
    """True when an EXPIRED row is only an older copy of a notice that also hit this address.
    Nothing in the code sets EXPIRED: a one-off cleanup on 2026-09-11 kept the newest row per
    source page and marked the rest EXPIRED. That hid real duplicates of one notice (the same
    article re-scraped with a drifted date), but also every other outage listed on the same EPS
    page, which were real. So an EXPIRED row still counts, unless the row the cleanup kept (same
    page, same kind, still ACTIVE) hit this address within a day of it."""
    if o['status'] != 'EXPIRED' or not o['source_url']:
        return False
    return any(h['status'] == 'ACTIVE' and h['source_url'] == o['source_url']
               and h['category'] == o['category'] and abs((h['day'] - o['day']).days) <= 1
               for h in hits)


def outage_history(target_ids, days: int = HISTORY_DAYS, today=None) -> dict:
    """{target_id: {"count": n, "hours": h}}: the outages dated in the last `days` days (today
    included, announced future ones not) that the alert rules would have sent to this address,
    registered by then or not - the same hexes, region scope and _target_match as a real alert,
    so a neighbouring street or house number never counts. Road closures don't count. A row with
    no stored parse can't be checked by house number, so it doesn't count either. The same event
    stored twice (re-published under another spelling, or an older copy hidden as EXPIRED - see
    _older_copy) counts once. Hours are from the announced windows, rounded."""
    today = today or datetime.now().date()
    since = today - timedelta(days=days)
    out = {tid: {"count": 0, "hours": 0.0} for tid in target_ids}
    if not out:
        return out
    parsed = {}
    conn = get_connection()
    try:
        for tid in out:
            t = conn.execute(
                "SELECT id, city, opstina, naselje, ulica, broj FROM targets WHERE id = ?", (tid,)
            ).fetchone()
            if not t:
                continue
            hexes = [r['hex_id'] for r in conn.execute(
                "SELECT hex_id FROM target_hexes WHERE target_id = ?", (tid,))]
            if not hexes:
                continue
            scope = _region_token_set(t['city'], t['opstina'], t['naselje'])
            rows = conn.execute(f'''
                SELECT DISTINCT o.id, o.region, o.outage_date, o.time_window, o.category,
                                o.match_quality, o.parsed_json, o.status, o.source_url
                FROM outage_hexes oh
                JOIN outages o ON o.id = oh.outage_id
                WHERE oh.hex_id IN ({','.join('?' * len(hexes))})
                  AND o.status IN ('ACTIVE', 'EXPIRED')
                  AND (o.category IS NULL OR o.category != 'WORKS')
                  AND o.parsed_json IS NOT NULL
            ''', hexes).fetchall()
            hits = []
            for o in rows:
                day = _parse_date(o['outage_date'] or '')
                if not day or not (since <= day.date() <= today):
                    continue
                if scope and not (_region_variants(o['region']) & scope):
                    continue
                if o['id'] not in parsed:
                    try:
                        alert = OutageAlert.model_validate_json(o['parsed_json'])
                        parsed[o['id']] = (alert, _inherit_settlement_context(
                            _dedup_locations(alert.affected_locations)))
                    except Exception:
                        parsed[o['id']] = None
                if not parsed[o['id']]:
                    continue
                alert, locs = parsed[o['id']]
                loc = _target_match(alert, locs, t, o['match_quality'])
                if loc is None:
                    continue
                hits.append({'day': day.date(), 'category': o['category'], 'status': o['status'],
                             'source_url': o['source_url'],
                             'window': loc.time_window or o['time_window']})
            seen = set()
            for h in hits:
                if _older_copy(h, hits):
                    continue
                key = (h['day'], h['category'], re.sub(r"\s+", "", h['window'] or ""))
                if key in seen:
                    continue
                seen.add(key)
                out[tid]["count"] += 1
                out[tid]["hours"] += _window_hours(h['window'])
    finally:
        conn.close()
    for v in out.values():
        v["hours"] = round(v["hours"])
    return out