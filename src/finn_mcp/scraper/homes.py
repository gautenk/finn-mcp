from __future__ import annotations

import html as html_lib
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


# ----------------------------------------------------------------------
# Detail (single ad) pages
#
# Sources, in order of preference (April 2026 layout). Each one is optional
# and read independently; later sources only fill what earlier ones left out.
#
#   1. Embedded data: the same React Router "turbo-stream" payload as on the
#      search page. ``loaderData[<route>].objectData`` holds ``ad`` (typed
#      fields, every free-text section of the ad, viewings, facilities) and
#      ``meta`` (edit/broadcast timestamps, owner URN).
#   2. The broker's company-profile JSON (``<script data-company-profile-data>``)
#      for agency and contact persons. Real-estate ad pages carry no JSON-LD.
#   3. DOM: ``data-testid`` blocks with <dt>/<dd> pairs, the "Om boligen" /
#      "Beskrivelse" section, the facilities grid, viewing blocks and the
#      "Annonseinformasjon" table.
# ----------------------------------------------------------------------

_TAG_RE = re.compile(r"</?[a-zA-Z][^<>]*>")
_BLOCK_END_RE = re.compile(
    r"</(?:p|div|ul|ol|h[1-6]|tr|table|section|blockquote)\s*>", flags=re.I
)


def _html_to_text(value: Any) -> str | None:
    """HTML fragment (or plain text) -> readable plain text.

    Tags are removed, entities decoded, and <br>, paragraphs and list items
    become line breaks. Nothing is shortened.
    """
    if not isinstance(value, str):
        return None
    s = re.sub(r"(?is)<(script|style|svg)\b.*?</\1\s*>", "", value)
    s = re.sub(r"(?s)<!--.*?-->", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)<li\b[^<>]*>", "\n- ", s)
    s = re.sub(r"(?i)</t[dh]\s*>", " ", s)  # keep table cells apart
    s = _BLOCK_END_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html_lib.unescape(s)
    lines = [_clean(line) or "" for line in s.split("\n")]
    lines = ["" if line == "-" else line for line in lines]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def _stream_flat(html: str) -> list[Any] | None:
    chunks = [json.loads(m.group(1)) for m in _STREAM_RE.finditer(html)]
    if not chunks:
        return None
    flat = json.loads("".join(chunks).split("\n", 1)[0])
    return flat if isinstance(flat, list) else None


def _embedded_ad(html: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (ad, meta) from the detail page's hydration payload, or ({}, {})."""
    flat = _stream_flat(html)
    if not flat:
        return {}, {}
    cache: dict[int, Any] = {}

    def hydrate(i: Any, depth: int = 0) -> Any:
        if isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= len(flat):
            return None
        if i in cache:
            return cache[i]
        if depth > 30:
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

    try:
        key = f"_{flat.index('objectData')}"
    except ValueError:
        return {}, {}
    for v in flat:
        if isinstance(v, dict) and key in v:
            data = hydrate(v[key])
            if isinstance(data, dict) and isinstance(data.get("ad"), dict):
                meta = data.get("meta")
                return data["ad"], meta if isinstance(meta, dict) else {}
    return {}, {}


def _parse_dt(value: Any) -> datetime | None:
    """ISO-8601 string -> datetime (aware when the string carries a zone)."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    s = re.sub(r"Z$", "+00:00", s)
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # nanoseconds -> microseconds
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    """Typed number, or a purely numeric string such as "46000" -> int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[\s\xa0]", "", value)
        if re.fullmatch(r"\d+", digits):
            return int(digits)
    return None


def _text(value: Any) -> str | None:
    return _clean(value) if isinstance(value, str) else None


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def _ical_utc(value: Any) -> str | None:
    """'20260419T133000Z' -> '2026-04-19T13:30:00+00:00'."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _strip_tracking(url: Any) -> str | None:
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    base, _, query = url.partition("?")
    query, _, _fragment = query.partition("#")
    kept = [p for p in query.split("&") if p and not p.startswith("utm_")]
    return base + ("?" + "&".join(kept) if kept else "")


_FURNISHING = {"møblert", "umøblert", "delvis møblert"}

_SIZE_KEYS = {
    "usable": "usable_area_m2",
    "usableAreaI": "internal_bra_m2",
    "usableAreaE": "external_bra_m2",
    "usableAreaB": "enclosed_balcony_m2",
    "primary": "primary_room_m2",
    "openArea": "open_area_m2",
}

_PRICE_KEYS = {
    "total": "total_price_nok",
    "collectiveDebt": "shared_debt_nok",
    "collectiveAssets": "collective_assets_nok",
    "taxValue": "asset_value_nok",
    "salesCostSum": "sales_costs_nok",
    "deposit": "deposit_nok",
}


def _embedded_description(ad: dict[str, Any]) -> str | None:
    """All free-text sections of the ad, in page order, as plain text.

    ``propertyInfo`` is the main text ("Om boligen" / "Beskrivelse").
    ``generalText`` holds the remaining sections the advertiser filled in
    (Innhold, Standard, Beliggenhet, Parkering, ...); each is appended under
    its own "## Heading" line. A section repeating text already included is
    skipped.
    """
    parts: list[str] = []
    seen: set[str] = set()

    def add(heading: str | None, raw: Any) -> None:
        text = _html_to_text(raw)
        if not text or text in seen:
            return
        seen.add(text)
        parts.append(f"## {heading}\n{text}" if heading else text)

    info = ad.get("propertyInfo")
    for n, item in enumerate(info if isinstance(info, list) else []):
        if isinstance(item, dict):
            add(_text(item.get("title")) if n else None, item.get("content"))
    if not parts:
        add(None, ad.get("summaryUnsafe"))
    general = ad.get("generalText")
    for item in general if isinstance(general, list) else []:
        if isinstance(item, dict):
            add(_text(item.get("heading")), item.get("textUnsafe"))
    add("Regulering", ad.get("regulations"))
    return "\n\n".join(parts) or None


def _embedded_attributes(ad: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is not None and value != "" and value != [] and value != {}:
            out[key] = value

    put("property_type", _text(ad.get("propertyType")))
    put("ownership_type", _text(ad.get("ownershipType")))
    put("bedrooms", _int(ad.get("bedrooms")))
    put("rooms", _int(ad.get("rooms")))
    put("floor", _int(ad.get("floor")))
    put("construction_year", _int(ad.get("constructionYear")))
    put("renovated_year", _int(ad.get("renovatedYear")))

    size = ad.get("size") if isinstance(ad.get("size"), dict) else {}
    for src, key in _SIZE_KEYS.items():
        put(key, _int(size.get(src)))
    put("area_description", _html_to_text(size.get("descriptionUnsafe")))

    plot = ad.get("plot") if isinstance(ad.get("plot"), dict) else {}
    put("plot_area_m2", _int(plot.get("area")))
    if isinstance(plot.get("owned"), bool) and "plot_area_m2" in out:
        out["plot_owned"] = plot["owned"]

    price = ad.get("price") if isinstance(ad.get("price"), dict) else {}
    for src, key in _PRICE_KEYS.items():
        put(key, _int(price.get(src)))
    put("rent_includes", _text(price.get("includes")))
    put("sales_costs_description", _html_to_text(price.get("salesCostIncludesUnsafe")))

    shared = ad.get("sharedCost") if isinstance(ad.get("sharedCost"), dict) else {}
    put("common_cost_monthly_nok", _int(shared.get("amount")))
    put("common_cost_includes", _html_to_text(shared.get("includesUnsafe")))

    energy = ad.get("energyLabel")
    if isinstance(energy, dict):
        put("energy_label", _text(energy.get("class")))

    put("local_area", _text(ad.get("localAreaName")))
    loc = ad.get("location") if isinstance(ad.get("location"), dict) else {}
    put("postal_code", _text(loc.get("postalCode")))
    put("postal_name", _text(loc.get("postalName")))
    pos = loc.get("position") if isinstance(loc.get("position"), dict) else {}
    lat, lon = pos.get("lat"), pos.get("lng")
    numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon))
    if numeric and (lat, lon) != (0, 0):
        out["lat"], out["lon"] = float(lat), float(lon)

    facilities = ad.get("facilities")
    if isinstance(facilities, list):
        put("facilities", [f for f in (_text(x) for x in facilities) if f])
    put("furnished", _text(ad.get("furnishing")))
    if isinstance(ad.get("animalsAllowed"), bool):
        out["animals_allowed"] = ad["animalsAllowed"]

    span = ad.get("timespan") if isinstance(ad.get("timespan"), dict) else {}
    put("available_from", _text(span.get("from")))
    put("available_to", _text(span.get("to")))

    viewings: list[dict[str, Any]] = []
    times: list[str] = []
    notes: list[str] = []
    for v in ad.get("viewings") if isinstance(ad.get("viewings"), list) else []:
        if not isinstance(v, dict):
            continue
        note = _text(v.get("note"))
        if note and note not in notes:
            notes.append(note)
        entry = {
            k: val
            for k, val in (
                ("date", _text(v.get("dateIso"))),
                ("from", _text(v.get("from"))),
                ("to", _text(v.get("to"))),
            )
            if val
        }
        if entry:
            viewings.append(entry)
        start = _ical_utc(v.get("iCalendarFrom"))
        if start:
            times.append(start)
    put("viewings", viewings)
    put("viewing_times", times)
    put("viewing_note", "\n".join(notes))

    put("preemption", _html_to_text(ad.get("preemption")))
    cadastres = ad.get("cadastres")
    if isinstance(cadastres, list) and cadastres and isinstance(cadastres[0], dict):
        put("cadastre", {_snake(k): v for k, v in cadastres[0].items() if v is not None})
    coop = ad.get("housingCooperative")
    if isinstance(coop, dict):
        put("housing_cooperative", _text(coop.get("name")))
        put("housing_cooperative_org_number", _text(coop.get("organisationNumber")))
    if isinstance(ad.get("changeOfOwnershipInsurance"), bool):
        out["change_of_ownership_insurance"] = ad["changeOfOwnershipInsurance"]
    put("reference", _text(ad.get("advertiserRef")))
    put("prospectus_url", _strip_tracking(ad.get("prospectusView")))
    if isinstance(ad.get("disposed"), bool):
        out["disposed"] = ad["disposed"]  # sold / let

    edited = _parse_dt(meta.get("edited"))
    if edited:
        out["last_edited"] = edited.isoformat()
    return out


def _embedded_published(meta: dict[str, Any]) -> datetime | None:
    """Earliest publication the page knows of.

    ``meta.history`` lists the ad's retained broadcasts; the first "PLAY" one
    is the timestamp the search results show for the ad. None when there is
    no history: the "Sist endret" time is a last-edit time, not a publication
    date, and is reported as ``attributes["last_edited"]`` only.
    """
    history = meta.get("history") if isinstance(meta.get("history"), list) else []
    entries = [h for h in history if isinstance(h, dict)]
    live = [h for h in entries if h.get("mode") == "PLAY"] or entries
    stamps = [d for d in (_parse_dt(h.get("broadcasted")) for h in live) if d]
    aware = [d for d in stamps if d.tzinfo is not None]
    if aware:
        return min(aware)
    return min(stamps) if stamps else None


def _contact(name: Any, title: Any, phone: Any) -> dict[str, Any]:
    pairs = (("name", _text(name)), ("title", _text(title)), ("phone", _text(phone)))
    return {k: v for k, v in pairs if v}


_COMPANY_JSON_RE = re.compile(
    r"<script[^>]*data-company-profile-data[^>]*>(.*?)</script>", re.DOTALL
)


def _seller_from_company_json(html: str) -> dict[str, Any] | None:
    m = _COMPANY_JSON_RE.search(html)
    if not m:
        return None
    data = json.loads(m.group(1))
    if not isinstance(data, dict):
        return None
    profile = data.get("companyProfile") or data.get("extendedProfileRealEstate")
    if not isinstance(profile, dict):
        return None
    contacts: list[dict[str, Any]] = []
    for c in profile.get("contacts") if isinstance(profile.get("contacts"), list) else []:
        if not isinstance(c, dict):
            continue
        phone = None
        phones = c.get("phone")
        if isinstance(phones, list) and phones and isinstance(phones[0], dict):
            phone = phones[0].get("phoneFormatted")
        entry = _contact(c.get("name"), c.get("title"), phone)
        if entry:
            contacts.append(entry)
    name = _text(profile.get("orgName"))
    if not name and not contacts:
        return None
    seller: dict[str, Any] = {"type": "professional"}
    if name:
        seller["name"] = name
    url = _strip_tracking(profile.get("homepageUrl"))
    if url:
        seller["url"] = url
    if contacts:
        seller["contacts"] = contacts
    return seller


def _seller_from_dom(tree: HTMLParser) -> dict[str, Any] | None:
    box = tree.css_first("[data-company-profile]")
    if box is not None:
        name = _node_text(box.css_first("h2"))
        contacts: list[dict[str, Any]] = []
        for h3 in box.css('h3[aria-label="Kontaktperson"]'):
            card = h3.parent
            title = phone = None
            if card is not None:
                title = _node_text(card.css_first("p"))
                phone = _node_text(card.css_first('a[href^="tel:"]'))
            entry = _contact(_node_text(h3), title, phone)
            if entry:
                contacts.append(entry)
        if name or contacts:
            seller: dict[str, Any] = {"type": "professional"}
            if name:
                seller["name"] = name
            for a in box.css("a[href]"):
                if _node_text(a) == "Hjemmeside":
                    url = _strip_tracking(a.attributes.get("href"))
                    if url:
                        seller["url"] = url
                    break
            if contacts:
                seller["contacts"] = contacts
            return seller
    legacy = _node_text(tree.css_first('[data-testid="broker-name"]'))
    if legacy:
        return {"name": legacy}
    if tree.css_first('[data-testid="user-profile-render"]') is not None:
        # A FINN user profile card instead of a company profile: the ad is
        # placed by a user account. Its name is only shown to logged-in users.
        return {"type": "private"}
    return None


def _seller_from_meta(ad: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any] | None:
    urn = meta.get("ownerUrn")
    if not isinstance(urn, str):
        return None
    if ":user:" in urn:
        # Private advertisers' phone numbers are in the payload but not shown
        # on the page to anonymous visitors, so they are not exposed here.
        return {"type": "private"}
    if ":organisation:" in urn:
        seller: dict[str, Any] = {"type": "professional"}
        contacts = []
        for c in ad.get("contact") if isinstance(ad.get("contact"), list) else []:
            if isinstance(c, dict):
                entry = _contact(c.get("name"), c.get("title"), c.get("mobile") or c.get("phone"))
                if entry:
                    contacts.append(entry)
        if contacts:
            seller["contacts"] = contacts
        return seller
    return None


# <dt> label on the detail page -> (attribute key, kind)
_DETAIL_LABELS: dict[str, tuple[str, str]] = {
    "Boligtype": ("property_type", "text"),
    "Eieform": ("ownership_type", "text"),
    "Soverom": ("bedrooms", "int"),
    "Rom": ("rooms", "int"),
    "Internt bruksareal": ("internal_bra_m2", "int"),
    "Bruksareal": ("usable_area_m2", "int"),
    "Eksternt bruksareal": ("external_bra_m2", "int"),
    "Innglasset balkong": ("enclosed_balcony_m2", "int"),
    "Balkong/Terrasse": ("open_area_m2", "int"),
    "Primærrom": ("primary_room_m2", "int"),
    "Bruttoareal": ("gross_area_m2", "int"),
    "Grunnflate": ("floor_area_m2", "int"),
    "Etasje": ("floor", "int"),
    "Byggeår": ("construction_year", "int"),
    "Renovert år": ("renovated_year", "int"),
    "Felleskost/mnd.": ("common_cost_monthly_nok", "int"),
    "Totalpris": ("total_price_nok", "int"),
    "Fellesgjeld": ("shared_debt_nok", "int"),
    "Fellesformue": ("collective_assets_nok", "int"),
    "Omkostninger": ("sales_costs_nok", "int"),
    "Kommunale avg.": ("municipal_fees_nok", "int"),
    "Eiendomsskatt": ("property_tax_nok", "int"),
    "Formuesverdi": ("asset_value_nok", "int"),
    "Verditakst": ("appraised_value_nok", "int"),
    "Lånetakst": ("loan_value_nok", "int"),
    "Festeavgift": ("leasehold_fee_nok", "int"),
    "Depositum": ("deposit_nok", "int"),
    "Inkluderer": ("rent_includes", "text"),
    "Tilbudsfrist": ("offer_deadline", "text"),
}

_CADASTRE_LABELS = {
    "Kommunenr": "municipality_number",
    "Gårdsnr": "land_number",
    "Bruksnr": "title_number",
    "Seksjonsnr": "section_number",
    "Festenr": "leasehold_number",
    "Borettslag-andelsnummer": "part_ownership_number",
}

_NB_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "des": 12,
}

_LAST_EDITED_RE = re.compile(
    r"(\d{1,2})\.\s*([a-zæøå]{3})[a-zæøå]*\.?\s*(\d{4})\s+(\d{1,2}):(\d{2})", flags=re.I
)


def _parse_last_edited(text: str | None) -> datetime | None:
    """'13. apr. 2026 09:22' -> datetime.

    The server renders this table in UTC (both saved pages show the embedded
    ``meta.edited`` time shifted from +02:00 to UTC), so the result is UTC.
    """
    m = _LAST_EDITED_RE.search(text or "")
    if not m or m.group(2).lower() not in _NB_MONTHS:
        return None
    try:
        return datetime(
            int(m.group(3)), _NB_MONTHS[m.group(2).lower()], int(m.group(1)),
            int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _dmy_to_iso(text: str) -> list[str]:
    out = []
    for d, mth, y in re.findall(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text):
        out.append(f"{y}-{int(mth):02d}-{int(d):02d}")
    return out


def _dom_description(tree: HTMLParser) -> str | None:
    section = tree.css_first('[data-testid="about-property"]') or tree.css_first(
        '[data-testid="beskrivelse"]'
    )
    if section is None:
        return None
    body = section.css_first(".description-area")
    if body is not None:
        return _html_to_text(body.html)
    # Unknown inner layout: the section minus its heading, buttons and the
    # boxes nested in it (viewings, links).
    sub = HTMLParser(section.html or "")
    for sel in (
        "button",
        "h2",
        ".sr-only",
        "section section",
        '[data-testid="viewings"]',
        '[data-testid="useful-links"]',
    ):
        for n in sub.css(sel):
            n.decompose()
    return _html_to_text(sub.body.html if sub.body is not None else None)


def _dom_attributes(tree: HTMLParser) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is not None and value != "" and value != [] and value != {} and key not in out:
            out[key] = value

    for node in tree.css('[data-testid^="info-"], [data-testid^="pricing-"]'):
        if len(node.css("dt")) > 1:
            continue  # a container of several pairs
        label = _node_text(node.css_first("dt"))
        value = _node_text(node.css_first("dd"))
        if not label or value is None:
            spans = [t for t in (_node_text(s) for s in node.css("span")) if t]
            if len(spans) == 2:
                label, value = spans
        if not label or not value:
            continue
        if label == "Tomteareal":
            n = _extract_num(value)
            if n is not None:
                put("plot_area_m2", n)
                if "(eiet)" in value:
                    put("plot_owned", True)
                elif "(festet)" in value:
                    put("plot_owned", False)
            continue
        if label == "Leieperiode":
            dates = _dmy_to_iso(value)
            if dates:
                put("available_from", dates[0])
                if len(dates) > 1:
                    put("available_to", dates[1])
            else:
                put("rental_period", value)
            continue
        spec = _DETAIL_LABELS.get(label)
        if spec is None:
            continue
        key, kind = spec
        if kind == "int":
            n = _extract_num(value)
            put(key, n if n is not None else value)
        else:
            put(key, value)

    energy = tree.css_first('[data-testid="energy-label"] [aria-label]')
    if energy is not None:
        m = re.search(r"Energimerke\s+([A-G])\b", energy.attributes.get("aria-label") or "")
        if m:
            put("energy_label", m.group(1))

    put("local_area", _node_text(tree.css_first('[data-testid="local-area-name"]')))

    fac = tree.css_first('[data-testid="object-facilities"]')
    if fac is not None:
        grid = next((c for c in fac.iter() if c.tag == "div"), None)
        items = [t for t in (_node_text(c) for c in (grid.iter() if grid is not None else [])) if t]
        if items and items[0].lower() in _FURNISHING:  # lettings list furnishing first
            put("furnished", items.pop(0))
        put("facilities", items)

    box = tree.css_first('[data-testid="viewings"]')
    if box is not None:
        viewings: list[dict[str, Any]] = []
        times: list[str] = []
        notes: list[str] = []
        for node in box.css('[data-testid^="viewings-"], [data-testid^="viewing-note-"]'):
            tid = node.attributes.get("data-testid") or ""
            # "viewing-note-N" belongs to one viewing, "viewings-note-N" to all.
            if tid.startswith(("viewing-note-", "viewings-note-")):
                note = _node_text(node)
                if note and note not in notes:
                    notes.append(note)
                continue
            start = None
            for a in node.css("a[href]"):
                m = re.search(r"iCalendarFrom=(\d{8}T\d{6}Z)", a.attributes.get("href") or "")
                if m:
                    start = _ical_utc(m.group(1))
                    break
            entry: dict[str, Any] = {}
            if start:
                times.append(start)
                entry["date"] = start[:10]
            own = re.sub(r"<!--.*?-->", "", node.html or "", flags=re.S)
            m = re.search(r">\s*(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\s*<", own)
            if m:
                entry["from"], entry["to"] = m.group(1), m.group(2)
            if entry:
                viewings.append(entry)
        put("viewings", viewings)
        put("viewing_times", times)
        put("viewing_note", "\n".join(notes))

    for tid, key in (
        ("area-description", "area_description"),
        ("preemption", "preemption"),
        ("common-cost", "common_cost_includes"),
        ("sales-costs", "sales_costs_description"),
    ):
        node = tree.css_first(f'[data-testid="{tid}"]')
        if node is not None:
            put(key, _html_to_text(node.html))

    cad = tree.css_first('[data-testid="cadastre-info"]')
    if cad is not None:
        cadastre: dict[str, Any] = {}
        for div in cad.css("div"):
            if any(d.mem_id != div.mem_id for d in div.css("div")):
                continue  # wrapper around the label/value rows
            label, sep, value = (_node_text(div) or "").partition(":")
            label, value = label.strip(), value.strip()
            if not sep or not value:
                continue
            if label in _CADASTRE_LABELS:
                n = _int(value)
                cadastre.setdefault(_CADASTRE_LABELS[label], n if n is not None else value)
            elif label == "Borettslag-navn":
                put("housing_cooperative", value)
            elif label == "Borettslag-orgnummer":
                put("housing_cooperative_org_number", value)
        put("cadastre", cadastre)

    info = tree.css_first('[data-testid="object-info"]')
    if info is not None:
        for row in info.css("tr"):
            label, value = _node_text(row.css_first("th")), _node_text(row.css_first("td"))
            if label == "Referanse":
                put("reference", value)
            elif label == "Sist endret":
                edited = _parse_last_edited(value)
                if edited:
                    put("last_edited", edited.isoformat())
    return out


def _image_key(url: str) -> str:
    return re.sub(r"^https://images\.finncdn\.no/dynamic/[^/]+/", "", url)


def _detail_images(ad: dict[str, Any], tree: HTMLParser, html: str) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()

    def add(url: Any) -> None:
        if isinstance(url, str) and url.startswith("http") and _image_key(url) not in seen:
            seen.add(_image_key(url))
            images.append(url)

    for img in ad.get("images") if isinstance(ad.get("images"), list) else []:
        if isinstance(img, dict):
            add(img.get("uri"))
    if images:
        return images
    og = tree.css_first('meta[property="og:image"]')
    if og is not None:
        add(og.attributes.get("content"))
    # Only the ad's own gallery when it can be found; the page also shows
    # thumbnails of other ads ("Lignende annonser").
    gallery = tree.css_first('[data-testid="image-gallery"]') or tree.css_first(
        '[data-testid="gallery-main"]'
    )
    scope = (gallery.html or "") if gallery is not None else html
    for m in re.finditer(r'https://images\.finncdn\.no/dynamic/\d+w/[^"\'<> ),]+', scope):
        add(m.group(0))
        if len(images) >= 40:
            break
    return images


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
        html = html or ""
        tree = HTMLParser(html)

        # Each source is optional and must not take the others down with it.
        def attempt(fn: Any, *args: Any, default: Any = None) -> Any:
            try:
                return fn(*args)
            except Exception:
                return default

        ad, meta = attempt(_embedded_ad, html, default=({}, {}))

        # Title: embedded, then the <h1>, then og:title.
        title = _text(ad.get("title"))
        if not title:
            title = _node_text(tree.css_first("h1"))
        if not title:
            og_title = tree.css_first('meta[property="og:title"]')
            if og_title is not None:
                title = _clean(og_title.attributes.get("content"))

        # Address: "Street 1, 0123 Place".
        location: str | None = None
        loc = ad.get("location") if isinstance(ad.get("location"), dict) else {}
        street = _text(loc.get("streetAddress"))
        place = _clean(
            " ".join(p for p in (_text(loc.get("postalCode")), _text(loc.get("postalName"))) if p)
        )
        if street or place:
            location = ", ".join(p for p in (street, place) if p)
        if not location:
            location = _node_text(tree.css_first('[data-testid="object-address"]'))

        # Attributes: embedded values first, the DOM fills what is missing.
        attributes: dict[str, Any] = attempt(_embedded_attributes, ad, meta, default={}) or {}
        for key, value in (attempt(_dom_attributes, tree, default={}) or {}).items():
            attributes.setdefault(key, value)

        # Price: asking price for sales, monthly rent for lettings. Note:
        # finn.no spells the testid "pricing-incicative-price" (sic).
        price_data = ad.get("price") if isinstance(ad.get("price"), dict) else {}
        price = _amount(price_data.get("suggestion")) or _amount(price_data.get("monthly"))
        if price is None:
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

        # Description: every text section from the embedded ad, else the
        # visible "Om boligen"/"Beskrivelse" body. Never the meta description,
        # which is cut off.
        description = attempt(_embedded_description, ad) or attempt(_dom_description, tree)

        images = attempt(_detail_images, ad, tree, html, default=[]) or []

        seller = (
            attempt(_seller_from_company_json, html)
            or attempt(_seller_from_dom, tree)
            or attempt(_seller_from_meta, ad, meta)
        )

        # Only from the publication history. The page's visible "Sist endret"
        # time is a last-edit time (a week after publication on the saved
        # lettings ad), so it stays in attributes["last_edited"].
        published_at = attempt(_embedded_published, meta)

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
            published_at=published_at,
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
