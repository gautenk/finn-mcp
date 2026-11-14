"""Filtering job searches by area name (``location``)."""

from __future__ import annotations

import httpx
import pytest

from finn_mcp import http_client, locations, server
from finn_mcp.backend.scraper_backend import ScraperBackend
from finn_mcp.cache import Cache
from .conftest import read_fixture

OSLO = "1.20001.20061"
BERGEN = "2.20001.22046.20220"
VESTLAND = "1.20001.22046"


# ---------- name resolution ----------

@pytest.mark.parametrize(
    "text,code",
    [
        ("Oslo", OSLO),
        ("oslo", OSLO),
        ("  OSLO ", OSLO),
        ("Bergen", BERGEN),
        ("Vestland", VESTLAND),
        ("Norge", "0.20001"),
        ("Bjerke", "2.20001.20061.20528"),
        ("Bygdøy - Frogner", "2.20001.20061.20507"),
        ("Frogner", "2.20001.20061.20507"),
        ("Frogner (Oslo)", "2.20001.20061.20507"),
        ("Oslo/Frogner", "2.20001.20061.20507"),
        ("Bergen (Vestland)", BERGEN),
        (OSLO, OSLO),
        ("9.99999.1", "9.99999.1"),
    ],
)
def test_resolve_one(text, code):
    assert locations.resolve_one(text).code == code


def test_resolve_several_deduplicates_and_keeps_order():
    assert locations.resolve(["Bergen", "Oslo", "bergen", ""]) == [BERGEN, OSLO]
    assert locations.resolve("Oslo") == [OSLO]


def test_unknown_location_suggests_names():
    with pytest.raises(locations.UnknownLocation) as exc:
        locations.resolve_one("Bergn")
    assert "Bergen" in exc.value.suggestions
    assert "job_locations" in str(exc.value)


def test_wrong_parent_is_unknown():
    with pytest.raises(locations.UnknownLocation):
        locations.resolve_one("Bergen (Oslo)")


def test_every_listed_name_resolves_to_its_own_code():
    for loc in locations.ALL:
        assert locations.resolve_one(loc.name).code == loc.code


def test_names_finn_qualifies_itself():
    innlandet = locations.resolve_one("Våler (Innlandet)")
    ostfold = locations.resolve_one("Våler (Østfold)")
    assert innlandet.code != ostfold.code
    assert locations.resolve_one("Innlandet/Våler").code == innlandet.code
    with pytest.raises(locations.UnknownLocation) as exc:
        locations.resolve_one("Våler")
    assert sorted(exc.value.suggestions) == ["Våler (Innlandet)", "Våler (Østfold)"]
    assert "ambiguous" in str(exc.value)


def test_location_table_shape():
    counties = [loc for loc in locations.ALL if loc.level == "county"]
    assert len(counties) == 16
    assert {"Oslo", "Vestland", "Trøndelag", "Finnmark"} <= {c.name for c in counties}
    assert len(locations.ALL) > 350
    assert len({loc.code for loc in locations.ALL}) == len(locations.ALL)


# ---------- lookup tool ----------

async def test_job_locations_without_query_lists_counties():
    rows = await server.job_locations()
    names = [r["name"] for r in rows]
    assert "Oslo" in names and "Vestland" in names and "Bergen" not in names
    assert all(r["level"] in ("country", "county") for r in rows)


async def test_job_locations_query_lists_matches_and_children():
    rows = await server.job_locations("oslo")
    assert rows[0] == {"name": "Oslo", "code": OSLO, "parent": "Norge", "level": "county"}
    assert any(r["name"] == "Bjerke" and r["parent"] == "Oslo" for r in rows)

    berg = await server.job_locations("berg")
    assert any(r["name"] == "Bergen" for r in berg)


# ---------- search ----------

class FakeBackend:
    def __init__(self):
        self.calls = []

    async def search(self, vertical, query, page=1, filters=None, job_type="all"):
        self.calls.append({"vertical": vertical, "filters": filters, "job_type": job_type})
        return []


@pytest.fixture
def fake(tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(server, "_backend", lambda: backend)
    monkeypatch.setattr(server, "_cache", Cache(db_path=tmp_path / "c.sqlite"))
    return backend


async def test_search_finn_location_name_becomes_code(fake):
    await server.search_finn("jobs", "sykepleier", location="Bergen", job_type="parttime")
    assert fake.calls == [
        {"vertical": "jobs", "filters": {"location": BERGEN}, "job_type": "parttime"}
    ]


async def test_search_finn_several_locations_and_other_filters_kept(fake):
    await server.search_finn(
        "jobs", "", filters={"occupation": "0.23"}, location=["Oslo", "Bergen"]
    )
    assert fake.calls[0]["filters"] == {"occupation": "0.23", "location": [OSLO, BERGEN]}


async def test_search_finn_without_location_leaves_filters_alone(fake):
    await server.search_finn("jobs", "", filters={"location": OSLO})
    await server.search_finn("jobs", "")
    assert [c["filters"] for c in fake.calls] == [{"location": OSLO}, None]


async def test_search_finn_unknown_location_is_reported_without_searching(fake):
    result = await server.search_finn("jobs", "", location="Bergn")
    assert result["error"] == "invalid_argument"
    assert "Bergen" in result["suggestions"]
    assert fake.calls == []


async def test_search_finn_location_rejected_for_other_verticals(fake):
    result = await server.search_finn("bap", "sofa", location="Oslo")
    assert result["error"] == "invalid_argument"
    assert fake.calls == []


async def test_saved_search_stores_location_codes_and_reuses_them(fake):
    saved = await server.save_search(
        "deltid-bergen", "jobs", "", job_type="parttime", location=["Bergen", "Oslo"]
    )
    assert saved.filters == {"location": [BERGEN, OSLO]}
    assert (await server.list_saved_searches())[0].filters == {"location": [BERGEN, OSLO]}

    await server.check_saved_search("deltid-bergen")
    assert fake.calls[-1] == {
        "vertical": "jobs",
        "filters": {"location": [BERGEN, OSLO]},
        "job_type": "parttime",
    }

    bad = await server.save_search("x", "jobs", "", location="Nowhereville")
    assert bad["error"] == "invalid_argument"


async def test_tool_schema_exposes_location():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert "job_locations" in tools
    for name in ("search_finn", "save_search"):
        assert "location" in tools[name].inputSchema["properties"]


# ---------- the request that reaches finn.no ----------

async def test_several_locations_are_sent_as_repeated_parameters(tmp_path, monkeypatch):
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, text=read_fixture("jobs_parttime_search.html"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_get_client():
        return client

    monkeypatch.setattr(http_client, "get_client", fake_get_client)
    monkeypatch.setattr(http_client.config, "REQUEST_DELAY_MIN", 0)
    monkeypatch.setattr(http_client.config, "REQUEST_DELAY_MAX", 0)
    backend = ScraperBackend(cache=Cache(db_path=tmp_path / "c.sqlite"))
    monkeypatch.setattr(server, "_backend", lambda: backend)

    results = await server.search_finn(
        "jobs", "lærer", location=["Oslo", "Bergen"], job_type="parttime"
    )
    await client.aclose()

    assert len(results) == 50
    assert seen[0].path == "/job/search"
    assert seen[0].params.get_list("location") == [OSLO, BERGEN]
    assert seen[0].params["extent"] == "3942"
    assert seen[0].params["q"] == "lærer"
