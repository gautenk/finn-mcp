from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any

from selectolax.parser import HTMLParser, Node

from ..models import Listing, SearchResult
from .base import VerticalScraper, _clean, now_utc, parse_price_nok
from .jsonld import extract_jsonld, find_by_type


# Search-card spec line: "2024 ∙ 65 000 km ∙ Plug-in Bensin ∙ 86 km rekkevidde".
# Tokens are classified by shape, not by position.
_SEP_RE = re.compile(r"\s*[∙•·]\s*")
_YEAR_TOKEN_RE = re.compile(r"^(?:19|20)\d{2}$")
_KM_TOKEN_RE = re.compile(r"^(\d[\d ]*) ?km$", re.IGNORECASE)
_RANGE_TOKEN_RE = re.compile(r"^(\d[\d ]*) ?km rekkevidde$", re.IGNORECASE)
_WARRANTY_TOKEN_RE = re.compile(r"^(\d+) ?mnd\.? garanti$", re.IGNORECASE)
_TRANSMISSIONS = {"automat": "Automat", "manuell": "Manuell"}
_SELLER_TYPES = {
    "privat": "Privat",
    "forhandler": "Forhandler",
    "merkeforhandler": "Merkeforhandler",
}
_SMIDIG = "Smidig bilhandel"
_PROMOTED = "Betalt plassering"
_HYDRATION_RE = re.compile(
    r"<script\b[^>]*\bdata-react-query-state\b[^>]*>([^<]+)</script>", re.IGNORECASE
)


def _extract_images(value: Any) -> list[str]:
    """schema.org image fields may be a string, list, or list of ImageObject dicts."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        url = value.get("contentUrl") or value.get("url")
        return [str(url)] if url else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_extract_images(item))
        return out
    return []


# ----- search-card helpers -----


def _node_text(node: Node) -> str | None:
    return _clean(node.text(separator=" ", strip=True))


def _element_children(node: Node) -> list[Node]:
    return [c for c in node.iter() if c.tag and not c.tag.startswith(("-", "_"))]


def _content_children(article: Node) -> list[Node]:
    """Direct element children of the card's content block (the heading's parent)."""
    content = article.css_first('[class*="search-ad-card-content"]')
    if content is None:
        h = article.css_first("h2") or article.css_first("h3")
        content = h.parent if h is not None else None
    if content is None:
        return []
    return _element_children(content)


def _price_div(children: list[Node]) -> Node | None:
    """The price element: first <div> after the spec <span> that holds an amount."""
    spec_seen = False
    for c in children:
        if c.tag == "span":
            spec_seen = True
        elif spec_seen and c.tag == "div" and parse_price_nok(_node_text(c)) is not None:
            return c
    return None


def _parse_spec_line(line: str) -> dict[str, Any]:
    """'2024 ∙ 65 000 km ∙ Plug-in Bensin ∙ 86 km rekkevidde' -> typed fields."""
    out: dict[str, Any] = {}
    for token in _SEP_RE.split(line):
        token = token.strip()
        if not token:
            continue
        if "year" not in out and _YEAR_TOKEN_RE.match(token):
            out["year"] = int(token)
        elif m := _RANGE_TOKEN_RE.match(token):
            out["range_km"] = int(m.group(1).replace(" ", ""))
        elif m := _KM_TOKEN_RE.match(token):
            out["mileage_km"] = int(m.group(1).replace(" ", ""))
        elif token.lower() in _TRANSMISSIONS:
            out["transmission"] = _TRANSMISSIONS[token.lower()]
        elif "fuel" not in out and not token[0].isdigit():
            # Whatever label FINN shows, verbatim: "El", "Diesel",
            # "Plug-in Bensin", "Hybrid bensin", ...
            out["fuel"] = token
    return out


def _has_seller_type(node: Node) -> bool:
    for span in node.css("span"):
        line = (_node_text(span) or "").lower()
        if any(t.strip(" –—-") in _SELLER_TYPES for t in _SEP_RE.split(line)):
            return True
    return False


def _parse_seller_block(article: Node) -> dict[str, Any]:
    """Location / seller line at the bottom of the card, plus trailing badges.

    First span: 'Hønefoss' or 'Hønefoss ∙ Bilia Hønefoss Volvo'.
    Second span: '[Smidig bilhandel – ]Privat|Forhandler|Merkeforhandler'
    followed by '∙'-separated perks ('22 mnd garanti', 'Bytterett', 'Service',
    'Tilstandsrapport').
    """
    out: dict[str, Any] = {}
    children = _content_children(article)
    price = _price_div(children)
    seller_div: Node | None = None
    if price is not None:
        idx = next(i for i, c in enumerate(children) if c.mem_id == price.mem_id)
        after = children[idx + 1 :]
        for c in after:
            if c.tag == "div" and c.css_first("span") is not None:
                seller_div = c
                break
    else:
        # No amount on the card (e.g. "Solgt"): find the seller line by its
        # seller-type token instead of by its position after the price.
        idx = next((i for i, c in enumerate(children) if c.tag == "span"), None)
        if idx is None:
            return out
        after = []
        for i, c in enumerate(children[idx + 1 :], start=idx + 1):
            if c.tag == "div" and _has_seller_type(c):
                seller_div = c
                after = children[i:]
                break
        if seller_div is None:
            return out
    badges: list[str] = []
    if seller_div is not None:
        # The two text spans sit in a wrapper <div> (css_first would match
        # seller_div itself, so walk the children explicitly).
        kids = _element_children(seller_div)
        inner = next((k for k in kids if k.tag == "div"), seller_div)
        spans = [s for s in _element_children(inner) if s.tag == "span"]
        if spans:
            parts = _SEP_RE.split(_node_text(spans[0]) or "", maxsplit=1)
            if parts and parts[0].strip():
                out["location"] = parts[0].strip()
            if len(parts) > 1 and parts[1].strip():
                out["seller_name"] = parts[1].strip()
        if len(spans) > 1:
            line = _node_text(spans[1]) or ""
            if _SMIDIG.lower() in line.lower():
                out["smidig_bilhandel"] = True
                line = re.sub(re.escape(_SMIDIG), "", line, flags=re.IGNORECASE)
                line = line.strip().lstrip("–—-").strip()
            for token in _SEP_RE.split(line):
                token = token.strip()
                low = token.lower()
                if not token:
                    continue
                if low in _SELLER_TYPES:
                    out["seller_type"] = _SELLER_TYPES[low]
                elif m := _WARRANTY_TOKEN_RE.match(token):
                    out["warranty_months"] = int(m.group(1))
                elif low == "bytterett":
                    out["exchange_right"] = True
                elif low == "service":
                    out["service_history"] = True
                elif low == "tilstandsrapport":
                    out["condition_report"] = True
                else:
                    badges.append(token)
    # Badge row(s) below the seller line, e.g. "Årets bruktbil 2026".
    for c in after:
        if c.tag != "div" or (seller_div is not None and c.mem_id == seller_div.mem_id):
            continue
        for leaf in c.css("div"):
            if _element_children(leaf):
                continue
            b = _node_text(leaf)
            if b and b not in badges:
                badges.append(b)
    if badges:
        out["badges"] = badges
    return out


def _hydration_entries(html: str) -> dict[str, dict[str, Any]]:
    """Result docs from the react-query hydration script, keyed by finnkode.

    Covers both the organic list (scope "search" -> docs) and the paid
    "pole position" card (scope "poleposition" -> results[].searchEntry).
    Returns {} if the script is missing or cannot be decoded.
    """
    m = _HYDRATION_RE.search(html)
    if not m:
        return {}
    raw = m.group(1).strip()
    try:
        state = json.loads(raw)
    except ValueError:
        try:
            state = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception:
            return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        for q in state.get("queries") or []:
            data = (q.get("state") or {}).get("data")
            if not isinstance(data, dict):
                continue
            docs = list(data.get("docs") or [])
            for r in data.get("results") or []:
                if isinstance(r, dict) and isinstance(r.get("searchEntry"), dict):
                    docs.append(r["searchEntry"])
            for d in docs:
                if not isinstance(d, dict):
                    continue
                key = d.get("ad_id") or d.get("id")
                if key is not None:
                    out.setdefault(str(key), d)
    except (AttributeError, TypeError):
        return {}
    return out


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _merge_hydration(result: SearchResult, entry: dict[str, Any]) -> None:
    """Fill gaps in a DOM-parsed card from its hydration-JSON doc.

    DOM values win; only missing fields are filled.
    """
    extra = result.extra

    def fill(key: str, value: Any) -> None:
        if value is None or value == "" or key in extra:
            return
        extra[key] = value

    def text(key: str) -> str | None:
        v = entry.get(key)
        return _clean(v) if isinstance(v, str) else None

    if result.location is None:
        result.location = text("location")
    if result.price is None:
        price = entry.get("price")
        if isinstance(price, dict):
            result.price = _as_int(price.get("amount"))
    if result.thumbnail_url is None:
        image = entry.get("image")
        if isinstance(image, dict) and isinstance(image.get("url"), str):
            result.thumbnail_url = image["url"]

    fill("make", text("make"))
    fill("model", text("model"))
    fill("subtitle", text("model_specification"))
    fill("year", _as_int(entry.get("year")))
    if entry.get("mileage_unit") in (None, "KILOMETER"):
        fill("mileage_km", _as_int(entry.get("mileage")))
    fill("fuel", text("fuel"))
    fill("transmission", text("transmission"))
    fill("range_km", _as_int(entry.get("driving_range")))
    fill("seller_type", text("dealer_segment"))
    fill("seller_name", text("organisation_name"))
    fill("warranty_months", _as_int(entry.get("warranty_duration")))
    docs = entry.get("service_documents")
    if isinstance(docs, list):
        if "Service" in docs:
            fill("service_history", True)
        if "Tilstandsrapport" in docs:
            fill("condition_report", True)
    flags = entry.get("flags")
    if isinstance(flags, list):
        if "right_to_exchange" in flags:
            fill("exchange_right", True)
        if "smooth_trade" in flags:
            fill("smidig_bilhandel", True)

    ts = _as_int(entry.get("timestamp"))
    if ts and "published_at" not in extra:
        try:
            extra["published_at"] = (
                datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
            )
        except (OverflowError, OSError, ValueError):
            pass


class _CarsBase(VerticalScraper):
    link_pattern = "/mobility/item/"
    finnkode_re = re.compile(r"/mobility/item/(\d+)")

    def detail_url(self, finnkode: str) -> str:
        return f"https://www.finn.no/mobility/item/{finnkode}"

    # ----- search cards -----
    #
    # A car card's content block looks like this (badges are optional):
    #
    #   [badge "Betalt plassering"]
    #   <h2>Volvo V90</h2>
    #   <div>T6 Plus Dark Krok, Barnesete++</div>                  subtitle
    #   <span>2024 ∙ 65 000 km ∙ Plug-in Bensin ∙ 86 km rekkevidde</span>
    #   <div>539 000 kr ...</div>                                  price
    #   <div><div><span>Hønefoss ∙ Bilia Hønefoss Volvo</span>
    #             <span>Merkeforhandler ∙ 22 mnd garanti ∙ Bytterett</span>
    #   </div></div>
    #   [<div><div>Årets bruktbil 2026</div></div>]                bottom badges
    #
    # The page also embeds the same result list as base64 JSON in a react-query
    # hydration script. The DOM is the primary source; the JSON fills in what
    # the card leaves out (e.g. transmission on cards that show range instead)
    # and is the fallback if the markup changes.

    def parse_search_cards(self, html: str) -> list[SearchResult]:
        results = super().parse_search_cards(html)
        entries = _hydration_entries(html)
        if entries:
            for r in results:
                entry = entries.get(r.finnkode)
                if entry:
                    _merge_hydration(r, entry)
        return results

    def _parse_card(self, finnkode: str, href: str, article: Node) -> SearchResult | None:
        result = super()._parse_card(finnkode, href, article)
        if result is None:
            return None
        # The base class regexes the whole card text for a price, which could
        # pick up amounts in the free-text subtitle ("... KR 6.100,-/MND").
        # Prefer the dedicated price element.
        children = _content_children(article)
        price_div = _price_div(children)
        if price_div is not None:
            price = parse_price_nok(_node_text(price_div))
            if price is not None:
                result.price = price
        elif any(c.tag == "span" for c in children):
            # Recognised card without a price element (e.g. "Solgt"): do not
            # keep an amount guessed from the title/subtitle text.
            result.price = None
        return result

    def _guess_location(self, article: Node, text: str) -> str | None:
        return _parse_seller_block(article).get("location")

    def _card_extras(self, article: Node, text: str) -> dict[str, Any]:
        extras: dict[str, Any] = {}
        children = _content_children(article)
        h_idx = next((i for i, c in enumerate(children) if c.tag in ("h2", "h3")), None)

        badges: list[str] = []
        if h_idx is not None:
            # Badges above the heading ("Betalt plassering").
            for c in children[:h_idx]:
                b = _node_text(c)
                if b:
                    badges.append(b)
            # Subtitle: the leaf <div> right after the heading.
            if h_idx + 1 < len(children):
                sub = children[h_idx + 1]
                if sub.tag == "div" and not _element_children(sub):
                    subtitle = _node_text(sub)
                    if subtitle:
                        extras["subtitle"] = subtitle

        spec = next((c for c in children if c.tag == "span"), None)
        if spec is not None:
            extras.update(_parse_spec_line(_node_text(spec) or ""))

        seller = _parse_seller_block(article)
        for key in (
            "seller_name",
            "seller_type",
            "warranty_months",
            "exchange_right",
            "service_history",
            "condition_report",
            "smidig_bilhandel",
        ):
            if key in seller:
                extras[key] = seller[key]
        for b in seller.get("badges", []):
            if b not in badges:
                badges.append(b)

        if _PROMOTED in badges:
            badges.remove(_PROMOTED)
            extras["promoted"] = True
        if badges:
            extras["badges"] = badges
        return extras

    def parse_detail(self, finnkode: str, html: str) -> Listing:
        blocks = extract_jsonld(html)
        product = find_by_type(blocks, "Product", "Car", "Vehicle")

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
            images = _extract_images(product.get("image"))
            brand = product.get("brand")
            if isinstance(brand, dict):
                attributes["brand"] = brand.get("name")
            elif isinstance(brand, str):
                attributes["brand"] = brand
            if product.get("model"):
                attributes["model"] = product["model"]
            offers = product.get("offers")
            if isinstance(offers, dict):
                try:
                    raw = offers.get("price")
                    price = int(float(raw)) if raw is not None else None
                except (TypeError, ValueError):
                    price = None
                currency = offers.get("priceCurrency") or currency

        # Supplement from DOM
        tree = HTMLParser(html)
        if title is None:
            h1 = tree.css_first("h1")
            if h1:
                title = _clean(h1.text(strip=True))

        # Pull any labelled attribute rows (year, mileage, fuel, transmission, etc.)
        for tid, key in (
            ("year", "year"),
            ("mileage", "mileage_km"),
            ("fuel", "fuel"),
            ("transmission", "transmission"),
            ("power", "power_hp"),
            ("color", "color"),
        ):
            node = tree.css_first(f'[data-testid="info-{tid}"]')
            if node is None:
                continue
            val = _clean(node.text(separator=" ", strip=True))
            if val:
                attributes.setdefault(key, val)

        if price is None:
            price = parse_price_nok(html)

        return Listing(
            finnkode=finnkode,
            vertical=self.vertical,
            url=self.detail_url(finnkode),
            title=title or f"Car listing {finnkode}",
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


class CarsUsedScraper(_CarsBase):
    vertical = "cars_used"

    def search_url(
        self, query: str, page: int, filters: dict[str, str] | None
    ) -> tuple[str, dict[str, str]]:
        params: dict[str, str] = {"q": query} if query else {}
        if page > 1:
            params["page"] = str(page)
        if filters:
            params.update(filters)
        return "https://www.finn.no/car/used/search.html", params


class CarsNewScraper(_CarsBase):
    vertical = "cars_new"

    def search_url(
        self, query: str, page: int, filters: dict[str, str] | None
    ) -> tuple[str, dict[str, str]]:
        # finn.no's /car/new/search.html is a JavaScript-rendered page and
        # does not expose listings in its initial HTML response. Scraping it
        # would require a real browser (Playwright) or the official API.
        raise NotImplementedError(
            "cars_new search is not supported by the scraper backend — "
            "the new-cars page is rendered client-side. Use cars_used or "
            "the official API backend once credentials are in place."
        )

    async def search(self, query, page=1, filters=None):  # type: ignore[override]
        raise NotImplementedError(
            "cars_new search is not supported by the scraper backend."
        )
