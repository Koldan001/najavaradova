"""nitoplana_scraper.py: date extraction and HEATING/WORKS classification, using real body text
captured live from https://nitoplana.rs/news/220 (2026-09-11).
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scrapers.nitoplana_scraper import _extract_date, _is_supply_cut

# Real captured body text (div.body, malformed nested <p> tags flattened by .get_text()).
REAL_INSTALLATION_NOTICE = (
    "ПОШТОВАНИ КОРИСНИЦИ Обавештавамо Вас да ће ЈКП „Градска топлана“ Ниш од 20.08.2026.год. "
    "почети са допунама грејних инсталација у циљу припреме система за грејну сезону. Уколико "
    "приметите било каква цурења на инсталацијама, посебно корисници код којих је извршена "
    "замена радијатора, молимо да нас обавестите. Ваша ЈКП „Градска топлана“"
)


def test_extract_date_reads_the_real_numeric_date_in_prose():
    assert _extract_date(REAL_INSTALLATION_NOTICE) == "20.08.2026"


def test_extract_date_none_when_no_date_present():
    assert _extract_date("Нема датума овде.") is None


def test_extract_date_rejects_impossible_date():
    assert _extract_date("Датум 32.13.2026. је нетачан.") is None


def test_real_installation_supplement_notice_is_not_a_supply_cut():
    # "допунама грејних инсталација" (supplements to heating installations) - real works, no
    # stated cut. Confirmed against the actual live page's own framing (WebFetch summary called
    # this "Heating Installation Supplements", not an outage).
    assert _is_supply_cut(REAL_INSTALLATION_NOTICE) is False


def test_stated_heating_cut_is_a_supply_cut():
    text = "Због радова без грејања ће бити корисници у улици Војводе Путника."
    assert _is_supply_cut(text) is True
