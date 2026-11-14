"""get_listing caching: re-parse cached pages, refetch incomplete ones, report blocks."""

from __future__ import annotations

import httpx
import pytest

from finn_mcp import http_client, server
from finn_mcp.backend.scraper_backend import ScraperBackend
from finn_mcp.cache import Cache
from .conftest import read_fixture

JOB = "480669377"
JOB_URL = f"https://www.finn.no/job/ad/{JOB}"


@pytest.fixture
def backend(tmp_path):
    return ScraperBackend(cache=Cache(db_path=tmp_path / "c.sqlite"))


@pytest.fixture
def fetches(monkeypatch):
    """Serve ``pages[url]`` and record every fetched URL."""
    state = {"pages": {}, "calls": []}

    async def fake_fetch(url, params=None, polite=True):
        state["calls"].append(url)
        page = state["pages"].get(url)
        if page is None:
            raise http_client.NotFoundError(url)
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(http_client, "fetch", fake_fetch)
    return state


async def test_complete_listing_is_served_from_cache(backend, fetches):
    fetches["pages"][JOB_URL] = read_fixture("jobs_parttime_ad.html")
    first = await backend.get_listing(JOB, vertical="jobs")
    second = await backend.get_listing(JOB, vertical="jobs")
    assert first.description
    assert second.description == first.description
    assert second.fetched_at.replace(microsecond=0) == first.fetched_at.replace(microsecond=0)
    assert fetches["calls"] == [JOB_URL]


async def test_listing_without_description_is_refetched(backend, fetches):
    # A page that parses to a bare title (what a parser miss looks like).
    fetches["pages"][JOB_URL] = "<html><body><h1>Bare tittel</h1></body></html>"
    bare = await backend.get_listing(JOB, vertical="jobs")
    assert bare.description is None

    fetches["pages"][JOB_URL] = read_fixture("jobs_parttime_ad.html")
    # No vertical given: the cached hint is used instead of probing.
    full = await backend.get_listing(JOB)
    assert full.description
    assert fetches["calls"] == [JOB_URL, JOB_URL]


async def test_cached_page_is_parsed_with_current_parser(backend, fetches, monkeypatch):
    from finn_mcp.scraper import get_scraper

    fetches["pages"][JOB_URL] = read_fixture("jobs_parttime_ad.html")
    await backend.get_listing(JOB, vertical="jobs")

    scraper = get_scraper("jobs")
    original = scraper.parse_detail

    def improved(finnkode, html):
        return original(finnkode, html).model_copy(update={"title": "Ny parser"})

    monkeypatch.setattr(scraper, "parse_detail", improved)
    assert (await backend.get_listing(JOB)).title == "Ny parser"
    assert fetches["calls"] == [JOB_URL]


async def test_block_stops_vertical_probing(backend, fetches):
    blocked = http_client.BlockedError("https://www.finn.no/mobility/item/1")
    fetches["pages"]["https://www.finn.no/mobility/item/1"] = blocked
    with pytest.raises(http_client.BlockedError):
        await backend.get_listing("1")
    # cars_used is probed first; the block must end the probe there.
    assert fetches["calls"] == ["https://www.finn.no/mobility/item/1"]


async def test_fetch_raises_blocked_on_403(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<p>Please enable JS</p>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_get_client():
        return client

    monkeypatch.setattr(http_client, "get_client", fake_get_client)
    with pytest.raises(http_client.BlockedError):
        await http_client.fetch("https://www.finn.no/job/search", polite=False)
    await client.aclose()


async def test_tools_report_blocked(monkeypatch, tmp_path):
    class Blocked:
        async def search(self, *a, **k):
            raise http_client.BlockedError("https://www.finn.no/x")

        async def get_listing(self, *a, **k):
            raise http_client.BlockedError("https://www.finn.no/x")

    monkeypatch.setattr(server, "_backend", lambda: Blocked())
    monkeypatch.setattr(server, "_cache", Cache(db_path=tmp_path / "c.sqlite"))

    assert (await server.search_finn("homes", "oslo"))["error"] == "blocked"
    assert (await server.get_listing("123"))["error"] == "blocked"
    await server.save_search("s", "homes", "oslo")
    assert (await server.check_saved_search("s"))["error"] == "blocked"
