"""scrapers._common.use_scraper_date: the page's date, unless the notice names a later day of its own.

Real case, 30.10.2026: Niš posts "обавештење за дан 18.09." about works "у суботу 19. септембра",
and the scraper set the post's 18.09. over the model's 19.09. (#1979, 31 outages in 45 days)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from parser import OutageAlert
from scrapers._common import use_scraper_date

NIS = ("Ниш, обавештење за дан 18.09.2026.\nПЛАНИРАНИ РАДОВИ У СЕЛУ ПОПОВАЦ У суботу 19. септембра "
       "планира се ископ у Јабланичкој улици код броја 43.")


def _alert(outage_date, end_date=None):
    return OutageAlert(city="НИШ", outage_date=outage_date, end_date=end_date, affected_locations=[])


@pytest.mark.parametrize("model_date, page_date, text, want", [
    ("19.09.2026", "18.09.2026", NIS, "19.09.2026"),            # the notice's own later day
    ("18.09.2026", "18.09.2026", NIS, "18.09.2026"),
    ("20.09.2026", "18.09.2026", NIS, "18.09.2026"),            # a day the notice never states
    ("17.09.2026", "18.09.2026", "Радови 17.09. и 18.09.", "18.09.2026"),   # earlier: the page wins
    ("30.10.2026", "18.09.2026", "Радови 30.10.2026.", "18.09.2026"),       # too far ahead
    ("N/A", "18.09.2026", NIS, "18.09.2026"),
    ("2026-09-19", "18.09.2026", NIS, "19.09.2026"),            # the model's own format, normalised
])
def test_use_scraper_date(model_date, page_date, text, want):
    alert = _alert(model_date)
    use_scraper_date(alert, page_date, text)
    assert alert.outage_date == want


def test_the_notices_end_date_is_not_taken_as_its_day():
    alert = _alert("25.09.2026", end_date="25.09.2026")
    use_scraper_date(alert, "18.09.2026", "Радови од 18.09. до 25.09.2026.")
    assert alert.outage_date == "18.09.2026"


def test_no_page_date_leaves_the_models():
    alert = _alert("19.09.2026")
    use_scraper_date(alert, None, NIS)
    assert alert.outage_date == "19.09.2026"
