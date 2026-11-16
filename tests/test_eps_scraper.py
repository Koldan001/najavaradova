"""scrapers/eps_scraper.py: both EPS table layouts parse, and a row lands in the opština it is
really about."""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scrapers.eps_scraper import (_belgrade_opstina, _extract_page_date, _inline_opstina,
                                  _long_over, extract_table_rows)
from spatial.spatial_engine import RGZ_DB_PATH

requires_rgz = pytest.mark.skipif(not os.path.exists(RGZ_DB_PATH), reason="data/rgz.db not present")


def _page(title, header, rows):
    cells = lambda values: "".join(f"<td><font>{v}</font></td>" for v in values)
    body = "".join(f"<tr>{cells(r)}</tr>" for r in rows)
    return (f"<TABLE><TR><TD><B>{title}</B></TD></TR></TABLE>"
            f"<TABLE><tr>{cells(header)}</tr>{body}</TABLE>")


REGIONAL = _page(
    "НОВИ САД - Планирана искључења за датум: 24.09.2026.",
    ["Огранак", "Општина", "Време", "Улице"],
    [["Нови Сад", "Бачки Петровац", "08:30 - 13:00", "Улица Михала Пагача"],
     ["Нови Сад", "Нови Сад", "08:00 - 13:45", "Ченеј-\" Реље Савића \""]],
)

# The Belgrade pages have no Огранак column. The old "at least 4 columns" rule dropped every row.
BELGRADE = _page(
    "БЕОГРАД - Планирана искључења за датум: 2026-09-25",
    ["Општина", "Време", "Улице"],
    [["Звездара", "08:30 - 14:00", "Насеље М.МОКРИ ЛУГ: НАРОДНОГ ФРОНТА: 69А,"],
     ["Вождовац", "09:00 - 14:00", "РАДА НЕИМАРА: 21,"],
     ["Палилула", "09:00 - 12:00", ""]],   # no streets - skipped
)


def test_regional_four_column_layout():
    rows = extract_table_rows(REGIONAL)
    assert [r["opstina"] for r in rows] == ["Бачки Петровац", "Нови Сад"]
    assert rows[0] == {"ogranak": "Нови Сад", "opstina": "Бачки Петровац",
                       "vreme": "08:30 - 13:00", "ulice": "Улица Михала Пагача"}


def test_belgrade_three_column_layout():
    rows = extract_table_rows(BELGRADE)
    assert [(r["opstina"], r["vreme"]) for r in rows] == [
        ("Звездара", "08:30 - 14:00"), ("Вождовац", "09:00 - 14:00")]
    assert rows[1]["ulice"] == "РАДА НЕИМАРА: 21," and rows[1]["ogranak"] == ""


def test_page_date_both_formats():
    assert _extract_page_date(REGIONAL) == "24.09.2026"
    assert _extract_page_date(BELGRADE) == "25.09.2026"


@requires_rgz
def test_belgrade_palilula_is_pinned_to_belgrade():
    assert _belgrade_opstina("Палилула") == "Палилула - Београд"
    assert _belgrade_opstina("Звездара") == "Звездара"


def test_regions_take_turns():
    """Region by region, one busy branch used up every cycle's time budget (24 Sep 2026: Novi Sad
    ~44 new rows, Belgrade never reached). Within a day the regions now alternate."""
    from scrapers.eps_scraper import _interleave
    assert _interleave([["ns1", "ns2", "ns3"], ["bg1"], ["ni1", "ni2"]]) == \
        ["ns1", "bg1", "ni1", "ns2", "ni2", "ns3"]
    assert _interleave([]) == []


def test_rows_long_over_skip_gemini():
    """A row whose window plus the map's 3 h grace is behind us would never show anywhere."""
    from datetime import datetime
    now = datetime(2026, 9, 24, 17, 30)
    assert _long_over("24.09.2026", "08:00 - 13:00", now)        # 13:00 + 3 h < 17:30
    assert not _long_over("24.09.2026", "09:00 - 15:00", now)    # still on the map until 18:00
    assert not _long_over("25.09.2026", "08:00 - 13:00", now)    # tomorrow


def test_row_naming_another_opstina():
    assert _inline_opstina("Општина Горњи Милановац: насеље Бољковци (засеок Рањци)") == "Горњи Милановац"
    assert _inline_opstina("Општина Љиг: насеље Бољковци") == "Љиг"
    # The branch's own heading style and ordinary street text are not an opština override.
    assert _inline_opstina("Опово: цело насељено место осим следећих улица") is None
    assert _inline_opstina("Насеље БОРЧА: ЗРЕЊАНИНСКИ ПУТ: 250,") is None
    assert _inline_opstina("Општина Непостојећа: нешто") is None
