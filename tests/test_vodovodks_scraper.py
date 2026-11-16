"""vodovodks_scraper.py: a real ЈКП "Водовод-Крушевац" info-servis doc can pack several unrelated
street-level cuts into one day's digest, same shape as vikns.rs. Split by paragraph, not by doc.

Fixtures below are real content field text captured live from
https://www.vodovodks.co.rs/api/info-servis (2026-09-11).
"""
import os
import sys
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scrapers.vodovodks_scraper import _looks_like_outage_para, _doc_date


def test_multi_cut_digest_splits_into_only_the_real_cuts():
    # Real doc id 275 (2026-09-11): 6 paragraphs, but only 3 are actual water cuts - a plain
    # reconstruction notice, a "reduced pressure" caveat, and a traffic-closure side-note all
    # describe the same underlying works without stating a cut, and must NOT count as items.
    paragraphs = [
        "Због радова на водоводној мрежи дана 11.09.2026.године од 09.30 до 15.00 часова без "
        "воде ће бити Крушевац - Мајке Југовића (од Косовске до Патријарха Чарнојевића).",
        "Због санације квара на водоводној мрежи дана 11.09.2026.године од 08.00 до 15.00 часова "
        "без воде ће бити Крушевац - Сликара Миловановића; Велики Купци - Џеверски сокак; Мало "
        "Крушинце.",
        "Због израде водоводних прикључака у петак 11.09.2026.године од 08.00 до 15.00 часова "
        "без воде ће бити Крушевац - Видовданска (од Душанове до паруновачког моста) и пратеће "
        "улице; Крушевац - Булевар Николе Пашића (од Александра Флеминга до Војводе Степе); "
        "Дедина - горња зона водоснабдевања.",
        "Дана 11.09.2026. године изводиће се радови на реконструкцији водоводне мреже у улици "
        "др Батута у Крушевцу.",
        "Због извођења радова на изради водоводног прикључка за Дом бораца у улици Мајке "
        "Југовића дана 11.09.2026. године може доћи до смањеног притиска воде у мрежи у ужем "
        "центру града.",
        "Због извођења радова на водоводној мрежи у делу улице Мајке Југовића у Крушевцу, од "
        "10.09. до 12.09.2026. године (од четвртка до суботе) за саобраћај ће бити затворена "
        "деоница од Косовске улице до улице Патријарха Чарнојевића.",
    ]
    items = [p for p in paragraphs if _looks_like_outage_para(p)]
    assert len(items) == 3
    assert "Мајке Југовића" in items[0]
    assert "Сликара Миловановића" in items[1]
    assert "Видовданска" in items[2]


def test_single_cut_with_a_related_traffic_note_is_not_split():
    # Real doc id 98 (2026-02-10): a water-cut paragraph plus an EMPTY <p></p> (a formatting
    # artifact from the CMS, not a paragraph at all once BeautifulSoup strips it) plus a
    # traffic-ban paragraph about the same burst-pipe repair - neither of the other two names a
    # water cut, so this must fall through to the single-announcement path, not split into 2.
    paragraphs = [
        "Због санације квара на водоводној мрежи дана 10.02.2026.године од 08.00 до 15.00 часова "
        "без воде ће бити Багдала 3 - Устаничка; Дедина - Светолика Ранковића (од Циглане до "
        "краја улице), део Савске улице; Церова - пут за цркву Света Петке; Друго Макрешане "
        "(после гробља).",
        "Услед извођења радова на санацији квара на водоводној мрежи дана 10.02.2026.године "
        "одређује се привремена забрана саобраћаја у Устаничкој улици на Багдали 3, у времену "
        "од 08.00 до 15.00 часова.",
    ]
    items = [p for p in paragraphs if _looks_like_outage_para(p)]
    assert len(items) == 1


def test_looks_like_outage_para_also_matches_latin_body():
    lat = ("Zbog sanacije kvara na vodovodnoj mrezi dana 11.09.2026.godine od 08.00 do 15.00 "
           "casova bez vode ce biti Krusevac - Zeleznicka.")
    assert _looks_like_outage_para(lat) is True


def test_doc_date_parses_iso_date_field():
    assert _doc_date({"date": "2026-09-11"}) == datetime(2026, 9, 11)


def test_doc_date_none_for_missing_or_malformed():
    assert _doc_date({}) is None
    assert _doc_date({"date": ""}) is None
    assert _doc_date({"date": "11.09.2026"}) is None
