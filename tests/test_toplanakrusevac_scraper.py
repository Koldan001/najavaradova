"""toplanakrusevac_scraper.py: distinguishing a real HEATING supply-cut from a WORKS notice from
routine PR filler, using real post titles/bodies captured live from
toplanakrusevac.rs/wp-json/wp/v2/posts (2026-09-11).
"""
import os
import sys
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scrapers.toplanakrusevac_scraper import _looks_technical, _is_supply_cut, _post_date


def test_stated_cut_with_a_named_area_is_technical_and_a_cut():
    # Real title, 17.3.2026 post.
    text = "17.3.2026. – Део Душанове и ул. Бруски пут без грејања"
    assert _looks_technical(text) is True
    assert _is_supply_cut(text) is True


def test_works_in_progress_with_no_stated_cut_is_technical_but_not_a_cut():
    # Real body text, radovi-na-toplovodnom-sistemu-u-balkanskoj-ulici (21.07.2026).
    text = ('ЈКП „Градска топлана“ Крушевац обавештава грађане да су у току радови на '
            'унапређењу топловодног система у Балканској улици.')
    assert _looks_technical(text) is True
    assert _is_supply_cut(text) is False


def test_routine_no_problems_status_post_is_not_technical():
    # Real title style: near-daily "everything's fine" status posts that must never reach the
    # parser at all.
    text = "20.3.2026. – Испорука топлотне енергије одвија се без проблема"
    assert _looks_technical(text) is False


def test_generic_pr_content_is_not_technical():
    for text in (
        "Свечана академија поводом 27. годишњице од бомбардовања Градске топлане",
        "Радно време током празничних дана",
        "За крушевачке медије, директор Тасић",
    ):
        assert _looks_technical(text) is False, text


def test_ambiguous_supply_status_wording_passes_prefilter_but_is_not_a_cut():
    # "Снабдевање" (supply) legitimately contains the same substring as the real _SUPPLY_OBJECTS
    # marker "снабдевањ" - this deliberately costs a wasted parse call rather than risk silently
    # dropping a real one, but must never be misclassified as an actual cut.
    text = "23.3.2026. – Снабдевање енергијом у складу са планираном динамиком"
    assert _looks_technical(text) is True
    assert _is_supply_cut(text) is False


def test_post_date_parses_the_datetime_prefix():
    assert _post_date({"date": "2026-08-31T09:05:36"}) == datetime(2026, 8, 31)


def test_post_date_none_for_missing_or_malformed():
    assert _post_date({}) is None
    assert _post_date({"date": ""}) is None
