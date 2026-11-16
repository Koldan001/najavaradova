"""The model eval: how well does a model read our notices, field by field, against the answers a
person checked (tests/golden/expected.json)? See core/model_eval.py for what is scored.

    python scripts/eval_model.py --stored                 # the readings stored back then, no calls
    python scripts/eval_model.py gemini-3.5-flash-lite    # read every checked notice afresh
    python scripts/eval_model.py gemini-3.5-flash-lite --notice 626 -v
    python scripts/eval_model.py groq/list                # Groq model names (needs GROQ_API_KEY)
    python scripts/eval_model.py groq/openai/gpt-oss-120b # any Groq model, "groq/" + its name

A model run makes one call per checked notice (38 at most) from that model's own daily quota, and
only notice text is sent. Results are also saved to data/evals/<time>-<model>.json, so two runs
can be compared later. Writes nothing else.
"""
import argparse
import json
import re
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.model_eval import load_expected, score, total

GOLDEN = ROOT / "tests" / "golden"


def pct(x: float) -> str:
    return f"{round(100 * x)}%"


def zone_of(raw_text: str) -> str | None:
    """The nstoplana TO zone a notice names ("конзумном подручју ТО „Југ“"), else None. Such a
    notice's streets come from the zone's table on nstoplana.rs (ns_toplana_scraper.
    _build_zone_alert), never from the model - the text doesn't list them."""
    from scrapers.ns_toplana_scraper import TO_ZONE_PATTERN, TO_ZONE_SLUGS
    m = TO_ZONE_PATTERN.search(raw_text or "")
    name = m and m.group(1).strip().upper()
    return name if name in TO_ZONE_SLUGS else None


_NIGHT = re.compile(r"\d{1,2}\s*/\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{4}\.?\s*године\s*[–-]")


def pipeline_steps(notice: dict, reading):
    """The fixed steps the real pipeline runs on a model's answer before anything is stored, so a
    fresh reading is scored like a real one:
    - BVK multi-night flushing: each stored row is one night ("5/6.11.2026. године – ...") of a
      week-long notice whose opening names the whole week; bvk_scraper sets that night's date and
      drops the end date, because the model reads the week (02.-07.11).
    - an unstated '00:00' start becomes '07:00' (matchmaker._normalize_time_window)."""
    if not reading:
        return reading
    reading = json.loads(json.dumps(reading))
    raw = notice["raw_text"] or ""
    if "bvk.rs" in (notice.get("source_url") or ""):
        from scrapers.bvk_scraper import _line_date
        nights = _NIGHT.findall(raw)
        if len(nights) == 1 and _line_date(nights[0]):
            reading["outage_date"], reading["end_date"] = _line_date(nights[0]), None
    from core.matchmaker import _normalize_time_window
    for loc in reading.get("affected_locations") or []:
        loc["time_window"] = _normalize_time_window(loc.get("time_window"), raw)
    return reading


def as_zone(reading, zone: str):
    """A zone notice scored on what the model does read there - date, end date, time - with its
    street list swapped for the zone as one place."""
    if not reading:
        return reading
    locs = reading.get("affected_locations") or []
    tw = locs[0].get("time_window") if locs else None
    return {**reading, "affected_locations": [{"location_type": "LANDMARK", "landmark": f"zona {zone}",
                                               "parity": "ALL", "time_window": tw}]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", nargs="?", help="Gemini model name to run")
    ap.add_argument("--stored", action="store_true", help="score the stored readings, no model calls")
    ap.add_argument("--notice", type=int, action="append", help="only this notice id (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show every mistake")
    args = ap.parse_args()
    if not args.stored and not args.model:
        ap.error("give a model name, or --stored")
    if args.model == "groq/list":
        from core.other_models import groq_models
        print("\n".join(groq_models()))
        return

    expected_path = GOLDEN / "expected.json"
    if not expected_path.exists():
        sys.exit("tests/golden/expected.json is missing - label the notices first.")
    expected = load_expected(expected_path)
    notices = {n["id"]: n for n in json.loads((GOLDEN / "notices.json").read_text(encoding="utf-8"))}
    ids = [i for i in sorted(expected) if i in notices and (not args.notice or i in args.notice)]

    label = "stored" if args.stored else args.model
    results, seconds = {}, []
    for oid in ids:
        if args.stored:
            got = json.loads(notices[oid]["parsed_json"] or "null")
        else:
            t0 = time.monotonic()
            if args.model.startswith("groq/"):
                from core.other_models import read_groq
                reading = read_groq(notices[oid]["raw_text"], args.model[len("groq/"):])
            else:
                from parser import parse_announcement
                reading = parse_announcement(notices[oid]["raw_text"], max_retries=2, model=args.model)
            seconds.append(time.monotonic() - t0)
            got = reading.model_dump() if reading is not None else None
        want = expected[oid]
        got = pipeline_steps(notices[oid], got)
        zone = zone_of(notices[oid]["raw_text"])
        if zone:
            want, got = as_zone(want, zone), as_zone(got, zone)
        results[oid] = score(want, got)
        r = results[oid]
        if zone:
            print(f"   (#{oid}: TO zone {zone} - streets come from the zone table, only date/time scored)")
        bad = r["missed"] or r["extra"] or r["segments_exact"] < r["segments_expected"] or not all(r["fields"].values())
        mark = "✅" if not bad else ("❌" if r["no_answer"] or r["missed"] else "⚠️")
        print(f"{mark} #{oid}: places {r['places_found']}/{r['places_expected']} found, "
              f"{len(r['extra'])} extra, exact {r['segments_exact']}/{r['segments_expected']}"
              + (f", fields wrong: {', '.join(r['field_values'])}" if r["field_values"] else ""))
        if args.verbose and bad:
            for m in r["missed"]:
                print(f"      missed: {m}")
            for e in r["extra"]:
                print(f"      extra:  {e}")
            for m, e in r["spelling"]:
                print(f"      spelling? expected '{m}', read '{e}'")
            for k, (want, have) in r["field_values"].items():
                print(f"      {k}: expected {want!r}, read {have!r}")

    t = total(results)
    print()
    print(f"{label}: {t['notices']} notices, {t['perfect_notices']} read perfectly"
          + (f", {t['no_answer']} no answer" if t["no_answer"] else ""))
    print(f"  places: precision {pct(t['place_precision'])} · recall {pct(t['place_recall'])} · "
          f"F1 {pct(t['place_f1'])}")
    print(f"  segments with every detail right: {pct(t['segment_exact'])}")
    print("  fields: " + " · ".join(f"{k} {pct(v)}" for k, v in t["fields"].items()))
    if seconds:
        print(f"  time per notice: {sum(seconds) / len(seconds):.1f}s average, {max(seconds):.1f}s slowest")

    out = ROOT / "data" / "evals"
    try:
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{datetime.now():%Y-%m-%d_%H%M}-{label.replace('/', '_')}.json"
        path.write_text(json.dumps({"label": label, "total": t, "notices": results}, ensure_ascii=False,
                                   indent=1), encoding="utf-8")
        print(f"  saved: data/evals/{path.name}")
    except OSError as e:
        print(f"  (not saved: {e})")


if __name__ == "__main__":
    main()
