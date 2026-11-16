"""ns_toplana_scraper: the outage date must come from the article prose, never the post's
publish/modified <time> stamp. nstoplana slugs carry no date (prekid/radova/poremecaj only),
and the site re-touches old posts - a run that trusted the <time> stamp re-ingested an August
outage as a fresh row dated "today" and mailed live users (outage #1063). The prose almost
always spells the month out ("26. августа 2026"), with a bare numeric date the exception.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scrapers.ns_toplana_scraper import _extract_date, _is_supply_cut


def test_numeric_date_in_prose():
    assert _extract_date("...дана 10.09.2026. године од 8 часова...") == "10.09.2026"


def test_serbian_word_month_cyrillic():
    assert _extract_date("У среду, 26 . августа 2026 . године, због радова") == "26.08.2026"


def test_serbian_word_month_fleeting_a_september():
    # септембар -> септембра in the genitive; the stem match must still resolve month 9.
    assert _extract_date("У среду, 9 . септембра 2026 . године , од 8 сати") == "09.09.2026"


def test_serbian_word_month_latin():
    assert _extract_date("u sredu, 26. avgusta 2026. godine, zbog radova") == "26.08.2026"


def test_tolerates_nstoplana_stray_spaces_in_the_year():
    # Real body text from outage #1063: "26 . августа 20 26 . године" and "202 6" both occur.
    assert _extract_date("истог дана, 26 . августа 20 26 . године, у вечерњим") == "26.08.2026"
    assert _extract_date("у среду, 9 . септембра 202 6 . године , од 8 сати") == "09.09.2026"


def test_none_when_no_date_anywhere():
    assert _extract_date("nema datuma nigde u ovom telu") is None


def test_impossible_numeric_date_is_rejected_not_returned():
    # 32.13.2026 is not a real date and there is no word-month to fall back on.
    assert _extract_date("stray 32.13.2026 number, ništa drugo") is None


def test_no_time_tag_fallback_argument():
    # _extract_date takes raw_text only now - the publish-time fallback (and its soup arg) is
    # gone on purpose. A dateless body yields None regardless of what the page's <time> says.
    assert _extract_date("obaveštenje bez ijednog datuma") is None


# A "почетак радова" post with no supply-cut phrasing must be category WORKS (grey, no notify);
# one that also announces an interruption stays HEATING.

def test_supply_cut_false_for_pure_works_notice():
    assert _is_supply_cut("Обавештавамо потрошаче о почетку радова на реконструкцији вреловода.") is False


def test_supply_cut_false_for_road_closure_wording():
    # Real nstoplana post (outage-on-map #Gundulićeva 29): works start + traffic suspended,
    # no hot water cut. "обуставља се саобраћај" must NOT read as a supply interruption.
    txt = ("почиње радове на изградњи вреловодног прикључка у Гундулићевој улици за објекат "
           "број 29. Због поменутих радова одређује се привремена измена режима саобраћаја. "
           "За време извођења радова потпуно се обуставља саобраћај у Гундулићевој улици "
           "испред броја 29. Гундулићева улица постаје слепа.")
    assert _is_supply_cut(txt) is False


def test_supply_cut_true_when_interruption_is_announced():
    assert _is_supply_cut("Због радова ће доћи до прекида испоруке топле воде.") is True
    assert _is_supply_cut("Потрошачи ће остати без грејања до 15 часова.") is True
    assert _is_supply_cut("Долази до обуставе испоруке топлотне енергије.") is True
    assert _is_supply_cut("Због квара, поремећај у испоруци топле воде.") is True
