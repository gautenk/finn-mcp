"""Search-result card parsing for the real-estate verticals (homes, lettings).

Everything a result card shows should be on the SearchResult, so callers do
not have to fetch each listing. Works only from the saved fixture pages.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from finn_mcp.scraper import get_scraper
from finn_mcp.scraper.homes import _embedded_docs, _parse_nok_range

FIXTURES = Path(__file__).parent / "fixtures"

# Marker of the embedded result JSON; renaming it makes the page "DOM only".
_STREAM_MARKER = "streamController.enqueue("


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(vertical: str, name: str, *, dom_only: bool = False):
    html = _html(name)
    if dom_only:
        assert _STREAM_MARKER in html
        html = html.replace(_STREAM_MARKER, "streamController.disabled(")
    results = get_scraper(vertical).parse_search_cards(html)
    return results, {r.finnkode: r for r in results}


def _fill(results) -> Counter:
    c: Counter = Counter()
    for r in results:
        for field in ("title", "price", "location", "thumbnail_url"):
            if getattr(r, field) is not None:
                c[field] += 1
        for key, value in r.extra.items():
            if value is not None:
                c[key] += 1
    return c


@pytest.fixture(scope="module")
def homes():
    return _parse("homes", "homes_search.html")


@pytest.fixture(scope="module")
def lettings():
    return _parse("lettings", "lettings_search.html")


# ---------------------------------------------------------------- homes


def test_homes_order_and_count_unchanged(homes):
    results, _ = homes
    assert len(results) == 51
    assert [r.finnkode for r in results[:3]] == ["459322772", "459873155", "445920563"]


def test_homes_promoted_first_card(homes):
    r = homes[1]["459322772"]
    assert r.price == 4_200_000  # prisantydning, not totalpris
    assert r.location == "Dælenenggata 14A, Oslo"
    assert r.thumbnail_url and r.thumbnail_url.startswith("https://images.finncdn.no/")
    e = r.extra
    assert e["area_m2"] == 41
    assert e["bedrooms"] == 1
    assert e["property_type"] == "Leilighet"
    assert e["ownership_type"] == "Andel"
    assert e["total_price_nok"] == 4_221_894
    assert e["common_cost_monthly_nok"] == 4_401
    assert e["broker"] == "DNB Eiendom AS"
    assert e["seller_type"] == "professional"
    assert e["viewing"] == "19. april kl. 15:30"
    assert e["promotion"] == "Ukens bolig"
    assert e["local_area"] == "Rodeløkka"
    assert e["viewing_times"][0] == "2026-04-19T13:30:00+00:00"


def test_homes_location_is_address_not_broker(homes):
    # Broker name contains a comma and used to be picked up as the location.
    r = homes[1]["459873155"]
    assert r.location == "Dyre Halses gate 5, Trondheim"
    assert r.extra["broker"] == "EiendomsMegler 1 Trondheim sentrum, Kongens gate"
    assert r.price == 3_590_000
    assert r.extra["total_price_nok"] == 3_682_477
    assert r.extra["common_cost_monthly_nok"] == 3_024
    assert r.extra["ownership_type"] == "Selveier"
    assert r.extra["area_m2"] == 42
    assert r.extra["viewing"] == "27. april kl. 15:00"
    assert r.extra["viewing_times"] == ["2026-04-27T13:00:00+00:00"]
    assert r.extra["lat"] == pytest.approx(63.43666)
    assert r.extra["lon"] == pytest.approx(10.41395)


def test_homes_card_without_shared_costs(homes):
    # A title fragment ("2 stuer, 4 soverom, 3 bad") used to become the location.
    r = homes[1]["459462660"]
    assert r.location == "Steinskjellveien 9, Hommersåk"
    assert r.price == 6_990_000
    e = r.extra
    assert e["total_price_nok"] == 7_165_840
    assert "common_cost_monthly_nok" not in e  # card shows none
    assert (e["ownership_type"], e["property_type"], e["bedrooms"]) == ("Selveier", "Enebolig", 4)
    assert e["area_m2"] == 261
    assert e["plot_area_m2"] == 796


def test_homes_location_without_street_number(homes):
    # Previously None: the address has neither a comma nor a digit.
    assert homes[1]["442336541"].location == "Giske"
    assert homes[1]["459887989"].location == "Brattlia 11, Krokstadelva"
    # Previously carried a stray "| " from the title.
    assert homes[1]["460136127"].location == "Fløytarvegen 6, Verdal"


def test_homes_project_card_price_range(homes):
    r = homes[1]["408870373"]
    assert r.location == "Mistelpark leiligheter Trinn 3, Holmestrand"
    assert r.url == "https://www.finn.no/realestate/project/ad.html?finnkode=408870373"
    e = r.extra
    assert e["is_project"] is True
    # Card shows "1 495 000 - 4 475 000 kr": price is the "from" amount.
    assert r.price == 1_495_000
    assert (e["price_min_nok"], e["price_max_nok"]) == (1_495_000, 4_475_000)
    assert (e["total_price_min_nok"], e["total_price_max_nok"]) == (3_009_490, 8_979_410)
    assert "total_price_nok" not in e
    assert e["ownership_type"] == "Andel"
    assert e["property_type"] == "Leilighet"
    assert e["viewing"] == "etter avtale"
    assert "bedrooms" not in e
    assert (e["bedrooms_min"], e["bedrooms_max"]) == (1, 3)
    assert (e["area_m2"], e["area_m2_max"]) == (41, 100)


def test_homes_fill_rates(homes):
    results, _ = homes
    fill = _fill(results)
    n = len(results)
    for key in (
        "title",
        "price",
        "location",
        "thumbnail_url",
        "area_m2",
        "property_type",
        "ownership_type",
        "broker",
        "seller_type",
        "viewing",
        "timestamp",
        "lat",
        "lon",
    ):
        assert fill[key] == n, key
    # 47 single-unit cards + 4 project cards with ranges.
    assert fill["bedrooms"] == 47 and fill["bedrooms_min"] == 4
    assert fill["total_price_nok"] == 47 and fill["total_price_min_nok"] == 4
    assert fill["common_cost_monthly_nok"] >= 25
    assert fill["viewing_times"] >= 40
    assert fill["local_area"] >= 35


def test_homes_values_are_sane(homes):
    results, _ = homes
    brokers = {r.extra["broker"] for r in results}
    for r in results:
        e = r.extra
        assert r.location not in brokers
        assert r.location != r.title
        assert "|" not in r.location
        assert 300_000 <= r.price <= 50_000_000
        if "total_price_nok" in e:
            assert e["total_price_nok"] >= r.price  # total includes debt and fees
        assert 5 <= e["area_m2"] <= 1_000
        assert e["ownership_type"] in {"Selveier", "Andel", "Aksje", "Obligasjon"}
        assert isinstance(e.get("bedrooms", 0), int)
        assert e["timestamp"].startswith("2026-")


# -------------------------------------------------------------- lettings


def test_lettings_order_and_count_unchanged(lettings):
    results, _ = lettings
    assert len(results) == 51
    assert [r.finnkode for r in results[:3]] == ["448510748", "240280653", "118288134"]


def test_lettings_promoted_private_card(lettings):
    r = lettings[1]["448510748"]
    assert r.price == 23_000  # monthly rent
    assert r.location == "Husavarden 17, Florvåg"
    e = r.extra
    assert e["area_m2"] == 99
    assert e["bedrooms"] == 3
    assert e["property_type"] == "Enebolig"
    assert e["seller_type"] == "private"
    assert "broker" not in e
    assert e["promotion"] == "Betalt plassering"
    assert e["furnished"] == "Umøblert"
    assert e["viewing_times"] == ["2026-04-19T16:00:00+00:00"]
    assert "total_price_nok" not in e and "ownership_type" not in e


def test_lettings_room_in_shared_flat(lettings):
    # The title (with commas and digits) used to become the location, and
    # "Rom i bofellesskap" was not a recognised property type.
    r = lettings[1]["460131562"]
    assert r.location == "ØSTFOLDGATA 2, Oslo"
    assert r.price == 7_280
    assert r.extra["property_type"] == "Rom i bofellesskap"
    assert r.extra["area_m2"] == 9
    assert r.extra["bedrooms"] == 1


def test_lettings_agency_card(lettings):
    r = lettings[1]["311114959"]
    assert r.location == "Marcus Thranes Gate 18, Kristiansand S"  # was None
    assert r.price == 16_000
    assert r.extra["broker"] == "Hybel.no"
    assert r.extra["seller_type"] == "professional"
    assert r.extra["property_type"] == "Hybel"
    assert (r.extra["area_m2"], r.extra["bedrooms"]) == (60, 2)


def test_lettings_parking_card_has_no_area_or_bedrooms(lettings):
    r = lettings[1]["460196233"]
    assert r.location == "EUGENIES GATE 23, Oslo"
    assert r.price == 1_300
    assert r.extra["property_type"] == "Garasje/Parkering"
    assert "area_m2" not in r.extra
    assert "bedrooms" not in r.extra


def test_lettings_location_without_street_number(lettings):
    assert lettings[1]["407750970"].location == "Vegsundvågen, Ålesund"
    assert lettings[1]["459111277"].location == "Øystese"


def test_lettings_fill_rates(lettings):
    results, _ = lettings
    fill = _fill(results)
    n = len(results)
    for key in ("title", "price", "location", "property_type", "seller_type", "timestamp", "lat", "lon"):
        assert fill[key] == n, key
    assert fill["thumbnail_url"] >= n - 1  # one ad has no photo
    assert fill["area_m2"] == 49 and fill["bedrooms"] == 49  # all but 2 parking spaces
    assert fill["furnished"] >= 30
    assert fill["broker"] >= 10
    assert Counter(r.extra["seller_type"] for r in results)["private"] >= 30


def test_lettings_values_are_sane(lettings):
    results, _ = lettings
    for r in results:
        assert 500 <= r.price <= 200_000
        assert r.location != r.title
        assert "kr" not in r.location and "m²" not in r.location
        assert r.extra.get("furnished") in {None, "Møblert", "Umøblert", "Delvis møblert"}


# ------------------------------------------- DOM / embedded JSON agreement


@pytest.mark.parametrize(
    "vertical,name",
    [("homes", "homes_search.html"), ("lettings", "lettings_search.html")],
)
def test_dom_only_parse_keeps_card_fields(vertical, name):
    """Without the embedded JSON the visible card fields are all still there."""
    full, _ = _parse(vertical, name)
    dom, _ = _parse(vertical, name, dom_only=True)
    assert [r.finnkode for r in dom] == [r.finnkode for r in full]
    json_only = {
        "lat", "lon", "timestamp", "viewing_times", "local_area", "plot_area_m2",
        "furnished", "area_m2_max", "bedrooms_min", "bedrooms_max",
    }
    for d, f in zip(dom, full):
        assert (d.title, d.price, d.location, d.thumbnail_url) == (
            f.title, f.price, f.location, f.thumbnail_url,
        )
        assert not (json_only & d.extra.keys())
        for key, value in f.extra.items():
            if key in json_only:
                continue
            if key == "area_m2" and f.extra.get("is_project"):
                continue  # project cards show no area; it comes from the JSON
            assert d.extra.get(key) == value, (d.finnkode, key)


@pytest.mark.parametrize(
    "vertical,name",
    [("homes", "homes_search.html"), ("lettings", "lettings_search.html")],
)
def test_embedded_json_agrees_with_dom(vertical, name):
    """The embedded result list covers every card and matches what is rendered."""
    docs = _embedded_docs(_html(name))
    dom, _ = _parse(vertical, name, dom_only=True)
    assert {r.finnkode for r in dom} <= docs.keys()
    for r in dom:
        doc = docs[r.finnkode]
        assert " ".join(doc["location"].split()) == r.location
        price = doc.get("price_suggestion") or {}
        rng = doc.get("price_range_suggestion") or {}
        assert r.price == (price.get("amount") or rng.get("amount_from"))
        if "total_price_nok" in r.extra:
            assert r.extra["total_price_nok"] == doc["price_total"]["amount"]
        if "common_cost_monthly_nok" in r.extra:
            assert r.extra["common_cost_monthly_nok"] == doc["price_shared_cost"]["amount"]
        if "bedrooms" in r.extra:
            assert r.extra["bedrooms"] == doc["number_of_bedrooms"]
        if "area_m2" in r.extra:
            assert r.extra["area_m2"] == doc["area_range"]["size_from"]
        assert r.extra["property_type"] == doc["property_type_description"].strip()
        if "ownership_type" in r.extra:
            assert r.extra["ownership_type"] == doc["owner_type_description"].strip()
        if "broker" in r.extra:
            assert r.extra["broker"] == doc["organisation_name"].strip()


# --------------------------------------------------------------- helpers


def test_embedded_docs_tolerates_missing_or_broken_payload():
    assert _embedded_docs("<html><body>no payload</body></html>") == {}
    scraper = get_scraper("homes")
    broken = '<script>window.x.streamController.enqueue("not json at all")</script>'
    assert scraper.parse_search_cards(broken) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("4\xa0200\xa0000\xa0kr", (4_200_000, None)),
        ("1\xa0495\xa0000 - 4\xa0475\xa0000\xa0kr", (1_495_000, 4_475_000)),
        ("Totalpris: 3\xa0009\xa0490 - 8\xa0979\xa0410\xa0kr", (3_009_490, 8_979_410)),
        ("Fellesutg.: 4\xa0401\xa0kr", (4_401, None)),
        ("Leilighet", (None, None)),
    ],
)
def test_parse_nok_range(text, expected):
    assert _parse_nok_range(text) == expected


# ------------------------------------------------ unknown layout fallback


def _shift_layout(html: str) -> str:
    """Wrap every heading in an extra <div>, so the cells are no longer its siblings."""
    return html.replace("<h2", "<div><h2").replace("</h2>", "</h2></div>")


@pytest.mark.parametrize(
    "vertical,name",
    [("homes", "homes_search.html"), ("lettings", "lettings_search.html")],
)
def test_embedded_json_overrules_guesses_when_layout_is_unknown(vertical, name):
    """If the card cells cannot be located, the text heuristics must not beat
    the embedded JSON (they pick brokers as locations, title numbers as area
    or bedrooms, and the upper bound of a price range)."""
    full, _ = _parse(vertical, name)
    shifted = get_scraper(vertical).parse_search_cards(_shift_layout(_html(name)))
    assert [r.finnkode for r in shifted] == [r.finnkode for r in full]
    for s, f in zip(shifted, full):
        assert (s.title, s.price, s.location, s.thumbnail_url) == (
            f.title, f.price, f.location, f.thumbnail_url,
        ), s.finnkode
        assert s.extra == f.extra, s.finnkode


@pytest.mark.parametrize(
    "vertical,name",
    [("homes", "homes_search.html"), ("lettings", "lettings_search.html")],
)
def test_unknown_layout_without_json_still_parses(vertical, name):
    html = _shift_layout(_html(name)).replace(_STREAM_MARKER, "streamController.disabled(")
    results = get_scraper(vertical).parse_search_cards(html)
    assert len(results) == 51
    for r in results:
        assert r.title and r.price
        assert all(not key.startswith("_") for key in r.extra)


def test_no_private_marker_keys_in_extra(homes, lettings):
    for results, _ in (homes, lettings):
        for r in results:
            assert all(not key.startswith("_") for key in r.extra)


@pytest.mark.parametrize(
    "vertical,name",
    [("homes", "homes_search.html"), ("lettings", "lettings_search.html")],
)
def test_every_visible_card_token_is_accounted_for(vertical, name):
    """Independent of the parser's cell logic: each text segment the card shows
    after the title must equal, or be contained in, a parsed value."""
    import re

    from selectolax.parser import HTMLParser

    html = _html(name)
    _, by_code = _parse(vertical, name)
    seen = 0
    for article in HTMLParser(html).css("article"):
        link = article.css_first('h2 a[href*="finnkode="]')
        if link is None:
            continue
        r = by_code[re.search(r"finnkode=(\d+)", link.attributes["href"]).group(1)]
        seen += 1
        e = r.extra
        spans = [
            " ".join(s.text(strip=True).replace("\xa0", " ").split())
            for s in article.css("span")
            if not s.css_first("span") and s.parent.tag not in ("a", "button")
        ]
        nums = {
            str(v)
            for k, v in e.items()
            if isinstance(v, int) and not isinstance(v, bool)
        } | {str(r.price)}
        words = {r.location, e.get("broker"), e.get("promotion"), e.get("ownership_type"), e.get("property_type")}
        if e.get("seller_type") == "private":
            words.add("Privat")
        for span in filter(None, spans):
            if span in words:
                continue
            for token in filter(None, (t.strip() for t in span.split("∙"))):
                if token in words:
                    continue
                if token.startswith("Visning"):
                    assert e["viewing"] in token, (r.finnkode, token)
                    continue
                digits = [d.replace(" ", "") for d in re.findall(r"\d[\d ]*", token)]
                assert digits, (r.finnkode, token)
                assert set(digits) <= nums, (r.finnkode, token)
    assert seen == 51


# ------------------------------------------- November 2026 pages (current markup)

_NOV_HOMES = "homes_search_2026_11.html"
_NOV_LETTINGS = "lettings_search_2026_11.html"
_needs_nov = pytest.mark.skipif(
    not (FIXTURES / _NOV_HOMES).exists() or not (FIXTURES / _NOV_LETTINGS).exists(),
    reason="November 2026 real-estate pages not saved",
)


@_needs_nov
def test_nov_homes_cards_parse_like_april():
    results, by_code = _parse("homes", _NOV_HOMES)
    assert len(results) == 51
    fill = _fill(results)
    for key in ("title", "price", "location", "thumbnail_url", "property_type",
                "ownership_type", "broker", "viewing", "timestamp"):
        assert fill[key] == 51, key
    r = results[0]
    assert (r.finnkode, r.price, r.location) == ("480311971", 5_590_000, "Freserveien 9, Oslo")
    assert r.extra["total_price_nok"] == 5_608_796
    assert r.extra["common_cost_monthly_nok"] == 5_688
    assert r.extra["viewing"] == "15. nov. kl. 14:15"
    assert r.extra["viewing_times"] == ["2026-11-15T13:15:00+00:00"]  # CET, not CEST
    assert r.extra["promotion"] == "Ukens bolig"
    # Project card: title mentions "Visningsuke", the card says "Visning etter avtale".
    p = by_code["270626010"]
    assert (p.price, p.extra["price_max_nok"], p.extra["viewing"]) == (3_398_000, 5_548_000, "etter avtale")


@_needs_nov
def test_nov_homes_parking_space_plot_size_is_not_area():
    # The card's area cell shows "6 161 m²", which is the plot of the whole
    # garage facility; the ad itself has no floor area.
    r = _parse("homes", _NOV_HOMES)[1]["480755633"]
    assert r.extra["property_type"] == "Garasje/Parkering"
    assert "area_m2" not in r.extra
    assert r.extra["plot_area_m2"] == 6_161
    assert r.price == 900_000


@_needs_nov
def test_nov_lettings_partner_ads_and_missing_position():
    results, by_code = _parse("lettings", _NOV_LETTINGS)
    assert len(results) == 51
    assert results[0].finnkode == "291082239"
    for r in results:
        assert 500 <= r.price <= 200_000
        assert r.location and "m²" not in r.location and " kr" not in r.location
        # 0/0 means "no position"; it must not be reported as coordinates.
        assert (r.extra.get("lat"), r.extra.get("lon")) != (0.0, 0.0)
    assert "lat" not in by_code["280124090"].extra
    qasa = by_code["480921391"]
    assert qasa.extra["broker"] == "Qasa"
    assert qasa.location == "Etterstadkroken 7D, Oslo"


@_needs_nov
@pytest.mark.parametrize("vertical,name", [("homes", _NOV_HOMES), ("lettings", _NOV_LETTINGS)])
def test_nov_unknown_layout_matches_normal_parse(vertical, name):
    full, _ = _parse(vertical, name)
    shifted = get_scraper(vertical).parse_search_cards(_shift_layout(_html(name)))
    for s, f in zip(shifted, full, strict=True):
        assert (s.finnkode, s.price, s.location) == (f.finnkode, f.price, f.location)
        assert s.extra == f.extra, s.finnkode
