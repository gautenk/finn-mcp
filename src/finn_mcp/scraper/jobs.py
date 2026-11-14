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

    def parse_detail(self, finnkode: str, html: str) -> Listing:
        blocks = extract_jsonld(html)
        posting = find_by_type(blocks, "JobPosting")

        title: str | None = None
        description: str | None = None
        location: str | None = None
        attributes: dict[str, Any] = {}
        seller: dict[str, Any] | None = None
        published_at: datetime | None = None

        if posting:
            title = _clean(posting.get("title"))
            description = _clean(posting.get("description"))
            emp_type = posting.get("employmentType")
            if emp_type:
                attributes["employment_type"] = emp_type
                job_type = _job_type_from_employment(emp_type)
                if job_type:
                    attributes["job_type"] = job_type
            valid_through = posting.get("validThrough")
            if valid_through:
                attributes["deadline"] = valid_through
            date_posted = posting.get("datePosted")
            if date_posted:
                try:
                    published_at = datetime.fromisoformat(str(date_posted))
                except ValueError:
                    attributes["date_posted"] = date_posted
            hiring = posting.get("hiringOrganization")
            if isinstance(hiring, dict):
                seller = {
                    "name": hiring.get("name"),
                    "url": hiring.get("sameAs") or hiring.get("url"),
                }
            elif isinstance(hiring, str):
                seller = {"name": hiring}
            job_loc = posting.get("jobLocation")
            if isinstance(job_loc, list) and job_loc:
                job_loc = job_loc[0]
            if isinstance(job_loc, dict):
                addr = job_loc.get("address")
                if isinstance(addr, dict):
                    parts = [
                        addr.get("addressLocality"),
                        addr.get("addressRegion"),
                        addr.get("addressCountry"),
                    ]
                    location = _clean(
                        ", ".join(str(p) for p in parts if p)
                    )

        if title is None:
            tree = HTMLParser(html)
            h1 = tree.css_first("h1")
            if h1:
                title = _clean(h1.text(strip=True))

        return Listing(
            finnkode=finnkode,
            vertical=self.vertical,
            url=self.detail_url(finnkode),
            title=title or f"Job listing {finnkode}",
            description=description,
            price=None,
            currency="NOK",
            location=location,
            images=[],
            attributes=attributes,
            seller=seller,
            published_at=published_at,
            fetched_at=now_utc(),
        )
