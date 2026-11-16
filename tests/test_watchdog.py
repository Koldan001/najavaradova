"""workers/watchdog.py check_pipeline: a long but healthy scrape cycle is not "container down"
(the false alarms of 21 and 24 Sep 2026); a hung or dead one still is."""
import json
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from workers import watchdog

NOW = 1_000_000.0
MIN = 60


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    """Point the watchdog at temp status/progress files and record alerts instead of sending."""
    monkeypatch.setattr(watchdog, "STATUS_FILE", tmp_path / "pipeline_status.json")
    monkeypatch.setattr(watchdog, "PROGRESS_FILE", tmp_path / "pipeline_progress.json")
    sent, cleared = [], []
    monkeypatch.setattr(watchdog, "send_operator_alert", lambda key, text, **kw: sent.append((key, text)))
    monkeypatch.setattr(watchdog, "clear_alert", lambda key, *a, **kw: cleared.append(key))

    def write(completed_ago=None, progress_ago=None, cycle_started_ago=None, step="eps"):
        if completed_ago is not None:
            watchdog.STATUS_FILE.write_text(json.dumps({"ts": NOW - completed_ago,
                                                        "scrapers_failed": 0}))
        if progress_ago is not None:
            watchdog.PROGRESS_FILE.write_text(json.dumps({
                "ts": NOW - progress_ago, "step": step,
                "cycle_started": NOW - cycle_started_ago}))
        watchdog.check_pipeline(now=NOW)
        return {k for k, _ in sent}, sent, cleared

    return write


def test_long_healthy_cycle_is_not_an_alarm(pipeline):
    # Last finish 50 min ago (the old rule fired at 45), but EPS beat a minute ago.
    keys, _, cleared = pipeline(completed_ago=50 * MIN, progress_ago=1 * MIN, cycle_started_ago=35 * MIN)
    assert "pipeline_heartbeat" not in keys and "pipeline_long_cycle" not in keys
    assert "pipeline_heartbeat" in cleared


def test_no_progress_is_an_alarm_naming_the_step(pipeline):
    keys, sent, _ = pipeline(completed_ago=80 * MIN, progress_ago=46 * MIN, cycle_started_ago=65 * MIN,
                             step="vikkg")
    assert "pipeline_heartbeat" in keys
    assert "vikkg" in dict(sent)["pipeline_heartbeat"]


def test_idle_between_cycles_is_fine(pipeline):
    # Finished 14 min ago, sleeping until the next cycle; the progress file is from that cycle.
    keys, _, _ = pipeline(completed_ago=14 * MIN, progress_ago=15 * MIN, cycle_started_ago=40 * MIN)
    assert not keys


def test_cycle_running_for_hours_is_flagged_separately(pipeline):
    keys, _, _ = pipeline(completed_ago=200 * MIN, progress_ago=1 * MIN, cycle_started_ago=130 * MIN)
    assert keys == {"pipeline_long_cycle"}


def test_nothing_ever_written_is_an_alarm(pipeline):
    keys, _, _ = pipeline()
    assert keys == {"pipeline_heartbeat"}


def test_older_deploy_without_progress_file_still_works(pipeline):
    assert pipeline(completed_ago=10 * MIN)[0] == set()
    assert "pipeline_heartbeat" in pipeline(completed_ago=50 * MIN)[0]


@pytest.fixture()
def outages_db(monkeypatch):
    """An in-memory outages table for check_stale_sources; alerts recorded instead of sent."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE outages (source_url TEXT, created_at TIMESTAMP)")

    class _Shared:  # check_stale_sources closes its connection; keep ours open across calls
        def execute(self, *a):
            return conn.execute(*a)

        def close(self):
            pass

    monkeypatch.setattr(watchdog, "get_connection", lambda: _Shared())
    sent, cleared = [], []
    monkeypatch.setattr(watchdog, "send_operator_alert", lambda key, text, **kw: sent.append(key))
    monkeypatch.setattr(watchdog, "clear_alert", lambda key, *a, **kw: cleared.append(key))
    return conn, sent, cleared


def _ts(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_daily_source_with_no_new_outage_is_stale(outages_db):
    # BVK, 29.09-26.10.2026: page loaded fine, nothing new stored for weeks.
    conn, sent, cleared = outages_db
    day = 86400
    conn.execute("INSERT INTO outages VALUES (?, ?)", ("https://www.bvk.rs/kvarovi-na-mrezi/", _ts(NOW - 3 * day)))
    conn.execute("INSERT INTO outages VALUES (?, ?)", ("https://elektrodistribucija.rs/x.htm", _ts(NOW - 1 * day)))
    watchdog.check_stale_sources(now=NOW)
    assert sent == ["source_stale:https://www.bvk.rs/kvarovi-na-mrezi/"]
    assert "source_stale:https://elektrodistribucija.rs/" in cleared


def test_fresh_and_never_seen_sources_are_quiet(outages_db):
    conn, sent, _ = outages_db
    conn.execute("INSERT INTO outages VALUES (?, ?)", ("https://www.bvk.rs/kvarovi-na-mrezi/", _ts(NOW - 3600)))
    watchdog.check_stale_sources(now=NOW)
    assert sent == []
