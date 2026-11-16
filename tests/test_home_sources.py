"""The home page's "Izvori koje pratimo" (PROVIDERS in web/index.html) is typed by hand, and it
had drifted: Vodovod Kruševac and both toplane of Niš and Kruševac were missing, and EPS was said
to cover five cities. Its water and heating names must be exactly the city pages' CITY_SOURCES."""
import os
import re
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scripts import build_city_pages as cp

INDEX = os.path.join(os.path.dirname(__file__), '..', 'web', 'index.html')


def _home_providers() -> dict[str, list[str]]:
    with open(INDEX, encoding='utf-8') as fh:
        page = fh.read()
    block = page[page.index('var PROVIDERS = ['):]
    block = block[:block.index('];')]
    rows = re.findall(r'kind: "([^"]+)".*?names: \[([^\]]*)\]', block, re.S)
    return {kind: re.findall(r'"([^"]+)"', names) for kind, names in rows}


def test_home_lists_exactly_the_sources_the_city_pages_read():
    home = _home_providers()
    city = {'VODA': set(), 'GREJANJE': set()}
    for sources in cp.CITY_SOURCES.values():
        for kind, name, _url in sources:
            city[kind].add(name)
    assert home['Struja'] == ['EPS Distribucija']
    assert sorted(home['Voda']) == sorted(city['VODA'])
    assert sorted(home['Grejanje i topla voda']) == sorted(city['GREJANJE'])
