"""NS_water_scraper: the outage date comes from the slug, not the body.

Regression: vikns.rs announcement bodies almost never contain a date ("...без воде ће од 8 до
16 сати бити улице ..."); the date is only in the slug/title. Feeding body-only to the parser
made Gemini invent a date (outage #1007: a 10.9.2026 notice parsed as 2024-06-05, then dropped
off the map as a 15-month-past outage).
"""
import os
import sys
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scrapers.NS_water_scraper import (
    _slug_date, _slug_is_stale, _looks_like_outage_para,
)


def test_slug_date_extracts_from_dated_slug():
    u = "https://www.vikns.rs/najava-radova-sr/na%d1%98ava-radova-za-cetvrtak-10-9-2026/"
    assert _slug_date(u) == datetime(2026, 9, 10)
    assert _slug_date(u).strftime("%d.%m.%Y") == "10.09.2026"


def test_slug_date_none_when_no_date_in_slug():
    assert _slug_date("https://www.vikns.rs/najava-radova-sr/12345/") is None
    assert _slug_date("https://www.vikns.rs/najava-radova-sr/some-permalink/") is None


def test_slug_date_rejects_impossible_date():
    assert _slug_date("https://www.vikns.rs/najava-radova-sr/x-32-13-2026/") is None


def test_multi_item_split_is_content_based_not_slug_based():
    # Real case: outage #1153 came from a plain "najava-radova-za-cetvrtak-10-9-2026" URL, not a
    # "servisne-informacije" digest, yet carried two far-apart water cuts. The split decision is
    # >=2 outage paragraphs, regardless of the slug.
    paras = [
        "Zbog radova na sanaciji havarije bez vode će do 15 sati biti Ulica Biće Kesića i "
        "okolne ulice u Stepanovićevu.",
        "Zbog planiranih radova na vodovodnoj mreži bez vode će od 8 do 16 sati biti ulice "
        "Vladimira Nazora i Novosadska u Sremskoj Kamenici.",
        "Postoji mogućnost da će u toku radova, ukoliko za tim bude potrebe, blokada vodovodne "
        "mreže biti produžena i proširena.",
    ]
    items = [p for p in paras if _looks_like_outage_para(p)]
    assert len(items) == 2   # the two cuts split; the "Postoji mogućnost" caveat is dropped


def test_single_outage_with_boilerplate_is_not_split():
    paras = [
        "Zbog planiranih radova bez vode će od 8 do 16 sati biti ulice Vladimira Nazora i "
        "Novosadska u Sremskoj Kamenici.",
        "Postoji mogućnost da će blokada vodovodne mreže biti produžena i proširena.",
    ]
    items = [p for p in paras if _looks_like_outage_para(p)]
    assert len(items) == 1   # one real item -> falls through to the single-announcement path


def test_looks_like_outage_para_keeps_real_items_drops_the_caveat():
    item = ("Због радова на санацији хаварије без воде ће до 13 сати бити "
            "Балзакова улица (од Булевара цара Лазара до Народног фронта).")
    caveat = ("Постоји могућност да ће у току радова, уколико за тим буде потребе, "
              "блокада водоводне мреже бити продужена и проширена.")
    assert _looks_like_outage_para(item) is True
    assert _looks_like_outage_para(caveat) is False
    assert _looks_like_outage_para("Кратко.") is False


def test_looks_like_outage_para_also_matches_latin_body():
    lat = ("Zbog radova na sanaciji havarije bez vode će do 15 sati biti Ulica Biće Kesića "
           "i okolne ulice u Stepanovićevu.")
    lat_caveat = ("Postoji mogućnost da će u toku radova, ukoliko za tim bude potrebe, "
                  "blokada vodovodne mreže biti produžena i proširena.")
    assert _looks_like_outage_para(lat) is True
    assert _looks_like_outage_para(lat_caveat) is False


def test_slug_is_stale_only_for_old_dated_slugs():
    old = "https://www.vikns.rs/najava-radova-sr/na%d1%98ava-radova-za-1-1-2020/"
    future = "https://www.vikns.rs/najava-radova-sr/na%d1%98ava-radova-za-1-1-2099/"
    undated = "https://www.vikns.rs/najava-radova-sr/12345/"
    assert _slug_is_stale(old) is True
    assert _slug_is_stale(future) is False
    assert _slug_is_stale(undated) is False   # no date -> falls through to fetch+hash dedup
