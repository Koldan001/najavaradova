"""Delivery timing for every test (core/delivery): no settle pause, and the clock at midday so
Telegram's quiet hours never hold a message just because the suite ran at night. Tests of those
rules set their own values."""
import os
import sys
from datetime import date, datetime, time

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture(autouse=True)
def _daytime_delivery(monkeypatch):
    from core import delivery
    monkeypatch.setattr(delivery, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(delivery, "serbia_now", lambda: datetime.combine(date.today(), time(12, 0)))


@pytest.fixture(autouse=True)
def _no_second_opinion_calls(monkeypatch):
    """core/send_check.py never reaches Gemini or Telegram from a test: the second opinion
    answers nothing and the operator message goes nowhere. Tests of it set their own."""
    os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")
    import parser
    from core import send_check
    monkeypatch.setattr(parser, "check_place_scope", lambda raw_text, places: None)
    monkeypatch.setattr(send_check, "_operator_post", lambda method, payload: None)
