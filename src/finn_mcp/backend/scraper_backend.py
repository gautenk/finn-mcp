from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .. import http_client
from ..cache import Cache
from ..models import Filters, ALL_JOB_TYPES, ALL_VERTICALS, JobType, Listing, SearchResult, Vertical
from ..scraper import get_scraper
from .base import FinnBackend, ListingNotFound

log = logging.getLogger(__name__)

# Path prefixes of ad pages, as reached through finn.no/<finnkode>.
_URL_VERTICALS: tuple[tuple[str, Vertical], ...] = (
    ("/job/", "jobs"),
    ("/recommerce/", "bap"),
    ("/mobility/", "cars_used"),
    ("/realestate/lettings", "lettings"),
    ("/realestate/", "homes"),
)


def vertical_for_url(url: str) -> Vertical | None:
    """Vertical of an ad page URL, or None for pages that are not ads."""
    parts = urlsplit(url)
    if not re.search(r"\d{6,}", parts.path + "?" + parts.query):
        return None
    for prefix, vertical in _URL_VERTICALS:
        if parts.path.startswith(prefix):
            return vertical
    return None


class ScraperBackend(FinnBackend):
    name = "scraper"

    def __init__(self, cache: Cache | None = None):
        self.cache = cache or Cache()

    async def search(
        self,
        vertical: Vertical,
        query: str,
        page: int = 1,
        filters: Filters | None = None,
        job_type: JobType = "all",
    ) -> list[SearchResult]:
        if vertical not in ALL_VERTICALS:
            raise ValueError(f"unknown vertical: {vertical}")
        if job_type not in ALL_JOB_TYPES:
            raise ValueError(f"unknown job_type: {job_type!r}")
        scraper = get_scraper(vertical)
        if vertical == "jobs":
            return await scraper.search(
                query=query, page=page, filters=filters, job_type=job_type
            )
        if job_type != "all":
            raise ValueError("job_type is only supported for vertical 'jobs'")
        return await scraper.search(query=query, page=page, filters=filters)

    async def get_listing(
        self,
        finnkode: str,
        vertical: Vertical | None = None,
    ) -> Listing:
        if not finnkode or not finnkode.isdigit():
            raise ValueError(f"invalid finnkode: {finnkode!r}")

        if vertical is None:
            vertical = await self.cache.vertical_hint(finnkode)

        row = await self.cache.get_listing_row(finnkode)
        if row is not None and (vertical is None or row["vertical"] == vertical):
            cached = self._listing_from_row(finnkode, row)
            # A cached page that yields no ad text is treated as a miss, so a
            # bad or half-parsed fetch is not served for the whole TTL.
            if cached is not None and cached.description:
                return cached
            vertical = row["vertical"]

        if vertical is not None:
            try:
                return await self._fetch_and_cache(finnkode, vertical)
            except http_client.NotFoundError:
                # Wrong vertical, or an ad type with its own URL (external
                # lettings, new-build projects): let finn.no route it.
                pass
        return await self._resolve_and_cache(finnkode)

    async def _resolve_and_cache(self, finnkode: str) -> Listing:
        """Fetch an ad through ``finn.no/<finnkode>``.

        finn.no redirects that address to the ad's real URL whatever the
        vertical, so one request both finds the vertical and gets the page.
        """
        try:
            html, final_url = await http_client.fetch_with_url(
                f"https://www.finn.no/{finnkode}"
            )
        except http_client.NotFoundError:
            raise ListingNotFound(finnkode) from None
        vertical = vertical_for_url(final_url)
        if vertical is None:
            raise ListingNotFound(finnkode)
        listing = get_scraper(vertical).parse_detail(finnkode, html)
        listing = listing.model_copy(update={"url": final_url})
        await self.cache.put_listing(listing, html)
        return listing

    def _listing_from_row(self, finnkode: str, row: dict[str, Any]) -> Listing | None:
        """Build a Listing from a cache row.

        The stored page is parsed again with the current parser, so parser
        fixes take effect without waiting for the cache to expire. Falls back
        to the stored parse result when there is no page or parsing fails.
        """
        raw_html = row.get("raw_html")
        if raw_html:
            try:
                listing = get_scraper(row["vertical"]).parse_detail(finnkode, raw_html)
                return listing.model_copy(
                    update={
                        "fetched_at": datetime.fromtimestamp(
                            row["fetched_at"], tz=timezone.utc
                        )
                    }
                )
            except Exception as exc:  # stale markup, parser bug
                log.warning("re-parse of cached %s failed: %s", finnkode, exc)
        try:
            return Listing.model_validate_json(row["parsed_json"])
        except ValueError:
            return None

    async def _fetch_and_cache(self, finnkode: str, vertical: Vertical) -> Listing:
        scraper = get_scraper(vertical)
        try:
            html, listing = await scraper.fetch_detail(finnkode)
        except http_client.NotFoundError:
            raise
        await self.cache.put_listing(listing, html)
        return listing

    async def aclose(self) -> None:
        await http_client.close_client()
