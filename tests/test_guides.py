"""The guide pages (scripts/build_guides.py, web/vodic/)."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scripts import build_city_pages as cp
from scripts import build_guides as bg


def test_published_guides_are_up_to_date():
    for path, page in bg.guide_paths().items():
        assert os.path.exists(path), f"run scripts/build_guides.py: {path} missing"
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == page, f"run scripts/build_guides.py: {path} is out of date"


def test_a_guide_tells_search_engines_what_it_shows():
    for slug, title, _, answer, sections in bg.GUIDES:
        page = bg.guide_html(slug, title, "", answer, sections)
        data = json.loads(re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', page, re.S).group(1))
        assert data["@type"] == "FAQPage"
        shown = re.findall(r"<h2>(.*?)</h2>", page)[:-1]  # the last h2 is "Još" (links)
        assert [q["name"] for q in data["mainEntity"]] == [bg.html.unescape(h) for h in shown]
        assert f'<link rel="canonical" href="https://najavaradova.rs/vodic/{slug}/">' in page
        assert "<p class=\"answer\">" in page  # the short answer comes first


def test_every_link_in_a_guide_leads_somewhere():
    for path, page in bg.guide_paths().items():
        for href in re.findall(r'href="(/[^"#?]*)"', page):
            if href == "/":
                continue
            target = os.path.join(cp.WEB_DIR, href.strip("/"))
            assert (os.path.exists(os.path.join(target, "index.html")) or os.path.exists(target + ".html")
                    or os.path.exists(target)), (path, href)


def test_guides_are_in_the_sitemap_and_linked_from_every_page():
    xml = cp.sitemap_xml([])
    for slug, *_ in bg.GUIDES:
        assert f"<loc>https://najavaradova.rs/vodic/{slug}/</loc>" in xml
        assert f'href="/vodic/{slug}/"' in cp.page_html("Kikinda", "kikinda", {}, "", [])
    for name in ("index.html", "faq.html"):
        with open(os.path.join(cp.WEB_DIR, name), encoding="utf-8") as fh:
            assert 'href="/vodic/' in fh.read(), name


def test_an_outage_card_names_its_streets_in_a_heading():
    groups = {0: [{"id": 1, "kind": "STRUJA", "streets": ["Partizanska (parni brojevi 84-116)"],
                   "region": "Novi Sad", "date": "12.10.2026.", "time": "08:00 - 10:30",
                   "summary": "Planirano isključenje."}]}
    page = cp.page_html("Novi Sad", "novi-sad", groups, "", [])
    assert '<h3 class="s">Partizanska (parni brojevi 84-116)</h3>' in page
