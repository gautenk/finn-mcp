"""Search-card parsing for cars: a SearchResult should carry what the card shows."""
from __future__ import annotations

import pytest

from finn_mcp.scraper import get_scraper
from finn_mcp.scraper import cars
from finn_mcp.scraper.cars import _extract_images, _hydration_entries, _parse_spec_line

from .conftest import read_fixture

NEW = "cars_used_search_2026_11.html"  # Nov 2026 markup, q=volvo
OLD = "cars_used_search.html"  # Apr 2026 markup, q=tesla


def _parse(fixture: str, *, hydration: bool = True):
    html = read_fixture(fixture)
    if not hydration:
        # Drop the embedded JSON so only the DOM path is exercised.
        html = html.replace("data-react-query-state", "data-removed")
        assert _hydration_entries(html) == {}
    return get_scraper("cars_used").parse_search_cards(html)


def _by_code(results):
    return {r.finnkode: r for r in results}


def _count(results, key):
    return sum(1 for r in results if r.extra.get(key) is not None)


# ---------- concrete cards, Nov 2026 markup ----------

def test_new_fixture_order_and_count():
    results = _parse(NEW)
    assert len(results) == 50
    assert [r.finnkode for r in results[:3]] == ["480518729", "480920735", "480920693"]
    assert results[-1].finnkode == "474778409"


def test_new_promoted_private_card_without_image():
    r = _by_code(_parse(NEW))["480518729"]
    assert r.title == "Volvo 960"
    assert r.price == 31_942
    assert r.location == "Ski"
    assert r.thumbnail_url is None  # the card shows a placeholder icon
    assert r.url == "https://www.finn.no/mobility/item/480518729"
    x = r.extra
    assert x["subtitle"] == "antikk, sedan limmusin."
    assert x["year"] == 1991
    assert x["mileage_km"] == 432_000
    assert x["fuel"] == "Bensin"
    assert x["transmission"] == "Automat"
    assert x["seller_type"] == "Privat"
    assert x["smidig_bilhandel"] is True
    assert x["service_history"] is True
    assert x["promoted"] is True
    assert "seller_name" not in x
    assert "warranty_months" not in x
    assert "range_km" not in x
    assert "badges" not in x  # "Betalt plassering" is reported as promoted


def test_new_brand_dealer_ev_card():
    r = _by_code(_parse(NEW))["480920735"]
    assert r.title == "Volvo EX90"
    assert r.price == 899_000
    assert r.location == "Hønefoss"
    assert r.thumbnail_url and r.thumbnail_url.startswith("https://images.finncdn.no/")
    x = r.extra
    assert x["subtitle"] == "Twin M Ultra 7s Krok, Bose, 360, luft +++"
    assert x["year"] == 2025
    assert x["mileage_km"] == 9_800
    assert x["fuel"] == "El"
    assert x["range_km"] == 602
    assert x["seller_name"] == "Bilia Hønefoss Volvo"
    assert x["seller_type"] == "Merkeforhandler"
    assert x["warranty_months"] == 48
    assert x["exchange_right"] is True
    assert x["service_history"] is True
    assert x["condition_report"] is True
    assert "promoted" not in x
    assert "smidig_bilhandel" not in x
    # The compare-button tooltip ("Nyhet! Sammenlign biler") is not a badge.
    assert "badges" not in x


def test_new_plugin_hybrid_is_not_labelled_plain_petrol():
    r = _by_code(_parse(NEW))["480920693"]
    assert r.title == "Volvo V90"
    assert r.price == 539_000
    assert r.location == "Hønefoss"
    assert r.extra["fuel"] == "Plug-in Bensin"
    assert r.extra["range_km"] == 86
    assert r.extra["mileage_km"] == 65_000
    assert r.extra["warranty_months"] == 22


def test_new_fuel_label_not_taken_from_subtitle():
    # Subtitle "T5 262HK | Inscription | H/K | Skinn| El-Sete| 1-Eier++" used
    # to make this plug-in hybrid an "El" car.
    r = _by_code(_parse(NEW))["480918057"]
    assert r.extra["fuel"] == "Plug-in Bensin"
    assert "El-Sete" in r.extra["subtitle"]
    assert r.location == "Lierstranda"
    assert r.extra["seller_name"] == "Røhneselmer AS avd. Lier"


def test_new_year_not_taken_from_subtitle():
    # Subtitle "D2 Momentum aut, EU ok til 10/2028" used to give year 2028.
    r = _by_code(_parse(NEW))["476381362"]
    assert r.title == "Volvo V60"
    assert "10/2028" in r.extra["subtitle"]
    assert r.extra["year"] == 2015
    assert r.extra["mileage_km"] == 102_800
    assert r.extra["fuel"] == "Diesel"
    assert r.extra["transmission"] == "Automat"
    assert r.location == "Skien"
    assert r.extra["seller_type"] == "Privat"
    assert r.price == 128_942


def test_new_dealer_name_containing_separators():
    r = _by_code(_parse(NEW))["480915597"]
    assert r.location == "Haugesund"
    assert r.extra["seller_name"] == "Kverneland Bil | Volvo, Polestar, Zeekr Haugesund"
    assert r.extra["seller_type"] == "Merkeforhandler"
    assert r.extra["warranty_months"] == 60


def test_new_last_card_plain_private():
    r = _by_code(_parse(NEW))["474778409"]
    assert r.title == "Volvo S80"
    assert r.price == 89_942
    assert r.location == "Hamar"
    x = r.extra
    assert x["subtitle"] == "DRIVe Summum aut, prinsesse Estelle utgave"
    assert (x["year"], x["mileage_km"], x["fuel"], x["transmission"]) == (
        2012, 188_900, "Diesel", "Automat",
    )
    assert x["seller_type"] == "Privat"
    for absent in ("seller_name", "warranty_months", "service_history", "exchange_right",
                   "condition_report", "smidig_bilhandel", "promoted", "badges", "range_km"):
        assert absent not in x


def test_new_other_fuel_labels_kept_verbatim():
    by = _by_code(_parse(NEW))
    assert by["476429362"].extra["fuel"] == "Plug-in Diesel"
    assert by["480905048"].extra["fuel"] == "Hybrid bensin"
    assert by["480905048"].extra["range_km"] == 44
    assert by["480904917"].extra["fuel"] == "Hybrid bensin"
    assert by["480904917"].extra["transmission"] == "Automat"


# ---------- concrete cards, Apr 2026 markup ----------

def test_old_promoted_first_card():
    results = _parse(OLD)
    assert len(results) == 50
    r = results[0]
    assert r.finnkode == "459820662"
    assert r.title == "Tesla Model 3"
    assert r.price == 400_000
    assert r.location == "Molde"
    assert r.thumbnail_url and "459820662" in r.thumbnail_url
    x = r.extra
    assert x["subtitle"] == "Model 3 Long Range Dual Motor firehjulsdrift"
    assert (x["year"], x["mileage_km"], x["fuel"], x["range_km"]) == (2024, 31_101, "El", 629)
    assert x["seller_type"] == "Privat"
    assert x["smidig_bilhandel"] is True
    assert x["service_history"] is True
    assert x["promoted"] is True
    assert x["badges"] == ["Årets bruktbil 2026"]


def test_old_dealer_card():
    r = _by_code(_parse(OLD))["460212366"]
    assert r.price == 189_000
    assert r.location == "Dal"
    x = r.extra
    assert x["subtitle"] == "Performance 513Hk Long range AWD/Facelift"
    assert (x["year"], x["mileage_km"], x["fuel"], x["range_km"]) == (2021, 192_450, "El", 567)
    assert x["seller_name"] == "ØVRE ROMERIKE SALG OG UTLEIE AS"
    assert x["seller_type"] == "Forhandler"
    assert x["warranty_months"] == 12
    assert x["badges"] == ["Årets bruktbil 2026"]
    assert "promoted" not in x
    assert "service_history" not in x


def test_old_mileage_not_taken_from_subtitle():
    # Subtitle "SR / Krok / Norsk / 455km (WLTP)" used to give mileage 455.
    r = _by_code(_parse(OLD))["460172035"]
    assert r.title == "Tesla Model Y"
    assert r.extra["mileage_km"] == 43_500
    assert r.extra["range_km"] == 455
    assert r.location == "Dal"
    assert r.extra["seller_type"] == "Privat"
    assert r.price == 369_505


def test_old_price_and_year_ignore_subtitle_text():
    by = _by_code(_parse(OLD))
    # "... AP KR 6.100,-/MND" in the subtitle must not be read as the price.
    monthly = [x for x in by.values() if "/MND" in (x.extra.get("subtitle") or "")]
    assert monthly, "expected the card with a monthly amount in its subtitle"
    assert monthly[0].price == 519_000
    assert monthly[0].extra["year"] == 2025
    # "... | EU 04.2028" in the subtitle used to give year 2028.
    eu = by["460148013"]
    assert "2028" in eu.extra["subtitle"]
    assert eu.extra["year"] == 2018
    assert eu.extra["transmission"] == "Automat"


def test_old_private_card_with_warranty_and_last_card():
    by = _by_code(_parse(OLD))
    last = by["450648850"]
    assert last.title == "Tesla Model 3"
    assert last.price == 334_532
    assert last.location == "Kautokeino"
    assert last.extra["seller_type"] == "Privat"
    assert last.extra["service_history"] is True
    assert last.extra["badges"] == ["Årets bruktbil 2026"]
    assert "seller_name" not in last.extra
    plain = by["381312590"]
    assert plain.location == "Bergen"
    assert plain.extra["subtitle"] == "Performance AWD +++"
    assert plain.extra["seller_type"] == "Privat"
    assert "badges" not in plain.extra


# ---------- fill rates ----------

# (key, min count in NEW, min count in OLD) out of 50 cards; exact counts were
# verified against the page's embedded result JSON.
_EXTRA_RATES = [
    ("subtitle", 49, 50),  # one Nov card has an empty subtitle
    ("year", 50, 50),
    ("mileage_km", 50, 50),
    ("fuel", 50, 50),
    ("seller_type", 50, 50),
    ("seller_name", 31, 28),  # dealers only
    ("warranty_months", 23, 23),
    ("service_history", 30, 22),
    ("exchange_right", 8, 9),
    ("smidig_bilhandel", 7, 10),
    ("range_km", 26, 45),
]


@pytest.mark.parametrize("hydration", [True, False])
@pytest.mark.parametrize("fixture,col", [(NEW, 1), (OLD, 2)])
def test_fill_rates(fixture, col, hydration):
    results = _parse(fixture, hydration=hydration)
    assert len(results) == 50
    assert all(r.title for r in results)
    assert sum(1 for r in results if r.price) == 50
    assert sum(1 for r in results if r.location) == 50
    assert sum(1 for r in results if r.thumbnail_url) == 49
    for row in _EXTRA_RATES:
        assert _count(results, row[0]) == row[col], row[0]
    assert _count(results, "promoted") == 1
    # Every card shows either a transmission or an electric range.
    assert all("transmission" in r.extra or "range_km" in r.extra for r in results)
    # Dealers always have a name on the card, private sellers never.
    for r in results:
        assert r.extra["seller_type"] in {"Privat", "Forhandler", "Merkeforhandler"}
        assert ("seller_name" in r.extra) == (r.extra["seller_type"] != "Privat")


def test_fill_rates_fixture_specific():
    new, old = _parse(NEW), _parse(OLD)
    assert _count(new, "condition_report") == 14
    assert _count(old, "badges") == 24  # "Årets bruktbil 2026"
    assert {r.extra["fuel"] for r in new} == {
        "El", "Bensin", "Diesel", "Plug-in Bensin", "Plug-in Diesel", "Hybrid bensin",
    }
    assert {r.extra["fuel"] for r in old} == {"El"}


def test_extra_value_types():
    for fixture in (NEW, OLD):
        for r in _parse(fixture):
            for key in ("year", "mileage_km", "range_km", "warranty_months"):
                if key in r.extra:
                    assert type(r.extra[key]) is int, key
            for key in ("exchange_right", "service_history", "condition_report",
                        "smidig_bilhandel", "promoted"):
                if key in r.extra:
                    assert r.extra[key] is True, key
            assert 1950 <= r.extra["year"] <= 2027
            assert r.extra["mileage_km"] >= 1000  # not a WLTP range


# ---------- embedded JSON vs DOM ----------

@pytest.mark.parametrize("fixture", [NEW, OLD])
def test_dom_agrees_with_embedded_json(fixture):
    html = read_fixture(fixture)
    entries = _hydration_entries(html)
    results = _parse(fixture, hydration=False)
    assert [r.finnkode for r in results] == [r.finnkode for r in _parse(fixture)]
    assert set(entries) == {r.finnkode for r in results}
    for r in results:
        e = entries[r.finnkode]
        assert r.title == e["heading"]
        assert r.price == e["price"]["amount"]
        assert r.location == e["location"]
        assert r.extra["year"] == e["year"]
        assert r.extra["mileage_km"] == e["mileage"]
        assert r.extra["fuel"] == e["fuel"]
        assert r.extra.get("range_km") == e.get("driving_range")
        assert r.extra["seller_type"] == e["dealer_segment"]
        assert r.extra.get("seller_name") == e.get("organisation_name")
        assert r.extra.get("warranty_months") == e.get("warranty_duration")
        if "transmission" in r.extra:
            assert r.extra["transmission"] == e["transmission"]


@pytest.mark.parametrize("fixture", [NEW, OLD])
def test_embedded_json_fills_what_the_card_omits(fixture):
    with_json, dom_only = _parse(fixture), _parse(fixture, hydration=False)
    assert _count(dom_only, "transmission") < 50  # EV cards show range instead
    assert "make" not in dom_only[0].extra
    assert _count(with_json, "transmission") == 50
    assert _count(with_json, "make") == 50
    assert _count(with_json, "model") == 50
    assert _count(with_json, "published_at") == 50
    # DOM values are never overwritten by the JSON merge.
    for a, b in zip(with_json, dom_only):
        assert (a.title, a.price, a.location, a.thumbnail_url) == (
            b.title, b.price, b.location, b.thumbnail_url,
        )
        for k, v in b.extra.items():
            assert a.extra[k] == v


def test_embedded_json_values():
    r = _by_code(_parse(NEW))["480920735"]
    assert r.extra["make"] == "Volvo"
    assert r.extra["model"] == "EX90"
    assert r.extra["transmission"] == "Automat"  # card shows range, JSON has gearbox
    assert r.extra["published_at"] == "2026-11-13T22:26:13+00:00"
    other_make = _by_code(_parse(NEW))["480914810"]
    assert other_make.title == "Mercedes-Benz EQA 250"
    assert other_make.extra["make"] == "Mercedes-Benz"
    assert other_make.extra["model"] == "EQA 250"


def test_json_is_fallback_when_card_markup_is_unrecognised(monkeypatch):
    # Simulate a markup change that breaks the DOM hooks entirely.
    monkeypatch.setattr(cars, "_content_children", lambda article: [])
    r = _by_code(_parse(NEW))["480920735"]
    assert r.location == "Hønefoss"
    assert r.price == 899_000
    x = r.extra
    assert (x["year"], x["mileage_km"], x["fuel"], x["range_km"]) == (2025, 9_800, "El", 602)
    assert x["subtitle"] == "Twin M Ultra 7s Krok, Bose, 360, luft +++"
    assert x["seller_name"] == "Bilia Hønefoss Volvo"
    assert x["seller_type"] == "Merkeforhandler"
    assert x["warranty_months"] == 48
    assert x["exchange_right"] is True
    assert x["service_history"] is True
    assert x["condition_report"] is True


def test_hydration_entries_tolerates_garbage():
    assert _hydration_entries("<html></html>") == {}
    assert _hydration_entries('<script data-react-query-state>!!!</script>') == {}
    assert _hydration_entries('<script data-react-query-state>[1,2]</script>') == {}
    plain = '<script data-react-query-state>{"queries":[{"state":{"data":{"docs":[{"ad_id":7}]}}}]}</script>'
    assert _hydration_entries(plain) == {"7": {"ad_id": 7}}


# ---------- unit ----------

@pytest.mark.parametrize(
    "line,expected",
    [
        ("2024 ∙ 65 000 km ∙ Plug-in Bensin ∙ 86 km rekkevidde",
         {"year": 2024, "mileage_km": 65000, "fuel": "Plug-in Bensin", "range_km": 86}),
        ("2009 ∙ 280 000 km ∙ Diesel ∙ Manuell",
         {"year": 2009, "mileage_km": 280000, "fuel": "Diesel", "transmission": "Manuell"}),
        ("2017 ∙ 145 000 km ∙ Hybrid bensin ∙ Automat",
         {"year": 2017, "mileage_km": 145000, "fuel": "Hybrid bensin", "transmission": "Automat"}),
        ("2021 ∙ El", {"year": 2021, "fuel": "El"}),
        ("", {}),
    ],
)
def test_parse_spec_line(line, expected):
    assert _parse_spec_line(line) == expected


def test_extract_images_unchanged():
    assert _extract_images(None) == []
    assert _extract_images("a") == ["a"]
    assert _extract_images({"contentUrl": "b"}) == ["b"]
    assert _extract_images(["a", {"url": "c"}, [{"contentUrl": "d"}]]) == ["a", "c", "d"]


# ---------- card without a price element ----------

_NO_PRICE_CARD = """
<article><div class="search-ad-card-content">
<h2><a href="/mobility/item/123456789">Volvo V70</a></h2>
<div>2,4D Momentum, leasing kr 3990 pr mnd</div>
<span>2010 ∙ 250 000 km ∙ Diesel ∙ Manuell</span>
<div><div><span>Solgt</span></div></div>
<div><div><span>Bodø ∙ Bil AS</span><span>Forhandler ∙ 6 mnd garanti</span></div><span></span></div>
</div></article>
"""


def test_card_without_price_keeps_seller_line_and_invents_no_price():
    (r,) = get_scraper("cars_used").parse_search_cards(_NO_PRICE_CARD)
    assert r.finnkode == "123456789"
    assert r.title == "Volvo V70"
    assert r.price is None  # not the "kr 3990" from the subtitle
    assert r.location == "Bodø"
    x = r.extra
    assert (x["year"], x["mileage_km"], x["fuel"], x["transmission"]) == (
        2010, 250_000, "Diesel", "Manuell",
    )
    assert x["seller_name"] == "Bil AS"
    assert x["seller_type"] == "Forhandler"
    assert x["warranty_months"] == 6
    assert "badges" not in x
