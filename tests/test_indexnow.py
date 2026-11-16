"""The IndexNow ping after each city-pages run: only pages the sitemap lists, and the key file
the search engines check is really published."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scripts import indexnow


def test_changed_files_become_listed_page_urls():
    listed = {"https://najavaradova.rs/grad/novi-sad/", "https://najavaradova.rs/grad/beograd/altina/",
              "https://najavaradova.rs/grad/"}
    paths = ["web/grad/novi-sad/index.html", "web/grad/beograd/altina/index.html",
             "web/grad/index.html",
             "web/grad/beograd/neko-naselje/index.html",   # a noindex page: not in the sitemap
             "web/sitemap.xml"]
    assert indexnow.changed_urls(paths, listed) == [
        "https://najavaradova.rs/grad/novi-sad/", "https://najavaradova.rs/grad/beograd/altina/",
        "https://najavaradova.rs/grad/"]


def test_nothing_listed_sends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(indexnow, "sitemap_urls", lambda: set())
    assert indexnow.main(["web/grad/novi-sad/index.html"]) == 0
    assert "nothing to send" in capsys.readouterr().out


def test_key_file_is_published_with_the_key():
    path = os.path.join(indexnow.WEB_DIR, f"{indexnow.KEY}.txt")
    with open(path, encoding="utf-8") as fh:
        assert fh.read().strip() == indexnow.KEY
    p = indexnow.payload(["https://najavaradova.rs/grad/novi-sad/"])
    assert p["keyLocation"] == f"https://najavaradova.rs/{indexnow.KEY}.txt"
    assert p["host"] == "najavaradova.rs"


def test_danas_page_is_pinged_too():
    listed = {"https://najavaradova.rs/danas/"}
    assert indexnow.changed_urls(["web/danas/index.html"], listed) == ["https://najavaradova.rs/danas/"]
