import os
from pathlib import Path
from dotenv import load_dotenv

# Base Directories
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = BASE_DIR / "cache"

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Load variables from a local .env file (gitignored) into the environment
load_dotenv(BASE_DIR / ".env")

# Centralized Database & Resource Paths
OUTAGES_DB_PATH = DATA_DIR / "outage_notifier.db"
SERBIA_STREETS_DB_PATH = DATA_DIR / "serbia_streets.db"

# Credentials & Parser Config
API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-3.5-flash-lite")
# Per-call limit (parser.py). A normal call takes ~2 s; an overloaded model has held one for ~10 min.
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "60"))
# Used only to retry an outage that came back fully UNRESOLVED (see core/matchmaker.py) - a
# stronger, pricier tier that's worth paying for on the rare retry, not on every call.
GEMINI_FALLBACK_MODEL_NAME = os.environ.get("GEMINI_FALLBACK_MODEL_NAME", "gemini-3.5-flash")

# Cascade of models tried in order on a real quota-exhausted response (429/RESOURCE_EXHAUSTED),
# used by parser.py's default (non-overridden) call path only - matchmaker.py's explicit retry
# with GEMINI_FALLBACK_MODEL_NAME above is untouched by this, still a single specific tier.
# MODEL_NAME is always tried first regardless of whether it's also listed here. Order matters:
# put higher-RPD/free tiers first. Comma-separated env override if the free lineup shifts again.
# gemini-3.5-flash is last, not second: 20 calls a day on the free tier, and on 24 Sep 2026 it
# took ~10 min per answer while the lite models were "503 high demand" - the lite tiers are the
# ones with room to absorb a primary outage.
# Only models that AI Studio's rate-limit page shows with a free quota AND that the API accepts
# (30 Sep 2026: 500 + 500 + 20 calls a day, about twice what the pipeline uses). gemini-2.5-flash-lite
# is still listed there, but the API answers "404 ... no longer available to new users", and
# "gemini-3-flash" is not in the API's model list.
GEMINI_MODEL_CASCADE = [
    m.strip() for m in os.environ.get(
        "GEMINI_MODEL_CASCADE",
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash"
    ).split(",") if m.strip()
]

# Per-model RPM ceiling for the cascade above. Only gemini-3.5-flash-lite (primary) and
# gemini-3.5-flash (fallback) have been confirmed against AI Studio's real dashboard (see
# GEMINI_PRIMARY_RPM_LIMIT / GEMINI_FALLBACK_RPM_LIMIT below) - every other cascade tier gets a
# deliberately conservative guessed default rather than a confirmed number, same honesty as
# core/api_budget.py's own docstring about not hardcoding unconfirmed limits.
GEMINI_CASCADE_DEFAULT_RPM = int(os.environ.get("GEMINI_CASCADE_DEFAULT_RPM", "5"))

# Hard daily cap on GEMINI_FALLBACK_MODEL_NAME calls (see core/api_budget.py). Confirmed real
# quota from AI Studio's rate-limit view (project "ViberNotifier"): gemini-3.5-flash free tier is
# RPD=20, RPM=5. Capped at 15 here, not 20, to leave headroom for the account's other real usage
# already showing on that same dashboard (2/20 at the time this was checked) rather than racing
# this scraper's own budget right up against the account-wide ceiling.
GEMINI_FALLBACK_DAILY_LIMIT = int(os.environ.get("GEMINI_FALLBACK_DAILY_LIMIT", "15"))
# How GEMINI_FALLBACK_DAILY_LIMIT is shared between its two users. The retry of a fully unresolved
# outage gets the rest, since it decides real alerts; the second reading (core/alert_check.py)
# only measures, and falls back to a lite model once its share is spent.
GEMINI_FALLBACK_CHECK_SHARE = int(os.environ.get("GEMINI_FALLBACK_CHECK_SHARE", "5"))
GEMINI_FALLBACK_RETRY_SHARE = max(0, GEMINI_FALLBACK_DAILY_LIMIT - GEMINI_FALLBACK_CHECK_SHARE)

# 👍 / 👎 under each alert (core/feedback.py): Telegram buttons and the email line. Off until the
# operator turns it on (FEEDBACK_BUTTONS=1 in .env): at a few dozen subscribers it would get a
# handful of answers a week, and the weekly review (core/review.py) is the real measure.
FEEDBACK_BUTTONS = os.environ.get("FEEDBACK_BUTTONS", "0") == "1"

# Confirmed real RPM ceilings (AI Studio, same dashboard) - used by core/rate_limiter.py to pace
# calls proactively instead of firing as fast as the network allows and reacting to 429s after
# the fact.
GEMINI_PRIMARY_RPM_LIMIT = int(os.environ.get("GEMINI_PRIMARY_RPM_LIMIT", "15"))
GEMINI_FALLBACK_RPM_LIMIT = int(os.environ.get("GEMINI_FALLBACK_RPM_LIMIT", "5"))

if not API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is not set. Create a .env file in the project root "
        "(see .env.example) or export it in your environment."
    )

# Telegram delivery. Not validated here (unlike API_KEY above) - only telegram_worker.py and
# the registration bot need it, and every scraper/parser script also imports config, so making
# this required would break scraper-only runs that have no reason to touch Telegram at all.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Telegram chat id that operational alerts go to (workers/watchdog.py via core/alerts.py):
# pipeline stalled, a scraper failed, the send queue is stuck, the nightly backup is stale.
# Unset -> alerts are only logged, never sent. Get the numeric id from @userinfobot.
OPERATOR_CHAT_ID = os.environ.get("OPERATOR_CHAT_ID")

# Email delivery via Brevo's free-tier SMTP relay (300/day, permanent free tier - not a trial).
# Not validated here, same reasoning as TELEGRAM_BOT_TOKEN above - only workers/email_worker.py
# needs these.
BREVO_SMTP_HOST = os.environ.get("BREVO_SMTP_HOST", "smtp-relay.brevo.com")
BREVO_SMTP_PORT = int(os.environ.get("BREVO_SMTP_PORT", "587"))
BREVO_SMTP_LOGIN = os.environ.get("BREVO_SMTP_LOGIN")
BREVO_SMTP_KEY = os.environ.get("BREVO_SMTP_KEY")
# The From: address on outgoing mail - must be a sender verified in Brevo's dashboard first, or
# Brevo rejects the send. Not necessarily the same as BREVO_SMTP_LOGIN (the account you log in
# with) - Brevo lets one account send from multiple verified senders.
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM_ADDRESS")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "NajavaRadova.rs")
# Magic-link login/confirm mail. Kept as its own From: so login and outage-notification
# reputation don't share a sender - abuse or a spam-trap hit on one doesn't drag the other's
# deliverability down. Falls back to the notification sender when unset, so nothing breaks
# before the second sender is verified in Brevo (see .env.example).
LOGIN_EMAIL_FROM_ADDRESS = os.environ.get("LOGIN_EMAIL_FROM_ADDRESS") or EMAIL_FROM_ADDRESS
LOGIN_EMAIL_FROM_NAME = os.environ.get("LOGIN_EMAIL_FROM_NAME", EMAIL_FROM_NAME)

# Scraper Targets
TARGET_URL = "https://www.vikns.rs/category/najava-radova-sr/?pismo=lat"