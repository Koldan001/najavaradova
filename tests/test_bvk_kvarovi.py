"""scrapers/bvk_kvarovi_scraper.py: each fault takes its restoration time from the "до HH:MM"
heading above it, whatever tag the heading is in and whichever alphabet its letters are typed in
(26.10.2026: "Дo 15:00" with a Latin o, in a <p> - missed, and the model guessed 07:00-19:00)."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scrapers import bvk_kvarovi_scraper as k

PAGE = """<section class="av_toggle_section"><p class="toggler" data-title="26.10.2026.">26.10.2026.</p>
<div class="toggle_content"><blockquote><p><strong>Дo 15:00</strong></p></blockquote>
<ul><li><strong>Стари град:</strong> Влајковићева 25</li></ul>
<h1>ДО 22:00</h1><ul><li><strong>Земун:</strong> Јована Миодраговића 1</li></ul></div></section>"""


class _Resp:
    text = PAGE


def test_cutoff_read_from_p_heading_with_mixed_alphabet(monkeypatch):
    seen = []
    monkeypatch.setattr(k.requests, "get", lambda *a, **kw: _Resp())
    monkeypatch.setattr(k, "is_hash_processed", lambda h: False)
    monkeypatch.setattr(k, "parse_announcement", lambda text: seen.append(text) or None)
    k.run_scraper()
    assert len(seen) == 2
    assert "(до 15:00 часова)" in seen[0] and "Влајковићева" in seen[0]
    assert "(до 22:00 часова)" in seen[1] and "Јована Миодраговића" in seen[1]


def test_opstina_label_is_the_city():
    # Left to the model the same fault came back as "БЕОГРАД", "Звездара" or "Београд - Стари
    # град" on different reads, and each spelling stored a second outage (26.10.2026).
    assert k._opstina_of("Звездара: Руди Чајавеца бб") == "ЗВЕЗДАРА"
    assert k._opstina_of("Нови Београд: Јурија Гагарина 253") == "НОВИ БЕОГРАД"
    assert k._opstina_of("Палилула : Патриса Лумумбе 16") == "ПАЛИЛУЛА - БЕОГРАД"
    assert k._opstina_of("Влајковићева 25") is None


def test_same_region_folds_cyrillic_case():
    from core.matchmaker import _same_region
    assert _same_region("Београд", "БЕОГРАД")
    assert _same_region("  нови   београд", "НОВИ БЕОГРАД")
    assert not _same_region("Звездара", "БЕОГРАД")


def test_a_fault_is_worded_as_one_with_its_end():
    assert k._fault_template("26.10.2026.", "15:00") == (
        "Kvar na vodovodnoj mreži u ulici *** dana 26.10.2026. BVK navodi da će kvar biti otklonjen do 15:00.")
    assert k._fault_template("26.10.2026.", None) == "Kvar na vodovodnoj mreži u ulici *** dana 26.10.2026."
