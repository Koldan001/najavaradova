"""An address typed without picking a city suggestion still lands on the map. 19.10.2026: a
Čukarica street was saved with city "Belgrade" - RGZ knows Belgrade only by district, so the
server called it uncovered, the address was never mapped and its owner would get no alerts.
Also: an address that can't be mapped tells the operator instead of staying silent."""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

from spatial.spatial_engine import RGZ_DB_PATH  # noqa: E402

requires_rgz = pytest.mark.skipif(not os.path.exists(RGZ_DB_PATH), reason="data/rgz.db not present")


@requires_rgz
@pytest.mark.parametrize("typed", ["Belgrade", "Beograd", "beograd", "БЕОГРАД", "Belgrad"])
def test_bare_or_english_belgrade_finds_the_district_by_street(typed):
    from scripts.local_web_server import coverage_tier, resolve_city, street_exists
    city = resolve_city(typed, "Arhimandrita Gerasima Zelića")
    assert city == "Beograd (Čukarica)"
    assert coverage_tier(city) == "full"
    assert street_exists(city, "Arhimandrita Gerasima Zelića")


@requires_rgz
def test_a_city_rgz_knows_is_left_alone():
    from scripts.local_web_server import resolve_city
    assert resolve_city("Novi Sad", "Futoška") == "Novi Sad"
    assert resolve_city("Beograd (Zemun)", "Glavna") == "Beograd (Zemun)"


@requires_rgz
@pytest.mark.parametrize("typed", ["Beograd (novi Beograd)", "beograd (novi beograd)", "Novi Beograd"])
def test_a_known_place_is_saved_in_its_own_spelling(typed):
    # The dashboard sent "Beograd (novi Beograd)" for months and it was saved as typed.
    from scripts.local_web_server import resolve_city
    assert resolve_city(typed, "Omladinskih Brigada") == "Beograd (Novi Beograd)"


@requires_rgz
def test_unknown_street_or_place_comes_back_unchanged():
    from scripts.local_web_server import resolve_city
    # No district has this street: nothing to narrow to, the coverage check decides as before.
    assert resolve_city("Beograd", "Ulica Koja Ne Postoji") == "Beograd"
    assert resolve_city("Nepostojeće Mesto", "Glavna") == "Nepostojeće Mesto"


def test_unmapped_address_alerts_the_operator(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "unmapped.db"))
    database.init_db()
    conn = database.get_connection()
    conn.execute("INSERT INTO targets (id, city, ulica, broj, is_active, email) "
                 "VALUES (94, 'Nigde', 'Nepostojeća', '12', 1, 'a@example.com')")
    conn.commit()
    conn.close()

    from scripts import map_targets
    monkeypatch.setattr(map_targets.spatial_engine, "get_street_hexes", lambda *a, **k: [])
    monkeypatch.setattr(map_targets.spatial_engine, "get_settlement_polygon_hexes", lambda *a, **k: [])
    sent = []
    import core.alerts as alerts
    monkeypatch.setattr(alerts, "send_operator_alert",
                        lambda key, text, cooldown_s=3600: sent.append((key, text, cooldown_s)) or True)

    map_targets.map_unmapped_targets()

    assert len(sent) == 1
    key, text, cooldown = sent[0]
    assert key == "unmapped_target:94"
    assert "Nigde, Nepostojeća 12" in text and "a@example.com" not in text
    assert cooldown == 7 * 24 * 3600
