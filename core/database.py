import sqlite3
import os
import config

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_NAME = "data/outage_notifier.db"


def get_connection():
    conn = sqlite3.connect(DB_NAME, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

def init_db():
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Historical & Active Outages
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS outages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT NOT NULL,
            affected_streets TEXT,
            outage_date TEXT,
            time_window TEXT,
            raw_text TEXT NOT NULL,
            raw_text_hash TEXT UNIQUE,
            geojson_geometry TEXT,
            status TEXT DEFAULT 'ACTIVE',
            match_quality TEXT DEFAULT 'EXACT',
            event_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 1b. Additive migration: match_quality on databases created before this column existed.
    try:
        cursor.execute("ALTER TABLE outages ADD COLUMN match_quality TEXT DEFAULT 'EXACT'")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 1c. Additive migration: event_type was computed by matchmaker.py all along but only ever
    # printed, never stored - map coloring by utility type needs it persisted.
    try:
        cursor.execute("ALTER TABLE outages ADD COLUMN event_type TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 1d. Additive migration: category (WATER/POWER/HEATING) - each scraper already knows its own
    # utility deterministically (bvk_scraper is always water, eps_scraper always power, ...), so
    # this is passed explicitly rather than guessed from event_type's free text. Confirmed real
    # miss: a BVK entry with event_type="kvar" (no "voda" substring at all) rendered orange
    # instead of blue on the map - same class of bug as trusting Gemini to guess a city it was
    # never told. event_type stays as the descriptive free-text field; category is the clean,
    # scraper-provided enum map coloring actually keys off.
    try:
        cursor.execute("ALTER TABLE outages ADD COLUMN category TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 1e. Additive migration: source_url - the announcement page an outage was scraped from.
    # Scrapers have always known it and thrown it away, so nothing downstream could link back to
    # the original post for the full context (the raw_text stored here is a trimmed extract).
    try:
        cursor.execute("ALTER TABLE outages ADD COLUMN source_url TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 1f. Additive migration: expires_at - when the outage stops being shown on the map. Set by
    # matchmaker from the announcement's own end (a stated multi-day range, an explicit finish
    # date, or the end of the time window on the outage day); NULL when nothing datable was
    # stated. Before this every scraped row stayed ACTIVE forever and the map only ever grew.
    try:
        cursor.execute("ALTER TABLE outages ADD COLUMN expires_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 1g. Additive migration: parsed_json - the full parsed OutageAlert (affected_locations with
    # their parity/number-range/settlement, excluded_locations, message_template) as JSON.
    # matchmaker only ever stored the flattened affected_streets string, so re-verifying a
    # STORED outage against a target that registered AFTER it was scraped (the registration
    # backfill in matchmaker.backfill_target_against_active_outages) had nothing structured to
    # check house numbers/parity against without re-calling Gemini. Rows scraped before this
    # column existed have it NULL - backfill falls back to a looser hex+street-name match there.
    try:
        cursor.execute("ALTER TABLE outages ADD COLUMN parsed_json TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 2. Outage Hexes (Spatial Grid Mapping)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS outage_hexes (
            outage_id INTEGER NOT NULL,
            hex_id TEXT NOT NULL,
            PRIMARY KEY (outage_id, hex_id),
            FOREIGN KEY (outage_id) REFERENCES outages (id) ON DELETE CASCADE
        )
    ''')

    # 3. Activation Codes (Monetization)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activation_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 4. Client Targets (User Registration)
    # viber_link is nullable (rather than the original NOT NULL) now that Telegram is a target's
    # primary delivery channel instead - see migration 4b for databases created before this.
    # telegram_chat_id is deliberately NOT UNIQUE: one person watches many addresses, so one
    # chat_id legitimately owns many target rows (see migration 4f for old DBs that still have
    # the constraint). Duplicate (chat_id, city, ulica, broj) is guarded in the app instead.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activation_code_id INTEGER,
            viber_link TEXT UNIQUE,
            telegram_chat_id TEXT,
            telegram_username TEXT,
            city TEXT NOT NULL DEFAULT 'НОВИ САД',
            opstina TEXT,
            ulica TEXT NOT NULL,
            broj TEXT NOT NULL,
            naselje TEXT,
            lat REAL,
            lng REAL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (activation_code_id) REFERENCES activation_codes (id)
        )
    ''')

    # 4b. Additive migration: telegram columns on databases created before this existed.
    for ddl in (
        "ALTER TABLE targets ADD COLUMN telegram_chat_id TEXT",
        "ALTER TABLE targets ADD COLUMN telegram_username TEXT",
        # 4d. Email channel - the web form's other notification option besides Telegram. No
        # confirmation loop for now (matches the agreed MVP scope), so the web endpoint inserts
        # straight into targets instead of going through pending_registrations like Telegram does.
        "ALTER TABLE targets ADD COLUMN email TEXT",
        # 4e. One-click unsubscribe token embedded in every email footer - Telegram's equivalent
        # is /stop, which needs no token since the chat_id itself identifies the sender. Email has
        # no such built-in identity, so each target gets its own opaque token instead of, say,
        # unsubscribing by email address alone (which anyone could guess/enter for someone else).
        "ALTER TABLE targets ADD COLUMN unsub_token TEXT",
    ):
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists

    # 4c. Migration: relax viber_link's original NOT NULL constraint, which predates Telegram
    # support and would otherwise reject any Telegram-only registration. SQLite can't ALTER a
    # column's NOT NULL flag directly, so rebuild the table when an old-schema NOT NULL is found.
    cursor.execute("PRAGMA table_info(targets)")
    viber_link_col = next((c for c in cursor.fetchall() if c[1] == 'viber_link'), None)
    # 4f. Migration: drop the UNIQUE on telegram_chat_id (and NOT NULL on viber_link, the
    # original 4c reason). Both need the same table rebuild - SQLite can't ALTER a constraint
    # in place - so do it in one pass whenever EITHER old-schema marker is present.
    targets_ddl = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='targets'"
    ).fetchone()
    chat_id_is_unique = bool(targets_ddl) and 'telegram_chat_id TEXT UNIQUE' in targets_ddl[0]
    if (viber_link_col and viber_link_col[3] == 1) or chat_id_is_unique:  # notnull / unique
        # Column list must stay in sync with every ALTER above (email, unsub_token) or the
        # rebuild silently drops those columns and their data.
        cursor.executescript('''
            CREATE TABLE targets_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activation_code_id INTEGER,
                viber_link TEXT UNIQUE,
                telegram_chat_id TEXT,
                telegram_username TEXT,
                city TEXT NOT NULL DEFAULT 'НОВИ САД',
                opstina TEXT,
                ulica TEXT NOT NULL,
                broj TEXT NOT NULL,
                naselje TEXT,
                lat REAL,
                lng REAL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                email TEXT,
                unsub_token TEXT,
                FOREIGN KEY (activation_code_id) REFERENCES activation_codes (id)
            );
            INSERT INTO targets_new (id, activation_code_id, viber_link, telegram_chat_id,
                telegram_username, city, opstina, ulica, broj, naselje, lat, lng, is_active,
                created_at, email, unsub_token)
                SELECT id, activation_code_id, viber_link, telegram_chat_id,
                       telegram_username, city, opstina, ulica, broj, naselje, lat, lng, is_active,
                       created_at, email, unsub_token
                FROM targets;
            DROP TABLE targets;
            ALTER TABLE targets_new RENAME TO targets;
        ''')

    # 5. Target Hexes (Offline User H3 Res 10 Mapping)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS target_hexes (
            target_id INTEGER NOT NULL,
            hex_id TEXT NOT NULL,
            PRIMARY KEY (target_id, hex_id),
            FOREIGN KEY (target_id) REFERENCES targets (id) ON DELETE CASCADE
        )
    ''')

    # 6. Master Street Registry
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS street_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,                  
            opstina TEXT,                        
            street_name TEXT NOT NULL,           
            primary_neighborhood TEXT NOT NULL,  
            secondary_neighborhood TEXT,        
            UNIQUE(city, opstina, street_name)
        )
    ''')

    # 7. Worker Fleet
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fleet_workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT UNIQUE NOT NULL,
            role TEXT DEFAULT 'WORKER', 
            status TEXT DEFAULT 'ACTIVE', 
            assigned_region TEXT,
            last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 8. The Message Queue
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outage_id INTEGER,
            target_id INTEGER,
            worker_id INTEGER,
            custom_message TEXT,
            status TEXT DEFAULT 'QUEUED',
            send_attempts INTEGER DEFAULT 0,
            last_attempt_at TIMESTAMP,
            scheduled_for TIMESTAMP,
            sent_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (outage_id) REFERENCES outages (id),
            FOREIGN KEY (target_id) REFERENCES targets (id),
            FOREIGN KEY (worker_id) REFERENCES fleet_workers (id)
        )
    ''')

    # 8b. Additive migration: custom_message on databases created before this column existed.
    # matchmaker.py has always written to this column - on an old schema the first real target
    # match (none have occurred in testing so far) would crash with "no such column".
    try:
        cursor.execute("ALTER TABLE message_queue ADD COLUMN custom_message TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 8c. Additive migration: created_at - when a message was queued. The watchdog
    # (workers/watchdog.py) uses it to detect "QUEUED but not sent for > N minutes", i.e. the
    # sender worker is stuck/down. No column default here: SQLite forbids a CURRENT_TIMESTAMP
    # default on ALTER TABLE ADD COLUMN (non-constant), so the insert sites in matchmaker.py
    # set it explicitly with datetime('now'). Old rows keep NULL and the check ignores them.
    try:
        cursor.execute("ALTER TABLE message_queue ADD COLUMN created_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 8d. Additive migration: send-retry bookkeeping. send_attempts is in the base CREATE but
    # predates this migration on older DBs; last_attempt_at is new. Together they drive the
    # retry-with-backoff logic in workers/telegram_worker.py + workers/email_worker.py: a
    # transient send error bumps send_attempts and stamps last_attempt_at instead of marking the
    # row FAILED outright, and the worker skips the row until a quadratic backoff window
    # (send_attempts^2 * 60s) has passed. Only after MAX_ATTEMPTS does the row become FAILED.
    for _stmt in (
        "ALTER TABLE message_queue ADD COLUMN send_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE message_queue ADD COLUMN last_attempt_at TIMESTAMP",
    ):
        try:
            cursor.execute(_stmt)
        except sqlite3.OperationalError:
            pass  # column already exists

    # 9. Worker Group Memberships
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS worker_group_memberships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (worker_id) REFERENCES fleet_workers (id),
            FOREIGN KEY (target_id) REFERENCES targets (id),
            UNIQUE(worker_id, target_id)
        )
    ''')

  # 10. Settlement Center Cache
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settlement_cache (
            name TEXT PRIMARY KEY,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            hex_id TEXT NOT NULL,
            radius_steps INTEGER NOT NULL DEFAULT 25
        )
    ''')

    # 10b. Additive migration: provenance columns for cache auditing (see scripts/audit_settlement_cache.py).
    # Guarded with try/except since SQLite has no "ADD COLUMN IF NOT EXISTS" and this table
    # may already exist without these columns on databases created before this change.
    for ddl in (
        "ALTER TABLE settlement_cache ADD COLUMN source TEXT DEFAULT 'nominatim'",
        "ALTER TABLE settlement_cache ADD COLUMN verified_opstina TEXT",
    ):
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists

    # 10c. Negative cache: names that failed to geocode as a settlement at all. Without this,
    # a name that will never resolve (e.g. a street name mistakenly tried as a place by the
    # "prema X" direction fallback in spatial_engine.py) gets re-queried against live Nominatim
    # on every single call, forever, since only successes were ever cached before.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settlement_lookup_failures (
            name TEXT PRIMARY KEY,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 11. Settlement Polygon Cache (Zero-latency H3 boundary cache)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settlement_polygon_cache (
            name TEXT PRIMARY KEY,
            hex_ids_json TEXT NOT NULL
        )
    ''')

    # 12. Pending Registrations (web form -> Telegram deep-link handoff)
    # The web form writes a row here with the address + chosen channel and gets back a one-time
    # `code`. For the Telegram channel, the form redirects the browser straight to
    # https://t.me/<bot>?start=<code> - Telegram shows one Start button, no typing. The bot's
    # /start handler looks up `code` here, and on match inserts the real `targets` row with the
    # chat_id it just received, then marks this row consumed so the link can't be replayed.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            channel TEXT NOT NULL,
            city TEXT NOT NULL,
            ulica TEXT NOT NULL,
            broj TEXT NOT NULL,
            email TEXT,
            consumed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 12b. Additive migration: subscriber_id - links an in-flight Telegram deep-link back to
    # whoever confirmed the email that created it (see 13. below). NULL on rows written before
    # login was mandatory; the bot's complete_registration() only copies it when present.
    try:
        cursor.execute("ALTER TABLE pending_registrations ADD COLUMN subscriber_id INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 13. Subscribers (account identity - see core/auth.py)
    # The person, as distinct from the address (targets) and the payment (targets.paid_until).
    # One row per email; a subscriber can hold many targets across both channels. No password -
    # every login is a fresh magic-link email (core/auth.py), so there is nothing here to hash or
    # leak beyond the email address itself.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 14. Login tokens (magic-link, single use)
    # token_hash, never the raw token - see core/auth.py's hash_token(). A DB leak then hands out
    # unusable hashes, not working login links. purpose='login' (no payload - just re-establish a
    # session) or purpose='confirm' (payload_json carries the {city,ulica,broj,channel} address
    # the visitor was signing up when they typed their email - created on first click, not before).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS login_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id INTEGER NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            purpose TEXT NOT NULL,
            payload_json TEXT,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subscriber_id) REFERENCES subscribers (id) ON DELETE CASCADE
        )
    ''')

    # 15. Sessions (the cookie the dashboard actually runs on, once a login_token is consumed)
    # Same token_hash-not-plaintext reasoning as login_tokens. expires_at is a sliding window -
    # core/auth.py refreshes it forward on every authenticated request, so an active visitor is
    # never logged out mid-use; only real inactivity (or explicit logout / account deletion,
    # which deletes the row outright) ends a session.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id INTEGER NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subscriber_id) REFERENCES subscribers (id) ON DELETE CASCADE
        )
    ''')

    # 16. Additive migration: targets.subscriber_id (who owns this address - see core/auth.py)
    # and targets.paid_until (NULL = free tier; a real date once billing exists - schema only for
    # now, the dashboard just shows "Besplatno" while it's NULL). Both nullable: existing rows
    # (and any created before a subscriber exists, which shouldn't happen going forward but costs
    # nothing to tolerate) simply show as unowned/free.
    for ddl in (
        "ALTER TABLE targets ADD COLUMN subscriber_id INTEGER",
        "ALTER TABLE targets ADD COLUMN paid_until TIMESTAMP",
    ):
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists

    # 17. Additive migration: per-channel controls, so a single target row that carries both a
    # telegram_chat_id and an email can have one silenced without touching the other (until now
    # is_active was the only switch, and it gated the whole row - see the dashboard's "add a
    # second channel to the same address" feature). Both default to whatever is_active already
    # was, not a bare 1 - a target someone already deactivated must NOT come back active on the
    # very first boot after this migration runs. The backfill only runs the moment each column is
    # actually created (inside the same try as the ALTER) - a plain post-loop UPDATE would instead
    # re-stamp both columns from is_active on every single boot, silently undoing anyone's later
    # per-channel toggle the next time the app restarts.
    for ddl, backfill in (
        ("ALTER TABLE targets ADD COLUMN email_active INTEGER DEFAULT 1",
         "UPDATE targets SET email_active = is_active"),
        ("ALTER TABLE targets ADD COLUMN telegram_active INTEGER DEFAULT 1",
         "UPDATE targets SET telegram_active = is_active"),
    ):
        try:
            cursor.execute(ddl)
            cursor.execute(backfill)
        except sqlite3.OperationalError:
            pass  # column already exists - leave whatever's already there, don't re-stamp it

    # 17b. Additive migration: message_queue.channel - which channel a queued row is actually
    # for. Before this, one message_queue row per (outage, target) was raced by BOTH
    # telegram_worker and email_worker (whichever polled first flipped status to SENT/FAILED, and
    # the other worker's query - filtering on that same status column - never saw the row again).
    # Harmless while every real target only ever had one channel; the moment a target legally
    # carries both (see above), that race would silently drop one channel's delivery every time.
    # matchmaker now inserts one row per channel the target actually has (see
    # core/matchmaker.py's _match_and_queue_target) and each worker's query filters to its own
    # channel. Existing QUEUED/SENT/FAILED rows are unambiguous to backfill because no live target
    # had two channels before this - whichever one the row's target has (it can only be one) is
    # the channel that row was always for.
    try:
        cursor.execute("ALTER TABLE message_queue ADD COLUMN channel TEXT")
        cursor.execute('''
            UPDATE message_queue
            SET channel = (
                SELECT CASE WHEN t.telegram_chat_id IS NOT NULL THEN 'telegram' ELSE 'email' END
                FROM targets t WHERE t.id = message_queue.target_id
            )
            WHERE channel IS NULL
        ''')
    except sqlite3.OperationalError:
        pass  # column already exists

    # 17c. Additive migration: pending_registrations.attach_target_id. NULL (default, and every
    # row written before this) means "bot completion creates a brand-new target row", same as
    # always. Set means "attach the confirmed telegram_chat_id onto this EXISTING target instead"
    # - the dashboard's "+ Poveži Telegram" button on an address that already has email, so the
    # two channels end up on one row instead of a second, duplicate address.
    try:
        cursor.execute("ALTER TABLE pending_registrations ADD COLUMN attach_target_id INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 17c2. Additive migration: pending_registrations.reminded_at - when the one reminder email
    # for a Telegram signup that never pressed Start went out (core/signup_reminder.py). NULL =
    # not reminded.
    try:
        cursor.execute("ALTER TABLE pending_registrations ADD COLUMN reminded_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 17d. Referral partners (see core/partners.py). A partner - a building-management platform
    # or a manager who recommends us - gets a share of what the subscribers it brought pay.
    # subscribers.referred_by holds the partner slug, set once when the account is created and
    # never overwritten; NULL = came directly.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS partners (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            commission_pct INTEGER NOT NULL DEFAULT 25,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try:
        cursor.execute("ALTER TABLE subscribers ADD COLUMN referred_by TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 17e. Where a new account found us, for the operator's /stats: signup_source is a site's
    # name ("google", "facebook", "direct") or a tag from our own links (?src=), signup_page the
    # city page the visitor came through ("kraljevo"). Set once when the account is created,
    # like referred_by; NULL = created before this was recorded. See core/auth.clean_source.
    for column in ("signup_source", "signup_page"):
        try:
            cursor.execute(f"ALTER TABLE subscribers ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

    # 17f. Važno obaveštenje (core/broadcast.py): a rare notice for a whole city - the heating
    # season starting - sent only after the operator approves it on Telegram. One broadcast_sends
    # row per person reached, so an approval never sends twice. targets.broadcasts_off is the
    # person's "not these, thanks" (/obavestenja off); outage alerts don't read it.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            body TEXT NOT NULL,
            source_url TEXT,
            source_outage_id INTEGER UNIQUE,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            send_after TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS broadcast_sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            claimed_at TIMESTAMP,
            sent_at TIMESTAMP,
            UNIQUE (broadcast_id, target_id, channel)
        )
    ''')
    try:
        cursor.execute("ALTER TABLE targets ADD COLUMN broadcasts_off INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 17g. The signup form, counted (scripts/local_web_server.py /api/ev): how many open it and how
    # many send it, per page and day. Counts only - no visitor, no IP, nothing to link to anyone.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS signup_events (
            day TEXT NOT NULL,
            event TEXT NOT NULL,
            page TEXT NOT NULL,
            n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, event, page)
        )
    ''')

    # 17h. subscribers.confirm_reminded_at - when the one "potvrdi prijavu" reminder went out to a
    # signup that never clicked its confirmation link (core/signup_reminder.py). NULL = not reminded.
    try:
        cursor.execute("ALTER TABLE subscribers ADD COLUMN confirm_reminded_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 17i. A second look before a risky alert goes out (core/send_check.py): a whole-area match or
    # a date the notice doesn't state is held (message_queue status HELD) until our rule and a
    # second model call agree, or the operator decides.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS send_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outage_id INTEGER NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP
        )
    ''')

    # 17j. A building manager's list of addresses from /upravnici (core/managers.py): saved when
    # sent, loaded onto the account when its email link is clicked. target_ids_json = the
    # addresses that load created, so the operator's ❌ removes exactly those and nothing else.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS manager_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            name TEXT,
            channels TEXT NOT NULL,
            addresses_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            subscriber_id INTEGER,
            target_ids_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            loaded_at TIMESTAMP,
            removed_at TIMESTAMP
        )
    ''')
    # 17j2. pending_registrations.attach_all: one Telegram link that connects every address of
    # the account still without Telegram (a manager's whole list), instead of one link each.
    try:
        cursor.execute("ALTER TABLE pending_registrations ADD COLUMN attach_all INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 17j3. targets.manager_list: this address came in as a building manager's list (/upravnici,
    # scripts/bulk_register.py, or marked by hand with scripts/mark_manager.py). Only such an
    # owner gets the tenant-notice layout (core/notice.py), as does anyone with 10+ addresses -
    # before 05.11.2026 3+ was enough, and a family with a few flats got "Tekst za stanare".
    # On the first run, accounts that already loaded a /upravnici list are flagged. (10+ active
    # addresses count as a manager anyway - core/notice.MANAGER_MIN_ADDRESSES.)
    try:
        cursor.execute("ALTER TABLE targets ADD COLUMN manager_list INTEGER NOT NULL DEFAULT 0")
        cursor.execute(
            "UPDATE targets SET manager_list = 1 WHERE is_active = 1 AND subscriber_id IN "
            "(SELECT subscriber_id FROM manager_requests WHERE status = 'LOADED' "
            "AND subscriber_id IS NOT NULL)")
    except sqlite3.OperationalError:
        pass  # column already exists

    # 17k. Second check of every alert (core/alert_check.py): another model reads the notice on
    # its own and we record whether it would reach the same person on the same day. Shadow mode -
    # nothing waits on it; /stats shows how often the two agree.
    # verdict AGREE | DISAGREE | NEARBY (a "u blizini" alert, not judged) | ERROR (no answer).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_checks (
            outage_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            reason TEXT,
            model TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (outage_id, target_id)
        )
    ''')

    # 17l. The operator's weekly review (core/review.py): human labels, the ground truth the
    # accuracy figures in /stats are measured against.
    # kind: alert (was this alert right for this person?) | street (one place our reading of a
    #       notice names - right or wrong?) | missing (what our reading of that notice left out:
    #       `n` places, typed by the operator as a reply)
    # picked: random | disagree (the second check disagreed) | feedback (a reader said 👎) | notice
    # shadow: the second check's verdict at the time it was picked, to measure how far it can be
    #         trusted. verdict: RIGHT | WRONG, or NULL until the operator answers.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch TEXT NOT NULL,
            kind TEXT NOT NULL,
            picked TEXT NOT NULL,
            outage_id INTEGER NOT NULL,
            target_id INTEGER,
            place TEXT,
            shadow TEXT,
            verdict TEXT,
            n INTEGER,
            note TEXT,
            message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            labelled_at TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS review_batches (
            batch TEXT PRIMARY KEY,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 17m. A reader's own 👍 / 👎 on an alert (core/feedback.py): the button under a Telegram
    # alert, the link in an email. One answer per person and outage; a second tap changes it.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_feedback (
            outage_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            verdict TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (outage_id, target_id)
        )
    ''')

    # Performance Indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_target_hexes_hex ON target_hexes (hex_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_outage_hexes_hex ON outage_hexes (hex_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_targets_address ON targets (city, ulica, broj)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_targets_subscriber ON targets (subscriber_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_login_tokens_hash ON login_tokens (token_hash)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sessions_hash ON sessions (token_hash)')

    conn.commit()
    conn.close()
    print("✅ Database schema initialized with settlement_cache!")

if __name__ == "__main__":
    init_db()