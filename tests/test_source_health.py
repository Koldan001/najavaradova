"""core/source_health.py: a scraper that swallows its own HTTP errors must still show up as down,
a quiet source must be flagged, and probe() must leave requests/the scraper module untouched."""
import os
import sys
import types

import pytest
import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core import source_health


class _Resp:
    def __init__(self, status):
        self.status_code = status


@pytest.fixture()
def fake_http(monkeypatch):
    """Session.request answers from a url -> status map (an Exception instance = raise it)."""
    routes = {}

    def fake_request(self, method, url, *a, **kw):
        result = routes[url]
        if isinstance(result, Exception):
            raise result
        return _Resp(result)

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)
    return routes


def _scraper_module(name, body):
    """A throwaway module shaped like a real scraper: module-level compute_text_hash, and a
    run_scraper that swallows every error the way the real ones do."""
    mod = types.ModuleType(name)
    mod.compute_text_hash = lambda text: "h"

    def run_scraper():
        try:
            body(mod)
        except Exception:
            pass

    run_scraper.__module__ = name
    mod.run_scraper = run_scraper
    sys.modules[name] = mod
    return mod


def test_swallowed_404_is_recorded(fake_http):
    fake_http["https://x.test/news"] = 404
    mod = _scraper_module("fake_scraper_404", lambda m: requests.get("https://x.test/news"))
    original_request = requests.sessions.Session.request

    with source_health.probe(mod.run_scraper) as stats:
        mod.run_scraper()

    assert stats == {"requests": 1, "ok_responses": 0, "errors": ["HTTP 404: https://x.test/news"],
                     "items": 0}
    assert requests.sessions.Session.request is original_request  # restored


def test_connection_error_is_recorded_even_when_swallowed(fake_http):
    fake_http["https://x.test/"] = requests.ConnectionError("boom")
    mod = _scraper_module("fake_scraper_conn", lambda m: requests.get("https://x.test/"))
    with source_health.probe(mod.run_scraper) as stats:
        mod.run_scraper()
    assert stats["ok_responses"] == 0 and stats["errors"] == ["ConnectionError: https://x.test/"]


def test_items_are_counted_and_hook_restored(fake_http):
    fake_http["https://x.test/list"] = 200

    def body(m):
        requests.get("https://x.test/list")
        for text in ("a", "b", "c"):
            m.compute_text_hash(text)

    mod = _scraper_module("fake_scraper_items", body)
    original_hash = mod.compute_text_hash
    with source_health.probe(mod.run_scraper) as stats:
        mod.run_scraper()
    assert stats["items"] == 3 and stats["ok_responses"] == 1
    assert mod.compute_text_hash is original_hash


def test_on_activity_beats_on_requests_and_items(fake_http):
    """main.py's liveness beat: every request and every announcement counts as progress."""
    fake_http["https://x.test/list"] = 200

    def body(m):
        requests.get("https://x.test/list")
        m.compute_text_hash("a")
        m.compute_text_hash("b")

    mod = _scraper_module("fake_scraper_beat", body)
    beats = []
    with source_health.probe(mod.run_scraper, on_activity=lambda: beats.append(1)):
        mod.run_scraper()
    assert len(beats) == 3


def _run(ok_responses, requests_made=1, items=0, crashed=False):
    return ({"requests": requests_made, "ok_responses": ok_responses,
             "errors": [] if ok_responses else ["HTTP 403: https://x.test/"], "items": items}, crashed)


def test_down_only_after_consecutive_failures_and_recovers():
    health, t = {}, 1_000_000.0
    for i in range(source_health.DOWN_AFTER_RUNS - 1):
        source_health.record(health, "le", *_run(0), now=t + i)
    assert source_health.problems(health, now=t + 10)[0] == {}

    source_health.record(health, "le", *_run(0), now=t + 20)
    down, _ = source_health.problems(health, now=t + 21)
    assert "le" in down and "HTTP 403" in down["le"]

    source_health.record(health, "le", *_run(1, items=1), now=t + 30)  # one good run clears it
    assert source_health.problems(health, now=t + 31) == ({}, {})


def test_partial_errors_are_not_down():
    """A list page that loads with one broken detail page is a healthy source."""
    health = {}
    for i in range(5):
        source_health.record(health, "kv", {"requests": 3, "ok_responses": 2,
                                            "errors": ["HTTP 404: https://x.test/img"], "items": 1},
                             crashed=False, now=100.0 + i)
    assert source_health.problems(health, now=106.0) == ({}, {})


def test_quiet_source_flagged_after_threshold():
    day = 86400
    health = {}
    source_health.record(health, "vr", *_run(1, items=2), now=0.0)
    source_health.record(health, "vr", *_run(1, items=0), now=5 * day)
    assert source_health.problems(health, now=5 * day)[1] == {}

    later = (source_health.QUIET_AFTER_DAYS + 1) * day
    source_health.record(health, "vr", *_run(1, items=0), now=later)
    assert "vr" in source_health.problems(health, now=later)[1]


def test_eps_has_a_shorter_quiet_threshold():
    day = 86400
    health = {}
    source_health.record(health, "eps", *_run(1, items=40), now=0.0)
    assert "eps" in source_health.problems(health, now=3 * day)[1]


def test_every_request_asks_for_a_fresh_copy(monkeypatch):
    # BVK's cache served a 4-week-old page to plain requests (26.10.2026); no-cache got the live one.
    seen = []

    def fake_request(self, method, url, *a, **kw):
        seen.append(kw.get("headers"))
        return _Resp(200)

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)

    def body(m):
        requests.get("https://x.test/list", headers={"User-Agent": "UA"})
        requests.get("https://x.test/other")

    mod = _scraper_module("fake_scraper_fresh", body)
    with source_health.probe(mod.run_scraper):
        mod.run_scraper()
    assert seen[0] == {"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "UA"}
    assert seen[1]["Cache-Control"] == "no-cache"
