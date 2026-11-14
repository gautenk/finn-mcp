"""Detail (single ad) parsing for homes and lettings.

Each page is parsed twice: as saved (embedded data + company JSON + DOM) and
with every <script> removed (DOM only), since any one source may disappear.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from finn_mcp.models import Listing
from finn_mcp.scraper import get_scraper
from finn_mcp.scraper.homes import _html_to_text

from .conftest import read_fixture

HOMES = ("homes", "homes_ad.html", "459322772")
LETTINGS = ("lettings", "lettings_ad.html", "448510748")

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.DOTALL)
_TAG_RE = re.compile(r"<[a-zA-Z/!]")
_ENTITY_RE = re.compile(r"&(?:[a-z]+|#\d+);")


def _parse(spec: tuple[str, str, str], *, strip_scripts: bool = False) -> Listing:
    vertical, fixture, finnkode = spec
    html = read_fixture(fixture)
    if strip_scripts:
        html = _SCRIPT_RE.sub("", html)
        assert "streamController" not in html
    return get_scraper(vertical).parse_detail(finnkode, html)


@pytest.fixture(scope="module")
def homes() -> Listing:
    return _parse(HOMES)


@pytest.fixture(scope="module")
def homes_dom() -> Listing:
    return _parse(HOMES, strip_scripts=True)


@pytest.fixture(scope="module")
def lettings() -> Listing:
    return _parse(LETTINGS)


@pytest.fixture(scope="module")
def lettings_dom() -> Listing:
    return _parse(LETTINGS, strip_scripts=True)


def _assert_plain_text(text: str | None) -> None:
    assert text
    assert not _TAG_RE.search(text), "description contains HTML tags"
    assert not _ENTITY_RE.search(text), "description contains undecoded entities"
    assert "\xa0" not in text
    assert "Vis hele beskrivelsen" not in text  # the expand button is not ad text
    assert "\n\n\n" not in text


# ---------------------------------------------------------------- homes


def test_homes_core_fields(homes):
    assert homes.finnkode == "459322772"
    assert homes.vertical == "homes"
    assert homes.title.startswith("Lys & innbydende 2-roms i meget høy 1.etg")
    assert homes.title.endswith("Sentralt, men rolig")
    assert homes.price == 4_200_000
    assert homes.currency == "NOK"
    assert homes.location == "Dælenenggata 14A, 0567 Oslo"


def test_homes_description_is_the_whole_ad_text(homes):
    desc = homes.description
    _assert_plain_text(desc)
    assert desc.startswith("DNB Eiendom v/ Marte Gabrielli har gleden av å presentere fine Dælenenggata 14A!")
    assert desc.endswith("Kopi av reguleringskart er vedlagt salgsoppgaven.")
    assert len(desc) > 15_000
    # Main text: paragraphs and the bullet list keep their line breaks.
    assert "rett inn!" in desc
    assert "\n\nVerdt å merke seg:\n* Varmtvann, fyring og tv/internett\n* God planløsning\n" in desc
    # The further sections of the ad follow under their own headings.
    for heading in ("Innhold", "Standard", "Oppvarming", "Beliggenhet", "Parkering", "Adkomst", "Regulering"):
        assert f"\n\n## {heading}\n" in desc, heading
    assert "\n\n## Innhold\nKjøkken\nLekkert og tidløst kjøkken" in desc
    assert "## Oppvarming\nOppvarming basert på radiatorer" in desc
    # Entities are decoded.
    assert 'via "gi bud"- lenken' in desc
    # The "Beskrivelse" section repeats the main text and is not added twice.
    assert "## Beskrivelse" not in desc
    assert desc.count("har gleden av å presentere fine Dælenenggata 14A!") == 1


def test_homes_published_and_edited(homes):
    assert homes.published_at == datetime(
        2026, 4, 13, 11, 29, 56, 612312, tzinfo=timezone(timedelta(hours=2))
    )
    assert homes.published_at.tzinfo is not None
    assert homes.attributes["last_edited"] == "2026-04-13T11:22:00+02:00"


def test_homes_seller_is_the_broker(homes):
    assert homes.seller == {
        "type": "professional",
        "name": "DNB Eiendom AS",
        "url": "https://dnbeiendom.no",
        "contacts": [
            {"name": "Marte Gabrielli", "title": "Eiendomsmegler", "phone": "41 42 99 50"},
            {"name": "Nana Badu", "title": "Eiendomsmegler", "phone": "46 22 43 02"},
        ],
    }


def test_homes_attributes(homes):
    a = homes.attributes
    expected = {
        "property_type": "Leilighet",
        "ownership_type": "Andel",
        "bedrooms": 1,
        "rooms": 2,
        "floor": 1,
        "construction_year": 1938,
        "usable_area_m2": 46,
        "internal_bra_m2": 41,
        "external_bra_m2": 5,
        "plot_area_m2": 1079,
        "plot_owned": True,
        "total_price_nok": 4_221_894,
        "shared_debt_nok": 12_398,
        "collective_assets_nok": 8_172,
        "asset_value_nok": 1_048_647,
        "sales_costs_nok": 9_496,
        "common_cost_monthly_nok": 4_401,
        "energy_label": "F",
        "local_area": "Rodeløkka",
        "postal_code": "0567",
        "postal_name": "Oslo",
        "lat": 59.9259,
        "lon": 10.7728,
        "facilities": [
            "Barnevennlig",
            "Bredbåndstilknytning",
            "Kjæledyr tillatt",
            "Kabel-TV",
            "Offentlig vann/kloakk",
            "Parkett",
            "Rolig",
            "Sentralt",
        ],
        "viewings": [
            {"date": "2026-04-19", "from": "15:30", "to": "16:15"},
            {"date": "2026-04-20", "from": "16:30", "to": "17:15"},
        ],
        "viewing_times": ["2026-04-19T13:30:00+00:00", "2026-04-20T14:30:00+00:00"],
        "cadastre": {
            "municipality_number": 301,
            "land_number": 227,
            "title_number": 85,
            "part_ownership_number": 84,
        },
        "housing_cooperative": "Dælenenga Borettslag",
        "housing_cooperative_org_number": "950 291 370",
        "change_of_ownership_insurance": False,
        "reference": "415250344",
        "prospectus_url": "https://dnbeiendom.no/415250344",
        "disposed": False,
    }
    for key, value in expected.items():
        assert a.get(key) == value, key
    assert a["viewing_note"].startswith("Velkommen til visning!")
    assert a["common_cost_includes"].startswith("Fyring, varmtvann, tv/internett")
    assert a["common_cost_includes"].endswith("felles bygningsforsikring.")
    assert a["preemption"].split("\n") == [
        "Ansiennitetsregler:",
        "1 - Andelseier i borettslaget",
        "2 - Storbyfellesskap (Bate, BOB, Vestbo)",
        "2 - Medlem i Usbl",
    ]
    assert a["area_description"].startswith("Boligbygg med flere boenheter\nBRA-i:\n1. etasje: 41 kvm")
    assert a["sales_costs_description"].startswith("Tinglysning av hjemmelsdok.: kr 545,-\n")
    for key in ("viewing_note", "common_cost_includes", "preemption", "area_description", "sales_costs_description"):
        assert not _TAG_RE.search(a[key]), key


def test_homes_images_are_the_ads_own_and_unique(homes):
    assert len(homes.images) == 30
    assert len(set(homes.images)) == 30
    assert homes.images[0].endswith("772_d044d84e-ad66-4468-98db-87611b6e064b.jpg")
    assert all(u.startswith("https://images.finncdn.no/") and "/459/322/772_" in u for u in homes.images)


def test_homes_without_scripts_falls_back_to_dom(homes, homes_dom):
    d = homes_dom
    assert d.title == homes.title
    assert d.price == 4_200_000
    assert d.location == "Dælenenggata 14A, 0567 Oslo"
    # Only the visible "Om boligen" text is in the DOM, but all of it.
    _assert_plain_text(d.description)
    assert d.description.startswith("DNB Eiendom v/ Marte Gabrielli har gleden av å presentere")
    assert d.description.endswith("* Kort vei til offentlig kommunikasjon")
    assert len(d.description) > 700
    assert homes.description.startswith(d.description)
    assert d.seller == homes.seller
    # The DOM has no publication date. "Sist endret" (rendered in UTC) is a
    # last-edit time and must not be passed off as one.
    assert d.published_at is None
    assert d.attributes["last_edited"] == "2026-04-13T09:22:00+00:00"
    assert datetime.fromisoformat(d.attributes["last_edited"]) == datetime.fromisoformat(
        homes.attributes["last_edited"]
    )
    # Same keys, same typed values as from the embedded data.
    same = (
        "property_type", "ownership_type", "bedrooms", "rooms", "floor", "construction_year",
        "usable_area_m2", "internal_bra_m2", "external_bra_m2", "plot_area_m2", "plot_owned",
        "total_price_nok", "shared_debt_nok", "collective_assets_nok", "asset_value_nok",
        "sales_costs_nok", "common_cost_monthly_nok", "energy_label", "local_area", "facilities",
        "viewings", "viewing_times", "viewing_note", "cadastre", "housing_cooperative",
        "housing_cooperative_org_number", "reference", "preemption", "area_description",
        "common_cost_includes", "sales_costs_description",
    )
    for key in same:
        assert d.attributes.get(key) == homes.attributes[key], key
    assert len(d.images) == 30
    assert len({re.sub(r"/dynamic/[^/]+/", "/", u) for u in d.images}) == 30


# ------------------------------------------------------------- lettings


def test_lettings_core_fields(lettings):
    assert lettings.vertical == "lettings"
    assert lettings.title == "Del av enebolig m/garasje og flott utsikt på florvåg."
    assert lettings.price == 23_000
    assert lettings.location == "Husavarden 17, 5305 Florvåg"


def test_lettings_description_is_full_plain_text(lettings):
    desc = lettings.description
    _assert_plain_text(desc)
    assert desc.startswith("Meget sentralt og romslig enebolig med 3 soverom")
    assert desc.endswith("leie del av kjeller også dersom man trenger mer plass.")
    assert len(desc) >= 540
    # <p> paragraphs become blank-line separated paragraphs.
    assert desc.split("\n\n")[1] == "Stor stue med god plass til både spisestue og stor salong."
    assert "Nytt kjøkken i 2026 fra IKEA. Monteres nå!\n\nHovedsoverom med egen terasse." in desc
    assert len(desc.split("\n\n")) == 7


def test_lettings_published_and_edited(lettings):
    assert lettings.published_at == datetime(
        2026, 4, 9, 9, 31, 58, 471071, tzinfo=timezone(timedelta(hours=2))
    )
    assert lettings.attributes["last_edited"] == "2026-04-16T20:33:03.194490+02:00"


def test_lettings_seller_is_private_without_invented_details(lettings):
    # The advertiser is a private user; the page shows neither a name nor a
    # phone number to visitors who are not logged in.
    assert lettings.seller == {"type": "private"}
    assert "92824705" not in lettings.model_dump_json()


def test_lettings_attributes(lettings):
    a = lettings.attributes
    expected = {
        "property_type": "Enebolig",
        "bedrooms": 3,
        "floor": 1,
        "primary_room_m2": 99,
        "open_area_m2": 35,
        "deposit_nok": 46_000,
        "rent_includes": "Internett",
        "furnished": "Umøblert",
        "animals_allowed": False,
        "available_from": "2026-05-01",
        "postal_code": "5305",
        "postal_name": "Florvåg",
        "facilities": [
            "Aircondition",
            "Balkong/Terrasse",
            "Bredbåndstilknytning",
            "Sentralt",
            "Barnevennlig",
            "Garasje/P-plass",
            "Turterreng",
            "Rolig",
            "Utsikt",
        ],
        "viewings": [{"date": "2026-04-19", "from": "18:00", "to": "19:30"}],
        "viewing_times": ["2026-04-19T16:00:00+00:00"],
        "viewing_note": "Ta kontakt for påmelding",
        "disposed": False,
    }
    for key, value in expected.items():
        assert a.get(key) == value, key
    assert a["lat"] == pytest.approx(60.4226, abs=1e-4)
    assert a["lon"] == pytest.approx(5.2336, abs=1e-4)
    assert "available_to" not in a  # open-ended rental period
    assert "Umøblert" not in a["facilities"]


def test_lettings_images(lettings):
    assert len(lettings.images) == 24
    assert len(set(lettings.images)) == 24
    assert all("/item/448510748/" in u for u in lettings.images)


def test_lettings_without_scripts_falls_back_to_dom(lettings, lettings_dom):
    d = lettings_dom
    assert d.title == lettings.title
    assert d.price == 23_000
    assert d.location == "Husavarden 17, 5305 Florvåg"
    assert d.description == lettings.description
    assert d.seller == {"type": "private"}
    assert d.published_at is None  # edited a week after publication; not a publish date
    assert d.attributes["last_edited"] == "2026-04-16T18:33:00+00:00"
    for key in (
        "property_type", "bedrooms", "floor", "primary_room_m2", "deposit_nok", "rent_includes",
        "furnished", "available_from", "facilities", "viewings", "viewing_times", "viewing_note",
    ):
        assert d.attributes.get(key) == lettings.attributes[key], key
    # Only this ad's gallery, not the "Lignende annonser" thumbnails.
    assert d.images and all("/item/448510748/" in u for u in d.images)


# ----------------------------------------------------- degraded pages


@pytest.mark.parametrize("vertical", ["homes", "lettings"])
@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><head><title>x</title></head><body></body></html>",
        "<html><body><h1>Bare tittel</h1><p>Ingen data</p></body></html>",
        # A payload that is not the expected shape.
        '<script>window.x.streamController.enqueue("[1,2,3]")</script>',
        '<script>window.x.streamController.enqueue("not json")</script>',
        '<script>window.x.streamController.enqueue("[{\\"_1\\":2},\\"objectData\\",\\"oops\\"]")</script>',
        '<script data-company-profile-data type="application/json">{broken</script>',
        '<section data-testid="about-property"></section><div data-testid="viewings"></div>'
        '<section data-testid="object-facilities"></section><section data-testid="object-info"></section>',
    ],
)
def test_parse_detail_never_raises_on_degraded_pages(vertical, html):
    listing = get_scraper(vertical).parse_detail("123456789", html)
    assert isinstance(listing, Listing)
    assert listing.finnkode == "123456789"
    assert listing.vertical == vertical
    assert listing.title
    assert listing.price is None
    assert listing.description is None
    assert listing.location is None
    assert listing.published_at is None
    assert listing.seller is None
    assert listing.images == []
    assert listing.attributes == {}


def test_minimal_page_uses_h1_title():
    listing = get_scraper("homes").parse_detail(
        "123456789", "<html><body><h1>Bare tittel</h1></body></html>"
    )
    assert listing.title == "Bare tittel"


def test_wrongly_typed_embedded_fields_do_not_break_the_dom_fallback():
    """Embedded data present but with unexpected types: the DOM still fills in."""
    html = read_fixture("lettings_ad.html")
    # Swap the hydration payload for one whose `ad` has nonsense types.
    payload = (
        '[{"_1":2},"objectData",{"_3":4,"_17":18},"ad",'
        '{"_5":6,"_7":8,"_9":10,"_11":12,"_13":14,"_15":16},'
        '"price","x","location",7,"viewings","no","facilities",{"_5":6},'
        '"propertyInfo",[6],"size",[6],"meta",[6]]'
    )
    script = "<script>window.__x.streamController.enqueue(" + json.dumps(payload) + ")</script>"
    html = _SCRIPT_RE.sub("", html) + script
    listing = get_scraper("lettings").parse_detail("448510748", html)
    assert listing.price == 23_000
    assert listing.location == "Husavarden 17, 5305 Florvåg"
    assert listing.description.startswith("Meget sentralt og romslig enebolig")
    assert listing.attributes["deposit_nok"] == 46_000
    assert listing.attributes["facilities"][0] == "Aircondition"


# ------------------------------------------------------- text helper


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<p>A &amp; B</p><p></p><p>C<br />D</p>", "A & B\n\nC\nD"),
        ("<ul><li>en</li><li>to</li></ul>", "- en\n- to"),
        ("Tekst med &#34;anførsel&#34; og&nbsp;mellomrom", 'Tekst med "anførsel" og mellomrom'),
        ("linje 1\nlinje 2", "linje 1\nlinje 2"),
        ("areal < 50 m2 og pris > 3", "areal < 50 m2 og pris > 3"),
        ("<b>Fet</b> tekst<br><br><br><br>slutt", "Fet tekst\n\nslutt"),
        (
            "<table><tr><th>Post</th><td>Sum</td></tr><tr><td>Leie</td><td>100</td></tr></table>",
            "Post Sum\nLeie 100",
        ),
        ("   ", None),
        (None, None),
    ],
)
def test_html_to_text(raw, expected):
    assert _html_to_text(raw) == expected
