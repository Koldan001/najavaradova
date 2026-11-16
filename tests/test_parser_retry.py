"""parser.parse_announcement against an overloaded Gemini (24 Sep 2026: flash-lite answering
"503 high demand", flash taking ~10 min per call): a 503 moves down the cascade after
TRANSIENT_TRIES_PER_MODEL tries, a timeout moves on at once, and only the last model gets the
full backoff."""
import os
import sys
import types

import httpx
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import parser as p

UNAVAILABLE = "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is experiencing high demand'}}"
GONE = ("404 NOT_FOUND. {'error': {'code': 404, 'message': 'This model models/gemini-2.5-flash-lite "
        "is no longer available to new users.', 'status': 'NOT_FOUND'}}")


@pytest.fixture()
def gemini(monkeypatch):
    """script: model -> list of outcomes, one per call (an Exception is raised, "ok" answers);
    the last outcome repeats."""
    script, calls, sleeps = {}, [], []
    answer = p.OutageAlert(city="X", outage_date="01.01.2026", message_template="m",
                           affected_locations=[]).model_dump_json()

    def generate_content(model, contents, config):
        calls.append(model)
        outcomes = script.get(model, ["ok"])
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return types.SimpleNamespace(text=answer)

    monkeypatch.setattr(p, "client", types.SimpleNamespace(
        models=types.SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr(p, "wait_for_slot", lambda *a: None)
    monkeypatch.setattr(p.time, "sleep", sleeps.append)
    monkeypatch.setattr(p.config, "MODEL_NAME", "lite")
    monkeypatch.setattr(p.config, "GEMINI_MODEL_CASCADE", ["lite", "lite2", "flash"])
    return script, calls, sleeps


def test_503_moves_to_the_next_model_after_two_tries(gemini):
    script, calls, sleeps = gemini
    script["lite"] = [Exception(UNAVAILABLE)]
    assert p.parse_announcement("x") is not None
    assert calls == ["lite", "lite", "lite2"]
    assert sleeps == [6]


def test_timeout_moves_on_at_once(gemini):
    script, calls, sleeps = gemini
    script["lite"] = [httpx.ReadTimeout("The read operation timed out")]
    assert p.parse_announcement("x") is not None
    assert calls == ["lite", "lite2"] and sleeps == []


def test_timeout_on_an_explicit_model_gives_up(gemini):
    """matchmaker's one-model retry: no second 60 s wait on a model that just timed out."""
    script, calls, _ = gemini
    script["flash"] = [httpx.ReadTimeout("timed out")]
    assert p.parse_announcement("x", model="flash") is None
    assert calls == ["flash"]


def test_last_model_still_gets_the_full_backoff(gemini):
    script, calls, sleeps = gemini
    for m in ("lite", "lite2", "flash"):
        script[m] = [Exception(UNAVAILABLE)]
    assert p.parse_announcement("x") is None
    assert calls == ["lite"] * 2 + ["lite2"] * 2 + ["flash"] * 4
    assert sleeps == [6, 6, 6, 12, 18, 24]


def test_retired_model_is_skipped_not_fatal(gemini):
    """30 Sep 2026: the backup was busy, the next model answered 404 "no longer available to new
    users", and the parse gave up with a working model still untried."""
    script, calls, sleeps = gemini
    script["lite"] = [Exception(UNAVAILABLE)]
    script["lite2"] = [Exception(GONE)]
    assert p.parse_announcement("x") is not None
    assert calls == ["lite", "lite", "lite2", "flash"]
    assert sleeps == [6]


def test_retired_explicit_model_gives_up_at_once(gemini):
    """matchmaker's one-model retry: a 404 is not worth backing off on."""
    script, calls, sleeps = gemini
    script["flash"] = [Exception(GONE)]
    assert p.parse_announcement("x", model="flash") is None
    assert calls == ["flash"] and sleeps == []


@pytest.mark.skipif("GEMINI_MODEL_CASCADE" in os.environ, reason="cascade overridden by the environment")
def test_default_cascade_has_no_retired_models():
    cascade = p.config.GEMINI_MODEL_CASCADE
    assert "gemini-2.5-flash-lite" not in cascade and "gemini-3-flash" not in cascade
    assert cascade[0] == "gemini-3.5-flash-lite" and cascade[-1] == "gemini-3.5-flash"


def test_client_has_a_timeout():
    assert p.GEMINI_TIMEOUT_MS == p.config.GEMINI_TIMEOUT_S * 1000 > 0
