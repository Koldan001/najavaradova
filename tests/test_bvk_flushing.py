"""BVK's multi-night flushing notices (#1984, "21.-26.09.2026. Редовно испирање водоводне мреже на
општини Савски Венац"): one outage per night on that night's date, each "блок оивичен улицама"
street counted only along the block's edge, and a card that says the water comes and goes rather
than "Bez vode". Network, Gemini and geometry are stubbed."""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import scrapers.bvk_scraper as bvk
from parser import OutageAlert, StreetSegment

INTRO = ("Екипе Београдског водовода и канализације изводиће радове на редовном испирању водоводне "
         "мреже у општини Савски венац, од понедељка, 21. септембра 2026. године, до суботе, 26. "
         "септембра 2026. године. Повремене прекиде у снабдевању и умањен притисак воде у времену од "
         "22.00 до 6.00 сати осетиће следећи потрошачи:")
NIGHTS = [
    "21/22.09.2026. године – блок оивичен улицама Бранков мост, Херцеговачком, Карађорђевом, "
    "Гаврила Принципа, Балканском, Адмирала Гепрата, Савском, Сарајевском (са припадајућом мрежом "
    "бочних улица),",
    "22/23.09.2026. године – блок оивичен улицама Кнеза Милоша, Немањином, Ресавском, Светозара "
    "Марковића, Масариковом, Краља Милана, Булеваром ослобођења и Аутопутом,",
    "23/24.09.2026. године – блок оивичен улицама Булевар кнеза Александра Карађорђевића, "
    "Незнаног јунака, Бањички венац, Љутице Богдана и Аутопутом,",
]
OUTRO = "У току радова може доћи и до краткотрајног замућења воде."
PAGE = f"""<html><body>
<section class="av_toggle_section"><p class="toggler"
  data-title="21.-26.09.2026. Редовно испирање водоводне мреже на општини Савски Венац">x</p>
<div class="toggle_content"><p>{INTRO}</p><ul>{''.join(f'<li>{n}</li>' for n in NIGHTS)}</ul>
<p>{OUTRO}</p></div></section>
<section class="av_toggle_section"><p class="toggler"
  data-title="22.09.2026. Планирани радови на водоводној мрежи у општини Раковица">x</p>
<div class="toggle_content"><p><strong>РАКОВИЦА</strong></p>
<p>Због радова у уторак, 22. септембра 2026. од 8.00 до 18.00 без воде ће остати улице:</p>
<ul><li>Хајдук Вељковој,</li><li>Стражевичкој,</li><li>Бродараца,</li></ul></div></section>
</body></html>"""


@pytest.mark.parametrize("line, day", [
    ("21/22.09.2026. године – блок", "21.09.2026"),
    ("22 / 23 . 09 . 2026. године", "22.09.2026"),
    ("30/01.10.2026. године", "30.09.2026"),        # the night crosses into October
    ("31/01.11.2026.", "31.10.2026"),
    ("31/01.01.2027.", "31.12.2026"),
    ("22.09.2026. – блок", "22.09.2026"),
    ("Хајдук Вељковој,", None),
    ("35/36.09.2026.", None),
])
def test_line_date(line, day):
    assert bvk._line_date(line) == day


def test_each_night_is_its_own_notice_with_the_opening_and_closing():
    nights = bvk.split_nights([INTRO] + NIGHTS + [OUTRO])
    assert [day for day, _ in nights] == ["21.09.2026", "22.09.2026", "23.09.2026"]
    assert nights[1][1] == [INTRO, NIGHTS[1], OUTRO]


def test_a_street_list_is_not_split():
    assert bvk.split_nights(["Без воде ће остати улице:", "Хајдук Вељковој,", "Стражевичкој,"]) is None
    assert bvk.split_nights([INTRO, NIGHTS[0], OUTRO]) is None      # one dated line: nothing to split


def _block_alert(*streets, settlement_loc=False):
    locs = [StreetSegment(location_type="STREET", main_street=s, time_window="22:00 - 06:00") for s in streets]
    if settlement_loc:
        locs.append(StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement="СЕЊАК", time_window="22:00 - 06:00"))
    return OutageAlert(city="БЕОГРАД", outage_date="21.09.2026", message_template="x", affected_locations=locs)


def test_block_streets_run_between_their_neighbours():
    alert = _block_alert("КНЕЗА МИЛОША", "НЕМАЊИНА", "РЕСАВСКА", "АУТОПУТ")
    bvk.bound_block_streets(alert, NIGHTS[1])
    assert [(l.intersection_start, l.main_street, l.intersection_end) for l in alert.affected_locations] == [
        ("АУТОПУТ", "КНЕЗА МИЛОША", "НЕМАЊИНА"),
        ("КНЕЗА МИЛОША", "НЕМАЊИНА", "РЕСАВСКА"),
        ("НЕМАЊИНА", "РЕСАВСКА", "АУТОПУТ"),
        ("РЕСАВСКА", "АУТОПУТ", "КНЕЗА МИЛОША"),
    ]


@pytest.mark.parametrize("alert, text", [
    (_block_alert("КНЕЗА МИЛОША", "НЕМАЊИНА", "РЕСАВСКА"), "без воде ће остати улице"),     # not a block
    (_block_alert("КНЕЗА МИЛОША", "НЕМАЊИНА", "РЕСАВСКА"), NIGHTS[0] + " " + NIGHTS[1]),     # two blocks
    (_block_alert("КНЕЗА МИЛОША", "НЕМАЊИНА", settlement_loc=True), NIGHTS[1]),              # names a place
    (_block_alert("КНЕЗА МИЛОША", "НЕМАЊИНА"), NIGHTS[1]),                                   # not a ring
])
def test_anything_but_one_ring_of_streets_is_left_alone(alert, text):
    bvk.bound_block_streets(alert, text)
    assert all(not l.intersection_start and not l.intersection_end for l in alert.affected_locations)


def test_the_scraper_saves_one_outage_per_night(monkeypatch):
    class Page:
        text = PAGE
    saved = []
    monkeypatch.setattr(bvk.requests, "get", lambda *a, **k: Page())
    monkeypatch.setattr(bvk, "is_hash_processed", lambda h: False)
    # The model reads the opening paragraph's whole run into every night.
    monkeypatch.setattr(bvk, "parse_announcement", lambda raw: OutageAlert(
        city="БЕОГРАД", outage_date="21.09.2026", end_date="26.09.2026", message_template="x",
        affected_locations=[StreetSegment(location_type="STREET", main_street=s, time_window="22:00 - 06:00")
                            for s in ("КНЕЗА МИЛОША", "НЕМАЊИНА", "РЕСАВСКА")]))
    monkeypatch.setattr(bvk, "process_and_queue_outage",
                        lambda parsed, raw, **kw: saved.append((parsed, raw)))
    bvk.run_scraper()

    flushing = [(p, raw) for p, raw in saved if "испирање" in raw]
    assert [(p.outage_date, p.end_date) for p, _ in flushing] == [
        ("21.09.2026", None), ("22.09.2026", None), ("23.09.2026", None)]
    for (p, raw), night in zip(flushing, NIGHTS):
        assert night in raw and sum(n in raw for n in NIGHTS) == 1
        assert p.affected_locations[0].intersection_start == "РЕСАВСКА"
    (rakovica, raw), = [(p, raw) for p, raw in saved if "испирање" not in raw]
    assert rakovica.end_date == "26.09.2026"               # an ordinary notice keeps the model's dates
    assert not rakovica.affected_locations[0].intersection_start


def test_a_flushing_card_says_the_water_comes_and_goes():
    from scripts.build_web_data import summarize
    raw = f"21.-26.09.2026. Редовно испирање водоводне мреже {INTRO} {NIGHTS[1]} {OUTRO}"
    assert summarize("VODA", "Prekid vode", raw) == (
        "Ispiranje vodovodne mreže.",
        ["Povremeni prekidi i slabiji pritisak vode.", "Moguće kratkotrajno zamućenje vode."])
    # A flushing notice that does cut the water says so, and the card keeps it.
    headline, effects = summarize("VODA", None, "Због испирања мреже без воде ће остати улице ...")
    assert effects[0] == "Bez vode u navedenom periodu."
