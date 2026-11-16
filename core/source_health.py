"""Per-source scrape health - so a dead or changed source site is noticed, not silently skipped.

Every scraper swallows its own errors (one broken site must not stop the other 18), and none of
them treat an HTTP error status as an error at all. So main.py's "failed" count only ever saw a
scraper that crashed outright - a source answering 404 (nitoplana moved its news page) or 403
(vodovodle blocking our User-Agent) looked exactly like "ran fine, nothing new today", for weeks.

This watches each scraper run from the outside, without touching the 19 scrapers:
- every HTTP request it makes (requests' Session.request is wrapped for the duration of the run),
  so an error status or a connection failure is recorded even when the scraper swallows it -
  and every one of them asks for a fresh copy (FRESH_HEADERS);
- how many announcements it saw (see _ITEM_HOOKS - the per-announcement functions every scraper
  calls, new or already stored, before deduping).

Results accumulate in data/source_health.json; workers/watchdog.py turns them into operator
alerts: a source down several runs in a row, or a source that has seen no announcements at all
for days (the page loads but the layout changed and the selectors now match nothing).
"""
import json
import sys
import time
from contextlib import contextmanager

import requests

import config

HEALTH_FILE = config.DATA_DIR / "source_health.json"

# main.py runs every ~15 min: 3 failed runs in a row = down for ~45 min, not one blip.
DOWN_AFTER_RUNS = 3
# No announcement seen at all for this long = the page probably changed. Small utilities can go
# a week or two without posting, and a few scrapers skip non-outage posts before counting, so the
# default is generous; EPS publishes every single day.
QUIET_AFTER_DAYS = 14
QUIET_AFTER_DAYS_BY_SOURCE = {"eps": 2}

# Module-level functions every scraper calls once per announcement it finds, before deciding it
# is already stored: compute_text_hash (all of them), and _source_url_already_stored (the three
# that dedupe by article URL first and so never reach the hash for an old article).
_ITEM_HOOKS = ("compute_text_hash", "_source_url_already_stored")

# Sent with every scraper request. BVK's nginx cache (X-Cache-Status: STALE) kept serving its
# 29.09.2026 copy of both its pages to plain requests for four weeks, while a browser got the
# live list: each run saw ~10 announcements, all already stored, so nothing looked wrong and
# Belgrade water went unreported until 26.10. A no-cache request gets the live page.
FRESH_HEADERS = {"Cache-Control": "no-cache", "Pragma": "no-cache"}


@contextmanager
def probe(run, on_activity=None):
    """Record the HTTP traffic and announcement count of one scraper run. Yields the stats dict;
    everything is restored on exit, including when the scraper raises.

    on_activity, if given, is called on every request and every announcement - main.py's liveness
    beat, so a long but healthy run (EPS posting a whole day's list at once) is told apart from a
    hung one."""
    stats = {"requests": 0, "ok_responses": 0, "errors": [], "items": 0}
    beat = on_activity or (lambda: None)

    original_request = requests.sessions.Session.request

    def recording_request(self, method, url, *args, **kwargs):
        beat()
        stats["requests"] += 1
        # The scraper's own headers win on any overlap.
        kwargs["headers"] = {**FRESH_HEADERS, **(kwargs.get("headers") or {})}
        try:
            response = original_request(self, method, url, *args, **kwargs)
        except Exception as e:
            stats["errors"].append(f"{type(e).__name__}: {url}")
            raise
        if response.status_code >= 400:
            stats["errors"].append(f"HTTP {response.status_code}: {url}")
        else:
            stats["ok_responses"] += 1
        return response

    module = sys.modules.get(getattr(run, "__module__", ""))
    originals = {n: getattr(module, n) for n in _ITEM_HOOKS if callable(getattr(module, n, None))}

    def counting(fn):
        def wrapper(*args, **kwargs):
            beat()
            stats["items"] += 1
            return fn(*args, **kwargs)
        return wrapper

    requests.sessions.Session.request = recording_request
    for n, fn in originals.items():
        setattr(module, n, counting(fn))
    try:
        yield stats
    finally:
        requests.sessions.Session.request = original_request
        for n, fn in originals.items():
            setattr(module, n, fn)


def load() -> dict:
    try:
        return json.loads(HEALTH_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def record(health: dict, name: str, stats: dict, crashed: bool, now: float | None = None) -> dict:
    """Fold one run into `name`'s entry. A run counts as failed when the scraper crashed, or when
    it made requests and not a single one succeeded (the list page itself is down/blocked). A
    run with some successes and some errors (one detail page 404) is healthy - the error is kept
    for context only. A run that made no requests at all changes nothing."""
    now = time.time() if now is None else now
    entry = health.setdefault(name, {"tracked_since": now})
    entry["last_run"] = now
    entry["last_items_count"] = stats["items"]
    if stats["errors"]:
        entry["last_error"] = stats["errors"][0]

    failed = crashed or (stats["requests"] > 0 and stats["ok_responses"] == 0)
    if failed:
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    elif stats["requests"] > 0:
        entry["consecutive_failures"] = 0
        entry["last_ok"] = now
    if stats["items"] > 0:
        entry["last_items"] = now
    return health


def save(health: dict) -> None:
    tmp = HEALTH_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(health, indent=1, sort_keys=True))
    tmp.replace(HEALTH_FILE)


def problems(health: dict, now: float | None = None) -> tuple[dict, dict]:
    """(down, quiet): name -> human-readable reason, for every source currently in trouble."""
    now = time.time() if now is None else now
    down, quiet = {}, {}
    for name, e in health.items():
        n = e.get("consecutive_failures", 0)
        if n >= DOWN_AFTER_RUNS:
            down[name] = f"{n} runs in a row failed - {e.get('last_error') or 'scraper crashed'}"
            continue  # a down source is also quiet; one alert is enough
        days = QUIET_AFTER_DAYS_BY_SOURCE.get(name, QUIET_AFTER_DAYS)
        since = next((e[k] for k in ("last_items", "tracked_since") if e.get(k) is not None), now)
        idle = (now - since) / 86400
        if idle >= days:
            quiet[name] = (f"no announcements seen for {idle:.0f} days (page loads, but the "
                           f"layout may have changed)")
    return down, quiet
