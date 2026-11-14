from __future__ import annotations

import base64
import html as html_lib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from selectolax.parser import HTMLParser, Node

from ..models import Listing, SearchResult
from .base import VerticalScraper, _clean, now_utc
from .cars import _extract_images
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


# ----- detail page helpers -----
#
# An ad page carries the same ad three times; they are read in this order and
# each one only fills what the previous ones left empty:
#   1. window.__staticRouterHydrationData (loaderData["item-recommerce"]): the
#      complete ad - full description, every image, postal code, edit time,
#      key facts ("extras"), trade type and shipping flags.
#   2. JSON-LD Product: title, price, schema.org condition, category. Its
#      description is cut at ~160 characters, so it is only a last resort.
#   3. DOM: h1, price slot, "Nøkkelinfo" chips, description section, address,
#      "Sist endret" line, gallery.
# The seller's name is never on the page for logged-out visitors.

_ROUTER_DATA_RE = re.compile(
    r'__staticRouterHydrationData\s*=\s*JSON\.parse\(\s*("(?:[^"\\]|\\.)*")\s*\)'
)
# Only real HTML tags, so "B<H" or "pris <500 kr> ..." in an ad text survives.
_TAG_RE = re.compile(
    r"</?(?:p|br|div|span|li|ul|ol|b|i|u|s|em|strong|a|h[1-6]|table|thead|tbody|tr|td|th"
    r"|section|blockquote|img|hr|small|sub|sup|font|pre|code|w-[a-z-]+)(?:\s[^>]*)?/?>",
    re.IGNORECASE,
)
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_BLOCK_END_RE = re.compile(
    r"</(?:p|div|li|ul|ol|h[1-6]|tr|table|section|blockquote)\s*>", re.IGNORECASE
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_EDITED_RE = re.compile(
    r"Sist endret\s*:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*kl\.\s*(\d{1,2})[:.](\d{2})"
)
_LENGTH_CM_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*cm$", re.IGNORECASE)
_TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*FINN[^|]*$")

# "Nøkkelinfo" chip label -> attribute key, matching the ids the embedded data
# uses for the same facts, so both sources produce the same keys.
_KEY_INFO_LABELS = {
    "Tilstand": "condition_label",
    "Merke": "brand",
    "Lagringsplass": "mobile_memory_size",
    "Modell": "mobile_model",
    "Bredde": "width",
}


def _plain_text(raw: Any) -> str | None:
    """Ad body as plain text: tags dropped, entities decoded, line breaks kept."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw
    markup = bool(_TAG_RE.search(text))
    if markup:
        text = _COMMENT_RE.sub("", text)
        text = _BREAK_RE.sub("\n", text)
        text = _LIST_ITEM_RE.sub("\n• ", text)
        text = _BLOCK_END_RE.sub("\n", text)
        text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = _clean(line) or ""
        # Markup: every block is one line. Plain text: the author's blank lines
        # stay, at most one in a row.
        if cleaned or (not markup and lines and lines[-1]):
            lines.append(cleaned)
    return "\n".join(lines).strip() or None


def _slug(label: str) -> str:
    out = label.lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return re.sub(r"[^a-z0-9]+", "_", out).strip("_")


def _node_text(node: Node | None) -> str | None:
    return _clean(node.text(separator=" ", strip=True)) if node is not None else None


def _typed_fact(attributes: dict[str, Any], key: str, value: str) -> None:
    """Add number-typed companions for facts whose text carries a unit."""
    m = _LENGTH_CM_RE.match(value)
    if m:
        number = float(m.group(1).replace(",", "."))
        attributes.setdefault(f"{key}_cm", int(number) if number.is_integer() else number)
    if key == "mobile_memory_size":
        gb = _storage_gb(value)
        if gb is not None:
            attributes.setdefault("storage_gb", gb)


def item_page_data(html: str) -> dict[str, Any]:
    """The ad page's embedded loader data ({} when missing or unreadable).

    Keys of interest: itemData, jsonLd, transactableData, transactableUiData,
    profileData, shopProfileData.
    """
    for m in _ROUTER_DATA_RE.finditer(html):
        try:
            state = json.loads(json.loads(m.group(1)))
        except (ValueError, TypeError):
            continue
        loader = state.get("loaderData") if isinstance(state, dict) else None
        if not isinstance(loader, dict):
            continue
        for route in (loader.get("item-recommerce"), *loader.values()):
            if isinstance(route, dict) and isinstance(route.get("itemData"), dict):
                return route
    return {}


def _category_path(category: Any) -> list[str]:
    names: list[str] = []
    for _ in range(10):
        if not isinstance(category, dict):
            break
        name = _clean(category["value"]) if isinstance(category.get("value"), str) else None
        if name:
            names.append(name)
        category = category.get("parent")
    return names[::-1]


def _profile_name(profile: Any) -> str | None:
    if not isinstance(profile, dict):
        return None
    for key in ("name", "displayName", "companyName"):
        value = profile.get(key)
        if isinstance(value, str) and _clean(value):
            return _clean(value)
    return None


def _oslo_time(year: int, month: int, day: int, hour: int, minute: int) -> datetime | None:
    """The page prints edit times in Norwegian local time, without a zone."""
    try:
        naive = datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    try:
        from zoneinfo import ZoneInfo

        return naive.replace(tzinfo=ZoneInfo("Europe/Oslo"))
    except Exception:  # no tz database on this machine: apply the EU summer-time rule
        pass

    def last_sunday(month: int) -> datetime:
        last = datetime(year, month, 31)
        return last - timedelta(days=(last.weekday() + 1) % 7)

    # Summer time runs from 02:00 local on the last Sunday of March to 03:00
    # local on the last Sunday of October.
    summer = last_sunday(3).replace(hour=2) <= naive < last_sunday(10).replace(hour=3)
    return naive.replace(tzinfo=timezone(timedelta(hours=2 if summer else 1)))


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

    # ----- ad page -----

    def parse_detail(self, finnkode: str, html: str) -> Listing:
        html = html or ""
        title: str | None = None
        description: str | None = None
        price: int | None = None
        currency = "NOK"
        location: str | None = None
        images: list[str] = []
        attributes: dict[str, Any] = {}
        seller: dict[str, Any] = {}
        published_at: datetime | None = None

        def put(key: str, value: Any) -> None:
            """First source wins; empty values are never stored."""
            if isinstance(value, str):
                value = _clean(value)
            if value is not None and key not in attributes:
                attributes[key] = value

        # 1. Embedded loader data.
        page = item_page_data(html)
        item = page.get("itemData") or {}
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        if item:
            if isinstance(item.get("title"), str):
                title = _clean(item["title"])
            description = _plain_text(item.get("description"))

            if isinstance(item.get("adViewTypeLabel"), str):
                put("trade_type", item["adViewTypeLabel"])
            amount = item.get("price")
            # 0 is how give-aways / wanted ads are encoded, not a price.
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
                price = int(amount)

            loc = item.get("location") if isinstance(item.get("location"), dict) else {}
            code = loc.get("postalCode") if isinstance(loc.get("postalCode"), str) else None
            place = loc.get("postalName") if isinstance(loc.get("postalName"), str) else None
            location = _clean(" ".join(part for part in (code, place) if part))
            put("postal_code", code)
            put("postal_place", place)

            pictures = item.get("images")
            for image in pictures if isinstance(pictures, list) else []:
                uri = image.get("uri") if isinstance(image, dict) else None
                if isinstance(uri, str) and uri and uri not in images:
                    images.append(uri)

            path = _category_path(item.get("category"))
            if path:
                put("category", " > ".join(path))
                if isinstance(item["category"].get("id"), int):
                    put("category_id", item["category"]["id"])

            facts = item.get("extras")
            for fact in facts if isinstance(facts, list) else []:
                if not isinstance(fact, dict):
                    continue
                value = fact.get("value")
                label = fact.get("label") if isinstance(fact.get("label"), str) else ""
                key = _slug(fact["id"]) if isinstance(fact.get("id"), str) else ""
                key = key or _KEY_INFO_LABELS.get(label) or _slug(label)
                if not key or isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    continue
                if key == "condition":
                    key = "condition_label"  # "condition" holds the schema.org value
                put(key, value)
                if label == "Merke":
                    put("brand", value)
                if isinstance(value, str):
                    _typed_fact(attributes, key, _clean(value) or "")

            if isinstance(meta.get("edited"), str):
                try:
                    published_at = datetime.fromisoformat(meta["edited"])
                except ValueError:
                    published_at = None
            if isinstance(item.get("disposed"), bool):
                put("sold", item["disposed"])
            if meta.get("isInactive") is True:
                put("inactive", True)

            if item.get("isWebstore") is True or isinstance(page.get("shopProfileData"), dict):
                seller["type"] = "retailer"
            elif meta.get("userOwner") is True:
                seller["type"] = "private"
            name = _profile_name(page.get("shopProfileData")) or _profile_name(
                page.get("profileData")
            )
            if name:
                seller["name"] = name
            if isinstance(meta.get("ownerId"), int):
                seller["owner_id"] = meta["ownerId"]

        shipping: list[str] = []
        deal = page.get("transactableData")
        if isinstance(deal, dict):
            for flag, key in (
                ("transactable", "fiks_ferdig"),
                ("sellerPaysShipping", "free_shipping"),
                ("buyNow", "buy_now"),
                ("eligibleForShipping", "eligible_for_shipping"),
            ):
                if isinstance(deal.get(flag), bool):
                    put(key, deal[flag])
            if deal.get("transactable") is True:
                shipping.append("Fiks ferdig")
        ui = page.get("transactableUiData")
        sections = ui.get("sections") if isinstance(ui, dict) else None
        sidebar = sections.get("sidebar") if isinstance(sections, dict) else None
        if isinstance(sidebar, dict):
            if "suggestShipping" in sidebar:
                put("fiks_ferdig_on_request", True)
            if "delivr" in sidebar:
                shipping.append("Delivr")

        # 2. JSON-LD Product (script tag, or the copy inside the loader data).
        product = find_by_type(extract_jsonld(html), "Product")
        if product is None and isinstance(page.get("jsonLd"), dict):
            product = find_by_type([page["jsonLd"]], "Product")
        teaser: str | None = None  # cut short by FINN; used only if nothing else exists
        if product:
            if title is None and isinstance(product.get("name"), str):
                title = _clean(product["name"])
            teaser = _plain_text(product.get("description"))
            if not images:
                images = _extract_images(product.get("image"))
            brand = product.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            if isinstance(brand, str):
                put("brand", brand)
            if product.get("itemCondition"):
                put("condition", str(product["itemCondition"]).rsplit("/", 1)[-1])
            offers = product.get("offers")
            if isinstance(offers, list):
                offers = next((o for o in offers if isinstance(o, dict)), None)
            if isinstance(offers, dict):
                if price is None and attributes.get("trade_type") in (None, "Til salgs"):
                    try:
                        amount = int(float(offers.get("price")))
                    except (TypeError, ValueError):
                        amount = 0
                    price = amount if amount > 0 else None
                if isinstance(offers.get("priceCurrency"), str) and offers["priceCurrency"]:
                    currency = offers["priceCurrency"]
                if offers.get("availability"):
                    put("availability", str(offers["availability"]).rsplit("/", 1)[-1])
                area = offers.get("areaServed")
                if isinstance(area, dict):
                    area = area.get("name")
                if location is None and isinstance(area, str):
                    location = _clean(area)
            props = product.get("additionalProperty")
            for prop in props if isinstance(props, list) else []:
                if not isinstance(prop, dict) or not isinstance(prop.get("name"), str):
                    continue
                value = prop.get("value")
                if _slug(prop["name"]) and isinstance(value, (str, int, float)):
                    put(_slug(prop["name"]), value)

        # 3. DOM, for whatever is still missing.
        tree = HTMLParser(html)
        heading = tree.css_first('[data-testid="object-title"]') or tree.css_first("h1")
        if title is None:
            title = _node_text(heading)
        if title is None:
            og = tree.css_first('meta[property="og:title"]')
            if og is not None:
                title = _clean(_TITLE_SUFFIX_RE.sub("", og.attributes.get("content") or ""))

        # Price slot next to the title: <h2>Til salgs</h2><p>2 500 kr</p>.
        box = heading.parent if heading is not None else None
        for h2 in box.css("h2") if box is not None else []:
            label = _node_text(h2)
            if label not in _TRADE_TYPES:
                continue
            put("trade_type", label)
            m = _PRICE_SLOT_RE.match(_node_text(_next_element(h2)) or "")
            if m and price is None and label == "Til salgs":
                price = int(m.group(1).replace(" ", "")) or None
            break

        if description is None:
            section = tree.css_first('[data-testid="description"]') or tree.css_first(
                'section[aria-label="Om annonsen"]'
            )
            if section is not None:
                body = section.css_first(".whitespace-pre-wrap") or section.css_first("div")
                description = _plain_text((body or section).html)
        if description is None:
            description = teaser

        if location is None:
            location = _node_text(tree.css_first('[data-testid="object-address"]'))

        for p in tree.css('section[aria-label="Nøkkelinfo"] p'):
            value = _node_text(p.css_first("b"))
            label = (_node_text(p) or "").split(":", 1)[0].strip()
            key = _KEY_INFO_LABELS.get(label) or _slug(label)
            if value and key:
                put(key, value)
                _typed_fact(attributes, key, value)

        if published_at is None:
            info = _node_text(tree.css_first('[data-testid="object-info"]')) or ""
            m = _EDITED_RE.search(info)
            if m:
                day, month, year, hour, minute = (int(g) for g in m.groups())
                published_at = _oslo_time(year, month, day, hour, minute)

        if not images:
            # Gallery slides first, then thumbnails; one URL per picture.
            seen: set[str] = set()
            for img in tree.css('[data-testid="image-gallery"] img, img[data-testid^="image-"]'):
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                picture = src.rsplit("/", 1)[-1]
                if src.startswith("http") and picture not in seen:
                    seen.add(picture)
                    images.append(src)

        if not isinstance(sidebar, dict):
            if tree.css_first('[data-testid="delivr"]') is not None:
                shipping.append("Delivr")
            if any(_node_text(b) == "Be om Fiks ferdig" for b in tree.css("button span")):
                put("fiks_ferdig_on_request", True)
        if shipping or isinstance(deal, dict) or isinstance(sidebar, dict):
            attributes["shipping_options"] = shipping

        # FINN prints only "Sist endret" (last changed); the page has no separate
        # publish time, so that is the time published_at carries.
        if published_at is not None:
            attributes["last_edited"] = published_at.isoformat()
        if seller.get("type"):
            attributes["seller_type"] = seller["type"]

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
            seller=seller or None,
            published_at=published_at,
            fetched_at=now_utc(),
        )
