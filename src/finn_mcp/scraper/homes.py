from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from selectolax.parser import HTMLParser, Node

from ..models import Listing, SearchResult, Vertical
from .base import VerticalScraper, _clean, now_utc, parse_price_nok


# Map Norwegian attribute labels seen on real-estate detail pages to stable keys.
# Matched as the leading word(s) of the data-testid container text.
_HOMES_LABELS = {
    "Boligtype": "property_type",
    "Eieform": "ownership_type",
    "Soverom": "bedrooms",
    "Internt bruksareal": "internal_bra_m2",
    "Bruksareal": "usable_area_m2",
    "Eksternt bruksareal": "external_bra_m2",
    "Primærrom": "primary_room_m2",
    "Etasje": "floor",
    "Byggeår": "construction_year",
    "Energimerking": "energy_label",
    "Tomteareal": "plot_area",
    "Felleskost/mnd.": "common_cost_monthly_nok",
    "Totalpris": "total_price_nok",
    "Fellesgjeld": "shared_debt_nok",
    "Kommunale avg.": "municipal_fees_nok",
    "Formuesverdi": "asset_value_nok",
    "Tilbudsfrist": "offer_deadline",
    "Visning": "viewing",
}


_NUM_RE = re.compile(r"(\d[\d\s]*)")


def _strip_label(value: str, label: str) -> str:
    stripped = value
    if stripped.startswith(label):
        stripped = stripped[len(label):]
    return stripped.strip(" :\xa0")


def _extract_num(text: str) -> int | None:
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return int(re.sub(r"\s+", "", m.group(1)))
    except ValueError:
        return None


_OWNERSHIP_TYPES = {"Selveier", "Andel", "Aksje", "Obligasjon", "Eierseksjon"}
_PROMOTION_BADGES = {"Ukens bolig", "Betalt plassering"}


def _to_int(text: str) -> int:
    return int(re.sub(r"\s+", "", text.replace("\xa0", " ")))


def _node_text(node: Node | None) -> str | None:
    if node is None:
        return None
    return _clean(node.text(separator=" ", strip=True))


def _parse_nok_range(text: str) -> tuple[int | None, int | None]:
    """'1 495 000 - 4 475 000 kr' -> (1495000, 4475000); '4 200 000 kr' -> (4200000, None)."""
    compact = text.replace("\xa0", " ")
    m = re.search(r"(\d[\d ]*)\s*[-–]\s*(\d[\d ]*)\s*kr\b", compact, flags=re.I)
    if m:
        return _to_int(m.group(1)), _to_int(m.group(2))
    return parse_price_nok(compact), None


def _card_parts(article: Node) -> dict[str, Node | None] | None:
    """Locate the card's cells relative to the heading. None if no <h2>/<h3>."""
    h = article.css_first("h2") or article.css_first("h3")
    if h is None or h.parent is None:
        return None
    siblings = list(h.parent.iter())  # element children only
    idx = next((i for i, n in enumerate(siblings) if n.mem_id == h.mem_id), None)
    if idx is None:
        return None
    before = siblings[:idx]
    after = siblings[idx + 1 :]
    broker: Node | None = None
    if before:
        broker = before[-1].css_first("span") or before[-1]
    address = after[0] if after else None
    # If the cell after the heading looks like a price/area cell, the card has
    # no address cell (or the layout shifted); do not trust it as an address.
    if address is not None:
        t = (address.text(separator=" ", strip=True) or "").replace("\xa0", " ")
        if "m²" in t or re.search(r"\d\s*kr\b", t):
            address = None
    # The key-info cell is the first one after the heading holding an area or
    # a "kr" amount; the cell after it is the details block.
    key_info: Node | None = None
    details: Node | None = None
    start = 1 if address is not None else 0
    for i in range(start, len(after)):
        t = (after[i].text(separator=" ", strip=True) or "").replace("\xa0", " ")
        if "m²" in t or re.search(r"\d\s*kr\b", t):
            key_info = after[i]
            if i + 1 < len(after):
                details = after[i + 1]
            break
    return {"broker": broker, "address": address, "key_info": key_info, "details": details}


def _price_cell(parts: dict[str, Node | None] | None) -> str | None:
    if not parts or parts["key_info"] is None:
        return None
    for span in parts["key_info"].css("span"):
        t = _node_text(span)
        if t and re.search(r"\d", t) and re.search(r"\bkr\b", t, flags=re.I):
            return t
    return None


# ----- embedded result JSON (React Router "turbo-stream" payload) -----

_STREAM_RE = re.compile(r'streamController\.enqueue\(("(?:[^"\\]|\\.)*")\)')


def _embedded_docs(html: str) -> dict[str, dict[str, Any]]:
    """Return {finnkode: search doc} from the page's hydration payload.

    The payload is one flat JSON array; objects are ``{"_<key idx>": <value
    idx>}`` and arrays are lists of indices, negative indices being
    null/undefined sentinels. Returns {} when the payload is absent or shaped
    differently.
    """
    chunks = [json.loads(m.group(1)) for m in _STREAM_RE.finditer(html)]
    if not chunks:
        return {}
    flat = json.loads("".join(chunks).split("\n", 1)[0])
    if not isinstance(flat, list):
        return {}

    cache: dict[int, Any] = {}

    def hydrate(i: Any, depth: int = 0) -> Any:
        if isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= len(flat):
            return None
        if i in cache:
            return cache[i]
        if depth > 12:
            return None
        v = flat[i]
        if isinstance(v, dict):
            out: dict[str, Any] = {}
            cache[i] = out
            for k, ref in v.items():
                key = flat[int(k[1:])] if k[:1] == "_" and k[1:].isdigit() else k
                out[str(key)] = hydrate(ref, depth + 1)
            return out
        if isinstance(v, list):
            if v and isinstance(v[0], str):  # typed value such as ["D", ts]
                cache[i] = None
                return None
            lst: list[Any] = []
            cache[i] = lst
            lst.extend(hydrate(ref, depth + 1) for ref in v)
            return lst
        cache[i] = v
        return v

    key_idx = {v: i for i, v in enumerate(flat) if isinstance(v, str) and v in ("ad_id", "heading")}
    if len(key_idx) < 2:
        return {}
    need = {f"_{key_idx['ad_id']}", f"_{key_idx['heading']}"}
    docs: dict[str, dict[str, Any]] = {}
    for i, v in enumerate(flat):
        if isinstance(v, dict) and need <= v.keys():
            doc = hydrate(i)
            if isinstance(doc, dict) and doc.get("ad_id") is not None:
                docs.setdefault(str(doc["ad_id"]), doc)
    return docs


def _amount(value: Any, key: str = "amount") -> int | None:
    if isinstance(value, dict):
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def _iso(value: Any) -> str | None:
    """Normalise an ISO-8601 string or epoch-millis number to an ISO string."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        if isinstance(value, str):
            fixed = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
            return datetime.fromisoformat(fixed).isoformat()
    except (ValueError, OverflowError, OSError):
        return value if isinstance(value, str) else None
    return None


# Private marker in SearchResult.extra while a page is being parsed: names of
# fields that were not read from a recognised card cell but guessed from the
# flattened card text (unknown layout). Always removed before returning.
_GUESSED = "_guessed"


def _merge_embedded_doc(
    result: SearchResult, doc: dict[str, Any], guessed: frozenset[str] = frozenset()
) -> None:
    """Fill gaps in a DOM-parsed card from its embedded search doc.

    Values read from the visible card's own cells win; the doc only adds what
    is missing, plus typed fields the card does not render. Fields named in
    ``guessed`` came from text heuristics, not from a recognised cell, so for
    those the doc wins and the guess is kept only if the doc has nothing.
    """
    extra = result.extra
    held = {k: extra.pop(k) for k in ("area_m2", "bedrooms", "property_type") if k in guessed and k in extra}
    held_price, held_location = result.price, result.location
    if "price" in guessed:
        result.price = None
    if "location" in guessed:
        result.location = None

    def put(key: str, value: Any) -> None:
        if value is not None and value != "" and extra.get(key) is None:
            extra[key] = value

    def text(key: str) -> str | None:
        v = doc.get(key)
        return _clean(v) if isinstance(v, str) else None

    if not result.location:
        result.location = text("location")
    if not result.thumbnail_url and isinstance(doc.get("image"), dict):
        result.thumbnail_url = doc["image"].get("url") or None

    # Price: single amount, or a from/to range on new-build project cards.
    rng = doc.get("price_range_suggestion")
    low, high = _amount(rng, "amount_from"), _amount(rng, "amount_to")
    if result.price is None:
        result.price = _amount(doc.get("price_suggestion")) or low
    if low is not None and high is not None and high != low:
        put("price_min_nok", low)
        put("price_max_nok", high)

    total = _amount(doc.get("price_total"))
    if total is not None and total != result.price:  # lettings repeat the rent here
        put("total_price_nok", total)
    rng = doc.get("price_range_total")
    low, high = _amount(rng, "amount_from"), _amount(rng, "amount_to")
    if low is not None and high is not None and "total_price_nok" not in extra:
        put("total_price_min_nok", low)
        put("total_price_max_nok", high)
    put("common_cost_monthly_nok", _amount(doc.get("price_shared_cost")))

    area = doc.get("area_range")
    a_from, a_to = _amount(area, "size_from"), _amount(area, "size_to")
    if a_from is None:
        a_from = _amount(doc.get("area"), "size")
    if "area_m2" not in extra and a_from is not None:
        extra["area_m2"] = a_from
        if a_to is not None and a_to != a_from:
            extra["area_m2_max"] = a_to
    plot = _amount(doc.get("area_plot"), "size")
    put("plot_area_m2", plot)
    # Ads without a floor area (parking spaces, plots) render the plot size in
    # the card's area cell; do not report that as the unit's own area.
    if a_from is None and plot is not None and extra.get("area_m2") == plot:
        del extra["area_m2"]

    if "bedrooms_min" not in extra:
        put("bedrooms", _amount(doc.get("number_of_bedrooms")))
    if "bedrooms" not in extra:
        rng = doc.get("bedrooms_range")
        put("bedrooms_min", _amount(rng, "start"))
        put("bedrooms_max", _amount(rng, "end"))

    put("ownership_type", text("owner_type_description"))
    put("property_type", text("property_type_description"))
    put("local_area", text("local_area_name"))
    put("furnished", text("furnished_state"))

    flags = doc.get("flags") if isinstance(doc.get("flags"), list) else []
    org = text("organisation_name") or text("external_partner_name")
    # Same rule as the card: an organisation name is shown in the seller slot
    # whenever there is one (also for ads placed through Hybel.no and similar,
    # which carry the "private" flag as well); "Privat" only without one.
    if org:
        put("broker", org)
        put("seller_type", "professional")
    elif "private" in flags:
        put("seller_type", "private")

    times = [t for t in (_iso(v) for v in doc.get("viewing_times") or []) if t]
    if times:
        put("viewing_times", times)
    put("timestamp", _iso(doc.get("timestamp")))

    coords = doc.get("coordinates")
    if isinstance(coords, dict):
        lat, lon = coords.get("lat"), coords.get("lon")
        numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon))
        if numeric and (lat, lon) != (0, 0):  # 0/0 is finn's "no position"
            put("lat", float(lat))
            put("lon", float(lon))

    if doc.get("type") == "realestate_project":
        put("is_project", True)

    # Keep a guess only where the doc had nothing to offer.
    if result.price is None:
        result.price = held_price
    if not result.location:
        result.location = held_location
    for key, value in held.items():
        if key == "bedrooms" and "bedrooms_min" in extra:
            continue
        if key == "area_m2" and value == plot:
            continue
        extra.setdefault(key, value)


class _RealEstateBase(VerticalScraper):
    vertical: Vertical  # set by subclass

    finnkode_re = re.compile(r"finnkode=(\d+)")
    link_pattern = "finnkode="

    # ------------------------------------------------------------------
    # Search-result cards
    #
    # Card layout (April 2026), anchored on the <h2> that holds the ad link:
    #
    #   <div>                      <- "info" container (h2.parent)
    #     <div><span>broker | "Privat"</span></div>
    #     <h2><a href=...>local area - heading</a></h2>
    #     <div><span>street address, place</span></div>
    #     <div><span>41 m²</span><span>4 200 000 kr</span></div>
    #     <div>
    #       <div><span>Totalpris: … ∙ Fellesutg.: …</span>
    #            <span>Andel ∙ Leilighet ∙ 1 soverom</span></div>
    #       <div><span>Visning - 19. april kl. 15:30</span></div>
    #     </div>
    #   </div>
    #   <span>Ukens bolig | Betalt plassering</span>   (promoted cards only)
    #
    # The page also embeds the result list as JSON (React Router turbo-stream),
    # which carries the same values typed, plus a few the card does not render
    # (coordinates, plot size, ISO viewing times, furnishing). The DOM is the
    # primary source; the JSON fills gaps and adds the typed extras. Either
    # source may disappear without breaking the other.
    # ------------------------------------------------------------------

    def parse_search_cards(self, html: str) -> list[SearchResult]:
        results = super().parse_search_cards(html)
        try:
            docs = _embedded_docs(html)
        except Exception:  # embedded data is best-effort only
            docs = {}
        for result in results:
            guessed = frozenset(result.extra.pop(_GUESSED, ()))
            doc = docs.get(result.finnkode)
            if doc:
                snapshot = (result.price, result.location, dict(result.extra))
                try:
                    _merge_embedded_doc(result, doc, guessed)
                except Exception:
                    result.price, result.location, result.extra = snapshot
        return results

    def _parse_card(self, finnkode: str, href: str, article: Node) -> SearchResult | None:
        result = super()._parse_card(finnkode, href, article)
        if result is None:
            return None
        # The generic price regex scans the whole card text and, for project
        # cards showing "1 495 000 - 4 475 000 kr", lands on the upper bound.
        # Read the dedicated price cell instead; a range yields its lower bound
        # as `price` plus explicit min/max extras.
        parts = _card_parts(article)
        cell = _price_cell(parts)
        # Note which fields did not come from a recognised cell, so the
        # embedded JSON (when present) can overrule the text heuristics.
        guessed: set[str] = set()
        if not (parts and parts["address"] is not None and _node_text(parts["address"])):
            guessed.add("location")
        if not cell:
            guessed.add("price")
        if not parts or parts["key_info"] is None:
            guessed.add("area_m2")
        if not parts or parts["details"] is None:
            guessed.update(("bedrooms", "property_type"))
        if guessed:
            result.extra[_GUESSED] = guessed
        if cell:
            low, high = _parse_nok_range(cell)
            if low is not None:
                result.price = low
                if high is not None and high != low:
                    result.extra["price_min_nok"] = low
                    result.extra["price_max_nok"] = high
        if "/project/" in href:
            result.extra["is_project"] = True
        return result

    def _guess_location(self, article: Node, text: str) -> str | None:
        # The address is the element right after the heading.
        parts = _card_parts(article)
        if parts and parts["address"] is not None:
            addr = _node_text(parts["address"])
            if addr:
                return addr
        # Fallback (unknown layout): a text segment shaped like
        # "Street 14A, Place", skipping the broker line and the title.
        broker = _node_text(parts["broker"]) if parts else None
        title = self._guess_title(article, text)
        for part in text.split(" | "):
            cleaned = _clean(part)
            if not cleaned or "kr" in cleaned.lower() or "m²" in cleaned:
                continue
            if cleaned == broker or (title and cleaned in title):
                continue
            if "," in cleaned and any(ch.isdigit() for ch in cleaned):
                return cleaned
        return None

    def _card_extras(self, article: Node, text: str) -> dict[str, Any]:
        extras: dict[str, Any] = {}
        parts = _card_parts(article)

        # --- area: the "41 m²" cell next to the price -----------------
        area_text = None
        if parts and parts["key_info"] is not None:
            for span in parts["key_info"].css("span"):
                t = _node_text(span)
                if t and "m²" in t:
                    area_text = t
                    break
            area_text = area_text or ""  # known layout, no area cell: do not guess
        m = re.search(
            r"(\d[\d ]*?)(?:\s*[-–]\s*(\d[\d ]*?))?\s*m²",
            (text if area_text is None else area_text).replace("\xa0", " "),
        )
        if m:
            extras["area_m2"] = _to_int(m.group(1))
            if m.group(2) and _to_int(m.group(2)) != extras["area_m2"]:
                extras["area_m2_max"] = _to_int(m.group(2))

        # --- details: "Totalpris ∙ Fellesutg. ∙ Eieform ∙ Type ∙ N soverom"
        details = parts["details"] if parts else None
        tokens: list[str] = []
        viewing: str | None = None
        if details is not None:
            for span in details.css("span"):
                t = _node_text(span)
                if not t:
                    continue
                if t.lower().startswith("visning"):
                    viewing = t
                    continue
                tokens.extend(tok for tok in (_clean(x) for x in t.split("∙")) if tok)
        plain: list[str] = []
        for tok in tokens:
            low = tok.lower()
            if low.startswith("totalpris"):
                lo, hi = _parse_nok_range(tok)
                if lo is not None and (hi is None or hi == lo):
                    extras["total_price_nok"] = lo
                elif lo is not None:
                    extras["total_price_min_nok"] = lo
                    extras["total_price_max_nok"] = hi
            elif low.startswith("fellesutg") or low.startswith("felleskost"):
                lo, _ = _parse_nok_range(tok)
                if lo is not None:
                    extras["common_cost_monthly_nok"] = lo
            elif re.fullmatch(r"\d+\s*soverom\w*", low):
                # \w* tolerates render glitches such as "3 soveromom".
                extras["bedrooms"] = int(re.match(r"\d+", low).group(0))
            elif re.fullmatch(r"\d+\s*[-–]\s*\d+\s*soverom\w*", low):
                a, b = re.findall(r"\d+", low)
                extras["bedrooms_min"], extras["bedrooms_max"] = int(a), int(b)
            elif ":" not in tok and not any(ch.isdigit() for ch in tok):
                plain.append(tok)
        # Remaining plain tokens are "<eieform> ∙ <boligtype>" (homes) or just
        # "<boligtype>" (lettings).
        if len(plain) >= 2:
            extras["ownership_type"], extras["property_type"] = plain[0], plain[1]
        elif len(plain) == 1:
            if plain[0] in _OWNERSHIP_TYPES:
                extras["ownership_type"] = plain[0]
            else:
                extras["property_type"] = plain[0]

        if details is None:
            # Unknown layout: previous regex heuristics over the card text.
            m = re.search(r"(\d+)\s*soverom", text)
            if m:
                extras["bedrooms"] = int(m.group(1))
            m = re.search(
                r"(Leilighet|Enebolig|Rekkehus|Tomannsbolig|Hytte|Gårdsbruk|Annet)",
                text,
            )
            if m:
                extras["property_type"] = m.group(1)
            m = re.search(r"\bVisning\b[^|]*", text)
            if m:
                viewing = _clean(m.group(0))

        if viewing:
            # "Visning - 19. april kl. 15:30" -> "19. april kl. 15:30";
            # "Visning etter avtale" -> "etter avtale".
            extras["viewing"] = (
                re.sub(r"^Visning\s*[-–:]?\s*", "", viewing, flags=re.I) or viewing
            )

        # --- broker / private seller ----------------------------------
        broker = _node_text(parts["broker"]) if parts else None
        if broker:
            if broker.lower() == "privat":
                extras["seller_type"] = "private"
            else:
                extras["broker"] = broker
                extras["seller_type"] = "professional"

        # --- promoted placement badge ---------------------------------
        for child in article.iter():
            if child.tag == "span":
                t = _node_text(child)
                if t in _PROMOTION_BADGES:
                    extras["promotion"] = t
                    break
        return extras

    def parse_detail(self, finnkode: str, html: str) -> Listing:
        tree = HTMLParser(html)

        # Title: the <h1> is the clean listing title.
        title: str | None = None
        h1 = tree.css_first("h1")
        if h1:
            title = _clean(h1.text(separator=" ", strip=True))

        # Address
        location: str | None = None
        addr_node = tree.css_first('[data-testid="object-address"]')
        if addr_node:
            location = _clean(addr_node.text(separator=" ", strip=True))

        # Attributes via data-testid buckets
        attributes: dict[str, Any] = {}
        for data_testid_prefix in (
            "info-",
            "pricing-",
            "common-",
            "energy-",
        ):
            for node in tree.css(f'[data-testid^="{data_testid_prefix}"]'):
                value = _clean(node.text(separator=" ", strip=True))
                if not value:
                    continue
                for label, key in _HOMES_LABELS.items():
                    if value.startswith(label):
                        raw = _strip_label(value, label)
                        if key.endswith("_nok") or key.endswith("_m2") or key in {
                            "bedrooms",
                            "construction_year",
                            "floor",
                        }:
                            n = _extract_num(raw)
                            if n is not None:
                                attributes.setdefault(key, n)
                            else:
                                attributes.setdefault(key, raw)
                        else:
                            attributes.setdefault(key, raw)
                        break

        # Price: prefer the asking price ("Prisantydning"). Note: finn.no
        # spells the testid "pricing-incicative-price" (sic, typo upstream).
        # For lettings, "Månedsleie" lives under pricing-common-monthly-cost.
        price: int | None = None
        for tid in (
            "pricing-incicative-price",
            "pricing-indicative-price",
            "pricing-common-monthly-cost",
            "pricing-details",
            "pricing-total-price",
        ):
            node = tree.css_first(f'[data-testid="{tid}"]')
            if node:
                price = parse_price_nok(node.text(separator=" ", strip=True))
                if price:
                    break

        # Description
        description: str | None = None
        desc_node = tree.css_first('[data-testid="about-property"]') or tree.css_first(
            '[data-testid="beskrivelse"]'
        )
        if desc_node:
            description = _clean(desc_node.text(separator=" ", strip=True))

        # Images: og:image + finncdn urls in HTML.
        images: list[str] = []
        og = tree.css_first('meta[property="og:image"]')
        if og:
            src = og.attributes.get("content")
            if src:
                images.append(src)
        for m in re.finditer(
            r'https://images\.finncdn\.no/dynamic/\d+w/[^"\'<> )]+',
            html,
        ):
            img = m.group(0)
            if img not in images:
                images.append(img)
            if len(images) >= 20:
                break

        # Broker / seller
        seller: dict[str, Any] | None = None
        broker_name = None
        broker_node = tree.css_first('[data-testid="broker-name"]')
        if broker_node:
            broker_name = _clean(broker_node.text(strip=True))
        if broker_name:
            seller = {"name": broker_name}

        return Listing(
            finnkode=finnkode,
            vertical=self.vertical,
            url=self.detail_url(finnkode),
            title=title or f"Real-estate listing {finnkode}",
            description=description,
            price=price,
            currency="NOK",
            location=location,
            images=images,
            attributes=attributes,
            seller=seller,
            published_at=None,
            fetched_at=now_utc(),
        )


class HomesScraper(_RealEstateBase):
    vertical = "homes"

    def search_url(
        self, query: str, page: int, filters: dict[str, str] | None
    ) -> tuple[str, dict[str, str]]:
        params: dict[str, str] = {}
        if query:
            params["q"] = query
        if page > 1:
            params["page"] = str(page)
        if filters:
            params.update(filters)
        return "https://www.finn.no/realestate/homes/search.html", params

    def detail_url(self, finnkode: str) -> str:
        return f"https://www.finn.no/realestate/homes/ad.html?finnkode={finnkode}"


class LettingsScraper(_RealEstateBase):
    vertical = "lettings"

    def search_url(
        self, query: str, page: int, filters: dict[str, str] | None
    ) -> tuple[str, dict[str, str]]:
        params: dict[str, str] = {}
        if query:
            params["q"] = query
        if page > 1:
            params["page"] = str(page)
        if filters:
            params.update(filters)
        return "https://www.finn.no/realestate/lettings/search.html", params

    def detail_url(self, finnkode: str) -> str:
        return f"https://www.finn.no/realestate/lettings/ad.html?finnkode={finnkode}"
