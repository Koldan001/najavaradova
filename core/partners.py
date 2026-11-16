"""Referral partners - who brought a subscriber, so the partner can be paid its share later.

A partner (a building-management platform, or a manager recommending us to colleagues) gets a
link like najavaradova.rs/?ref=<slug>. The signup form passes the ref along, and the account is
tagged with it at creation - first touch wins, an existing account is never re-attributed.
Operator-assisted signups use scripts/bulk_register.py --ref. Partners are managed and reported
on from the SSH CLI (scripts/partners.py), never a web page.
"""
import re

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")

# Monthly-plan list prices (EUR - what LemonSqueezy will charge) by how many active addresses an
# account has. 3/6/12-month plans are cheaper per month; the monthly price is the ceiling. Billing
# isn't live yet (free until 1 Jan 2027); these only feed the partner report's "what this would
# earn" column. (max_addresses, tier, price_eur) - first row whose max covers the count wins.
TIERS = (
    (2, "Free", 0),
    (10, "S", 12),
    (30, "M", 29),
    (100, "L", 59),
)


def normalize_slug(ref) -> str | None:
    """A well-formed slug, or None. Anything from a URL is untrusted - bad shape just means no ref."""
    if not isinstance(ref, str):
        return None
    slug = ref.strip().lower()
    return slug if SLUG_RE.match(slug) else None


def known_partner(conn, ref) -> str | None:
    """The partner slug if `ref` names a registered partner, else None - an unknown or made-up
    ref is silently ignored, so nobody can attribute signups to themselves by inventing one."""
    slug = normalize_slug(ref)
    if not slug:
        return None
    row = conn.execute("SELECT slug FROM partners WHERE slug = ?", (slug,)).fetchone()
    return row["slug"] if row else None


def tier_for(address_count: int) -> tuple[str, int | None]:
    """(tier name, monthly list price EUR) for an account with this many active addresses. Above the
    largest tier the price is by agreement - None."""
    for max_addresses, tier, price in TIERS:
        if address_count <= max_addresses:
            return tier, price
    return "Custom", None
