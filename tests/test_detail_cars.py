"""Detail (single ad) parsing for cars: a Listing should carry what the ad page shows."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from finn_mcp.models import Listing
from finn_mcp.scraper import get_scraper
from finn_mcp.scraper.cars import (
    _html_to_text,
    _parse_no_datetime,
    _spec_pair,
    _targeting,
)
from selectolax.parser import HTMLParser

from .conftest import read_fixture

ITEM = "mobility_item.html"  # April 2026 markup, Tesla Model 3 from a dealer
CODE = "460212366"

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)


def _parse(html: str, finnkode: str = CODE) -> Listing:
    return get_scraper("cars_used").parse_detail(finnkode, html)


@pytest.fixture(scope="module")
def html() -> str:
    return read_fixture(ITEM)


@pytest.fixture(scope="module")
def listing(html) -> Listing:
    return _parse(html)


@pytest.fixture(scope="module")
def dom_only(html) -> Listing:
    """All <script> tags removed: no JSON-LD, no embedded targeting data."""
    stripped = _SCRIPT_RE.sub("", html)
    assert "application/ld+json" not in stripped
    assert "advertising-initial-state" not in stripped
    return _parse(stripped)


# ---------- core fields ----------


def test_core_fields(listing):
    assert listing.finnkode == CODE
    assert listing.vertical == "cars_used"
    assert listing.url == "https://www.finn.no/mobility/item/460212366"
    assert listing.title == "Tesla Model 3"
    assert listing.price == 189_000
    assert listing.currency == "NOK"


def test_description_is_the_full_ad_body_as_plain_text(listing):
    d = listing.description
    assert d is not None
    assert d.startswith("Vi har nå en fin Tesla Performance Long range AWD.")
    assert d.endswith("Forbehold om feil i annonsen")
    assert len(d) > 1500
    # Well past the ~160-char teaser the meta tags carry.
    assert "Bilen selges med garanti 12Måneder/200000km." in d
    assert "Velkommen til en hyggelig handel." in d
    assert "<" not in d and ">" not in d
    assert "&nbsp;" not in d and "\xa0" not in d
    assert "description_truncated" not in listing.attributes


def test_description_keeps_line_breaks(listing):
    lines = listing.description.split("\n")
    assert "Bilen selges med garanti 12Måneder/200000km." in lines
    assert "Ta kontakt for en hyggelig bil prat." in lines
    assert "Ronny: 938 64 190" in lines
    # <br><br> is one blank line, never more.
    assert "\n\n\n" not in listing.description
    assert all(line == line.strip() for line in lines)


def test_location(listing):
    assert listing.location == "Industrivegen 69, 2072 Dal"
    a = listing.attributes
    assert a["postal_code"] == "2072"
    assert a["city"] == "Dal"
    assert a["latitude"] == pytest.approx(60.251147)
    assert a["longitude"] == pytest.approx(11.192167)


def test_seller_is_the_dealer(listing):
    s = listing.seller
    assert s is not None
    assert s["name"] == "ØVRE ROMERIKE SALG OG UTLEIE AS"
    assert s["type"] == "Forhandler"
    assert s["org_number"] == "916020546"
    assert s["dealer_id"] == "719557881"
    assert s["dealer_url"] == (
        "https://www.finn.no/mobility/dealer/719557881/ovre-romerike-salg-og-utleie-as"
    )
    assert s["address"] == "INDUSTRIVEGEN 69, 2072 Dal"
    assert s["website"] == "https://orsu.no/"
    assert s["tenure"] == "5+ år på FINN"


def test_dates(listing):
    # The page only states when the ad was last changed, not when it was
    # published, so published_at stays empty rather than being guessed.
    assert listing.published_at is None
    assert listing.attributes["last_updated"] == "2026-04-17T08:56:00+02:00"
    assert listing.attributes["reference"] == "3800"


def test_spec_table_attributes_are_typed(listing):
    a = listing.attributes
    expected = {
        "brand": "Tesla",
        "model": "Model 3",
        "subtitle": "Performance 513Hk Long range AWD/Facelift",
        "year": 2021,
        "mileage_km": 192_450,
        "fuel": "El",
        "transmission": "Automat",
        "power_hp": 513,
        "range_km": 567,
        "body_type": "Kombi 5-dørs",
        "wheel_drive": "Firehjulsdrift",
        "weight_kg": 1893,
        "seats": 5,
        "doors": 5,
        "color": "Hvit",
        "country": "Norge",
        "tax_class": "Personbil",
        "registration_number": "EB98636",
        "vin": "5YJ3E7EC7MF830038",
        "first_registration": "2020-12-23",
        "owners": 1,
        "warranty": "Fragus GOSafe Premium",
        "warranty_months": 12,
        "warranty_km": 200_000,
        "sales_form": "Bruktbil til salgs",
        "reregistration_fee": 4532,
        "price_excl_reregistration": 184_468,
    }
    for key, value in expected.items():
        assert a.get(key) == value, key
        assert type(a[key]) is type(value), key
    # "Årsavgift: Les mer" is a link, not a value.
    assert not any("rsavgift" in k for k in a)
    assert all(re.fullmatch(r"[a-z0-9_]+", k) for k in a)


def test_equipment_list(listing):
    eq = listing.attributes["equipment"]
    assert len(eq) == 32
    assert eq[0] == "ABS-bremser"
    assert eq[-1] == "Vinterdekk på aluminiumsfelg"
    assert "Panorama glasstak" in eq


def test_images_cover_the_whole_gallery(listing):
    imgs = listing.images
    assert len(imgs) == 25
    assert len(set(imgs)) == 25
    # JSON-LD images first, unchanged.
    assert imgs[0] == (
        "https://images.finncdn.no/dynamic/default/item/460212366/"
        "edd74ded-e314-4784-86ab-3a19c0138913"
    )
    assert all(u.startswith("https://images.finncdn.no/dynamic/") for u in imgs)
    assert all("/item/460212366/" in u for u in imgs)


# ---------- every source is optional ----------


def test_dom_only_page_gives_the_same_listing(listing, dom_only):
    assert dom_only.title == "Tesla Model 3"
    assert dom_only.price == 189_000  # "Totalpris", not the 4 532 kr fee
    assert dom_only.description == listing.description
    assert dom_only.location == listing.location
    assert dom_only.seller == listing.seller
    assert dom_only.attributes == listing.attributes
    assert len(dom_only.images) == 25


def test_without_jsonld_embedded_data_and_dom_still_fill_in(html):
    no_ld = html.replace("application/ld+json", "application/x-removed")
    r = _parse(no_ld)
    assert r.title == "Tesla Model 3"
    assert r.price == 189_000
    assert r.attributes["brand"] == "Tesla"
    assert r.attributes["model"] == "Model 3"
    assert len(r.images) == 25


def test_embedded_targeting_alone_gives_typed_facts(html):
    m = re.search(
        r'<script id="advertising-initial-state"[^>]*>.*?</script>', html, re.DOTALL
    )
    assert m
    r = _parse(f"<html><body>{m.group(0)}</body></html>")
    a = r.attributes
    assert (a["year"], a["mileage_km"], a["range_km"], a["power_hp"]) == (2021, 192_450, 567, 513)
    assert (a["seats"], a["doors"], a["owners"]) == (5, 5, 1)
    assert a["registration_number"] == "EB98636"
    assert a["subtitle"] == "Performance 513Hk Long range AWD/Facelift"
    assert r.price == 189_000
    assert len(r.images) == 25
    # Coded values (fuel=4, body_type=2, ...) are not passed off as facts.
    for key in ("fuel", "body_type", "transmission", "color", "wheel_drive"):
        assert key not in a
    assert r.description is None and r.location is None and r.seller is None


def test_targeting_for_another_ad_is_ignored(html):
    assert _targeting(html, CODE)["year"] == ["2021"]
    assert _targeting(html, "999999999") == {}


def test_teaser_description_is_flagged_as_truncated(html):
    # Without the ad body the only text left is the ~160-char meta teaser.
    cut = html.replace(">Beskrivelse</h2>", ">Fjernet</h2>")
    r = _parse(cut)
    assert r.description is not None
    assert r.description.startswith("Vi har nå en fin Tesla")
    assert r.description.endswith("...")
    assert r.attributes["description_truncated"] is True


@pytest.mark.parametrize(
    "page",
    [
        "",
        "<html></html>",
        "<html><head><title>x</title></head><body><main></main></body></html>",
        "<h1>Bare en tittel</h1>",
        '<script type="application/ld+json">{not json</script>',
        '<script type="application/ld+json">{"@type":"Product","offers":[],"brand":null,"image":7}</script>',
        '<script id="advertising-initial-state" type="application/json">{"config":[]}</script>',
        "<h2>Beskrivelse</h2><h2>Spesifikasjoner</h2><dl><dt>Modellår</dt></dl><h2>Sted</h2>",
    ],
)
def test_minimal_pages_do_not_raise(page):
    r = _parse(page, "123456789")
    assert isinstance(r, Listing)
    assert r.finnkode == "123456789"
    assert r.title
    assert r.price is None
    assert r.location is None
    assert r.seller is None
    assert r.published_at is None
    assert r.images == []


def test_title_falls_back_to_h1_and_placeholder():
    assert _parse("<main><h1>Volvo V70</h1></main>", "1").title == "Volvo V70"
    assert _parse("<p>ingenting</p>", "1").title == "Car listing 1"
    # A footer/branding <h1> is not the ad title.
    assert _parse("<footer><h1>FINN.no</h1></footer>", "1").title == "Car listing 1"


# ---------- helpers ----------


def test_html_to_text_handles_paragraphs_lists_and_entities():
    node = HTMLParser(
        "<div><p>F&oslash;rste &amp; beste</p><ul><li>En</li><li>To&nbsp;tre</li></ul>"
        "Slutt<br>linje<script>x()</script></div>"
    ).css_first("div")
    assert _html_to_text(node) == "Første & beste\n\nEn\n\nTo tre\n\nSlutt\nlinje"
    assert _html_to_text(None) is None


def test_private_seller_block_and_synthetic_specs():
    page = """
    <main><div><h1>Volvo V70</h1><p>2,4D Momentum</p>
      <div><span>Modellår</span><p>2010</p></div>
      <div><span>Kilometerstand</span><p>250&nbsp;000 km</p></div>
      <div><p>Totalpris</p><h2>45&nbsp;000&nbsp;kr</h2></div></div>
    <section><h2>Beskrivelse</h2><div><p>Selges som den står.</p><p>EU-godkjent.</p></div></section>
    <section><h2>Spesifikasjoner</h2><dl>
      <div><dt>Effekt</dt><dd>120 kW</dd></div>
      <div><dt>Garantiens varighet</dt><dd>2 år</dd></div>
      <div><dt>Batterikapasitet</dt><dd>75 kWh</dd></div>
      <div><dt>Ukjent felt</dt><dd>Noe</dd></div>
    </dl></section>
    <div><h2>Sted</h2><a href="/map?x=1">0581 Oslo</a></div>
    <div><h2>Annonseinformasjon</h2><div><div><p>Sist endret</p><p>3. jan. 2026 14:05</p></div></div></div>
    </main>
    <script type="application/ld+json">{"@type":"Product","name":"Volvo V70",
      "offers":{"@type":"Offer","seller":{"@type":"Person","name":"Kari"}}}</script>
    """
    r = _parse(page, "1")
    assert r.title == "Volvo V70"
    assert r.price == 45_000
    assert r.description == "Selges som den står.\n\nEU-godkjent."
    assert r.location == "0581 Oslo"
    assert r.seller == {"name": "Kari", "type": "Privat"}
    a = r.attributes
    assert a["subtitle"] == "2,4D Momentum"
    assert (a["year"], a["mileage_km"]) == (2010, 250_000)
    assert a["power_kw"] == 120 and "power_hp" not in a
    assert a["warranty_months"] == 24
    assert a["battery_kwh"] == 75.0
    assert a["ukjent_felt"] == "Noe"
    assert (a["postal_code"], a["city"]) == ("0581", "Oslo")
    assert "latitude" not in a
    assert a["last_updated"] == "2026-01-03T14:05:00+01:00"


def test_spec_pair_conversions():
    assert _spec_pair("Kilometerstand", "192 450 km") == ("mileage_km", 192_450)
    assert _spec_pair("1. gang registrert", "23.12.2020") == ("first_registration", "2020-12-23")
    assert _spec_pair("Effekt", "513 hk") == ("power_hp", 513)
    assert _spec_pair("Årsavgift", "Les mer") is None
    assert _spec_pair("Modellår", "ukjent") == ("year", "ukjent")
    assert _spec_pair(None, "x") is None and _spec_pair("x", None) is None


def test_norwegian_datetimes_get_the_oslo_offset():
    summer = _parse_no_datetime("17. april 2026, 08:56")
    assert summer == datetime.fromisoformat("2026-04-17T08:56:00+02:00")
    assert summer.utcoffset() == timedelta(hours=2)
    winter = _parse_no_datetime("3. des. 2026 kl. 10:00")
    assert winter.utcoffset() == timedelta(hours=1)
    # DST boundaries: last Sunday of March / October.
    assert _parse_no_datetime("29. mars 2026, 01:59").utcoffset() == timedelta(hours=1)
    assert _parse_no_datetime("29. mars 2026, 03:00").utcoffset() == timedelta(hours=2)
    assert _parse_no_datetime("25. oktober 2026, 03:00").utcoffset() == timedelta(hours=1)
    assert _parse_no_datetime("i dag") is None
    assert _parse_no_datetime("31. februar 2026") is None
    assert _parse_no_datetime(None) is None


def test_description_fallback_does_not_leak_other_sections(html):
    # Same body without the data-testid marker: still exactly the ad text.
    unmarked = html.replace('data-testid="expandable-section"', "")
    assert unmarked != html
    assert _parse(unmarked).description == _parse(html).description

    # Heading in a wide container: only what sits between it and the next
    # heading is the description, never the rest of the page.
    page = (
        "<html><body><div id='app'><h1>Volvo V90</h1><nav><a href='/x'>Torget</a></nav>"
        "<h2>Beskrivelse</h2><div>Pen bil.<br>Lite brukt.</div><p>EU-godkjent.</p>"
        "<h2>Utstyr</h2><ul><li>ABS</li></ul>"
        "<div><h2>Lignende annonser</h2><p>Audi A4 til salgs</p></div>"
        "<footer>FINN.no AS</footer></div></body></html>"
    )
    assert _parse(page, "1").description == "Pen bil.\nLite brukt.\n\nEU-godkjent."

    # Headings directly in the page root scope nothing.
    bare = (
        "<html><body><h1>Volvo V90</h1><a href='/x'>Torget</a>"
        "<h2>Beskrivelse</h2><h2>Sted</h2><h2>Utstyr</h2><ul><li>Meny</li></ul></body></html>"
    )
    listing = _parse(bare, "1")
    assert listing.description is None
    assert listing.location is None
    assert "equipment" not in listing.attributes
