"""Šabac water (JKP "Vodovod Šabac", WordPress REST API, Cyrillic): only posts saying water stops
reach the parser - pressure drops, a power cut at the source with water still running, holiday
hours never do - the page builder's shortcodes are stripped, the publish date is put in front,
and the city is always ШАБАЦ."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import pytest  # noqa: E402

from scrapers import vodovodsabac_scraper as sa  # noqa: E402
from spatial.spatial_engine import RGZ_DB_PATH  # noqa: E402

requires_rgz = pytest.mark.skipif(not os.path.exists(RGZ_DB_PATH), reason="data/rgz.db not present")

# Real posts, 2025-2026 - every way this utility has said the water stops.
CUTS = [
    ("ВАЖНО ОБАВЕШТЕЊЕ: ПРЕКИД ВОДОСНАБДЕВАЊА У УЛИЦИ ВИЋЕНТИЈА РАКИЋА",
     "бићемо сутра принуђени да од 10-15 часова искључимо воду"),
    ("ОБАВЕШТЕЊЕ О РАДОВИМА У УЛИЦИ Норвешкој",
     "бићемо сутра принуђени да од 10-15 часова искључимо воду на делу од Хиландарске улице до Проте Смиљанић"),
    ("ОБАВЕШТЕЊЕ О РАДОВИМА У УЛИЦИ МОЦАРТОВА", "у интервалу од 8.30 часова до 13 часова,без воде ће остати улица Моцартова"),
    ("ОБАВЕШТЕЊЕ ЗА ГРАЂАНЕ JEВРЕМОВЦА", "без вооде ће остати део насеља Јевремовац,Варна,Мала и Велика Врањска"),
    ("ОБАВЕШТЕЊЕ ЗА ГРАЂАНЕ Улице Авде Карабеговића У ШАПЦУ", "биће привремена обустава воде у делу улице Авде Карабеговића"),
    ("ОБАВЕШТЕЊЕ ЗА ГРАЂАНЕ Улице Ћирила и Методија У ШАПЦУ", "тај део ће бити без снабдевања пијаћом водом у том периоду"),
    ("OБAВEШTEЊE O РAДOВИMA НA ВOДOВOДНOJ MРEЖИ У МАЈУРУ", "биће затворена вода у Улицама Радивоја Марковића, Херцеговачка и Босанска у Мајуру"),
    ("OБAВEШTEЊE O РAДOВИMA НA ВOДOВOДНOJ MРEЖИ", "у том периоду ове улице неће имати воду"),
    ("ВАЖНО ОБАВЕШТЕЊЕ: ПРЕКИД ВОДОСНАБДЕВАЊА У СРЕМСКОЈ УЛИЦИ", "дошло је до прекида у водоснабдевању у Сремској улици"),
]
NOT_CUTS = [
    ("OБAВEШTEЊE O РAДOВИMA НA БУСТЕР СТАНИЦИ ЈЕВРЕМОВАЦ",
     "биће могућности појаве пада притиска у местима Варна, Јевремовац, Мала Врањска"),
    ("Важно саопштење за јавност – снабдевање електричном енергијом",
     "Престанка снабдевања водом неће бити, али ће се водоснабдевање вршити смањеним интензитетом"),
    ("ОБАВЕШТЕЊЕ О РАДНОМ ВРЕМЕНУ ЗА УСКРШЊЕ ПРАЗНИКЕ", "ЈКП Водовод-Шабац неће радити почев од Великог петка"),
    ("Молимо грађане да рационално користе воду", "долази до пада притиска у мрежи"),
]


def test_only_water_cuts_count():
    assert all(sa._is_cut(t, b) for t, b in CUTS), [t for t, b in CUTS if not sa._is_cut(t, b)]
    assert not any(sa._is_cut(t, b) for t, b in NOT_CUTS)


def test_page_builder_leftovers_are_stripped():
    html = ("<p>[vc_row type=“vc_default“ margin_top=“30″][vc_column][vc_column_text css=““]Због радова "
            "без воде ће остати улица Моцартова.[/vc_column_text][/vc_column][/vc_row]</p>"
            "<style>.gallery-carousel-shortcode .owl-nav div { width: 36px; }</style>")
    assert sa._text(html) == "Због радова без воде ће остати улица Моцартова."


def test_publish_date_and_tomorrow_in_front():
    # Left to work out "сутра" itself, the parser dated a 13.10 post's "сутра" 13.10 (Norveška).
    raw = sa._raw_text("Наслов", "тело", sa._post_date({"date": "2026-10-31T13:40:00"}))
    assert raw.startswith("Шабац, објављено 31.10.2026. Данас је 31.10.2026, сутра је 01.11.2026.")


@requires_rgz
def test_misspelled_village_is_read_as_the_real_one():
    # Jevremovac notices came back from the AI as "ЈЕВРЕГОВАЦ" / "ЈЕВРЕЈОВАЦ" and fell back to a
    # 293-hex area. Real villages and town districts are left as they are.
    from types import SimpleNamespace as Loc
    from scrapers._common import drop_unknown_settlements
    locs = [Loc(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="ЈЕВРЕГОВАЦ"),
            Loc(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="ЈЕВРЕЈОВАЦ"),
            Loc(location_type="ENTIRE_SETTLEMENT", main_street=None, settlement="ВАРНА"),
            Loc(location_type="STREET", main_street="РАДИВОЈА МАРКОВИЋА", settlement="МАЈУР")]
    drop_unknown_settlements(Loc(affected_locations=locs), "ШАБАЦ")
    assert [l.settlement for l in locs] == ["ЈЕВРЕМОВАЦ", "ЈЕВРЕМОВАЦ", "ВАРНА", "МАЈУР"]


def test_run_parses_cuts_only_and_sets_city(monkeypatch):
    posts = [{"id": 1, "date": "2026-10-23T13:40:00", "link": "https://vodovodsabac.rs/p1/",
              "title": {"rendered": CUTS[0][0]}, "content": {"rendered": f"<p>{CUTS[0][1]}, у улици Вићентија Ракић.</p>"}},
             {"id": 2, "date": "2026-10-23T08:00:00", "link": "https://vodovodsabac.rs/p2/",
              "title": {"rendered": NOT_CUTS[0][0]}, "content": {"rendered": f"<p>{NOT_CUTS[0][1]}.</p>"}}]

    class _Resp:
        def json(self):
            return posts

    class _Parsed:
        city = None
        affected_locations = []

    seen, queued = [], []
    monkeypatch.setattr(sa.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(sa, "is_hash_processed", lambda h: False)
    monkeypatch.setattr(sa, "drop_unknown_settlements", lambda parsed, opstina: None)
    monkeypatch.setattr(sa, "parse_announcement", lambda raw: seen.append(raw) or _Parsed())
    monkeypatch.setattr(sa, "process_and_queue_outage", lambda parsed, raw, **kw: queued.append((parsed.city, kw)))
    sa.run_scraper()
    assert len(seen) == 1 and seen[0].startswith("Шабац, објављено 23.10.2026.")
    assert queued == [("ШАБАЦ", {"text_hash": queued[0][1]["text_hash"], "category": "WATER",
                                  "source_url": "https://vodovodsabac.rs/p1/", "live_hashes": queued[0][1]["live_hashes"]})]
