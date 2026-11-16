"""The model eval's scoring (core/model_eval.py): what counts as the same place, and how places,
details and fields are scored. No model is called."""
import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.model_eval import norm, score, total


def _reading(*locs, date="12.11.2026", event="WATER"):
    return {"event_type": event, "outage_date": date, "end_date": None, "road_closure_only": False,
            "affected_locations": [{"location_type": "STREET", "parity": "ALL", "time_window": "08:00 - 14:00",
                                    **l} for l in locs]}


def test_script_case_and_punctuation_do_not_count():
    assert norm("Ул. ФУТОШКА") == norm("futoška") == "futoska"
    assert norm("В.Р.ПУТНИКА") == norm("V. R. Putnika")


def test_a_perfect_reading():
    exp = _reading({"main_street": "ФУТОШКА", "number_range": "20-40", "parity": "PARNA"})
    r = score(exp, json.loads(json.dumps(exp)))
    assert (r["places_found"], r["segments_exact"], r["missed"], r["extra"]) == (1, 1, [], [])
    assert all(r["fields"].values())


def test_a_misspelled_street_is_wrong_and_flagged_as_spelling():
    """The real 'ЦРОВА' case: one dropped letter fails the street lookup like a missing street."""
    r = score(_reading({"main_street": "ЦЕРОВА"}), _reading({"main_street": "ЦРОВА"}))
    assert r["places_found"] == 0 and r["missed"] == ["cerova"] and r["extra"] == ["crova"]
    assert r["spelling"] == [("cerova", "crova")]


def test_right_street_wrong_range_counts_the_place_not_the_segment():
    r = score(_reading({"main_street": "ФУТОШКА", "number_range": "20-40"}),
              _reading({"main_street": "ФУТОШКА", "number_range": "20-42"}))
    assert r["places_found"] == 1 and r["segments_exact"] == 0


def test_house_letters_count_with_or_without_a_space():
    """'22 А' (Cyrillic, spaced) = '22a': the letter is kept, not dropped to a bare 22."""
    exp = _reading({"main_street": "ВАСЕ СТАЈИЋА", "explicit_numbers": "22, 22А, 22Б"})
    got = _reading({"main_street": "ВАСЕ СТАЈИЋА", "explicit_numbers": "22, 22 А, 22 Б"})
    assert score(exp, got)["segments_exact"] == 1
    missing_b = _reading({"main_street": "ВАСЕ СТАЈИЋА", "explicit_numbers": "22, 22 А"})
    assert score(exp, missing_b)["segments_exact"] == 0
    # a lowercase word after a number is not its letter
    words = _reading({"main_street": "ВАСЕ СТАЈИЋА", "explicit_numbers": "22 и 24"})
    assert score(_reading({"main_street": "ВАСЕ СТАЈИЋА", "explicit_numbers": "22, 24"}), words)["segments_exact"] == 1


def test_a_wrong_surrounding_streets_flag_is_a_wrong_segment():
    """'у околним улицама: А, Б, В' names them all - flagging okolne would alert unnamed streets too."""
    exp = _reading({"main_street": "ПУШКИНОВА"})
    r = score(exp, _reading({"main_street": "ПУШКИНОВА", "includes_surrounding_streets": True}))
    assert r["places_found"] == 1 and r["segments_exact"] == 0


def test_date_formats_are_the_same_date_and_a_wrong_day_is_wrong():
    assert score(_reading(date="12.11.2026"), _reading(date="2026-11-12"))["fields"]["outage_date"]
    r = score(_reading(date="12.11.2026"), _reading(date="13.11.2026"))
    assert not r["fields"]["outage_date"] and r["field_values"]["outage_date"] == ("12.11.2026", "13.11.2026")


def test_no_answer_misses_everything():
    r = score(_reading({"main_street": "ФУТОШКА"}), None)
    assert r["no_answer"] and r["missed"] == ["futoska"]


def test_totals_weigh_every_place():
    a = score(_reading({"main_street": "А"}, {"main_street": "Б"}, {"main_street": "В"}),
              _reading({"main_street": "А"}, {"main_street": "Б"}, {"main_street": "Г"}))
    b = score(_reading({"main_street": "Д"}), _reading({"main_street": "Д"}))
    t = total({1: a, 2: b})
    assert t["place_recall"] == 3 / 4 and t["place_precision"] == 3 / 4
    assert t["perfect_notices"] == 1


def test_settlements_and_streets_are_different_places():
    exp = {"affected_locations": [{"location_type": "ENTIRE_SETTLEMENT", "settlement": "БЕГЕЧ"}]}
    got = {"affected_locations": [{"location_type": "STREET", "main_street": "БЕГЕЧКА"}]}
    assert score(exp, got)["places_found"] == 0
