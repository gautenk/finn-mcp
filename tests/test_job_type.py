"""Full-time / part-time (``job_type``) support for the jobs vertical."""

from __future__ import annotations

import sqlite3

import pytest

from finn_mcp import http_client, server
from finn_mcp.backend.scraper_backend import ScraperBackend
from finn_mcp.cache import Cache
from finn_mcp.models import SavedSearch, SearchResult
from finn_mcp.scraper import get_scraper
from .conftest import read_fixture

OSLO = {"location": "1.20001.20061"}


# ---------- URL building ----------

@pytest.mark.parametrize(
    "job_type,expected_extent",
    [("fulltime", "3947"), ("parttime", "3942"), ("all", None)],
)
def test_jobs_search_url_job_type(job_type, expected_extent):
    url, params = get_scraper("jobs").search_url("lærer", 1, OSLO, job_type)
    assert url == "https://www.finn.no/job/search"
    assert params["q"] == "lærer"
    assert params["location"] == "1.20001.20061"
    assert params.get("extent") == expected_extent


def test_jobs_search_url_defaults_to_all():
    _, params = get_scraper("jobs").search_url("", 2, None)
    assert params == {"page": "2"}


def test_jobs_search_url_all_keeps_explicit_extent_filter():
    _, params = get_scraper("jobs").search_url("", 1, {"extent": "3942"}, "all")
    assert params["extent"] == "3942"


def test_jobs_search_url_job_type_overrides_extent_filter():
    _, params = get_scraper("jobs").search_url("", 1, {"extent": "3942"}, "fulltime")
    assert params["extent"] == "3947"


def test_jobs_search_url_rejects_unknown_job_type():
    with pytest.raises(ValueError):
        get_scraper("jobs").search_url("", 1, None, "weekends")  # type: ignore[arg-type]


# ---------- parsing part-time fixtures ----------

def test_parttime_search_cards():
    results = get_scraper("jobs").parse_search_cards(
        read_fixture("jobs_parttime_search.html")
    )
    assert len(results) >= 40
    assert results[0].finnkode == "480669377"
    assert results[0].title == "Vil du bli en del av teamet mitt?"
    assert len({r.finnkode for r in results}) == len(results)
    for r in results:
        assert r.vertical == "jobs"
        assert r.finnkode.isdigit()
        assert r.title
        assert r.url == f"https://www.finn.no/job/ad/{r.finnkode}"


def test_parttime_detail():
    listing = get_scraper("jobs").parse_detail(
        "480669377", read_fixture("jobs_parttime_ad.html")
    )
    assert listing.title == "Vil du bli en del av teamet mitt?"
    assert listing.attributes["employment_type"] == ["PART_TIME"]
    assert listing.attributes["job_type"] == "parttime"
    assert listing.location and "Oslo" in listing.location
    assert listing.seller and listing.seller["name"]
    assert listing.published_at is not None


# ---------- backend ----------

@pytest.fixture
def captured_fetch(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_fetch(url, params=None, polite=True):
        calls.append((url, dict(params or {})))
        return read_fixture("jobs_parttime_search.html")

    monkeypatch.setattr(http_client, "fetch", fake_fetch)
    return calls


async def test_backend_search_passes_job_type(tmp_path, captured_fetch):
    backend = ScraperBackend(cache=Cache(db_path=tmp_path / "c.sqlite"))
    results = await backend.search("jobs", "", filters=OSLO, job_type="parttime")
    assert results
    assert captured_fetch == [
        ("https://www.finn.no/job/search", {"location": "1.20001.20061", "extent": "3942"})
    ]


async def test_backend_rejects_job_type_for_other_verticals(tmp_path, captured_fetch):
    backend = ScraperBackend(cache=Cache(db_path=tmp_path / "c.sqlite"))
    with pytest.raises(ValueError):
        await backend.search("bap", "sofa", job_type="parttime")
    assert captured_fetch == []


# ---------- saved searches ----------

async def test_saved_search_job_type_round_trip(tmp_path):
    cache = Cache(db_path=tmp_path / "c.sqlite")
    await cache.save_search(
        SavedSearch(name="deltid", vertical="jobs", query="", job_type="parttime")
    )
    await cache.save_search(SavedSearch(name="alt", vertical="jobs", query=""))
    assert (await cache.get_search("deltid")).job_type == "parttime"
    assert (await cache.get_search("alt")).job_type == "all"


async def test_saved_search_migrates_db_without_job_type_column(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE saved_searches (
          name TEXT PRIMARY KEY, vertical TEXT NOT NULL, query TEXT NOT NULL,
          filters_json TEXT NOT NULL DEFAULT '{}', last_checked_at INTEGER,
          last_finnkodes_json TEXT NOT NULL DEFAULT '[]'
        );
        INSERT INTO saved_searches (name, vertical, query) VALUES ('old', 'jobs', 'x');
        """
    )
    conn.commit()
    conn.close()

    cache = Cache(db_path=db)
    old = await cache.get_search("old")
    assert old is not None and old.job_type == "all"
    await cache.save_search(
        SavedSearch(name="new", vertical="jobs", query="", job_type="fulltime")
    )
    assert (await cache.get_search("new")).job_type == "fulltime"


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.finnkodes: list[str] = []
        self.calls: list[dict] = []

    async def search(self, vertical, query, page=1, filters=None, job_type="all"):
        self.calls.append(
            {"vertical": vertical, "query": query, "filters": filters, "job_type": job_type}
        )
        return [
            SearchResult(
                finnkode=k,
                vertical=vertical,
                title=f"Jobb {k}",
                url=f"https://www.finn.no/job/ad/{k}",
            )
            for k in self.finnkodes
        ]


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(server, "_backend", lambda: backend)
    monkeypatch.setattr(server, "_cache", Cache(db_path=tmp_path / "c.sqlite"))
    return backend


async def test_check_saved_search_with_job_type_returns_only_new(fake_server):
    saved = await server.save_search(
        "deltid-oslo", "jobs", "", filters=OSLO, job_type="parttime"
    )
    assert saved.job_type == "parttime"
    assert [s.job_type for s in await server.list_saved_searches()] == ["parttime"]

    fake_server.finnkodes = ["1", "2"]
    first = await server.check_saved_search("deltid-oslo")
    assert [r.finnkode for r in first] == ["1", "2"]

    # Nothing new since last check.
    assert await server.check_saved_search("deltid-oslo") == []

    # One new listing appears; one old one drops out.
    fake_server.finnkodes = ["3", "2"]
    third = await server.check_saved_search("deltid-oslo")
    assert [r.finnkode for r in third] == ["3"]

    assert all(c["job_type"] == "parttime" for c in fake_server.calls)
    assert all(c["filters"] == OSLO for c in fake_server.calls)


async def test_search_finn_passes_job_type_and_defaults_to_all(fake_server):
    await server.search_finn("jobs", "kokk", job_type="fulltime")
    await server.search_finn("jobs", "kokk")
    assert [c["job_type"] for c in fake_server.calls] == ["fulltime", "all"]


async def test_save_search_rejects_job_type_for_other_verticals(fake_server):
    result = await server.save_search("x", "bap", "sofa", job_type="parttime")
    assert result["error"] == "invalid_argument"
    assert await server.list_saved_searches() == []


async def test_search_finn_tool_schema_exposes_job_type():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    for name in ("search_finn", "save_search"):
        prop = tools[name].inputSchema["properties"]["job_type"]
        assert prop["default"] == "all"
        assert sorted(prop["enum"]) == ["all", "fulltime", "parttime"]
