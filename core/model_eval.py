"""Scoring a model's reading of a notice against the answer a person checked (tests/golden/
expected.json) - the model eval. scripts/eval_model.py runs it.

The golden alerts test (tests/test_golden_alerts.py) replays the model's STORED reading, so it
tests our rules, never the model. This is the other half: the model reads the notice afresh and
every field is compared with the checked answer. Run it before switching model or changing the
prompt; a new model has to score at least as well.

What is scored, per notice and in total:
- places: every street / settlement / landmark the notice names. Precision (of the places the
  model read, how many are real) and recall (of the real places, how many it read). A misspelled
  street counts as wrong: it fails the street lookup just like a missing one ("ЦРОВА" for
  "ЦЕРОВА"). Near misses are listed apart, as "spelling".
- segments: a place with every detail right - house range, numbers, odd/even, from/to streets.
- fields: start date, end date, "road closure only", the time windows.
"""
import difflib
import json
import re
import unicodedata

PLACE_FIELDS = ("number_range", "explicit_numbers", "parity", "intersection_start", "intersection_end")


def _latin(text: str) -> str:
    from spatial.spatial_engine import cyrillic_to_latin
    return cyrillic_to_latin(text or "")


def norm(text) -> str:
    """Case, script, diacritics, punctuation and spacing don't count: 'Ул. Футошка' = 'futoska'."""
    t = _latin(str(text or "")).lower()
    t = unicodedata.normalize("NFKD", t.replace("đ", "dj"))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\b(ulica|ul|bulevar|bul)\b\.?", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def _numbers(text) -> str:
    """'22, 22 А, 22Б' = '22,22a,22b': a house letter counts with or without a space before it,
    in either case and script. Only a capital counts after a space, so the 'и' in '22 и 24' is a
    word, not a letter of 22."""
    out = set()
    for num, space, letter in re.findall(r"(\d+)(?:(\s?)([^\W\d_])(?![^\W\d_]))?", str(text or "")):
        if space and not letter.isupper():
            letter = ""
        out.add(num + _latin(letter).lower())
    return ",".join(sorted(out))


def _date(text) -> str:
    """'2026-11-12', '12.11.2026', '12.11.2026.' -> '12.11.2026'; anything else as given."""
    t = str(text or "").strip().rstrip(".")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return f"{int(m[3]):02d}.{int(m[2]):02d}.{m[1]}"
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", t)
    if m:
        return f"{int(m[1]):02d}.{int(m[2]):02d}.{m[3]}"
    return t


def _time(text) -> str:
    return "-".join(f"{int(h):02d}:{m}" for h, m in re.findall(r"(\d{1,2})[:.](\d{2})", str(text or "")))


def place_key(loc: dict) -> str:
    if loc.get("location_type") == "ENTIRE_SETTLEMENT" and not loc.get("main_street"):
        return "naselje " + norm(loc.get("settlement"))
    if loc.get("main_street"):
        return norm(loc["main_street"])
    return "mesto " + norm(loc.get("landmark") or loc.get("settlement"))


def _detail(loc: dict) -> tuple:
    return (place_key(loc),
            loc.get("number_range") and _numbers(loc["number_range"]) or "",
            _numbers(loc.get("explicit_numbers")),
            loc.get("parity") or "ALL",
            norm(loc.get("intersection_start")),
            norm(loc.get("intersection_end")),
            # sends "moguće" alerts to the unnamed side streets, so a wrong flag is a wrong segment
            bool(loc.get("includes_surrounding_streets")))


def _fields(reading: dict) -> dict:
    # Not event_type: the model words it freely ("Kvar na vodovodnoj mreži", "electricity"), and
    # the utility an alert names comes from the scraper's category, never from it.
    return {
        "outage_date": _date(reading.get("outage_date")),
        "end_date": _date(reading.get("end_date")),
        "road_closure_only": bool(reading.get("road_closure_only")),
        "times": ",".join(sorted({_time(l.get("time_window")) for l in reading.get("affected_locations") or []})),
    }


def score(expected: dict, got: dict | None) -> dict:
    """One notice. `got` None = the model gave no answer (scored as missing everything)."""
    exp_locs = expected.get("affected_locations") or []
    got_locs = (got or {}).get("affected_locations") or []
    exp_keys, got_keys = {place_key(l) for l in exp_locs}, {place_key(l) for l in got_locs}
    found, missed, extra = exp_keys & got_keys, exp_keys - got_keys, got_keys - exp_keys
    spelling = [(m, e) for m in sorted(missed) for e in sorted(extra)
                if difflib.SequenceMatcher(None, m, e).ratio() >= 0.8]

    exp_seg = [_detail(l) for l in exp_locs]
    got_seg = [_detail(l) for l in got_locs]
    exact = 0
    pool = list(got_seg)
    for s in exp_seg:
        if s in pool:
            pool.remove(s)
            exact += 1

    ef, gf = _fields(expected), _fields(got or {})
    return {
        "places_expected": len(exp_keys), "places_read": len(got_keys), "places_found": len(found),
        "missed": sorted(missed), "extra": sorted(extra), "spelling": spelling,
        "segments_expected": len(exp_seg), "segments_exact": exact,
        "fields": {k: ef[k] == gf[k] for k in ef},
        "field_values": {k: (ef[k], gf[k]) for k in ef if ef[k] != gf[k]},
        "no_answer": got is None,
    }


def total(results: dict) -> dict:
    """All notices together: micro-averaged, so a notice with 20 streets weighs 20 times one
    with one street - the way alerts are actually sent."""
    r = list(results.values())
    exp = sum(x["places_expected"] for x in r)
    read = sum(x["places_read"] for x in r)
    found = sum(x["places_found"] for x in r)
    seg = sum(x["segments_expected"] for x in r)
    fields = {}
    for x in r:
        for k, ok in x["fields"].items():
            fields.setdefault(k, [0, 0])
            fields[k][0] += ok
            fields[k][1] += 1
    precision = found / read if read else 0.0
    recall = found / exp if exp else 0.0
    return {
        "notices": len(r), "no_answer": sum(x["no_answer"] for x in r),
        "place_precision": precision, "place_recall": recall,
        "place_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "segment_exact": sum(x["segments_exact"] for x in r) / seg if seg else 0.0,
        "fields": {k: ok / n for k, (ok, n) in fields.items()},
        "perfect_notices": sum(1 for x in r if not x["missed"] and not x["extra"]
                               and x["segments_exact"] == x["segments_expected"] and all(x["fields"].values())),
    }


def load_expected(path) -> dict[int, dict]:
    with open(path, encoding="utf-8") as f:
        return {int(k): v for k, v in json.load(f).items()}
