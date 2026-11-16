"""Shared helpers for the per-utility scrapers."""
from datetime import datetime, timedelta

from spatial.spatial_engine import closest_rgz_naselje, get_rgz_naselje_streets


def published_line(place: str, posted: datetime | None) -> str:
    """The first line put in front of a post that says "данас" / "сутра" without a date: the
    publish date, and the two days spelled out. Left to work out "сутра" itself, the parser dated
    a 13.10.2026 post's "сутра" 13.10 (Šabac, Norveška)."""
    if not posted:
        return f"{place}."
    day = "%d.%m.%Y"
    return (f"{place}, објављено {posted.strftime(day)}. Данас је {posted.strftime(day)}, "
            f"сутра је {(posted + timedelta(days=1)).strftime(day)}.")


# How far past the scraper's date a notice can announce its own day and still be read as that day.
AHEAD_DAYS = 14


def use_scraper_date(parsed, date_str: str | None, raw_text: str) -> None:
    """Set the outage's date to the one the scraper found on the page, unless the notice names a
    later day of its own. For many sources the page's date is the day the post went up: Niš posts
    "обавештење за дан 18.09." about works "у суботу 19. септембра". Set over the model's 19.09.,
    the map showed the 18th and the outage expired before it began (#1979; 31 outages in 45 days,
    Niš, Kruševac, 30.10.2026). The model's day is kept only when the notice's text states it, it
    falls in the AHEAD_DAYS after the page's date, and it isn't the notice's end date."""
    if not date_str:
        return
    from core.matchmaker import _parse_date, notice_days
    page, own = _parse_date(date_str), _parse_date(parsed.outage_date or '')
    if (page and own and 0 < (own - page).days <= AHEAD_DAYS
            and (parsed.outage_date or '').strip() != (parsed.end_date or '').strip()
            and (own.day, own.month) in notice_days(raw_text)):
        parsed.outage_date = own.strftime("%d.%m.%Y")
        return
    parsed.outage_date = date_str


def drop_unknown_settlements(parsed, opstina: str):
    """Many city announcements scope a street to a named kvart ('МИЛОРАДОВИЋ' in Pančevo,
    'Шумарице' / 'Станово' in Kragujevac). A kvart is not a distinct RGZ naselje - the whole
    town is one - and matches no geocoder place either, so scoping a street to it makes the
    street UNRESOLVED even when RGZ carries it plainly under the town's own naselje. Clearing a
    settlement that is neither the opština itself nor a real RGZ naselje under it lets the street
    resolve against the town, where it actually sits. Villages (Ердеч, Долово, …) are real RGZ
    naselja and are kept.

    A settlement name RGZ doesn't know that is a letter or two off one real naselje of this
    opština is a misspelling ("ЈЕВРЕГОВАЦ" for Јевремовац) and is corrected first - whole-village
    notices included - so the location lands on that village, not on a fallback area."""
    want = (opstina or "").strip().upper()
    for loc in parsed.affected_locations:
        s = (loc.settlement or "").strip()
        if s and s.upper() != want:
            fixed = closest_rgz_naselje(s, opstina)
            if fixed:
                print(f"   ↳ settlement {s!r} read as RGZ naselje {fixed!r}")
                loc.settlement = s = fixed
        # Only clear street-scoped locations. An ENTIRE_SETTLEMENT *is* its settlement name —
        # clearing that would delete the location entirely (matchmaker skips a nameless one);
        # a kvart there is meant to fall through to the geocoder, not be dropped.
        if loc.location_type == "ENTIRE_SETTLEMENT" or not loc.main_street:
            continue
        if s and s.upper() != want and not get_rgz_naselje_streets(s, opstina):
            loc.settlement = None
