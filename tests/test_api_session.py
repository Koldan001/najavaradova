"""GET /api/session tells the marketing site and city pages (web/signup.js) whether the visitor is
logged in - always with a 200, so a logged-out visitor's page view logs no console error - and
carries nothing but that yes/no."""
import os
import sys
import threading
from http.server import HTTPServer

import pytest
import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")


@pytest.fixture()
def server(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "session.db"))
    database.init_db()
    from scripts.local_web_server import Handler
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Handler, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_logged_out_is_200_not_401(server):
    _, base = server
    r = requests.get(base + "/api/session", timeout=5)
    assert r.status_code == 200
    assert r.json() == {"logged_in": False}
    assert r.headers["Cache-Control"] == "no-store"


def test_logged_in_says_so_and_nothing_else(server, monkeypatch):
    handler, base = server
    monkeypatch.setattr(handler, "_current_subscriber_id", lambda self: 7)
    r = requests.get(base + "/api/session", timeout=5)
    assert r.status_code == 200
    assert r.json() == {"logged_in": True}


def test_main_site_may_read_it_with_credentials(server):
    _, base = server
    r = requests.get(base + "/api/session", headers={"Origin": "https://najavaradova.rs"}, timeout=5)
    assert r.headers.get("Access-Control-Allow-Origin") == "https://najavaradova.rs"
    assert r.headers.get("Access-Control-Allow-Credentials") == "true"
