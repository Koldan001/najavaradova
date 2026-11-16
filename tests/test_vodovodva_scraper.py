"""Valjevo water (JKP Vodovod Valjevo, WordPress REST API): only posts saying someone is without
water reach the parser, the publish date is put in front of the text, the city is always ВАЉЕВО,
and every notice still on the feed is passed as a live sibling to the dedup."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

from scrapers import vodovodva_scraper as va  # noqa: E402

CUT = ("<p>Zbog punjenja rezervoara i stabilizacije sistema, bez vodosnabdevanja će biti potrošači "
       "u Novom naselju, Goriću, Popučkama, Lukavcu, Divcima i Dupljaju, večeras, 04.09.2026.godine, "
       "u periodu od 23 časa do 6 ujutru.</p>")
NEWS = ("<p>JKP „Vodovod Valjevo“ nabavilo savremenu usisno-potisnu cisternu za održavanje "
        "kanalizacione mreže.</p>")
APPEAL = "<p>Molimo potrošače da racionalno troše vodu, kako ne bi ostali bez vode u večernjim satima.</p>"


def _post(pid, title, html, date="2026-09-04T10:00:00"):
    return {"id": pid, "date": date, "link": f"https://vodovodva.co.rs/p{pid}/",
            "title": {"rendered": title}, "content": {"rendered": html}}


def test_only_water_cuts_count():
    assert va._is_cut("Obaveštenje potrošačima", va._text(CUT))
    assert va._is_cut("Obaveštenje potrošačima havarija", "u pojedinim delovima grada postoje problemi "
                      "sa vodosnabdevanjem u smislu nižeg pritiska")
    assert not va._is_cut("Nova cisterna", va._text(NEWS))
    assert not va._is_cut("Apel za racionalnu potrošnju", va._text(APPEAL))


def test_photo_caption_dropped_and_publish_date_in_front():
    body = va._text("<p>bez vodosnabdevanja će biti potrošači na Radinom brdu</p>"
                    "<p>Fotografija predstavlja ilustraciju popravke kvara</p>")
    assert "Fotografija" not in body
    raw = va._raw_text("Obaveštenje", body, va._post_date({"date": "2026-08-24T09:00:00"}))
    assert raw.startswith("Valjevo, objavljeno 24.08.2026.")


def test_run_parses_cuts_only_sets_city_and_live_siblings(monkeypatch):
    posts = [_post(1, "Obaveštenje potrošačima punjenje rezervoara", CUT),
             _post(2, "Nova cisterna", NEWS),
             _post(3, "Obaveštenje potrošačima Vojvode Mišića",
                   "<p>Danas, 09.10.2026, od 13 do 19 časova bez vodosnabdevanja će biti potrošači "
                   "u ulici Vojvode Mišića.</p>", date="2026-10-09T08:00:00")]

    class _Resp:
        def json(self):
            return posts

    monkeypatch.setattr(va.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(va, "is_hash_processed", lambda h: False)
    monkeypatch.setattr(va, "drop_unknown_settlements", lambda parsed, opstina: None)

    class _Parsed:
        city = None
        affected_locations = []

    parsed_texts, queued = [], []
    monkeypatch.setattr(va, "parse_announcement", lambda raw: parsed_texts.append(raw) or _Parsed())
    monkeypatch.setattr(va, "process_and_queue_outage",
                        lambda parsed, raw, **kw: queued.append((parsed.city, kw)))

    va.run_scraper()

    assert len(parsed_texts) == 2                      # the cistern news never reaches Gemini
    assert all(city == "ВАЉЕВО" for city, _ in queued)
    assert all(kw["category"] == "WATER" for _, kw in queued)
    assert len(queued[0][1]["live_hashes"]) == 2
    assert queued[0][1]["source_url"] == "https://vodovodva.co.rs/p1/"
