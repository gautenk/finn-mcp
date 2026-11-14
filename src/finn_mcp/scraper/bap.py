from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterator

from selectolax.parser import HTMLParser, Node

from ..models import Listing, SearchResult
from .base import VerticalScraper, _clean, now_utc, parse_price_nok
from .jsonld import extract_jsonld, find_by_type

# Relative age shown on a card: "47 min.", "3 t.", "1 dg.", "2 uker", "3 mnd." ...
_AGE_RE = re.compile(r"^\d+\s*(?:sek|min|t|dg|d|uke|uker|mnd|år)\.{0,2}$", re.IGNORECASE)
_PRICE_SLOT_RE = re.compile(r"^(\d[\d ]*)\s*kr$", re.IGNORECASE)
_STORAGE_CHIP_RE = re.compile(r"^(\d+)\s*(GB|TB)$", re.IGNORECASE)
_IMAGE_COUNT_RE = re.compile(r"\b1 (?:of|av) (\d+)\b")
_HYDRATION_RE = re.compile(
    r"<script\b[^>]*\bdata-react-query-state\b[^>]*>([^<]+)</script>", re.IGNORECASE
)

# Texts that replace the price on cards that are not ordinary sales.
_TRADE_TYPES = ("Til salgs", "Gis bort", "Ønskes kjøpt")
# Badge text on the card -> extra key (only emitted when the badge is shown).
_BADGES = {
    "Fiks ferdig": "fiks_ferdig",
    "Fri frakt": "free_shipping",
    "Kjøp nå": "buy_now",
    "Betalt plassering": "paid_placement",
    "Solgt": "sold",
}
# Flag / label ids in the embedded search JSON -> extra key.
_FLAGS = {
    "shipping_exists": "fiks_ferdig",
    "fiks_ferdig": "fiks_ferdig",
    "seller_pays_shipping": "free_shipping",
    "buy_now": "buy_now",
    "sold": "sold",
}


def _next_element(node: Node | None, forward: bool = True) -> Node | None:
    cur = (node.next if forward else node.prev) if node is not None else None
    while cur is not None and cur.tag.startswith("-"):
        cur = cur.next if forward else cur.prev
    return cur


def _span_texts(node: Node | None) -> list[str]:
    if node is None:
        return []
    out = []
    for span in node.css("span"):
        t = _clean(span.text(strip=True))
        if t:
            out.append(t)
    return out


def _storage_gb(text: str) -> int | None:
    m = _STORAGE_CHIP_RE.match(text.strip())
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2).upper() == "TB" else n


def _walk_ads(obj: Any) -> Iterator[dict[str, Any]]:
    """Yield every ad-shaped dict (search doc or promoted entry) in a JSON tree."""
    if isinstance(obj, dict):
        if "heading" in obj and "canonical_url" in obj and ("id" in obj or "ad_id" in obj):
            yield obj
            return
        for v in obj.values():
            yield from _walk_ads(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_ads(v)


def hydrated_ads(html: str) -> dict[str, dict[str, Any]]:
    """Ads from the page's embedded (base64) react-query state, keyed by finnkode.

    Returns {} when the blob is missing or unreadable; callers fall back to DOM.
    """
    ads: dict[str, dict[str, Any]] = {}
    for m in _HYDRATION_RE.finditer(html):
        raw = m.group(1).strip()
        try:
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode("utf-8")
            state = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        for ad in _walk_ads(state):
            key = str(ad.get("id") or ad.get("ad_id") or "")
            if key:
                ads.setdefault(key, ad)
    return ads


class BapScraper(VerticalScraper):
    vertical = "bap"
    link_pattern = "/recommerce/forsale/item/"
    finnkode_re = re.compile(r"/recommerce/forsale/item/(\d+)")

    def search_url(
        self, query: str, page: int, filters: dict[str, str] | None
    ) -> tuple[str, dict[str, str]]:
        params: dict[str, str] = {"q": query}
        if page > 1:
            params["page"] = str(page)
        if filters:
            params.update(filters)
        return "https://www.finn.no/recommerce/forsale/search", params

    def detail_url(self, finnkode: str) -> str:
        return f"https://www.finn.no/recommerce/forsale/item/{finnkode}"

    # ----- search cards -----
    #
    # Card layout (both the April and November 2026 markup):
    #   article
    #     <images>
    #     div                      content
    #       div                    head:   div(price | "Gis bort" | "Ønskes kjøpt"), h2, div(chips)
    #       div                    footer: div(span location, span age), [div(dealer)], div(badges)
    #     span / w-badge           "Fiks ferdig", "Betalt plassering"
    #
    # The DOM gives what the card renders. The page also embeds the same result
    # list as JSON, which adds typed values the card only hints at (publish
    # timestamp, trade type, private/dealer). DOM values win; JSON fills gaps.

    def parse_search_cards(self, html: str) -> list[SearchResult]:
        results = super().parse_search_cards(html)
        ads = hydrated_ads(html)
        if ads:
            for r in results:
                ad = ads.get(r.finnkode)
                if ad:
                    self._merge_ad(r, ad)
        return results

    def _parse_card(self, finnkode: str, href: str, article: Node) -> SearchResult | None:
        result = super()._parse_card(finnkode, href, article)
        if result is None:
            return None
        slot = self._price_slot(article)
        if slot is not None:
            # The card has a dedicated price slot: trust it over the whole-card
            # regex, which misses "1 kr" and could pick up amounts in the title.
            m = _PRICE_SLOT_RE.match(slot)
            if m:
                result.price = int(m.group(1).replace(" ", ""))
            elif slot in _TRADE_TYPES:
                result.price = None
        return result

    @staticmethod
    def _heading(article: Node) -> Node | None:
        return article.css_first("h2") or article.css_first("h3")

    @classmethod
    def _price_slot(cls, article: Node) -> str | None:
        slot = _next_element(cls._heading(article), forward=False)
        if slot is None:
            return None
        return _clean(slot.text(separator=" ", strip=True))

    @staticmethod
    def _age_span(article: Node) -> Node | None:
        """The footer pairs <span>place</span><span>age</span>; anchor on the age."""
        for span in article.css("span"):
            age = _clean(span.text(strip=True))
            if not age or not _AGE_RE.match(age):
                continue
            place = _next_element(span, forward=False)
            if place is not None and place.tag == "span":
                return span
        return None

    @classmethod
    def _location_and_age(cls, article: Node) -> tuple[str | None, str | None]:
        span = cls._age_span(article)
        if span is None:
            return None, None
        age = _clean(span.text(strip=True)) or ""
        if age.endswith(".."):  # screen-reader full stop appended to "6 min."
            age = age[:-1]
        return _clean(_next_element(span, forward=False).text(strip=True)), age

    @classmethod
    def _dealer_name(cls, article: Node) -> str | None:
        """Dealer cards print the shop name in the footer, after place/age.

        The same footer also holds the badge row, so only a single leftover
        text that is not a known badge counts.
        """
        span = cls._age_span(article)
        row = span.parent if span is not None else None
        names: list[str] = []
        sib = _next_element(row)
        while sib is not None:
            for t in _span_texts(sib):
                if t.rstrip(".") not in _BADGES and any(c.isalnum() for c in t):
                    names.append(t)
            sib = _next_element(sib)
        return names[0] if len(names) == 1 else None

    def _guess_location(self, article: Node, text: str) -> str | None:
        return self._location_and_age(article)[0]

    def _card_extras(self, article: Node, text: str) -> dict[str, Any]:
        extras: dict[str, Any] = {}
        h = self._heading(article)

        slot = self._price_slot(article)
        if slot in _TRADE_TYPES:
            extras["trade_type"] = slot

        # Chips under the title: optional storage size, then brand.
        brands: list[str] = []
        for chip in _span_texts(_next_element(h)):
            gb = _storage_gb(chip)
            if gb is not None:
                extras["storage_gb"] = gb
            else:
                brands.append(chip)
        if len(brands) == 1:
            extras["brand"] = brands[0]
        if "storage_gb" not in extras:
            m = re.search(r"(\d+)\s*GB\b", text)
            if m:
                extras["storage_gb"] = int(m.group(1))

        _, age = self._location_and_age(article)
        if age:
            extras["published_text"] = age

        dealer = self._dealer_name(article)
        if dealer:
            extras["seller_name"] = dealer

        # Badges are whole text nodes outside the heading, so a title that
        # mentions "Fri frakt" or ends in "| Solgt" does not count.
        def parts(node: Node) -> list[str | None]:
            raw = node.text(separator="\x00", strip=True).split("\x00")
            return [_clean(part.rstrip(".")) for part in raw]

        segments = parts(article)
        for part in parts(h) if h is not None else []:
            if part in segments:
                segments.remove(part)
        for label, key in _BADGES.items():
            if label in segments:
                extras[key] = True

        for a in article.css("a[aria-label]"):
            m = _IMAGE_COUNT_RE.search(a.attributes.get("aria-label") or "")
            if m:
                extras["image_count"] = int(m.group(1))
                break
        return extras

    @staticmethod
    def _merge_ad(result: SearchResult, ad: dict[str, Any]) -> None:
        """Fill gaps in a DOM-parsed card from the matching embedded-JSON ad."""
        extra = result.extra

        trade = ad.get("trade_type")
        if isinstance(trade, str) and _clean(trade):
            extra.setdefault("trade_type", _clean(trade))

        price = ad.get("price")
        if result.price is None and isinstance(price, dict):
            amount = price.get("amount")
            # amount 0 is how give-aways / wanted ads are encoded, not a price.
            if isinstance(amount, (int, float)) and amount > 0:
                result.price = int(amount)

        if result.location is None and isinstance(ad.get("location"), str):
            result.location = _clean(ad["location"])
        image = ad.get("image")
        if result.thumbnail_url is None and isinstance(image, dict) and image.get("url"):
            result.thumbnail_url = str(image["url"])

        if "brand" not in extra and isinstance(ad.get("brand"), str) and _clean(ad["brand"]):
            extra["brand"] = _clean(ad["brand"])
        if "storage_gb" not in extra and isinstance(ad.get("memory_size"), str):
            gb = _storage_gb(ad["memory_size"])
            if gb is not None:
                extra["storage_gb"] = gb

        ts = ad.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            published = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            extra["published_at"] = published.isoformat().replace("+00:00", "Z")

        flags = ad.get("flags") if isinstance(ad.get("flags"), list) else []
        labels = ad.get("labels") if isinstance(ad.get("labels"), list) else []
        ids = {f for f in flags if isinstance(f, str)}
        ids |= {
            lab["id"] for lab in labels if isinstance(lab, dict) and isinstance(lab.get("id"), str)
        }
        if "retailer" in ids:
            extra["seller_type"] = "retailer"
        elif "private" in ids:
            extra["seller_type"] = "private"
        for flag, key in _FLAGS.items():
            if flag in ids:
                extra[key] = True

        # The JSON is authoritative on who sells; drop the DOM footer guess.
        extra.pop("seller_name", None)
        org = ad.get("organisation_name")
        if isinstance(org, str) and _clean(org):
            extra["seller_name"] = _clean(org)

        urls = ad.get("image_urls")
        if "image_count" not in extra and isinstance(urls, list) and urls:
            extra["image_count"] = len(urls)

    def parse_detail(self, finnkode: str, html: str) -> Listing:
        blocks = extract_jsonld(html)
        product = find_by_type(blocks, "Product")

        title: str | None = None
        description: str | None = None
        price: int | None = None
        currency = "NOK"
        images: list[str] = []
        attributes: dict[str, Any] = {}
        location: str | None = None

        if product:
            title = _clean(product.get("name"))
            description = _clean(product.get("description"))
            from .cars import _extract_images as _extract_images_helper
            images = _extract_images_helper(product.get("image"))
            brand = product.get("brand")
            if isinstance(brand, dict):
                attributes["brand"] = brand.get("name")
            elif isinstance(brand, str):
                attributes["brand"] = brand
            condition = product.get("itemCondition")
            if condition:
                attributes["condition"] = str(condition).rsplit("/", 1)[-1]
            offers = product.get("offers")
            if isinstance(offers, dict):
                price_raw = offers.get("price")
                try:
                    price = int(float(price_raw)) if price_raw is not None else None
                except (TypeError, ValueError):
                    price = None
                currency = offers.get("priceCurrency") or currency
                area = offers.get("areaServed")
                if isinstance(area, dict):
                    location = _clean(area.get("name"))
                elif isinstance(area, str):
                    location = _clean(area)
            for prop in product.get("additionalProperty") or []:
                if not isinstance(prop, dict):
                    continue
                name = prop.get("name")
                value = prop.get("value")
                if name:
                    attributes[str(name).lower()] = value

        if title is None:
            tree = HTMLParser(html)
            h1 = tree.css_first("h1")
            if h1:
                title = _clean(h1.text(strip=True))
        if price is None:
            price = parse_price_nok(html)

        return Listing(
            finnkode=finnkode,
            vertical=self.vertical,
            url=self.detail_url(finnkode),
            title=title or f"BAP listing {finnkode}",
            description=description,
            price=price,
            currency=currency,
            location=location,
            images=images,
            attributes=attributes,
            seller=None,
            published_at=None,
            fetched_at=now_utc(),
        )
