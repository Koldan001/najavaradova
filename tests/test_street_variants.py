"""A street written another way ("Хероја Пинкија" for Bukovac's "Пинкијева", EPS Sep 2026) is read
as that street only by exact RGZ name, in the same settlement, right next to the announcement's
other streets - never as a loose guess."""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from spatial.spatial_engine import RGZ_DB_PATH, query_rgz_streets, street_name_variants

requires_rgz = pytest.mark.skipif(not os.path.exists(RGZ_DB_PATH), reason="data/rgz.db not present")


def test_variants():
    assert street_name_variants("ХЕРОЈА ПИНКИЈА") == ["PINKIJEVA"]
    assert street_name_variants("Народног хероја Пинкија") == ["PINKIJEVA"]
    assert street_name_variants("КРАЉА МАРКА") == ["MARKOVA"]
    # Nothing to re-spell: no title word, or a full name left over, or already possessive.
    assert street_name_variants("ПИНКИЈЕВА") == []
    assert street_name_variants("ЈОВАНА ЦВИЈИЋА") == []
    assert street_name_variants("КРАЉА ПЕТРА ПРВОГ") == []


@requires_rgz
def test_exact_only_never_widens():
    # The loose passes would turn "MARKOVA" into a LIKE on "MARKOV%" - Svetozara Markovića.
    assert query_rgz_streets("MARKOVA", naselje="БУКОВАЦ", opstina="НОВИ САД", exact_only=True) == []
    assert query_rgz_streets("PINKIJEVA", naselje="БУКОВАЦ", opstina="НОВИ САД", exact_only=True)


def _bukovac_alert(*streets):
    from parser import OutageAlert, StreetSegment
    return OutageAlert(
        city="НОВИ САД", outage_date="25.09.2026", message_template="x",
        affected_locations=[
            StreetSegment(location_type="STREET", main_street=street, settlement=settlement,
                          includes_surrounding_streets=False, time_window="08:00 - 14:00")
            for street, settlement in streets])


@requires_rgz
def test_misnamed_street_next_to_the_others_is_read_as_the_real_one():
    from core import matchmaker as mm
    alert = _bukovac_alert(("КЕСТЕЊАРА", "БУКОВАЦ"), ("КОСОВСКА", "БУКОВАЦ"),
                           ("ХЕРОЈА ПИНКИЈА", "БУКОВАЦ"))
    mm._name_corrections.clear()
    hexes, quality = mm.calculate_outage_hexes(alert)

    pinki = alert.affected_locations[2]
    assert pinki.main_street == "PINKIJEVA"           # users on Pinkijeva now match by name
    assert quality == "FALLBACK"                       # a corrected name is flagged, never EXACT
    assert mm._name_corrections and "PINKIJEVA" in mm._name_corrections[0]
    real = query_rgz_streets("PINKIJEVA", naselje="БУКОВАЦ", opstina="НОВИ САД", exact_only=True)
    from spatial.spatial_engine import h3
    assert {h3.latlng_to_cell(pt[1], pt[0], 9) for s in real for pt in s} <= set(hexes)


@requires_rgz
def test_misnamed_street_alone_is_not_guessed():
    from core import matchmaker as mm
    alert = _bukovac_alert(("ХЕРОЈА ПИНКИЈА", "БУКОВАЦ"))
    _, quality = mm.calculate_outage_hexes(alert)
    assert alert.affected_locations[0].main_street == "ХЕРОЈА ПИНКИЈА"
    assert quality != "EXACT"


# The owner's three rules (2026-09-24), for "Хероја Пинкија" among Bukovac streets:
#   1. the text puts it under Bukovac  -> the similar street next to the others (Пинкијева);
#   2. it is tagged Novi Sad           -> exactly Novi Sad's Хероја Пинкија, no correction;
#   3. the text gives it no place      -> the street exactly as written, in the opština.

def _pinki_with(pinki_settlement):
    return _bukovac_alert(("КЕСТЕЊАРА", "БУКОВАЦ"), ("КОСОВСКА", "БУКОВАЦ"),
                          ("ХЕРОЈА ПИНКИЈА", pinki_settlement))


def _novi_sad_heroja_pinkija_hexes():
    from spatial.spatial_engine import h3
    segs = query_rgz_streets("ХЕРОЈА ПИНКИЈА", naselje="НОВИ САД", opstina="НОВИ САД", exact_only=True)
    return {h3.latlng_to_cell(pt[1], pt[0], 9) for s in segs for pt in s}


@requires_rgz
def test_rule1_text_heading_puts_it_under_bukovac():
    from core import matchmaker as mm
    alert = _pinki_with(None)   # the model dropped the place; the text's heading still has it
    mm.calculate_outage_hexes(alert, "Буковац: Кестењара, Косовска, Хероја Пинкија")
    assert alert.affected_locations[2].main_street == "PINKIJEVA"


@requires_rgz
def test_rule2_tagged_novi_sad_is_exactly_that_street():
    from core import matchmaker as mm
    alert = _pinki_with("НОВИ САД")
    hexes, _ = mm.calculate_outage_hexes(alert, "Буковац: Кестењара, Косовска; Нови Сад: Хероја Пинкија")
    assert alert.affected_locations[2].main_street == "ХЕРОЈА ПИНКИЈА"
    assert _novi_sad_heroja_pinkija_hexes() <= set(hexes)


@requires_rgz
def test_rule3_no_place_in_the_text_is_the_street_as_written():
    from core import matchmaker as mm
    alert = _pinki_with(None)
    # Bukovac is only named AFTER the street, so it is not the street's heading.
    hexes, _ = mm.calculate_outage_hexes(alert, "Хероја Пинкија. Буковац: Кестењара, Косовска")
    pinki = alert.affected_locations[2]
    assert pinki.main_street == "ХЕРОЈА ПИНКИЈА" and not pinki.settlement
    assert _novi_sad_heroja_pinkija_hexes() <= set(hexes)


def test_place_named_before():
    from core.matchmaker import _place_named_before
    assert _place_named_before("Буковац: Кестењара, Хероја Пинкија", "БУКОВАЦ", "ХЕРОЈА ПИНКИЈА")
    assert not _place_named_before("Хероја Пинкија. Буковац: Кестењара", "БУКОВАЦ", "ХЕРОЈА ПИНКИЈА")
    assert not _place_named_before("Кестењара, Хероја Пинкија", "БУКОВАЦ", "ХЕРОЈА ПИНКИЈА")
    # abbreviated heading vs the model's full name, and a case ending on the place
    assert _place_named_before("Насеље М.МОКРИ ЛУГ: НАРОДНОГ ФРОНТА: 69А", "Мали Мокри Луг", "НАРОДНОГ ФРОНТА")
    assert _place_named_before("Делови насеља Рибњак; Делови улица: Цара Лазара", "РИБЊАК", "ЦАРА ЛАЗАРА")


@requires_rgz
def test_misnamed_street_far_from_the_others_is_not_guessed():
    from core import matchmaker as mm
    # The only resolved street is in central Novi Sad, ~6 km from Bukovac's Pinkijeva.
    alert = _bukovac_alert(("БУЛЕВАР ОСЛОБОЂЕЊА", "НОВИ САД"), ("ХЕРОЈА ПИНКИЈА", "БУКОВАЦ"))
    _, quality = mm.calculate_outage_hexes(alert)
    assert alert.affected_locations[1].main_street == "ХЕРОЈА ПИНКИЈА"
    assert quality == "UNRESOLVED"
