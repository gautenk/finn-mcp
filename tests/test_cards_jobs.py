"""Search-result cards for jobs must carry everything the card shows."""
from __future__ import annotations

import pytest

from finn_mcp.scraper import get_scraper
from finn_mcp.scraper import jobs as jobs_module
from finn_mcp.scraper.jobs import extract_embedded_job_docs

from .conftest import read_fixture

PARTTIME = "jobs_parttime_search.html"  # November 2026 markup
FULLTIME = "jobs_search.html"  # April 2026 markup


def _cards(fixture: str):
    results = get_scraper("jobs").parse_search_cards(read_fixture(fixture))
    return results, {r.finnkode: r for r in results}


def _filled(results, key: str) -> int:
    return sum(1 for r in results if r.extra.get(key) not in (None, "", []))


@pytest.fixture
def dom_only(monkeypatch):
    """Simulate a page without (decodable) embedded search data."""
    monkeypatch.setattr(jobs_module, "extract_embedded_job_docs", lambda html: {})


# ---------- November 2026 markup (part-time search) ----------


def test_parttime_card_count_and_order_unchanged():
    results, _ = _cards(PARTTIME)
    assert len(results) == 50
    assert results[0].finnkode == "480669377"
    assert results[1].finnkode == "480916262"
    assert results[-1].finnkode == "480671668"
    assert len({r.finnkode for r in results}) == 50


def test_parttime_sponsored_first_card():
    _, by_id = _cards(PARTTIME)
    r = by_id["480669377"]
    assert r.title == "Vil du bli en del av teamet mitt?"
    assert r.price is None
    assert r.location == "Oslo"
    assert r.thumbnail_url and r.thumbnail_url.startswith("https://images.finncdn.no/")
    assert r.extra["employer"] == "ULOBA - INDEPENDENT LIVING NORGE SA"
    assert r.extra["heading"] == "Brukerstyrt Personlig Assistent (BPA)"
    assert r.extra["published"] == "2026-11-10T09:37:59.000Z"
    assert r.extra["published_text"] == "4 dager siden"
    assert r.extra["sponsored"] is True
    assert r.extra["labels"] == ["Betalt plassering"]
    assert r.extra["positions"] == 1
    assert "deadline" not in r.extra  # the ad has no deadline
    assert "salary_text" not in r.extra


def test_parttime_card_with_hourly_salary_range():
    _, by_id = _cards(PARTTIME)
    r = by_id["480897902"]
    assert r.title == "Erfarne vektere søkes til eventer, arrangementer og festivaler"
    assert r.price is None
    assert r.location == "Oslo - Sentrum"
    assert r.extra["employer"] == "PSS Securitas"
    assert r.extra["published_text"] == "Ny i dag"
    assert r.extra["salary_text"] == "Timelønn: 244 - 306 kr"
    assert r.extra["salary_period"] == "hour"
    assert r.extra["salary_min"] == 244
    assert r.extra["salary_max"] == 306
    assert r.extra["positions"] == 99
    assert "sponsored" not in r.extra
    assert "labels" not in r.extra


def test_parttime_card_with_yearly_salary_and_note():
    _, by_id = _cards(PARTTIME)
    r = by_id["480794685"]
    assert r.title == "Sportslig leder i friidrettsgruppa"
    assert r.price is None
    assert r.location == "Oslo - Lambertseter"
    assert r.extra["employer"] == "LAMBERTSETER IDRETTSFORENING"
    assert r.extra["salary_text"].startswith("Årslønn: 600 000 kr (")
    assert r.extra["salary_period"] == "year"
    assert r.extra["salary_min"] == 600_000
    assert "salary_max" not in r.extra
    assert r.extra["salary_note"].startswith("Årslønn oppgitt i 100% stilling")
    assert r.extra["labels"] == ["Enkel søknad"]
    assert r.extra["deadline"] == "2026-11-29"
    assert r.extra["positions"] == 1
    assert r.extra["locations"] == ["Oslo", "Lambertseter"]

    r = by_id["480739082"]
    assert r.extra["salary_text"] == "Årslønn: 150 000 - 350 000 kr"
    assert (r.extra["salary_min"], r.extra["salary_max"]) == (150_000, 350_000)
    assert r.extra["positions"] == 15


def test_parttime_card_with_several_workplaces():
    _, by_id = _cards(PARTTIME)
    r = by_id["480837378"]
    assert r.title == "Kommunikasjonsrådgiver"
    # The card renders "Oslo - Sentrum +1 til"; the counter is not a place.
    assert r.location == "Oslo - Sentrum"
    assert r.extra["additional_locations"] == 1
    assert r.extra["employer"] == "NORSUS Norsk institutt for bærekraftsforskning AS"
    assert r.extra["heading"] == "Kommunikasjonsrådgiver innen bærekraftsforskning, deltid"
    assert r.extra["deadline"] == "2026-12-06"
    assert "Fredrikstad" in r.extra["locations"]


def test_parttime_last_card():
    results, _ = _cards(PARTTIME)
    r = results[-1]
    assert r.finnkode == "480671668"
    assert r.title == "Langvaktstillinger i Helse og omsorg"
    assert r.location == "Oslo - Ullern"
    assert r.extra["employer"] == "Humana omsorg og assistanse"
    assert r.extra["heading"] == "Har du erfaring fra helse og omsorg?"
    assert r.extra["published"] == "2026-11-10T09:52:31.000Z"
    assert r.extra["deadline"] == "2026-11-26"
    assert r.extra["positions"] == 3


def test_parttime_fill_rates():
    results, _ = _cards(PARTTIME)
    n = len(results)
    assert all(r.title for r in results)
    assert all(r.location for r in results)
    assert sum(1 for r in results if r.thumbnail_url) >= 48
    for key in ("employer", "published", "published_text", "positions", "locations"):
        assert _filled(results, key) == n, key
    # heading is only rendered when it differs from the job title
    assert _filled(results, "heading") >= 45
    # not every ad has a deadline
    assert _filled(results, "deadline") >= 33
    assert _filled(results, "salary_text") == 3
    assert _filled(results, "labels") == 13
    assert sum(1 for r in results if r.extra.get("sponsored")) == 1


# ---------- April 2026 markup (full-time search) ----------


def test_fulltime_sponsored_first_card():
    results, _ = _cards(FULLTIME)
    assert len(results) == 51
    r = results[0]
    assert r.finnkode == "459777164"
    assert r.title == "Dynamics 365 CRM-utvikler"
    assert r.price is None
    assert r.location == "Oslo"
    assert r.extra["employer"] == "Kreftforeningen"
    assert r.extra["heading"] == (
        "Vil du bidra til å utvikle og styrke Kreftforeningens CRM-løsninger?"
    )
    assert r.extra["published"] == "2026-04-13T13:58:13.000Z"
    assert r.extra["published_text"] == "4 dager siden"
    assert r.extra["sponsored"] is True
    # Same deadline as the detail page (tests/fixtures/jobs_ad.html).
    assert r.extra["deadline"] == "2026-04-30"
    assert r.extra["positions"] == 1


def test_fulltime_card_without_heading():
    _, by_id = _cards(FULLTIME)
    r = by_id["459756914"]
    assert r.title == "Utvikler"
    assert r.location == "Skien"
    assert r.extra["employer"] == "Pasientreiser HF"
    assert "heading" not in r.extra  # identical to the title, so not shown
    assert r.extra["published"] == "2026-04-13T11:25:43.000Z"
    assert r.extra["deadline"] == "2026-04-26"
    assert r.extra["locations"] == ["Telemark", "Skien"]
    assert "sponsored" not in r.extra
    assert "labels" not in r.extra


def test_fulltime_card_with_deadline_badge():
    _, by_id = _cards(FULLTIME)
    r = by_id["454588477"]
    assert r.title == "Senior utvikler/arkitekt"
    assert r.location == "Ålesund"
    assert r.extra["employer"] == "FISHED AS"
    assert r.extra["labels"] == ["Siste frist"]
    assert r.extra["published_text"] == "en måned siden"
    assert r.extra["deadline"] == "2026-04-17"


def test_fulltime_middle_and_last_cards():
    results, by_id = _cards(FULLTIME)
    r = by_id["459310332"]
    assert r.title == "Embedded Developer"
    assert r.location == "Trondheim"
    assert r.extra["employer"] == "Siemens Energy AS"
    assert r.extra["heading"] == (
        "Er du en utvikler med kunnskap innen elektronikk og koding?"
    )
    assert r.extra["deadline"] == "2026-05-21"

    last = results[-1]
    assert last.finnkode == "455247512"
    assert last.title == "Produkteier Videoannonser"
    assert last.location == "Oslo"
    assert last.extra["employer"] == "Schibsted"
    assert "deadline" not in last.extra
    assert last.extra["positions"] == 1


def test_fulltime_fill_rates():
    results, _ = _cards(FULLTIME)
    n = len(results)
    assert all(r.location for r in results)
    assert all(r.thumbnail_url for r in results)
    for key in ("employer", "published", "published_text", "positions", "locations"):
        assert _filled(results, key) == n, key
    assert _filled(results, "heading") >= 41
    assert _filled(results, "deadline") >= 27
    assert _filled(results, "labels") == 11


# ---------- cross-cutting ----------


@pytest.mark.parametrize("fixture", [PARTTIME, FULLTIME])
def test_jobs_never_have_a_price(fixture):
    results, _ = _cards(fixture)
    assert [r.finnkode for r in results if r.price is not None] == []


@pytest.mark.parametrize("fixture", [PARTTIME, FULLTIME])
def test_extra_values_are_typed(fixture):
    results, _ = _cards(fixture)
    for r in results:
        for key in ("positions", "salary_min", "salary_max", "additional_locations"):
            if key in r.extra:
                assert type(r.extra[key]) is int, (r.finnkode, key)
        for key in ("employer", "heading", "published", "published_text", "deadline"):
            if key in r.extra:
                assert isinstance(r.extra[key], str) and r.extra[key], (r.finnkode, key)
        if "deadline" in r.extra:
            assert len(r.extra["deadline"]) == 10
            # a deadline is never before the ad was published
            assert r.extra["deadline"] >= r.extra["published"][:10]
        assert r.extra["published"].endswith("Z")


@pytest.mark.parametrize("fixture,expected", [(PARTTIME, 50), (FULLTIME, 51)])
def test_embedded_data_agrees_with_dom(fixture, expected):
    html = read_fixture(fixture)
    docs = extract_embedded_job_docs(html)
    results = get_scraper("jobs").parse_search_cards(html)
    assert len(results) == expected
    assert all(r.finnkode in docs for r in results)
    for r in results:
        doc = docs[r.finnkode]
        assert r.extra["employer"] == " ".join(doc["company_name"].split())
        assert r.title == " ".join(doc["job_title"].split())


@pytest.mark.parametrize("fixture", [PARTTIME, FULLTIME])
def test_dom_fallback_without_embedded_data(fixture, dom_only):
    results, _ = _cards(fixture)
    n = len(results)
    assert all(r.location for r in results)
    assert all(r.price is None for r in results)
    for key in ("employer", "published", "published_text"):
        assert _filled(results, key) == n, key
    assert _filled(results, "heading") >= 41
    # these only exist in the embedded data, so they are absent, not guessed
    assert _filled(results, "deadline") == 0
    assert _filled(results, "positions") == 0


def test_dom_fallback_still_parses_salary(dom_only):
    _, by_id = _cards(PARTTIME)
    r = by_id["480897902"]
    assert r.extra["salary_text"] == "Timelønn: 244 - 306 kr"
    assert (r.extra["salary_min"], r.extra["salary_max"]) == (244, 306)
    assert r.extra["salary_period"] == "hour"


def test_embedded_docs_tolerates_garbage():
    assert extract_embedded_job_docs("") == {}
    junk = '<script type="application/json">' + "!" * 400 + "</script>"
    assert extract_embedded_job_docs(junk) == {}
    results = get_scraper("jobs").parse_search_cards("<html><body></body></html>")
    assert results == []
