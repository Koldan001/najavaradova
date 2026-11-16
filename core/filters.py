import re

# One range written as two numbers and a dash - any dash a model or a source might type:
# hyphen, non-breaking hyphen, figure dash, en/em dash, minus ("14-32", "14 – 32", "14‑32").
# Either end may carry a house letter: EPS writes "40Б-52А", "1А-3", "0ББ-20". Read without the
# letter, "40Б-52А" was two houses, 40 and 52, and 41 to 51 were never told (50 such lists in
# the stored notices on 09.10.2026).
_RANGE = re.compile(r"(\d+)[^\W\d_]{0,2}\s*[-‐‑‒–—−]\s*(\d+)")
# ...or in words: "od 20 do 40", "од бр. 20 до 40".
_WORDS_RANGE = re.compile(
    r"(?:od|од)\s*(?:br\.?|broja|бр\.?|броја)?\s*(\d+)\s*(?:do|до)\s*(?:br\.?|broja|бр\.?|броја)?\s*(\d+)",
    re.IGNORECASE)
# A remark in brackets that isn't numbers - "5 (ulazi 1-3)" is house 5, not houses 1 to 3.
_ASIDE = re.compile(r"\([^)]*[^\d\s,.;()\-‐‑‒–—−][^)]*\)")


def _pairs(text: str) -> tuple[list[tuple[int, int]], str]:
    """Every range in `text`, low end first ("19-1" is 1-19), and the text left without them."""
    pairs = []
    for rx in (_WORDS_RANGE, _RANGE):
        pairs += [(min(int(a), int(b)), max(int(a), int(b))) for a, b in rx.findall(text)]
        text = rx.sub(" ", text)
    return pairs, text


def _ranges(number_range) -> list[tuple[int, int]] | None:
    """The ranges in a number_range value. "1-19, 2-30" is two ranges; "od 20 do 40" is one, and
    so are two bare numbers. None when it can't be read as ranges at all ("20", "od 5 do kraja",
    "1-19 i 25"): the caller then counts the whole side as affected."""
    pairs, rest = _pairs(_ASIDE.sub(" ", str(number_range or "")))
    left = [int(n) for n in re.findall(r"\d+", rest)]
    if pairs and not left:
        return pairs
    if not pairs and len(left) == 2:
        return [(min(left), max(left))]
    return None


def _in_range(num: int, lo: int, hi: int, own_side: bool) -> bool:
    """Is house `num` in lo-hi? With `own_side`, a range whose ends are both even or both odd
    holds only that side: EPS writes "Руменачка 30 – 54, 31 – 41" for 30, 32 ... 54 and 31, 33 ...
    41, so 53 is not in it (#3929: Rumenačka 53 was told of an outage it never had)."""
    if not lo <= num <= hi:
        return False
    return not own_side or lo % 2 != hi % 2 or num % 2 == lo % 2


def is_house_number_affected(
    user_number_str: str,
    parity: str = "ALL",
    number_range: str | None = None,
    explicit_numbers: str | None = None
) -> bool:
    """Does the announcement's number scope cover this house number?

    A number named outright (explicit_numbers) is affected, whatever the parity says. So is one
    inside a range (number_range, or a range the model wrote into explicit_numbers) on the
    announced side of the street. The two are alternatives: "14-32 i 11, 13" covers 11 as well
    as 20. A range with two even or two odd ends is one side of the street ("30-54" is 30, 32 ...
    54), unless the announcement names the side itself. Both used to have to hold at once, so a segment carrying both matched nobody on the
    street (outage #6, Srbobranska), and a descending "19-1" matched nobody either.

    An announcement whose numbers can't be read for certain counts as affected - a missed alert
    is worse than an extra one. A house with no number ("BB") is the other way round: the whole
    street reaches it, but a list or a stretch of numbers does not. Real case: "П.СМИЉАНИЋА 48,
    50, 52, 54" (4 houses of 276 in Šabac) alerted "Prote Smiljanića BB" twice (#6058, #6503)."""
    if parity == "ALL" and not number_range and not explicit_numbers:
        return True

    match = re.search(r'\d+', str(user_number_str))
    if not match:
        return False  # no house number ("BB"): nothing places it inside a stretch or a list

    num = int(match.group())
    on_side = not ((parity == "PARNA" and num % 2) or (parity == "NEPARNA" and not num % 2))

    ranges, rest = _pairs(_ASIDE.sub(" ", str(explicit_numbers or "")))
    listed = [int(n) for n in re.findall(r"\d+", rest)]
    if num in listed:
        return True

    if number_range:
        read = _ranges(number_range)
        if read is None:
            return on_side
        ranges += read
    if ranges:
        return on_side and any(_in_range(num, lo, hi, parity == "ALL") for lo, hi in ranges)
    if listed:
        return False  # a list of numbers, and this one isn't on it
    return on_side


# Any time of day a notice states, in Latin or Cyrillic: "08:00", "8.00", "od 8 do 14 časova",
# "до 15 часова", "ujutru", "у вечерњим часовима"... A date ("28.09.2026.") is not a time: a
# dotted pair followed by another dot or digit is left alone.
_STATED_TIME = re.compile(
    r"\b([01]?\d|2[0-3]):[0-5]\d\b"
    r"|\b([01]?\d|2[0-3])\.[0-5]\d\b(?![.\d])"
    r"|\b\d{1,2}\s*(h\b|č|c|ч|sat|сат)"
    r"|ujutr|јутр|jutarnj|јутарњ|prepodn|преподн|popodn|poslepodn|послеподн|večer|vecer|вечер"
    r"|noćn|nocn|ноћн|noću|nocu|ноћу|tokom dana|током дана|celodnevn|целодневн|non-?stop",
    re.IGNORECASE)


# Lower case, Cyrillic to Latin, no diacritics - "грејање", "grejanje" and "grejanje" alike.
_FOLD = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "dj", "е": "e", "ж": "z", "з": "z",
    "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m", "н": "n", "њ": "nj", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "ћ": "c", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "c", "џ": "dz", "ш": "s", "š": "s", "ž": "z", "č": "c", "ć": "c", "đ": "dj",
})
# Beograd writes "potrošne tople vode", Novi Sad "топле потрошне воде".
_HOT_WATER = re.compile(r"topl\w*\s+(?:(?:potrosn|sanitarn)\w*\s+)?vod|(?:potrosn|sanitarn)\w*\s+topl|\bptv\b")
_HEATING = re.compile(r"grejanj")
# Not heating being cut: the plant's service area ("celo grejno područje toplane Cerak"), the
# season ("pre početka sezone grejanja"), and heating as a service ("sistem daljinskog grejanja",
# "usluge grejanja", "stabilno grejanje" - Niš and Kragujevac works notices).
_NOT_HEATING = re.compile(r"grejn\w*|sezon\w*\s+grejanj\w*"
                          r"|(?:sistem|uslug|stabiln|daljinsk)\w*\s+(?:daljinsk\w*\s+)?grejanj\w*")
_NOTHING_CUT = re.compile(r"nece (biti prekid|doci do prekid|uticati)|bez prekida|ne dolazi do prekid"
                          r"|nema uticaja|bez obustave")
# The notice says something is cut...
_CUT = re.compile(r"prekid|prekin|iskljuc|obustav|poremecaj|bez (grejanja|tople|toplot|isporuk)"
                  r"|nema (grejanja|tople)|nece biti (isporuk|grejanja|tople)"
                  r"|smanjen\w*\s+(isporuk|pritis|temperatur)")
# ...but not traffic being stopped, nor Niš toplana's page footer ("obaveštenja o radovima i
# prekidima u snabdevanju").
_NOT_A_CUT = re.compile(r"(obustav|prekid|prekin)\w*\s+(\w+\s+){0,2}saobracaj"
                        r"|radovima i prekidima u snabdevanju")
_WORKS = re.compile(r"\b(radov|izgradnj|popravk|rekonstrukcij|sanacij|zamen|remont)")


def _fold(text: str | None) -> str:
    return (text or "").lower().translate(_FOLD)


def _says_cut(folded: str) -> bool:
    return bool(_CUT.search(_NOT_A_CUT.sub(" ", folded)))


def heat_supply_cut(text: str | None) -> str | None:
    """What a toplana notice takes away: "HOT_WATER", "HEATING", "BOTH", or None when the text
    doesn't say (or says nothing is cut). Beogradske elektrane's "prekid u isporuci toplotne
    energije za pripremu potrošne tople vode" is hot water only - the alert said "toplotne
    energije (grejanje i topla voda)" and the map card "Bez grejanja", in September, before the
    heating season (#3465, Toplana Cerak's remont). A notice that names heating but cuts nothing
    ("radovi ... u cilju sprečavanja curenja na sistemu daljinskog grejanja") takes nothing."""
    t = _fold(text)
    if not t or _NOTHING_CUT.search(t) or not _says_cut(t):
        return None
    hot = bool(_HOT_WATER.search(t))
    heat = bool(_HEATING.search(_NOT_HEATING.sub(" ", t)))
    return "BOTH" if hot and heat else "HOT_WATER" if hot else "HEATING" if heat else None


def heat_works_only(text: str | None) -> bool:
    """Works on a toplana's network, with nothing said to be cut: Beogradske elektrane's daily
    "Planirani radovi na toplovodnoj mreži" ("Radovi na izgradnji toplovodne mreže u Ulici braće
    Jerković"), Niš toplana's "početak radova na toplovodu". A notice that says nothing will be
    cut is not one of these - telling people "moguć prekid" would contradict it."""
    t = _fold(text)
    return bool(_WORKS.search(t)) and not _NOTHING_CUT.search(t) and not _says_cut(t)


def states_time(text: str | None) -> bool:
    """Does the notice itself say when in the day? The parser fills a window either way
    (parser.py rule 8: no start -> 07:00, no end -> 19:00), so a window it made up can only be
    told from the source text."""
    return bool(_STATED_TIME.search(text or ""))


# The heating being switched on, not off: Niš toplana's "tople probe na teritoriji celog grada"
# (#4298), "početak grejne sezone", "probno grejanje". Kruševac's tople probe notices also name the
# buildings left "bez grejanja" that day - that is a cut, and stays an ordinary outage.
_HEAT_PROBE = re.compile(r"\btopl\w* prob|\bprobn\w* (grejanj|rad\w* (sistem|toplan))")
# "pred početak grejne sezone" (works done before it - Kruševac filling its network, #1329) is not it.
_HEAT_SEASON = re.compile(r"(?<!pred )(?<!pre )(?<!do )(?<!uoci )(pocet|pocinj)\w* (\w+ )?grejn\w* sezon"
                          r"|grejn\w* sezon\w* (pocinj|pocet|start)"
                          r"|pustanj\w* (u rad )?(sistem\w* )?grejanj|ukljucenj\w* grejanj")


# A sentence about what might go wrong: "ukoliko dođe do eventualnih poremećaja u isporuci" (#4298).
_IF = re.compile(r"\b(ukoliko|ako|u slucaju|eventualn\w*)\b")


def heating_start(text: str | None) -> str | None:
    """"PROBE" (tople probe), "SEASON" (the season starting), or None - also None when the notice
    says something is cut, since then people on those streets must be told as usual. A cut only
    in an "if" sentence ("ukoliko izostane isporuka ... prijavite kvar") is not one."""
    t = _fold(text)
    if not t or _says_cut(" ".join(x for x in re.split(r"[.!?;]\s", t) if not _IF.search(x))):
        return None
    if _HEAT_PROBE.search(t):
        return "PROBE"
    if _HEAT_SEASON.search(t):
        return "SEASON"
    return None
