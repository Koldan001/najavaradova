"""Tell the IndexNow search engines (Bing, and through it ChatGPT search and Copilot; also Yandex,
Seznam, Naver) which city pages just changed, so they recrawl them within minutes instead of
whenever they next pass by. Run by .github/workflows/city-pages.yml after it pushes, with the
changed files as arguments:

    python scripts/indexnow.py web/grad/novi-sad/index.html web/grad/beograd/altina/index.html

IndexNow proves the site is ours by fetching the key file web/<KEY>.txt, so the key is public by
design, not a secret. Only URLs the sitemap lists are sent: a noindex neighbourhood page is not
something to ask a search engine to crawl.
"""
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
SITE = "https://najavaradova.rs"
HOST = "najavaradova.rs"
KEY = "72525f877c4899c76409e74685574e2b"
ENDPOINT = "https://api.indexnow.org/indexnow"


def sitemap_urls(path: str = os.path.join(WEB_DIR, "sitemap.xml")) -> set[str]:
    with open(path, encoding="utf-8") as fh:
        return set(re.findall(r"<loc>([^<]+)</loc>", fh.read()))


def page_url(path: str) -> str | None:
    """web/grad/beograd/altina/index.html -> https://najavaradova.rs/grad/beograd/altina/"""
    path = path.replace("\\", "/")
    m = re.fullmatch(r"(?:.*/)?web/(grad/(?:.+/)?|danas/)index\.html", path)
    return f"{SITE}/{m.group(1)}" if m else None


def changed_urls(paths: list[str], listed: set[str]) -> list[str]:
    urls = (page_url(p) for p in paths)
    return list(dict.fromkeys(u for u in urls if u and u in listed))


def payload(urls: list[str]) -> dict:
    return {"host": HOST, "key": KEY, "keyLocation": f"{SITE}/{KEY}.txt", "urlList": urls}


def main(argv: list[str]) -> int:
    urls = changed_urls(argv, sitemap_urls())
    if not urls:
        print("IndexNow: no listed page changed, nothing to send.")
        return 0
    import requests
    try:
        r = requests.post(ENDPOINT, json=payload(urls[:10000]), timeout=30)
        # 200 and 202 both mean accepted; anything else is worth seeing in the run log.
        print(f"IndexNow: sent {len(urls)} URL(s), answer {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        print(f"::warning::IndexNow ping failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
