"""Operator watchdog - the thing that notices when the pipeline breaks.

Every WATCHDOG_INTERVAL seconds it checks four signals and DMs the operator (core/alerts.py,
hourly cooldown per problem, recovery notice on clear):

  1. Pipeline heartbeat  - main.py is making progress (data/pipeline_progress.json, written at
                           every step and while a scraper works) and no one cycle runs for hours;
                           data/pipeline_status.json carries the last finished cycle's summary.
  2. Scraper health      - last run's failed-count is 0; publish_web_data succeeded; and per
                           source (core/source_health.py): not failing run after run, and still
                           yielding announcements; and the daily sources still yielding NEW
                           outages (check_stale_sources).
  3. Send queue          - nothing QUEUED for too long (sender down); FAILED count not climbing.
  4. Backup freshness    - data/.last_backup_ok (touched by deploy/backup_to_r2.sh) is recent.

Runs as its own compose service. Read-only against the app data except for alert_log.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
from core import source_health
from core.alerts import clear_alert, send_operator_alert, send_operator_note
from core.database import get_connection

INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "300"))
# No progress at all for this long = the `scrapers` container is down or hung. main.py beats at
# every step and at least once a minute while a scraper is busy, so a long but healthy cycle
# never trips this - it used to measure "no finished cycle", and did (21 and 24 Sep 2026).
HEARTBEAT_MAX_AGE = int(os.environ.get("WATCHDOG_HEARTBEAT_MAX_AGE", "2700"))
# A cycle still making progress but running this long is looping somewhere; every other source
# waits for it.
CYCLE_MAX_AGE = int(os.environ.get("WATCHDOG_CYCLE_MAX_AGE", "7200"))
QUEUE_STUCK_AGE = int(os.environ.get("WATCHDOG_QUEUE_STUCK_AGE", "1800"))
BACKUP_MAX_AGE = int(os.environ.get("WATCHDOG_BACKUP_MAX_AGE", str(26 * 3600)))
# Sources that publish every day or two, keyed by source_url prefix. No NEW outage stored from one
# for this many days means we are reading a page that no longer changes. check_sources can't see
# that: BVK's stale cache (29.09-26.10.2026) loaded fine with ~10 announcements every run, all
# already stored, and four weeks of Belgrade water went unreported. Each threshold sits above the
# longest gap that source had between 8 Sep and 26 Oct 2026. Seasonal and small sources are left
# to source_health's "no announcements at all" check.
STALE_AFTER_DAYS = {
    "https://www.bvk.rs/kvarovi-na-mrezi/": 2,
    "https://elektrodistribucija.rs/": 4,
    "https://jkpnaissus.co.rs/": 5,
    "https://vodovodks.rs/": 6,  # was www.vodovodks.co.rs until 31.10.2026
    "https://www.vikns.rs/": 7,
    "https://vodovodca.rs/": 7,
    "https://jkpvik-kg.com/": 12,
}

STATUS_FILE = config.DATA_DIR / "pipeline_status.json"
PROGRESS_FILE = config.DATA_DIR / "pipeline_progress.json"
BACKUP_MARKER = config.DATA_DIR / ".last_backup_ok"
# High-water marks for the "someone signed up" DM. Polled here rather than hooked into the
# signup paths themselves: registration is the one flow that must never break, and a poll
# catches every route into the DB (web confirm, dashboard add, Telegram attach) with one check.
SIGNUP_MARKER = config.DATA_DIR / ".last_seen_signup"


def _read_json(path) -> dict | None:
    """None when the file doesn't exist; {} when it can't be read (logged, not alerted)."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"watchdog: unreadable {path}: {e}")
        return {}


def check_pipeline(now: float | None = None):
    now = time.time() if now is None else now
    s = _read_json(STATUS_FILE)
    p = _read_json(PROGRESS_FILE)
    if s is None and p is None:
        send_operator_alert(
            "pipeline_heartbeat",
            "Pipeline status file missing - main.py has not completed a run since deploy. "
            "Check the `scrapers` container.",
        )
        return
    s, p = s or {}, p or {}

    completed = float(s.get("ts", 0))
    last_progress = max(completed, float(p.get("ts", 0)))
    step = p.get("step") or "unknown"
    idle = now - last_progress
    if idle > HEARTBEAT_MAX_AGE:
        send_operator_alert(
            "pipeline_heartbeat",
            f"Pipeline has made no progress for {int(idle // 60)} min "
            f"(threshold {HEARTBEAT_MAX_AGE // 60} min, last step: {step}). "
            f"The `scrapers` container may be down or hung.",
        )
    else:
        clear_alert("pipeline_heartbeat", "Pipeline is running again.")

    started = float(p.get("cycle_started", 0))
    running_for = now - started
    if started > completed and running_for > CYCLE_MAX_AGE:
        send_operator_alert(
            "pipeline_long_cycle",
            f"One scrape cycle has been running for {int(running_for // 60)} min (now at: {step}). "
            f"Still making progress, but every other source waits until it finishes.",
        )
    else:
        clear_alert("pipeline_long_cycle", "The long scrape cycle finished.")

    failed_names = s.get("failed_names") or []
    if s.get("scrapers_failed", 0) > 0:
        send_operator_alert(
            "scrapers_failed",
            f"Last scrape run: {s['scrapers_failed']} scraper(s) failed "
            f"({', '.join(failed_names) or 'names unavailable'}). "
            f"{s.get('scrapers_ok', '?')} ok.",
        )
    else:
        clear_alert("scrapers_failed", "All scrapers ran clean on the last cycle.")

    if not s.get("publish_ok", True):
        send_operator_alert(
            "publish_failed",
            "Last run: publish_web_data failed - the live map did not update this cycle.",
        )
    else:
        clear_alert("publish_failed")


def check_queue():
    conn = get_connection()
    stuck = conn.execute(
        # 'SENDING' too: a row a worker claimed and then crashed on sits there until the next
        # run's reclaim_stale_sending() (5 min); a pipeline actually wedged still trips this.
        # A row held for the morning (Telegram quiet hours, core/delivery) is due at its
        # scheduled_for, not when it was queued - held overnight is not stuck.
        "SELECT COUNT(*) AS c FROM message_queue "
        "WHERE status IN ('QUEUED', 'SENDING') AND created_at IS NOT NULL "
        "AND COALESCE(scheduled_for, created_at) < datetime('now', ?)",
        (f"-{QUEUE_STUCK_AGE} seconds",),
    ).fetchone()["c"]
    failed = conn.execute(
        "SELECT COUNT(*) AS c FROM message_queue WHERE status = 'FAILED'"
    ).fetchone()["c"]
    conn.close()

    if stuck > 0:
        send_operator_alert(
            "queue_stuck",
            f"{stuck} message(s) have been QUEUED for over {QUEUE_STUCK_AGE // 60} min - "
            f"the `sender` / `email_sender` worker is not draining the queue.",
        )
    else:
        clear_alert("queue_stuck", "Send queue is draining again.")

    if failed > 0:
        send_operator_alert(
            "queue_failed",
            f"{failed} message(s) in FAILED state - notifications that were never delivered. "
            f"Check the sender logs.",
            cooldown_s=6 * 3600,
        )


def check_backup():
    if not BACKUP_MARKER.exists():
        send_operator_alert(
            "backup_stale",
            "No successful R2 backup marker yet (data/.last_backup_ok). "
            "Run deploy/backup_to_r2.sh and check deploy/backup.env.",
        )
        return
    age = time.time() - BACKUP_MARKER.stat().st_mtime
    if age > BACKUP_MAX_AGE:
        send_operator_alert(
            "backup_stale",
            f"Last successful R2 backup was {int(age // 3600)}h ago "
            f"(threshold {BACKUP_MAX_AGE // 3600}h). The nightly systemd timer may be failing.",
        )
    else:
        clear_alert("backup_stale", "Nightly R2 backup is current again.")


def check_new_signups():
    """DM the operator when someone registers. First run just records where the DB is now, so
    enabling this never replays every existing subscriber as "new"."""
    from core.admin_stats import format_new, new_since

    try:
        seen = json.loads(SIGNUP_MARKER.read_text())
        last_sub = int(seen.get("subscriber_id", 0))
        last_tgt = int(seen.get("target_id", 0))
        first_run = False
    except Exception:
        last_sub = last_tgt = 0
        first_run = True

    feed = new_since(last_sub, last_tgt)
    marks = {"subscriber_id": feed["max_subscriber_id"], "target_id": feed["max_target_id"]}

    if first_run:
        SIGNUP_MARKER.write_text(json.dumps(marks))
        print(f"watchdog: signup baseline recorded {marks}")
        return

    text = format_new(feed)
    if not text:
        return
    # Only advance the marker once the DM actually went out, so a Telegram blip re-announces
    # next cycle instead of silently swallowing a signup.
    if send_operator_note(text):
        SIGNUP_MARKER.write_text(json.dumps(marks))
    else:
        print("watchdog: new signups found but the DM failed - will retry next cycle")


def check_sources():
    """Per-source health from data/source_health.json (written by main.py, see
    core/source_health.py). Catches what check_pipeline can't: a scraper that "ran fine" while
    its site answered 404/403, or loads but no longer yields a single announcement."""
    health = source_health.load()
    if not health:
        return
    down, quiet = source_health.problems(health)
    for name in health:
        if name in down:
            # Every 6 h, not hourly - it's the utility's site, nothing to do but wait, and the
            # "responding again" note below still arrives the moment it's back.
            send_operator_alert(f"source_down:{name}", f"Source {name} is DOWN: {down[name]}",
                                cooldown_s=6 * 3600)
        else:
            clear_alert(f"source_down:{name}", f"Source {name} is responding again.")
        if name in quiet:
            # Daily, not hourly - a quiet source is a "look at it today" problem, not an outage.
            send_operator_alert(f"source_quiet:{name}", f"Source {name}: {quiet[name]}",
                                cooldown_s=86400)
        else:
            clear_alert(f"source_quiet:{name}", f"Source {name} is producing announcements again.")


def check_stale_sources(now: float | None = None):
    """A daily source with no new outage stored for STALE_AFTER_DAYS: the page still loads and
    still lists announcements (so check_sources is happy), but it has stopped changing - a stale
    cache, or new posts going somewhere our scraper doesn't look."""
    now = time.time() if now is None else now
    conn = get_connection()
    try:
        for prefix, days in STALE_AFTER_DAYS.items():
            newest = conn.execute(
                "SELECT MAX(created_at) FROM outages WHERE substr(source_url, 1, ?) = ?",
                (len(prefix), prefix)).fetchone()[0]
            if not newest:
                continue  # never stored anything from it - nothing to compare against
            # created_at is SQLite CURRENT_TIMESTAMP: UTC, "YYYY-MM-DD HH:MM:SS"
            idle = (now - datetime.fromisoformat(newest).replace(tzinfo=timezone.utc).timestamp()) / 86400
            key = f"source_stale:{prefix}"
            if idle >= days:
                # Daily, like a quiet source: look at it today, it won't fix itself.
                send_operator_alert(
                    key,
                    f"No new outage from {prefix} for {idle:.0f} days (it normally posts every day "
                    f"or two). Open the page in a browser and compare: a stale cache or a moved "
                    f"page looks exactly like this.",
                    cooldown_s=86400)
            else:
                clear_alert(key, f"New outages are coming from {prefix} again.")
    finally:
        conn.close()


def run_once():
    for fn in (check_pipeline, check_sources, check_stale_sources, check_queue, check_backup,
               check_new_signups):
        try:
            fn()
        except Exception as e:
            print(f"watchdog: {fn.__name__} raised {e!r}")


def main():
    print(
        f"watchdog up - interval {INTERVAL}s, heartbeat<{HEARTBEAT_MAX_AGE}s, "
        f"cycle<{CYCLE_MAX_AGE}s, "
        f"queue<{QUEUE_STUCK_AGE}s, backup<{BACKUP_MAX_AGE}s"
    )
    while True:
        run_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
