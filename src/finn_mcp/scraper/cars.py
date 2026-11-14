from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

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


# ----- detail-page helpers -----
#
# A car ad page (April 2026 markup) carries its data in three places:
#
#   * <script id="advertising-initial-state"> - ad-server targeting key/values.
#     Numeric facts (year, mileage, price, range, hp, seats, doors, owners),
#     the registration number and the model specification are plain values;
#     fuel/body/colour/gearbox are opaque numeric codes and are NOT used.
#   * JSON-LD Product - name, price, brand, model and the first few images.
#     No description, location, seller name or dates.
#   * DOM - everything a reader sees: the full ad text ("Beskrivelse"), the
#     spec table ("Spesifikasjoner", a <dl>), equipment ("Utstyr"), address
#     ("Sted"), the seller card and "Annonseinformasjon" (last updated).
#
# Every source is optional; each helper returns an empty value when its
# source is missing or malformed.

_ADV_STATE_RE = re.compile(
    r'<script\b[^>]*\bid="advertising-initial-state"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_SKIP_TAGS = {"script", "style", "template", "svg", "button", "noscript", "w-icon"}
_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "tr", "table", "section", "article",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "dl",
}
_NUM_RE = re.compile(r"\d[\d   .]*")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "des": 12,
}
_NO_DATETIME_RE = re.compile(
    r"(\d{1,2})\.\s*([a-zæøå]{3,})\.?\s+(\d{4})(?:,?\s*(?:kl\.?\s*)?(\d{1,2})[:.](\d{2}))?",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_POSTAL_RE = re.compile(r"(?:^|,\s*)(\d{4})\s+([^,]+)$")
_ORGNR_RE = re.compile(r"orgnr=(\d{9})")
_DEALER_HREF_RE = re.compile(r"/mobility/dealer/(\d+)")
_IMAGE_KEY_RE = re.compile(r"/dynamic/[^/]+/(.+)$")
_IMAGE_PREFIX = "https://images.finncdn.no/dynamic/default/"
_TRUNCATED_RE = re.compile(r"(?:\.\.\.|…)\s*$")


def _is_element(node: Node) -> bool:
    return bool(node.tag) and not node.tag.startswith(("-", "_"))


def _html_to_text(node: Node | None, skip: set[int] | None = None) -> str | None:
    """Readable plain text of an element: <br> and block tags become newlines.

    Entities are already decoded by the HTML parser. Runs of blank lines are
    collapsed to one, and nothing is truncated.
    """
    if node is None:
        return None
    parts: list[str] = []

    def walk(n: Node) -> None:
        for child in n.iter(include_text=True):
            tag = child.tag or ""
            if tag == "-text":
                parts.append(child.text_content or "")
                continue
            if not _is_element(child) or tag in _SKIP_TAGS:
                continue
            if skip and child.mem_id in skip:
                continue
            if tag == "br":
                parts.append("\n")
                continue
            block = tag in _BLOCK_TAGS
            if block:
                parts.append("\n")
            walk(child)
            if block:
                parts.append("\n")

    walk(node)
    text = "".join(parts).replace("\xa0", " ").replace("​", "").replace("\r", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) or None


def _own_text(node: Node) -> str | None:
    """Text directly inside the node (ignores nested tooltips/buttons/links)."""
    return _clean(node.text(deep=False, separator=" "))


def _heading_parent(tree: HTMLParser, *titles: str) -> Node | None:
    """Parent element of the first h2/h3 whose text equals one of ``titles``."""
    wanted = {t.casefold() for t in titles}
    for h in tree.css("h2, h3"):
        text = _clean(h.text(separator=" ", strip=True))
        if text and text.casefold() in wanted and h.parent is not None:
            # A heading sitting directly in the page root scopes nothing:
            # its "section" would be the whole page.
            if h.parent.tag in ("body", "html", "main"):
                return None
            return h.parent
    return None


def _first_int(text: str | None) -> int | None:
    """'192 450 km' -> 192450, '4 532 kr (avgifter)' -> 4532."""
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    return int(digits) if digits else None


def _first_float(text: str | None) -> float | None:
    """'75 kWh' -> 75.0, '1,6 l' -> 1.6."""
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text.replace("\xa0", "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _oslo_tz(naive: datetime) -> timezone:
    """UTC offset in Norway for a local wall-clock time (EU DST rules).

    Hand-rolled because ``zoneinfo`` needs the tzdata package on Windows.
    """

    def last_sunday(year: int, month: int) -> datetime:
        d = datetime(year, month, 31)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    start = last_sunday(naive.year, 3).replace(hour=2)
    end = last_sunday(naive.year, 10).replace(hour=3)
    return timezone(timedelta(hours=2 if start <= naive < end else 1))


def _parse_no_datetime(text: str | None) -> datetime | None:
    """'17. april 2026, 08:56' -> aware datetime in Norwegian local time."""
    if not text:
        return None
    m = _NO_DATETIME_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower()[:3])
    if month is None:
        return None
    try:
        naive = datetime(
            int(m.group(3)), month, int(m.group(1)),
            int(m.group(4) or 0), int(m.group(5) or 0),
        )
    except ValueError:
        return None
    return naive.replace(tzinfo=_oslo_tz(naive))


def _iso_date(text: str | None) -> str | None:
    """'23.12.2020' -> '2020-12-23'; anything else is returned unchanged."""
    if not text:
        return None
    m = _NUMERIC_DATE_RE.match(text.strip())
    if not m:
        return text
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return text


def _months(text: str | None) -> int | str | None:
    """'12 måneder' -> 12, '2 år' -> 24."""
    n = _first_int(text)
    if n is None or not text:
        return text
    low = text.lower()
    if "år" in low:
        return n * 12
    if "mnd" in low or "måned" in low:
        return n
    return text


def _power(text: str | None) -> tuple[str, Any] | None:
    n = _first_int(text)
    if n is None or not text:
        return None
    low = text.lower()
    if "hk" in low:
        return "power_hp", n
    if "kw" in low:
        return "power_kw", n
    return None


def _slug(label: str) -> str:
    s = label.lower()
    for a, b in (("æ", "ae"), ("ø", "o"), ("å", "a")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


# Spec-table label (lower-case) -> (attribute key, converter). Keys shared
# with the search cards (year, mileage_km, fuel, transmission, range_km,
# warranty_months) use the same names and types there.
_SPEC_FIELDS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "merke": ("brand", str),
    "modell": ("model", str),
    "modellår": ("year", _first_int),
    "årsmodell": ("year", _first_int),
    "karosseri": ("body_type", str),
    "drivstoff": ("fuel", str),
    "kilometerstand": ("mileage_km", _first_int),
    "rekkevidde (wltp)": ("range_km", _first_int),
    "rekkevidde": ("range_km", _first_int),
    "batterikapasitet": ("battery_kwh", _first_float),
    "girkasse": ("transmission", str),
    "hjuldrift": ("wheel_drive", str),
    "vekt": ("weight_kg", _first_int),
    "seter": ("seats", _first_int),
    "dører": ("doors", _first_int),
    "farge": ("color", str),
    "fargebeskrivelse": ("color_description", str),
    "interiørfarge": ("interior_color", str),
    "bilen står i": ("country", str),
    "avgiftsklasse": ("tax_class", str),
    "registreringsnummer": ("registration_number", str),
    "chassis nr. (vin)": ("vin", str),
    "1. gang registrert": ("first_registration", _iso_date),
    "eiere": ("owners", _first_int),
    "garanti": ("warranty", str),
    "garantiens varighet": ("warranty_months", _months),
    "garanti inntil": ("warranty_km", _first_int),
    "salgsform": ("sales_form", str),
    "omregistrering": ("reregistration_fee", _first_int),
    "pris eksl. omreg.": ("price_excl_reregistration", _first_int),
    "slagvolum": ("engine_volume_l", _first_float),
    "co2-utslipp": ("co2_g_km", _first_int),
    "maksimal tilhengervekt": ("max_trailer_weight_kg", _first_int),
}
# A <dd> that only holds a link ("Årsavgift: Les mer") carries no value.
_LINK_ONLY_VALUES = {"les mer"}


def _spec_pair(label: str | None, value: str | None) -> tuple[str, Any] | None:
    if not label or not value or value.lower() in _LINK_ONLY_VALUES:
        return None
    low = label.lower().rstrip(":").strip()
    if low == "effekt":
        return _power(value) or ("power", value)
    field = _SPEC_FIELDS.get(low)
    if field is None:
        # Unknown row: keep it verbatim under a slug of its label.
        key = _slug(low)
        if not key:
            return None
        return key, (int(value) if value.isdigit() else value)
    key, convert = field
    converted = convert(value)
    return key, (converted if converted is not None else value)


def _dom_specs(tree: HTMLParser) -> dict[str, Any]:
    """The 'Spesifikasjoner' <dl> as typed attributes."""
    out: dict[str, Any] = {}
    for dt in tree.css("dl dt"):
        dd = dt.next
        while dd is not None and dd.tag != "dd":
            dd = dd.next
        if dd is None:
            continue
        # Own text first: skips the tooltip inside <dt> and the trailing
        # "(avgifter)" link inside <dd>.
        label = _own_text(dt) or _clean(dt.text(separator=" ", strip=True))
        value = _own_text(dd) or _clean(dd.text(separator=" ", strip=True))
        pair = _spec_pair(label, value)
        if pair and pair[0] not in out:
            out[pair[0]] = pair[1]
    return out


def _dom_key_facts(h1: Node | None) -> dict[str, Any]:
    """Icon row under the title: <span>Modellår</span><p>2021</p> pairs."""
    out: dict[str, Any] = {}
    if h1 is None or h1.parent is None:
        return out
    for span in h1.parent.css("span"):
        p = span.next
        while p is not None and not _is_element(p):
            p = p.next
        if p is None or p.tag != "p":
            continue
        label = _clean(span.text(separator=" ", strip=True))
        if not label or label.lower() not in _SPEC_FIELDS:
            continue
        pair = _spec_pair(label, _clean(p.text(separator=" ", strip=True)))
        if pair and pair[0] not in out:
            out[pair[0]] = pair[1]
    return out


def _targeting(html: str, finnkode: str) -> dict[str, list[str]]:
    """Ad-server targeting key/values for this ad, or {}."""
    m = _ADV_STATE_RE.search(html)
    if not m:
        return {}
    out: dict[str, list[str]] = {}
    try:
        state = json.loads(m.group(1))
        ad_server = state["config"]["adServer"]
        pairs = (ad_server.get("gam") or {}).get("targeting") or (
            ad_server.get("prebid") or {}
        ).get("targeting")
        for pair in pairs or []:
            key, value = pair.get("key"), pair.get("value")
            if isinstance(key, str) and isinstance(value, list):
                out[key] = [str(v) for v in value]
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}
    # Only trust it when it is demonstrably about this ad.
    if out.get("id") != [str(finnkode)]:
        return {}
    return out


def _targeting_attributes(t: dict[str, list[str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def one(key: str) -> str | None:
        v = t.get(key)
        return v[0] if v and len(v) == 1 and v[0] else None

    for src, key in (
        ("year", "year"),
        ("mileage", "mileage_km"),
        ("driving_range", "range_km"),
        ("engine_effect", "power_hp"),
        ("seats", "seats"),
        ("doors", "doors"),
        ("owners", "owners"),
    ):
        v = one(src)
        if v and v.isdigit():
            out[key] = int(v)
    if one("registration_number"):
        out["registration_number"] = one("registration_number")
    if one("car_model_spec"):
        out["subtitle"] = _clean(one("car_model_spec"))
    return out


def _meta_content(html: str, *names: str) -> str | None:
    for name in names:
        for tag in _META_RE.findall(html):
            if f'"{name}"' not in tag:
                continue
            m = re.search(r'content="([^"]*)"', tag)
            if m and m.group(1).strip():
                # Round-trip through the parser to decode entities.
                node = HTMLParser(f"<p>{m.group(1)}</p>").css_first("p")
                return _clean(node.text()) if node is not None else None
    return None


def _image_key(url: str) -> str:
    m = _IMAGE_KEY_RE.search(url)
    return m.group(1) if m else url


def _largest_srcset(srcset: str) -> str | None:
    best: tuple[int, str] | None = None
    for cand in srcset.split(","):
        bits = cand.strip().split()
        if not bits or not bits[0].startswith("http"):
            continue
        width = (_first_int(bits[1]) if len(bits) > 1 else None) or 0
        if best is None or width > best[0]:
            best = (width, bits[0])
    return best[1] if best else None


def _gallery_images(tree: HTMLParser) -> list[str]:
    out: list[str] = []
    for img in tree.css('img[id^="gallery-image-"]'):
        attrs = img.attributes
        url = _largest_srcset(attrs.get("srcset") or "")
        if url is None:
            src = attrs.get("src") or ""
            url = src if src.startswith("http") else None
        if url:
            out.append(url)
    return out


def _merge_images(*sources: list[str]) -> list[str]:
    """Concatenate image lists, keeping the first URL seen per image."""
    seen: set[str] = set()
    out: list[str] = []
    for source in sources:
        for url in source:
            key = _image_key(url)
            if key not in seen:
                seen.add(key)
                out.append(url)
    return out


def _dom_description(tree: HTMLParser) -> str | None:
    section = _heading_parent(tree, "Beskrivelse")
    if section is None:
        return None
    body = section.css_first('[data-testid="expandable-section"]')
    if body is not None:
        text = _html_to_text(body)
        if text:
            return text
    # No marked body element: take what follows the heading, up to the next
    # heading, so a wide container cannot leak other sections into the text.
    heading = next(
        (
            c
            for c in section.iter()
            if c.tag in ("h2", "h3")
            and (_clean(c.text(separator=" ", strip=True)) or "").casefold() == "beskrivelse"
        ),
        None,
    )
    if heading is None:
        return None
    chunks: list[str] = []
    sib = heading.next
    while sib is not None:
        if _is_element(sib):
            if sib.tag in ("h1", "h2", "h3") or sib.css_first("h1, h2, h3") is not None:
                break
            text = _html_to_text(sib) if sib.tag not in _SKIP_TAGS else None
            if text:
                chunks.append(text)
        elif sib.tag == "-text":
            text = _clean(sib.text_content or "")
            if text:
                chunks.append(text)
        sib = sib.next
    return "\n\n".join(chunks) or None


def _map_link_location(tree: HTMLParser) -> tuple[str | None, dict[str, Any]]:
    """Place name from the map link under the gallery.

    Pages without a 'Sted' section (Nov 2026 markup) only have this link:
    its text is the place, its query string has coordinates and, for
    private ads, the postal code.
    """
    extra: dict[str, Any] = {}
    for a in tree.css('a[href*="finn.no/map"], a[href^="/map"]'):
        place = _clean(a.text(separator=" ", strip=True))
        if not place:
            continue
        qs = parse_qs(urlsplit(a.attributes.get("href") or "").query)
        try:
            lat, lon = qs.get("lat"), qs.get("lon") or qs.get("lng")
            if lat and lon:
                extra["latitude"], extra["longitude"] = float(lat[0]), float(lon[0])
        except ValueError:
            pass
        postal = (qs.get("postalCode") or [""])[0].strip()
        if postal.isdigit():
            extra["postal_code"] = postal
            extra["city"] = place
            return f"{postal} {place}", extra
        return place, extra
    return None, extra


def _dom_location(tree: HTMLParser) -> tuple[str | None, dict[str, Any]]:
    """Address under the 'Sted' heading plus what its map link encodes."""
    extra: dict[str, Any] = {}
    block = _heading_parent(tree, "Sted")
    if block is None:
        return _map_link_location(tree)
    location: str | None = None
    for a in block.css("a"):
        text = _clean(a.text(separator=" ", strip=True))
        if text and location is None:
            location = text
        try:
            qs = parse_qs(urlsplit(a.attributes.get("href") or "").query)
            lat, lon = qs.get("lat"), qs.get("lon") or qs.get("lng")
            if lat and lon and "latitude" not in extra:
                extra["latitude"], extra["longitude"] = float(lat[0]), float(lon[0])
        except ValueError:
            pass
    if location is None:
        # Nov 2026 markup: the address is plain text after the heading.
        parts = [
            p
            for p in (
                _clean(s) for s in block.text(separator="\n", strip=True).split("\n")
            )
            if p and p.casefold() != "sted"
        ]
        if parts:
            location = parts[0]
    if "latitude" not in extra:
        _, link_extra = _map_link_location(tree)
        for key in ("latitude", "longitude"):
            if key in link_extra:
                extra[key] = link_extra[key]
    if location:
        m = _POSTAL_RE.search(location)
        if m:
            extra["postal_code"] = m.group(1)
            extra["city"] = m.group(2).strip()
    return location, extra


def _dom_seller(tree: HTMLParser, product: dict[str, Any] | None) -> dict[str, Any] | None:
    seller: dict[str, Any] = {}

    card = _heading_parent(tree, "Selgerens infokort")
    if card is not None:
        h3 = card.css_first("h3")
        name = _clean(h3.text(separator=" ", strip=True)) if h3 is not None else None
        if name:
            seller["name"] = name
        for p in card.css("p"):
            text = _clean(p.text(separator=" ", strip=True))
            if text and "på finn" in text.lower():
                seller["tenure"] = text
                break

    dealer_link: Node | None = None
    for a in tree.css('a[href*="/mobility/dealer/"]'):
        href = a.attributes.get("href") or ""
        m = _DEALER_HREF_RE.search(href)
        if m:
            dealer_link = a
            seller["dealer_id"] = m.group(1)
            seller["dealer_url"] = href if href.startswith("http") else f"https://www.finn.no{href}"
            break
    if "name" not in seller and dealer_link is not None and dealer_link.parent is not None:
        h2 = dealer_link.parent.css_first("h2")
        name = _clean(h2.text(separator=" ", strip=True)) if h2 is not None else None
        if name:
            seller["name"] = name

    for a in tree.css('a[href*="orgnr="]'):
        m = _ORGNR_RE.search(a.attributes.get("href") or "")
        if m:
            seller["org_number"] = m.group(1)
            break

    for a in tree.css('a[href*="google.com/maps"]'):
        spans = [_clean(s.text(separator=" ", strip=True)) for s in a.css("span")]
        spans = [s for s in spans if s and s != ","]
        if spans:
            seller["address"] = spans[-1]
            break

    for a in tree.css("a"):
        if "Besøk nettsiden" in (a.text() or ""):
            href = a.attributes.get("href") or ""
            if href.startswith("http"):
                seller["website"] = href
            break

    # Seller type: only from explicit evidence on the page.
    ld_seller = None
    if product and isinstance(product.get("offers"), dict):
        ld_seller = product["offers"].get("seller")
    if isinstance(ld_seller, dict):
        ld_name = ld_seller.get("name")
        if "name" not in seller and isinstance(ld_name, str) and _clean(ld_name):
            seller["name"] = _clean(ld_name)
    if "dealer_id" in seller or "org_number" in seller:
        seller["type"] = "Forhandler"
    elif isinstance(ld_seller, dict) and ld_seller.get("@type") == "Person":
        seller["type"] = "Privat"
    elif isinstance(ld_seller, dict) and ld_seller.get("@type") == "Organization" and seller:
        seller["type"] = "Forhandler"

    return seller or None


def _dom_ad_info(tree: HTMLParser) -> dict[str, str]:
    """'Annonseinformasjon': <div><p>label</p><p>value</p></div> rows."""
    out: dict[str, str] = {}
    block = _heading_parent(tree, "Annonseinformasjon")
    if block is None:
        return out
    for div in block.css("div"):
        kids = [c for c in div.iter() if _is_element(c)]
        if len(kids) == 2 and kids[0].tag == "p" and kids[1].tag == "p":
            label = _clean(kids[0].text(separator=" ", strip=True))
            value = _clean(kids[1].text(separator=" ", strip=True))
            if label and value:
                out.setdefault(label.lower(), value)
    return out


def _dom_price(tree: HTMLParser) -> int | None:
    """The amount next to the 'Totalpris'/'Pris' label, never a random 'kr'."""
    for p in tree.css("p, span"):
        label = _clean(p.text(separator=" ", strip=True))
        if not label or label.lower() not in ("totalpris", "pris"):
            continue
        parent = p.parent
        if parent is None:
            continue
        for cand in parent.css("h2, h3, p, span"):
            if cand.mem_id == p.mem_id:
                continue
            price = parse_price_nok(_clean(cand.text(separator=" ", strip=True)))
            if price is not None:
                return price
    return None


def _main_h1(tree: HTMLParser) -> Node | None:
    h1 = tree.css_first("main h1")
    if h1 is not None:
        return h1
    for h in tree.css("h1"):
        cur, inside_chrome = h.parent, False
        while cur is not None:
            if cur.tag in ("nav", "footer", "header"):
                inside_chrome = True
                break
            cur = cur.parent
        if not inside_chrome:
            return h
    return None


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
        html = html or ""
        blocks = extract_jsonld(html)
        product = find_by_type(blocks, "Product", "Car", "Vehicle")
        targeting = _targeting(html, finnkode)
        tree = HTMLParser(html)

        title: str | None = None
        price: int | None = None
        currency = "NOK"
        images: list[str] = []
        attributes: dict[str, Any] = {}

        if product:
            name = product.get("name")
            title = _clean(name) if isinstance(name, str) else None
            images = _extract_images(product.get("image"))
            brand = product.get("brand")
            if isinstance(brand, dict) and brand.get("name"):
                attributes["brand"] = brand.get("name")
            elif isinstance(brand, str) and brand:
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

        # ----- title / subtitle -----
        h1 = _main_h1(tree)
        if title is None and h1 is not None:
            title = _clean(h1.text(separator=" ", strip=True))
        subtitle: str | None = None
        if h1 is not None:
            sib = h1.next
            while sib is not None and not _is_element(sib):
                sib = sib.next
            if sib is not None and sib.tag == "p":
                subtitle = _clean(sib.text(separator=" ", strip=True))

        # ----- attributes: embedded targeting, then spec table, then key facts -----
        for source in (_targeting_attributes(targeting), _dom_specs(tree), _dom_key_facts(h1)):
            for key, value in source.items():
                if value is not None and value != "":
                    attributes.setdefault(key, value)
        if subtitle:
            attributes.setdefault("subtitle", subtitle)

        equipment_block = _heading_parent(tree, "Utstyr")
        if equipment_block is not None:
            items = (_clean(li.text(separator=" ", strip=True)) for li in equipment_block.css("li"))
            equipment = [e for e in items if e]
            if equipment:
                attributes["equipment"] = equipment

        # ----- description: the ad body; teasers only as a flagged last resort -----
        description = _dom_description(tree)
        if description is None:
            teaser: str | None = None
            if product and isinstance(product.get("description"), str):
                teaser = _html_to_text(
                    HTMLParser(f"<div>{product['description']}</div>").css_first("div")
                )
            teaser = teaser or _meta_content(html, "og:description", "description")
            if teaser:
                description = teaser
                if _TRUNCATED_RE.search(teaser):
                    attributes["description_truncated"] = True

        # ----- location -----
        location, location_extra = _dom_location(tree)
        for key, value in location_extra.items():
            attributes.setdefault(key, value)

        # ----- seller -----
        seller = _dom_seller(tree, product)
        # A bare place name ("Hønefoss") is upgraded to the dealer's street
        # address when that address is in the same place.
        address = (seller or {}).get("address")
        if location and address and location.casefold() != address.casefold():
            if address.casefold().endswith(location.casefold()):
                location = address
                m = _POSTAL_RE.search(address)
                if m:
                    attributes.setdefault("postal_code", m.group(1))
                    attributes.setdefault("city", m.group(2).strip())

        # ----- dates and reference -----
        # Car ads only show "Sist oppdatert" (last changed). It is exposed as
        # attributes["last_updated"]; published_at is set only when the page
        # states a publication date.
        published_at: datetime | None = None
        info = _dom_ad_info(tree)
        for label in ("publisert", "lagt ut", "opprettet"):
            published_at = published_at or _parse_no_datetime(info.get(label))
        updated = _parse_no_datetime(info.get("sist oppdatert") or info.get("sist endret"))
        if updated is not None:
            attributes["last_updated"] = updated.isoformat()
        if info.get("referanse"):
            attributes["reference"] = info["referanse"]

        # ----- price -----
        if price is None:
            v = targeting.get("price")
            if v and len(v) == 1 and v[0].isdigit():
                price = int(v[0])
        if price is None:
            price = _dom_price(tree)

        # ----- images: JSON-LD has the first few, the gallery has all -----
        gallery = _gallery_images(tree)
        if not gallery:
            gallery = [_IMAGE_PREFIX + path for path in targeting.get("images", []) if path]
        images = _merge_images(images, gallery)
        if not images:
            og_image = _meta_content(html, "og:image")
            if og_image and og_image.startswith("http"):
                images = [og_image]

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
            seller=seller,
            published_at=published_at,
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
