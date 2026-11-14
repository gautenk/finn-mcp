from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from selectolax.parser import HTMLParser, Node

from .. import http_client
from ..models import JobType, Listing, SearchResult
from .base import VerticalScraper, _clean, now_utc
from .jsonld import extract_jsonld, find_by_type

# The legacy /job/fulltime/ and /job/parttime/ paths on finn.no now redirect
# to /job/search, where full-time vs part-time is the ``extent`` filter.
JOB_TYPE_EXTENT: dict[str, str] = {
    "fulltime": "3947",
    "parttime": "3942",
}

_EMPLOYMENT_TYPE_JOB_TYPE = {
    "FULL_TIME": "fulltime",
    "PART_TIME": "parttime",
}


def _job_type_from_employment(emp_type: Any) -> str | None:
    values = emp_type if isinstance(emp_type, list) else [emp_type]
    found = {
        _EMPLOYMENT_TYPE_JOB_TYPE[str(v).upper()]
        for v in values
        if str(v).upper() in _EMPLOYMENT_TYPE_JOB_TYPE
    }
    return found.pop() if len(found) == 1 else None


# ----- search-card helpers -----

_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>([^<]+)</script>', re.IGNORECASE
)
# "Oslo - Sentrum +1 til": the card collapses extra workplaces into a counter.
_MORE_LOCATIONS_RE = re.compile(r"\s*\+\s*(\d+)\s+til$")
# "Timelønn: 244 - 306 kr", "Årslønn: 600 000 kr (…)"
_SALARY_RE = re.compile(
    r"^(?P<kind>[^\W\d]+):\s*(?P<min>\d[\d ]*?)\s*(?:[-–]\s*(?P<max>\d[\d ]*?)\s*)?kr\b",
    re.IGNORECASE,
)
_SALARY_PERIODS = {"timelønn": "hour", "månedslønn": "month", "årslønn": "year"}
_SPONSORED_LABEL = "Betalt plassering"


def _iter_job_docs(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every job search entry found anywhere in a decoded JSON tree."""
    if isinstance(node, dict):
        if node.get("type") == "job" and node.get("id") and "company_name" in node:
            yield node
            return
        for value in node.values():
            yield from _iter_job_docs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_job_docs(value)


def extract_embedded_job_docs(html: str) -> dict[str, dict[str, Any]]:
    """Return ``{finnkode: doc}`` from the search page's hydration data.

    FINN ships the result list as base64-encoded JSON in
    ``<script type="application/json">`` blocks: the organic hits under
    ``docs`` and the paid top placement as a separate ``searchEntry``. The
    paid entry is not always present, so this is optional enrichment on top
    of the DOM. Returns ``{}`` if nothing can be decoded.
    """
    docs: dict[str, dict[str, Any]] = {}
    for m in _JSON_SCRIPT_RE.finditer(html):
        raw = m.group(1).strip()
        if len(raw) < 200:
            continue
        try:
            if raw[0] not in "{[":
                raw = base64.b64decode(raw, validate=True).decode("utf-8")
            data = json.loads(raw)
        except (binascii.Error, ValueError):
            continue
        for doc in _iter_job_docs(data):
            # First occurrence wins, but a later copy that carries a
            # deadline replaces one that does not.
            key = str(doc["id"])
            if key not in docs or (doc.get("deadline") and not docs[key].get("deadline")):
                docs[key] = doc
    return docs


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _epoch_ms_to_datetime(value: Any) -> datetime | None:
    if not _is_number(value):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _epoch_ms_to_iso(value: Any) -> str | None:
    dt = _epoch_ms_to_datetime(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z") if dt else None


def _epoch_ms_to_local_date(value: Any) -> str | None:
    """Epoch ms -> ISO date in Norwegian time.

    Deadlines are stored as 23:59:59.999 Norwegian time (21:59 or 22:59 UTC),
    so shifting by the minimum offset (CET, +1h) always lands on the right
    calendar day without needing a tz database.
    """
    dt = _epoch_ms_to_datetime(value)
    return (dt + timedelta(hours=1)).date().isoformat() if dt else None


def _to_int(digits: str | None) -> int | None:
    if not digits:
        return None
    try:
        return int(digits.replace(" ", ""))
    except ValueError:
        return None


def _salary_fields(salary_text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"salary_text": salary_text}
    m = _SALARY_RE.match(salary_text)
    if not m:
        return out
    period = _SALARY_PERIODS.get(m.group("kind").lower())
    if period:
        out["salary_period"] = period
    low = _to_int(m.group("min"))
    high = _to_int(m.group("max"))
    if low is not None:
        out["salary_min"] = low
    if high is not None:
        out["salary_max"] = high
    return out


def _str(value: Any) -> str | None:
    return _clean(value) if isinstance(value, str) else None


# ----- ad page (detail) helpers -----
#
# A job ad page carries the same ad in up to three places:
#   1. Embedded route data: the React Router "turbo-stream" payload passed to
#      ``window.__reactRouterContext.streamController.enqueue("…")``. It holds
#      ``jobAd`` = {adType, ad, adSummary, meta, …} and is the only complete
#      source: it is present on every ad type.
#   2. JSON-LD ``JobPosting``. Missing on ads FINN imports from other sites
#      (``adType: AGGREGATED_JOB``, e.g. NAV/Arbeidsplassen), which is why
#      those used to come back with a title only.
#   3. The rendered DOM inside ``<section aria-label="Jobbdetaljer">``.

_ENQUEUE_RE = re.compile(r'streamController\.enqueue\(("(?:[^"\\]+|\\.)*")\)')

_JOB_TYPE_EMPLOYMENT = {v: k for k, v in _EMPLOYMENT_TYPE_JOB_TYPE.items()}
_JOB_TYPE_EXTENT_LABEL = {"fulltime": "Heltid", "parttime": "Deltid"}
_EXTENT_JOB_TYPE = {
    **{extent_id: job_type for job_type, extent_id in JOB_TYPE_EXTENT.items()},
    "heltid": "fulltime",
    "deltid": "parttime",
    "fulltime": "fulltime",
    "parttime": "parttime",
    "full_time": "fulltime",
    "part_time": "parttime",
}

_NO_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_SPACE_RE = re.compile(r"[ \t\r\f\v\xa0​]+")
_ANY_SPACE_RE = re.compile(r"[\s\xa0​]+")

_TEXT_SKIP_TAGS = {"script", "style", "template", "svg", "noscript", "head", "title"}
_TEXT_BLOCK_TAGS = {
    "p", "div", "section", "article", "ul", "ol", "dl", "table", "blockquote",
    "pre", "h1", "h2", "h3", "h4", "h5", "h6",
}
_TEXT_LINE_TAGS = {"li", "tr", "dt", "dd"}


def _node_to_text(root: Node | None) -> str | None:
    """Render a DOM subtree as plain text.

    Paragraphs and headings are separated by a blank line, list items and
    ``<br>`` by a newline; list items get a "- " prefix. Entities are
    already decoded by the HTML parser.
    """
    if root is None:
        return None
    out: list[str] = []
    state = {"pending": 0, "bullet": False, "li_depth": 0}

    def walk(node: Node) -> None:
        tag = node.tag
        if tag == "-text":
            text = _ANY_SPACE_RE.sub(" ", node.text() or "")
            if not text.strip():
                if out and not state["pending"] and not out[-1].endswith(" "):
                    out.append(" ")
                return
            if state["pending"]:
                if out:
                    out.append("\n" * state["pending"])
                state["pending"] = 0
                text = text.lstrip()
            elif not out:
                text = text.lstrip()
            if state["bullet"]:
                text = "- " + text
                state["bullet"] = False
            out.append(text)
            return
        if tag is None or tag.startswith("-") or tag in _TEXT_SKIP_TAGS:
            return
        if tag == "br":
            state["pending"] = min(2, state["pending"] + 1)
            return
        is_item = tag in _TEXT_LINE_TAGS
        is_block = tag in _TEXT_BLOCK_TAGS
        if is_item or is_block:
            strength = 1 if (is_item or state["li_depth"]) else 2
            state["pending"] = max(state["pending"], strength)
        if tag == "li":
            state["li_depth"] += 1
            state["bullet"] = True
        for child in node.iter(include_text=True):
            walk(child)
        if tag == "li":
            state["li_depth"] -= 1
            state["bullet"] = False
        if is_item or is_block:
            strength = 1 if (is_item or state["li_depth"]) else 2
            state["pending"] = max(state["pending"], strength)

    walk(root)
    lines = [_SPACE_RE.sub(" ", line).strip() for line in "".join(out).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def _html_to_text(fragment: Any) -> str | None:
    """HTML fragment (ad body as stored by FINN) -> readable plain text."""
    if not isinstance(fragment, str) or not fragment.strip():
        return None
    tree = HTMLParser(fragment)
    return _node_to_text(tree.body or tree.root)


def _decode_turbo_stream(flat: Any) -> Any:
    """Rebuild the object tree from a turbo-stream "flat" array.

    Element 0 is the root. Objects are ``{"_<key index>": <value index>}``,
    arrays are lists of value indices, negative indices are the
    null/undefined sentinels. Arrays that start with a string are typed
    values (dates, promises, …) that the ad data does not use; they decode
    to ``None``.
    """
    if not isinstance(flat, list) or not flat:
        return None
    size = len(flat)
    cache: dict[int, Any] = {}

    def hydrate(index: Any) -> Any:
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        if index < 0 or index >= size:
            return None
        if index in cache:
            return cache[index]
        value = flat[index]
        if isinstance(value, dict):
            obj: dict[str, Any] = {}
            cache[index] = obj
            for raw_key, value_index in value.items():
                key: Any = None
                if raw_key.startswith("_") and raw_key[1:].isdigit():
                    key_index = int(raw_key[1:])
                    key = flat[key_index] if key_index < size else None
                if isinstance(key, str):
                    obj[key] = hydrate(value_index)
            return obj
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                cache[index] = None
                return None
            items: list[Any] = []
            cache[index] = items
            items.extend(hydrate(i) for i in value)
            return items
        return value

    return hydrate(0)


def _find_job_ad(tree: Any) -> dict[str, Any] | None:
    """Locate ``jobAd`` ({ad, meta, adSummary, …}) anywhere in the tree."""
    stack = [tree]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            ad = node.get("ad")
            if isinstance(ad, dict) and ("description" in ad or "jobTitle" in ad):
                return node
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def extract_embedded_job_ad(html: str) -> dict[str, Any] | None:
    """Return the ad page's embedded ``jobAd`` object, or None."""
    for m in _ENQUEUE_RE.finditer(html):
        try:
            payload = json.loads(m.group(1))
            # Later lines of a chunk, if any, are deferred promise values.
            flat = json.loads(payload.split("\n", 1)[0])
            job_ad = _find_job_ad(_decode_turbo_stream(flat))
        except (ValueError, RecursionError, AttributeError):
            continue
        if job_ad:
            return job_ad
    return None


def _label(value: Any) -> str | None:
    """``{"id": …, "value": "Fast"}`` or a plain string -> "Fast"."""
    if isinstance(value, dict):
        value = value.get("value") or value.get("label") or value.get("name")
    return _str(value)


def _labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        label = _label(item)
        if label and label not in out:
            out.append(label)
    return out


def _parse_date(value: Any) -> datetime | None:
    """ISO date/datetime -> datetime. A bare date becomes midnight UTC,
    the same instant FINN's own search data uses for the publish date."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None and len(text) <= 10:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _deadline_fields(text: str | None) -> dict[str, Any]:
    """Deadline as shown on the page: "30.4.2026", "2026-04-30" or "Snarest"."""
    if not text:
        return {}
    m = _NO_DATE_RE.match(text)
    if m:
        return {"deadline": f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"}
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return {"deadline": text}
    return {"deadline_text": text}


def _format_location(
    street: str | None,
    postal_code: str | None,
    city: str | None,
    country: str | None = None,
) -> str | None:
    place = " ".join(p for p in (postal_code, city) if p)
    return ", ".join(p for p in (street, place, country) if p) or None


def _image_url(value: Any) -> str | None:
    return _str(value.get("url")) if isinstance(value, dict) else None


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    if value is None or value == "" or value == [] or value == {}:
        return
    target[key] = value


def _detail_from_embedded(html: str) -> dict[str, Any]:
    job_ad = extract_embedded_job_ad(html)
    if not job_ad:
        return {}
    ad = job_ad.get("ad") if isinstance(job_ad.get("ad"), dict) else {}
    meta = job_ad.get("meta") if isinstance(job_ad.get("meta"), dict) else {}
    attrs: dict[str, Any] = {}
    out: dict[str, Any] = {"attributes": attrs}

    _put(out, "title", _str(ad.get("jobTitle")) or _str(ad.get("heading")))
    _put(attrs, "heading", _str(ad.get("heading")))
    _put(out, "description", _html_to_text(ad.get("description")))
    _put(out, "published_at", _parse_date(meta.get("datePosted")))
    _put(attrs, "updated_at", _str(meta.get("edited")))
    _put(attrs, "status", _str(meta.get("adStatus")))
    _put(attrs, "ad_type", _str(job_ad.get("adType")))

    # full-time / part-time
    job_type = _EXTENT_JOB_TYPE.get(str(ad.get("jobType") or "").lower())
    extent = ad.get("extent")
    if isinstance(extent, dict):
        _put(attrs, "extent", _label(extent))
        if not job_type:
            job_type = _EXTENT_JOB_TYPE.get(str(extent.get("id") or "")) or (
                _EXTENT_JOB_TYPE.get((_label(extent) or "").lower())
            )
    _put(attrs, "job_type", job_type)

    _put(attrs, "employment_form", _label(ad.get("duration")))
    _put(attrs, "sector", _label(ad.get("sector")))
    _put(attrs, "leadership_category", _label(ad.get("leadershipCategory")))
    positions = ad.get("noOfPositions")
    if _is_number(positions) and positions > 0:
        attrs["positions"] = int(positions)

    deadline = ad.get("applicationDeadline")
    if isinstance(deadline, dict):
        attrs.update(_deadline_fields(_str(deadline.get("date"))))
        _put(attrs, "deadline_text", _str(deadline.get("note")))
    _put(attrs, "application_url", _str(ad.get("applicationUrl")))

    _put(attrs, "occupations", _labels(ad.get("occupations")))
    _put(attrs, "industries", _labels(ad.get("industry")))
    _put(attrs, "remote_options", _labels(ad.get("remoteOption")))
    _put(attrs, "working_languages", _labels(ad.get("workingLanguage")))
    _put(attrs, "keywords", _labels(ad.get("keywords")))
    skills = ad.get("skills")
    if isinstance(skills, list):
        _put(attrs, "skills", _labels(skills))
        essential = [
            s
            for s in skills
            if isinstance(s, dict) and s.get("requirementLevel") == "ESSENTIAL"
        ]
        _put(attrs, "essential_skills", _labels(essential))

    # workplace
    loc = ad.get("location")
    if isinstance(loc, dict):
        street = _str(loc.get("streetAddress"))
        postal_code = _str(loc.get("postalCode"))
        city = _str(loc.get("postalName"))
        country = None
        if str(loc.get("countryCode") or "NO").upper() != "NO":
            country = _str(loc.get("countryName"))
        _put(out, "location", _format_location(street, postal_code, city, country))
        _put(attrs, "street_address", street)
        _put(attrs, "postal_code", postal_code)
        _put(attrs, "city", city)
        _put(attrs, "country", _str(loc.get("countryName")))
        areas = loc.get("postalCodeAreaNodes")
        if isinstance(areas, list) and areas:
            district = _label(areas[0])
            if district and district != city:
                attrs["district"] = district
        geo = loc.get("geoPosition")
        if isinstance(geo, dict):
            if _is_number(geo.get("lat")) and _is_number(geo.get("lon")):
                attrs["latitude"] = float(geo["lat"])
                attrs["longitude"] = float(geo["lon"])

    # employer and contact persons
    seller: dict[str, Any] = {}
    company = ad.get("company")
    if isinstance(company, dict):
        _put(seller, "name", _str(company.get("name")))
        _put(seller, "url", _str(company.get("url")))
        _put(seller, "organisation_number", _str(company.get("organisationNumber")))
        _put(attrs, "employer_description", _html_to_text(company.get("description")))
    _put(seller, "logo_url", _image_url(ad.get("logo")))
    _put(out, "seller", seller)

    contacts: list[dict[str, Any]] = []
    people = ad.get("contact")
    for person in people if isinstance(people, list) else []:
        if not isinstance(person, dict):
            continue
        contact: dict[str, Any] = {}
        _put(contact, "name", _str(person.get("name")))
        _put(contact, "title", _str(person.get("title")))
        for src in ("mobile", "phone"):
            number = person.get(src)
            if isinstance(number, dict):
                number = number.get("original") or number.get("formatted")
            _put(contact, src, _str(number))
        _put(contact, "email", _str(person.get("email")))
        if contact:
            contacts.append(contact)
    _put(attrs, "contacts", contacts)

    images: list[str] = []
    extra_images = ad.get("additionalImages")
    for image in [ad.get("mainImage"), *(extra_images if isinstance(extra_images, list) else [])]:
        url = _image_url(image)
        if url and url not in images:
            images.append(url)
    _put(out, "images", images)

    # FINN's own short version of the ad ("Kortversjonen")
    summary = job_ad.get("adSummary")
    if isinstance(summary, dict):
        _put(attrs, "summary", _str(summary.get("shortDescription")))
        _put(attrs, "summary_qualifications", _labels(summary.get("qualifications")))
        _put(attrs, "summary_offers", _labels(summary.get("whatWeOffer")))

    # where an imported ad was originally published
    source = ad.get("source")
    if isinstance(source, dict):
        _put(attrs, "source", _str(source.get("name")))
        _put(attrs, "source_url", _str(source.get("url")))
    return out


def _detail_from_jsonld(html: str) -> dict[str, Any]:
    posting = find_by_type(extract_jsonld(html), "JobPosting")
    if not posting:
        return {}
    attrs: dict[str, Any] = {}
    out: dict[str, Any] = {"attributes": attrs}

    _put(out, "title", _str(posting.get("title")))
    _put(out, "description", _html_to_text(posting.get("description")))

    emp_type = posting.get("employmentType")
    if emp_type:
        attrs["employment_type"] = emp_type
        _put(attrs, "job_type", _job_type_from_employment(emp_type))
    valid_through = posting.get("validThrough")
    if valid_through:
        attrs["deadline"] = valid_through
    date_posted = posting.get("datePosted")
    if date_posted:
        published_at = _parse_date(str(date_posted))
        if published_at:
            out["published_at"] = published_at
        else:
            attrs["date_posted"] = date_posted

    hiring = posting.get("hiringOrganization")
    seller: dict[str, Any] = {}
    if isinstance(hiring, dict):
        _put(seller, "name", _str(hiring.get("name")))
        _put(seller, "url", _str(hiring.get("sameAs")) or _str(hiring.get("url")))
    elif isinstance(hiring, str):
        _put(seller, "name", _str(hiring))
    _put(out, "seller", seller)

    job_loc = posting.get("jobLocation")
    if isinstance(job_loc, list) and job_loc:
        job_loc = job_loc[0]
    addr = job_loc.get("address") if isinstance(job_loc, dict) else None
    if isinstance(addr, dict):
        street = _str(addr.get("streetAddress"))
        postal_code = _str(addr.get("postalCode"))
        city = _str(addr.get("addressLocality"))
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        country = _str(country)
        if country and country.upper() in ("NO", "NORGE", "NORWAY"):
            country = None
        _put(out, "location", _format_location(street, postal_code, city, country))
        _put(attrs, "street_address", street)
        _put(attrs, "postal_code", postal_code)
        _put(attrs, "city", city)
    return out


# Labelled facts in the rendered ad: label -> (attribute key, kind).
_DOM_FACTS: dict[str, tuple[str, str]] = {
    "sektor": ("sector", "text"),
    "ansettelsesform": ("employment_form", "text"),
    "lederkategori": ("leadership_category", "text"),
    "antall stillinger": ("positions", "int"),
    "hjemmekontor": ("remote_options", "list"),
    "bransje": ("industries", "list"),
    "stillingsfunksjon": ("occupations", "list"),
    "arbeidsspråk": ("working_languages", "list"),
    "sted": ("location", "location"),
    "frist": ("deadline", "deadline"),
    "søknadsfrist": ("deadline", "deadline"),
}


def _node_text(node: Node | None) -> str | None:
    return _clean(node.text(separator=" ", strip=True)) if node is not None else None


def _iter_dom_facts(scope: Node) -> Iterator[tuple[str, str, list[str]]]:
    """Yield (label, value text, link texts) for the ad's labelled facts.

    Two shapes are in use: a detail list ``<li><span class="font-bold">
    Label:</span> value</li>`` and key-info tiles ``<li class="flex
    flex-col">Label <strong>value</strong></li>``.
    """
    for li in scope.css("li"):
        full = _node_text(li)
        if not full or len(full) > 400:
            continue
        links = [t for t in (_node_text(a) for a in li.css("a")) if t]
        first_text = _node_text(li.css_first("span"))
        label: str | None = None
        value: str | None = None
        if first_text and first_text.endswith(":") and full.startswith(first_text):
            label = first_text[:-1].strip()
            value = full[len(first_text):].strip()
        else:
            value = _node_text(li.css_first("strong") or li.css_first("span.font-bold"))
            if value and full.endswith(value) and len(full) > len(value):
                label = full[: -len(value)].strip().rstrip(":").strip()
        if label and value:
            yield label, value, links


def _detail_from_dom(html: str) -> dict[str, Any]:
    tree = HTMLParser(html)
    scope = tree.css_first('section[aria-label="Jobbdetaljer"]') or tree.body
    if scope is None:
        return {}
    attrs: dict[str, Any] = {}
    seller: dict[str, Any] = {}
    out: dict[str, Any] = {"attributes": attrs}

    title_node = scope.css_first("h2.t2")
    heading = _node_text(scope.css_first("h1") or tree.css_first("h1"))
    _put(out, "title", _node_text(title_node) or heading)
    _put(attrs, "heading", heading)

    # Ad body: the first imported-HTML container (the other one, when
    # present, is the employer presentation under "Om arbeidsgiveren").
    body = scope.css_first('[data-testid="ad-description"] .import-decoration')
    for node in scope.css(".import-decoration"):
        section = node.parent
        h2 = section.css_first("h2") if section is not None else None
        if _node_text(h2) == "Om arbeidsgiveren":
            _put(attrs, "employer_description", _node_to_text(node))
        elif body is None:
            body = node
    _put(out, "description", _node_to_text(body))

    # Employer name: header strip (new layout) or the line under the job
    # title (old layout); the logo link's label as a last resort.
    employer = _node_text(scope.css_first(".header-logo-container strong"))
    sibling = title_node.next if title_node is not None else None
    while sibling is not None:
        text = _node_text(sibling) if sibling.tag in ("p", "div") else None
        if text and sibling.tag == "p" and not employer:
            employer = text
        elif text and sibling.tag == "div":
            # "Fast stilling ∙ Deltid ∙ Oslo"
            for token in (t.strip() for t in text.split("∙")):
                if token.lower().endswith(" stilling"):
                    _put(attrs, "employment_form", token[: -len(" stilling")].strip())
                elif token.lower() in ("heltid", "deltid"):
                    attrs["extent"] = token
                    attrs["job_type"] = _EXTENT_JOB_TYPE[token.lower()]
        sibling = sibling.next
    logo = scope.css_first("a.company-logo")
    if logo is not None:
        label = _clean(logo.attributes.get("aria-label"))
        if not employer and label and label.endswith(" logo"):
            employer = label[: -len(" logo")].strip()
        href = _clean(logo.attributes.get("href"))
        if href and href.startswith("http") and "finn.no" not in href:
            seller["url"] = href
    _put(seller, "name", employer)

    fact_scopes = [scope]
    for section in tree.css("section"):
        if _node_text(section.css_first("h2")) == "Annonseinformasjon":
            fact_scopes.append(section)
            stamp = section.css_first("time[datetime]")
            if stamp is not None:
                _put(attrs, "updated_at", _clean(stamp.attributes.get("datetime")))
            for li in section.css("li"):
                text = _node_text(li) or ""
                m = re.search(r"(?<!\d)\d{9}(?!\d)", text)
                if text.startswith("Org.nr") and m:
                    seller["organisation_number"] = m.group(0)
    for fact_scope in fact_scopes:
        for label, value, links in _iter_dom_facts(fact_scope):
            spec = _DOM_FACTS.get(label.lower())
            if not spec:
                continue
            key, kind = spec
            if kind == "text":
                attrs.setdefault(key, value)
            elif kind == "int":
                number = _to_int(value)
                if number is not None:
                    attrs.setdefault(key, number)
            elif kind == "list":
                items = links or [v.strip() for v in value.split(",") if v.strip()]
                attrs.setdefault(key, items)
            elif kind == "location":
                out.setdefault("location", value)
            elif kind == "deadline":
                for k, v in _deadline_fields(value).items():
                    attrs.setdefault(k, v)

    contacts: list[dict[str, Any]] = []
    for card in scope.css(".contact-card"):
        name_node = card.css_first(".contact-card-name")
        name = _node_text(name_node)
        if not name:
            continue
        contact = {"name": name}
        title_el = name_node.next
        while title_el is not None and title_el.tag != "p":
            title_el = title_el.next
        _put(contact, "title", _node_text(title_el))
        contacts.append(contact)
    _put(attrs, "contacts", contacts)

    # The apply button sits in the side column, outside the details section.
    for a in tree.css("a[href]"):
        text = _node_text(a) or ""
        if text.startswith(("Søk her", "Gå til søknad")):
            href = _clean(a.attributes.get("href"))
            if href:
                attrs["application_url"] = (
                    href if href.startswith("http") else f"https://www.finn.no{href}"
                )
                break

    og_image = tree.css_first('meta[property="og:image"]')
    if og_image is not None:
        url = _clean(og_image.attributes.get("content"))
        if url and "images.finncdn.no" in url:
            out["images"] = [url]

    _put(out, "seller", seller)
    return out


def _safe_part(extract: Any, html: str) -> dict[str, Any]:
    """Run one source extractor; a broken source contributes nothing."""
    try:
        return extract(html) or {}
    except Exception:
        return {}


def _first(parts: list[dict[str, Any]], key: str) -> Any:
    for part in parts:
        value = part.get(key)
        if value:
            return value
    return None


class JobsScraper(VerticalScraper):
    vertical = "jobs"
    link_pattern = "/job/ad/"
    finnkode_re = re.compile(r"/job/ad/(\d+)")

    def search_url(
        self,
        query: str,
        page: int,
        filters: dict[str, str] | None,
        job_type: JobType = "all",
    ) -> tuple[str, dict[str, str]]:
        params: dict[str, str] = {"q": query} if query else {}
        if page > 1:
            params["page"] = str(page)
        if filters:
            params.update(filters)
        if job_type != "all":
            if job_type not in JOB_TYPE_EXTENT:
                raise ValueError(
                    f"unknown job_type: {job_type!r}; "
                    "expected 'fulltime', 'parttime' or 'all'"
                )
            # An explicit job_type wins over any ``extent`` in filters.
            params["extent"] = JOB_TYPE_EXTENT[job_type]
        return "https://www.finn.no/job/search", params

    async def search(  # type: ignore[override]
        self,
        query: str,
        page: int = 1,
        filters: dict[str, str] | None = None,
        job_type: JobType = "all",
    ) -> list[SearchResult]:
        url, params = self.search_url(query, page, filters, job_type)
        html = await http_client.fetch(url, params=params)
        return self.parse_search_cards(html)

    def detail_url(self, finnkode: str) -> str:
        return f"https://www.finn.no/job/ad/{finnkode}"

    def _guess_title(self, article, text):  # type: ignore[override]
        # Jobs cards put the job title in the <a> anchor text (not h2/h3).
        # h3 in the card is the "hook" sub-line; avoid picking it.
        a = article.css_first(f'a[href*="{self.link_pattern}"]')
        if a:
            title = _clean(a.text(separator=" ", strip=True))
            if title and len(title) > 2:
                return title
        return super()._guess_title(article, text)

    # ----- search cards -----
    #
    # Card anatomy (identical in the April and November 2026 markup):
    #   <a id="card-anchor-…">job title</a>  <strong>employer</strong>
    #   <h3>ad heading</h3>                  (omitted when equal to the title)
    #   <footer>
    #     <ul><li><span>workplace</span></li>
    #         <li><time datetime=…>4 dager siden</time></li></ul>
    #     <p>salary line</p>                 (optional)
    #     <div>badges</div>                  (optional)
    #   </footer>
    # Deadline and number of positions are not rendered on the card; they
    # only exist in the embedded search data and are merged in from there.

    def parse_search_cards(self, html: str) -> list[SearchResult]:
        results = super().parse_search_cards(html)
        try:
            docs = extract_embedded_job_docs(html)
        except Exception:  # hydration data is optional enrichment only
            docs = {}
        for result in results:
            doc = docs.get(result.finnkode)
            if doc:
                self._enrich_from_doc(result, doc)
        return results

    def _parse_card(self, finnkode, href, article):  # type: ignore[override]
        result = super()._parse_card(finnkode, href, article)
        if result is not None:
            # Jobs have no price; the generic parser would otherwise pick up
            # salary/bonus amounts mentioned in the card text.
            result.price = None
        return result

    def _guess_location(self, article: Node, text: str) -> str | None:
        span = article.css_first("footer li span")
        if span is None:
            return None
        location = _clean(span.text(separator=" ", strip=True))
        if not location:
            return None
        return _MORE_LOCATIONS_RE.sub("", location) or None

    def _card_extras(self, article: Node, text: str) -> dict[str, Any]:
        extra: dict[str, Any] = {}

        employer = None
        strong = article.css_first("strong")
        if strong is not None:
            employer = _clean(strong.text(separator=" ", strip=True))
        if not employer:
            img = article.css_first("img")
            alt = _clean(img.attributes.get("alt")) if img is not None else None
            if alt and alt.endswith(" logo"):
                employer = alt[: -len(" logo")].strip() or None
        if employer:
            extra["employer"] = employer

        h3 = article.css_first("h3")
        if h3 is not None:
            heading = _clean(h3.text(separator=" ", strip=True))
            if heading:
                extra["heading"] = heading

        time_el = article.css_first("footer time") or article.css_first("time")
        if time_el is not None:
            published = _clean(time_el.attributes.get("datetime"))
            if published:
                extra["published"] = published
            published_text = _clean(time_el.text(separator=" ", strip=True))
            if published_text:
                extra["published_text"] = published_text

        footer = article.css_first("footer")
        if footer is None:
            return extra

        span = footer.css_first("li span")
        if span is not None:
            m = _MORE_LOCATIONS_RE.search(_clean(span.text(strip=True)) or "")
            if m:
                extra["additional_locations"] = int(m.group(1))

        labels: list[str] = []
        for child in footer.iter():
            if child.tag == "ul":
                continue
            if child.tag == "p":
                salary_text = _clean(child.text(separator=" ", strip=True))
                if salary_text and "salary_text" not in extra:
                    extra.update(_salary_fields(salary_text))
                continue
            for badge in list(child.iter()) or [child]:
                label = _clean(badge.text(separator=" ", strip=True))
                if label and label not in labels:
                    labels.append(label)
        if (
            _SPONSORED_LABEL in labels
            or footer.css_first('[variant="sponsored"]') is not None
        ):
            extra["sponsored"] = True
        if labels:
            extra["labels"] = labels
        return extra

    def _enrich_from_doc(self, result: SearchResult, doc: dict[str, Any]) -> None:
        """Merge the embedded search entry into a card; DOM values win."""
        extra = result.extra

        if not result.location:
            result.location = _str(doc.get("location"))
        if not result.thumbnail_url:
            logo = doc.get("logo") or doc.get("image")
            if isinstance(logo, dict):
                result.thumbnail_url = _str(logo.get("url"))

        if "employer" not in extra:
            company = _str(doc.get("company_name"))
            if company:
                extra["employer"] = company

        if "heading" not in extra:
            heading = _str(doc.get("heading"))
            if heading and heading != result.title:
                extra["heading"] = heading

        if "published" not in extra:
            published = _epoch_ms_to_iso(doc.get("published") or doc.get("timestamp"))
            if published:
                extra["published"] = published

        deadline = _epoch_ms_to_local_date(doc.get("deadline"))
        if deadline:
            extra["deadline"] = deadline

        positions = doc.get("no_of_positions")
        if _is_number(positions) and positions > 0:
            extra["positions"] = int(positions)

        locations = doc.get("locations")
        if isinstance(locations, list):
            places = [p for p in (_str(x) for x in locations) if p and p != "Norge"]
            if places:
                extra["locations"] = places

        comp = doc.get("compensation")
        if isinstance(comp, dict):
            for src, key in (
                ("salary_range_min", "salary_min"),
                ("salary_range_max", "salary_max"),
            ):
                value = comp.get(src)
                if key not in extra and _is_number(value):
                    extra[key] = int(value)
            note = _str(comp.get("additional_compensation"))
            if note:
                extra["salary_note"] = note

        doc_labels = doc.get("labels")
        if isinstance(doc_labels, list):
            labels = list(extra.get("labels") or [])
            for item in doc_labels:
                label = _str(item.get("text")) if isinstance(item, dict) else None
                if label and label not in labels:
                    labels.append(label)
            if labels:
                extra["labels"] = labels

    # ----- ad page -----

    def parse_detail(self, finnkode: str, html: str) -> Listing:
        """Parse a job ad page.

        Three sources are read independently and merged field by field in
        this order: the page's embedded route data, JSON-LD ``JobPosting``,
        and the rendered DOM. Each one is optional; a source that is missing
        or fails to parse simply contributes nothing.
        """
        parts = [
            _safe_part(_detail_from_embedded, html),
            _safe_part(_detail_from_jsonld, html),
            _safe_part(_detail_from_dom, html),
        ]

        attributes: dict[str, Any] = {}
        seller: dict[str, Any] = {}
        for part in parts:  # first source to provide a key wins
            for key, value in (part.get("attributes") or {}).items():
                attributes.setdefault(key, value)
            for key, value in (part.get("seller") or {}).items():
                seller.setdefault(key, value)

        # schema.org employmentType only exists in JSON-LD; ads without it
        # get the equivalent value from what the page says about extent.
        job_type = attributes.get("job_type")
        if "employment_type" not in attributes and job_type in _JOB_TYPE_EMPLOYMENT:
            attributes["employment_type"] = [_JOB_TYPE_EMPLOYMENT[job_type]]
        if "extent" not in attributes and job_type in _JOB_TYPE_EXTENT_LABEL:
            attributes["extent"] = _JOB_TYPE_EXTENT_LABEL[job_type]

        title = _first(parts, "title")
        heading = attributes.get("heading")
        if heading and (title is None or heading == title):
            attributes.pop("heading")
            title = title or heading

        return Listing(
            finnkode=finnkode,
            vertical=self.vertical,
            url=self.detail_url(finnkode),
            title=title or f"Job listing {finnkode}",
            description=_first(parts, "description"),
            price=None,
            currency="NOK",
            location=_first(parts, "location"),
            images=_first(parts, "images") or [],
            attributes=attributes,
            seller=seller or None,
            published_at=_first(parts, "published_at"),
            fetched_at=now_utc(),
        )
