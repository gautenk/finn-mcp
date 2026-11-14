"""Torget (bap) search cards: a search result must carry what the card shows."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from finn_mcp.models import SearchResult
from finn_mcp.scraper import get_scraper
from finn_mcp.scraper.bap import _HYDRATION_RE, hydrated_ads

FIXTURES = Path(__file__).parent / "fixtures"
NOV = "bap_search_2026_11.html"  # query "sofa", current markup
APR = "bap_search.html"  # query "iphone"

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(name: str, *, dom_only: bool = False) -> dict[str, SearchResult]:
    html = _html(name)
    if dom_only:
        html = _HYDRATION_RE.sub("", html)
        assert hydrated_ads(html) == {}
    results = get_scraper("bap").parse_search_cards(html)
    assert len(results) == 54
    return {r.finnkode: r for r in results}


def _count(cards: dict[str, SearchResult], key: str) -> int:
    if key in SearchResult.model_fields:
        return sum(getattr(r, key) is not None for r in cards.values())
    return sum(key in r.extra for r in cards.values())


@pytest.fixture(scope="module")
def nov() -> dict[str, SearchResult]:
    return _parse(NOV)


@pytest.fixture(scope="module")
def apr() -> dict[str, SearchResult]:
    return _parse(APR)


# ---------- November 2026 fixture ("sofa") ----------


def test_nov_order_is_unchanged(nov):
    assert list(nov)[:3] == ["480838766", "480920014", "480919817"]


def test_nov_promoted_card_with_one_krone_price(nov):
    # "1 kr" used to be missed by the generic price regex.
    r = nov["480838766"]
    assert r.title == "Stockholm sofa selges"
    assert r.price == 1
    assert r.location == "Ålesund"
    assert r.extra == {
        "brand": "IKEA",
        "published_text": "1 dg.",
        "published_at": "2026-11-12T16:50:21Z",
        "paid_placement": True,
        "image_count": 5,
        "trade_type": "Til salgs",
        "seller_type": "private",
    }


def test_nov_giveaway_card_has_no_price(nov):
    r = nov["480919817"]
    assert r.title == "Ektorp sofa gis bort!"
    assert r.price is None
    assert r.location == "Gjøvik"
    assert r.extra["trade_type"] == "Gis bort"
    assert r.extra["published_text"] == "57 min."
    assert r.extra["published_at"] == "2026-11-13T22:08:26Z"
    assert "brand" not in r.extra
    assert "paid_placement" not in r.extra


def test_nov_retailer_card(nov):
    r = nov["480725550"]
    assert r.price == 9499
    assert r.location == "Porsgrunn"
    assert r.extra["brand"] == "Bolia"
    assert r.extra["seller_type"] == "retailer"
    assert r.extra["seller_name"] == "SOFASENTRALEN"
    assert r.extra["trade_type"] == "Til salgs"


def test_nov_plain_private_card(nov):
    r = nov["480913232"]
    assert r.title == "Mørkegrå 3 seter sofa"
    assert r.price == 1000
    assert r.location == "Skedsmokorset"
    assert r.extra == {
        "published_text": "3 t.",
        "published_at": "2026-11-13T19:39:35Z",
        "image_count": 2,
        "trade_type": "Til salgs",
        "seller_type": "private",
    }


def test_nov_fill_rates(nov):
    for key in (
        "title",
        "location",
        "thumbnail_url",
        "published_text",
        "published_at",
        "trade_type",
        "seller_type",
        "image_count",
    ):
        assert _count(nov, key) == 54, key
    # The 11 cards without a price are all give-aways, which show no amount.
    assert _count(nov, "price") == 43
    no_price = [r for r in nov.values() if r.price is None]
    assert {r.extra["trade_type"] for r in no_price} == {"Gis bort"}
    assert sum(r.extra["trade_type"] == "Gis bort" for r in nov.values()) == 11
    assert _count(nov, "brand") == 17
    assert _count(nov, "seller_name") == 5
    assert _count(nov, "paid_placement") == 1


# ---------- April 2026 fixture ("iphone") ----------


def test_apr_order_is_unchanged(apr):
    assert list(apr)[:3] == ["460054796", "460211124", "460209096"]


def test_apr_promoted_card(apr):
    r = apr["460054796"]
    assert r.price == 10500
    assert r.location == "Tynset"
    assert r.extra == {
        "storage_gb": 256,
        "brand": "Apple",
        "published_text": "1 dg.",
        "published_at": "2026-04-15T18:29:14Z",
        "free_shipping": True,
        "buy_now": True,
        "fiks_ferdig": True,
        "paid_placement": True,
        "image_count": 10,
        "trade_type": "Til salgs",
        "seller_type": "private",
    }


def test_apr_terabyte_storage_chip(apr):
    r = apr["460211124"]
    assert r.title == "Apple iPhone 16 Pro 1 TB"
    assert r.price == 12175
    assert r.location == "Trøgstad"
    assert r.extra["storage_gb"] == 1024
    assert r.extra["brand"] == "Apple"
    assert r.extra["published_text"] == "6 min."
    for absent in ("fiks_ferdig", "buy_now", "free_shipping", "paid_placement"):
        assert absent not in r.extra


def test_apr_fiks_ferdig_and_free_shipping(apr):
    r = apr["460192405"]
    assert r.title == "Iphone 17"
    assert r.price == 9500
    assert r.location == "Fyllingsdalen"
    assert r.extra["fiks_ferdig"] is True
    assert r.extra["free_shipping"] is True
    assert r.extra["buy_now"] is True
    assert r.extra["storage_gb"] == 256

    # Shipping offered, but no "Kjøp nå" on the card.
    r = apr["460185707"]
    assert r.extra["fiks_ferdig"] is True
    assert "buy_now" not in r.extra
    assert "free_shipping" not in r.extra


def test_apr_wanted_cards(apr):
    # Shows "Ønskes kjøpt" where the price would be.
    r = apr["450513790"]
    assert r.price is None
    assert r.location == "Hamar"
    assert r.extra["trade_type"] == "Ønskes kjøpt"
    assert r.extra["brand"] == "IDEAL OF SWEDEN"
    assert "storage_gb" not in r.extra

    # A wanted ad that does state an amount keeps it.
    r = apr["460187577"]
    assert r.price == 13500
    assert r.extra["trade_type"] == "Ønskes kjøpt"


def test_apr_fill_rates(apr):
    for key in (
        "title",
        "location",
        "thumbnail_url",
        "brand",
        "published_text",
        "published_at",
        "trade_type",
        "seller_type",
        "image_count",
    ):
        assert _count(apr, key) == 54, key
    assert _count(apr, "price") == 53
    assert _count(apr, "free_shipping") == 3  # unchanged from before
    assert _count(apr, "storage_gb") >= 47  # was 47; "1 TB" chip now counted
    assert _count(apr, "fiks_ferdig") == 37
    assert _count(apr, "buy_now") == 36
    assert _count(apr, "paid_placement") == 1


# ---------- both fixtures ----------


@pytest.mark.parametrize("name", [NOV, APR])
def test_extra_values_are_typed(name):
    for r in _parse(name).values():
        assert ISO_UTC.match(r.extra["published_at"])
        assert isinstance(r.extra["image_count"], int) and r.extra["image_count"] >= 1
        assert r.extra["trade_type"] in {"Til salgs", "Gis bort", "Ønskes kjøpt"}
        assert r.extra["seller_type"] in {"private", "retailer"}
        if "storage_gb" in r.extra:
            assert isinstance(r.extra["storage_gb"], int)
        for flag in ("fiks_ferdig", "buy_now", "free_shipping", "paid_placement", "sold"):
            assert r.extra.get(flag, True) is True
        assert r.price is None or r.price > 0


@pytest.mark.parametrize("name", [NOV, APR])
def test_embedded_json_covers_every_card_and_agrees_with_dom(name):
    ads = hydrated_ads(_html(name))
    dom = _parse(name, dom_only=True)
    assert set(dom) <= set(ads)
    for finnkode, r in dom.items():
        ad = ads[finnkode]
        assert r.title == " ".join(ad["heading"].split())
        assert r.location == ad["location"]
        assert (r.price or 0) == ad["price"]["amount"]
        assert r.extra.get("brand") == ad.get("brand")


@pytest.mark.parametrize(
    ("name", "prices", "trade_types"), [(NOV, 43, 11), (APR, 53, 1)]
)
def test_dom_fallback_without_embedded_json(name, prices, trade_types):
    """If the hydration blob disappears, everything rendered is still parsed."""
    dom = _parse(name, dom_only=True)
    full = _parse(name)
    assert list(dom) == list(full)
    for key in ("title", "location", "thumbnail_url", "published_text"):
        assert _count(dom, key) == 54, key
    assert _count(dom, "price") == prices
    # Only non-sale cards print their trade type.
    assert _count(dom, "trade_type") == trade_types
    assert _count(dom, "published_at") == 0
    for finnkode, r in dom.items():
        assert r.price == full[finnkode].price
        for key in (
            "brand",
            "storage_gb",
            "free_shipping",
            "buy_now",
            "paid_placement",
            "seller_name",
            "image_count",
            "published_text",
        ):
            if key == "image_count" and key not in r.extra:
                continue  # single-image cards have no gallery label
            assert r.extra.get(key) == full[finnkode].extra.get(key), (finnkode, key)
        # "Fiks ferdig" is only missing from the DOM where the promoted card
        # shows "Betalt plassering" in the badge slot instead.
        if "paid_placement" not in r.extra:
            assert r.extra.get("fiks_ferdig") == full[finnkode].extra.get("fiks_ferdig")


def test_every_card_matches_its_own_text():
    """Independent of the parser: split each card into text nodes and compare."""
    from selectolax.parser import HTMLParser

    badge_keys = {
        "Fiks ferdig": "fiks_ferdig",
        "Fri frakt": "free_shipping",
        "Kjøp nå": "buy_now",
        "Betalt plassering": "paid_placement",
    }
    noise = {"Legg til som favoritt", "Gå til annonsen", "Lastebil i bevegelse", "•"}
    for name in (NOV, APR):
        html = _html(name)
        cards = _parse(name, dom_only=True)
        seen = 0
        for article in HTMLParser(html).css("article"):
            m = re.search(r"/recommerce/forsale/item/(\d+)", article.html or "")
            if not m:
                continue
            seen += 1
            r = cards[m.group(1)]
            segs = [
                " ".join(s.replace("\xa0", " ").split()).rstrip(".")
                for s in article.text(separator="\x00", strip=True).split("\x00")
            ]
            segs = [s for s in segs if s and s not in noise]
            head = segs.pop(0)
            pm = re.fullmatch(r"([\d ]+) kr", head)
            assert r.price == (int(pm.group(1).replace(" ", "")) if pm else None), r.finnkode
            assert pm or r.extra["trade_type"] == head, r.finnkode
            assert r.title.rstrip(".") == segs.pop(0), r.finnkode
            age = next(i for i, s in enumerate(segs) if re.fullmatch(r"\d+ (min|t|dg)", s))
            assert r.location == segs[age - 1], r.finnkode
            assert r.extra["published_text"] == segs[age] + ".", r.finnkode
            chips = segs[: age - 1]
            sizes = [c for c in chips if re.fullmatch(r"\d+ (GB|TB)", c)]
            assert [c for c in chips if c not in sizes] == (
                [r.extra["brand"]] if "brand" in r.extra else []
            ), r.finnkode
            if sizes:
                n, unit = sizes[0].split()
                assert r.extra["storage_gb"] == int(n) * (1024 if unit == "TB" else 1)
            tail = segs[age + 1 :]
            for label, key in badge_keys.items():
                assert (label in tail) == (key in r.extra), (r.finnkode, key)
            rest = [t for t in tail if t not in badge_keys]
            assert rest == ([r.extra["seller_name"]] if "seller_name" in r.extra else [])
            assert "sold" not in r.extra
        assert seen == 54


def test_badge_words_in_title_are_not_badges():
    html = _html(NOV).replace(
        "Bolia Scandinavia sofa med sjeselong | INKL LEVERING",
        "Bolia Scandinavia sofa med sjeselong | Solgt",
    )
    r = {c.finnkode: c for c in get_scraper("bap").parse_search_cards(html)}["480303772"]
    assert r.title.endswith("| Solgt")
    assert "sold" not in r.extra
    assert r.extra["seller_name"] == "DIVANO AS"


def test_malformed_hydration_blob_is_ignored():
    html = _html(NOV)
    broken = _HYDRATION_RE.sub(
        '<script type="application/json" data-react-query-state>not-base64!</script>', html
    )
    assert hydrated_ads(broken) == {}
    results = get_scraper("bap").parse_search_cards(broken)
    assert len(results) == 54
    assert results[0].price == 1
    assert results[0].location == "Ålesund"
