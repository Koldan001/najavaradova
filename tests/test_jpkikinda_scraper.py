"""Kikinda water (JP "Kikinda", WordPress REST API, Cyrillic): only posts saying water stops reach
the parser - parking, the shelter, water-meter readings and holiday hours never do - the publish
date is put in front, and the city is always КИКИНДА."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

from scrapers import jpkikinda_scraper as ki  # noqa: E402

# Real posts, 2026.
CUTS = [
    ("Уторак – Башаид повремено без воде", "без воде повремено ће у наведеном периоду бити цело насељено место Башаид"),
    ("Обавештење о привременом прекиду водоснабдевања у Кикинди", "доћи до привременог прекида водоснабдевања у улици Краљевића Марка"),
    ("Обавештење за потрошаче у Башаиду", "постоји могућност повременог прекида у водоснабдевању"),
    ("Привремени прекид у водоснабдевању у насељеном месту Винцаид", "До обуставе испоруке воде долази због радова"),
    ("Најава планираних радова на водоводној мрежи", "могућа повремена искључења воде на подручју Башаида и Мокрина"),
]
NOT_CUTS = [
    ("Обавештење о контроли и очитавању водомера на подручју Пристаништа", "контрола и очитавање водомера"),
    ("Током „Дана лудаје“ у Кикинди без контроле и наплате паркирања", "без контроле и наплате паркирања"),
    ("ЈП „Кикинда“ обавештава грађане о правилном коришћењу јавних надземних хидраната", "крађа воде из хидраната"),
    ("Обавештење о прекиду фиксне телефоније", "прекид фиксне телефоније"),
]


def test_only_water_cuts_count():
    assert all(ki._is_cut(t, b) for t, b in CUTS)
    assert not any(ki._is_cut(t, b) for t, b in NOT_CUTS)


def test_publish_date_in_front():
    raw = ki._raw_text("Наслов", "тело", ki._post_date({"date": "2026-08-27T09:00:00"}))
    assert raw.startswith("Кикинда, објављено 27.08.2026.")


def test_run_parses_cuts_only_and_sets_city(monkeypatch):
    posts = [{"id": 1, "date": "2026-10-20T08:00:00", "link": "https://www.jpkikinda.rs/p1/",
              "title": {"rendered": CUTS[0][0]}, "content": {"rendered": f"<p>{CUTS[0][1]}, данас 20.10.2026.</p>"}},
             {"id": 2, "date": "2026-10-20T08:00:00", "link": "https://www.jpkikinda.rs/p2/",
              "title": {"rendered": NOT_CUTS[0][0]}, "content": {"rendered": f"<p>{NOT_CUTS[0][1]} на подручју Пристаништа</p>"}}]

    class _Resp:
        def json(self):
            return posts

    class _Parsed:
        city = None
        affected_locations = []

    seen, queued = [], []
    monkeypatch.setattr(ki.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(ki, "is_hash_processed", lambda h: False)
    monkeypatch.setattr(ki, "drop_unknown_settlements", lambda parsed, opstina: None)
    monkeypatch.setattr(ki, "parse_announcement", lambda raw: seen.append(raw) or _Parsed())
    monkeypatch.setattr(ki, "process_and_queue_outage", lambda parsed, raw, **kw: queued.append((parsed.city, kw)))
    ki.run_scraper()
    assert len(seen) == 1
    assert queued == [("КИКИНДА", {"text_hash": queued[0][1]["text_hash"], "category": "WATER",
                                    "source_url": "https://www.jpkikinda.rs/p1/", "live_hashes": queued[0][1]["live_hashes"]})]
