"""The daily budget for the stronger Gemini model (core/api_budget.py): every request counts,
retries included, and the day turns at midnight Pacific time, when Google resets its quota."""
import datetime
import os
import sys
from zoneinfo import ZoneInfo

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def budget(tmp_path, monkeypatch):
    import core.api_budget as api_budget
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "budget.db"))
    database.init_db()
    return api_budget


def test_stops_at_the_limit(budget):
    assert [budget.try_consume_budget("m:retry", 2) for _ in range(3)] == [True, True, False]
    assert budget.used_today("m:retry") == 2
    assert budget.remaining("m:retry", 2) == 0


def test_shares_are_counted_apart(budget):
    budget.try_consume_budget("m:retry", 10)
    assert budget.remaining("m:check", 5) == 5
    assert budget.remaining("m:retry", 10) == 9


def test_day_is_googles_pacific_day(budget):
    assert budget._today() == datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def test_every_request_counts_retries_included(budget, monkeypatch):
    """A model answering 503 every time: a budget of 3 allows 3 requests, not 1 job of 4 tries."""
    import parser
    sent = []

    class Models:
        def generate_content(self, **kw):
            sent.append(kw["model"])
            raise Exception("503 UNAVAILABLE")

    class Client:
        models = Models()

    monkeypatch.setattr(parser, "client", Client())
    monkeypatch.setattr(parser, "wait_for_slot", lambda *a: None)
    monkeypatch.setattr(parser.time, "sleep", lambda s: None)

    out = parser.parse_announcement("notice", max_retries=4, model="m", budget=("m:retry", 3))
    assert out is None
    assert len(sent) == 3
    assert budget.used_today("m:retry") == 3
