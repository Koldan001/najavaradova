"""Generate one static, indexable HTML page per covered city from the outage feed.

The SPA (web/index.html) renders client-side, so Google has almost nothing to rank for
non-brand queries like "iskljucenja struje novi sad" or "najava radova beograd". These pages
are plain server-rendered HTML - one per city, listing that city's current and upcoming
outages with schema.org SpecialAnnouncement markup - real content for Google to index, each
linking back into the app.

Data source (pick one):
    --from-feed [URL]   fetch the published feed
                        (default URL: https://api.najavaradova.rs/outages.json)
    --from-db           build straight from the local DB via build_web_data.build()
    --from-file PATH    a saved feed

Writes web/grad/<slug>/index.html for every covered city, refreshes web/sitemap.xml, and
prints what changed. Meant to run on a schedule (.github/workflows/city-pages.yml), commit the
diff, and let Cloudflare Pages redeploy.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

SERBIA_TZ = ZoneInfo("Europe/Belgrade")

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
GRAD_DIR = os.path.join(WEB_DIR, "grad")
SITE = "https://najavaradova.rs"
DEFAULT_FEED = "https://api.najavaradova.rs/outages.json"

# Always emit a page for these even on a quiet day, so the URL stays indexed and ranks the
# moment an outage lands. EPS (power) is national; ViK/toplana coverage grows over time.
ALWAYS_CITIES = [
    "Beograd", "Novi Sad", "Niš", "Kragujevac", "Kraljevo", "Čačak", "Kruševac",
    "Pančevo", "Zrenjanin", "Leskovac", "Vranje", "Novi Pazar", "Subotica", "Sremska Mitrovica",
]

# Belgrade's and Niš's own sources (BVK, EPS) name the gradska opština, not the city, so a grad
# page built from region == "Beograd" alone missed Zvezdara, Savski Venac, Zemun... Such an
# outage is listed on its opština's own page and on its grad's page. A bare "Palilula" belongs
# to neither: both cities have one.
GRAD_MEMBERS = {
    "Beograd": ["Barajevo", "Čukarica", "Grocka", "Lazarevac", "Mladenovac", "Novi Beograd",
                "Obrenovac", "Palilula - Beograd", "Palilula (Beograd)", "Rakovica", "Savski Venac",
                "Sopot", "Stari Grad", "Surčin", "Voždovac", "Vračar", "Zemun", "Zvezdara"],
    "Niš": ["Crveni Krst", "Medijana", "Niška Banja", "Palilula - Niš", "Palilula (Niš)", "Pantelej"],
}

# What we read for each place ("Koje izvore pratimo"). Power is EPS everywhere; water and heating
# only where main.py runs a dedicated scraper. A grad's sources cover its urban gradske opštine
# too, not the outer ones, which have their own utilities.
EPS_SOURCE = ("STRUJA", "Elektrodistribucija Srbije (EPS)", "https://elektrodistribucija.rs/")
CITY_SOURCES = {
    "Beograd": [("VODA", "BVK Beograd", "https://www.bvk.rs/kvarovi-na-mrezi/"),
                ("GREJANJE", "Beogradske elektrane", "https://beoelektrane.co.rs/category/poremecaji-u-snabdevanju/")],
    "Novi Sad": [("VODA", "ViK Novi Sad", "https://www.vikns.rs/category/najava-radova-sr/"),
                 ("GREJANJE", "Novosadska toplana", "https://nstoplana.rs/")],
    "Niš": [("VODA", "JKP Naissus", "https://jkpnaissus.co.rs/servisne-informacije/"),
            ("GREJANJE", "Gradska toplana Niš", "https://nitoplana.rs/")],
    "Kragujevac": [("VODA", "ViK Kragujevac", "https://jkpvik-kg.com/iskljucenja/"),
                   ("GREJANJE", "Energetika Kragujevac", "https://www.energetika-kragujevac.com/vesti.php")],
    "Zrenjanin": [("VODA", "ViK Zrenjanin", "https://www.vikzr.rs/category/vesti/najnovije-vesti/"),
                  ("GREJANJE", "Toplana Zrenjanin", "https://www.gradskatoplanazr.rs/servisne-informacije.html")],
    "Kruševac": [("VODA", "Vodovod Kruševac", "https://vodovodks.rs/sr/informations/info-servis"),
                 ("GREJANJE", "Toplana Kruševac", "https://toplanakrusevac.rs/")],
    "Pančevo": [("VODA", "Vodovod Pančevo", "https://www.vodovodpa.rs/")],
    "Čačak": [("VODA", "Vodovod Čačak", "https://vodovodca.rs/kvarovi-na-mrezi/")],
    "Kraljevo": [("VODA", "Vodovod Kraljevo", "https://www.vodovodkv.co.rs/kvarovi_mreza.php")],
    "Leskovac": [("VODA", "Vodovod Leskovac", "https://www.vodovodle.rs/servisne-informacije.php")],
    "Vranje": [("VODA", "Vodovod Vranje", "https://vodovodvranje.rs/")],
    "Valjevo": [("VODA", "Vodovod Valjevo", "https://vodovodva.co.rs/category/vesti/")],
    "Kikinda": [("VODA", "JP Kikinda", "https://www.jpkikinda.rs/vesti/")],
    "Šabac": [("VODA", "Vodovod Šabac", "https://vodovodsabac.rs/category/aktuelnosti/")],
}
OUTER_OPSTINE = {"Barajevo", "Grocka", "Lazarevac", "Mladenovac", "Obrenovac", "Sopot"}
RECENT_SHOWN = 6

DAY_LABELS = {0: "Danas", 1: "Sutra", 2: "Prekosutra"}
KIND_LABEL = {"VODA": "Voda", "STRUJA": "Struja", "GREJANJE": "Grejanje", "RADOVI": "Radovi"}
KIND_GENITIVE = {"VODA": "vode", "STRUJA": "struje", "GREJANJE": "grejanja"}
SCHEMA_CATEGORY = {
    "VODA": "https://www.wikidata.org/wiki/Q7907009",
    "STRUJA": "https://www.wikidata.org/wiki/Q1531667",
    "GREJANJE": "https://www.wikidata.org/wiki/Q890888",
}


def slugify(city: str) -> str:
    # đ has no NFKD decomposition - it would vanish ("Inđija" -> "inija").
    s = unicodedata.normalize("NFKD", city.replace("đ", "dj").replace("Đ", "Dj"))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "grad"


_MEMBER_TO_GRAD = {slugify(m): grad for grad, members in GRAD_MEMBERS.items() for m in members}


def canonical_region(name: str | None) -> str:
    """"Beograd - Vračar", as a notice sometimes names it, is Vračar's page, not one of its own
    (a duplicate /grad/beograd-vracar/ appeared on 27.09.2026). "Beograd - Palilula" is
    "Palilula - Beograd"."""
    name = (name or "").strip()
    m = re.match(r"^(Beograd|Niš)\s*-\s*(.+)$", name)
    if m:
        for member in (m[2], f"{m[2]} - {m[1]}"):
            if _MEMBER_TO_GRAD.get(slugify(member)) == m[1]:
                return member
    return name


def pages_for(outages: list[dict], extra_cities=()) -> dict[str, tuple[str, dict[int, list[dict]]]]:
    """slug -> (page name, {day: [outages]}): every outage under its own region, a gradska
    opština's also under its grad; a page for every ALWAYS_CITIES / extra city even when empty."""
    pages: dict[str, tuple[str, dict[int, list[dict]]]] = {}

    def add(name: str, o: dict | None):
        _, groups = pages.setdefault(slugify(name), (name, {}))
        if o is not None:
            groups.setdefault(int(o.get("d", 0)), []).append(o)

    for o in outages:
        region = canonical_region(o.get("region"))
        if not region:
            continue
        if region != o.get("region"):
            o = dict(o, region=region)
        add(region, o)
        grad = _MEMBER_TO_GRAD.get(slugify(region))
        if grad:
            add(grad, o)
    for city in list(ALWAYS_CITIES) + list(extra_cities):
        add(canonical_region(city), None)
    return pages


def sources_for(city: str, slug: str) -> list[tuple[str, str, str]]:
    own = CITY_SOURCES.get(city)
    if own is None and city not in OUTER_OPSTINE:
        own = CITY_SOURCES.get(_MEMBER_TO_GRAD.get(slug, ""))
    return [EPS_SOURCE] + list(own or [])


def _join(words: list[str], last: str) -> str:
    """["vode", "struje", "grejanja"], "i" -> "vode, struje i grejanja"."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {last} {words[-1]}"


def covered(sources: list[tuple[str, str, str]], last: str) -> str:
    """What a page's sources cover, in the genitive: "vode i struje", or "struje" alone where
    only EPS is read. A page never promises water or heating it has no source for."""
    return _join([KIND_GENITIVE[k] for k in ("VODA", "STRUJA", "GREJANJE")
                  if any(s[0] == k for s in sources)], last)


def summary_line(label: str, sources, groups: dict[int, list[dict]],
                 history: tuple[dict[str, int], list[dict]] | None) -> str:
    """The page's answer in one plain sentence under the heading: "Novi Sad danas: 6 najava
    (struja, radovi) — ulice, vreme i link na zvaničnu objavu." AI search engines and Google's
    snippets quote short, self-contained statements like this, not a list of cards. It says
    "najava", not "isključenje": weeks of works on a street are not a cut. No date of its own:
    the day labels already move with each day, and a date would rewrite every quiet page."""
    total = sum(len(v) for v in groups.values())
    if not total:
        n = sum(history[0].values()) if history else 0
        past = (f" U poslednjih 30 dana: {plural(n, 'isključenje', 'isključenja', 'isključenja')}."
                if n else "")
        return f"{label}: trenutno nema najavljenih isključenja {covered(sources, 'ili')}.{past}"
    by_day: dict[str, list[dict]] = {}
    for d in sorted(groups):
        by_day.setdefault(DAY_LABELS.get(d, "Kasnije").lower(), []).extend(groups[d])

    def kinds(items):
        present = {o.get("kind") if o.get("kind") in KIND_LABEL else "RADOVI" for o in items}
        return ", ".join(KIND_LABEL[k].lower() for k in ("STRUJA", "VODA", "GREJANJE", "RADOVI")
                         if k in present)

    count = plural(total, "najava", "najave", "najava")
    if len(by_day) == 1 and "kasnije" not in by_day:
        day, items = next(iter(by_day.items()))
        return f"{label} {day}: {count} ({kinds(items)}) — ulice, vreme i link na zvaničnu objavu."
    parts = ", ".join(f"{day} {len(items)} ({kinds(items)})" for day, items in by_day.items())
    return f"{label}: {count} — {parts}. Ispod su ulice, vreme i link na zvaničnu objavu."


def faq_items(label: str, sources) -> list[tuple[str, str]]:
    """Questions people ask a search engine or a chatbot about this place, answered from what the
    page really does. Shown on the page and repeated as FAQPage data, which must match it."""
    names = _join([name for _, name, _ in sources], "i")
    return [
        (f"Gde da proverim da li će nestati {covered(sources, 'ili')}?",
         f"{label}: najave objavljuju {names}. Na ovoj stranici su sve na jednom mestu, sa "
         "ulicama, vremenom i linkom na zvaničnu objavu. Stranica se osvežava više puta dnevno."),
        ("Kako da dobijem obaveštenje za svoju adresu?",
         "Klikni „Prijavi adresu” na ovoj stranici, upiši ulicu i broj i izaberi Telegram ili "
         "e-mail. Kad se tvoja adresa nađe u najavi, stiže ti poruka. Besplatno je, a odjava je "
         "jednim klikom."),
        ("Koliko unapred se najavljuju isključenja?",
         "Planirani radovi se obično najavljuju dan ili više unapred. Kvarovi se objavljuju tek "
         "kad nastanu, pa ih ovde vidiš čim ih izvor objavi."),
        ("Da li je NajavaRadova.rs zvanični sajt?",
         "Ne. NajavaRadova.rs je nezavisan servis koji prikuplja javne objave ovih izvora. Uz "
         "svaku najavu je link na zvaničnu objavu, i ona uvek ima prednost."),
    ]


def _searched_kinds(sources) -> list[str]:
    """What a page covers, in the genitive and in the order people search it: power first
    ("nestanak struje" outnumbers every water query), then water, then heating."""
    return [KIND_GENITIVE[k] for k in ("STRUJA", "VODA", "GREJANJE") if any(s[0] == k for s in sources)]


def page_title(city: str, sources: list[tuple[str, str, str]], kvart: str | None = None) -> str:
    """The words people type, first - Google cuts a title at ~60 characters. Search Console
    (04.10.-31.10.2026) had "nestanak struje novi pazar" (363), "iskljucenje struje subotica
    danas" (188), "ledine bez struje" (95), while the titles started with the place and
    "najave isključenja", and Mirijevo III's ran to 101 characters.
    - a city: "Nestanak struje i vode Kraljevo danas — najave (Vodovod Kraljevo, EPS)". People
      also search by the utility's name ("vodovod krusevac iskljucenja"), so the sources stay -
      as sources, in brackets, never as if the page were theirs. Heating stays out of city
      titles: nobody searched a city for it.
    - a neighbourhood (`kvart`, with `city` its city): "Mirijevo III bez struje, vode ili
      grejanja? Najave danas — Beograd". Here heating is searched ("grejanje banovo brdo").
    Google shows the site's own name above the title, so the brand isn't repeated in it."""
    kinds = _searched_kinds(sources)
    if kvart:
        return f"{kvart} bez {_join(kinds, 'ili')}? Najave danas — {city}"
    local = ([name for kind, name, _ in sources if kind == "VODA"]
             or [name for kind, name, _ in sources if kind == "GREJANJE"])
    names = local[:1] + ["EPS"]
    return f"Nestanak {_join(kinds[:2], 'i')} {city} danas — najave ({', '.join(names)})"


def _last_day(history) -> str:
    """The most recent past outage's day, "30.10.", or "" when the history has none."""
    for r in (history[1] if history else []):
        try:
            return datetime.strptime(r.get("date", ""), "%Y-%m-%d").strftime("%d.%m.")
        except ValueError:
            continue
    return ""


DESC_ITEMS_CHARS = 75   # street names + day/kind labels; Google shows ~155 characters in all


def page_description(city: str, sources, days: dict[int, list[dict]],
                     history: tuple[dict[str, int], list[dict]] | None) -> str:
    """The snippet under the title: what is actually on, by street, so a search for a street
    or "bez vode" finds its own answer. No clock time: that changed every run, rewrote all 89
    pages every three hours, and told Google each one was new."""
    lead = f"Bez {covered(sources, 'ili')}? {city}"
    total = sum(len(v) for v in days.values())
    if not total:
        # Leads with what happened here, not "trenutno nema najavljenih isključenja": a searcher
        # read that as "nothing on this page" - Subotica, 106 power cuts in 30 days, had 0.7% of
        # its 1,076 impressions clicked (October 2026).
        n = sum(history[0].values()) if history else 0
        if not n:
            return (f"{city}: najave isključenja {covered(sources, 'i')} na jednom mestu - ulice, "
                    f"vreme i zvanična objava. Poruka za tvoju adresu dan ranije.")
        last = _last_day(history)
        had = [KIND_GENITIVE[k] for k in ("STRUJA", "VODA", "GREJANJE") if history[0].get(k)]
        return (f"{city}: {plural(n, 'isključenje', 'isključenja', 'isključenja')} "
                f"{_join(had, 'i') if had else covered(sources, 'i')} u poslednjih 30 dana{', poslednje ' + last if last else '.'} "
                f"Proveri svoju ulicu i dobij poruku dan ranije.")
    # Each notice by its first street, grouped as "danas, voda: Cerska, Ljubljanska".
    items = [((d, o.get("kind", "RADOVI")), re.sub(r"\s*\+\d+$", "", (o.get("street") or "").split(",")[0]))
             for d in sorted(days) for o in days[d]]
    groups: dict[tuple[int, str], list[str]] = {}
    shown, used = 0, 0
    for key, name in items:
        if shown and used + len(name) > DESC_ITEMS_CHARS:
            break
        shown += 1
        if name and name not in groups.get(key, []):
            groups.setdefault(key, []).append(name)
            used += len(name) + 2 + (0 if len(groups[key]) > 1 else 14)
    parts = "; ".join(f"{DAY_LABELS.get(d, 'Kasnije').lower()}, {KIND_LABEL.get(kind, 'Radovi').lower()}: "
                      f"{', '.join(names)}" for (d, kind), names in groups.items())
    more = f" i još {plural(total - shown, 'najava', 'najave', 'najava')}" if total > shown else ""
    return f"{lead} — {parts}{more}. Ulice, vreme i zvanična objava."


def dedupe_days(groups: dict[int, list[dict]]) -> dict[int, list[dict]]:
    """Works running for weeks are in the feed once per day shown (d = 0, 1, 2), with the same
    dates and hours each time; list them once, on the first day - the dates already say how
    long they run. Kraljevo's page showed Jug Bogdanove (08.09.–28.10.) under Danas, Sutra and
    Prekosutra."""
    seen, out = set(), {}
    for d in sorted(groups):
        for o in groups[d]:
            # A notice in several far-apart places is one entry per place (o["part"]).
            key = (o["id"], tuple(o.get("part") or ()), o.get("date"), o.get("time")) \
                if o.get("id") is not None else id(o)
            if key not in seen:
                seen.add(key)
                out.setdefault(d, []).append(o)
    return out


def history_for(city: str, slug: str, regions: dict) -> tuple[dict[str, int], list[dict]]:
    """(outages per kind, the most recent past ones) over the feed's history window, for a page:
    its own region, plus every gradska opština's on a grad page."""
    counts: dict[str, int] = {}
    recent: list[dict] = []
    for region, entry in regions.items():
        region = canonical_region(region)
        rs = slugify(region)
        if rs != slug and _MEMBER_TO_GRAD.get(rs) != city:
            continue
        for kind, n in (entry.get("counts") or {}).items():
            counts[kind] = counts.get(kind, 0) + n
        recent += [dict(r, region=region) for r in entry.get("recent") or []]
    recent.sort(key=lambda r: r.get("date", ""), reverse=True)
    return counts, recent[:RECENT_SHOWN]


# Neighbourhood pages, /grad/beograd/cerak/: Beograd, Novi Sad and Niš, one page per kvart the
# feed ties outages to (core/kvartovi.py tags each outage by where it is drawn; the kvartovi come
# from OpenStreetMap, spatial/kvartovi.json). A kvart gets a page once it has had
# MIN_KVART_OUTAGES in the past 30 days - a page with one old notice is the kind of thin page
# Google counts against a site - and keeps it while it has any. One that falls quiet stays online
# as noindex and leaves the sitemap.
KVARTOVI_PATH = os.path.join(BASE_DIR, "spatial", "kvartovi.json")
MIN_KVART_OUTAGES = 3
NEARBY_SHOWN = 12
_NOINDEX = '<meta name="robots" content="noindex">'


def kvart_names() -> dict[str, dict]:
    """id -> {"name", "city", "at": (lat, lng)}; empty when the file is missing."""
    try:
        with open(KVARTOVI_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for k in data.get("kvartovi", []):
        if k.get("pt"):
            at = tuple(k["pt"])
        else:
            ring = (k.get("poly") or [[]])[0]
            at = (sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)) if ring else None
        out[k["id"]] = {"name": k["name"], "city": k["city"], "at": at}
    return out


def published_kvartovi() -> dict[str, bool]:
    """id -> still indexed, for the kvart pages already online."""
    out = {}
    for city_slug in sorted(os.listdir(GRAD_DIR)) if os.path.isdir(GRAD_DIR) else []:
        city_dir = os.path.join(GRAD_DIR, city_slug)
        if not os.path.isdir(city_dir):
            continue
        for kv_slug in os.listdir(city_dir):
            path = os.path.join(city_dir, kv_slug, "index.html")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    out[f"{city_slug}/{kv_slug}"] = _NOINDEX not in fh.read()
    return out


def kvart_pages(outages: list[dict], hist: dict | None, names: dict[str, dict],
                published: dict[str, bool]) -> dict[str, dict]:
    """id -> {"name", "city", "groups", "history", "indexed"} for every kvart with a page: one
    that qualifies now, and one already online (rewritten, maybe as noindex)."""
    groups: dict[str, dict[int, list[dict]]] = {}
    for o in outages:
        for k in o.get("kv") or []:
            if k in names:
                groups.setdefault(k, {}).setdefault(int(o.get("d", 0)), []).append(o)
    counts_by = (hist or {}).get("kvartovi") if isinstance(hist, dict) else None
    recent_all = (hist or {}).get("kv_recent") or [] if isinstance(hist, dict) else []
    pages = {}
    for k, meta in names.items():
        entry = (counts_by or {}).get(k) or {}
        counts = entry.get("counts") or {}
        n = sum(counts.values())
        recent = [recent_all[i] for i in entry.get("recent") or [] if isinstance(i, int) and i < len(recent_all)]
        indexed = n >= MIN_KVART_OUTAGES or (published.get(k, False) and (n > 0 or k in groups))
        if not indexed and k not in published:
            continue
        pages[k] = {"name": meta["name"], "city": meta["city"], "groups": groups.get(k, {}),
                    "history": (counts, recent) if counts_by is not None else None, "indexed": indexed}
    return pages


def nearby(k: str, pages: dict[str, dict], names: dict[str, dict]) -> list[tuple[str, str]]:
    """The NEARBY_SHOWN closest indexed kvart pages of the same city, as (name, slug)."""
    here = names.get(k, {}).get("at")
    if not here:
        return []
    others = []
    for o, p in pages.items():
        at = names.get(o, {}).get("at")
        if o != k and p["indexed"] and p["city"] == pages[k]["city"] and at:
            others.append(((at[0] - here[0]) ** 2 + ((at[1] - here[1]) * 0.71) ** 2, p["name"], o))
    return [(name, slug) for _, name, slug in sorted(others)[:NEARBY_SHOWN]]


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


# "Najave isključenja vode i struje — Kraljevo"; pages from before 28.09.2026 read
# "Isključenja struje, vode i grejanja — Kraljevo".
_H1_RE = re.compile(r"<h1>(?:Najave i|I)sključenja [^<—]*— (.+?)</h1>")


def existing_page_cities() -> list[str]:
    """Names of the pages already published. A page whose city has no outage today is rewritten
    as "nothing announced", not left behind listing the last ones it ever had."""
    names = []
    if not os.path.isdir(GRAD_DIR):
        return names
    for slug in sorted(os.listdir(GRAD_DIR)):
        path = os.path.join(GRAD_DIR, slug, "index.html")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            m = _H1_RE.search(fh.read())
        if m:
            names.append(html.unescape(m.group(1)))
    return names


_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_feed(args) -> dict | None:
    if getattr(args, "from_file", None):
        with open(args.from_file, encoding="utf-8") as fh:
            return json.load(fh)
    if args.from_db:
        from scripts.build_web_data import build
        return build()
    import time as _t

    import requests
    # .strip(): a secret pasted with a trailing Enter arrived as ".../outages.json\n" -> 404.
    url = (args.from_feed or DEFAULT_FEED).strip()
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    # The Worker's workers.dev address serves only callers holding FEED_KEY (cf/worker.js).
    key = os.environ.get("FEED_KEY", "").strip()
    if key:
        headers["x-feed-key"] = key
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            _t.sleep(3 * (attempt + 1))
    # Cloudflare Bot Fight Mode answers datacenter IPs (GitHub runners) on api.najavaradova.rs
    # with a 403 - the workflow reads the Worker's workers.dev address (FEED_URL) for that
    # reason. A failed fetch fails the run: returning quietly here kept every city page frozen
    # at 09.09.2026 for two weeks behind a green check mark.
    print(f"⚠️  feed fetch failed after retries ({last}); pages left untouched")
    return None


def esc(v) -> str:
    return html.escape(str(v or ""), quote=True)


def map_link(ids) -> str:
    """The home page's map opened on these outages (focusLinked() in web/index.html). The ids go
    after '#', so to Google every such link is the home page, not a new URL per notice."""
    return "/#o=" + ",".join(str(i) for i in ids)


def _opstina_name(name: str) -> str:
    """"Palilula - Beograd" / "Palilula (Niš)" -> "Palilula": the opština as the signup form's
    place list writes it inside "Beograd (Palilula)"."""
    return re.sub(r"\s*(?:-\s*|\()(?:Beograd|Niš)\)?$", "", name).strip()


def signup_places(city: str, slug: str, parent: tuple[str, str] | None = None,
                  groups: dict[int, list[dict]] | None = None,
                  history: tuple[dict[str, int], list[dict]] | None = None) -> list[str]:
    """The place names the home page's signup form may know this page by, most specific first.
    Its list has Belgrade's and Niš's urban opštine as "Beograd (Zemun)", their outer ones and
    every other town by their own name. The form takes the first it finds; with none (the
    "Beograd" page itself) it fills in the last and lets the visitor pick from its suggestions.
    A neighbourhood is placed by the opština its outages are listed under."""
    grad, member = (parent[0], None) if parent else (_MEMBER_TO_GRAD.get(slug), city)
    if parent:
        seen = Counter(o.get("region") for d in (groups or {}) for o in groups[d] if o.get("region"))
        seen.update(r.get("region") for r in (history[1] if history else []) if r.get("region"))
        member = next((r for r, _ in seen.most_common() if _MEMBER_TO_GRAD.get(slugify(r)) == grad), None)
    names = []
    if grad and member:
        names += [f"{grad} ({_opstina_name(member)})", _opstina_name(member)]
    names.append(grad or city)
    return list(dict.fromkeys(names))


# The signup card comes after the list. On a long list (Beograd often has 40+ notices) it comes
# after the first few instead, so it isn't a long scroll away.
LONG_LIST = 8
ASK_AFTER = 5


def signup_href(places: list[str]) -> str:
    return "/?" + urlencode({"prijava": "1", "grad": "|".join(places)})


def signup_card(places: list[str], busy: bool = False) -> str:
    """The invitation, once per page (#prijava-ovde), plus the bar on phones (sticky_bar). Its
    button opens the signup form right here, with the place already set (NRSignup.open() in
    web/signup.js); the visitor adds the street. Without JavaScript the same link opens the form
    on the home page instead.
    It says what the visitor gets. The reason to want it - how often it happened here - is the
    past month's card right above it on a quiet page (month_card), so it isn't said again here:
    until 11.11.2026 a quiet page carried this box twice, a few lines apart, and "24 isključenja"
    three times, which read like spam. On a busy page it asks whether your street is next."""
    title = "Da li je tvoja ulica sledeća?" if busy else "Sledeći put saznaj dan ranije"
    return (f'<section class="ask" id="prijava-ovde">'
            f'<p class="t">{title}</p>'
            f'<p class="x">Javljamo samo kad je tvoja ulica i broj u najavi. Telegram ili e-mail, besplatno.</p>'
            f'<a class="btn" data-signup href="{esc(signup_href(places))}">🔔 Prijavi svoju adresu</a>'
            f'<p class="ok">Bez reklama · odjava jednim klikom</p></section>')


# The past month as a card: how many, of what kind, and - when the feed carries it - a strip of
# the 30 days with each day coloured by what was cut. On a quiet day this is the page's reason to
# sign up, so it comes before the invitation.
KIND_ORDER = ("STRUJA", "VODA", "GREJANJE", "RADOVI")
KIND_BIT = {"STRUJA": 1, "VODA": 2, "GREJANJE": 4, "RADOVI": 8}


def month_card(history, days: str | None) -> str:
    """`history` is (counts per kind, recent); `days` the feed's 31-character day string, oldest
    first, each a hex digit of KIND_BIT flags (build_web_data.history), or None.
    The number never stands alone: what it is made of ("struja 24"), how often that is ("oko 6
    nedeljno") and when it last happened go with it - "24" on its own said nothing (11.11.2026)."""
    counts, recent = history
    n = sum(counts.values())
    if not n:
        return ""
    kinds = [k for k in KIND_ORDER if counts.get(k)]
    last = _last_day(history)
    weekly = round(n * 7 / 30)
    rate = f" · oko {weekly} nedeljno" if weekly >= 1 else ""
    bar = '<div class="kb">' + "".join(
        f'<span class="{k.lower()}" style="flex:{counts[k]}"></span>' for k in kinds) + "</div>"
    strip = ""
    if days and re.fullmatch(r"[0-9a-f]{20,40}", days):
        cells = []
        for ch in days:
            bits = int(ch, 16)
            present = [k.lower() for k in KIND_ORDER if bits & KIND_BIT[k]]
            cells.append("<span>" + "".join(f'<i class="{k}"></i>' for k in present) + "</span>")
        start = (datetime.now(SERBIA_TZ).date() - timedelta(days=len(days) - 1)).strftime("%d.%m.")
        strip = (f'<div class="strip" role="img" aria-label="Dani sa isključenjima u poslednjih 30 dana">'
                 f'{"".join(cells)}</div><div class="ax"><span>{start}</span><span>danas</span></div>')
    legend = '<div class="lg">' + "".join(
        f'<span><i class="{k.lower()}"></i>{esc(KIND_LABEL[k].lower())} <b>{counts[k]}</b></span>'
        for k in kinds) + "</div>"
    return (f'<section class="month"><div class="top"><div><div class="big">{n}</div>'
            f'<div class="lab">{plural(n, "isključenje", "isključenja", "isključenja").split(" ", 1)[1]} '
            f'u poslednjih 30 dana{rate}</div></div>'
            f'<div class="lab">{"poslednje: " + esc(last) if last else ""}</div></div>'
            f'{bar}{legend}{strip}</section>')


# On a phone the invitation is a bar at the bottom of the screen, once the visitor has scrolled
# a little (the first screen is the outages themselves) and while no card is in view.
_STICKY_JS = """<script>
(function () {
  var bar = document.querySelector(".stick"), cards = document.querySelectorAll(".ask");
  if (!bar) return;
  var seen = new Set(), scrolled = false;
  function show() { bar.classList.toggle("off", !scrolled || seen.size > 0); }
  window.addEventListener("scroll", function () {
    var s = window.scrollY > 240;
    if (s !== scrolled) { scrolled = s; show(); }
  }, { passive: true });
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) seen.add(e.target); else seen.delete(e.target); });
      show();
    });
    cards.forEach(function (c) { io.observe(c); });
  }
})();
</script>"""


def sticky_bar(places: list[str]) -> str:
    """Hidden ("off") until the visitor scrolls a little: the first screen on a phone is for the
    outages. 92% of the search visitors are on phones and ~8% of them opened the form (Search
    Console and signup_events, October 2026), so the bar is brighter and slides in. The owner
    kept the wording: "Javi mi za moju ulicu" read less friendly than this (02.11.2026)."""
    return (f'<a class="stick off" data-signup href="{esc(signup_href(places))}">'
            '🔔 Prijavi adresu, besplatno</a>' + _STICKY_JS)


# The small map under the title: the page's outages one day at a time, drawn from the same shapes
# the home page's map uses (the feed's "paths" and "areas"), on the same basemap. Day tabs above
# it ("Danas 2 · Sutra 7"): the first version drew every day at once with nothing saying so, and
# its link opened the big map on today with the view on tomorrow's streets - empty (11.11.2026).
# Now the map and its link are always about the same day.
# It costs the first screen nothing: the map library (~1 MB) and the shapes (karta.json, next to
# the page) load only once the box scrolls near the screen, and the page is complete without them
# - the box is a link to the big map either way. 92% of the visitors come from a search on a
# phone, and page speed counts for ranking. It doesn't move or zoom: a finger scrolling the page
# must scroll the page.
MAP_FILE = "karta.json"
_MINIMAP_JS = """<script>
(function () {
  var box = document.querySelector(".mm");
  if (!box) return;
  var link = box.querySelector(".mapbtn"), legend = document.querySelector(".mlg");
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".mt button"));
  var day = +box.getAttribute("data-day"), draw = null;
  function names(kinds) {
    return kinds.split(",").filter(Boolean).map(function (k) {
      return '<span><i class="' + k + '"></i>' + k + "</span>";
    }).join("");
  }
  tabs.forEach(function (b) {
    b.addEventListener("click", function () {
      day = +b.getAttribute("data-d");
      tabs.forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      link.setAttribute("href", "/#o=" + b.getAttribute("data-ids"));
      if (legend) legend.innerHTML = names(b.getAttribute("data-kinds"));
      if (draw) draw();
    });
  });
  if (!("IntersectionObserver" in window) || !window.fetch || !window.Promise) return;
  function css(h) { var l = document.createElement("link"); l.rel = "stylesheet"; l.href = h; document.head.appendChild(l); }
  function js(s) {
    return new Promise(function (ok, no) {
      var e = document.createElement("script"); e.src = s; e.onload = ok; e.onerror = no; document.head.appendChild(e);
    });
  }
  var io = new IntersectionObserver(function (es) {
    if (!es.some(function (e) { return e.isIntersecting; })) return;
    io.disconnect();
    css("/vendor/leaflet/leaflet.css"); css("/vendor/maplibre-gl/maplibre-gl.css");
    Promise.all([
      fetch(box.getAttribute("data-src")).then(function (r) { return r.json(); }),
      js("/vendor/leaflet/leaflet.js").then(function () { return js("/vendor/maplibre-gl/maplibre-gl.js"); })
        .then(function () { return js("/vendor/maplibre-gl-leaflet/leaflet-maplibre-gl.js"); })
    ]).then(function (res) {
      var data = res[0];
      var map = L.map(box.querySelector(".mmc"), { zoomControl: false, dragging: false, touchZoom: false,
        doubleClickZoom: false, scrollWheelZoom: false, boxZoom: false, keyboard: false, tap: false });
      var dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
      if (L.maplibreGL) L.maplibreGL({
        style: "https://tiles.openfreemap.org/styles/" + (dark ? "dark" : "positron"),
        attributionControl: { customAttribution: '<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> ' +
          '<a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">&copy; OpenMapTiles</a> Data from ' +
          '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>' }
      }).addTo(map);
      var g = L.featureGroup().addTo(map);
      draw = function () {
        g.clearLayers();
        (data.o || []).forEach(function (o) {
          if (o.d !== day) return;
          (o.p || []).forEach(function (p) { L.polyline(p, { color: o.c, weight: 5, opacity: .95, interactive: false }).addTo(g); });
          (o.a || []).forEach(function (a) {
            if (a.hull && a.hull.length > 2) L.polygon(a.hull, { color: o.c, weight: 2, fillOpacity: .28, interactive: false }).addTo(g);
            var pt = a.dot || a.pin;
            if (pt) L.circleMarker(pt, { radius: 6, color: o.c, fillColor: o.c, fillOpacity: .9, weight: 2, interactive: false }).addTo(g);
          });
        });
        if (g.getLayers().length) map.fitBounds(g.getBounds(), { padding: [22, 22], maxZoom: 15 });
        else map.setView([44.2, 20.9], 7);
      };
      draw();
      box.classList.add("on");
    }).catch(function () { box.classList.add("off"); });
  }, { rootMargin: "250px" });
  io.observe(box);
})();
</script>"""


def map_data(groups: dict[int, list[dict]]) -> dict:
    """karta.json: the page's outages under the day they are listed under (dedupe_days) - colour,
    lines and areas."""
    seen, out = set(), []
    for d in sorted(groups):
        for o in groups[d]:
            key = (o.get("id"), tuple(o.get("part") or ()), d)
            if key in seen or not (o.get("paths") or o.get("areas")):
                continue
            seen.add(key)
            out.append({"d": int(d), "c": o.get("color") or "#6b7075",
                        "p": o.get("paths") or [], "a": o.get("areas") or []})
    return {"o": out}


def minimap(slug: str, groups: dict[int, list[dict]]) -> str:
    """The day tabs, the map box and its legend, for a page whose outages have shapes; "" for none."""
    per_day = []
    for d in sorted(groups):
        drawn = [o for o in groups[d] if str(o.get("id")).isdigit() and (o.get("paths") or o.get("areas"))]
        if drawn:
            ids = list(dict.fromkeys(str(o["id"]) for o in drawn))
            kinds = [k.lower() for k in KIND_ORDER if any(o.get("kind") == k for o in drawn)]
            # The tab counts what the list under that day heading counts, as the sentence above
            # does ("sutra 7"), not only the notices that have a shape to draw.
            per_day.append((d, ids, kinds, len(groups[d])))
    if not per_day:
        return ""
    first, first_ids, first_kinds, _ = per_day[0]
    tabs = "".join(
        f'<button type="button" data-d="{d}" data-ids="{",".join(ids)}" data-kinds="{",".join(kinds)}" '
        f'aria-pressed="{"true" if d == first else "false"}">{esc(DAY_LABELS.get(d, "Kasnije"))} <b>{n}</b></button>'
        for d, ids, kinds, n in per_day)
    legend = "".join(f'<span><i class="{k}"></i>{k}</span>' for k in first_kinds)
    return (f'<div class="mt">{tabs}</div>'
            f'<div class="mm" data-src="/grad/{esc(slug)}/{MAP_FILE}" data-day="{first}"><div class="mmc"></div>'
            f'<a class="mapbtn" href="{map_link(first_ids)}"><span>🗺️ Otvori veliku mapu</span></a></div>'
            f'<div class="lg mlg">{legend}</div>{_MINIMAP_JS}')


def _or_days(strings) -> str | None:
    """Several day strings (a grad's opštine) as one: a day has every kind any of them had."""
    strings = [s for s in strings if isinstance(s, str) and re.fullmatch(r"[0-9a-f]+", s)]
    if not strings or len({len(s) for s in strings}) != 1:
        return None
    out = []
    for i in range(len(strings[0])):
        bits = 0
        for s in strings:
            bits |= int(s[i], 16)
        out.append(format(bits, "x"))
    return "".join(out)


def days_for(city: str, slug: str, regions: dict) -> str | None:
    """The page's day string: its own region's, or on a grad page all its opštine's together."""
    picked = []
    for region, entry in regions.items():
        rs = slugify(canonical_region(region))
        if rs == slug or _MEMBER_TO_GRAD.get(rs) == city:
            picked.append((entry or {}).get("days"))
    return _or_days(picked)


def guide_links() -> str:
    from scripts.build_guides import GUIDES
    return " · ".join(f'<a href="/vodic/{slug}/">{esc(title)}</a>' for slug, title, *_ in GUIDES)


def page_html(city: str, slug: str, groups: dict[int, list[dict]], generated: str, others: list[tuple[str, str]],
              history: tuple[dict[str, int], list[dict]] | None = None, parent: tuple[str, str] | None = None,
              naselja: list[tuple[str, str, int]] | None = None, noindex: bool = False,
              days: str | None = None) -> str:
    """A city's page, or with `parent` (its city's name and slug) a neighbourhood's: `city` is then
    the kvart ("Cerak") and `slug` its path ("beograd/cerak"). `naselja` lists a city page's
    neighbourhood pages as (name, slug, notices now). `days` is the past month day by day, from
    the feed (month_card); the small map's data is written next to the page (map_data)."""
    groups = dedupe_days(groups)
    total = sum(len(v) for v in groups.values())
    # Serbian clock time for readers - the runner's UTC showed "19:07" at 21:07.
    when = (datetime.fromisoformat(generated.replace("Z", "+00:00")).astimezone(SERBIA_TZ)
            .strftime("%d.%m.%Y. u %H:%M")) if generated else ""
    sources = sources_for(*parent) if parent else sources_for(city, slug)
    label = f"{city}, {parent[0]}" if parent else city
    title = page_title(parent[0], sources, kvart=city) if parent else page_title(label, sources)
    h1 = f"Najave isključenja {covered(sources, 'i')} — {label}"
    desc = page_description(label, sources, groups, history)
    # A notice's own place is named unless it's this page's (or, on a kvart page, its city's).
    here = {slug, parent[1]} if parent else {slug}

    # Svi gradovi › Beograd › Zvezdara: a gradska opština's page leads up to its grad's, a kvart's
    # to its city's.
    grad = parent[0] if parent else _MEMBER_TO_GRAD.get(slug)
    trail = [("Svi gradovi", f"{SITE}/grad/")]
    if grad:
        trail.append((grad, f"{SITE}/grad/{parent[1] if parent else slugify(grad)}/"))
    trail.append((city, f"{SITE}/grad/{slug}/"))
    crumbs = " › ".join(f'<a href="{esc(url[len(SITE):])}">{esc(name)}</a>' for name, url in trail[:-1])
    schema_items = [{
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [{"@type": "ListItem", "position": i, "name": name, "item": url}
                            for i, (name, url) in enumerate(trail, 1)],
    }]
    for d, items in sorted(groups.items()):
        for o in items:
            streets = ", ".join(o.get("streets") or [o.get("street", "")])
            item = {
                "@context": "https://schema.org",
                "@type": "SpecialAnnouncement",
                "name": f"{KIND_LABEL.get(o.get('kind'), 'Radovi')} — {o.get('street', '')} ({label})",
                "text": f"{o.get('summary', '')} {streets}. {o.get('date', '')} {o.get('time', '')}".strip(),
                "category": SCHEMA_CATEGORY.get(o.get("kind"), ""),
                "datePosted": generated,
                "spatialCoverage": {"@type": "Place",
                                    "name": f"{o.get('street', '')}, {o.get('region') or city}"},
            }
            if o.get("sourceUrl"):
                item["url"] = o["sourceUrl"]
            schema_items.append(item)

    faq = faq_items(label, sources)
    schema_items.append({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": q,
                        "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq],
    })

    # The map is the product; a page that is only a list never shows it. A small map of today's
    # outages sits right under the title (minimap); tapping it opens the big map on them.
    ids = list(dict.fromkeys(str(o.get("id")) for d in sorted(groups) for o in groups[d]
                             if str(o.get("id")).isdigit()))
    places = signup_places(city, slug, parent, groups, history)
    card = signup_card(places, busy=bool(total))

    rows, shown = [], 0
    for d in sorted(groups):
        rows.append(f'<h2>{esc(DAY_LABELS.get(d, "Kasnije"))}</h2>')
        for o in groups[d]:
            streets = o.get("streets") or [o.get("street", "")]
            kind = o.get("kind", "RADOVI")
            on_map = (f'<a class="onmap" href="{map_link([o["id"]])}">📍 Na mapi</a>'
                      if str(o.get("id")).isdigit() else "")
            src = (f'<a href="{esc(o["sourceUrl"])}" rel="nofollow noopener" target="_blank">Zvanična objava</a>'
                   if o.get("sourceUrl") else "")
            links = " · ".join(link for link in (on_map, src) if link)
            links = f'<p class="l">{links}</p>' if links else ""
            # On a grad page, say which of its opštine ("Zvezdara · 25.09. · 08:30 - 14:00").
            where = o.get("region") if o.get("region") and slugify(o["region"]) not in here else ""
            rows.append(
                f'<article class="o {esc(kind.lower())}">'
                f'<div class="k">{esc(KIND_LABEL.get(kind, "Radovi"))}</div>'
                # A heading, not a paragraph: people search by street ("nestanak struje
                # Partizanska"), and search engines weigh headings when matching and quoting.
                f'<div class="b"><h3 class="s">{esc(", ".join(streets))}</h3>'
                f'<p class="m">{esc(where) + " · " if where else ""}'
                f'{esc(o.get("date",""))}{" · " + esc(o.get("time","")) if o.get("time") else ""}'
                f'{" · <b>u toku</b>" if o.get("live") else ""}</p>'
                f'<p class="d">{esc(o.get("summary",""))}</p>'
                f'{links}</div></article>'
            )
            shown += 1
            if total > LONG_LIST and shown == ASK_AFTER:
                rows.append(card)
    if total and total <= LONG_LIST:
        rows.append(card)
    if not total:
        rows.append(f'<p class="empty">Trenutno nema najavljenih isključenja za {"ovo naselje" if parent else "ovaj grad"}.</p>')

    # The past month, so a page on a quiet day is still worth reading and indexing. On a quiet
    # page it is the reason to sign up, so the invitation comes right after it. history is None
    # when the feed has no history block - that section is left out then.
    about = []
    if history is not None:
        counts, recent = history
        n = sum(counts.values())
        about.append("<h2>Poslednjih 30 dana</h2>")
        if n:
            about.append(month_card(history, days))
        else:
            about.append(f'<p class="p">{esc(label)} — u prethodnih 30 dana nismo zabeležili nijedno '
                         f'isključenje.</p>')
        if not total:
            about.append(card)
        items = []
        for r in recent:
            try:
                day = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d.%m.")
            except (KeyError, ValueError):
                continue
            where = r.get("region") if r.get("region") and slugify(r["region"]) not in here else ""
            items.append(f'<li><span class="hd">{day}</span><i class="kd {esc(str(r.get("kind", "RADOVI")).lower())}" '
                         f'title="{esc(KIND_LABEL.get(r.get("kind"), "Radovi"))}"></i>'
                         f'{esc(KIND_LABEL.get(r.get("kind"), "Radovi"))} · '
                         f'{esc(where) + " · " if where else ""}{esc(r.get("street", ""))}</li>')
        if items:
            about.append(f'<h2>Nedavna isključenja</h2><ul class="h">{"".join(items)}</ul>')
    elif not total:
        about.append(card)
    if naselja:
        about.append('<h2>Naselja</h2><p class="chips">' + "".join(
            f'<a href="/grad/{esc(s)}/">{esc(n)}' + (f' <b>{c}</b>' if c else "") + "</a>"
            for n, s, c in naselja) + "</p>")

    # Sources, how it works and the questions fold into rows that open on a tap: the text stays
    # in the page (search engines read it), the wall of it is gone.
    missing = [KIND_LABEL[k] for k in ("VODA", "GREJANJE") if all(s[0] != k for s in sources)]
    about.append(
        "<h2>O ovoj stranici</h2>"
        "<details><summary>Koje izvore pratimo</summary><ul class=\"h\">" + "".join(
            f'<li><b>{esc(KIND_LABEL[k])}</b> — <a href="{esc(url)}" rel="nofollow noopener" '
            f'target="_blank">{esc(name)}</a></li>' for k, name, url in sources) + "</ul>"
        + (f'<p class="p">{" i ".join(missing).capitalize()}: za ovo mesto još nemamo direktan izvor. '
           f'Kad ga dodamo, te najave će se pojaviti i ovde.</p>' if missing else "")
        + "</details>"
        "<details><summary>Kako funkcioniše</summary>"
        '<p class="p">Sajtove ovih izvora proveravamo automatski, tokom celog dana. Iz svake objave '
        "čitamo ulice, povezujemo ih sa adresnim registrom RGZ-a i prikazujemo na mapi. Kad se "
        "tvoja ulica nađe u najavi, stiže ti poruka na Telegram ili e-mail: za planirane radove "
        "obično dan ili više unapred, za kvarove čim ih izvor objavi.</p></details>")
    about.append("<h2>Česta pitanja</h2>" + "".join(
        f'<details><summary><h3 class="q">{esc(q)}</h3></summary><p class="p">{esc(a)}</p></details>'
        for q, a in faq))

    other_links = " · ".join(
        f'<a href="/grad/{s}/">{esc(c)}</a>' for c, s in others if s != slug
    )
    if parent:
        others_line = (f'<p>Obližnja naselja: {other_links + " · " if other_links else ""}'
                       f'<a href="/grad/{parent[1]}/">{esc(parent[0])}</a> · <a href="/grad/">svi gradovi</a></p>'
                       '<p>Granice naselja: © <a href="https://www.openstreetmap.org/copyright" rel="nofollow noopener" '
                       'target="_blank">OpenStreetMap</a> saradnici.</p>')
    else:
        others_line = f'<p>Drugi gradovi: {other_links} · <a href="/grad/">svi gradovi</a></p>'

    return f"""<!DOCTYPE html>
<html lang="sr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{SITE}/grad/{slug}/">{chr(10) + _NOINDEX if noindex else ""}
<link rel="icon" href="/icon-512.png" type="image/png" sizes="512x512">
<meta property="og:type" content="website">
<meta property="og:site_name" content="NajavaRadova.rs">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{SITE}/grad/{slug}/">
<meta property="og:image" content="{SITE}/icon-512.png">
<meta property="og:locale" content="sr_RS">
<script type="application/ld+json">
{json.dumps(schema_items, ensure_ascii=False)}
</script>
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#16171b; --mut:#5b5d66; --line:#e5e5ea;
    --voda:#2f80b8; --struja:#b07d29; --grejanje:#c0512e; --radovi:#6b7075; --card:#f6f7f9;
    --chip:rgba(255,255,255,.92); --ok:#23955f; --ok-soft:rgba(35,149,95,.16); --warn-soft:rgba(176,125,41,.18);
    --ask-line:rgba(31,111,235,.35); --ask-a:rgba(31,111,235,.09); --ask-b:rgba(31,111,235,.03); }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#101013; --fg:#e9e9ee; --mut:#9a9aa4; --line:#26262c;
    --card:#17171c; --chip:rgba(16,16,19,.88); --ok:#3fb27f; --ok-soft:rgba(63,178,127,.18);
    --warn-soft:rgba(209,154,59,.2); --ask-line:rgba(59,130,246,.45); --ask-a:rgba(31,111,235,.16);
    --ask-b:rgba(31,111,235,.05); --struja:#d19a3b; --voda:#3b93d1; --grejanje:#d9653d; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
    font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:44rem; margin:0 auto; padding:28px 20px 60px; }}
  a {{ color:#1f6feb; }}
  .brand {{ font-weight:700; letter-spacing:-.02em; text-decoration:none; color:var(--fg); }}
  .bc {{ color:var(--mut); font-size:.85rem; margin-top:6px; }}
  .bc a {{ color:var(--mut); }}
  h1 {{ font-size:1.6rem; line-height:1.25; letter-spacing:-.02em; margin:18px 0 6px; }}
  .upd {{ color:var(--mut); font-size:.85rem; margin:0 0 16px; }}
  h2 {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.09em; color:var(--mut);
    margin:26px 0 10px; }}
  .o {{ display:flex; gap:12px; padding:13px 0; border-top:1px solid var(--line); }}
  .o .k {{ flex:0 0 84px; font-size:.72rem; text-transform:uppercase; letter-spacing:.05em;
    font-weight:700; padding-top:2px; }}
  .o.voda .k {{ color:var(--voda); }} .o.struja .k {{ color:var(--struja); }}
  .o.grejanje .k {{ color:var(--grejanje); }} .o.radovi .k {{ color:var(--radovi); }}
  .o .s {{ font-size:1rem; line-height:inherit; font-weight:600; margin:0 0 3px; }}
  .o .m {{ color:var(--mut); font-size:.9rem; margin:0 0 4px; }}
  .o .d {{ color:var(--mut); font-size:.9rem; margin:0; }}
  .o .l {{ font-size:.9rem; margin:5px 0 0; }}
  .o .onmap {{ font-weight:600; text-decoration:none; }}
  .empty {{ color:var(--mut); border-top:1px solid var(--line); padding-top:16px; }}
  .p {{ margin:0 0 10px; }}
  .q {{ font-size:1rem; margin:16px 0 4px; }}
  .sum {{ margin:0 0 2px; }}
  ul.h {{ list-style:none; padding:0; margin:0 0 10px; }}
  ul.h li {{ padding:8px 0; border-top:1px solid var(--line); font-size:.95rem; }}
  ul.h .hd {{ color:var(--mut); font-variant-numeric:tabular-nums; margin-right:8px; }}
  .stat .sum {{ display:flex; gap:9px; align-items:baseline; font-weight:600; }}
  .stat .sum::before {{ content:""; flex:none; width:9px; height:9px; border-radius:50%;
    background:var(--ok); box-shadow:0 0 0 4px var(--ok-soft); transform:translateY(-1px); }}
  .stat.busy .sum::before {{ background:var(--struja); box-shadow:0 0 0 4px var(--warn-soft); }}
  .mt {{ display:flex; gap:6px; margin:10px 0 8px; flex-wrap:wrap; }}
  .mt button {{ font:inherit; font-size:.9rem; border:1px solid var(--line); background:transparent;
    color:var(--mut); border-radius:999px; padding:5px 13px; cursor:pointer; }}
  .mt button b {{ font-weight:700; margin-left:3px; }}
  .mt button[aria-pressed="true"] {{ background:var(--fg); color:var(--bg); border-color:var(--fg); }}
  .lg b {{ color:var(--fg); font-weight:700; margin-left:2px; }}
  .mm {{ position:relative; margin:4px 0 6px; border:1px solid var(--line); border-radius:14px;
    overflow:hidden; aspect-ratio:16/10; max-height:320px; background:var(--card); }}
  .mm .mmc {{ position:absolute; inset:0; }}
  .mm::before {{ content:"Mapa se učitava…"; position:absolute; inset:0; display:grid; place-items:center;
    color:var(--mut); font-size:.9rem; }}
  .mm.on::before {{ display:none; }}
  .mapbtn {{ position:absolute; inset:0; z-index:450; display:flex; align-items:flex-end;
    justify-content:flex-end; padding:10px; text-decoration:none; }}
  .mapbtn span {{ background:var(--chip); color:var(--fg); border:1px solid var(--line); border-radius:9px;
    padding:7px 12px; font-weight:600; font-size:.9rem; }}
  .mapbtn:hover span {{ border-color:var(--mut); }}
  .mm .leaflet-control-attribution {{ font-size:10px; }}
  .lg {{ display:flex; flex-wrap:wrap; gap:4px 14px; color:var(--mut); font-size:.82rem; margin:8px 0 0; }}
  .lg i, ul.h i.kd {{ display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; }}
  ul.h i.kd {{ margin-right:6px; vertical-align:0; }}
  i.struja {{ background:var(--struja); }} i.voda {{ background:var(--voda); }}
  i.grejanje {{ background:var(--grejanje); }} i.radovi {{ background:var(--radovi); }}
  .month {{ border:1px solid var(--line); background:var(--card); border-radius:14px; padding:14px 16px; }}
  .month .top {{ display:flex; justify-content:space-between; align-items:flex-end; gap:12px; }}
  .month .big {{ font-size:2rem; font-weight:800; letter-spacing:-.03em; line-height:1;
    font-variant-numeric:tabular-nums; }}
  .month .lab {{ color:var(--mut); font-size:.85rem; margin-top:4px; text-align:right; }}
  .month .top > div:first-child .lab {{ text-align:left; }}
  .kb {{ display:flex; height:8px; border-radius:4px; overflow:hidden; margin:12px 0 0; gap:2px; }}
  .kb span {{ min-width:4px; }}
  .kb .struja {{ background:var(--struja); }} .kb .voda {{ background:var(--voda); }}
  .kb .grejanje {{ background:var(--grejanje); }} .kb .radovi {{ background:var(--radovi); }}
  .strip {{ display:grid; grid-template-columns:repeat(31,1fr); gap:2px; margin:12px 0 4px; }}
  .strip span {{ display:flex; flex-direction:column; gap:1px; aspect-ratio:1/1.7; border-radius:2px;
    overflow:hidden; background:var(--line); }}
  .strip span i {{ flex:1; }}
  .ax {{ display:flex; justify-content:space-between; color:var(--mut); font-size:.75rem; }}
  .ask {{ margin:22px 0 8px; padding:16px; border:1px solid var(--ask-line); border-radius:14px;
    background:linear-gradient(180deg,var(--ask-a),var(--ask-b)); }}
  .ask .t {{ font-weight:800; font-size:1.1rem; letter-spacing:-.01em; margin:0; }}
  .ask .x {{ color:var(--mut); font-size:.92rem; margin:4px 0 12px; }}
  .ask .btn {{ display:block; text-align:center; padding:12px 16px; background:#1f6feb; color:#fff;
    border-radius:10px; text-decoration:none; font-weight:700; }}
  .ask .btn:hover {{ background:#1a5fd0; }}
  .ask .ok {{ color:var(--mut); font-size:.8rem; text-align:center; margin:8px 0 0; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:6px; margin:0; }}
  .chips a {{ border:1px solid var(--line); border-radius:999px; padding:4px 11px; text-decoration:none;
    color:var(--fg); font-size:.9rem; }}
  .chips a b {{ color:var(--struja); font-weight:700; margin-left:2px; }}
  details {{ border-top:1px solid var(--line); padding:12px 0; }}
  details:last-of-type {{ border-bottom:1px solid var(--line); }}
  summary {{ cursor:pointer; font-weight:600; list-style:none; display:flex; justify-content:space-between;
    gap:12px; }}
  summary::-webkit-details-marker {{ display:none; }}
  summary::after {{ content:"+"; color:var(--mut); font-weight:400; }}
  details[open] > summary::after {{ content:"–"; }}
  summary .q {{ font-size:1rem; margin:0; font-weight:600; }}
  details .p, details ul.h {{ margin:10px 0 0; color:var(--mut); }}
  .stick {{ display:none; }}
  @media (max-width:640px) {{
    main {{ padding-bottom:72px; }}
    .stick {{ display:block; position:fixed; left:12px; right:12px; bottom:12px; z-index:50;
      padding:14px 16px; background:linear-gradient(135deg,#1f6feb,#7c4dff); color:#fff;
      border-radius:14px; text-align:center; font-weight:700; font-size:1.05rem; line-height:1.3;
      text-decoration:none; box-shadow:0 8px 24px rgba(31,111,235,.45);
      transition:opacity .25s, transform .25s; }}
    .stick.off {{ opacity:0; transform:translateY(24px); pointer-events:none; }}
  }}
  footer {{ margin-top:40px; padding-top:20px; border-top:1px solid var(--line);
    color:var(--mut); font-size:.85rem; }}
  footer a {{ color:var(--mut); }}
</style>
<link rel="stylesheet" href="/signup.css">
</head>
<body>
<main>
  <a class="brand" href="/">NajavaRadova<span style="color:#1f6feb">.rs</span></a>
  <nav class="bc" aria-label="Putanja">{crumbs}</nav>
  <h1>{esc(h1)}</h1>
  <div class="stat {'busy' if total else 'calm'}"><p class="sum">{esc(summary_line(label, sources, groups, history))}</p></div>
  <p class="upd">Proveravamo zvanične objave na svakih 15–20 minuta.</p>
  {minimap(slug, groups)}
  {''.join(rows)}
  {''.join(about)}
  {sticky_bar(places)}
  <footer>
    {others_line}
    <p><a href="/">NajavaRadova.rs</a> — automatske najave isključenja za celu Srbiju. Podaci sa sajtova EPS-a, vodovoda i toplana. · <a href="/faq">Česta pitanja</a></p>
    <p>Vodiči: {guide_links()}</p>
    <p>Autor i održavanje: <a href="https://koldan.dev">Branimir Koldan</a></p>
  </footer>
</main>
<script src="/signup.js" defer></script>
{carry_script(slug.replace("/", "-"))}
</body>
</html>
"""


_CARRY_JS = """<script>
// Where the visitor came from (a site's name only) and this page's name, carried in the link
// to the signup form on the home page (?src=, ?from=) - read there once, with the signup, and
// never stored on the device. Set on a real click only, so crawlers see the plain links.
(function () {
  var page = "__PAGE__", src = "";
  try {
    var q = new URLSearchParams(location.search);
    var host = document.referrer ? new URL(document.referrer).hostname : "";
    src = q.get("src") || q.get("utm_source") || (host !== location.hostname ? host : "");
  } catch (err) {}
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href^='/']");
    if (!a) return;
    // "Prijavi adresu" opens the signup form right here (web/signup.js), with this page's place
    // names and where the visitor came from. Until that script has loaded, or if it fails, the
    // link still works: it opens the same form on the home page.
    if (a.hasAttribute("data-signup") && window.NRSignup) {
      try {
        var grad = new URL(a.getAttribute("href"), location.href).searchParams.get("grad") || "";
        window.NRSignup.open({ grad: grad.split("|"), source: { src: src, from: page } });
        e.preventDefault();
        return;
      } catch (err) {}
    }
    try {
      var u = new URL(a.getAttribute("href"), location.href);
      if (src) u.searchParams.set("src", src);
      if (u.pathname === "/" && page) u.searchParams.set("from", page);
      a.setAttribute("href", u.pathname + u.search + u.hash);
    } catch (err) {}
  });
})();
</script>"""


def carry_script(slug: str) -> str:
    return _CARRY_JS.replace("__PAGE__", slug)


def hub_html(index: list[tuple[str, str]], pages: dict[str, tuple[str, dict[int, list[dict]]]]) -> str:
    """/grad/: every city and opština page in one list, one click from the home page, with what
    each covers and how many notices it has now. Before it, Google had found 46 of the 92 URLs in
    the sitemap."""
    rows = []
    for city, slug in index:
        kinds = [KIND_LABEL[k].lower() for k in ("STRUJA", "VODA", "GREJANJE")
                 if any(s[0] == k for s in sources_for(city, slug))]
        n = sum(len(v) for v in dedupe_days(pages[slug][1]).values())
        grad = _MEMBER_TO_GRAD.get(slug)
        meta = ([grad] if grad else []) + [", ".join(kinds)] + ([plural(n, "najava", "najave", "najava")] if n else [])
        rows.append(f'<li><a href="/grad/{slug}/">{esc(city)}</a> <span>{esc(" · ".join(meta))}</span></li>')
    title = "Najave isključenja struje, vode i grejanja po gradovima Srbije"
    desc = (f"Bez struje, vode ili grejanja? Izaberi grad ili opštinu — {len(index)} mesta, sve najave "
            "sa ulicama, vremenom i linkom na zvaničnu objavu.")
    return f"""<!DOCTYPE html>
<html lang="sr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{SITE}/grad/">
<link rel="icon" href="/icon-512.png" type="image/png" sizes="512x512">
<meta property="og:type" content="website">
<meta property="og:site_name" content="NajavaRadova.rs">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{SITE}/grad/">
<meta property="og:image" content="{SITE}/icon-512.png">
<meta property="og:locale" content="sr_RS">
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#16171b; --mut:#5b5d66; --line:#e5e5ea; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#101013; --fg:#e9e9ee; --mut:#9a9aa4; --line:#26262c; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
    font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:44rem; margin:0 auto; padding:28px 20px 60px; }}
  a {{ color:#1f6feb; }}
  .brand {{ font-weight:700; letter-spacing:-.02em; text-decoration:none; color:var(--fg); }}
  h1 {{ font-size:1.6rem; line-height:1.25; letter-spacing:-.02em; margin:18px 0 6px; }}
  .lede {{ color:var(--mut); margin:0 0 22px; }}
  ul {{ list-style:none; padding:0; margin:0; }}
  li {{ padding:9px 0; border-top:1px solid var(--line); }}
  li a {{ font-weight:600; text-decoration:none; }}
  li span {{ color:var(--mut); font-size:.9rem; margin-left:6px; }}
</style>
</head>
<body>
<main>
  <a class="brand" href="/">NajavaRadova<span style="color:#1f6feb">.rs</span></a>
  <h1>Najave isključenja po gradovima</h1>
  <p class="lede">Struja za celu Srbiju, voda i grejanje gde pratimo lokalni vodovod i toplanu. Izaberi mesto za sve najave sa ulicama, vremenom i linkom na zvaničnu objavu.</p>
  <ul>{''.join(rows)}</ul>
</main>
{carry_script("svi-gradovi")}
</body>
</html>
"""


DANAS_STREETS = 4   # first streets shown per city on /danas/


MONTHS_GEN = ("januara", "februara", "marta", "aprila", "maja", "juna", "jula", "avgusta",
              "septembra", "oktobra", "novembra", "decembra")

DANAS_FAQ = [
    ("Gde da proverim da li će danas nestati struje ili vode?",
     "Na ovoj stranici su sve najave isključenja struje, vode i grejanja za danas i sutra iz cele "
     "Srbije, po gradovima, sa ulicama, vremenom i linkom na zvaničnu objavu. Struju najavljuje "
     "Elektrodistribucija Srbije (EPS), vodu lokalni vodovodi, a grejanje toplane."),
    ("Zašto je nestala struja ako nije bilo najave?",
     "Najave pokrivaju planirane radove. Kvarovi se ne najavljuju unapred: izvor ih objavi tek kad "
     "nastanu, a ovde se pojave čim ih objavi."),
    ("Kada će ponovo doći struja ili voda?",
     "Uz svaku najavu je period isključenja koji je naveo izvor. Struja i voda se obično vrate do "
     "kraja tog perioda, a često i ranije, ako se radovi završe pre roka."),
    ("Kako da dobijem obaveštenje samo za svoju ulicu?",
     "Klikni „Prijavi adresu”, upiši ulicu i broj i izaberi Telegram ili e-mail. Poruka stiže dan "
     "ranije, samo kad se najava tiče tvoje adrese. Besplatno je, a odjava je jednim klikom."),
]


def danas_html(index: list[tuple[str, str]], pages: dict[str, tuple[str, dict[int, list[dict]]]],
               generated: str = "") -> str:
    """/danas/: today's and tomorrow's notices for all of Serbia, one line per city, most first,
    each linking to its city page. Aimed at the biggest searches, which name no city:
    "najnovije vesti nestanak struje" (~7K a month), "nestanak struje" (~3.8K), "iskljucenja
    struje" (~3.4K) - Ahrefs, October 2026. A Beograd/Niš opština is counted on its grad's line
    only, so nothing is listed twice."""
    def kinds_of(items):
        c = Counter(o.get("kind") if o.get("kind") in KIND_LABEL else "RADOVI" for o in items)
        return ", ".join(f"{KIND_LABEL[k].lower()} {c[k]}" for k in ("STRUJA", "VODA", "GREJANJE", "RADOVI") if c[k])

    sections, totals, today_kinds = [], {}, Counter()
    for d, day in ((0, "Danas"), (1, "Sutra")):
        lines = []
        for city, slug in index:
            if _MEMBER_TO_GRAD.get(slug):
                continue
            items = dedupe_days(pages[slug][1]).get(d, [])
            if not items:
                continue
            if d == 0:
                today_kinds.update(o.get("kind") if o.get("kind") in KIND_LABEL else "RADOVI" for o in items)
            streets = list(dict.fromkeys(re.sub(r"\s*\+\d+$", "", (o.get("street") or "").split(",")[0])
                                         for o in items if o.get("street")))
            more = f" i još {len(streets) - DANAS_STREETS}" if len(streets) > DANAS_STREETS else ""
            lines.append((len(items), city,
                          f'<li><a href="/grad/{slug}/">{esc(city)}</a> <span>{esc(kinds_of(items))}</span>'
                          f'<p>{esc(", ".join(streets[:DANAS_STREETS]) + more)}</p></li>'))
        lines.sort(key=lambda x: (-x[0], x[1].lower()))
        totals[day] = (sum(n for n, _, _ in lines), [c for _, c, _ in lines])
        body = "".join(li for _, _, li in lines) or "<li><span>Nema najava za ovaj dan.</span></li>"
        sections.append(f'<h2>{day} <span>{esc(plural(totals[day][0], "najava", "najave", "najava"))}'
                        f' u {len(lines)} mesta</span></h2><ul>{body}</ul>')

    n, cities = totals["Danas"]
    # The page's answer in one sentence, first thing under the heading - what AI answers and
    # Google's snippets quote for "nestanak struje danas": the day, how many, where most.
    now = (datetime.fromisoformat(generated.replace("Z", "+00:00")).astimezone(SERBIA_TZ)
           if generated else datetime.now(SERBIA_TZ))
    day_words = f"{now.day}. {MONTHS_GEN[now.month - 1]} {now.year}. godine"
    if n:
        what = ", ".join(f"{KIND_LABEL[k].lower()} {today_kinds[k]}"
                         for k in ("STRUJA", "VODA", "GREJANJE", "RADOVI") if today_kinds[k])
        most = cities[:2]
        # "najava", not "isključenje": works on a street are not a cut (summary_line).
        answer = (f"Danas, {day_words}, u Srbiji: {plural(n, 'najava', 'najave', 'najava')} ({what}) u "
                  f"{plural(len(cities), 'mestu', 'mesta', 'mesta')}"
                  + (f" (najviše: {', '.join(most)})" if len(cities) > 1 else f": {cities[0]}") + ".")
    else:
        answer = f"Danas, {day_words}, u Srbiji nema najavljenih isključenja struje, vode ni grejanja."
    updated = now.strftime("%d.%m.%Y. u %H:%M")
    faq = "".join(f'<h3>{esc(q)}</h3><p class="lede">{esc(a)}</p>' for q, a in DANAS_FAQ)
    faq_schema = json.dumps({
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                       for q, a in DANAS_FAQ]}, ensure_ascii=False)
    title = "Nestanak struje danas — sve najave isključenja struje i vode u Srbiji"
    desc = (f"Danas u Srbiji: {plural(n, 'najava', 'najave', 'najava')} isključenja struje i vode"
            + (f" — {', '.join(cities[:4])}" if cities else "")
            + ". Ulice i vreme za svaki grad, iz zvaničnih objava.")
    return f"""<!DOCTYPE html>
<html lang="sr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{SITE}/danas/">
<link rel="icon" href="/icon-512.png" type="image/png" sizes="512x512">
<meta property="og:type" content="website">
<meta property="og:site_name" content="NajavaRadova.rs">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{SITE}/danas/">
<meta property="og:image" content="{SITE}/icon-512.png">
<meta property="og:locale" content="sr_RS">
<script type="application/ld+json">{faq_schema}</script>
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#16171b; --mut:#5b5d66; --line:#e5e5ea; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#101013; --fg:#e9e9ee; --mut:#9a9aa4; --line:#26262c; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
    font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:44rem; margin:0 auto; padding:28px 20px 60px; }}
  a {{ color:#1f6feb; }}
  .brand {{ font-weight:700; letter-spacing:-.02em; text-decoration:none; color:var(--fg); }}
  h1 {{ font-size:1.6rem; line-height:1.25; letter-spacing:-.02em; margin:18px 0 6px; }}
  h2 {{ font-size:1.2rem; margin:26px 0 6px; }}
  h3 {{ font-size:1rem; margin:16px 0 4px; }}
  .answer {{ font-size:1.05rem; margin:0 0 6px; }}
  .upd {{ color:var(--mut); font-size:.85rem; margin:0 0 12px; }}
  h2 span {{ color:var(--mut); font-weight:400; font-size:.95rem; margin-left:6px; }}
  .lede {{ color:var(--mut); margin:0 0 10px; }}
  ul {{ list-style:none; padding:0; margin:0; }}
  li {{ padding:9px 0; border-top:1px solid var(--line); }}
  li a {{ font-weight:600; text-decoration:none; }}
  li span {{ color:var(--mut); font-size:.9rem; margin-left:6px; }}
  li p {{ margin:2px 0 0; color:var(--mut); font-size:.9rem; overflow-wrap:anywhere; }}
  .ask {{ margin:22px 0; padding:14px 16px; border:1px solid var(--line); border-radius:12px; }}
  .ask p {{ margin:0 0 8px; }}
  .btn {{ display:inline-block; padding:8px 14px; border-radius:8px; background:#1f6feb; color:#fff;
    text-decoration:none; font-weight:600; }}
</style>
</head>
<body>
<main>
  <a class="brand" href="/">NajavaRadova<span style="color:#1f6feb">.rs</span></a>
  <h1>Nestanak struje i vode danas u Srbiji</h1>
  <p class="answer">{esc(answer)}</p>
  <p class="upd">Ažurirano: {esc(updated)} · proveravamo zvanične objave na svakih 15–20 minuta.</p>
  <p class="lede">Sve najave isključenja struje, vode i grejanja za danas i sutra, po gradovima. Klikni grad za ulice, vreme i link na zvaničnu objavu, ili <a href="/">pogledaj na mapi</a>.</p>
  <div class="ask"><p>🔔 Ne moraš da proveravaš svaki dan: javimo ti dan ranije, samo kad se najava tiče tvoje ulice i broja.</p>
    <a class="btn" data-signup href="/?prijava=1">Prijavi adresu, besplatno</a></div>
  {''.join(sections)}
  <h2>Česta pitanja</h2>
  {faq}
  <p class="lede" style="margin-top:22px"><a href="/grad/">Svi gradovi →</a></p>
</main>
{carry_script("danas")}
</body>
</html>
"""


def previous_lastmods(path: str) -> dict[str, str]:
    """<loc> -> <lastmod> from the sitemap already published."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return dict(re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", fh.read()))


def sitemap_xml(slugs: list[str], lastmod: dict[str, str] | None = None) -> str:
    """lastmod only where it is known: a city page's is when its content last changed. Every URL
    used to say "today" on every run, the privacy page included - a date Google learns to ignore.
    Google ignores changefreq and priority, so they're left out."""
    lastmod = lastmod or {}
    from scripts.build_guides import GUIDES
    urls = [f"{SITE}/", f"{SITE}/faq", f"{SITE}/upravnici/", f"{SITE}/privacy", f"{SITE}/terms", f"{SITE}/grad/", f"{SITE}/danas/"]
    urls += [f"{SITE}/vodic/{g[0]}/" for g in GUIDES]
    urls += [f"{SITE}/grad/{s}/" for s in slugs]
    body = "\n".join(
        f"  <url>\n    <loc>{u}</loc>\n"
        + (f"    <lastmod>{lastmod[u]}</lastmod>\n" if u in lastmod else "")
        + "  </url>"
        for u in urls
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}\n</urlset>\n'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-feed", nargs="?", const=DEFAULT_FEED, default=None,
                    help=f"fetch the feed (default {DEFAULT_FEED})")
    ap.add_argument("--from-db", action="store_true", help="build from the local DB instead")
    ap.add_argument("--from-file", help="build from a saved feed (outages.json)")
    args = ap.parse_args()
    if not args.from_db and not args.from_file and args.from_feed is None:
        args.from_feed = DEFAULT_FEED

    feed = load_feed(args)
    if feed is None:
        return 1  # feed unreachable - fail the run so it's seen (see load_feed)
    outages = feed.get("outages", [])
    hist = feed.get("history")
    regions = hist.get("regions") if isinstance(hist, dict) else None
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")

    pages = pages_for(outages, existing_page_cities())
    index = sorted(((name, slug) for slug, (name, _) in pages.items()), key=lambda cs: cs[0].lower())
    names = kvart_names()
    published = published_kvartovi()
    # A feed without kvart data (the VM not yet on the version that tags them) says nothing about
    # neighbourhoods: their pages are left as they are, not turned noindex.
    has_kv = isinstance(hist, dict) and isinstance(hist.get("kvartovi"), dict)
    kvartovi = kvart_pages(outages, hist, names, published) if has_kv else {}
    naselja: dict[str, list[tuple[str, str, int]]] = {}
    for k, p in sorted(kvartovi.items(), key=lambda kp: kp[1]["name"].lower()):
        if p["indexed"]:
            now = sum(len(v) for v in dedupe_days(p["groups"]).values())
            naselja.setdefault(p["city"], []).append((p["name"], k, now))

    os.makedirs(GRAD_DIR, exist_ok=True)
    sitemap_path = os.path.join(WEB_DIR, "sitemap.xml")
    previous = previous_lastmods(sitemap_path)
    lastmod: dict[str, str] = {}
    written = []

    def publish(path: str, url: str, new: str) -> bool:
        old = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                old = fh.read()
        # Ignore the timestamp line when deciding "changed" so a quiet cycle is a no-op commit.
        changed = _strip_ts(old) != _strip_ts(new)
        if changed:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
        lastmod[url] = generated if changed else previous.get(url, generated)
        return changed

    def write_map(out_dir: str, groups) -> None:
        """karta.json for the small map, or none when the page has nothing to draw today."""
        path = os.path.join(out_dir, MAP_FILE)
        data = map_data(dedupe_days(groups))
        if data["o"]:
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if old != body:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
        elif os.path.exists(path):
            os.remove(path)

    kv_hist = hist.get("kvartovi") if isinstance(hist, dict) else None
    for city, slug in index:
        groups = pages[slug][1]
        out_dir = os.path.join(GRAD_DIR, slug)
        os.makedirs(out_dir, exist_ok=True)
        history = history_for(city, slug, regions) if isinstance(regions, dict) else None
        days = days_for(city, slug, regions) if isinstance(regions, dict) else None
        write_map(out_dir, groups)
        if publish(os.path.join(out_dir, "index.html"), f"{SITE}/grad/{slug}/",
                   page_html(city, slug, groups, generated, index, history, naselja=naselja.get(city),
                             days=days)):
            written.append(slug)
    for k, p in sorted(kvartovi.items()):
        out_dir = os.path.join(GRAD_DIR, *k.split("/"))
        os.makedirs(out_dir, exist_ok=True)
        write_map(out_dir, p["groups"])
        days = _or_days([((kv_hist or {}).get(k) or {}).get("days")])
        if publish(os.path.join(out_dir, "index.html"), f"{SITE}/grad/{k}/",
                   page_html(p["name"], k, p["groups"], generated, nearby(k, kvartovi, names), p["history"],
                             parent=(p["city"], slugify(p["city"])), noindex=not p["indexed"], days=days)):
            written.append(k)
    if publish(os.path.join(GRAD_DIR, "index.html"), f"{SITE}/grad/", hub_html(index, pages)):
        written.append("(svi gradovi)")
    danas_dir = os.path.join(WEB_DIR, "danas")
    os.makedirs(danas_dir, exist_ok=True)
    if publish(os.path.join(danas_dir, "index.html"), f"{SITE}/danas/", danas_html(index, pages, generated)):
        written.append("(danas)")

    kv_indexed = (sorted(k for k, p in kvartovi.items() if p["indexed"]) if has_kv
                  else sorted(k for k, indexed in published.items() if indexed))
    with open(sitemap_path, "w", encoding="utf-8") as fh:
        fh.write(sitemap_xml([s for _, s in index] + kv_indexed, lastmod))

    print(f"{len(index)} city pages, {len(kv_indexed)} neighbourhood pages "
          f"(+{len(kvartovi) - len(kv_indexed)} noindex); {len(written)} changed: "
          f"{', '.join(written[:40]) or 'none'}{' …' if len(written) > 40 else ''}")
    print(f"sitemap.xml: {len(index) + len(kv_indexed) + 5} urls")
    return 0


def _strip_ts(s: str) -> str:
    s = re.sub(r'Ažurirano:?[^<]*', 'Ažurirano:', s)
    s = re.sub(r'"datePosted":\s*"[^"]*"', '"datePosted":""', s)
    s = re.sub(r'<lastmod>[^<]*</lastmod>', '<lastmod></lastmod>', s)
    return s


if __name__ == "__main__":
    raise SystemExit(main())
