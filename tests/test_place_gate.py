"""A typed place that names one we have streets for is resolved or must be picked from the list -
never saved as "uncovered". 27.10.2026: "Beograd, Grocka" + Vojvode Stepe Stepanovića 9K was
called uncovered, its owner ticked "razumem", and the address (really Kaluđerica, opština Grocka)
never reached the map. Runs on a small fake registry, so it also runs in CI (no data/rgz.db)."""
import os
import sqlite3
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

from scripts import local_web_server as lws  # noqa: E402

# (street id, name, naselje, opština, house numbers)
STREETS = [
    ("1", "VOJVODE STEPE STEPANOVIĆA", "KALUĐERICA", "GROCKA", ["9", "9К"]),
    ("2", "VOJVODE STEPE STEPANOVIĆA", "BOLEČ", "GROCKA", ["9"]),
    ("3", "BULEVAR OSLOBOĐENJA", "GROCKA", "GROCKA", ["1"]),
    ("4", "GLAVNA", "BEOGRAD (ZEMUN)", "ZEMUN", ["1"]),
    ("5", "ARHIMANDRITA GERASIMA ZELIĆA", "BEOGRAD (ČUKARICA)", "ČUKARICA", ["5"]),
    ("6", "FUTOŠKA", "NOVI SAD", "NOVI SAD", ["10"]),
    ("7", "GLAVNA", "JABUKA", "PANČEVO", ["1"]),
    ("8", "CARIGRADSKA", "BEOGRAD (PALILULA)", "PALILULA (BEOGRAD)", ["1"]),
    ("9", "CARIGRADSKA", "NIŠ (PALILULA)", "PALILULA (NIŠ)", ["1"]),
]


@pytest.fixture(autouse=True)
def fake_rgz(tmp_path, monkeypatch):
    db = tmp_path / "rgz.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rgz_streets (ulica_maticni_broj TEXT PRIMARY KEY, name TEXT, name_lat TEXT, "
                 "naselje TEXT, naselje_lat TEXT, opstina TEXT, opstina_lat TEXT, geometry_json TEXT)")
    conn.execute("CREATE TABLE rgz_housenumbers (kucni_broj_id TEXT PRIMARY KEY, ulica_maticni_broj TEXT, "
                 "kucni_broj TEXT, lat REAL, lon REAL, naselje_lat TEXT, opstina_lat TEXT, is_active INTEGER)")
    for sid, name, naselje, opstina, numbers in STREETS:
        conn.execute("INSERT INTO rgz_streets (ulica_maticni_broj, name_lat, naselje_lat, opstina_lat) "
                     "VALUES (?, ?, ?, ?)", (sid, name, naselje, opstina))
        for n in numbers:
            conn.execute("INSERT INTO rgz_housenumbers (kucni_broj_id, ulica_maticni_broj, kucni_broj) "
                         "VALUES (?, ?, ?)", (f"{sid}-{n}", sid, n))
    conn.commit()
    conn.close()
    monkeypatch.setattr(lws, "RGZ_DB_PATH", str(db))
    for cache in ("_naselje_cache", "_naselje_opstina_cache", "_uncovered_opstine_cache"):
        monkeypatch.setattr(lws, cache, None)


@pytest.mark.parametrize("city, broj", [
    ("Beograd, Grocka", "9K"),          # the real signup
    ("Grocka", "9K"),                   # the opština alone: the street is not in Grocka itself
    ("Beograd", "9K"),                  # Kaluđerica is in Belgrade's outer opština Grocka
    ("Grocka - Kaluđerica", "9"),       # named directly: wins over Boleč, which also has 9
    ("Kaluđerica Grocka", "9"),         # no separator at all
    ("Kaluđerica, Grocka", "9"),
])
def test_named_place_is_resolved_to_the_rgz_place(city, broj):
    assert lws.check_address(city, "vojvode Stepe Stepanovica", broj, True) == \
        ("Kaluđerica", "Vojvode Stepe Stepanovića", None)


def test_a_place_named_with_another_opstina_is_not_moved_there():
    # Jabuka (Pančevo) has a Glavna, but "Jabuka, Grocka" means a Jabuka in Grocka.
    assert lws.check_address("Jabuka, Grocka", "Glavna", "1", True)[2]["code"] == "PICK_PLACE"
    assert lws.check_address("Jabuka", "Glavna", "1", False) == ("Jabuka", "Glavna", None)


def test_house_number_narrows_and_never_guesses():
    # 9 is on the street in Kaluđerica and Boleč, and the text names neither: pick from the list.
    city, ulica, err = lws.check_address("Beograd, Grocka", "Vojvode Stepe Stepanovića", "9", True)
    assert err["code"] == "PICK_PLACE" and city is None


@pytest.mark.parametrize("city", ["Beograd, Grocka", "Kaluđerica Grocka", "Grocka - Beograd",
                                  "Beograd Zemunn, Grocka", "Palilula, Beograd"])
def test_ticking_uncovered_cannot_save_a_place_we_cover(city):
    # Whatever the client sends - ack_uncovered=True included - a covered place's address with a
    # street we can't place is refused, not saved where no alert reaches it.
    city_out, _, err = lws.check_address(city, "Nepostojeća", "1", True)
    assert city_out is None and err["code"] == "PICK_PLACE"


def test_really_uncovered_place_still_needs_the_ack_and_then_saves():
    assert lws.check_address("Prijepolje", "Glavna", "1", False)[2]["code"] == "UNCOVERED"
    assert lws.check_address("Prijepolje", "Glavna", "1", True) == ("Prijepolje", "Glavna", None)
    # A village sharing a covered one's name, with its uncovered opština written out.
    assert lws.check_address("Jabuka, Prijepolje", "Glavna", "1", True) == ("Jabuka, Prijepolje", "Glavna", None)


def test_known_places_are_left_alone():
    assert lws.check_address("Novi Sad", "Futoška", "10", False) == ("Novi Sad", "Futoška", None)
    assert lws.check_address("Beograd (Zemun)", "Glavna", "1", False) == ("Beograd (Zemun)", "Glavna", None)
    assert lws.check_address("Belgrade", "Arhimandrita Gerasima Zelića", "5", False) == \
        ("Beograd (Čukarica)", "Arhimandrita Gerasima Zelića", None)
    # A covered place with a street it doesn't have is still "pick the street", not uncovered.
    assert lws.check_address("Novi Sad", "Nepostojeća", "1", True)[2]["error"].startswith("Ulica nije prepoznata")


def test_saved_address_shows_the_place_and_says_when_it_moved():
    typed = {"city": "Beograd, Grocka", "ulica": "vojvode Stepe Stepanovica"}
    city, ulica, _ = lws.check_address(typed["city"], typed["ulica"], "9K", True)
    assert lws.saved_address(typed, city, ulica, "9K") == {
        "label": "Vojvode Stepe Stepanovića 9K, Kaluđerica (opština Grocka)", "changed": True}
    # Picked from the suggestions: nothing moved, no opština repeated after a same-named place.
    assert lws.saved_address({"city": "Novi Sad", "ulica": "Futoška"}, "Novi Sad", "Futoška", "10") == {
        "label": "Futoška 10, Novi Sad", "changed": False}


def test_cities_list_carries_each_place_opstina():
    out = {}
    handler = lws.Handler.__new__(lws.Handler)
    handler._json = lambda status, body: out.update(body)
    handler._api_cities()
    places = dict(zip(out["cities"], out["opstine"]))
    assert places["KALUĐERICA"] == "GROCKA" and places["BEOGRAD (ZEMUN)"] == "ZEMUN"
    assert "Grocka" not in out["nostreet"] and "Palilula" not in out["nostreet"]
    assert "Prijepolje" in out["nostreet"]


def test_bulk_loader_uses_the_same_gate(monkeypatch, capsys):
    import scripts.bulk_register as bulk
    assert bulk.check_address is lws.check_address


@pytest.mark.parametrize("typed, saved", [
    ("SELEVAC", "Selevac"),
    ("KNEZA MILOSA", "Kneza Milosa"),
    ("kneza miloša", "Kneza Miloša"),
    ("ulica 1. maja", "Ulica 1. Maja"),
    ("Kneza MILOSA", "Kneza MILOSA"),   # mixed case is the person's own spelling
    ("dr Ivana Ribara", "dr Ivana Ribara"),
])
def test_a_hand_typed_uncovered_address_is_saved_in_normal_case(typed, saved):
    """01.11.2026: "SELEVAC, KNEZA MILOSA 47" went into the signup e-mail and dashboard in caps."""
    assert lws.normal_case(typed) == saved


def test_the_gate_saves_an_uncovered_address_in_normal_case():
    assert lws.check_address("PRIJEPOLJE", "GLAVNA", "1", True) == ("Prijepolje", "Glavna", None)
