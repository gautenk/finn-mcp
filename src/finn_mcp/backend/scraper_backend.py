from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .. import http_client
from ..cache import Cache
from ..models import Filters, ALL_JOB_TYPES, ALL_VERTICALS, JobType, Listing, SearchResult, Vertical
from ..scraper import get_scraper
from .base import FinnBackend, ListingNotFound

log = logging.getLogger(__name__)

# Probe order when the caller hasn't supplied a vertical. Roughly by popularity.
_VERTICAL_PROBE_ORDER: tuple[Vertical, ...] = (
    "cars_used",
    "bap",
    "homes",
    "lettings",
    "jobs",
)


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
            return await self._fetch_and_cache(finnkode, vertical)

        # No hint, no cache — probe each vertical until one returns 200.
        last_exc: Exception | None = None
        for candidate in _VERTICAL_PROBE_ORDER:
            try:
                return await self._fetch_and_cache(finnkode, candidate)
            except http_client.NotFoundError:
                continue
            except (http_client.BlockedError, http_client.RateLimitedError):
                # Probing the remaining verticals would only add requests.
                raise
            except Exception as exc:  # network, parse, etc.
                log.warning("probe %s failed for %s: %s", candidate, finnkode, exc)
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise ListingNotFound(finnkode)

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
