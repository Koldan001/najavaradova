"""
Regression tests for core/matchmaker.py's per-target matching logic — no DB required, these
test the pure decision functions directly.

Run with: pytest tests/test_matchmaker.py -v
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import date, timedelta

from core.matchmaker import (
    _streets_match,
    _location_matches_target,
    _match_eligible_locations,
    _inherit_settlement_context,
    _render_message,
    _message_dates_are_its_own,
    _date_span,
    _region_variants,
    _region_token_set,
    _outage_last_day,
    _match_and_queue_target,
    _target_channels,
    calculate_outage_hexes,
)
from parser import OutageAlert, StreetSegment
from spatial.spatial_engine import SERBIA_DB_PATH

requires_street_db = pytest.mark.skipif(
    not os.path.exists(SERBIA_DB_PATH),
    reason="data/serbia_streets.db not present locally",
)


def _seg(**kwargs) -> StreetSegment:
    defaults = dict(location_type="STREET", time_window="08:00 - 12:00")
    defaults.update(kwargs)
    return StreetSegment(**defaults)


# ---------------------------------------------------------------------------
# _streets_match — Serbian abbreviation / case-ending robustness
# ---------------------------------------------------------------------------

def test_streets_match_bridges_initial_abbreviation():
    # "Т.Рајића" (abbreviated) vs "Тозе Рајића" (full) — same street.
    assert _streets_match("Т.Рајића", "Тозе Рајића")


def test_streets_match_rejects_different_streets():
    assert not _streets_match("Планинска", "9. Новембар")


def test_streets_match_case_insensitive_exact():
    assert _streets_match("Синђелићева", "СИНЂЕЛИЋЕВА")


def test_streets_match_handles_missing_values():
    assert not _streets_match(None, "Планинска")
    assert not _streets_match("Планинска", None)


# ---------------------------------------------------------------------------
# _streets_match — a bare street-type classifier ("Trg", "Bulevar") must not
# match every street that starts with that word. Real case: an outage naming
# only "Trg" pulled in every square in town and scattered hexes across the map.
# ---------------------------------------------------------------------------

def test_streets_match_bare_trg_does_not_match_every_square():
    assert not _streets_match("Trg", "Trg Carice Milice")
    assert not _streets_match("Trg", "Trg Dositeja Obradovića")
    assert not _streets_match("ТРГ", "Трг младенаца")


def test_streets_match_bare_classifier_both_sides_reversed():
    assert not _streets_match("Bulevar oslobođenja", "Bulevar")
    assert not _streets_match("Ulica", "Ulica Jevrejska")


def test_streets_match_exact_trg_still_matches():
    # a settlement whose square really is registered as just "Trg"
    assert _streets_match("Trg", "ТРГ")


def test_streets_match_multiword_starting_with_classifier_still_matches():
    assert _streets_match("Trg Marije Trandafil", "Трг Марије Трандафил")


# ---------------------------------------------------------------------------
# _region_variants / _region_token_set — a target's city can carry a Belgrade/Niš
# gradska-opština suffix ("Beograd (Voždovac)") while the scraped outage names only
# the grad ("БЕОГРАД"). The macro-scope check has to bridge the two, or a real
# hex-overlapping outage is dropped from the candidate set (was: target #40 on
# Braće Jerkovića / Voždovac never matched outage #889 region "БЕОГРАД").
# ---------------------------------------------------------------------------

def test_region_variants_splits_parenthesised_submunicipality():
    v = _region_variants("Beograd (Voždovac)")
    assert "BEOGRAD (VOŽDOVAC)" in v
    assert "BEOGRAD" in v
    assert "VOŽDOVAC" in v


def test_region_variants_plain_name_is_single_token():
    assert _region_variants("Novi Sad") == {"NOVI SAD"}


def test_region_variants_empty_is_empty_set():
    assert _region_variants(None) == set()
    assert _region_variants("") == set()


def test_region_scope_bridges_cyrillic_grad_to_latin_submunicipality():
    # outage side: scraped Cyrillic grad only
    outage = _region_variants("БЕОГРАД")
    # target side: Latin city with the opština suffix the autocomplete produces
    target = _region_token_set("Beograd (Voždovac)", None, None)
    assert outage & target  # non-empty -> candidate is kept


def test_region_scope_still_separates_different_cities():
    assert not (_region_variants("НОВИ САД") & _region_token_set("Beograd (Voždovac)", None, None))


# ---------------------------------------------------------------------------
# _streets_match — ordinal spelling equivalence (Roman numeral / digit / word).
# A utility writes "Краља Петра Првог"; RGZ (and the user, from the autocomplete)
# has "Kralja Petra I". Same street.
# ---------------------------------------------------------------------------

def test_streets_match_roman_numeral_vs_ordinal_word():
    assert _streets_match("Kralja Petra I", "Краља Петра Првог")
    assert _streets_match("КРАЉА ПЕТРА ПРВОГ", "Kralja Petra I")


def test_streets_match_digit_vs_ordinal_word():
    # Same noun on both sides, only the ordinal spelling differs.
    assert _streets_match("Kralja Petra 1", "Kralja Petra Prvog")
    assert _streets_match("1 Vinogradi", "Prvi Vinogradi")


def test_streets_match_ordinal_does_not_merge_different_numbers():
    assert not _streets_match("Prvi maj", "Deveti maj")


def test_streets_match_ordinal_fold_keeps_rejecting_unrelated():
    # The digit-token path must not turn "9. Новембар" into a match for an unrelated street.
    assert not _streets_match("Планинска", "9. Новембар")
    assert not _streets_match("Осмог Марта", "Планинска")


# ---------------------------------------------------------------------------
# _match_eligible_locations — an approximate (FALLBACK/UNRESOLVED) outage's
# whole-settlement / landmark locations must not blanket-notify off hexes that
# are themselves only an approximation (real case: outage #792, "Делови насеља
# Рибњак" polygon clipped one Novi Sad cell and mis-hit Futoška).
# ---------------------------------------------------------------------------

def test_fallback_outage_drops_no_street_locations():
    street = _seg(location_type="STREET", main_street="КАМЕНИЧКИ ПУТ")
    whole = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="РИБЊАК")
    landmark = _seg(location_type="LANDMARK", main_street=None)
    locs = [whole, street, landmark]
    assert _match_eligible_locations(locs, "FALLBACK") == [street]
    assert _match_eligible_locations(locs, "UNRESOLVED") == [street]


def test_exact_outage_keeps_all_locations():
    street = _seg(location_type="STREET", main_street="КАМЕНИЧКИ ПУТ")
    whole = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="РИБЊАК")
    locs = [whole, street]
    assert _match_eligible_locations(locs, "EXACT") == locs
    assert _match_eligible_locations(locs, None) == locs


def test_exact_outage_landmark_cannot_blanket_notify():
    # Real case: outage #2178 (Novi Sad, 22.09.2026) named Железничка + Васе Стајића and four
    # bank buildings as LANDMARK entries. The banks have no hexes of their own, but as
    # no-street locations they matched a Danila Kiša resident whose hex merely overlapped a
    # named street's - skipping the street-name check entirely.
    street = _seg(location_type="STREET", main_street="ЖЕЛЕЗНИЧКА")
    bank = _seg(location_type="LANDMARK", main_street=None, landmark="НАРОДНА БАНКА")
    whole = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="РИБЊАК")
    eligible = _match_eligible_locations([street, bank, whole], "EXACT")
    assert bank not in eligible
    assert street in eligible and whole in eligible
    assert not any(_location_matches_target(loc, "Danila Kiša", "25") for loc in
                   _match_eligible_locations([street, bank], "EXACT"))


# ---------------------------------------------------------------------------
# _inherit_settlement_context — "Делови насеља Рибњак; Делови улица: …" - the
# streets belong to Рибњак; carry that scope onto the bare STREET segments so
# they don't resolve against a same-named street in the city proper.
# ---------------------------------------------------------------------------

def test_settlement_context_flows_to_bare_streets():
    whole = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="РИБЊАК")
    bare = _seg(location_type="STREET", main_street="ЦАРА ЛАЗАРА")
    owns = _seg(location_type="STREET", main_street="ГАЈЕВА", settlement="ЧЕНЕЈ")
    out = _inherit_settlement_context([whole, bare, owns])
    assert out[0].settlement == "РИБЊАК"
    assert out[1].settlement == "РИБЊАК"   # inherited
    assert out[2].settlement == "ЧЕНЕЈ"    # kept its own


def test_settlement_context_does_not_reach_streets_before_it():
    before = _seg(location_type="STREET", main_street="ФУТОШКА")
    whole = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="РИБЊАК")
    after = _seg(location_type="STREET", main_street="ЦАРА ЛАЗАРА")
    out = _inherit_settlement_context([before, whole, after])
    assert out[0].settlement is None      # untouched - it came first
    assert out[2].settlement == "РИБЊАК"


def test_settlement_context_switches_between_areas():
    a = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="A")
    sa = _seg(location_type="STREET", main_street="X")
    b = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="B")
    sb = _seg(location_type="STREET", main_street="Y")
    out = _inherit_settlement_context([a, sa, b, sb])
    assert out[1].settlement == "A"
    assert out[3].settlement == "B"


# ---------------------------------------------------------------------------
# _location_matches_target — the street-scoping fix
# ---------------------------------------------------------------------------

def test_location_with_street_and_range_requires_matching_street():
    """Before this fix, a location's number_range applied regardless of which street the
    candidate target actually lived on — a target on a totally different street could match
    purely because their house number happened to fall in the range."""
    loc = _seg(main_street="ПЛАНИНСКА", number_range="17-53")

    assert _location_matches_target(loc, "Планинска", "21")       # right street, in range
    assert not _location_matches_target(loc, "Планинска", "61")   # right street, out of range
    assert not _location_matches_target(loc, "9. Новембар", "21")  # wrong street entirely


def test_entire_settlement_location_has_no_street_scope():
    """A location with no main_street (ENTIRE_SETTLEMENT) legitimately covers everyone the hex
    match already found — no street name to scope against."""
    loc = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None)
    assert _location_matches_target(loc, "Било која улица", "1")


# ---------------------------------------------------------------------------
# Number-range scoping — a neighbour on the same street but outside the announced
# range must NOT be notified. This is the whole promise of the product: people only
# hear from us when it's actually their address.
# ---------------------------------------------------------------------------

def test_same_street_outside_announced_range_is_not_notified():
    """'Руменачка 39-53' — #45 is affected, #100 lives on the same street but well outside the
    announced range and must be left alone."""
    affected = [_seg(main_street="РУМЕНАЧКА", number_range="39-53")]

    def is_matched(street, number):
        return any(_location_matches_target(loc, street, number) for loc in affected)

    assert is_matched("Руменачка", "45")          # inside the range
    assert is_matched("Руменачка", "39")          # lower bound, inclusive
    assert is_matched("Руменачка", "53")          # upper bound, inclusive
    assert not is_matched("Руменачка", "100")     # same street, outside range
    assert not is_matched("Руменачка", "38")      # one below the range
    assert not is_matched("Руменачка", "54")      # one above the range


def test_same_street_wrong_parity_is_not_notified():
    """'parna' (even) side only — an odd-numbered house on that street stays quiet."""
    affected = [_seg(main_street="В.Р.ПУТНИКА", parity="PARNA", number_range="20-40")]

    def is_matched(street, number):
        return any(_location_matches_target(loc, street, number) for loc in affected)

    assert is_matched("В.Р.Путника", "22")        # even, in range
    assert not is_matched("В.Р.Путника", "23")    # odd — wrong side of the street
    assert not is_matched("В.Р.Путника", "60")    # even but outside the range


def test_same_street_not_in_explicit_number_list_is_not_notified():
    """'Матије Гупца 41, 43 и 50' — only those three houses, nobody else on the street."""
    affected = [_seg(main_street="МАТИЈЕ ГУПЦА", explicit_numbers="41, 43, 50")]

    def is_matched(street, number):
        return any(_location_matches_target(loc, street, number) for loc in affected)

    assert is_matched("Матије Гупца", "43")
    assert not is_matched("Матије Гупца", "42")   # between listed numbers, not listed
    assert not is_matched("Матије Гупца", "51")


def test_street_with_no_number_scope_notifies_whole_street():
    """No range and no explicit numbers means the entire street is affected — everyone on it
    should be notified, which is the correct default for most announcements."""
    affected = [_seg(main_street="ЦАНКАРЕВА")]
    assert _location_matches_target(affected[0], "Цанкарева", "1")
    assert _location_matches_target(affected[0], "Цанкарева", "999")


# ---------------------------------------------------------------------------
# Past-dated outages must never generate a notification, however they got queued
# (a source re-publishing an old post -> new hash -> re-ingest; a NULL expires_at
# backfilled onto a late signup). See outage #1063 — nstoplana re-touched August
# posts and one mailed three live users.
# ---------------------------------------------------------------------------

def _dmy(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def test_outage_last_day_uses_single_date_when_no_end_date():
    alert = OutageAlert(city="НОВИ САД", outage_date="26.08.2026", affected_locations=[])
    assert _outage_last_day(alert).date() == date(2026, 8, 26)


def test_outage_last_day_prefers_multi_day_end_date():
    alert = OutageAlert(city="НОВИ САД", outage_date="26.08.2026", end_date="02.09.2026",
                        affected_locations=[])
    assert _outage_last_day(alert).date() == date(2026, 9, 2)


def test_outage_last_day_none_when_date_unparseable():
    alert = OutageAlert(city="НОВИ САД", outage_date="N/A", affected_locations=[])
    assert _outage_last_day(alert) is None


def test_match_and_queue_target_skips_a_past_dated_outage():
    """The guard is the first thing in the function and returns before touching the cursor or
    the target — pass sentinels that would blow up if it fell through."""
    yesterday = _dmy(date.today() - timedelta(days=1))
    alert = OutageAlert(city="НОВИ САД", outage_date=yesterday,
                        message_template="Prekid *** ", affected_locations=[])
    assert _match_and_queue_target(None, 1, alert, [], None, set()) == (0, 0, 0)


@requires_street_db
def test_village_entire_settlement_stays_exact_and_notifies():
    """Regression guard for the city-district downgrade in calculate_outage_hexes: a real
    village whole-settlement outage (Меленци) must keep EXACT so everyone in it is still
    notified. Only a large city sub-district (a Zrenjanin mesna zajednica like "Д2") should be
    downgraded to FALLBACK - see outage #1106."""
    alert = OutageAlert(
        city="ЗРЕЊАНИН", outage_date="09.09.2026",
        affected_locations=[StreetSegment(
            location_type="ENTIRE_SETTLEMENT", main_street=None,
            settlement="МЕЛЕНЦИ", time_window="08:00 - 16:00")],
    )
    hexes, quality = calculate_outage_hexes(alert, "")
    assert hexes
    assert quality == 'EXACT'


def test_target_channels_skips_email_when_email_active_is_zero():
    """A row is queued per channel _target_channels returns. The email worker's send-time
    predicate requires email_active=1, so queueing an email row for email_active=0 strands it
    as QUEUED forever and trips the "queue not draining" watchdog. Real case: queue row #25."""
    unsubbed = {"telegram_chat_id": 111, "email": "x@example.com", "email_active": 0}
    assert _target_channels(unsubbed) == ["telegram"]

    active = {"telegram_chat_id": 111, "email": "x@example.com", "email_active": 1}
    assert _target_channels(active) == ["telegram", "email"]

    # A caller whose SELECT omits the column is treated as active (worker still gates at send).
    legacy = {"telegram_chat_id": None, "email": "x@example.com"}
    assert _target_channels(legacy) == ["email"]


def test_match_and_queue_target_does_not_skip_todays_outage():
    """An outage dated today still notifies — the guard is strictly-before-today, so it must
    fall through past the early return (and then fail on the None cursor/target sentinels)."""
    today = _dmy(date.today())
    alert = OutageAlert(city="НОВИ САД", outage_date=today,
                        message_template="Prekid *** ", affected_locations=[])
    with pytest.raises((TypeError, AttributeError)):
        _match_and_queue_target(None, 1, alert, [], None, set())


# ---------------------------------------------------------------------------
# Message templating — one *** template per announcement, personalised per target
# ---------------------------------------------------------------------------

def _alert(template: str, **kwargs) -> OutageAlert:
    defaults = dict(city="НОВИ САД", outage_date="26.08.2026", message_template=template,
                    affected_locations=[])
    defaults.update(kwargs)
    return OutageAlert(**defaults)


def test_render_message_substitutes_recipients_street():
    alert = _alert("Planirano isključenje tople vode u ulici *** dana 26.08.2026.")
    loc = _seg(main_street="ЦАНКАРЕВА", time_window="N/A")
    assert _render_message(alert, loc, "Цанкарева") == (
        "Planirano isključenje tople vode u ulici Cankareva dana 26.08.2026."
    )


def test_render_message_appends_time_window_when_template_omits_it():
    # toplana outage #915: template carried street + date, no hours; time_window '08:00 - 10:00'
    # was parsed and must reach the recipient.
    alert = _alert("Prekid u isporuci tople vode u ulici *** dana 09.09.2026.")
    loc = _seg(main_street="ДАНИЛА КИША", time_window="08:00 - 10:00")
    msg = _render_message(alert, loc, "Данила Киша", category="HEATING")
    assert "08:00 - 10:00" in msg
    assert msg.count("08:00 - 10:00") == 1


@pytest.mark.parametrize("template, outage_date", [
    # The real ones, 30.10.2026: BVK's week of night flushing in Voždovac split per night, each
    # night's template still written from the notice's opening "od 2. novembra".
    ("Planirano isključenje vode u ulici *** dana 02.11.2026. u periodu 22:00 - 06:00.", "05.11.2026"),
    ("Planirano isključenje vode u ulici *** dana 02.11.2026. u periodu 22:00 - 06:00.", "06.11.2026"),
    ("Planirano isključenje vode u ulici *** u periodu 02.11 - 07.11.2026. od 22:00 do 06:00 "
     "časova zbog ispiranja vodovodne mreže.", "04.11.2026"),
])
def test_render_message_rebuilds_a_template_naming_another_day(template, outage_date):
    alert = _alert(template, outage_date=outage_date)
    loc = _seg(main_street="БРАЋЕ ЈЕРКОВИЋ", time_window="22:00 - 06:00")
    msg = _render_message(alert, loc, "Браће Јерковић", category="WATER")
    assert msg == f"Planirano isključenje vode u ulici Braće Jerković dana {outage_date} u periodu 22:00 - 06:00."
    assert _message_dates_are_its_own(msg, alert, loc)


@pytest.mark.parametrize("text, outage_date, end_date, window, ok", [
    ("dana 05.11.2026. u periodu 22:00 - 06:00", "05.11.2026", None, "22:00 - 06:00", True),
    # a night window ends the next morning: "05/06.11." and "do 06.11. u 06:00" are its own
    ("u noći 05/06.11.2026. od 22:00 do 06:00", "05.11.2026", None, "22:00 - 06:00", True),
    ("u noći 05/06.11.2026. od 22:00 do 06:00", "05.11.2026", None, "08:00 - 12:00", False),
    ("u noći 31/01.11.2026.", "31.10.2026", None, "22:00 - 06:00", True),
    # a multi-day notice: any day from its date to its end date
    ("od 30.09. do 01.10.2026.", "30.09.2026", "01.10.2026", "", True),
    ("traju od 21.10.2026 do 25.10.2026", "21.10.2026", "25.10.2026", "", True),
    ("od 30.09. do 03.10.2026.", "30.09.2026", "01.10.2026", "", False),
    ("dana 26.08.2026.", "27.08.2026", None, "08:00 - 12:00", False),
    ("dana 26.08.2027.", "26.08.2026", None, "08:00 - 12:00", False),
    # clock times written with a dot are not dates
    ("u periodu 22.00 - 06.00, 12.10 do 13.30", "05.11.2026", None, "22:00 - 06:00", True),
    # no stored date to compare with: nothing to say
    ("dana 02.11.2026.", "N/A", None, "", True),
])
def test_message_dates_are_its_own(text, outage_date, end_date, window, ok):
    alert = _alert("", outage_date=outage_date, end_date=end_date)
    assert _message_dates_are_its_own(text, alert, _seg(time_window=window)) is ok


def test_render_message_does_not_duplicate_a_time_window_already_in_the_template():
    alert = _alert("Nestanak struje u ulici *** u periodu 08:00 - 14:00.")
    loc = _seg(main_street="ПАРТИЗАНСКА", time_window="08:00 - 14:00")
    msg = _render_message(alert, loc, "Партизанска", category="POWER")
    assert msg.count("08:00 - 14:00") == 1


# ---------------------------------------------------------------------------
# _render_message — the model's template must not contradict the known utility
# ---------------------------------------------------------------------------

def test_render_message_rejects_a_template_naming_the_wrong_utility():
    """The real bug: an EPS row is bare "Opština | Vreme | Ulice" with no utility word, so the
    model announced a power cut as a water one. The scraper's own category is ground truth."""
    alert = _alert("Planirano isključenje vode u ulici *** dana 26.08.2026.")
    loc = _seg(main_street="ПАРТИЗАНСКА", time_window="08:00 - 14:00")
    msg = _render_message(alert, loc, "Партизанска", category="POWER")
    assert "vode" not in msg.lower()
    assert "struje" in msg.lower()
    assert "Partizanska" in msg
    assert "26.08.2026" in msg and "08:00 - 14:00" in msg


def test_render_message_keeps_a_template_that_matches_its_category():
    for category, template, keep in [
        ("POWER", "Nestanak struje u ulici ***.", "struje"),
        ("POWER", "Isključenje električne energije u ulici ***.", "električne"),
        ("POWER", "Prekid napajanja u ulici ***.", "napajanja"),
        ("WATER", "Planirano isključenje vode u ulici ***.", "vode"),
        ("WATER", "Prekid u vodosnabdevanju u ulici ***.", "vodosnabdevanju"),
        # A toplana supplies heating AND hot water; every one of these is a legitimate,
        # correct HEATING message and none may be rewritten.
        ("HEATING", "Isključenje tople vode u ulici ***.", "tople vode"),
        ("HEATING", "Prekid grejanja u ulici ***.", "grejanja"),
        ("HEATING", "Prekid u isporuci toplotne energije u ulici ***.", "toplotne energije"),
        ("HEATING", "Bez grejanja i tople vode u ulici ***.", "grejanja i tople vode"),
    ]:
        msg = _render_message(_alert(template), _seg(main_street="ЦАНКАРЕВА"),
                              "Цанкарева", category=category)
        assert keep in msg, (category, template, msg)
        assert "Cankareva" in msg


def test_render_message_heating_fallback_claims_neither_heating_nor_hot_water_alone():
    """A toplana cut can be heating, hot water, or both. When the model's template is unusable
    the rebuilt sentence must not pick one and be wrong about it - it names the supply."""
    alert = _alert("Nestanak struje u ulici ***.")          # plainly wrong for a toplana
    msg = _render_message(alert, _seg(main_street="ЦАНКАРЕВА"), "Цанкарева", category="HEATING")
    assert "struje" not in msg.lower()
    assert "toplotne energije" in msg
    assert "grejanje i topla voda" in msg


def test_render_message_water_outage_is_not_described_as_heating():
    alert = _alert("Prekid grejanja u ulici ***.")
    msg = _render_message(alert, _seg(main_street="ЦАНКАРЕВА"), "Цанкарева", category="WATER")
    assert "grejanja" not in msg.lower()
    assert "vode" in msg.lower()


def test_render_message_leaves_template_alone_when_category_unknown():
    alert = _alert("Planirano isključenje vode u ulici ***.")
    loc = _seg(main_street="ЦАНКАРЕВА", time_window="N/A")
    msg = _render_message(alert, loc, "Цанкарева", category=None)
    assert msg == "Planirano isključenje vode u ulici Cankareva."


def test_render_message_fallback_is_neutral_when_category_unknown():
    alert = _alert("")
    msg = _render_message(alert, _seg(main_street="ЦАНКАРЕВА"), "Цанкарева")
    assert "Planirano isključenje" in msg and "Cankareva" in msg


def test_render_message_transliterates_to_latin():
    """The scraped source is Cyrillic but notifications go out in Latin — the substituted street
    must not reintroduce Cyrillic into an otherwise Latin sentence."""
    alert = _alert("Isključenje u ulici ***.")
    rendered = _render_message(alert, _seg(main_street="ЈАНКА ЧМЕЛИКА", time_window="N/A"),
                               "Јанка Чмелика")
    assert rendered == "Isključenje u ulici Janka Čmelika."
    assert not any('Ѐ' <= ch <= 'ӿ' for ch in rendered)


def test_render_message_includes_house_number_scope():
    """When the outage is limited to part of a street, the recipient should see that."""
    alert = _alert("Isključenje u ulici ***.")
    loc = _seg(main_street="РУМЕНАЧКА", number_range="39-53", time_window="N/A")
    assert _render_message(alert, loc, "Руменачка") == "Isključenje u ulici Rumenačka 39-53."


def test_render_message_falls_back_to_settlement_for_whole_settlement_outage():
    alert = _alert("Isključenje u naselju ***.")
    loc = _seg(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="ВЕТЕРНИК",
               time_window="N/A")
    assert _render_message(alert, loc, "Било која") == "Isključenje u naselju Veternik."


def test_render_message_appends_street_when_model_omits_placeholder():
    """The model occasionally returns a template with no *** in it — the street must still reach
    the recipient rather than being silently dropped."""
    alert = _alert("Planirano isključenje tople vode dana 26.08.2026.")
    rendered = _render_message(alert, _seg(main_street="ЦАНКАРЕВА"), "Цанкарева")
    assert "Cankareva" in rendered


def test_render_message_survives_empty_template():
    alert = _alert("")
    assert "Cankareva" in _render_message(alert, _seg(main_street="ЦАНКАРЕВА"), "Цанкарева")


# ---------------------------------------------------------------------------
# Exclusion pattern ("осим" / "izuzev" / "sem") — outage #21 (Чалма) scenario
# ---------------------------------------------------------------------------

def test_exclusion_carves_out_matching_target():
    """'Чалма: већи део села осим Планинска 17-53' — a target on Planinska #20 should be
    excluded even though the broad ENTIRE_SETTLEMENT location would otherwise match them."""
    affected = [_seg(location_type="ENTIRE_SETTLEMENT", main_street=None)]
    excluded = [_seg(main_street="ПЛАНИНСКА", number_range="17-53")]

    target_on_excluded_street = ("Планинска", "21")
    target_elsewhere = ("Мала", "5")

    def is_matched(target_street, target_number):
        matched = any(_location_matches_target(loc, target_street, target_number) for loc in affected)
        if matched and excluded:
            if any(_location_matches_target(excl, target_street, target_number) for excl in excluded):
                matched = False
        return matched

    assert not is_matched(*target_on_excluded_street)  # carved out
    assert is_matched(*target_elsewhere)                # still covered by the settlement-wide match


def test_exclusion_does_not_affect_other_streets_in_range():
    """The excluded number_range is scoped to its own street — a target on a different street
    with the same house number must NOT be excluded."""
    excluded = [_seg(main_street="ПЛАНИНСКА", number_range="17-53")]
    # Same number (20), different street — should NOT be caught by the exclusion.
    assert not any(_location_matches_target(excl, "Мала", "20") for excl in excluded)


# ---------------------------------------------------------------------------
# calculate_outage_hexes — settlement-size-aware fallback + match_quality
# ---------------------------------------------------------------------------

@requires_street_db
def test_settlement_fallback_geocoded_to_another_town_is_blocked():
    """'Јанда' (an informal weekend-zone lane near Stara Pazova's Виногради) isn't a real indexed
    street, so the matcher falls back to the settlement. This test used to assert that fallback
    produced hexes - and it did, but in the wrong town: "Виногради" is a generic toponym, and the
    geocoder's answer for it is the Vinogradi in Kragujevac (44.03, 20.89), ~110 km from Stara
    Pazova. Those hexes would have notified Kragujevac for a Stara Pazova outage. The leak gate
    keeps everything inside the announced opština: no hexes, flagged UNRESOLVED for review."""
    from spatial.spatial_engine import envelope_distance_km, h3, region_envelope
    alert = {
        "city": "СТАРА ПАЗОВА",
        "outage_date": "N/A",
        "affected_locations": [{
            "location_type": "VECTOR", "settlement": "СЛ.ВИНОГРАДИ", "main_street": "ЈАНДА",
            "time_window": "x",
        }],
    }
    hexes, quality = calculate_outage_hexes(alert)
    env = region_envelope("СТАРА ПАЗОВА")
    assert all(envelope_distance_km(env, *h3.cell_to_latlng(h)) <= 0 for h in hexes)
    assert quality == "UNRESOLVED"


@requires_street_db
def test_unresolved_street_in_big_city_does_not_spam_whole_city():
    """'Горењска' isn't in the local street DB for Subotica (a real, confirmed data gap — OSM
    does have a street by this name, but it's in Belgrade, ~44.78/20.51, correctly filtered out
    by the settlement-distance check rather than wrongly accepted), and Subotica itself is a real
    city (radius_steps=94, far above village scale). Falling back to settlement-wide coverage
    there would notify people many km from the real outage — this must stay UNRESOLVED instead,
    not silently broadcast city-wide."""
    alert = {
        "city": "СУБОТИЦА",
        "outage_date": "N/A",
        "affected_locations": [{
            "location_type": "STREET", "settlement": "СУБОТИЦА", "main_street": "ГОРЕЊСКА",
            "time_window": "x",
        }],
    }
    hexes, quality = calculate_outage_hexes(alert)
    assert hexes == []
    assert quality == "UNRESOLVED"


@requires_street_db
def test_one_unresolved_street_does_not_blanket_the_settlement():
    """An announcement where most streets resolve precisely must NOT also pull in whole-settlement
    coverage on account of one straggler — that would drown the precise streets in tens of
    thousands of unrelated addresses. Real case: a 12-street Звездара announcement was adding 279
    settlement hexes alongside ~24 real ones. The outage is still flagged UNRESOLVED for review,
    but only genuinely-affected addresses get notified."""
    alert = {
        "city": "ЗВЕЗДАРА",
        "outage_date": "N/A",
        "affected_locations": [
            {"location_type": "STREET", "settlement": "ЗВЕЗДАРА", "main_street": "БУКУРЕШКА",
             "time_window": "x"},
            # Not a real street anywhere in Zvezdara — the straggler.
            {"location_type": "STREET", "settlement": "ЗВЕЗДАРА", "main_street": "НЕПОСТОЈЕЋА УЛИЦА XYZ",
             "time_window": "x"},
        ],
    }
    hexes, quality = calculate_outage_hexes(alert)

    # Букурешка's own hexes are present and the count stays street-scale, not settlement-scale.
    assert hexes
    assert len(hexes) < 50, f"expected street-scale coverage, got {len(hexes)} hexes"
    assert quality == "UNRESOLVED"


@requires_street_db
def test_failed_segment_clip_falls_back_to_whole_street_not_settlement():
    """Прешевска (Звездара) is digitized as two disjoint chains, and its announced cross-street
    bounds land on different ones — clip_polyline_by_intersections correctly refuses to combine
    them. The result must be the whole street (over-covers by one street), never the whole
    municipality."""
    alert = {
        "city": "ЗВЕЗДАРА",
        "outage_date": "N/A",
        "affected_locations": [{
            "location_type": "VECTOR", "settlement": "ЗВЕЗДАРА", "main_street": "ПРЕШЕВСКА",
            "intersection_start": "БАТУТОВА", "intersection_end": "ЧИНГРИЈИНА",
            "time_window": "x",
        }],
    }
    hexes, quality = calculate_outage_hexes(alert)

    assert hexes, "clip failure must not zero out a street that is otherwise well known"
    assert len(hexes) < 50, f"expected whole-street scale, got {len(hexes)} hexes"
    assert quality == "FALLBACK"


@requires_street_db
def test_cleanly_resolved_outage_is_exact():
    alert = {
        "city": "ПАНЧЕВО",
        "outage_date": "N/A",
        "affected_locations": [{
            "location_type": "STREET", "settlement": "ПАНЧЕВО", "main_street": "СИНЂЕЛИЋЕВА",
            "intersection_start": "С.МИЛЕТИЋА", "intersection_end": "Б.ЈОВАНОВИЋ",
            "time_window": "x",
        }],
    }
    hexes, quality = calculate_outage_hexes(alert)
    assert hexes
    assert quality == "EXACT"


# ---------------------------------------------------------------------------
# #3465 (30.09.2026): Toplana Cerak's remont - "celo grejno područje toplane Cerak", no hours,
# until 01.10. Two people were told "u ulici Cerak dana 30.09.2026. u periodu 07:00 - 19:00."
# ---------------------------------------------------------------------------

def _cerak(**kwargs):
    return _seg(location_type="ENTIRE_SETTLEMENT", settlement="ЦЕРАК", main_street=None, **kwargs)


def test_a_whole_area_match_names_the_area_not_a_street():
    alert = _alert("Prekid u isporuci tople vode u ulici *** dana 30.09.2026. u periodu 08:00 - 12:00.",
                   outage_date="30.09.2026")
    msg = _render_message(alert, _cerak(), "Репишка", category="HEATING")
    assert msg == "Prekid u isporuci tople vode u naselju Cerak dana 30.09.2026. u periodu 08:00 - 12:00."


def test_a_whole_area_match_on_the_fallback_sentence_names_the_area_too():
    alert = _alert("", outage_date="30.09.2026")
    msg = _render_message(alert, _cerak(time_window=""), "Репишка", category="WATER")
    assert msg == "Planirano isključenje vode u naselju Cerak dana 30.09.2026."


def test_a_multi_day_notice_without_hours_states_its_days_not_made_up_hours():
    template = "Planirano isključenje toplotne energije u ulici *** dana 30.09.2026. u periodu 07:00 - 19:00."
    alert = _alert(template, city="БЕОГРАД", outage_date="30.09.2026", end_date="01.10.2026",
                   affected_locations=[
                       _seg(main_street="ПОРУЧНИКА СПАСИЋА И МАШЕРЕ", explicit_numbers="4", time_window=""),
                       _cerak(time_window="")])
    area = _render_message(alert, alert.affected_locations[1], "Репишка", category="HEATING")
    assert area == ("Planirani prekid u isporuci toplotne energije (grejanje i topla voda) u naselju Cerak "
                    "od 30.09. do 01.10.2026.")
    street = _render_message(alert, alert.affected_locations[0], "Поручника Спасића и Машере", category="HEATING")
    assert "07:00" not in street and " u ulici " in street and street.endswith(" 4 od 30.09. do 01.10.2026.")


def test_a_multi_day_notice_with_stated_hours_keeps_its_template():
    alert = _alert("Bez vode u ulici *** od 30.09. do 01.10.2026, svakog dana 08:00 - 16:00.",
                   outage_date="30.09.2026", end_date="01.10.2026",
                   affected_locations=[_seg(main_street="ЦАНКАРЕВА", time_window="08:00 - 16:00")])
    msg = _render_message(alert, alert.affected_locations[0], "Цанкарева", category="WATER")
    assert msg == "Bez vode u ulici Cankareva od 30.09. do 01.10.2026, svakog dana 08:00 - 16:00."


def test_the_date_span_writes_the_year_once_unless_it_changes():
    assert _date_span(_alert("x", outage_date="30.09.2026", end_date="01.10.2026")) == "od 30.09. do 01.10.2026."
    assert _date_span(_alert("x", outage_date="30.12.2026", end_date="02.01.2027")) == "od 30.12.2026. do 02.01.2027."
    assert _date_span(_alert("x", outage_date="30.09.2026", end_date="30.09.2026")) is None
    assert _date_span(_alert("x", outage_date="30.09.2026")) is None


def test_a_street_never_matches_part_of_another_streets_name():
    """Outage #4264 (09.10.2026) listed a street named "Нова"; plain substring containment sent its
    power cut to Prešernova 13, 13A and Puškinova 13."""
    from core.matchmaker import _streets_match
    for target in ("Prešernova", "Puškinova", "Novosadska", "Jovanova", "Ivanova"):
        assert not _streets_match("НОВА", target), target
    assert not _streets_match("Липа", "Filipa Višnjića")
    assert not _streets_match("Сава", "Savska")
    # the other false pairs the old rule held over real subscriber streets (09.10.2026)
    assert not _streets_match("БАРА", "Ive Lole Ribara")
    assert not _streets_match("МИРА", "Svetomira Đukića")
    assert not _streets_match("НОВА", "Hadži-prodanova")
    assert not _streets_match("БОКОКОТОРСКА", "Kotorska")
    assert not _streets_match("УЛИЦА 1", "1300 Kaplara")
    # and one real pair it missed: a hyphen is a word break
    assert _streets_match("ХАЏИ ПРОДАНОВА", "Hadži-prodanova")
    # whole words still match, either way round
    assert _streets_match("Нова", "Nova")
    assert _streets_match("Oslobođenja", "Bulevar oslobođenja")
    assert _streets_match("Др. Душана Поповића", "Dušana Popovića")
    assert _streets_match("Т.Рајића", "Тозе Рајића")
    assert _streets_match("Kralja Petra I", "Краља Петра Првог")


def test_an_abbreviated_street_must_fit_its_initials_and_name_the_street():
    """Every pair below was matched by the rules before 09.10.2026, over real subscriber streets
    and real announced ones."""
    from core.matchmaker import _streets_match
    # the initial is part of the name
    assert not _streets_match("Ј.КОЧИЋА", "Petra Kočića")
    assert not _streets_match("Р.ПАВЛОВИЋА", "Teodora Pavlovića")
    assert not _streets_match("С.БАЈИЋА", "Branka Bajića")
    for other in ("Б. ЈОВАНОВИЋА", "Ж. ЈОВАНОВИЋА", "С.ЈОВАНОВИЋА"):
        assert not _streets_match(other, "Zdravka Jovanovića"), other
    assert not _streets_match("Ж. ЈОВАНОВИЋА", "Б. ЈОВАНОВИЋА")
    # a lone surname is not a full name
    assert not _streets_match("ЈОВАНОВИЋА", "Zdravka Jovanovića")
    assert not _streets_match("ЈАНКОВИЋА", "Učitelja Miloša Jankovića")
    # numbers alone name no street
    assert not _streets_match("1.", "1. Novembar")
    assert not _streets_match("УЛИЦА 1", "1. Novembar")
    assert not _streets_match("ПРВА", "1. Novembar")
    assert not _streets_match("1.", "Bulevar Kralja Petra I")
    assert not _streets_match("ПРВА", "Bulevar Kralja Petra I")
    # what the abbreviations are for still works
    for short, full in (("Б.БУХЕ", "Boška Buhe"), ("Б. БУХЕ", "Boška Buhe"),
                        ("И.Л.РИБАРА", "Ive Lole Ribara"), ("И. Л. РИБАРА", "Ive Lole Ribara"),
                        ("ЛОЛЕ РИБАРА", "Ive Lole Ribara"), ("Ј.ЧМЕЛИКА", "Janka Čmelika"),
                        ("П.КОЧИЋА", "Petra Kočića"), ("М.ПУПИНА", "Bulevar Mihajla Pupina"),
                        ("МИХАЈЛА ПУПИНА", "Bulevar Mihajla Pupina"), ("ОСЛОБОЂЕЊА", "Bulevar Oslobođenja"),
                        ("ЦАРА ЛАЗАРА", "Bulevar Cara Lazara"), ("ВОЈВОДЕ СТЕПЕ СТЕПАНОВИЋА", "Vojvode Stepe"),
                        ("К.ПЕТРА ПРВОГ", "Bulevar Kralja Petra I"), ("КРАЉА ПЕТРА", "Bulevar Kralja Petra I"),
                        ("ПАРТИЗАНСКЕ", "Partizanska"), ("Т.Рајића", "Тозе Рајића")):
        assert _streets_match(short, full), (short, full)


def test_a_street_typed_without_diacritics_matches_the_whole_name_only():
    """01.11.2026: an address in Selevac was typed "KNEZA MILOSA" and a notice for "КНЕЗА
    МИЛОША" could never reach it. Dropping č/ć/ž/š/đ is allowed only when one side has none at
    all, and only for the whole name."""
    from core.matchmaker import _streets_match
    assert _streets_match("КНЕЗА МИЛОША", "KNEZA MILOSA")
    assert _streets_match("Kneza Milosa", "Кнеза Милоша")
    assert _streets_match("ЂУРЕ ЈАКШИЋА", "Djure Jaksica")
    assert _streets_match("ЦАРА ДУШАНА", "cara dusana")
    assert _streets_match("Краља Петра Првог", "Kralja Petra I")
    # whole name only: no abbreviations, no lone surnames, no part of a word
    assert not _streets_match("К. МИЛОША", "Kneza Milosa")
    assert not _streets_match("МИЛОША", "Kneza Milosa")
    assert not _streets_match("ЈОВАНОВИЋА", "Zdravka Jovanovica")
    assert not _streets_match("НОВА", "Presernova")
    assert not _streets_match("ТРГ", "Trg Slobode")
    # someone who types diacritics is taken at their word
    assert not _streets_match("Čičina", "Ćićina")


def test_a_house_range_may_carry_letters_at_either_end():
    from core.filters import is_house_number_affected as hit
    assert hit("46", explicit_numbers="2-6, 40Б-52А, 66-86")
    assert not hit("45", explicit_numbers="2-6, 40Б-52А, 66-86")  # 40-52 is the even side
    assert hit("1", explicit_numbers="1А-3, 7") and hit("3", explicit_numbers="1А-3, 7")
    assert not hit("5", explicit_numbers="1А-3, 7")
    assert hit("10", explicit_numbers="0ББ-20,24-34,38-54Г,72")
    assert hit("50", explicit_numbers="0ББ-20,24-34,38-54Г,72")
    assert not hit("60", explicit_numbers="0ББ-20,24-34,38-54Г,72")
    assert hit("36", explicit_numbers="36-36А") and not hit("37", explicit_numbers="36-36А")
    assert hit("47", number_range="45А-49А", parity="NEPARNA")
    assert not hit("48", number_range="45А-49А", parity="NEPARNA")


def test_a_range_with_two_even_or_two_odd_ends_is_one_side():
    """EPS writes each side of a street as its own range: "Руменачка 30 – 54, 31 – 41" is 30, 32
    ... 54 and 31, 33 ... 41. Rumenačka 53 was told of an outage it never had (#3929, 07.10.2026)."""
    from core.filters import is_house_number_affected as hit
    assert not hit("53", number_range="30-54") and not hit("53", number_range="31-41")
    assert hit("52", number_range="30-54") and hit("33", number_range="31-41")
    assert not hit("51", explicit_numbers="26, 30-54, 58-60, 25, 29, 39А-43, 73-73А, 87А-95")
    assert hit("41", explicit_numbers="26, 30-54, 58-60, 25, 29, 39А-43, 73-73А, 87А-95")
    assert hit("73", explicit_numbers="73-73А")
    # a range with one even and one odd end covers both sides
    assert hit("7", number_range="1-20") and hit("8", number_range="1-20")
    # the side the announcement names wins over the range ends
    assert hit("5", number_range="2-10", parity="NEPARNA")
    # what can't be read is still affected
    assert hit("53", number_range="od 30 do kraja")
