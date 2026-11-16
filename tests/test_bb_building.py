"""A street a notice gives only as "бб" (bez broja) is one building on it, not the street.
#6654 (nstoplana, 02.11.2026) listed "Корнелија Станковића бб (ЗГОП)"; the parser dropped the
"бб" and a building manager at another number on Kornelija Stankovića got the heating cut.
No data files needed, so it runs in CI."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

from core import matchmaker as mm  # noqa: E402
from parser import OutageAlert  # noqa: E402

TEXT = ("У уторак, 3. новембра 2026. године, због радова на изградњи вреловодног прикључка за објекат "
        "у Улици Стевана Дороњског бб (угао са Индустријском улицом), биће прекинута испорука за "
        "објекте у следећим улицама: Булевар Јаше Томића 14-18; Јована Храниловића 51-67; Стевана "
        "Дороњског 2-6; Корнелија Станковића бб (ЗГОП).")


def _alert(*locations):
    return OutageAlert.model_validate({
        "event_type": "grejanje", "city": "НОВИ САД", "outage_date": "03.11.2026",
        "message_template": "Prekid grejanja u ulici ***.", "affected_locations": list(locations),
        "excluded_locations": []})


def _loc(street, numbers=None, rng=None, landmark=None):
    return {"location_type": "STREET", "main_street": street, "explicit_numbers": numbers,
            "number_range": rng, "landmark": landmark, "time_window": "07:00 - 19:00"}


def test_a_bb_street_is_one_building_not_the_street():
    alert = _alert(_loc("БУЛЕВАР ЈАШЕ ТОМИЋА", rng="14-18"), _loc("СТЕВАНА ДОРОЊСКОГ", rng="2-6"),
                   _loc("КОРНЕЛИЈА СТАНКОВИЋА", landmark="ЗГОП"))
    assert mm._drop_bb_streets(alert, TEXT) == ["КОРНЕЛИЈА СТАНКОВИЋА бб (ЗГОП)"]
    bb = alert.affected_locations[2]
    assert (bb.location_type, bb.main_street, bb.landmark) == ("LANDMARK", None, "КОРНЕЛИЈА СТАНКОВИЋА бб (ЗГОП)")
    # the numbered lines are untouched - Stevana Dorońskog is also named "бб" in the reason, but
    # this line has its numbers
    assert [l.main_street for l in alert.affected_locations[:2]] == ["БУЛЕВАР ЈАШЕ ТОМИЋА", "СТЕВАНА ДОРОЊСКОГ"]
    assert mm._BB_LABEL.search(bb.landmark)


def test_a_street_also_named_without_bb_stays_the_street():
    text = "Прекид за улице: Корнелија Станковића; Корнелија Станковића бб."
    alert = _alert(_loc("КОРНЕЛИЈА СТАНКОВИЋА"))
    assert mm._drop_bb_streets(alert, text) == []
    assert alert.affected_locations[0].main_street == "КОРНЕЛИЈА СТАНКОВИЋА"


def test_spellings_of_bb():
    for written in ("б.б.", "ББ", "bb", "b.b", "без броја"):
        alert = _alert(_loc("ГЛАВНА"))
        assert mm._drop_bb_streets(alert, f"Прекид: Главна {written}, и Јужна 3.") == [f"ГЛАВНА бб"], written
    # "ББ" must stand alone, and a whole street stays whole
    alert = _alert(_loc("ГЛАВНА"))
    assert mm._drop_bb_streets(alert, "Прекид: Главна, Јужна 3.") == []
