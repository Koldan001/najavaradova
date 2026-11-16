"""scripts/build_city_pages.py: a grad page lists its gradske opštine too, a page never lingers on
outages that are gone, and an unreachable feed fails the run instead of passing silently."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scripts import build_city_pages as cp


def _o(region, d=1, street="X"):
    return {"region": region, "d": d, "kind": "STRUJA", "street": street, "streets": [street]}


def test_belgrade_opstine_are_on_the_belgrade_page_too():
    pages = cp.pages_for([_o("Zvezdara"), _o("Savski Venac", d=0), _o("Palilula - Beograd"),
                          _o("Beograd"), _o("Palilula - Niš"), _o("Palilula"), _o("Kikinda")])
    beograd = pages["beograd"][1]
    assert sorted(o["region"] for day in beograd.values() for o in day) == \
        ["Beograd", "Palilula - Beograd", "Savski Venac", "Zvezdara"]
    assert [o["region"] for day in pages["nis"][1].values() for o in day] == ["Palilula - Niš"]
    # each opština keeps its own page; a bare "Palilula" goes to neither city
    assert "zvezdara" in pages and "palilula" in pages and "kikinda" in pages
    assert all(o["region"] != "Palilula" for day in beograd.values() for o in day)


def test_slugs_keep_every_letter():
    assert cp.slugify("Inđija") == "indjija"
    assert cp.slugify("Mali Iđoš") == "mali-idjos"
    assert cp.slugify("Čukarica") == "cukarica" and cp.slugify("Niš") == "nis"
    assert cp.slugify("Palilula - Beograd") == "palilula-beograd"


def test_every_known_city_gets_a_page_even_when_quiet():
    pages = cp.pages_for([], extra_cities=["Kikinda"])
    assert {"beograd", "novi-sad", "kikinda"} <= set(pages)
    assert pages["kikinda"] == ("Kikinda", {})


def test_published_pages_are_found_again(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "GRAD_DIR", str(tmp_path))
    (tmp_path / "kikinda").mkdir()
    (tmp_path / "kikinda" / "index.html").write_text(
        cp.page_html("Kikinda", "kikinda", {}, "2026-09-24T18:00:00+00:00", []), encoding="utf-8")
    assert cp.existing_page_cities() == ["Kikinda"]


def test_grad_page_names_the_opstina_of_each_row():
    html = cp.page_html("Beograd", "beograd", {1: [_o("Zvezdara", street="Partizanska")]},
                        "2026-09-24T18:00:00+00:00", [])
    assert "Zvezdara · " in html
    own = cp.page_html("Zvezdara", "zvezdara", {1: [_o("Zvezdara", street="Partizanska")]},
                       "2026-09-24T18:00:00+00:00", [])
    assert "Zvezdara · " not in own


def test_the_page_says_how_often_it_is_checked_not_a_date_that_looks_stale():
    # "Ažurirano 10.10.2026. u 23:41", read on the 12th, made a page that changes only when its
    # notices do look abandoned. A cycle is ~4 min of scraping plus a 15 min pause.
    html = cp.page_html("Beograd", "beograd", {}, "2026-09-24T19:07:38+00:00", [])
    assert "na svakih 15–20 minuta" in html and "Ažurirano" not in html


def test_feed_url_with_a_trailing_newline_still_works(monkeypatch):
    """A secret pasted with Enter at the end reached requests as ".../outages.json\\n" -> 404."""
    import requests
    seen = []

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"outages": []}

    monkeypatch.setattr(requests, "get", lambda url, **kw: seen.append(url) or _R())
    args = type("A", (), {"from_db": False, "from_feed": "https://x.test/outages.json\n"})()
    assert cp.load_feed(args) == {"outages": []}
    assert seen == ["https://x.test/outages.json"]


def test_feed_key_is_sent_only_when_set(monkeypatch):
    import requests
    sent = []

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"outages": []}

    monkeypatch.setattr(requests, "get", lambda url, headers=None, **kw: sent.append(headers) or _R())
    args = type("A", (), {"from_db": False, "from_feed": "https://x.test/outages.json"})()
    monkeypatch.delenv("FEED_KEY", raising=False)
    cp.load_feed(args)
    monkeypatch.setenv("FEED_KEY", "k3y\n")
    cp.load_feed(args)
    assert "x-feed-key" not in sent[0]
    assert sent[1]["x-feed-key"] == "k3y"


def test_sitemap_lists_only_final_urls():
    """Pages serves /privacy.html as a 308 to /privacy; Search Console flags a redirecting sitemap URL."""
    xml = cp.sitemap_xml(["beograd"])
    assert ".html" not in xml
    assert "<loc>https://najavaradova.rs/privacy</loc>" in xml
    assert "<loc>https://najavaradova.rs/faq</loc>" in xml


def test_faq_page_says_what_its_data_says():
    """web/faq.html is kept by hand: its FAQPage data must match what the page shows, and every
    page leads to it."""
    import json
    import re
    page = open(os.path.join(cp.WEB_DIR, "faq.html"), encoding="utf-8").read()
    data = json.loads(re.search(r'<script type="application/ld\+json">\s*(.+?)\s*</script>', page, re.S).group(1))
    visible = re.sub(r"<[^>]+>", "", page)
    assert data["@type"] == "FAQPage" and len(data["mainEntity"]) >= 10
    for q in data["mainEntity"]:
        assert q["name"] in visible and q["acceptedAnswer"]["text"] in visible
    assert '<link rel="canonical" href="https://najavaradova.rs/faq">' in page
    assert 'href="/faq"' in cp.page_html("Kikinda", "kikinda", {}, "", [])
    for other in ("index.html", "privacy.html", "terms.html"):
        with open(os.path.join(cp.WEB_DIR, other), encoding="utf-8") as fh:
            assert 'href="/faq"' in fh.read(), other


_HIST = {"regions": {
    "Zvezdara": {"counts": {"VODA": 2, "STRUJA": 1},
                 "recent": [{"date": "2026-09-20", "kind": "VODA", "street": "Partizanska"}]},
    "Beograd": {"counts": {"VODA": 3}, "recent": [{"date": "2026-09-22", "kind": "VODA", "street": "Knez Mihailova"}]},
    "Lazarevac": {"counts": {"STRUJA": 1}, "recent": []},
    "Palilula - Niš": {"counts": {"VODA": 5}, "recent": []},
}}


def test_grad_page_history_sums_its_opstine():
    counts, recent = cp.history_for("Beograd", "beograd", _HIST["regions"])
    assert counts == {"VODA": 5, "STRUJA": 2}
    assert [r["street"] for r in recent] == ["Knez Mihailova", "Partizanska"]
    html = cp.page_html("Beograd", "beograd", {}, "2026-09-24T19:07:38+00:00", [], (counts, recent))
    # One number and its split by kind, once (month_card), then the latest few.
    assert '<div class="big">7</div>' in html and 'struja <b>2</b>' in html and 'voda <b>5</b>' in html
    assert '20.09.</span><i class="kd voda" title="Voda"></i>Voda · Zvezdara · Partizanska' in html
    assert '22.09.</span><i class="kd voda" title="Voda"></i>Voda · Knez Mihailova' in html


def test_quiet_page_still_has_standing_content():
    html = cp.page_html("Vranje", "vranje", {}, "2026-09-24T19:07:38+00:00", [], ({}, []))
    assert "u prethodnih 30 dana nismo zabeležili nijedno isključenje" in html
    assert "Elektrodistribucija Srbije (EPS)" in html and "Vodovod Vranje" in html
    assert "Grejanje: za ovo mesto još nemamo direktan izvor" in html
    assert "Kako funkcioniše" in html


def test_sources_follow_the_grad_only_for_its_urban_opstine():
    names = lambda c, s: [n for _, n, _ in cp.sources_for(c, s)]
    assert names("Zvezdara", "zvezdara") == ["Elektrodistribucija Srbije (EPS)", "BVK Beograd", "Beogradske elektrane"]
    assert names("Lazarevac", "lazarevac") == ["Elektrodistribucija Srbije (EPS)"]
    assert names("Sombor", "sombor") == ["Elektrodistribucija Srbije (EPS)"]
    assert names("Kikinda", "kikinda") == ["Elektrodistribucija Srbije (EPS)", "JP Kikinda"]


def test_feed_without_history_leaves_the_section_out():
    html = cp.page_html("Vranje", "vranje", {}, "2026-09-24T19:07:38+00:00", [])
    assert "Poslednjih 30 dana" not in html and "Koje izvore pratimo" in html


def test_serbian_plurals():
    assert cp.plural(1, "najava", "najave", "najava") == "1 najava"
    assert cp.plural(22, "najava", "najave", "najava") == "22 najave"
    assert cp.plural(12, "najava", "najave", "najava") == "12 najava"
    assert cp.plural(21, "isključenje", "isključenja", "isključenja") == "21 isključenje"


def test_unreachable_feed_fails_the_run(monkeypatch):
    monkeypatch.setattr(cp, "load_feed", lambda args: None)
    monkeypatch.setattr(sys, "argv", ["build_city_pages.py", "--from-feed", "https://x.test/outages.json"])
    assert cp.main() == 1


# --- search: titles, snippets, one listing per notice, honest sitemap dates, the /grad/ hub ---

def test_title_names_what_the_page_covers_and_its_sources():
    title = lambda city, slug: cp.page_title(city, cp.sources_for(city, slug))
    assert title("Kraljevo", "kraljevo") == "Nestanak struje i vode Kraljevo danas — najave (Vodovod Kraljevo, EPS)"
    assert title("Beograd", "beograd") == "Nestanak struje i vode Beograd danas — najave (BVK Beograd, EPS)"
    # only EPS is read for Sombor: the page doesn't promise water it has no source for
    assert title("Sombor", "sombor") == "Nestanak struje Sombor danas — najave (EPS)"
    # a neighbourhood: its name and the searched words first, all three kinds
    assert cp.page_title("Beograd", cp.sources_for("Beograd", "beograd"), kvart="Mirijevo III") == \
        "Mirijevo III bez struje, vode ili grejanja? Najave danas — Beograd"


def test_old_and_new_headings_are_both_found(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "GRAD_DIR", str(tmp_path))
    for slug, h1 in (("kikinda", "Najave isključenja struje — Kikinda"),
                     ("vranje", "Isključenja struje, vode i grejanja — Vranje")):
        (tmp_path / slug).mkdir()
        (tmp_path / slug / "index.html").write_text(f"<h1>{h1}</h1>", encoding="utf-8")
    assert cp.existing_page_cities() == ["Kikinda", "Vranje"]


def _w(id_, d, street, date="08.09.–28.10.", time="ceo dan", kind="VODA"):
    return {"id": id_, "region": "Kraljevo", "d": d, "kind": kind, "street": street, "streets": [street],
            "date": date, "time": time}


def test_description_names_the_streets_and_no_clock_time():
    src = cp.sources_for("Kraljevo", "kraljevo")
    desc = cp.page_description("Kraljevo", src, {0: [_w(1, 0, "Jug Bogdanove")],
                                                 1: [_w(2, 1, "Cara Lazara", kind="STRUJA")]}, None)
    assert desc == ("Bez vode ili struje? Kraljevo — danas, voda: Jug Bogdanove; sutra, struja: Cara Lazara. "
                    "Ulice, vreme i zvanična objava.")
    many = {1: [_w(i, 1, f"Ulica broj {i} sa dugim imenom") for i in range(10)]}
    assert cp.page_description("Kraljevo", src, many, None).endswith(
        " i još 8 najava. Ulice, vreme i zvanična objava.")
    # a quiet page leads with what happened there, never "trenutno nema"
    sombor = cp.sources_for("Sombor", "sombor")
    quiet = cp.page_description("Sombor", sombor, {}, ({"STRUJA": 5}, [{"date": "2026-10-30"}]))
    assert quiet == ("Sombor: 5 isključenja struje u poslednjih 30 dana, poslednje 30.10. "
                     "Proveri svoju ulicu i dobij poruku dan ranije.")
    assert cp.page_description("Kraljevo", src, {}, ({"VODA": 1}, [])) == (
        "Kraljevo: 1 isključenje vode u poslednjih 30 dana. Proveri svoju ulicu i dobij poruku dan ranije.")
    assert cp.page_description("Sombor", sombor, {}, None) == (
        "Sombor: najave isključenja struje na jednom mestu - ulice, vreme i zvanična objava. "
        "Poruka za tvoju adresu dan ranije.")
    html = cp.page_html("Kraljevo", "kraljevo", {0: [_w(1, 0, "Jug Bogdanove")]}, "2026-09-27T16:56:56+00:00", [])
    assert "18:56" not in html.split("<body>")[0]      # the clock time is on the page, not in <head>


def test_works_running_for_weeks_are_listed_once():
    groups = {d: [_w(7, d, "Jug Bogdanove")] for d in (0, 1, 2)}
    groups[1].append(_w(8, 1, "Cara Lazara", date="28.09.2026", time="08:00 - 14:00"))
    groups[2].append(_w(8, 2, "Cara Lazara", date="29.09.2026", time="08:00 - 14:00"))  # a second day
    html = cp.page_html("Kraljevo", "kraljevo", groups, "2026-09-27T16:56:56+00:00", [])
    body = html.split("<body>")[1]
    assert body.count(">Jug Bogdanove<") == 1 and body.count(">Cara Lazara<") == 2
    assert "3 najave" in body


def test_an_opstina_page_leads_up_to_its_grad():
    html = cp.page_html("Zvezdara", "zvezdara", {}, "2026-09-27T16:56:56+00:00", [])
    assert '<a href="/grad/">Svi gradovi</a> › <a href="/grad/beograd/">Beograd</a></nav>' in html
    assert '"item": "https://najavaradova.rs/grad/zvezdara/"' in html
    assert '<a href="/grad/">Svi gradovi</a></nav>' in cp.page_html("Kikinda", "kikinda", {}, "", [])


def test_hub_lists_every_page_with_what_it_covers():
    pages = cp.pages_for([_w(1, 0, "Jug Bogdanove"), _w(1, 1, "Jug Bogdanove")], extra_cities=["Zvezdara"])
    index = sorted(((n, s) for s, (n, _) in pages.items()), key=lambda cs: cs[0].lower())
    hub = cp.hub_html(index, pages)
    assert '<a href="/grad/kraljevo/">Kraljevo</a> <span>struja, voda · 1 najava</span>' in hub
    assert '<a href="/grad/zvezdara/">Zvezdara</a> <span>Beograd · struja, voda, grejanje</span>' in hub
    assert '<link rel="canonical" href="https://najavaradova.rs/grad/">' in hub


def test_sitemap_date_moves_only_when_a_page_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "WEB_DIR", str(tmp_path))
    monkeypatch.setattr(cp, "GRAD_DIR", str(tmp_path / "grad"))
    feed = {"outages": [_w(1, 0, "Jug Bogdanove")]}
    monkeypatch.setattr(cp, "load_feed", lambda args: feed)
    monkeypatch.setattr(sys, "argv", ["build_city_pages.py", "--from-feed", "https://x.test/outages.json"])
    lastmods = lambda: cp.previous_lastmods(str(tmp_path / "sitemap.xml"))

    assert cp.main() == 0
    first = lastmods()
    kraljevo, beograd = "https://najavaradova.rs/grad/kraljevo/", "https://najavaradova.rs/grad/beograd/"
    assert kraljevo in first and "https://najavaradova.rs/grad/" in first
    assert "https://najavaradova.rs/privacy" not in first          # no date we can't vouch for
    assert (tmp_path / "grad" / "index.html").exists()

    # same feed later: nothing changed, every date stays
    import datetime as dt

    class Later(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 28, 9, 0, tzinfo=tz)

    monkeypatch.setattr(cp, "datetime", Later)
    assert cp.main() == 0
    # /danas/ names the day, so a new day is a new page; every other date stays
    danas = "https://najavaradova.rs/danas/"
    assert {k: v for k, v in lastmods().items() if k != danas} == {k: v for k, v in first.items() if k != danas}

    # Kraljevo's works end: its page and the hub move, Beograd's doesn't
    feed["outages"] = []
    assert cp.main() == 0
    now = lastmods()
    assert now[kraljevo] == "2026-09-28T09:00:00+00:00" and now["https://najavaradova.rs/grad/"] == now[kraljevo]
    assert now[beograd] == first[beograd]


def test_belgrade_prefixed_region_is_the_opstina_page():
    assert cp.canonical_region("Beograd - Vračar") == "Vračar"
    assert cp.canonical_region("Beograd - Palilula") == "Palilula - Beograd"
    assert cp.canonical_region("Niš - Medijana") == "Medijana"
    assert cp.canonical_region("Beograd - Negde") == "Beograd - Negde"      # not an opština: left alone
    pages = cp.pages_for([dict(_o("Beograd - Vračar"), street="Njegoševa")], extra_cities=["Beograd - Vračar"])
    assert "beograd-vracar" not in pages
    assert [o["region"] for day in pages["vracar"][1].values() for o in day] == ["Vračar"]
    assert [o["street"] for day in pages["beograd"][1].values() for o in day] == ["Njegoševa"]
    counts, _ = cp.history_for("Vračar", "vracar", {"Beograd - Vračar": {"counts": {"VODA": 2}, "recent": []},
                                                    "Vračar": {"counts": {"VODA": 1}, "recent": []}})
    assert counts == {"VODA": 3}


def test_page_opens_the_map_on_its_outages():
    """A city page was only a list; the map, the product's strongest part, was never shown. A
    button opens the home map on all of the page's notices, a link on each row on that one. The
    ids go after '#', so Google sees no new URLs."""
    line = {"paths": [[[43.7, 20.6], [43.71, 20.61]]]}
    groups = {0: [dict(_w(7, 0, "Jug Bogdanove"), **line)],
              1: [dict(_w(7, 1, "Jug Bogdanove"), **line), dict(_w(8, 1, "Cara Lazara"), **line)]}
    body = cp.page_html("Kraljevo", "kraljevo", groups, "2026-09-29T16:00:00+00:00", []).split("<body>")[1]
    assert '<a class="mapbtn" href="/#o=7">' in body                       # today's, the first tab
    # the row links, plus the map box on today's tab (only #7 is today)
    assert body.count('href="/#o=7"') == 2 and body.count('href="/#o=8"') == 1
    assert body.index('class="mapbtn"') < body.index("Jug Bogdanove")      # above the list

    quiet = cp.page_html("Kikinda", "kikinda", {}, "2026-09-29T16:00:00+00:00", [])
    assert 'class="mapbtn"' not in quiet.split("<body>")[1]              # nothing to show
    no_id = cp.page_html("Kraljevo", "kraljevo", {1: [_o("Kraljevo")]}, "", [])
    assert "#o=" not in no_id


def test_signup_box_knows_the_place_the_form_knows():
    # The form's list has "Beograd (Zemun)" for Belgrade's urban opštine, plain names elsewhere.
    assert cp.signup_places("Zemun", "zemun") == ["Beograd (Zemun)", "Zemun", "Beograd"]
    assert cp.signup_places("Palilula - Niš", "palilula-nis") == ["Niš (Palilula)", "Palilula", "Niš"]
    assert cp.signup_places("Vranje", "vranje") == ["Vranje"]
    assert cp.signup_places("Beograd", "beograd") == ["Beograd"]
    # A neighbourhood goes by the opština its outages are listed under.
    groups = {1: [{"region": "Zemun"}, {"region": "Beograd"}]}
    history = ({}, [{"region": "Zemun"}])
    assert cp.signup_places("Altina", "beograd/altina", ("Beograd", "beograd"), groups, history) == [
        "Beograd (Zemun)", "Zemun", "Beograd"]
    assert cp.signup_places("Liman", "novi-sad/liman", ("Novi Sad", "novi-sad"), {}, None) == ["Novi Sad"]


def test_page_asks_to_sign_up_once_and_quietly():
    o = {"id": 7, "region": "Vranje", "street": "Katun (celo naselje)", "kind": "STRUJA",
         "date": "06.10.2026.", "time": "08:00 - 10:00", "summary": "Kvar."}
    page = cp.page_html("Vranje", "vranje", {1: [o]}, "2026-10-05T16:00:00+00:00", [])
    assert page.count('id="prijava-ovde"') == 1
    assert '<a class="btn" data-signup href="/?prijava=1&amp;grad=Vranje">🔔 Prijavi svoju adresu</a>' in page
    # the form opens right on the page: the shared signup script and its styles are loaded
    assert '<script src="/signup.js" defer></script>' in page
    assert '<link rel="stylesheet" href="/signup.css">' in page
    # the card comes after the list
    assert page.index('<article class="o') < page.index('id="prijava-ovde"')
    # no form and no button on every card
    for gone in ("<form", 'class="notify"', 'id="subBar"', 'class="cta"', 'class="lede"'):
        assert gone not in page


def test_the_invitation_comes_once_and_the_reason_once():
    """Until 11.11.2026 a quiet page carried the box twice, a few lines apart, and its month's
    figure three times: it read like spam. Now: one box, the figure once, in the month card."""
    o = {"id": 7, "region": "Vranje", "street": "Katun (celo naselje)", "kind": "STRUJA",
         "date": "06.10.2026.", "time": "08:00 - 10:00", "summary": "Kvar."}
    history = ({"STRUJA": 3, "VODA": 1},
               [{"date": "2026-10-01", "kind": "STRUJA", "street": "Katun", "region": "Vranje"}])
    page = cp.page_html("Vranje", "vranje", {1: [o]}, "2026-10-05T16:00:00+00:00", [], history=history)
    assert page.count('class="ask"') == 1 and page.count('id="prijava-ovde"') == 1
    assert "Da li je tvoja ulica sledeća?" in page                      # a busy page
    assert "Javljamo samo kad je tvoja ulica i broj u najavi." in page
    assert page.index('id="prijava-ovde"') < page.index("Poslednjih 30 dana")
    # and the bar for phones, opening the same form - hidden until the visitor scrolls
    assert ('<a class="stick off" data-signup href="/?prijava=1&amp;grad=Vranje">'
            '🔔 Prijavi adresu, besplatno</a>') in page
    assert "window.scrollY > 240" in page
    # a quiet page: the past month first, as the reason, then the one box
    quiet = cp.page_html("Vranje", "vranje", {}, "2026-10-05T16:00:00+00:00", [], history=history)
    assert quiet.count('class="ask"') == 1 and "Sledeći put saznaj dan ranije" in quiet
    assert quiet.index('class="month"') < quiet.index('id="prijava-ovde"') < quiet.index("Nedavna isključenja")
    body = quiet.split("<body>")[1]
    assert body.count("u poslednjih 30 dana") == 1


def test_month_card_strip_colours_each_day_by_what_was_cut():
    days = "0" * 28 + "1" + "3" + "4"          # power; power and water; heating (today)
    card = cp.month_card(({"STRUJA": 2, "VODA": 1, "GREJANJE": 1}, []), days)
    strip = card.split('class="strip"')[1].split("</div>")[0]
    assert strip.count("<span>") == 31
    assert '<span><i class="struja"></i><i class="voda"></i></span>' in card
    assert '<span><i class="grejanje"></i></span>' in card
    assert 'struja <b>2</b>' in card and 'grejanje <b>1</b>' in card and '<div class="kb">' in card
    assert "oko 1 nedeljno" in card
    assert 'class="strip"' not in cp.month_card(({"STRUJA": 2}, []), "zz")     # not ours: no strip
    assert cp.month_card(({}, []), days) == ""


def test_a_grad_page_strip_has_every_opstina_day():
    regions = {"Zvezdara": {"days": "0012"}, "Palilula - Beograd": {"days": "0210"}, "Vranje": {"days": "4444"}}
    assert cp.days_for("Beograd", "beograd", regions) == "0212"
    assert cp.days_for("Vranje", "vranje", regions) == "4444"
    assert cp._or_days(["01", "012"]) is None and cp._or_days([None]) is None


def test_month_card_never_shows_a_bare_number():
    """Kikinda's card said "24" and nothing else (11.11.2026): what of, how often, since when."""
    card = cp.month_card(({"STRUJA": 24}, [{"date": "2026-11-04", "kind": "STRUJA", "street": "X"}]), None)
    assert '<div class="big">24</div>' in card and "oko 6 nedeljno" in card
    assert "struja <b>24</b>" in card and "poslednje: 04.11." in card and '<div class="kb">' in card


def test_small_map_shows_one_day_and_opens_the_big_map_on_that_day():
    """It drew today and tomorrow at once, and its link opened the big map on today framed on
    tomorrow's streets - empty (11.11.2026). Now a tab per day; map and link are the same day."""
    a = dict(_w(7, 0, "Jug Bogdanove"), color="#E0B05C", paths=[[[43.7, 20.6], [43.71, 20.61]]], areas=[], kind="RADOVI")
    b = dict(_w(8, 1, "Cara Lazara"), color="#4A9EE0", paths=[], areas=[{"dot": [43.72, 20.62]}], kind="STRUJA")
    c = dict(_w(9, 1, "Bez oblika"), paths=[], areas=[], kind="STRUJA")           # nothing to draw
    groups = {0: [a], 1: [b, c]}
    data = cp.map_data(cp.dedupe_days(groups))
    assert [(o["d"], o["c"]) for o in data["o"]] == [(0, "#E0B05C"), (1, "#4A9EE0")]
    html = cp.minimap("kraljevo", groups)
    assert '<button type="button" data-d="0" data-ids="7" data-kinds="radovi" aria-pressed="true">Danas <b>1</b></button>' in html
    # counts like the list under "Sutra" (2), though only one of them has a shape to draw
    assert 'data-d="1" data-ids="8" data-kinds="struja" aria-pressed="false">Sutra <b>2</b>' in html
    assert '<a class="mapbtn" href="/#o=7">' in html                               # today's only
    assert 'data-src="/grad/kraljevo/karta.json" data-day="0"' in html
    tomorrow_only = cp.minimap("kraljevo", {1: [b]})
    assert 'data-day="1"' in tomorrow_only and 'href="/#o=8"' in tomorrow_only
    assert cp.minimap("kikinda", {}) == "" and cp.minimap("x", {1: [c]}) == ""


def test_on_a_long_list_the_card_comes_after_the_first_few():
    items = [{"id": i, "region": "Beograd", "street": f"Ulica {i}", "kind": "VODA"} for i in range(12)]
    page = cp.page_html("Beograd", "beograd", {0: items}, "2026-10-08T10:00:00+00:00", [])
    card = page.index('id="prijava-ovde"')
    assert page.count('<article class="o', 0, card) == cp.ASK_AFTER
    quiet = cp.page_html("Kikinda", "kikinda", {}, "2026-10-08T10:00:00+00:00", [])
    assert quiet.index('class="empty"') < quiet.index('id="prijava-ovde"')


def test_page_answers_in_one_sentence_what_is_on():
    src = cp.sources_for("Kraljevo", "kraljevo")
    groups = {0: [_w(1, 0, "Jug Bogdanove"), _w(2, 0, "Cara Lazara", kind="STRUJA")],
              1: [_w(3, 1, "Ibarska", kind="STRUJA")]}
    assert cp.summary_line("Kraljevo", src, groups, None) == \
        "Kraljevo: 3 najave — danas 2 (struja, voda), sutra 1 (struja). Ispod su ulice, vreme i link na zvaničnu objavu."
    assert cp.summary_line("Novi Sad", src, {0: groups[0]}, None) == \
        "Novi Sad danas: 2 najave (struja, voda) — ulice, vreme i link na zvaničnu objavu."
    assert cp.summary_line("Kraljevo", src, {}, ({"STRUJA": 4, "VODA": 1}, [])) == \
        "Kraljevo: trenutno nema najavljenih isključenja vode ili struje. U poslednjih 30 dana: 5 isključenja."
    page = cp.page_html("Kraljevo", "kraljevo", groups, "2026-10-07T10:00:00+00:00", [])
    assert '<p class="sum">Kraljevo: 3 najave — danas 2' in page


def test_faq_is_shown_and_repeated_as_faqpage_data():
    import json
    import re
    page = cp.page_html("Vranje", "vranje", {}, "2026-10-07T10:00:00+00:00", [])
    assert "<h2>Česta pitanja</h2>" in page
    assert "Gde da proverim da li će nestati vode ili struje?" in page    # no heating source there
    assert "Vranje: najave objavljuju Elektrodistribucija Srbije (EPS) i Vodovod Vranje." in page
    data = json.loads(re.search(r'<script type="application/ld\+json">\s*(.+?)\s*</script>', page, re.S).group(1))
    faq = next(item for item in data if item["@type"] == "FAQPage")
    for q in faq["mainEntity"]:
        assert cp.esc(q["name"]) in page and cp.esc(q["acceptedAnswer"]["text"]) in page


def test_home_and_city_pages_share_one_signup_form():
    """The form lives once, in web/signup.js: the home page loads it before its own script and
    keeps no copy of the markup."""
    with open(os.path.join(cp.WEB_DIR, "index.html"), encoding="utf-8") as fh:
        home = fh.read()
    with open(os.path.join(cp.WEB_DIR, "signup.js"), encoding="utf-8") as fh:
        js = fh.read()
    assert 'id="modalOverlay"' not in home and 'id="modalOverlay"' in js
    assert home.index('<script src="/signup.js"></script>') < home.index("NRSignup.openFromUrl();")
    assert '<link rel="stylesheet" href="/signup.css">' in home
    assert "window.NRSignup = { open: open, openFromUrl: openFromUrl, me: me };" in js


def test_danas_lists_today_and_tomorrow_by_city_most_first():
    """/danas/ answers "nestanak struje danas" for all of Serbia: one line per city, a Beograd
    opština counted on Beograd's line only, each line linking to its city page."""
    outages = [_w(1, 0, "Jug Bogdanove"), _w(2, 0, "Cara Lazara", kind="STRUJA"), _w(3, 1, "Omladinska"),
               dict(_w(4, 0, "Gorička"), region="Zvezdara")]
    pages = cp.pages_for(outages)
    index = sorted(((n, s) for s, (n, _) in pages.items()), key=lambda cs: cs[0].lower())
    html = cp.danas_html(index, pages)
    assert "<title>Nestanak struje danas — sve najave isključenja struje i vode u Srbiji</title>" in html
    assert '<link rel="canonical" href="https://najavaradova.rs/danas/">' in html
    today, tomorrow = html.split("<h2>Danas")[1].split("<h2>Sutra")
    assert today.index("/grad/kraljevo/") < today.index("/grad/beograd/")   # 2 notices before 1
    assert "struja 1, voda 1" in today and "Jug Bogdanove, Cara Lazara" in today
    assert "/grad/zvezdara/" not in html                                   # on Beograd's line
    assert "Omladinska" in tomorrow and "Omladinska" not in today
    assert "Danas u Srbiji: 3 najave isključenja struje i vode — Kraljevo, Beograd." in html


def test_danas_answers_in_one_sentence_and_has_faq():
    """AI answers and Google's snippets quote a short self-contained sentence: the day, how many,
    where most. The FAQ on the page is repeated as FAQPage data, which must match it."""
    import json
    outages = [_w(1, 0, "Jug Bogdanove"), _w(2, 0, "Cara Lazara", kind="STRUJA"),
               dict(_w(3, 0, "Gorička", kind="STRUJA"), region="Zvezdara")]
    pages = cp.pages_for(outages)
    index = sorted(((n, s) for s, (n, _) in pages.items()), key=lambda cs: cs[0].lower())
    html = cp.danas_html(index, pages, "2026-11-03T08:00:00+00:00")
    assert ('<p class="answer">Danas, 3. novembra 2026. godine, u Srbiji: 3 najave '
            '(struja 2, voda 1) u 2 mesta (najviše: Kraljevo, Beograd).</p>') in html
    assert "Ažurirano: 03.11.2026. u 09:00" in html
    data = json.loads(html.split('<script type="application/ld+json">')[1].split("</script>")[0])
    assert data["@type"] == "FAQPage"
    for q in data["mainEntity"]:
        assert f"<h3>{q['name']}</h3>" in html
    quiet = cp.danas_html([], {}, "2026-11-03T08:00:00+00:00")
    assert "Danas, 3. novembra 2026. godine, u Srbiji nema najavljenih isključenja struje, vode ni grejanja." in quiet


def test_sitemap_lists_danas():
    assert "<loc>https://najavaradova.rs/danas/</loc>" in cp.sitemap_xml(["beograd"])
