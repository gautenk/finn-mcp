"""Ad pages saved in November 2026 (current markup), one per ad type.

Fetched anonymously through a browser; they cover the variants the April
pages did not: a private and a dealer car ad, a shop ad on Torget, external
lettings and new-build projects.
"""

from __future__ import annotations

import pytest

from finn_mcp.scraper import get_scraper
from .conftest import read_fixture


def _parse(vertical: str, fixture: str, finnkode: str):
    return get_scraper(vertical).parse_detail(finnkode, read_fixture(fixture))


def _assert_clean_text(text: str | None, min_len: int) -> None:
    assert text and len(text) >= min_len
    assert "<p>" not in text and "<br" not in text and "&amp;" not in text


def test_car_dealer_ad():
    listing = _parse("cars_used", "mobility_item_dealer_2026_11.html", "480920735")
    assert listing.title == "Volvo EX90"
    assert listing.price == 899_000
    assert listing.location == "Asbjørnsens gate 21, 3513 Hønefoss"
    assert listing.seller["name"] == "Bilia Hønefoss Volvo"
    assert listing.seller["type"] == "Forhandler"
    assert len(listing.images) == 41
    _assert_clean_text(listing.description, 5000)
    a = listing.attributes
    assert a["postal_code"] == "3513" and a["city"] == "Hønefoss"
    assert a["latitude"] == pytest.approx(60.175864)
    assert (a["year"], a["mileage_km"], a["fuel"], a["power_hp"]) == (2025, 9800, "El", 408)
    assert a["range_km"] == 602 and a["seats"] == 7
    assert a["transmission"] == "Automat"
    assert a["last_updated"].startswith("2026-11-13T23:25")


def test_car_private_ad():
    listing = _parse("cars_used", "mobility_item_private_2026_11.html", "480518729")
    assert listing.title == "Volvo 960"
    assert listing.price == 31_942
    assert listing.location == "1400 Ski"
    # A private ad shows no seller card; nothing is invented.
    assert listing.seller is None
    _assert_clean_text(listing.description, 300)
    a = listing.attributes
    assert a["postal_code"] == "1400" and a["city"] == "Ski"
    assert (a["year"], a["mileage_km"], a["fuel"]) == (1991, 432_000, "Bensin")
    assert a["transmission"] == "Automat" and a["wheel_drive"] == "Bakhjulsdrift"
    assert a["first_registration"] == "1990-10-29"


def test_torget_shop_ad():
    listing = _parse("bap", "bap_item_shop_2026_11.html", "480725550")
    assert listing.title.startswith("Bolia Sofa")
    assert listing.price == 9499
    assert listing.location == "3921 Porsgrunn"
    assert listing.seller["type"] == "retailer"
    assert listing.seller["name"] == "SOFASENTRALEN"
    assert len(listing.images) == 5
    _assert_clean_text(listing.description, 500)
    assert listing.description.endswith("Prisen kan diskuteres ved rask avgjørelse!")
    assert listing.attributes["brand"] == "Bolia"
    assert listing.attributes["width_cm"] == 230
    assert listing.published_at is not None and listing.published_at.tzinfo is not None


def test_home_ad():
    listing = _parse("homes", "homes_ad_2026_11.html", "480700692")
    assert listing.price == 13_800_000
    assert listing.location == "Ekebergveien 239D, 1166 Oslo"
    assert listing.seller["name"] == "Nordvik Frogner"
    assert listing.seller["contacts"][0]["name"] == "Stian Pøhner"
    assert len(listing.images) == 42
    _assert_clean_text(listing.description, 5000)
    assert listing.description.startswith("Velkommen til leilighet A206")
    a = listing.attributes
    assert a["property_type"] == "Leilighet" and a["ownership_type"] == "Selveier"
    assert (a["bedrooms"], a["rooms"], a["construction_year"]) == (3, 4, 2024)
    assert a["usable_area_m2"] == 125 and a["total_price_nok"] == 13_909_090
    assert listing.published_at is not None and listing.published_at.tzinfo is not None


def test_letting_ad():
    listing = _parse("lettings", "lettings_ad_2026_11.html", "480912157")
    assert listing.price == 18_500
    assert listing.location == "Grenseveien 11c, 0571 Oslo"
    assert listing.seller["name"] == "Husleie.no"
    assert len(listing.images) == 20
    _assert_clean_text(listing.description, 2000)
    a = listing.attributes
    assert a["property_type"] == "Leilighet" and a["bedrooms"] == 1
    assert a["deposit_nok"] == 55_500 and a["furnished"] == "Møblert"
    assert (a["available_from"], a["available_to"]) == ("2026-12-16", "2027-01-14")
    assert a["rent_includes"] == "Strøm, bredbånd, oppvarming"


def test_external_letting_ad():
    # Served under /realestate/lettingsexternal/, parsed by the lettings parser.
    listing = _parse("lettings", "lettingsexternal_ad_2026_11.html", "480917710")
    assert listing.title == "Korttidsleie bofelleskap fra 01/04"
    assert listing.price == 11_547
    assert listing.location == "Siloveien 14, 0574 Oslo"
    assert listing.seller == {"type": "private"}
    assert len(listing.images) == 10
    _assert_clean_text(listing.description, 400)
    a = listing.attributes
    assert a["property_type"] == "Rom i bofelleskap" and a["bedrooms"] == 2
    assert a["available_from"] == "2027-04-01" and a["deposit_nok"] == 0


@pytest.mark.parametrize(
    "fixture,finnkode,price,location,agency",
    [
        ("project_ad_2026_11.html", "390069564", 19_200_000, "Kleivveien 22, 1356 Bekkestua", "Nordvik Frogner"),
        ("projectsingle_ad_2026_11.html", "480868667", 22_000_000, "Leangveien 43A, 1387 Asker", "PrivatMegleren Nyeboliger Oslo"),
    ],
)
def test_new_build_ads(fixture, finnkode, price, location, agency):
    # Served under /realestate/project*/, parsed by the homes parser.
    listing = _parse("homes", fixture, finnkode)
    assert listing.price == price
    assert listing.location == location
    assert listing.seller["name"] == agency
    assert len(listing.images) >= 10
    _assert_clean_text(listing.description, 5000)
    assert listing.attributes["facilities"]
    assert listing.published_at is not None
