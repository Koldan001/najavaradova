"""Registration + account API - serves web/ as static files (same as `python -m http.server
--directory web`), GET /api/* lookups against the RGZ address registry (data/rgz.db) that power
the form's city/street/number autocomplete and validation, and the account/auth surface:

    POST /auth/start           - signup: address + email + channel -> magic-link email
    POST /auth/login           - re-login: email only -> magic-link email
    GET  /auth/verify?token=   - the emailed link: shows a one-button confirm page (does NOT
                                  spend the token - mail scanners follow links, and they GET)
    POST /auth/verify?token=   - the button: consumes the token, sets the session cookie,
                                  finishes the pending signup (if any), redirects to /dashboard
    POST /auth/resend?token=   - an expired, unclicked link: mails a fresh one to the same address
    GET  /dashboard            - the account page itself (app/dashboard.html, NOT under web/ -
                                  see that file's own comment for why)
    GET  /api/me               - the logged-in subscriber's targets, session-cookie gated
    GET  /api/session          - {"logged_in": bool} only, always 200 (web/signup.js)
    POST /api/targets          - add another address once logged in (no new email needed)
    POST /api/targets/toggle   - flip one owned target's is_active (master on/off for the address)
    POST /api/targets/toggle-channel - flip email_active or telegram_active on one owned target
    POST /api/targets/delete   - permanently remove ONE owned address (hard delete, unlike the
                                   is_active=0 pause /api/targets/toggle does)
    POST /api/targets/add-email      - attach the account's own (already-verified) email to a
                                         Telegram-only target, no confirmation round trip
    POST /api/targets/add-telegram   - deep-link to attach Telegram to an email-only target
                                         (same pending_registrations flow as a new signup)
    POST /api/delete-account   - hard-delete everything for the logged-in subscriber
    POST /auth/logout
    GET  /unsubscribe?token=   - no-login unsubscribe: shows a confirm button (does NOT mutate)
    POST /unsubscribe?token=   - performs the unsubscribe; also the RFC 8058 one-click target
                                  (List-Unsubscribe-Post) mail clients POST to directly
    GET  /tacno?o=&t=&v=y|n    - a reader's 👍 / 👎 from an alert email: only asks (does NOT mutate)
    POST /tacno?o=&t=&v=y|n    - stores the answer (core/feedback.py)

No passwords anywhere - see core/auth.py for the token/session model. There used to be an open,
no-login POST /register here; it's been replaced by /auth/start now that every signup requires a
confirmed account (see PLAN.md's subscriber design) - nothing else in the repo still calls the
old route.

Runs both locally (python scripts/local_web_server.py, http://localhost:8000, same-origin so no
CORS needed) and as the `api` service in docker-compose.yml on the VM, where the deployed
web/index.html reaches it cross-origin over a real subdomain (see API_BASE in web/index.html and
ALLOWED_ORIGINS below). The dashboard is different: it's served BY this same process at
app.najavaradova.rs/dashboard, so its own fetches to /api/me etc. are same-origin - no CORS, no
Access-Control-Allow-Credentials needed for the cookie to work there.

Usage: python scripts/local_web_server.py
Then open http://localhost:8000
"""
import html
import ipaddress
import json
import re
import secrets
import socketserver
import sqlite3
import sys
import os
import threading
import time
from collections import defaultdict
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(REPO_ROOT)

import config
from core.database import get_connection
from core import auth, managers
from core.serbia_places import OPSTINE as RS_OPSTINE

WEB_DIR = os.path.join(REPO_ROOT, "web")
RGZ_DB_PATH = os.path.join(REPO_ROOT, "data", "rgz.db")
# Deliberately NOT under web/ - that whole directory is also synced as-is to the static
# Cloudflare Pages site (najavaradova.rs), which has no backend and can't share the session
# cookie (that only works same-origin with app.najavaradova.rs, which is this process). A file
# with the same name in both places would silently be two different, confusing pages.
DASHBOARD_PATH = os.path.join(REPO_ROOT, "app", "dashboard.html")

SESSION_COOKIE_NAME = "session"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# 1-4 digits, optionally with one trailing letter suffix (any script - the RGZ registry has both
# Latin and Cyrillic, e.g. "8A" / "8А") - or the literal "BB" (bez broja - a real, common Serbian
# address form for a building with no assigned number, not a placeholder). Previously digits-only
# by design, which silently rejected both of these as if they weren't real addresses.
_BROJ_NUM_RE = re.compile(r"^(\d{1,4})(\D)?$")


def valid_broj(broj: str) -> bool:
    b = (broj or "").strip().upper()
    if b == "BB":
        return True
    m = _BROJ_NUM_RE.match(b)
    return bool(m) and (m.group(2) is None or m.group(2).isalpha())

# Prep for when the frontend (Cloudflare Pages) and this API live on different origins - not
# needed while everything's served same-origin off localhost, but the real deployment will need
# it immediately, and it's cheap to have ready now rather than discovered as a mystery failure.
ALLOWED_ORIGINS = {
    "https://najavaradova.rs",
    "https://www.najavaradova.rs",
    "https://outage-notifier.pages.dev",
    "http://localhost:8000",
}

# Bare in-memory per-IP throttle - a single-process dev server has no shared state to worry
# about, so a plain dict is enough. Not meant to survive a restart or scale past one process;
# the real deployment should use something sturdier (e.g. Cloudflare rate limiting rules) but
# this stops a naive script from hammering the endpoint today.
#
# Two buckets, deliberately: the PRE-auth surface (/auth/start, /auth/login - anyone on the
# internet, each hit claims a fresh identity and sends an email) is the one that has to be
# tight, and is the whole reason mandatory login exists. An already-logged-in caller adding
# Telegram to their OWN addresses is a different risk profile entirely - bounded by their own
# account size, no fresh identity claimed - so it gets a roomier limit that a real manager
# onboarding 10-20 addresses in one sitting won't trip.
#
# The bucket key MUST be the visitor's real IP, not the TCP peer. In production this process
# only ever receives connections from Caddy over the Docker network (reverse_proxy api:8000),
# so self.client_address[0] is Caddy's container IP - identical for every request on earth.
# Keying the throttle on it turns PREAUTH_RATE_LIMIT_MAX into one global budget: five requests
# total per window, and any one script locks out every real user. _client_ip() below recovers
# the real address from the proxy chain instead. Trust order: CF-Connecting-IP (Cloudflare
# sets and overwrites it, and the origin is only reachable through Cloudflare - it presents a
# Cloudflare Origin CA cert), then the left-most X-Forwarded-For hop, then the TCP peer for
# local runs where neither header exists.
# Hard ceiling on any request body this process will read into memory. Every real payload here
# is a small JSON object (an address form, a target id); the biggest is the dashboard's
# add-address form with a 2-item channels array, well under a kilobyte. Without a cap, an
# unauthenticated POST can announce Content-Length: 500000000 and make the server buffer half a
# gigabyte before it ever parses - a handful of those OOM-kill the container. Anything over this
# is refused as a bad request (see _read_json).
MAX_BODY_BYTES = 64 * 1024

_PROXY_IP_HEADERS = ("CF-Connecting-IP", "X-Forwarded-For")


def _client_ip_from(headers, peer_ip):
    for name in _PROXY_IP_HEADERS:
        raw = headers.get(name)
        if not raw:
            continue
        candidate = raw.split(",")[0].strip()
        # X-Forwarded-For entries are bare IPs, but be tolerant of an ipv4:port or [ipv6]:port.
        if candidate.startswith("["):
            candidate = candidate[1:].split("]")[0]
        elif candidate.count(":") == 1:
            candidate = candidate.split(":")[0]
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return peer_ip


RATE_LIMIT_WINDOW_SECONDS = 600
PREAUTH_RATE_LIMIT_MAX = 5
AUTHED_RATE_LIMIT_MAX = 40
_preauth_hits = defaultdict(list)
_authed_hits = defaultdict(list)


# /api/upravnici/check: a manager re-checks a list a few times while fixing lines.
MANAGER_CHECK_RATE_LIMIT_MAX = 30
_manager_check_hits = defaultdict(list)


def _throttle(bucket, ip, limit):
    now = time.time()
    hits = [t for t in bucket[ip] if now - t < RATE_LIMIT_WINDOW_SECONDS]
    hits.append(now)
    bucket[ip] = hits
    return len(hits) > limit


def rate_limited(ip):
    """Pre-auth surface (/auth/start, /auth/login)."""
    return _throttle(_preauth_hits, ip, PREAUTH_RATE_LIMIT_MAX)


def authed_rate_limited(ip):
    """Logged-in self-service actions (e.g. adding a Telegram deep-link for an owned address)."""
    return _throttle(_authed_hits, ip, AUTHED_RATE_LIMIT_MAX)


# Coverage honesty (PLAN.md §3) - which opština actually get a live utility-outage feed, vs.
# which only have RGZ street data so a power outage (EPS) *might* match. Verified 2026-09-04:
# EPS's 5 regional pages (see scrapers/eps_scraper.py) are effectively nationwide/rotating, so
# the real limiter for a *usable* registration is RGZ street coverage (37 opština, see
# data/rgz.db) - a naselje outside that set can't pick a real address at all (city_exists()
# below already returns False for it). Within the 37, "full" also has a dedicated water/heating
# scraper; "partial" is EPS-power-only. Curated display names (not DB-derived) for the site's
# covered-areas list and the modal.
FULL_COVERAGE_DISPLAY = [
    "Beograd", "Novi Sad", "Niš", "Kragujevac", "Zrenjanin",
    "Pančevo", "Čačak", "Kraljevo", "Leskovac", "Vranje", "Kruševac", "Valjevo",
    "Kikinda", "Šabac",
]
PARTIAL_COVERAGE_DISPLAY = [
    "Jagodina", "Vršac", "Zaječar", "Novi Pazar",
    "Smederevo", "Sombor", "Užice", "Subotica", "Sremska Mitrovica", "Pirot",
]
# opstina_lat values (data/rgz.db) backing FULL_COVERAGE_DISPLAY - used to tell a full-coverage
# registration from a power-only one so /register can attach the right soft notice.
FULL_COVERAGE_OPSTINE = {
    "NOVI SAD", "KRAGUJEVAC", "ZRENJANIN", "PANČEVO", "ČAČAK", "KRALJEVO", "LESKOVAC", "VRANJE",
    "KRUŠEVAC", "VALJEVO", "KIKINDA", "ŠABAC",
    # Beograd's rgz.db opština (grad Beograd has no single "BEOGRAD" opstina_lat row - it's
    # split into these sub-opštine, see PRAGMA check 2026-09-04):
    "VOŽDOVAC", "ZVEZDARA", "ZEMUN", "NOVI BEOGRAD", "PALILULA (BEOGRAD)", "RAKOVICA",
    "SAVSKI VENAC", "STARI GRAD", "ČUKARICA", "VRAČAR", "SURČIN",
    # Niš, same story:
    "MEDIJANA", "NIŠKA BANJA", "PALILULA (NIŠ)", "PANTELEJ", "CRVENI KRST",
}
PARTIAL_COVERAGE_OPSTINE = {
    "JAGODINA", "VRŠAC", "ZAJEČAR", "NOVI PAZAR",
    "SMEDEREVO", "SOMBOR", "UŽICE", "SUBOTICA", "SREMSKA MITROVICA", "PIROT",
}

BOT_USERNAME = None


def get_bot_username():
    global BOT_USERNAME
    if BOT_USERNAME is None:
        res = requests.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe", timeout=10)
        data = res.json()
        if not data.get("ok"):
            raise RuntimeError(f"getMe failed: {data}")
        BOT_USERNAME = data["result"]["username"]
    return BOT_USERNAME


def get_rgz_connection():
    conn = sqlite3.connect(RGZ_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


_CYR = {"а":"a","б":"b","в":"v","г":"g","д":"d","ђ":"dj","е":"e","ж":"z","з":"z","и":"i","ј":"j",
        "к":"k","л":"l","љ":"lj","м":"m","н":"n","њ":"nj","о":"o","п":"p","р":"r","с":"s","т":"t",
        "ћ":"c","у":"u","ф":"f","х":"h","ц":"c","ч":"c","џ":"dz","ш":"s"}
_LAT = {"č":"c","ć":"c","ž":"z","š":"s","đ":"dj"}

# Cyrillic -> Serbian Latin keeping diacritics, for DISPLAY only (the confirm/unsubscribe card).
# fold() above deliberately drops diacritics for matching; this one keeps them so the label
# reads like the dashboard ("Futoška", "Novi Sad"). Canonical copies:
# spatial/spatial_engine.cyrillic_to_latin and scripts/build_web_data.CYR_TO_LAT - kept local
# and tiny here so the registration server needn't import the spatial stack for one label.
_CYR_DISPLAY = {"а":"a","б":"b","в":"v","г":"g","д":"d","ђ":"đ","е":"e","ж":"ž","з":"z","и":"i",
                "ј":"j","к":"k","л":"l","љ":"lj","м":"m","н":"n","њ":"nj","о":"o","п":"p","р":"r",
                "с":"s","т":"t","ћ":"ć","у":"u","ф":"f","х":"h","ц":"c","ч":"č","џ":"dž","ш":"š"}


def _display_latin(s: str) -> str:
    """Title-cased Serbian Latin for a stored (often Cyrillic, upper-cased) name."""
    return "".join(_CYR_DISPLAY.get(ch, ch) for ch in (s or "").lower()).title()


# One email can be about several of a person's addresses in the same outage
# (workers/email_worker.run_worker), so its unsubscribe link carries one token per address
# (?token=a&token=b) and turns them all off. Capped - no email covers more addresses than this.
UNSUB_TOKENS_MAX = 50


def _unsub_tokens(qs) -> list[str]:
    out = []
    for token in qs.get("token") or []:
        token = token.strip()
        if token and token not in out:
            out.append(token)
    return out[:UNSUB_TOKENS_MAX]


def fold(s: str) -> str:
    """Plain-ASCII lowercase form: Cyrillic -> Latin, Serbian diacritics stripped. Mirrors the
    JS fold() in web/index.html so "indjija"/"Инђија" both match "INĐIJA", "cacak" matches
    "ČAČAK", etc. Used for diacritic/script-insensitive street and city matching."""
    return "".join(_CYR.get(ch) or _LAT.get(ch) or ch for ch in (s or "").lower())


def _name_matches(folded_name: str, folded_q: str) -> bool:
    """folded_q is a prefix of folded_name or of any word inside it."""
    return folded_name.startswith(folded_q) or (" " + folded_q) in folded_name


_naselje_cache = None


def _resolve_naselje(conn, typed: str):
    """Map whatever the form sent for `city` to a real naselje_lat value, fold()'d - so a
    non-canonical spelling ("indjija", "Инђија", "stara pazova") still resolves. Exact
    (upper) match first; then a unique fold match; then, for a big-city district typed on its
    own ("Novi Beograd", "Zemun"), the compound "<GRAD> (<DISTRICT>)" form RGZ files it under -
    but only when that maps to exactly one naselje (a bare "Palilula" is both a Beograd and a
    Niš one, so it stays unresolved). None if nothing / ambiguous."""
    global _naselje_cache
    up = typed.strip().upper()
    if _naselje_cache is None:
        _naselje_cache = [r[0] for r in conn.execute(
            "SELECT DISTINCT naselje_lat FROM rgz_streets"
        ).fetchall()]
    if up in _naselje_cache:
        return up
    ft = fold(typed)
    hits = [n for n in _naselje_cache if fold(n) == ft]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        paren = [n for n in _naselje_cache if fold(n).endswith("(" + ft + ")")]
        if len(paren) == 1:
            return paren[0]
    return None


def street_exists(city, ulica):
    """Case-insensitive check that `ulica` is a real street name in the RGZ registry for the
    given place - the server-side half of the "limits" ask, not just client-side polish.

    `city` is a naselje_lat value (actual place/town name - "PALIĆ", "BEOGRAD (ZEMUN)"), not
    opstina_lat (administrative municipality, only 38 of those and missing exactly the names
    people actually type - no plain "Beograd", no "Palić" at all). naselje_lat has ~1200 real
    place names and is also the finer-grained, more accurate scope for the street list itself.
    """
    conn = get_rgz_connection()
    naselje = _resolve_naselje(conn, city)
    if not naselje:
        conn.close()
        return False
    fu = fold(ulica)
    names = conn.execute(
        "SELECT DISTINCT name_lat FROM rgz_streets WHERE naselje_lat = ?", (naselje,)
    ).fetchall()
    conn.close()
    return any(fold(r[0]) == fu for r in names)


# Names people type for a city RGZ files under its Serbian one. Keys are fold()ed.
CITY_ALIASES = {"belgrade": "Beograd", "belgrad": "Beograd", "nish": "Niš"}


# Opštine of Beograd / Niš whose places RGZ files under their own name, not "BEOGRAD (...)" -
# people living there still write the big city ("Beograd, Grocka" for Kaluđerica, 27.10.2026).
CITY_OUTER_OPSTINE = {
    "beograd": {"GROCKA", "BARAJEVO", "LAZAREVAC", "MLADENOVAC", "OBRENOVAC", "SOPOT", "SURČIN"},
    "nis": {"NIŠKA BANJA"},
}
# "Beograd, Grocka", "Grocka - Kaluđerica", "Kaluđerica/Grocka" - the pieces of a typed place.
_PLACE_SPLIT_RE = re.compile(r"[,;/()]|\s[-–—]\s|(?<=\w)-(?=\w)")

_naselje_opstina_cache = None


def _naselje_opstina(conn) -> dict:
    """naselje_lat -> opstina_lat for every place RGZ has streets for."""
    global _naselje_opstina_cache
    if _naselje_opstina_cache is None:
        _naselje_opstina_cache = {
            r[0]: r[1] for r in conn.execute("SELECT DISTINCT naselje_lat, opstina_lat FROM rgz_streets")
        }
    return _naselje_opstina_cache


def _words(s: str) -> str:
    """" kaludjerica grocka " - fold()ed, punctuation to spaces, padded, for whole-word `in`."""
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", fold(s)).split()) + " "


def _named_naselja(conn, typed: str) -> tuple:
    """The RGZ places a typed city text names, as (direct, wider). Direct: places named
    themselves, as whole words anywhere in the text ("Kaluđerica", "Grocka", the district
    "Zemun"). Wider: everything in an opština the text names ("Grocka" also means Kaluđerica,
    Vinča...) or in a big city it names (its districts and outer opštine)."""
    words = _words(typed)
    for alias, city in CITY_ALIASES.items():
        words = words.replace(" " + alias + " ", _words(city))
    big = [c for c in ("beograd", "nis") if " " + c + " " in words]
    outer = set().union(*(CITY_OUTER_OPSTINE[c] for c in big)) if big else set()

    def named(name):
        key = _words(name)
        return len(key.strip()) >= 3 and key in words

    direct, wider = set(), set()
    for naselje, opstina in _naselje_opstina(conn).items():
        city, _, district = naselje.partition(" (")
        if named(district.rstrip(")")) if district else named(naselje):
            direct.add(naselje)
        if (named(opstina.partition(" (")[0]) or opstina in outer
                or (district and fold(city) in big)):
            wider.add(naselje)
    # A place named next to an opština or big city must lie in it: "Jabuka, Grocka" is not the
    # Jabuka near Pančevo.
    by_opstina = _naselje_opstina(conn)
    if wider:
        areas = {by_opstina[n] for n in wider}
        direct = {n for n in direct if by_opstina[n] in areas}
    return direct, wider


def _places_with_street(conn, naselja, ulica: str, broj: str = "") -> list:
    """[(naselje, RGZ street name)] for the places among `naselja` that have street `ulica`. When
    that is more than one place and a house number is given, only the places where that number
    exists on the street (Kaluđerica has Vojvode Stepe Stepanovića 9К, Boleč only 9)."""
    if not naselja:
        return []
    fu = fold(ulica)
    marks = ",".join("?" * len(naselja))
    streets = [r for r in conn.execute(
        f"SELECT ulica_maticni_broj, name_lat, naselje_lat FROM rgz_streets WHERE naselje_lat IN ({marks})",
        list(naselja)).fetchall() if fold(r[1]) == fu]
    places = {}
    for sid, name, naselje in streets:
        places.setdefault(naselje, (name, []))[1].append(sid)
    if len(places) > 1 and broj and broj.upper() != "BB":
        fb = fold(broj)
        def has_number(ids):
            marks = ",".join("?" * len(ids))
            return any(fold(r[0]) == fb for r in conn.execute(
                f"SELECT kucni_broj FROM rgz_housenumbers WHERE ulica_maticni_broj IN ({marks})", ids))
        places = {n: v for n, v in places.items() if has_number(v[1])}
    return [(n, v[0]) for n, v in sorted(places.items())]


def resolve_address(city: str, ulica: str, broj: str = "") -> tuple:
    """(city, ulica) as RGZ knows them, for an address typed without picking a suggestion.

    Beograd and Niš exist in RGZ only by district ("BEOGRAD (ČUKARICA)"), and a village is filed
    under its own name, not its opština's. 19.10.2026: a Čukarica street saved as "Belgrade";
    27.10.2026: a Kaluđerica street saved as "Beograd, Grocka". Both were called uncovered, never
    reached the map, and their owners got no alerts. So when the typed place does not have the
    street, the places the text names are searched for it - first the ones named directly, then
    every place in a named opština or big city, plus the rest of a resolved place's opština (see
    _named_naselja) - narrowed by house number when needed. Exactly one place -> that place and
    its RGZ street name. None or several -> unchanged, never a guess; the caller's coverage and
    street checks then decide.

    A place RGZ knows is saved in its own spelling, whatever case the client sent: the
    dashboard saved "Beograd (novi Beograd)" for months (27.10.2026)."""
    conn = get_rgz_connection()
    try:
        naselje = _resolve_naselje(conn, city)
        if naselje and _places_with_street(conn, [naselje], ulica):
            return naselje.title(), ulica
        if any(_words(o) in _words(city) for o in uncovered_opstine()):
            return city, ulica   # "Jabuka, Prijepolje": a place we have no streets for
        direct, wider = _named_naselja(conn, city)
        if naselje:
            by_opstina = _naselje_opstina(conn)
            direct.add(naselje)
            wider |= {n for n, o in by_opstina.items() if o == by_opstina[naselje]}
        for places in (direct, direct | wider):
            hits = _places_with_street(conn, sorted(places), ulica, broj)
            if len(hits) == 1:
                place, name = hits[0]
                return place.title(), name.title()
            if hits:
                break   # several places have it - a wider search only adds more
        return city, ulica
    finally:
        conn.close()


def resolve_city(city: str, ulica: str, broj: str = "") -> str:
    return resolve_address(city, ulica, broj)[0]


def covered_places_named(city: str) -> list:
    """Places we have streets for that a typed city still names, after resolve_address could not
    place the address - "Beograd, Grocka" -> [Beograd, Grocka]. Such an address must be picked
    from the suggestions, never saved as "uncovered" (that is what hid the Kaluđerica address).
    Empty when the text also names an opština we have no streets for ("Jabuka, Prijepolje": a
    real uncovered village sharing a covered one's name)."""
    words = _words(city)
    if any(_words(o) in words for o in uncovered_opstine()):
        return []
    conn = get_rgz_connection()
    try:
        direct, wider = _named_naselja(conn, city)
        by_opstina = _naselje_opstina(conn)
    finally:
        conn.close()
    names = {n.title() for n in direct}
    names |= {by_opstina[n].partition(" (")[0].title() for n in wider
              if _words(by_opstina[n].partition(" (")[0]) in words}
    names |= {c.title() for c in ("Beograd", "Niš") if _words(c) in words and wider}
    return sorted(names)


_uncovered_opstine_cache = None


def uncovered_opstine() -> list:
    """Serbian opštine we have no RGZ streets for - the "nostreet" half of /api/cities. A
    covered place's name ("Grocka", "Zemun", "Beograd") is never in it."""
    global _uncovered_opstine_cache
    if _uncovered_opstine_cache is None:
        conn = get_rgz_connection()
        try:
            by_opstina = _naselje_opstina(conn)
        finally:
            conn.close()
        # Drop an opština already covered: a place of that name ("Grocka"), a big city RGZ
        # stores by district ("beograd (" / "niš ("), a district ("Zemun"), or the opština
        # itself ("Palilula" - both Beograd's and Niš's, so the form offers the two districts).
        covered = {fold(n) for n in by_opstina}
        covered_prefixes = {f.split(" (", 1)[0] for f in covered if " (" in f}
        covered_suffixes = {f[f.index("(") + 1:-1].strip() for f in covered if " (" in f and f.endswith(")")}
        covered_opstine = {fold(o).split(" (", 1)[0] for o in by_opstina.values()}
        _uncovered_opstine_cache = [
            o for o in RS_OPSTINE
            if not {fold(o)} & (covered | covered_prefixes | covered_suffixes | covered_opstine)
        ]
    return _uncovered_opstine_cache


def normal_case(text: str) -> str:
    """A hand-typed place or street in ALL CAPS or all lower case, saved as "Selevac" / "Kneza
    Milosa" - the notice e-mails and the dashboard show it as typed (01.11.2026: "SELEVAC, KNEZA
    MILOSA 47"). Mixed case is the person's own spelling and is kept."""
    text = (text or "").strip()
    if text != text.upper() and text != text.lower():
        return text
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), text)


def check_address(city: str, ulica: str, broj: str, ack_uncovered: bool) -> tuple:
    """The one gate every way of adding an address goes through (signup, dashboard, bulk
    loader). Returns (city, ulica, None) as they should be saved, or (None, None, error dict).

    "Uncovered" is only for a place we truly have no streets for. A typed place that names one
    we do have ("Beograd, Grocka") is resolved to the real place, or refused until it is picked
    from the suggestions - the "I understand it isn't covered" box can't save it, whatever the
    client sends (27.10.2026: that box saved a Kaluđerica address no alert could ever reach)."""
    city, ulica = resolve_address(city, ulica, broj)
    tier = coverage_tier(city)
    if tier == "none":
        named = covered_places_named(city)
        if named:
            return None, None, {
                "error": f"Imamo spisak ulica za {', '.join(named[:3])} - izaberi tačno mesto "
                         f"(selo ili deo grada) iz predloga, pa ulicu iz predloga.",
                "code": "PICK_PLACE",
            }
        if not ack_uncovered:
            return None, None, {
                "error": "Ova lokacija trenutno nije pokrivena - nemamo ulice za nju u bazi.",
                "code": "UNCOVERED",
            }
        city, ulica = normal_case(city), normal_case(ulica)
    elif not street_exists(city, ulica):
        return None, None, {"error": "Ulica nije prepoznata - izaberi je iz predloga."}
    if not valid_broj(broj):
        return None, None, {"error": "Neispravan broj (npr. 12, 12a ili bb)."}
    return city, ulica, None


def saved_address(typed: dict, city: str, ulica: str, broj: str) -> dict:
    """The address as it was saved, for the form to show right away - "Vojvode Stepe
    Stepanovića 9K, Kaluđerica (opština Grocka)" - and whether check_address moved it from what
    was typed ("Beograd, Grocka"), so the visitor sees the change before confirming."""
    conn = get_rgz_connection()
    try:
        naselje = _resolve_naselje(conn, city)
        opstina = _naselje_opstina(conn).get(naselje) if naselje else None
    finally:
        conn.close()
    place = city.strip()
    if opstina and "(" not in place and fold(opstina) != fold(place):
        place = f"{place} (opština {opstina.partition(' (')[0].title()})"
    changed = (fold(typed.get("city") or "").strip() != fold(city).strip()
               or fold(typed.get("ulica") or "").strip() != fold(ulica).strip())
    return {"label": f"{ulica} {broj}, {place}", "changed": changed}


# ---------- A manager's pasted list (/upravnici) ----------

# "Bul. oslobođenja" / "Ul. Futoška" as people write them, to the registry's form.
_STREET_ABBR = [(re.compile(r"^(bul|blvd|bulev)\.?\s+", re.I), "Bulevar "),
                (re.compile(r"^(ul\.|ulica\s)\s*", re.I), "")]
# "Futoška 15", "Narodnog fronta 12 a", "Kisačka br. 99", "Kamenjar 2 13" (a street named with
# a number) - the street, then its house number.
_LINE_RE = re.compile(r"^(?P<ulica>.*?[^\W\d_].*?)[\s,]+(?:br\.?\s*)?(?P<broj>\d{1,4}\s?[^\W\d_]?|bb)\.?$",
                      re.I | re.U)
# A spreadsheet's header row ("Ulica ⇥ Broj ⇥ Grad") - skipped, not reported as a bad address.
_HEADER_WORDS = {"ulica", "adresa", "broj", "kucni", "grad", "mesto", "naselje", "zgrada", "br"}


def _clean_broj(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").upper()


def _split_line(line: str, default_city: str):
    """(city, ulica, broj) out of one pasted line, or None for a header row. Spreadsheet columns
    arrive tab-separated: the number is its own cell, a cell naming a place is the city, the rest
    is the street. Plain text is "street number[, city]". A line without its own city gets
    `default_city`. ulica/broj come back empty when the line has no house number."""
    cells = [c.strip() for c in line.split("\t") if c.strip()]
    if not any(ch.isdigit() for ch in line) and set(_words(line).split()) <= _HEADER_WORDS:
        return None
    if len(cells) > 1:
        broj = next((c for c in cells if valid_broj(_clean_broj(c))), None)
        if broj:
            rest = [c for c in cells if c is not broj]
            city = default_city
            if len(rest) > 1:
                conn = get_rgz_connection()
                try:
                    places = [c for c in rest if _resolve_naselje(conn, c) or covered_places_named(c)]
                finally:
                    conn.close()
                if places:
                    city = places[-1]
                    rest = [c for c in rest if c is not city]
            return city, " ".join(rest), _clean_broj(broj)
        line = ", ".join(cells)
    head, _, tail = line.partition(",")
    m = _LINE_RE.match(head.strip())
    city = tail.strip() or default_city
    if not m:
        return city, head.strip(), ""
    return city, m["ulica"].strip(" ,"), _clean_broj(m["broj"])


def _expand_street(ulica: str) -> str:
    for rx, repl in _STREET_ABBR:
        ulica = rx.sub(repl, ulica)
    return ulica.strip()


def _house_in_registry(city: str, ulica: str, broj: str) -> bool:
    if broj.upper() == "BB":
        return True
    conn = get_rgz_connection()
    try:
        naselje = _resolve_naselje(conn, city)
        return bool(naselje) and _number_on_street(conn, naselje, ulica, broj)
    finally:
        conn.close()


def _number_on_street(conn, naselje: str, ulica: str, broj: str) -> bool:
    fu, fb = fold(ulica), fold(broj)
    ids = [r[0] for r in conn.execute("SELECT ulica_maticni_broj, name_lat FROM rgz_streets "
                                      "WHERE naselje_lat = ?", (naselje,)) if fold(r[1]) == fu]
    if not ids:
        return False
    marks = ",".join("?" * len(ids))
    return any(fold(r[0]) == fb for r in conn.execute(
        f"SELECT kucni_broj FROM rgz_housenumbers WHERE ulica_maticni_broj IN ({marks})", ids))


def _street_suggestions(city: str, ulica: str, broj: str, limit: int = 3) -> list:
    """Real streets close to a misspelled one ("Narodnog frotna" -> Narodnog fronta), in the
    place the line names (or the places it names, for "Beograd"). Only ever offered - the
    manager picks one, nothing is saved from here on its own."""
    import difflib
    conn = get_rgz_connection()
    try:
        naselje = _resolve_naselje(conn, city)
        places = [naselje] if naselje else sorted(set().union(*_named_naselja(conn, city)))
        if not places:
            return []
        marks = ",".join("?" * len(places))
        rows = conn.execute(f"SELECT DISTINCT name_lat, naselje_lat FROM rgz_streets "
                            f"WHERE naselje_lat IN ({marks})", places).fetchall()
    finally:
        conn.close()
    fu = fold(ulica)
    typed_words = set(_words(ulica).split())
    scored = []
    for name, naselje in rows:
        fn = fold(name)
        score = difflib.SequenceMatcher(None, fu, fn).ratio()
        # "Cara Lazara" for "Bulevar cara Lazara": every typed word is in the name.
        if typed_words and typed_words <= set(_words(name).split()):
            score = max(score, 0.86)
        if "(" in naselje:
            score += 0.03   # "Beograd": a city district before a village of an outer opština
        if score >= 0.8:
            scored.append((score, name, naselje))
    scored.sort(key=lambda s: (-s[0], s[1]))
    out, seen = [], set()
    for _, name, naselje in scored:
        key = (name, naselje)
        if key in seen:
            continue
        seen.add(key)
        place = city if naselje == _resolve_naselje_safe(city) else naselje.title()
        out.append({"city": place, "ulica": name.title(), "broj": broj})
        if len(out) == limit:
            break
    return out


def _registry_spelling(city: str, ulica: str) -> str:
    """"Futoska" -> "Futoška": the registry's letters when the typed ones differ beyond case."""
    conn = get_rgz_connection()
    try:
        naselje = _resolve_naselje(conn, city)
        hits = _places_with_street(conn, [naselje], ulica) if naselje else []
    finally:
        conn.close()
    if hits and hits[0][1].lower() != ulica.lower():
        return hits[0][1].title()
    return ulica


def _resolve_naselje_safe(city: str):
    conn = get_rgz_connection()
    try:
        return _resolve_naselje(conn, city)
    finally:
        conn.close()


def check_address_lines(default_city: str, text: str) -> list:
    """Every line of a manager's pasted list, checked before anything is saved. One dict each:
    status "ok" (found as typed or with "Bul."-style shorthand expanded), "num" (the street is
    real, the number isn't in the registry - a new building; alerts still reach the street),
    "maybe" (a misspelled street: `options` to pick from), or "bad" (`message` says why).
    city/ulica/broj on ok/num are exactly what /api/upravnici then saves."""
    out = []
    lines = [ln for ln in (text or "").splitlines() if ln.strip()][:MANAGER_MAX_LINES]
    for i, line in enumerate(lines):
        parsed = _split_line(line.strip(), (default_city or "").strip())
        if parsed is None:
            continue
        city, ulica, broj = parsed
        row = {"i": i, "line": line.strip(), "status": "bad", "options": [], "message": ""}
        out.append(row)
        if not city:
            row["message"] = "Upišite grad (gore ili posle zareza)."
            continue
        if not broj or not valid_broj(broj):
            row["message"] = "Nedostaje kućni broj (npr. 12, 12a ili bb)."
            continue
        ulica = _expand_street(ulica)
        c2, u2, err = check_address(city, ulica, broj, False)
        if not err:
            u2 = _registry_spelling(c2, u2)
            row.update(status="ok" if _house_in_registry(c2, u2, broj) else "num",
                       city=c2, ulica=u2, broj=broj, label=f"{u2} {broj}, {c2}")
            continue
        if err.get("code") == "UNCOVERED":
            row["message"] = f"Za mesto „{city}“ još nemamo spisak ulica."
            continue
        options = _street_suggestions(city, ulica, broj)
        for o in options:
            o["label"] = f"{o['ulica']} {o['broj']}, {o['city']}"
        if options:
            same_name = all(fold(o["ulica"]) == fold(ulica) for o in options)
            row.update(status="maybe", options=options,
                       message="Ova ulica postoji na više mesta, izaberite:" if same_name
                       else "Da li ste mislili:")
        elif err.get("code") == "PICK_PLACE":
            row["message"] = "Mesto nije jasno - dopišite selo ili deo grada posle zareza."
        else:
            row["message"] = f"Nema takve ulice u mestu {city}."
    return out


MANAGER_MAX_LINES = 200


def coverage_tier(city):
    """'full' (dedicated utility scraper + power), 'partial' (EPS power only), or 'none' (no RGZ
    street data at all - can't register a real address here yet, and street_exists() would have
    nothing to check against). See FULL_COVERAGE_OPSTINE / PARTIAL_COVERAGE_OPSTINE above for how
    these were derived."""
    conn = get_rgz_connection()
    naselje = _resolve_naselje(conn, city)
    if not naselje:
        conn.close()
        return "none"
    opstina = _naselje_opstina(conn).get(naselje)
    conn.close()
    if opstina in FULL_COVERAGE_OPSTINE:
        return "full"
    if opstina in PARTIAL_COVERAGE_OPSTINE:
        return "partial"
    return "partial"  # in RGZ but not in either curated set - conservatively "power might hit it"


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/cities":
            self._api_cities()
        elif parsed.path == "/api/streets":
            self._api_streets(qs)
        elif parsed.path == "/api/housenumbers":
            self._api_housenumbers(qs)
        elif parsed.path == "/api/coverage":
            self._api_coverage()
        elif parsed.path == "/unsubscribe":
            self._unsubscribe_page(qs)
        elif parsed.path == "/obavestenja":
            self._city_notices_page(qs)
        elif parsed.path == "/tacno":
            self._feedback_page(qs)
        elif parsed.path == "/auth/verify":
            self._auth_verify_page(qs)
        elif parsed.path == "/dashboard":
            self._dashboard_page()
        elif parsed.path == "/api/me":
            self._api_me()
        elif parsed.path == "/api/session":
            self._api_session()
        else:
            super().do_GET()

    def send_error(self, code, message=None, explain=None):
        """A 404 on a normal browser GET (e.g. someone types app.najavaradova.rs/dashboard/1)
        would otherwise be BaseHTTPRequestHandler's bare "Error response" page. Serve the same
        branded web/404.html the static site uses instead - one file, no duplicate markup.
        API/POST 404s and every other status keep the default behaviour."""
        if code == 404 and self.command == "GET":
            try:
                with open(os.path.join(WEB_DIR, "404.html"), "rb") as f:
                    body = f.read()
            except OSError:
                super().send_error(code, message, explain)
                return
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().send_error(code, message, explain)

    def _unsub_address_label(self, token):
        """'<Ulica> <broj>, <Grad>' for the still-active target this unsub token belongs to, so
        the confirm page and the success page name which address is being turned off (the email
        can carry several addresses' links). None if the token matches no active target - the
        page then falls back to the generic wording, exactly as before this existed."""
        if not token:
            return None
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT city, ulica, broj FROM targets WHERE unsub_token = ? AND is_active = 1",
                (token,),
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            return None
        if not row:
            return None
        street = _display_latin(row["ulica"])
        grad = _display_latin(row["city"])
        label = f"{street} {row['broj']}".strip()
        return f"{label}, {grad}" if grad else label

    def _unsubscribe_page(self, qs):
        """GET /unsubscribe - only ASKS. The is_active=0 write is on the POST below. A GET that
        mutated state would let a mail scanner / link-prefetch / chat preview silently
        unsubscribe someone just by following the link in a notification email."""
        tokens = _unsub_tokens(qs)
        if not tokens:
            self._send_html(200, self._card_page(
                "Odjava", "⚠️",
                "Ovaj link za odjavu nije važeći ili je već iskorišćen.",
                "<a href='https://najavaradova.rs'>Nazad na sajt</a>"))
            return
        safe = html.escape(urlencode([("token", t) for t in tokens]), quote=True)
        labels = [l for l in (self._unsub_address_label(t) for t in tokens) if l]
        if len(labels) > 1:
            prompt = (f"Odjaviti adrese {', '.join(f'<b>{html.escape(l)}</b>' for l in labels)} "
                      f"sa obaveštenja o radovima?")
        elif labels:
            prompt = f"Odjaviti adresu <b>{html.escape(labels[0])}</b> sa obaveštenja o radovima?"
        else:
            prompt = "Odjaviti ovu adresu sa obaveštenja o radovima?"
        action = (
            f"<form method='POST' action='/unsubscribe?{safe}'>"
            f"<button type='submit'>Odjavi me sa obaveštenja</button></form>"
            f"<a class='ghost' href='https://najavaradova.rs'>Otkaži</a>"
        )
        self._send_html(200, self._card_page("Odjava", "🔕", prompt, action))

    def _unsubscribe_confirm(self):
        """POST /unsubscribe - the actual unsubscribe. Serves both the button on the page above
        and RFC 8058 one-click (Gmail / Yahoo POST straight here, body 'List-Unsubscribe=One-Click',
        token in the query string). Body is drained, not parsed. Idempotent: the AND is_active = 1
        makes a repeat POST a no-op, so there's nothing to rate-limit."""
        self._drain_body()
        tokens = _unsub_tokens(parse_qs(urlparse(self.path).query))
        # Read the address labels before the UPDATE clears is_active (the WHERE below needs it =1).
        labels = {t: self._unsub_address_label(t) for t in tokens}
        turned_off = []
        if tokens:
            conn = get_connection()
            for token in tokens:
                cursor = conn.execute(
                    "UPDATE targets SET is_active = 0 WHERE unsub_token = ? AND is_active = 1", (token,)
                )
                if cursor.rowcount > 0:
                    turned_off.append(labels[token])
            conn.commit()
            conn.close()
        found = bool(turned_off)
        # Same page for every outcome (missing / used / never existed) - no signal an attacker
        # could use to tell "valid but already off" from "invalid token". The address name is
        # only added on a real transition (found), where ✅ already confirms the token was live.
        icon = "✅" if found else "⚠️"
        named = [l for l in turned_off if l]
        if len(named) > 1:
            message = (f"Adrese {', '.join(f'<b>{html.escape(l)}</b>' for l in named)} "
                       f"su odjavljene sa obaveštenja.")
        elif found:
            message = (f"Adresa <b>{html.escape(named[0])}</b> je odjavljena sa obaveštenja."
                       if named else "Uspešno si odjavljen sa obaveštenja.")
        else:
            message = "Ovaj link za odjavu nije važeći ili je već iskorišćen."
        self._send_html(200, self._card_page(
            "Odjava", icon, message,
            "<a href='https://najavaradova.rs'>Nazad na sajt</a>"))

    def _api_event(self):
        """POST /api/ev - web/signup.js counts the form being opened and sent, per page. One
        counter per (day, event, page); nothing about the visitor is read or kept. Anything that
        doesn't look like one of ours is ignored, and the answer is always the same 204."""
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 512)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            data = {}
        event = data.get("e") if isinstance(data, dict) else None
        page = str((data or {}).get("p") or "")[:80].lower() if isinstance(data, dict) else ""
        if event in ("open", "submit"):
            page = page if re.fullmatch(r"[a-z0-9-]{1,80}", page) else "other"
            conn = get_connection()
            conn.execute(
                "INSERT INTO signup_events (day, event, page, n) VALUES (date('now'), ?, ?, 1) "
                "ON CONFLICT(day, event, page) DO UPDATE SET n = n + 1", (event, page))
            conn.commit()
            conn.close()
        self.send_response(204)
        self.end_headers()

    @staticmethod
    def _feedback_args(qs):
        """(outage id, token, 'y'|'n') from a /tacno link, or None when it isn't one of ours."""
        o, t, v = (qs.get(k, [""])[0] for k in ("o", "t", "v"))
        if not (o.isdigit() and v in ("y", "n") and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", t or "")):
            return None
        return int(o), t, v

    def _feedback_page(self, qs):
        """GET /tacno - a reader's 👍 / 👎 link from an alert email. Only asks, like /unsubscribe:
        the answer is stored on the POST, so a mail scanner opening the link records nothing."""
        args = self._feedback_args(qs)
        if not args:
            self._send_html(200, self._card_page(
                "Povratna informacija", "⚠️", "Ovaj link nije važeći.",
                "<a href='https://najavaradova.rs'>Nazad na sajt</a>"))
            return
        o, t, v = args
        label = self._unsub_address_label(t)
        where = f" za adresu <b>{html.escape(label)}</b>" if label else ""
        other = html.escape(urlencode([("o", o), ("t", t), ("v", "n" if v == "y" else "y")]), quote=True)
        safe = html.escape(urlencode([("o", o), ("t", t), ("v", v)]), quote=True)
        if v == "y":
            prompt, button, switch = (f"Obaveštenje je bilo tačno{where}?", "👍 Da, tačno je",
                                      "Ipak nije tačno")
        else:
            prompt, button, switch = (f"Obaveštenje nije bilo tačno{where}?", "👎 Nije tačno",
                                      "Ipak je tačno")
        action = (f"<form method='POST' action='/tacno?{safe}'>"
                  f"<button type='submit'>{button}</button></form>"
                  f"<a class='ghost' href='/tacno?{other}'>{switch}</a>")
        self._send_html(200, self._card_page("Povratna informacija", "🗳️", prompt, action))

    def _feedback_save(self):
        """POST /tacno - stores the answer (core/feedback.py), only for an address the alert was
        really sent to. The same answer page for an unknown link as for a used one."""
        self._drain_body()
        args = self._feedback_args(parse_qs(urlparse(self.path).query))
        stored = 0
        if args:
            from core import feedback
            o, t, v = args
            stored = feedback.record(o, "email", t, feedback.VERDICTS[v])
        if stored and args[2] == "y":
            icon, message = "✅", "Hvala! Zabeleženo."
        elif stored:
            icon, message = "✅", "Hvala! Zabeleženo - proverićemo ovo obaveštenje."
        else:
            icon, message = "⚠️", "Ovaj link nije važeći."
        self._send_html(200, self._card_page(
            "Povratna informacija", icon, message,
            "<a href='https://najavaradova.rs'>Nazad na sajt</a>"))

    def _city_notices_page(self, qs):
        """GET /obavestenja?token= - the city-wide notices' opt-out link (core/broadcast.py). Only
        asks, like /unsubscribe: the write is on the POST."""
        tokens = _unsub_tokens(qs)[:1]
        safe = html.escape(urlencode([("token", t) for t in tokens]), quote=True)
        action = (f"<form method='POST' action='/obavestenja?{safe}'>"
                  f"<button type='submit'>Isključi obaveštenja za ceo grad</button></form>"
                  f"<a class='ghost' href='https://najavaradova.rs'>Otkaži</a>")
        self._send_html(200, self._card_page(
            "Obaveštenja za ceo grad", "🔕",
            "Isključiti retka obaveštenja za ceo grad (npr. početak grejne sezone)? Obaveštenja o "
            "radovima na vašoj adresi stižu i dalje.", action))

    def _city_notices_off(self):
        """POST /obavestenja?token= - turns them off for every address of that token's email."""
        self._drain_body()
        from core.broadcast import set_opt_out
        tokens = _unsub_tokens(parse_qs(urlparse(self.path).query))[:1]
        email = None
        if tokens:
            conn = get_connection()
            row = conn.execute("SELECT email FROM targets WHERE unsub_token = ?", (tokens[0],)).fetchone()
            conn.close()
            email = row["email"] if row else None
        done = bool(email) and set_opt_out(email=email, off=True) > 0
        self._send_html(200, self._card_page(
            "Obaveštenja za ceo grad", "✅" if done else "⚠️",
            "Isključeno. Obaveštenja o radovima na vašoj adresi stižu i dalje." if done
            else "Ovaj link nije važeći.",
            "<a href='https://najavaradova.rs'>Nazad na sajt</a>"))

    # ---------- cookies / session ----------

    def _get_cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def _current_subscriber_id(self):
        return auth.get_subscriber_id_for_session(self._get_cookie(SESSION_COOKIE_NAME))

    def _client_ip(self):
        """Real visitor IP for rate limiting - NOT self.client_address[0], which behind the
        Caddy reverse proxy is one shared container IP. See _client_ip_from for the trust chain."""
        return _client_ip_from(self.headers, self.client_address[0])

    def _session_cookie_header(self, raw_token):
        return (f"{SESSION_COOKIE_NAME}={raw_token}; Path=/; HttpOnly; Secure; SameSite=Lax; "
                f"Max-Age={auth.SESSION_TTL_DAYS * 86400}")

    def _clear_cookie_header(self):
        return f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"

    def _read_json(self):
        """Parsed JSON object from the request body, or None for anything malformed, oversized,
        or not a JSON object. Callers treat None as a 400. The Content-Length parse is INSIDE the
        guard - a junk header ('abc') must not raise straight out of here."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            raw = self.rfile.read(length)
        except Exception:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        # Every caller immediately does data.get(...) - a bare JSON array, string or number
        # would sail past this function and crash the handler. Only an object is valid input.
        return data if isinstance(data, dict) else None

    def _validate_address(self, data, allow_multi=False):
        """Shared by /auth/start (not logged in yet) and /api/targets (already logged in, adding
        another address) - same five checks /register used to do inline. Returns
        (city, ulica, broj, channel, None) on success or (None, None, None, None, error_dict).

        allow_multi (dashboard only): the 4th element comes back as a list of 1-2 channels read
        from `channels` (the dashboard's add-address form lets you pick Telegram AND email at
        once). /auth/start stays single-channel and passes allow_multi=False."""
        city = (data.get("city") or "").strip()
        ulica = (data.get("ulica") or "").strip()
        broj = (data.get("broj") or "").strip().upper()
        ack_uncovered = bool(data.get("ack_uncovered"))
        if allow_multi:
            raw = data.get("channels")
            channel = [c for c in ("telegram", "email")
                       if isinstance(raw, list) and c in raw]  # fixed order, deduped, whitelisted
        else:
            channel = data.get("channel")
        channel_ok = bool(channel) if allow_multi else channel in ("telegram", "email")
        if not city or not ulica or not broj or not channel_ok:
            return None, None, None, None, {"error": "Nedostaju obavezna polja."}
        city, ulica, err = check_address(city, ulica, broj, ack_uncovered)
        if err:
            return None, None, None, None, err
        return city, ulica, broj, channel, None

    def _create_email_target(self, subscriber_id, email, city, ulica, broj, backfill=True):
        """Shared by /auth/verify (first address, right after email confirmation) and
        /api/targets (every address after that, already logged in). Returns the target id
        (existing or new). Idempotent: re-adding the exact same active address is a no-op if it
        already has an email; if a row for that address exists WITHOUT an email (it was
        Telegram-only), this adds the email channel onto that same row rather than a duplicate."""
        conn = get_connection()
        dup = conn.execute(
            "SELECT id, email FROM targets WHERE is_active = 1 AND subscriber_id = ? "
            "AND UPPER(city) = UPPER(?) AND UPPER(ulica) = UPPER(?) AND broj = ?",
            (subscriber_id, city, ulica, broj),
        ).fetchone()
        if dup:
            added = False
            if dup["email"] is None:
                conn.execute(
                    "UPDATE targets SET email = ?, email_active = 1, unsub_token = ? WHERE id = ?",
                    (email, secrets.token_urlsafe(24), dup["id"]),
                )
                conn.commit()
                added = True
            conn.close()
            if added and backfill:
                self._backfill_target(dup["id"])
            return dup["id"]
        unsub_token = secrets.token_urlsafe(24)
        cur = conn.execute(
            "INSERT INTO targets (city, ulica, broj, email, unsub_token, subscriber_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (city, ulica, broj, email, unsub_token, subscriber_id),
        )
        new_target_id = cur.lastrowid
        conn.commit()
        conn.close()
        if backfill:
            self._backfill_target(new_target_id)
        return new_target_id

    def _live_pending_code(self, conn, subscriber_id, attach_target_id, city, ulica, broj):
        """An unconsumed, not-yet-expired Telegram deep-link for this exact address (and, for an
        attach, this exact target) - or None. Every caller that hands out a link goes through
        this first: clicking "Poveži" five times must return the SAME link five times, not five
        rows that then render as five identical reminder banners on the dashboard.

        Matched on the same (attach_target_id, address) group /api/me collapses on, and bounded
        by the same 24h window workers/telegram_bot.py's find_pending() honours - an expired code
        is correctly treated as absent, so a new one gets minted."""
        rows = conn.execute(
            "SELECT id, code FROM pending_registrations "
            "WHERE subscriber_id = ? AND consumed_at IS NULL "
            "  AND created_at >= datetime('now', '-24 hours') "
            "  AND IFNULL(attach_target_id, -1) = IFNULL(?, -1) "
            "  AND UPPER(city) = UPPER(?) AND UPPER(ulica) = UPPER(?) AND broj = ? "
            "ORDER BY id DESC",
            (subscriber_id, attach_target_id, city, ulica, broj),
        ).fetchall()
        if not rows:
            return None
        # Self-heal: drop any superseded duplicates in this group instead of leaving them for
        # /api/me to filter out at display time. Rows written before the reuse fix (and anything
        # a race squeezes in) get cleaned up the next time this address hands out a link, so the
        # table doesn't quietly carry junk for 24h.
        stale = [r["id"] for r in rows[1:]]
        if stale:
            conn.execute(
                "DELETE FROM pending_registrations WHERE id IN (%s)" % ",".join("?" * len(stale)),
                stale,
            )
            conn.commit()
        return rows[0]["code"]

    def _backfill_target(self, target_id):
        """Map + match against outages already ACTIVE, so a signup (or a newly added channel) for
        a known upcoming outage is queued now, not only on a future re-scrape. Never blocks the
        caller's response - the row is already saved; main.py's periodic pass is the safety net."""
        try:
            from core.matchmaker import backfill_target_against_active_outages
            backfill_target_against_active_outages(target_id)
        except Exception as be:
            print(f"⚠️ backfill for target #{target_id} failed (registration OK): {be}")

    def _card_page(self, title, icon, message_html, action_html):
        """The one dark centred-card layout every no-login email-landing page uses: auth error,
        the /auth/verify confirm button, the /unsubscribe confirm button, the unsubscribe
        result. `message_html` / `action_html` are built here or from already-escaped values -
        never raw request text. noindex because these are per-token one-off pages."""
        return f"""<!doctype html><html lang='sr'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<meta name='robots' content='noindex'>
<title>{title} - NajavaRadova.rs</title>
<style>
  :root {{ --bg:#101013; --text:#F2F0EE; --dim:#A5A1AB; }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    padding:20px;
  }}
  .card {{
    width:100%; max-width:420px; background:#1B1B20; border:1px solid #2B2B31;
    border-radius:16px; padding:36px 30px; text-align:center;
    box-shadow:0 20px 60px rgba(0,0,0,.5);
  }}
  .icon {{ font-size:34px; margin-bottom:14px; }}
  .icon:empty {{ display:none; }}
  p {{ font-size:15px; color:var(--dim); line-height:1.5; margin:0 0 20px; }}
  form {{ margin:0 0 12px; }}
  a, button {{
    display:inline-block; padding:11px 22px; border-radius:10px; border:0;
    background:var(--text); color:var(--bg); text-decoration:none;
    font-size:14.5px; font-weight:600; font-family:inherit; cursor:pointer;
  }}
  a.ghost {{ background:transparent; color:var(--dim); font-weight:500; padding:8px; }}
  button:focus-visible, a:focus-visible {{ outline:2px solid #7FA9F5; outline-offset:2px; }}
</style></head>
<body><div class='card'>
  <div class='icon'>{icon}</div>
  <p>{message_html}</p>
  {action_html}
</div></body></html>""".encode("utf-8")

    def _auth_error_page(self, message):
        return self._card_page(
            "Prijava", "⚠️", html.escape(message),
            "<a href='https://najavaradova.rs'>Nazad na sajt</a>")

    def _drain_body(self):
        """Read and discard the request body (up to the size cap) so a keep-alive connection
        stays aligned for the next request. For POSTs whose body we don't parse - /auth/verify
        confirm, one-click /unsubscribe. If the client announced more than the cap, close the
        connection rather than leave unread bytes on the socket."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return
        take = min(max(length, 0), MAX_BODY_BYTES)
        if take:
            try:
                self.rfile.read(take)
            except Exception:
                pass
        if length > take:
            self.close_connection = True

    def _send_html(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_verify_page(self, qs):
        """GET /auth/verify - does NOT consume the token. Corporate mail scanners (Microsoft
        Safe Links, Proofpoint, Barracuda...) fetch every link in an incoming email before the
        human opens it; a GET that burned the single-use token would lock the real user out of
        their own login. The token is spent only by the POST below.

        For a real visitor the page just shows "Prijavljujemo te…" for a beat while a script
        submits the form, then the dashboard loads - no button, no interaction. Scanners don't
        run that script / don't complete form posts, so the token survives for the human. The
        submit button exists only for the no-JS case and is hidden unless <noscript> reveals
        it."""
        token = (qs.get("token") or [""])[0].strip()
        if not token:
            self._send_html(400, self._auth_error_page(
                "Ovaj link za prijavu nije važeći. Zatraži novi na sajtu."))
            return
        safe = html.escape(token, quote=True)
        action = (
            f"<form id='go' method='POST' action='/auth/verify?token={safe}'>"
            f"<button type='submit' style='display:none'>Prijavi se</button></form>"
            f"<noscript><style>#go button{{display:inline-block!important}}</style>"
            f"<p>Klikni dugme da nastaviš.</p></noscript>"
            f"<script>setTimeout(function(){{var f=document.getElementById('go');"
            f"if(f)f.submit();}},150);</script>"
        )
        self._send_html(200, self._card_page(
            "Prijava", "", "Prijavljujemo te na tvoj nalog…", action))

    def _auth_resend(self):
        """POST /auth/resend?token= - the "Pošalji mi novi link" button on an expired link. The
        new link goes only to the address the old one was sent to (core/auth.resend_expired_link)."""
        self._drain_body()
        if rate_limited(self._client_ip()):
            self._send_html(429, self._auth_error_page("Previše zahteva, pokušaj ponovo za nekoliko minuta."))
            return
        token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0].strip()
        if auth.resend_expired_link(token):
            self._send_html(200, self._card_page(
                "Prijava", "📬",
                "Poslali smo ti novi link. Otvori najnoviji mejl od NajavaRadova.rs i klikni dugme. "
                "Ne vidiš ga? Pogledaj u folderu Spam ili Promocije.",
                "<a href='https://najavaradova.rs'>Nazad na sajt</a>"))
        else:
            self._send_html(400, self._auth_error_page(
                "Ovaj link ne može da se obnovi. Zatraži novi na sajtu."))

    def _auth_verify_consume(self):
        """POST /auth/verify - burns the token, opens the session. Same work the old GET handler
        did, moved behind a POST (see _auth_verify_page)."""
        self._drain_body()
        token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0].strip()
        result = auth.verify_and_consume_token(token)
        if not result and auth.is_expired_unused(token):
            safe = html.escape(token, quote=True)
            self._send_html(400, self._card_page(
                "Prijava", "⏰",
                "Ovaj link je istekao. Pošaljemo ti novi na isti mejl? Adresa koju si upisao/la ostaje ista.",
                f"<form method='POST' action='/auth/resend?token={safe}'>"
                f"<button type='submit'>Pošalji mi novi link</button></form>"))
            return
        if not result:
            # Clicking the same email link again from the browser it already logged in: their
            # page, not "already used" - which read as "the signup failed" (29.10.2026).
            if self._current_subscriber_id():
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
                return
            self._send_html(400, self._auth_error_page(
                "Ovaj link za prijavu nije važeći ili je već iskorišćen. Zatraži novi na sajtu."
            ))
            return
        subscriber_id, payload = result
        if payload and payload.get("manager_request"):
            # A building manager's list (/upravnici): load it now, then their page - or, with
            # Telegram chosen, one link that connects all of the addresses at once.
            loaded, tg_link = self._load_manager_request(subscriber_id, int(payload["manager_request"]))
            session_token = auth.create_session(subscriber_id)
            if tg_link:
                body = self._card_page(
                    "Zgrade unete", "✅",
                    f"Unete su Vaše adrese ({loaded}). Još jedan korak: povežite Telegram, "
                    f"jednim klikom za sve adrese.",
                    f"<a href='{html.escape(tg_link, quote=True)}'>Poveži Telegram</a>"
                    f"<p style='margin:16px 0 0'><a href='/dashboard' style='background:none;"
                    f"color:var(--dim);padding:0'>Moja stranica &rarr;</a></p>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", self._session_cookie_header(session_token))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", "/dashboard")
            self.send_header("Set-Cookie", self._session_cookie_header(session_token))
            self.end_headers()
            return
        if payload:
            if payload.get("telegram_code"):
                # The Telegram deep-link (pending_registrations) was already written at
                # /auth/start time - this just attaches it to the now-confirmed subscriber so the
                # bot's complete_registration() can copy subscriber_id onto the real target row.
                conn = get_connection()
                conn.execute(
                    "UPDATE pending_registrations SET subscriber_id = ? "
                    "WHERE code = ? AND consumed_at IS NULL",
                    (subscriber_id, payload["telegram_code"]),
                )
                conn.commit()
                conn.close()
            else:
                email = auth.get_subscriber_email(subscriber_id)
                self._create_email_target(
                    subscriber_id, email, payload["city"], payload["ulica"], payload["broj"]
                )
                if payload.get("cancel_telegram_code"):
                    # The signup reminder's "Šalji mi na email umesto toga"
                    # (core/signup_reminder.py): the address is saved with email now, so the
                    # unfinished Telegram link goes - it would stay on the dashboard as "Poveži
                    # Telegram" for the same address.
                    conn = get_connection()
                    conn.execute(
                        "DELETE FROM pending_registrations "
                        "WHERE code = ? AND subscriber_id = ? AND consumed_at IS NULL",
                        (payload["cancel_telegram_code"], subscriber_id),
                    )
                    conn.commit()
                    conn.close()
        session_token = auth.create_session(subscriber_id)
        self.send_response(302)
        self.send_header("Location", "/dashboard")
        self.send_header("Set-Cookie", self._session_cookie_header(session_token))
        self.end_headers()

    def _dashboard_page(self):
        try:
            with open(DASHBOARD_PATH, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_session(self):
        """Logged in or not, and nothing else - for najavaradova.rs and the city pages
        (web/signup.js), which only need the yes/no. Always 200: /api/me's 401 for a logged-out
        visitor showed up as a browser console error on every page view."""
        self._json(200, {"logged_in": bool(self._current_subscriber_id())})

    def _api_me(self):
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        email = auth.get_subscriber_email(subscriber_id)
        conn = get_connection()
        # telegram_username / email are the channel DESTINATIONS, shown in the dashboard's
        # per-address detail so it's visible where each notification actually goes (the chat_id
        # itself is deliberately not exposed - the @username is the human-readable half).
        targets = conn.execute(
            "SELECT id, city, ulica, broj, is_active, paid_until, email_active, telegram_active, "
            "telegram_username, email, "
            "(telegram_chat_id IS NOT NULL) AS has_telegram, (email IS NOT NULL) AS has_email "
            "FROM targets WHERE subscriber_id = ? ORDER BY id",
            (subscriber_id,),
        ).fetchall()
        # One reminder per address, never a stack of identical ones. The endpoints below now
        # reuse a live code instead of minting a new one per click, but rows created before that
        # fix (and any genuine race) still need collapsing here: keep only the newest row in each
        # (attach_target_id, address) group.
        pending = conn.execute(
            """
            SELECT code, city, ulica, broj, attach_target_id FROM pending_registrations p
            WHERE p.subscriber_id = ? AND p.consumed_at IS NULL
              AND p.created_at >= datetime('now', '-24 hours')
              AND p.id = (
                  SELECT MAX(q.id) FROM pending_registrations q
                  WHERE q.subscriber_id = p.subscriber_id
                    AND q.consumed_at IS NULL
                    AND q.created_at >= datetime('now', '-24 hours')
                    AND IFNULL(q.attach_target_id, -1) = IFNULL(p.attach_target_id, -1)
                    AND UPPER(q.city) = UPPER(p.city)
                    AND UPPER(q.ulica) = UPPER(p.ulica)
                    AND q.broj = p.broj
              )
            ORDER BY p.id
            """,
            (subscriber_id,),
        ).fetchall()
        conn.close()
        # Per address, the outages of the last 90 days the alert rules would have sent there
        # (core.matchmaker.outage_history) - the dashboard's "Poslednjih 90 dana" line. Extra
        # information only: if it fails, the page still loads, just without those numbers.
        target_list = [dict(zip(r.keys(), r)) for r in targets]
        try:
            from core.matchmaker import outage_history
            history = outage_history([t["id"] for t in target_list])
            for t in target_list:
                h = history.get(t["id"]) or {}
                t["history_90d"] = h.get("count", 0)
                t["history_hours"] = h.get("hours", 0)
        except Exception as e:
            print(f"⚠️ /api/me history skipped: {e!r}")
        bot_username = None
        try:
            bot_username = get_bot_username()
        except Exception:
            pass
        self._json(200, {
            "email": email,
            "targets": target_list,
            "pending_telegram": [
                {
                    **dict(zip(r.keys(), r)),
                    "link": f"https://t.me/{bot_username}?start={r['code']}" if bot_username else None,
                }
                for r in pending
            ],
        })

    def _api_coverage(self):
        """Static, curated - not DB-derived - so the site's "covered areas" list can't drift out
        of sync with what /register actually enforces without someone noticing (both read the
        same FULL_COVERAGE_DISPLAY / PARTIAL_COVERAGE_DISPLAY constants)."""
        self._json(200, {"full": FULL_COVERAGE_DISPLAY, "partial": PARTIAL_COVERAGE_DISPLAY})

    def _api_cities(self):
        conn = get_rgz_connection()
        by_opstina = _naselje_opstina(conn)
        conn.close()
        rows = sorted(by_opstina)
        # "opstine" runs parallel to "cities", so typing "Grocka" also offers Kaluđerica, Vinča...
        # (a village person may only know their opština). "nostreet": every Serbian opština we
        # DON'T have RGZ streets for, so the city field autocompletes nationwide (PLAN.md §3) -
        # picking one lands the form in uncovered mode (free-text street + ack).
        self._json(200, {"cities": rows, "opstine": [by_opstina[n] for n in rows],
                         "nostreet": uncovered_opstine()})

    def _api_streets(self, qs):
        city = (qs.get("city") or [""])[0].strip()
        q = (qs.get("q") or [""])[0].strip()
        if not city or len(q) < 2:
            self._json(200, {"streets": []})
            return
        conn = get_rgz_connection()
        naselje = _resolve_naselje(conn, city)
        rows = []
        if naselje:
            fq = fold(q)
            # Fold-match every street in the naselje (a few hundred to ~few thousand rows -
            # cheap) instead of a Cyrillic/diacritic-sensitive SQL LIKE, so "matije"/"матије"
            # both find "MATIJE GUPCA" and "gupca" finds it too.
            all_rows = conn.execute(
                "SELECT DISTINCT ulica_maticni_broj, name_lat, opstina_lat FROM rgz_streets "
                "WHERE naselje_lat = ? ORDER BY name_lat",
                (naselje,),
            ).fetchall()
            rows = [r for r in all_rows if _name_matches(fold(r["name_lat"]), fq)][:15]
        conn.close()
        # opstina_lat, not naselje_lat, as the sub-label here - the place (naselje) is already
        # fixed by what the visitor picked for city, so echoing it back adds nothing; the
        # municipality is the useful extra context instead.
        self._json(200, {"streets": [{"id": r[0], "name": r[1], "opstina": r[2]} for r in rows]})

    def _api_housenumbers(self, qs):
        street_id = (qs.get("street_id") or [""])[0].strip()
        q = (qs.get("q") or [""])[0].strip().upper()
        if not street_id:
            self._json(200, {"numbers": []})
            return
        conn = get_rgz_connection()
        rows = conn.execute(
            "SELECT DISTINCT kucni_broj FROM rgz_housenumbers "
            "WHERE ulica_maticni_broj = ? AND kucni_broj LIKE ? ORDER BY kucni_broj LIMIT 60",
            (street_id, q + "%"),
        ).fetchall()
        conn.close()
        # valid_broj() now accepts the registry's own lettered suffixes (e.g. "8А") - this filter
        # only exists to drop whatever's left that still wouldn't validate on submit.
        numbers = [r[0] for r in rows if valid_broj(r[0])][:20]
        self._json(200, {"numbers": numbers})

    def do_OPTIONS(self):
        # CORS preflight - browsers send this before a cross-origin POST with a JSON content
        # type. Same-origin requests (today's local setup) never trigger it, but the real
        # cross-domain deployment will immediately need this to exist.
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _cors_headers(self):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            # Lets najavaradova.rs (the static marketing site) call GET /api/me with
            # credentials: 'include' and actually see the response - needed so the site can
            # recognise an already-logged-in visitor and skip sending them a redundant signup
            # email. Safe to pair with a reflected, non-wildcard Allow-Origin (never "*" - see
            # above): app.najavaradova.rs and najavaradova.rs share the same registrable domain,
            # so this is a same-site-different-subdomain request, not a truly cross-site one -
            # SameSite=Lax on the session cookie already does the real access control (a genuine
            # cross-site origin never gets the cookie attached in the first place, regardless of
            # this header).
            self.send_header("Access-Control-Allow-Credentials", "true")

    def do_POST(self):
        parsed = urlparse(self.path)
        routes = {
            "/auth/start": self._auth_start,
            "/auth/login": self._auth_login,
            "/auth/logout": self._auth_logout,
            "/auth/verify": self._auth_verify_consume,
            "/auth/resend": self._auth_resend,
            "/unsubscribe": self._unsubscribe_confirm,
            "/api/ev": self._api_event,
            "/obavestenja": self._city_notices_off,
            "/tacno": self._feedback_save,
            "/api/targets": self._api_add_target,
            "/api/targets/toggle": self._api_toggle_target,
            "/api/targets/toggle-channel": self._api_toggle_channel,
            "/api/targets/delete": self._api_delete_target,
            "/api/pending/cancel": self._api_cancel_pending,
            "/api/pending/to-email": self._api_pending_to_email,
            "/api/targets/add-email": self._api_add_email_channel,
            "/api/targets/add-telegram": self._api_add_telegram_channel,
            "/api/delete-account": self._api_delete_account,
            "/api/upravnici/check": self._api_manager_check,
            "/api/upravnici": self._api_manager_submit,
        }
        fn = routes.get(parsed.path)
        if not fn:
            self.send_error(404)
            return
        try:
            fn()
        except Exception as e:
            print(f"⚠️ {parsed.path} failed: {e}")
            self._json(500, {"error": "Greška na serveru, pokušaj ponovo."})

    def _auth_start(self):
        """Signup: address + email + channel -> one magic-link email. Replaces the old open
        /register - every signup now needs a confirmed account (see module docstring)."""
        if rate_limited(self._client_ip()):
            self._json(429, {"error": "Previše zahteva, pokušaj ponovo za nekoliko minuta."})
            return
        data = self._read_json()
        if data is None:
            self._json(400, {"error": "Loše formatiran zahtev."})
            return

        email = (data.get("email") or "").strip().lower()
        if not EMAIL_RE.match(email):
            self._json(400, {"error": "Neispravna email adresa."})
            return
        city, ulica, broj, channel, err = self._validate_address(data)
        if err:
            self._json(400, err)
            return

        if channel == "telegram":
            # Same pending_registrations row and deep-link the bot has always consumed - just
            # not yet tied to a subscriber (see /auth/verify, which fills that in once the email
            # is confirmed) and not usable via Telegram's Start button until then.
            code = secrets.token_urlsafe(8)
            conn = get_connection()
            conn.execute(
                "INSERT INTO pending_registrations (code, channel, city, ulica, broj, email) "
                "VALUES (?, 'telegram', ?, ?, ?, ?)",
                (code, city, ulica, broj, email),
            )
            conn.commit()
            conn.close()
            payload = {"telegram_code": code}
        else:
            payload = {"city": city, "ulica": ulica, "broj": broj, "channel": "email"}

        # Always the same response regardless of whether this email already had an account -
        # same anti-enumeration reasoning as core/auth.py's start_login(). `ref` is the partner
        # slug from a ?ref= referral link and `source` where the visitor found us (a site's
        # name, core/auth.clean_source), both applied only if this creates a new account.
        auth.start_login(email, payload=payload, ref=data.get("ref"), source=data.get("source"))
        self._json(200, {"ok": True, "saved": saved_address(data, city, ulica, broj)})

    def _auth_login(self):
        """Returning visitor, no new address - just re-establish a session."""
        if rate_limited(self._client_ip()):
            self._json(429, {"error": "Previše zahteva, pokušaj ponovo za nekoliko minuta."})
            return
        data = self._read_json()
        email = (data.get("email") or "").strip().lower() if data else ""
        if not EMAIL_RE.match(email):
            self._json(400, {"error": "Neispravna email adresa."})
            return
        auth.start_login(email, payload=None)
        self._json(200, {"ok": True})

    def _auth_logout(self):
        auth.delete_session(self._get_cookie(SESSION_COOKIE_NAME))
        self._json(200, {"ok": True}, extra_headers={"Set-Cookie": self._clear_cookie_header()})

    def _api_add_target(self):
        """Add another address once already logged in - no email round-trip needed, this is the
        fast path for a manager registering many addresses in one session. `channels` may be
        ["telegram"], ["email"], or both - pick both and the email side is created synchronously,
        then Telegram is attached to that same row (one address, two channels), not a duplicate."""
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        data = self._read_json()
        if data is None:
            self._json(400, {"error": "Loše formatiran zahtev."})
            return
        city, ulica, broj, channels, err = self._validate_address(data, allow_multi=True)
        if err:
            self._json(400, err)
            return

        email = auth.get_subscriber_email(subscriber_id)
        telegram_link = None
        email_target_id = None
        if "email" in channels:
            email_target_id = self._create_email_target(subscriber_id, email, city, ulica, broj)

        if "telegram" in channels:
            conn = get_connection()
            attach_id = None
            if email_target_id is not None:
                # Both channels: attach Telegram to the row we just made/kept - unless it already
                # has Telegram (re-adding an address that was already dual-channel).
                has_slot = conn.execute(
                    "SELECT 1 FROM targets WHERE id = ? AND subscriber_id = ? AND telegram_chat_id IS NULL",
                    (email_target_id, subscriber_id),
                ).fetchone()
                if not has_slot:
                    conn.close()
                    self._json(200, {"ok": True, "telegram_link": None,
                                     "saved": saved_address(data, city, ulica, broj)})
                    return
                attach_id = email_target_id
            code = self._live_pending_code(conn, subscriber_id, attach_id, city, ulica, broj)
            if not code:
                code = secrets.token_urlsafe(8)
                conn.execute(
                    "INSERT INTO pending_registrations "
                    "(code, channel, city, ulica, broj, email, subscriber_id, attach_target_id) "
                    "VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?)",
                    (code, city, ulica, broj, email, subscriber_id, attach_id),
                )
                conn.commit()
            conn.close()
            telegram_link = f"https://t.me/{get_bot_username()}?start={code}"

        self._json(200, {"ok": True, "telegram_link": telegram_link,
                         "saved": saved_address(data, city, ulica, broj)})

    def _api_manager_check(self):
        """POST /api/upravnici/check {city, text} - a manager's pasted list, every line checked
        (check_address_lines). Saves nothing. Own roomy throttle: re-checking after fixing a line
        is normal use, and each call is bounded at MANAGER_MAX_LINES lines."""
        if _throttle(_manager_check_hits, self._client_ip(), MANAGER_CHECK_RATE_LIMIT_MAX):
            self._json(429, {"error": "Previše provera, pokušajte ponovo za nekoliko minuta."})
            return
        data = self._read_json()
        if data is None:
            self._json(400, {"error": "Loše formatiran zahtev."})
            return
        self._json(200, {"lines": check_address_lines(str(data.get("city") or ""),
                                                      str(data.get("text") or ""))})

    def _api_manager_submit(self):
        """POST /api/upravnici {email, name, channels, addresses: [{city, ulica, broj}]} - the
        addresses the manager confirmed on the page. Each is checked again here (the page is
        never trusted), the request is saved, and the email link goes out; the addresses load
        when it is clicked (_load_manager_request). Same per-IP limit as a signup."""
        if rate_limited(self._client_ip()):
            self._json(429, {"error": "Previše zahteva, pokušajte ponovo za nekoliko minuta."})
            return
        data = self._read_json()
        if data is None:
            self._json(400, {"error": "Loše formatiran zahtev."})
            return
        if data.get("website"):
            # Honeypot: a field people never see. A bot that fills it gets the usual answer.
            self._json(200, {"ok": True, "count": 0})
            return
        email = (data.get("email") or "").strip().lower()
        if not EMAIL_RE.match(email):
            self._json(400, {"error": "Neispravna email adresa."})
            return
        raw = data.get("channels")
        channels = [c for c in ("email", "telegram") if isinstance(raw, list) and c in raw]
        if not channels:
            self._json(400, {"error": "Izaberite kako da Vam stižu obaveštenja."})
            return
        items = data.get("addresses")
        if not isinstance(items, list) or not items:
            self._json(400, {"error": "Nema potvrđenih adresa za slanje."})
            return
        if len(items) > managers.MAX_ADDRESSES:
            self._json(400, {"error": f"Najviše {managers.MAX_ADDRESSES} adresa odjednom - "
                                      "za veći spisak pišite nam na obavestenja@najavaradova.rs."})
            return
        addresses, seen = [], set()
        for a in items:
            if not isinstance(a, dict):
                continue
            broj = str(a.get("broj") or "").strip().upper()
            city, ulica, err = check_address(str(a.get("city") or "").strip(),
                                             str(a.get("ulica") or "").strip(), broj, False)
            if err:
                self._json(400, {"error": f"Adresa „{a.get('ulica')} {a.get('broj')}, {a.get('city')}“ "
                                          f"nije potvrđena - proverite spisak ponovo."})
                return
            key = (fold(city), fold(ulica), broj)
            if key not in seen:
                seen.add(key)
                addresses.append({"city": city, "ulica": ulica, "broj": broj})
        request_id = managers.create_request(email, data.get("name"), channels, addresses)
        auth.start_login(email, payload={"manager_request": request_id,
                                         "label": f"{len(addresses)} adresa"},
                         source=data.get("source"))
        self._json(200, {"ok": True, "count": len(addresses)})

    def _load_manager_request(self, subscriber_id, request_id):
        """The confirmed manager's addresses onto their account - once (claim_for_load). Email
        targets as the dashboard creates them; email turned off on the new ones if only Telegram
        was chosen. Returns (how many loaded, Telegram link or None)."""
        req = managers.claim_for_load(request_id, subscriber_id)
        if not req:
            return 0, None
        email = auth.get_subscriber_email(subscriber_id)
        conn = get_connection()
        before = {r[0] for r in conn.execute(
            "SELECT id FROM targets WHERE subscriber_id = ? AND is_active = 1", (subscriber_id,))}
        conn.close()
        loaded, new_ids, all_ids = 0, [], []
        for a in json.loads(req["addresses_json"]):
            try:
                tid = self._create_email_target(subscriber_id, email, a["city"], a["ulica"], a["broj"],
                                                backfill=False)
            except Exception as e:
                print(f"⚠️ manager request #{request_id}: {a} not loaded: {e}")
                continue
            loaded += 1
            all_ids.append(tid)
            if tid not in before:
                new_ids.append(tid)
        if all_ids:
            # Signed up as a manager: from now on this account gets the tenant-notice layout
            # (core/notice.py manager_sql), whatever it adds later.
            conn = get_connection()
            conn.executemany("UPDATE targets SET manager_list = 1 WHERE id = ? AND subscriber_id = ?",
                             [(tid, subscriber_id) for tid in all_ids])
            conn.commit()
            conn.close()
        channels = req["channels"].split(",")
        if "email" not in channels and new_ids:
            conn = get_connection()
            conn.executemany("UPDATE targets SET email_active = 0 WHERE id = ? AND subscriber_id = ?",
                             [(tid, subscriber_id) for tid in new_ids])
            conn.commit()
            conn.close()
        managers.record_load(request_id, new_ids)
        # Matched against outages already announced after the page has answered - up to 200
        # addresses, too slow to make the click wait. main.py's periodic pass is the safety net.
        threading.Thread(target=lambda: [self._backfill_target(t) for t in all_ids], daemon=True).start()
        managers.notify_operator(request_id, loaded)
        link = None
        if "telegram" in channels:
            code = secrets.token_urlsafe(8)
            conn = get_connection()
            # ulica/broj are only the label the dashboard's pending banner shows: "Poveži
            # Telegram za sve adrese (8)".
            conn.execute(
                "INSERT INTO pending_registrations (code, channel, city, ulica, broj, email, "
                "subscriber_id, attach_all) VALUES (?, 'telegram', '', 'sve adrese', ?, ?, ?, 1)",
                (code, f"({loaded})", email, subscriber_id))
            conn.commit()
            conn.close()
            link = f"https://t.me/{get_bot_username()}?start={code}"
        return loaded, link

    def _api_toggle_target(self):
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        data = self._read_json()
        target_id = data.get("id") if data else None
        if not isinstance(target_id, int):
            self._json(400, {"error": "Nedostaje id."})
            return
        conn = get_connection()
        # Ownership check is the WHERE clause itself, not a separate SELECT-then-trust step - a
        # client-supplied id for someone else's target simply matches zero rows, not an error
        # that leaks whether that id exists at all.
        # Pausing and resuming keep each channel's own on/off: the dashboard greys the channel
        # switches out while the address is paused and shows them as they were, so someone who
        # had muted email gets it back muted, not silently re-enabled. The one exception: resuming
        # an address whose every connected channel is off turns them all on, since "on" with
        # nothing to send through would still mean silence. The CASE arms read the PRE-update
        # row (SQLite, like standard SQL, evaluates every SET expression against the old values),
        # so `is_active = 0` there means "this click resumes it".
        cur = conn.execute('''
            UPDATE targets
            SET is_active = NOT is_active,
                email_active = CASE WHEN is_active = 0 AND NOT (
                        (email IS NOT NULL AND IFNULL(email_active, 0) = 1) OR
                        (telegram_chat_id IS NOT NULL AND IFNULL(telegram_active, 0) = 1))
                    THEN 1 ELSE email_active END,
                telegram_active = CASE WHEN is_active = 0 AND NOT (
                        (email IS NOT NULL AND IFNULL(email_active, 0) = 1) OR
                        (telegram_chat_id IS NOT NULL AND IFNULL(telegram_active, 0) = 1))
                    THEN 1 ELSE telegram_active END
            WHERE id = ? AND subscriber_id = ?
        ''', (target_id, subscriber_id))
        conn.commit()
        found = cur.rowcount > 0
        row = conn.execute(
            "SELECT is_active, email_active, telegram_active FROM targets WHERE id = ?",
            (target_id,)).fetchone() if found else None
        conn.close()
        if not found:
            self._json(404, {"error": "Adresa nije pronađena."})
            return
        # The page mirrors these instead of guessing what the server did to the channels.
        self._json(200, {"ok": True, "is_active": row[0], "email_active": row[1],
                         "telegram_active": row[2]})

    def _api_cancel_pending(self):
        """Drop a Telegram deep-link the visitor started and doesn't want to finish. Without this
        an abandoned link sits on the dashboard as a banner for a full 24h with no way to dismiss
        it. Deletes every unconsumed row in that address's group, not just one, so a stack left
        over from before the reuse fix clears in a single click.

        Scoped by `code`, and the code must belong to the caller - subscriber_id is in the WHERE
        clause, so someone else's code matches nothing."""
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        data = self._read_json()
        code = (data.get("code") or "").strip() if data else ""
        if not code:
            self._json(400, {"error": "Nedostaje kod."})
            return
        conn = get_connection()
        row = conn.execute(
            "SELECT attach_target_id, city, ulica, broj FROM pending_registrations "
            "WHERE code = ? AND subscriber_id = ? AND consumed_at IS NULL",
            (code, subscriber_id),
        ).fetchone()
        if not row:
            conn.close()
            self._json(404, {"error": "Link nije pronađen."})
            return
        conn.execute(
            "DELETE FROM pending_registrations "
            "WHERE subscriber_id = ? AND consumed_at IS NULL "
            "  AND IFNULL(attach_target_id, -1) = IFNULL(?, -1) "
            "  AND UPPER(city) = UPPER(?) AND UPPER(ulica) = UPPER(?) AND broj = ?",
            (subscriber_id, row["attach_target_id"], row["city"], row["ulica"], row["broj"]),
        )
        conn.commit()
        conn.close()
        self._json(200, {"ok": True})

    def _api_pending_to_email(self):
        """"Nemaš Telegram? Šalji mi na email" on a new address still waiting for its Telegram
        Start: the address is saved with the account's (already confirmed) email right away and
        the Telegram link goes - the dashboard twin of the signup reminder's email button. Only
        for a new address; Telegram being added to one that already works has its own channels."""
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        data = self._read_json()
        code = (data.get("code") or "").strip() if data else ""
        conn = get_connection()
        row = conn.execute(
            "SELECT city, ulica, broj FROM pending_registrations "
            "WHERE code = ? AND subscriber_id = ? AND consumed_at IS NULL AND attach_target_id IS NULL",
            (code, subscriber_id),
        ).fetchone() if code else None
        conn.close()
        if not row:
            self._json(404, {"error": "Link nije pronađen."})
            return
        email = auth.get_subscriber_email(subscriber_id)
        self._create_email_target(subscriber_id, email, row["city"], row["ulica"], row["broj"])
        conn = get_connection()
        conn.execute(
            "DELETE FROM pending_registrations "
            "WHERE subscriber_id = ? AND consumed_at IS NULL AND attach_target_id IS NULL "
            "  AND UPPER(city) = UPPER(?) AND UPPER(ulica) = UPPER(?) AND broj = ?",
            (subscriber_id, row["city"], row["ulica"], row["broj"]),
        )
        conn.commit()
        conn.close()
        self._json(200, {"ok": True})

    def _api_delete_target(self):
        """Permanently remove ONE owned address - the per-address counterpart to the account-wide
        /api/delete-account. Hard delete, not the is_active=0 pause the master toggle does.

        Every child row is deleted explicitly, with no reliance on any declared ON DELETE CASCADE
        (SQLite doesn't enforce those here - PRAGMA foreign_keys is off and get_connection() never
        turns it on; see core/auth.py's delete_subscriber_completely for the same reasoning).
        pending_registrations rows pointing at this target are included: an unconsumed
        attach_target_id deep-link would otherwise outlive the row it was meant to attach to."""
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        data = self._read_json()
        target_id = data.get("id") if data else None
        if not isinstance(target_id, int):
            self._json(400, {"error": "Nedostaje id."})
            return
        conn = get_connection()
        try:
            # Ownership proven before anything is deleted - a foreign or unknown id matches zero
            # rows and leaves without touching a thing.
            if not auth.delete_target(conn, subscriber_id, target_id):
                self._json(404, {"error": "Adresa nije pronađena."})
                return
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._json(200, {"ok": True})

    def _api_toggle_channel(self):
        """Silence/restore ONE channel on an address that has both, independent of the other and
        independent of the master is_active switch (see _api_toggle_target above)."""
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        data = self._read_json()
        target_id = data.get("id") if data else None
        channel = data.get("channel") if data else None
        if not isinstance(target_id, int) or channel not in ("email", "telegram"):
            self._json(400, {"error": "Nedostaje id ili kanal."})
            return
        # channel is checked against a fixed 2-value set above (never concatenated as-is), so
        # picking one of two hardcoded column names from it is safe - nothing user-controlled
        # ever reaches the SQL text itself.
        active_col = "email_active" if channel == "email" else "telegram_active"
        presence_col = "email" if channel == "email" else "telegram_chat_id"
        conn = get_connection()
        # Ownership check + "this channel actually exists on this row" are both baked into the
        # WHERE clause - can't silence a channel that was never configured, and can't touch
        # someone else's target either way.
        cur = conn.execute(
            f"UPDATE targets SET {active_col} = NOT {active_col} "
            f"WHERE id = ? AND subscriber_id = ? AND {presence_col} IS NOT NULL",
            (target_id, subscriber_id),
        )
        conn.commit()
        found = cur.rowcount > 0
        conn.close()
        if not found:
            self._json(404, {"error": "Adresa ili kanal nisu pronađeni."})
            return
        self._json(200, {"ok": True})

    def _api_add_email_channel(self):
        """Add email notifications to an address that only had Telegram - no confirmation round
        trip needed, the account's email is already verified (that's how the session exists at
        all)."""
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        data = self._read_json()
        target_id = data.get("id") if data else None
        if not isinstance(target_id, int):
            self._json(400, {"error": "Nedostaje id."})
            return
        email = auth.get_subscriber_email(subscriber_id)
        unsub_token = secrets.token_urlsafe(24)
        conn = get_connection()
        # "email IS NULL" makes this a one-shot: a second call on the same row matches zero rows
        # instead of overwriting an existing address's email or minting a second unsub_token.
        cur = conn.execute(
            "UPDATE targets SET email = ?, email_active = 1, unsub_token = ? "
            "WHERE id = ? AND subscriber_id = ? AND email IS NULL",
            (email, unsub_token, target_id, subscriber_id),
        )
        conn.commit()
        found = cur.rowcount > 0
        conn.close()
        if not found:
            self._json(404, {"error": "Adresa nije pronađena ili već ima email."})
            return
        # New channel on an existing address - queue it against outages already ACTIVE for it,
        # same as a fresh signup (the Telegram attach path does this too).
        self._backfill_target(target_id)
        self._json(200, {"ok": True})

    def _api_add_telegram_channel(self):
        """Add Telegram to an address that only had email - reuses the exact same
        pending_registrations + bot deep-link flow as a brand-new signup, just tagged with
        attach_target_id so the bot updates this row instead of inserting a new one."""
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        if authed_rate_limited(self._client_ip()):
            self._json(429, {"error": "Previše zahteva, pokušaj ponovo za nekoliko minuta."})
            return
        data = self._read_json()
        target_id = data.get("id") if data else None
        if not isinstance(target_id, int):
            self._json(400, {"error": "Nedostaje id."})
            return
        conn = get_connection()
        # Ownership + "doesn't already have Telegram" in one shot, same pattern as everywhere
        # else in this file.
        row = conn.execute(
            "SELECT city, ulica, broj FROM targets "
            "WHERE id = ? AND subscriber_id = ? AND telegram_chat_id IS NULL",
            (target_id, subscriber_id),
        ).fetchone()
        if not row:
            conn.close()
            self._json(404, {"error": "Adresa nije pronađena ili već ima Telegram."})
            return
        email = auth.get_subscriber_email(subscriber_id)
        # Reuse a live link rather than minting one per click - five clicks used to mean five
        # rows, which the dashboard then drew as five identical "Poveži Telegram" banners.
        code = self._live_pending_code(
            conn, subscriber_id, target_id, row["city"], row["ulica"], row["broj"]
        )
        if not code:
            code = secrets.token_urlsafe(8)
            conn.execute(
                "INSERT INTO pending_registrations "
                "(code, channel, city, ulica, broj, email, subscriber_id, attach_target_id) "
                "VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?)",
                (code, row["city"], row["ulica"], row["broj"], email, subscriber_id, target_id),
            )
            conn.commit()
        conn.close()
        self._json(200, {"ok": True, "telegram_link": f"https://t.me/{get_bot_username()}?start={code}"})

    def _api_delete_account(self):
        subscriber_id = self._current_subscriber_id()
        if not subscriber_id:
            self._json(401, {"error": "Nisi prijavljen."})
            return
        auth.delete_subscriber_completely(subscriber_id)
        self._json(200, {"ok": True}, extra_headers={"Set-Cookie": self._clear_cookie_header()})

    def _json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Session-scoped data (/api/me is one subscriber's addresses and email). Without this a
        # browser is free to reuse a stale copy, which is both a privacy smell on a shared
        # machine and a real source of "I refreshed and nothing changed" confusion.
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Plain HTTPServer handles ONE request at a time - confirmed for real, live on the VM: with
    port 80 open to the whole internet (not just Cloudflare), the constant background noise of
    scanners/garbage connections (TLS bytes sent to a plain-HTTP port, malformed requests) was
    enough to wedge the single-threaded server so it stopped answering ANY request, including a
    healthcheck from inside its own container. One slow/hostile connection blocking every other
    visitor is a real DoS risk on a server that's reachable by anyone, not a theoretical one."""
    daemon_threads = True


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set - see .env.example. Aborting.")
        return
    if not os.path.exists(RGZ_DB_PATH):
        print(f"❌ Missing {RGZ_DB_PATH} - autocomplete/validation needs it. Aborting.")
        return

    # 0.0.0.0, not "localhost" - binding to loopback only would make this unreachable from
    # outside its own container once this runs as the `api` docker-compose service; harmless
    # locally since nothing outside your machine can reach it there anyway.
    handler = partial(Handler, directory=WEB_DIR)
    httpd = ThreadingHTTPServer(("0.0.0.0", 8000), handler)
    print("🌐 Serving web/ + /register + /api/* on port 8000 (Ctrl+C to stop)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
