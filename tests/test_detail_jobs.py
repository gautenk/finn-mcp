"""Job ad pages (get_listing): parse_detail for the jobs vertical.

Three saved pages:
  * jobs_ad_no_jsonld.html  480588571, Nov 2026. Imported from NAV
    (adType AGGREGATED_JOB): no JSON-LD at all, only embedded data + DOM.
  * jobs_parttime_ad.html   480669377, Nov 2026. Ordinary ad, deadline
    "Snarest" (no date), part time.
  * jobs_ad.html            459777164, April 2026 markup. Full time.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from finn_mcp.models import Listing
from finn_mcp.scraper import get_scraper
from finn_mcp.scraper.jobs import (
    _decode_turbo_stream,
    _html_to_text,
    extract_embedded_job_ad,
)
from .conftest import read_fixture

NO_JSONLD = ("jobs_ad_no_jsonld.html", "480588571")
PARTTIME = ("jobs_parttime_ad.html", "480669377")
APRIL = ("jobs_ad.html", "459777164")
ALL_PAGES = [NO_JSONLD, PARTTIME, APRIL]

_TAG_RE = re.compile(r"<[a-zA-Z/!][^>]*>")
_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]+|#\d+);")
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.DOTALL)
_EMBEDDED_RE = re.compile(
    r"<script>window\.__reactRouterContext\.streamController.*?</script>", re.DOTALL
)


def parse(page: tuple[str, str], html: str | None = None) -> Listing:
    fixture, finnkode = page
    if html is None:
        html = read_fixture(fixture)
    return get_scraper("jobs").parse_detail(finnkode, html)


def without_embedded(page: tuple[str, str]) -> str:
    html = read_fixture(page[0])
    stripped = _EMBEDDED_RE.sub("", html)
    assert stripped != html
    return stripped


def without_scripts(page: tuple[str, str]) -> str:
    return _SCRIPT_RE.sub("", read_fixture(page[0]))


def assert_plain_text(text: str | None, min_length: int) -> None:
    assert text is not None
    assert len(text) >= min_length
    assert not _TAG_RE.search(text), _TAG_RE.search(text)
    assert not _ENTITY_RE.search(text), _ENTITY_RE.search(text)
    assert "\xa0" not in text
    assert "\n\n\n" not in text


# ---------- the ad that used to come back with a title only ----------


def test_ad_without_jsonld_is_complete():
    listing = parse(NO_JSONLD)

    assert listing.finnkode == "480588571"
    assert listing.url == "https://www.finn.no/job/ad/480588571"
    assert listing.title == (
        "Prosjektleder i Utekontakten – kompetanseheving om spiseforstyrrelser"
    )
    assert listing.price is None
    assert listing.location == "Langbølgen 2A, 1154 Oslo"
    assert listing.published_at == datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert listing.seller == {
        "name": "FHBU - Team ung, Oslo kommune, Bydel Nordstrand",
        "url": "http://www.oslo.kommune.no",
        "organisation_number": "979589301",
    }

    desc = listing.description
    assert_plain_text(desc, 3900)
    assert desc.startswith("Utekontakten i Bydel Nordstrand er en del av Team Ung")
    assert desc.endswith("Søknadsfrist: 19.11.2026")
    # headings, paragraphs and list items keep their line breaks
    assert "\n\nArbeidsoppgaver\n\n" in desc
    assert "\n- Bedriftsmedlemskap på SATS.\n- Godt arbeidsmiljø.\n" in desc
    assert "Stillingsprosent: 50%" in desc

    a = listing.attributes
    assert a["job_type"] == "parttime"
    assert a["extent"] == "Deltid"
    assert a["employment_type"] == ["PART_TIME"]
    assert a["employment_form"] == "Engasjement"
    assert a["deadline"] == "2026-11-19"
    assert "deadline_text" not in a
    assert a["sector"] == "Offentlig"
    assert a["positions"] == 1
    assert a["occupations"] == ["Barnevernspedagog", "Sosionom", "Veileder"]
    assert a["application_url"].startswith(
        "https://candidate.webcruiter.com/cv?advertid=5202591301"
    )
    assert a["contacts"] == [
        {"name": "Lise-Lotte Ramdahl", "title": "Teamleder", "mobile": "+4799285126"}
    ]
    assert a["street_address"] == "Langbølgen 2A"
    assert a["postal_code"] == "1154"
    assert a["city"] == "Oslo"
    assert a["district"] == "Lambertseter"
    assert a["latitude"] == pytest.approx(59.8749, abs=1e-3)
    assert a["longitude"] == pytest.approx(10.8187, abs=1e-3)
    assert a["updated_at"] == "2026-11-09T07:34:37.587931+01:00"
    assert a["status"] == "PUBLISHED"
    # why this page has no JSON-LD: it is imported from another job board
    assert a["ad_type"] == "AGGREGATED_JOB"
    assert a["source"] == "NAV / Arbeidsplassen.no"
    assert a["source_url"].startswith("https://arbeidsplassen.nav.no/stillinger/")
    assert a["summary"].startswith("Lede og videreutvikle prosjektet")
    assert len(a["summary_qualifications"]) == 5
    assert "Bedriftsmedlemskap på SATS" in a["summary_offers"]
    assert_plain_text(a["employer_description"], 1000)
    # same title and heading: no separate heading attribute
    assert "heading" not in a


def test_parttime_ad_is_complete():
    listing = parse(PARTTIME)

    assert listing.title == "Vil du bli en del av teamet mitt?"
    assert listing.price is None
    assert listing.location == "0580 Oslo"  # the ad has no street address
    assert listing.published_at == datetime(2026, 11, 10, tzinfo=timezone.utc)
    assert listing.seller["name"] == "ULOBA - INDEPENDENT LIVING NORGE SA"
    assert listing.seller["url"] == "https://www.uloba.no/"
    assert listing.seller["organisation_number"] == "963890095"
    assert listing.seller["logo_url"].startswith("https://images.finncdn.no/")

    desc = listing.description
    assert_plain_text(desc, 2400)
    assert desc.startswith("Du må like å arbeide med mennesker")
    assert desc.endswith("Arbeidstid\n\n- Helg\n- Dag\n- Kveld")
    assert "\n\nArbeidsoppgaver\n\n- Assistere med aktiviteter og daglige gjøremål\n" in desc

    a = listing.attributes
    assert a["heading"] == "Brukerstyrt Personlig Assistent (BPA)"
    assert a["employment_type"] == ["PART_TIME"]
    assert a["job_type"] == "parttime"
    assert a["extent"] == "Deltid"
    assert a["employment_form"] == "Fast"
    assert a["deadline_text"] == "Snarest"
    assert "deadline" not in a  # no date on the page: nothing is made up
    assert a["sector"] == "Samvirke"
    assert a["positions"] == 1
    assert a["occupations"] == ["Brukerstyrt personlig assistent (BPA)"]
    assert a["industries"] == ["Helse og omsorg", "Ideelle organisasjoner"]
    assert a["working_languages"] == ["Norsk"]
    assert a["keywords"][:2] == ["bpa", "personlig assistent"]
    assert a["application_url"] == "https://www.finn.no/job-apply/480669377/apply"
    assert a["contacts"] == [
        {
            "name": "Jorid Irene Haldorsen",
            "title": "Abeidsleder",
            "mobile": "+4791174915",
        }
    ]
    assert a["postal_code"] == "0580"
    assert a["city"] == "Oslo"
    assert a["district"] == "Bjerke"
    assert "street_address" not in a
    assert a["ad_type"] == "JOB"
    assert a["updated_at"] == "2026-11-10T10:36:03.820465+01:00"
    assert a["summary_offers"][0] == "Opplæring vil bli gitt"


def test_april_ad_is_complete():
    listing = parse(APRIL)

    assert listing.title == "Dynamics 365 CRM-utvikler"
    assert listing.price is None
    assert listing.location == "Kongens gate 6, 0152 Oslo"
    assert listing.published_at == datetime(2026, 4, 13, tzinfo=timezone.utc)
    assert listing.seller["name"] == "Kreftforeningen"
    assert listing.seller["url"] == "https://kreftforeningen.no"
    assert len(listing.images) == 3
    assert all(i.startswith("https://images.finncdn.no/") for i in listing.images)

    desc = listing.description
    assert_plain_text(desc, 3900)
    assert desc.startswith("I Kreftforeningen spiller teknologi en avgjørende rolle")
    assert desc.endswith("Semac gjennomfører også en samtale med den det gjelder.")
    assert "\n\nHva går jobben ut på?\n\n- Delta i design av løsninger" in desc

    a = listing.attributes
    assert a["heading"] == (
        "Vil du bidra til å utvikle og styrke Kreftforeningens CRM-løsninger?"
    )
    assert a["employment_type"] == ["FULL_TIME"]
    assert a["job_type"] == "fulltime"
    assert a["extent"] == "Heltid"
    assert a["employment_form"] == "Fast"
    assert a["deadline"] == "2026-04-30"
    assert a["sector"] == "Organisasjoner"
    assert a["positions"] == 1
    assert a["remote_options"] == ["Delvis hjemmekontor"]
    assert a["occupations"] == ["Backend-utvikler", "Løsningsarkitekt", "CRM-spesialist"]
    assert a["industries"] == [
        "IT - programvare",
        "Ideelle organisasjoner",
        "Konsulent og rådgivning",
    ]
    assert a["working_languages"] == ["Norsk"]
    assert a["keywords"][0] == "Dynamics 365"
    assert "Microsoft Dynamics 365" in a["essential_skills"]
    assert "C# (Programmeringsspråk)" in a["skills"]
    assert "C# (Programmeringsspråk)" not in a["essential_skills"]
    assert a["application_url"].startswith("https://candidate.hr-manager.net/")
    assert a["contacts"] == [{"name": "Muhammad Imran Mughal", "title": "Seksjonsleder"}]
    assert a["district"] == "Sentrum"
    assert a["updated_at"] == "2026-04-14T13:54:47.465594+02:00"


# ---------- every source is optional ----------


@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: p[0])
def test_embedded_data_is_found_on_every_page(page):
    job_ad = extract_embedded_job_ad(read_fixture(page[0]))
    assert job_ad is not None
    assert job_ad["meta"]["adId"] == page[1]
    assert job_ad["ad"]["description"].startswith("<p>")


@pytest.mark.parametrize("page", [PARTTIME, APRIL], ids=lambda p: p[0])
def test_jsonld_and_dom_without_embedded_data(page):
    full = parse(page)
    listing = parse(page, without_embedded(page))

    assert listing.title == full.title
    assert listing.description == full.description
    assert listing.location == full.location
    assert listing.published_at == full.published_at
    assert listing.seller["name"] == full.seller["name"]
    assert listing.seller["url"] == full.seller["url"]
    for key in (
        "employment_type",
        "job_type",
        "extent",
        "employment_form",
        "sector",
        "heading",
        "working_languages",
        "application_url",
        "updated_at",
    ):
        assert listing.attributes[key] == full.attributes[key], key
    for key in ("occupations", "industries"):
        assert sorted(listing.attributes[key]) == sorted(full.attributes[key]), key
    for key in ("deadline", "deadline_text"):
        assert listing.attributes.get(key) == full.attributes.get(key), key


@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: p[0])
def test_dom_only_when_all_scripts_are_removed(page):
    full = parse(page)
    html = without_scripts(page)
    assert "application/ld+json" not in html and "streamController" not in html
    listing = parse(page, html)

    assert listing.title == full.title
    assert listing.description == full.description
    assert_plain_text(listing.description, 2400)
    assert listing.location == full.location
    assert listing.seller["name"] == full.seller["name"]
    # The rendered page shows "Sist endret" but not the publish date.
    assert listing.published_at is None
    a = listing.attributes
    assert a["updated_at"] == full.attributes["updated_at"]
    assert a["employment_form"] == full.attributes["employment_form"]
    assert a["sector"] == full.attributes["sector"]
    assert a["application_url"] == full.attributes["application_url"]
    assert sorted(a["occupations"]) == sorted(full.attributes["occupations"])
    for key in ("deadline", "deadline_text", "heading"):
        assert a.get(key) == full.attributes.get(key), key


def test_dom_only_job_type_comes_from_the_summary_line():
    # "Fast stilling ∙ Deltid ∙ Oslo" under the job title
    a = parse(PARTTIME, without_scripts(PARTTIME)).attributes
    assert a["job_type"] == "parttime"
    assert a["extent"] == "Deltid"
    assert a["employment_type"] == ["PART_TIME"]
    assert a["contacts"] == [{"name": "Jorid Irene Haldorsen", "title": "Abeidsleder"}]

    # The NAV-imported ad does not render heltid/deltid: nothing is guessed.
    a = parse(NO_JSONLD, without_scripts(NO_JSONLD)).attributes
    assert "job_type" not in a
    assert "employment_type" not in a


def test_dom_only_reads_organisation_number_and_employer_link():
    seller = parse(NO_JSONLD, without_scripts(NO_JSONLD)).seller
    assert seller["organisation_number"] == "979589301"
    assert seller["url"] == "http://www.oslo.kommune.no"


@pytest.mark.parametrize(
    "html",
    [
        "",
        "   ",
        "<html></html>",
        "<html><head><title>x</title></head><body></body></html>",
        "<!doctype html><p>Annonsen er ikke lenger tilgjengelig</p>",
        # broken / unexpected structured sources
        '<script type="application/ld+json">{not json</script>',
        '<script type="application/ld+json">{"@type":"JobPosting","title":5,'
        '"jobLocation":"Oslo","hiringOrganization":[1],"datePosted":"i går",'
        '"employmentType":7,"description":null}</script>',
        '<script>window.__reactRouterContext.streamController.enqueue("not json");</script>',
        '<script>window.__reactRouterContext.streamController.enqueue("[]");</script>',
        '<script>window.__reactRouterContext.streamController.enqueue('
        '"[{\\"_1\\":2,\\"_99\\":5},\\"jobAd\\",{\\"_3\\":4},\\"ad\\",{\\"_5\\":-7},'
        '\\"description\\"]");</script>',
        '<section aria-label="Jobbdetaljer"><ul><li></li><li><span>:</span></li>'
        "<li><strong>x</strong></li></ul></section>",
    ],
)
def test_never_raises_on_pages_without_usable_data(html):
    listing = get_scraper("jobs").parse_detail("123", html)
    assert isinstance(listing, Listing)
    assert listing.finnkode == "123"
    assert listing.title == "Job listing 123"
    assert listing.description is None
    assert listing.location is None
    assert listing.published_at is None
    assert listing.price is None
    assert listing.images == []


def test_minimal_page_uses_what_is_there():
    listing = get_scraper("jobs").parse_detail(
        "123", "<html><body><h1>Kokk s&oslash;kes</h1></body></html>"
    )
    assert listing.title == "Kokk søkes"
    assert listing.description is None
    assert listing.seller is None
    assert listing.attributes == {}


# ---------- helpers ----------


def test_html_to_text_keeps_structure_and_decodes_entities():
    html = (
        "<p></p><p>F&oslash;rste&nbsp;avsnitt<br>med linjeskift &amp; tegn.</p>"
        "<h3>Krav</h3><ul><li><p>En</p></li><li>To <strong>fet</strong> tekst</li></ul>"
        "<p> </p><p>Siste   avsnitt</p>"
    )
    assert _html_to_text(html) == (
        "Første avsnitt\nmed linjeskift & tegn.\n\n"
        "Krav\n\n- En\n- To fet tekst\n\n"
        "Siste avsnitt"
    )
    assert _html_to_text("Bare tekst") == "Bare tekst"
    assert _html_to_text("") is None
    assert _html_to_text("<p> </p>") is None
    assert _html_to_text(None) is None


def test_decode_turbo_stream():
    flat = [
        {"_1": 2, "_5": -5, "_6": 7},
        "jobAd",
        {"_3": 4},
        "title",
        "Kokk",
        "missing",
        "tags",
        [4, 8, -7],
        12,
    ]
    assert _decode_turbo_stream(flat) == {
        "jobAd": {"title": "Kokk"},
        "missing": None,
        "tags": ["Kokk", 12, None],
    }
    assert _decode_turbo_stream([]) is None
    assert _decode_turbo_stream("x") is None
    # out-of-range references decode to None instead of raising
    assert _decode_turbo_stream([{"_1": 99}, "a"]) == {"a": None}
