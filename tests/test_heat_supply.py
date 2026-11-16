"""A toplana notice names what people lose. Beogradske elektrane's "prekid u isporuci toplotne
energije za pripremu potrošne tople vode" (#3465, Toplana Cerak's remont, 30.09.2026) is hot water
only. The alert said "Planirani prekid u isporuci toplotne energije (grejanje i topla voda)" and the
map card said "Bez grejanja u navedenom periodu.", in September, before the heating season."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

from core.filters import heat_supply_cut, heat_works_only
from core.matchmaker import _render_message
from parser import OutageAlert, StreetSegment
from scripts.build_web_data import summarize

CERAK = ("Beograd. Trenutni prekid u isporuci toplotne energije.\n"
         "Izveštaj o trenutnom prekidu u isporuci toplotne energije za pripremu potrošne tople vode\n"
         "- PORUČNIKA SPASIĆA I MAŠERE 4 - radovi Gradskog stambenog\n"
         "- CELO GREJNO PODRUČJE TOPLANE \"CERAK\" - remontni radovi koji će se obavljati do "
         "četvrtka, 01. oktobra.")


def test_what_the_notice_says_is_cut():
    assert heat_supply_cut(CERAK) == "HOT_WATER"                    # "grejno područje" is a place
    assert heat_supply_cut("Prekid isporuke toplotne energije za grejanje i pripremu potrošne tople vode") == "BOTH"
    assert heat_supply_cut("Због хаварије без грејања остају станови у улици Змај Јовиној") == "HEATING"
    assert heat_supply_cut("Planirani prekid u isporuci toplotne energije") is None      # doesn't say
    assert heat_supply_cut("Radovi neće uticati na isporuku tople vode") is None         # nothing cut
    assert heat_supply_cut("Radovi pre početka sezone grejanja: bez tople vode") == "HOT_WATER"


# Novi Sad's word order (#3394, 29.09.2026): the card said "Moguć prekid" for a hot water cut.
NS_HOT_WATER = ("У уторак, 29. септембра 2026. године, од 10.00 сати, због радова на санацији хаварије на "
                "вреловодној мрежи, биће прекинута испорука топлотне енергије за припрему топле потрошне "
                "воде за објекте у следећим улицама: – Душана Даниловића")
# Beogradske elektrane's daily list (#3459): works, nothing said to be cut.
PIPE_WORKS = ("Beograd, opština Voždovac. Planirani radovi na toplovodnoj mreži (30.09.2026).\n"
              "- Radovi na izgradnji toplovodne mreže u Mitrovdanskoj i Ulici braće Jerković.\n"
              "- Radovi u okviru postavljanja optičkih kablova u Vojvode Stepe kod broja 244")
# Niš toplana (#2724): heating named as a service, traffic stopped, the page footer says "prekidima".
NIS_WORKS = ("Obaveštavamo vas da će JKP „Gradska toplana“ Niš, dana 20.07.2026. početi sa izvođenjem "
             "radova na magistralnom toplovodu. Radovi se izvode u cilju sprečavanja curenja na "
             "distributivnom sistemu daljinskog grejanja i pružanja kvalitetnije usluge grejanja. Biće "
             "potpuna obustava saobraćaja za motorna vozila. — Obaveštenja o radovima i prekidima u snabdevanju")


def test_novi_sad_word_order_is_hot_water():
    assert heat_supply_cut(NS_HOT_WATER) == "HOT_WATER"
    assert not heat_works_only(NS_HOT_WATER)


def test_works_that_cut_nothing_take_nothing():
    for text in (PIPE_WORKS, NIS_WORKS):
        assert heat_supply_cut(text) is None
        assert heat_works_only(text)
    assert not heat_works_only(CERAK)                                             # a cut, with works
    assert not heat_works_only("Planirani prekid u isporuci toplotne energije")   # a cut, no works
    assert not heat_works_only("Radovi neće uticati na isporuku tople vode")      # says nothing is cut


def _alert(template, **kwargs):
    defaults = dict(city="БЕОГРАД", outage_date="30.09.2026", message_template=template, affected_locations=[])
    defaults.update(kwargs)
    return OutageAlert(**defaults)


def _street(**kwargs):
    return StreetSegment(**{"location_type": "STREET", "main_street": "ЗАХУМСКА", "time_window": "08:00 - 14:00",
                            **kwargs})


def test_a_template_in_the_utilitys_words_is_rebuilt_to_say_what_is_lost():
    alert = _alert("Planirano isključenje toplotne energije u ulici *** dana 30.09.2026. u periodu 08:00 - 14:00.")
    msg = _render_message(alert, _street(), "Захумска", category="HEATING", heat_supply="HOT_WATER")
    assert msg == "Bez tople vode u ulici Zahumska dana 30.09.2026 u periodu 08:00 - 14:00."


def test_a_template_that_already_says_it_is_kept():
    template = "Prekid u isporuci tople vode u ulici *** dana 30.09.2026. u periodu 08:00 - 14:00."
    msg = _render_message(_alert(template), _street(), "Захумска", category="HEATING", heat_supply="HOT_WATER")
    assert msg == "Prekid u isporuci tople vode u ulici Zahumska dana 30.09.2026. u periodu 08:00 - 14:00."


def test_a_template_claiming_heating_for_a_hot_water_cut_is_rebuilt():
    alert = _alert("Bez grejanja i tople vode u ulici *** dana 30.09.2026.")
    msg = _render_message(alert, _street(), "Захумска", category="HEATING", heat_supply="HOT_WATER")
    assert msg.startswith("Bez tople vode u ulici Zahumska")


def test_the_cerak_notice_reads_as_hot_water_over_its_days():
    cerak = StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement="ЦЕРАК", time_window="")
    alert = _alert("Planirano isključenje toplotne energije u ulici *** dana 30.09.2026. u periodu 07:00 - 19:00.",
                   end_date="01.10.2026", affected_locations=[cerak])
    msg = _render_message(alert, cerak, "Репишка", category="HEATING", heat_supply=heat_supply_cut(CERAK))
    assert msg == "Bez tople vode u naselju Cerak od 30.09. do 01.10.2026."


def test_without_a_supply_the_neutral_wording_stays():
    alert = _alert("")
    msg = _render_message(alert, _street(time_window=""), "Захумска", category="HEATING")
    assert msg.startswith("Planirani prekid u isporuci toplotne energije (grejanje i topla voda)")


def test_the_map_card_says_hot_water_once():
    _, effects = summarize("GREJANJE", "isključenje toplotne energije", CERAK)
    assert effects[0] == "Bez tople vode u navedenom periodu."
    assert "Bez grejanja u navedenom periodu." not in effects and "Bez tople vode." not in effects
    _, effects = summarize("GREJANJE", None, "Hitna intervencija, bez grejanja do 15 časova.")
    assert effects[0] == "Bez grejanja u navedenom periodu."


def test_the_map_card_claims_no_cut_the_notice_doesnt_state():
    # #3459: construction of the network in two streets, nothing said about supply.
    _, effects = summarize("GREJANJE", "toplota", "Beograd, opština Voždovac. Planirani radovi na "
                           "toplovodnoj mreži (30.09.2026). Radovi na izgradnji toplovoda u Ulici vojvode Đurovića.")
    assert effects[0] == "Moguć prekid grejanja ili tople vode."
