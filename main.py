"""Orchestrator: runs every scraper, one after another, isolated so one broken site can't take
down the rest of the run. Each scraper's own run_scraper() already calls
core/matchmaker.py's process_and_queue_outage() internally and dedupes exact re-scrapes via
raw_text_hash - this file's only job is triggering all of them on a schedule.

Sending is NOT triggered here on purpose - workers/telegram_worker.py runs as its own
continuously-looping service (see docker-compose.yml's `sender`), polling message_queue
independently. Decoupling scrape-cadence from send-cadence means a slow/stuck scraper never
delays delivery of outages already queued by the others.

Usage: python main.py  (intended to be re-run every 5-10 min - see docker-compose.yml's
`scrapers` service for the actual schedule)
"""
import json
import logging
import sys
import time

import config
from core import source_health
from scrapers.NS_water_scraper import run_scraper as run_ns_water
from scrapers.beoelektrane_scraper import run_scraper as run_beoelektrane
from scrapers.bvk_kvarovi_scraper import run_scraper as run_bvk_kvarovi
from scrapers.bvk_scraper import run_scraper as run_bvk
from scrapers.energetika_kg_scraper import run_scraper as run_energetika_kg
from scrapers.eps_scraper import run_eps_scraper as run_eps
from scrapers.naissus_scraper import run_scraper as run_naissus
from scrapers.nitoplana_scraper import run_scraper as run_nitoplana
from scrapers.ns_toplana_scraper import run_scraper as run_ns_toplana
from scrapers.toplana_zr_scraper import run_scraper as run_toplana_zr
from scrapers.toplanakrusevac_scraper import run_scraper as run_toplanakrusevac
from scrapers.vikkg_scraper import run_scraper as run_vikkg
from scrapers.vikzr_scraper import run_scraper as run_vikzr
from scrapers.vodovodca_scraper import run_scraper as run_vodovodca
from scrapers.vodovodkv_scraper import run_scraper as run_vodovodkv
from scrapers.vodovodle_scraper import run_scraper as run_vodovodle
from scrapers.vodovodks_scraper import run_scraper as run_vodovodks
from scrapers.vodovodpa_scraper import run_scraper as run_vodovodpa
from scrapers.vodovodva_scraper import run_scraper as run_vodovodva
from scrapers.jpkikinda_scraper import run_scraper as run_jpkikinda
from scrapers.vodovodsabac_scraper import run_scraper as run_vodovodsabac
from scrapers.vodovodvranje_scraper import run_scraper as run_vodovodvranje
from core.retention import prune_ephemeral
from core.signup_reminder import send_confirm_reminders, send_signup_reminders
from scripts.map_targets import map_unmapped_targets
from scripts.publish_web_data import main as publish_web_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# name -> callable. A plain list, not a dict comprehension over the imports above, so a scraper
# can be commented out here (e.g. temporarily broken upstream site) without touching its import.
SCRAPERS = [
    ("NS_water", run_ns_water),
    ("beoelektrane", run_beoelektrane),
    ("bvk_kvarovi", run_bvk_kvarovi),
    ("bvk", run_bvk),
    ("energetika_kg", run_energetika_kg),
    ("eps", run_eps),
    ("naissus", run_naissus),
    ("nitoplana", run_nitoplana),
    ("ns_toplana", run_ns_toplana),
    ("toplana_zr", run_toplana_zr),
    ("toplanakrusevac", run_toplanakrusevac),
    ("vikkg", run_vikkg),
    ("vikzr", run_vikzr),
    ("vodovodca", run_vodovodca),
    ("vodovodks", run_vodovodks),
    ("vodovodkv", run_vodovodkv),
    ("vodovodle", run_vodovodle),
    ("vodovodpa", run_vodovodpa),
    ("vodovodva", run_vodovodva),
    ("jpkikinda", run_jpkikinda),
    ("vodovodsabac", run_vodovodsabac),
    ("vodovodvranje", run_vodovodvranje),
]

# Liveness for workers/watchdog.py, separate from pipeline_status.json (which only a finished
# cycle writes). A cycle can legitimately run past half an hour - EPS posts the next day's whole
# list at once, and every new row is a paced Gemini call plus geocoding - and the watchdog used to
# read that as "container down" (21 and 24 Sep 2026). It now alerts on "no progress", not on "no
# finished cycle".
PROGRESS_FILE = config.DATA_DIR / "pipeline_progress.json"
BEAT_EVERY_S = 60  # at most one progress write a minute while a scraper is busy


def _write_json_atomic(target, data: dict) -> None:
    """tmp + replace, so the watchdog never reads a half-written file."""
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(target)


class _Progress:
    """Writes {"ts", "step", "cycle_started"} to PROGRESS_FILE: at every step, and on every
    request / announcement inside a scraper (via source_health.probe's on_activity), throttled."""

    def __init__(self):
        self.cycle_started = time.time()
        self.step = "start"
        self._last_write = 0.0

    def at(self, step: str) -> None:
        self.step = step
        self.beat(force=True)

    def beat(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_write < BEAT_EVERY_S:
            return
        self._last_write = now
        try:
            _write_json_atomic(PROGRESS_FILE, {"ts": now, "step": self.step,
                                               "cycle_started": self.cycle_started})
        except Exception:
            logging.exception("⚠️ could not write pipeline_progress.json")


def main():
    logging.info(f"Starting Outage Notifier pipeline - {len(SCRAPERS)} scrapers...")
    progress = _Progress()
    progress.at("map_unmapped_targets")

    # Map any target that registered since the last run onto the H3 grid BEFORE the scrapers,
    # so an outage matched this cycle can actually see it. Registration also maps + backfills
    # inline (workers/telegram_bot.py, scripts/local_web_server.py); this is the safety net for
    # anything that path missed (resolution transient, DB busy, older signups).
    try:
        logging.info("-> map_unmapped_targets")
        map_unmapped_targets()
    except Exception:
        logging.exception("⚠️ map_unmapped_targets failed, continuing - scrapers still run")

    ok, failed, failed_names = 0, 0, []
    health = source_health.load()
    for name, run in SCRAPERS:
        crashed = False
        progress.at(name)
        # probe() sees what the scraper itself swallows: HTTP error statuses, connection
        # failures, and how many announcements it found (see core/source_health.py).
        with source_health.probe(run, on_activity=progress.beat) as stats:
            try:
                logging.info(f"-> {name}")
                run()
                ok += 1
            except Exception:
                # One scraper's dead upstream site (layout change, timeout, 500) must never stop
                # the others from running - each is an independent HTTP source with its own
                # failure mode.
                logging.exception(f"⚠️ {name} scraper failed, continuing with the rest")
                crashed = True
                failed += 1
                failed_names.append(name)
        source_health.record(health, name, stats, crashed)
        if stats["errors"]:
            logging.warning(f"⚠️ {name}: {len(stats['errors'])} HTTP error(s), first: {stats['errors'][0]}")
    try:
        source_health.save(health)
    except Exception:
        logging.exception("⚠️ could not write source_health.json - watchdog keeps the previous view")
    logging.info(f"Pipeline finished. {ok} ok, {failed} failed.")

    # Housekeeping: drop spent/expired auth artifacts (login_tokens, sessions, stale
    # pending_registrations). Narrow on purpose - never touches outages or message_queue. Own
    # try/except so a locked DB here never blocks the map publish below.
    try:
        pruned = prune_ephemeral()
        if any(pruned.values()):
            logging.info(f"-> prune_ephemeral {pruned}")
    except Exception:
        logging.exception("⚠️ prune_ephemeral failed - non-fatal, retried next cycle")

    # One reminder email for a Telegram signup that never pressed Start, and one for a signup that
    # never clicked its confirmation link (core/signup_reminder.py).
    try:
        reminded = send_signup_reminders()
        if reminded:
            logging.info(f"-> signup reminders sent: {reminded}")
        confirmed = send_confirm_reminders()
        if confirmed:
            logging.info(f"-> confirm reminders sent: {confirmed}")
    except Exception:
        logging.exception("⚠️ signup reminders failed - non-fatal, retried next cycle")

    # Held alerts the scrape path didn't settle (a later signup's backfill, a crash): their second
    # look (core/send_check.py).
    try:
        from core import send_check
        checked = send_check.run_pending()
        if checked:
            logging.info(f"-> send checks run: {checked}")
    except Exception:
        logging.exception("⚠️ send checks failed - non-fatal, retried next cycle")

    # Every recent alert read a second time by another model, for the accuracy figure in /stats
    # (core/alert_check.py). Shadow mode: nothing waits on it.
    try:
        from core import alert_check
        judged = alert_check.run_pending()
        if judged:
            logging.info(f"-> alert checks: {judged}")
    except Exception:
        logging.exception("⚠️ alert checks failed - non-fatal, retried next cycle")

    # The operator's weekly review (core/review.py): Monday from 09:00, once a week, a random
    # sample of last week's alerts and notices to label in Telegram - the ground truth for /stats.
    try:
        from core import review
        reviewed = review.run_due()
        if reviewed:
            logging.info(f"-> weekly review sent: {reviewed} items")
    except Exception:
        logging.exception("⚠️ weekly review failed - non-fatal, retried next cycle")

    # Scraping alone never touched the live map before this - web/outages.json only updated
    # when someone ran scripts/publish_web_data.py by hand. Folding it in here means every
    # scheduled scrape cycle also pushes the current DB state to the site automatically.
    publish_ok = True
    progress.at("publish_web_data")
    try:
        logging.info("-> publish_web_data")
        # It reports a failed upload (Worker down, bad token, HTTP 5xx) by returning 1, not by
        # raising - read that too, or the map goes stale with the watchdog none the wiser.
        if publish_web_data() != 0:
            publish_ok = False
            logging.error("⚠️ publish_web_data failed - the live map didn't update this cycle")
    except Exception:
        publish_ok = False
        logging.exception("⚠️ publish_web_data failed - scrapers still ran fine, map just didn't update this cycle")

    # Run summary for workers/watchdog.py. Written last, so a fresh timestamp means "a full cycle
    # actually completed".
    try:
        _write_json_atomic(config.DATA_DIR / "pipeline_status.json", {
            "ts": time.time(),
            "scrapers_ok": ok,
            "scrapers_failed": failed,
            "failed_names": failed_names,
            "publish_ok": publish_ok,
        })
    except Exception:
        logging.exception("⚠️ could not write pipeline_status.json - watchdog will see a stale heartbeat")


if __name__ == "__main__":
    main()
