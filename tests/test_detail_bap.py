"""Torget (bap) ad pages: get_listing must carry what the ad page shows."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from finn_mcp.models import Listing
from finn_mcp.scraper import get_scraper
from finn_mcp.scraper.bap import _plain_text, item_page_data

FIXTURES = Path(__file__).parent / "fixtures"
SOFA = ("bap_item_2026_11.html", "480920014")  # November 2026 markup
IPHONE = ("bap_item.html", "460211124")  # April 2026 markup

SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.DOTALL | re.IGNORECASE)
ROUTER_RE = re.compile(r"<script>\s*window\.__staticRouterHydrationData.*?</script>", re.DOTALL)
LDJSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>.*?</script>', re.DOTALL)
TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

IPHONE_LINES = [
    "Apple iPhone 16 Pro",
    "• Farge : Dessert Titanium",
    "• Lagring : 1TB",
    "• Tilstand : nærmere ny veldig pent brukt med beskyttelse på skjermen og Apple deksel.",
    "• Frakt : Post ca 175kr +/-",
    "• Betaling : Vipps eller kontant",
    "• Garanti : Ja",
    "• Batteri : original med 90-91%",
    "• Pris : 12.000",
    "Kan sende med Norges Post inkl . sporing",
]
SOFA_TEXT = "Selger en veldig pent brukt lounge sofa «ypperlig» fra IKEA i samarbeid med HAY."


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(page: tuple[str, str], *strip: re.Pattern[str]) -> Listing:
    html = _html(page[0])
    for pattern in strip:
        html, n = pattern.subn("", html)
        assert n, f"{pattern.pattern} matched nothing in {page[0]}"
    return get_scraper("bap").parse_detail(page[1], html)


@pytest.fixture(scope="module")
def sofa() -> Listing:
    return _parse(SOFA)


@pytest.fixture(scope="module")
def iphone() -> Listing:
    return _parse(IPHONE)


# ---------- full page ----------


def test_iphone_description_is_the_whole_ad_text(iphone):
    d = iphone.description
    assert d.split("\n") == IPHONE_LINES
    assert d.split()[0] == "Apple" and d.split()[-1] == "sporing"
    assert len(d) >= 300  # JSON-LD / og:description stop at 156 characters ("• Frak")
    assert not TAG_RE.search(d) and "<" not in d
    assert "&amp;" not in d and "\xa0" not in d


def test_sofa_description(sofa):
    assert sofa.description == SOFA_TEXT
    assert sofa.description.split()[0] == "Selger" and sofa.description.split()[-1] == "HAY."
    assert len(sofa.description) == 80
    assert "<" not in sofa.description


def test_core_fields_sofa(sofa):
    assert sofa.finnkode == "480920014" and sofa.vertical == "bap"
    assert sofa.url == "https://www.finn.no/recommerce/forsale/item/480920014"
    assert sofa.title == "Ypperlig Lounge sofa fra HAY / Ikea"
    assert sofa.price == 2500 and sofa.currency == "NOK"
    assert sofa.location == "0688 Oslo"
    assert len(sofa.images) == 3
    assert sofa.images[0].endswith("/item/480920014/eed7c675-aadc-410a-b48f-4791bc8002ec")
    assert all(i.startswith("https://images.finncdn.no/") for i in sofa.images)


def test_core_fields_iphone(iphone):
    assert iphone.title == "Apple iPhone 16 Pro 1 TB"
    assert iphone.price == 12175 and iphone.currency == "NOK"
    assert iphone.location == "1860 Trøgstad"
    assert len(iphone.images) == 4
    assert len(set(iphone.images)) == 4


def test_published_at_is_the_last_changed_time_with_zone(sofa, iphone):
    # The page only prints "Sist endret"; that is what published_at carries.
    assert sofa.published_at.isoformat() == "2026-11-13T23:19:38.216105+01:00"
    assert sofa.published_at.utcoffset() == timedelta(hours=1)
    assert sofa.attributes["last_edited"] == "2026-11-13T23:19:38.216105+01:00"
    assert iphone.published_at.isoformat() == "2026-04-17T08:48:57.411692+02:00"
    assert iphone.published_at.utcoffset() == timedelta(hours=2)


def test_seller_is_private_without_invented_name(sofa, iphone):
    # Logged-out pages hide the profile ("Du må være logget inn ..."): no name.
    assert sofa.seller == {"type": "private", "owner_id": 192371781}
    assert iphone.seller == {"type": "private", "owner_id": 525492093}
    assert sofa.attributes["seller_type"] == "private"
    assert "name" not in iphone.seller


def test_attributes_sofa(sofa):
    a = sofa.attributes
    assert a["condition"] == "UsedCondition"
    assert a["condition_label"] == "Pent brukt - I god stand"
    assert a["category"] == "Møbler og interiør > Sofaer og lenestoler > Sofaer"
    assert a["category_id"] == 8374
    assert a["trade_type"] == "Til salgs"
    assert a["sold"] is False
    assert a["availability"] == "InStock"
    assert a["width"] == "150 cm" and a["width_cm"] == 150
    assert a["postal_code"] == "0688" and a["postal_place"] == "Oslo"
    assert "brand" not in a  # the page states no brand; the title is not a source
    # Shipping: not Fiks ferdig, but Delivr pickup is offered.
    assert a["fiks_ferdig"] is False and a["buy_now"] is False and a["free_shipping"] is False
    assert a["eligible_for_shipping"] is True
    assert a["shipping_options"] == ["Delivr"]
    assert "fiks_ferdig_on_request" not in a


def test_attributes_iphone(iphone):
    a = iphone.attributes
    assert a["condition"] == "NewCondition"
    assert a["condition_label"] == "Som ny - Ikke synlig brukt"
    assert a["brand"] == "Apple"
    assert a["category"] == "Elektronikk og hvitevarer > Telefoner og tilbehør > Mobiltelefoner"
    assert a["size"] == "1 TB"
    assert a["mobile_memory_size"] == "1 TB" and a["storage_gb"] == 1024
    assert a["mobile_model"] == "iPhone 16/Plus/Pro/Max"
    assert a["trade_type"] == "Til salgs"
    assert a["sold"] is False
    assert a["fiks_ferdig"] is False and a["eligible_for_shipping"] is False
    assert a["fiks_ferdig_on_request"] is True  # "Be om Fiks ferdig" button
    assert a["shipping_options"] == []


def test_attribute_keys_are_snake_case_and_values_typed(sofa, iphone):
    for listing in (sofa, iphone):
        for key, value in listing.attributes.items():
            assert re.fullmatch(r"[a-z][a-z0-9_]*", key), key
            assert isinstance(value, (str, int, float, bool, list)), key
            assert value != "", key


# ---------- sources removed one at a time ----------


@pytest.mark.parametrize("page", [SOFA, IPHONE])
def test_without_embedded_data_jsonld_and_dom_fill_in(page):
    full = _parse(page)
    listing = _parse(page, ROUTER_RE)
    assert listing.title == full.title
    assert listing.description == full.description  # from the DOM, not the JSON-LD teaser
    assert listing.price == full.price
    assert listing.location == full.location
    for key in ("condition", "condition_label", "category", "trade_type"):
        assert listing.attributes[key] == full.attributes[key]
    assert listing.images and "finncdn" in listing.images[0]
    assert listing.seller is None  # nothing on the page says who sells


@pytest.mark.parametrize("page", [SOFA, IPHONE])
def test_without_jsonld_only_schema_org_values_are_lost(page):
    full = _parse(page)
    listing = _parse(page, LDJSON_RE)
    assert listing.description == full.description
    assert listing.images == full.images
    assert listing.published_at == full.published_at
    assert listing.seller == full.seller
    # the embedded data carries its own copy of the Product block
    assert listing.attributes == full.attributes


def test_dom_only_iphone():
    listing = _parse(IPHONE, SCRIPT_RE)
    assert listing.title == "Apple iPhone 16 Pro 1 TB"
    assert listing.description.split("\n") == IPHONE_LINES
    assert "<" not in listing.description
    assert listing.price == 12175
    assert listing.location == "1860 Trøgstad"
    assert listing.published_at.replace(tzinfo=None) == datetime(2026, 4, 17, 8, 48)
    assert listing.seller is None
    a = listing.attributes
    assert a["condition_label"] == "Som ny - Ikke synlig brukt"
    assert a["brand"] == "Apple"
    assert a["mobile_memory_size"] == "1 TB" and a["storage_gb"] == 1024
    assert a["mobile_model"] == "iPhone 16/Plus/Pro/Max"
    assert a["trade_type"] == "Til salgs"
    assert a["fiks_ferdig_on_request"] is True
    assert "condition" not in a and "sold" not in a  # not stated in the DOM
    assert len(listing.images) == 4


def test_dom_only_sofa():
    listing = _parse(SOFA, SCRIPT_RE)
    assert listing.title == "Ypperlig Lounge sofa fra HAY / Ikea"
    assert listing.description == SOFA_TEXT
    assert listing.price == 2500
    assert listing.location == "0688 Oslo"
    assert listing.published_at.replace(tzinfo=None) == datetime(2026, 11, 13, 23, 19)
    a = listing.attributes
    assert a["condition_label"] == "Pent brukt - I god stand"
    assert a["width"] == "150 cm" and a["width_cm"] == 150
    assert a["shipping_options"] == ["Delivr"]
    assert len(listing.images) == 3


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html></html>",
        "<html><body><h1></h1><section data-testid='description'></section></body></html>",
        '<script>window.__staticRouterHydrationData = JSON.parse("{not json");</script>',
        '<script>window.__staticRouterHydrationData = JSON.parse("[1,2]");</script>',
        '<script>window.__staticRouterHydrationData = JSON.parse("{\\"loaderData\\":'
        '{\\"item-recommerce\\":{\\"itemData\\":{\\"title\\":null,\\"price\\":\\"x\\",'
        '\\"location\\":null,\\"extras\\":[1,{}],\\"images\\":[null],\\"category\\":7,'
        '\\"meta\\":{\\"edited\\":\\"yesterday\\"}},\\"transactableData\\":3}}}");</script>',
        '<script type="application/ld+json">{"@type":"Product","offers":[],"brand":5}</script>',
    ],
)
def test_pages_without_usable_sources_never_raise(html):
    listing = get_scraper("bap").parse_detail("123", html)
    assert listing.finnkode == "123"
    assert listing.title == "BAP listing 123"
    assert listing.description is None
    assert listing.price is None
    assert listing.location is None
    assert listing.published_at is None
    assert listing.seller is None
    assert listing.images == []


def test_minimal_dom_page():
    html = "<html><body><div><h1>Stol</h1><h2>Gis bort</h2><p>0 kr</p></div></body></html>"
    listing = get_scraper("bap").parse_detail("5", html)
    assert listing.title == "Stol"
    assert listing.price is None  # a give-away has no price
    assert listing.attributes == {"trade_type": "Gis bort"}


def test_item_page_data_is_empty_when_missing():
    assert item_page_data("") == {}
    assert item_page_data("<script>var x = 1</script>") == {}
    assert item_page_data(_html(SOFA[0]))["itemData"]["meta"]["adId"] == "480920014"


# ---------- description text ----------


def test_plain_text_strips_markup_and_keeps_breaks():
    raw = (
        "<p>Pent&nbsp;brukt &amp; lite slitt</p><ul><li>Én</li><li>To <b>fet</b></li></ul>"
        "Linje<br/>ny linje<!-- skjult -->"
    )
    assert _plain_text(raw) == "Pent brukt & lite slitt\n• Én\n• To fet\nLinje\nny linje"


def test_plain_text_keeps_plain_newlines_and_comparisons():
    assert _plain_text("a \n\n\n\n b\r\nc < d > e") == "a\n\nb\nc < d > e"
    assert _plain_text("   ") is None
    assert _plain_text(None) is None


# ---------- added by the independent check ----------


def test_plain_text_keeps_angle_brackets_that_are_not_tags():
    assert _plain_text("Mål: B<H, 5 < 6 > 3 <strong>fin</strong><br>Ny") == (
        "Mål: B<H, 5 < 6 > 3 fin\nNy"
    )


def test_odd_shaped_embedded_lists_and_jsonld_properties_never_raise():
    scraper = get_scraper("bap")
    blob = (
        '<script>window.__staticRouterHydrationData = JSON.parse("{\\"loaderData\\":'
        '{\\"item-recommerce\\":{\\"itemData\\":{\\"title\\":\\"T\\",\\"images\\":5,'
        '\\"extras\\":7},\\"transactableUiData\\":{\\"sections\\":[]}}}}");</script>'
    )
    assert scraper.parse_detail("1", blob).title == "T"
    ld = (
        '<script type="application/ld+json">{"@type":"Product","name":"N",'
        '"additionalProperty":[{"name":5,"value":{}},{"name":"Size X","value":{}},'
        '{"name":"category","value":"A > B"}]}</script>'
    )
    assert scraper.parse_detail("1", ld).attributes == {"category": "A > B"}


@pytest.mark.parametrize("page, offset_hours", [(SOFA, 1), (IPHONE, 2)])
def test_dom_only_time_is_norwegian_time_with_zone(page, offset_hours):
    dom = _parse(page, SCRIPT_RE).published_at
    full = _parse(page).published_at
    assert dom.utcoffset().total_seconds() == offset_hours * 3600
    assert dom == full.replace(second=0, microsecond=0)


@pytest.mark.parametrize("page", [SOFA, IPHONE])
def test_dom_only_images_are_one_full_size_url_per_picture(page):
    full = _parse(page).images
    dom = _parse(page, SCRIPT_RE).images
    assert [u.rsplit("/", 1)[-1] for u in dom] == [u.rsplit("/", 1)[-1] for u in full]
    assert all("/960w/" in u for u in dom)
