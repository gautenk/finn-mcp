from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config, http_client, locations
from .backend.base import ListingNotFound
from .cache import Cache
from .models import Filters, JobType, Listing, SavedSearch, SearchResult, Vertical
from .scraper.base import now_utc

log = logging.getLogger("finn_mcp")

mcp = FastMCP("finn-mcp")


def _backend():
    return config.get_backend()


_cache = Cache()


def _with_location(
    vertical: Vertical, filters: Filters | None, location: str | list[str] | None
) -> Filters | None:
    """Merge a named job ``location`` into ``filters`` as finn.no area codes."""
    if not location:
        return filters
    if vertical != "jobs":
        raise ValueError(
            "location names are only supported for vertical 'jobs'; "
            "pass finn.no's own location code in filters for other verticals"
        )
    codes = locations.resolve(location)
    if not codes:
        return filters
    return {**(filters or {}), "location": codes[0] if len(codes) == 1 else codes}


def _invalid_argument(exc: ValueError) -> dict[str, Any]:
    error: dict[str, Any] = {"error": "invalid_argument", "message": str(exc)}
    if isinstance(exc, locations.UnknownLocation):
        error["suggestions"] = exc.suggestions
    return error


@mcp.tool()
async def job_locations(query: str | None = None) -> list[dict[str, Any]]:
    """Look up area names usable as ``location`` in job searches.

    Without ``query``: Norway's counties. With ``query``: every area whose
    name contains it, plus the areas inside an exact match (``"Oslo"`` lists
    the Oslo districts, ``"Vestland"`` its municipalities).
    """
    return [loc.as_dict() for loc in locations.search(query)]


@mcp.tool()
async def search_finn(
    vertical: Vertical,
    query: str,
    page: int = 1,
    filters: Filters | None = None,
    job_type: JobType = "all",
    location: str | list[str] | None = None,
) -> list[SearchResult] | dict[str, Any]:
    """Search finn.no within a vertical.

    ``vertical`` is one of: ``bap`` (Torget / used goods), ``homes`` (real
    estate for sale), ``lettings`` (real estate for rent), ``cars_used``,
    ``cars_new`` (not yet supported by the scraper backend), ``jobs``.

    ``query`` is a free-text keyword. ``filters`` is a dict of extra
    URL-query parameters understood by finn.no (e.g. ``{"price_to":"300000"}``
    for cars, ``{"location":"1.20001.20061"}`` for Oslo).

    ``job_type`` only applies to ``vertical="jobs"``: ``fulltime`` (heltid),
    ``parttime`` (deltid) or ``all`` (default, both).

    ``location`` filters job searches by area, by name: a county
    (``"Vestland"``), a municipality (``"Bergen"``), an Oslo district
    (``"Grünerløkka"``), or a list of several. Use ``job_locations`` to look
    up valid names. Only for ``vertical="jobs"``; other verticals take
    finn.no's own codes through ``filters``.

    A ``filters`` value may be a list to repeat a parameter.

    Returns up to ~50 ``SearchResult`` entries. Fewer if finn.no returned
    fewer matches; an empty list is a valid result.
    """
    try:
        filters = _with_location(vertical, filters, location)
        return await _backend().search(
            vertical, query, page=page, filters=filters, job_type=job_type
        )
    except ValueError as exc:
        return _invalid_argument(exc)
    except NotImplementedError as exc:
        return {"error": "not_supported", "message": str(exc)}
    except http_client.BlockedError as exc:
        return {"error": "blocked", "message": str(exc)}
    except http_client.RateLimitedError as exc:
        return {"error": "rate_limited", "retry_after": exc.retry_after}
    except Exception as exc:
        log.exception("search_finn failed")
        return {"error": "parse_failed", "message": str(exc)}


@mcp.tool()
async def get_listing(
    finnkode: str,
    vertical: Vertical | None = None,
) -> Listing | dict[str, Any]:
    """Fetch a full listing by its ``finnkode``.

    If ``vertical`` is omitted the server tries a cache hint first, then
    probes each vertical until one returns a valid page. Results are cached
    for 24 hours in a local SQLite database.
    """
    try:
        return await _backend().get_listing(finnkode, vertical=vertical)
    except http_client.NotFoundError:
        return {"error": "not_found", "finnkode": finnkode}
    except ListingNotFound:
        return {"error": "not_found", "finnkode": finnkode}
    except http_client.BlockedError as exc:
        return {"error": "blocked", "message": str(exc)}
    except http_client.RateLimitedError as exc:
        return {"error": "rate_limited", "retry_after": exc.retry_after}
    except NotImplementedError as exc:
        return {"error": "not_supported", "message": str(exc)}
    except Exception as exc:
        log.exception("get_listing failed for %s", finnkode)
        return {
            "error": "parse_failed",
            "finnkode": finnkode,
            "vertical": vertical,
            "message": str(exc),
        }


@mcp.tool()
async def save_search(
    name: str,
    vertical: Vertical,
    query: str,
    filters: Filters | None = None,
    job_type: JobType = "all",
    location: str | list[str] | None = None,
) -> SavedSearch | dict[str, Any]:
    """Persist a named search for repeated monitoring via ``check_saved_search``.

    ``job_type`` (``fulltime`` | ``parttime`` | ``all``) and ``location``
    (place names, see ``search_finn``) only apply to ``vertical="jobs"``.
    The location is stored as finn.no area codes in ``filters``.
    """
    try:
        filters = _with_location(vertical, filters, location)
    except ValueError as exc:
        return _invalid_argument(exc)
    if job_type != "all" and vertical != "jobs":
        return {
            "error": "invalid_argument",
            "message": "job_type is only supported for vertical 'jobs'",
        }
    saved = SavedSearch(
        name=name,
        vertical=vertical,
        query=query,
        filters=filters or {},
        job_type=job_type,
        last_checked_at=None,
        last_finnkodes=[],
    )
    await _cache.save_search(saved)
    return saved


@mcp.tool()
async def list_saved_searches() -> list[SavedSearch]:
    """Return every saved search in the local store, ordered by name."""
    return await _cache.list_searches()


@mcp.tool()
async def delete_saved_search(name: str) -> bool:
    """Delete a saved search by name. Returns True if it existed."""
    return await _cache.delete_search(name)


@mcp.tool()
async def check_saved_search(name: str) -> list[SearchResult] | dict[str, Any]:
    """Run a saved search, return only listings that are new since the last check.

    Updates the saved search's ``last_checked_at`` and ``last_finnkodes``.
    On the first call all current results are "new".
    """
    saved = await _cache.get_search(name)
    if saved is None:
        return {"error": "not_found", "name": name}

    try:
        results = await _backend().search(
            saved.vertical,
            saved.query,
            page=1,
            filters=saved.filters,
            job_type=saved.job_type,
        )
    except ValueError as exc:
        return {"error": "invalid_argument", "message": str(exc)}
    except NotImplementedError as exc:
        return {"error": "not_supported", "message": str(exc)}
    except http_client.BlockedError as exc:
        return {"error": "blocked", "message": str(exc)}
    except http_client.RateLimitedError as exc:
        return {"error": "rate_limited", "retry_after": exc.retry_after}
    except Exception as exc:
        log.exception("check_saved_search failed for %s", name)
        return {"error": "parse_failed", "message": str(exc)}

    previous = set(saved.last_finnkodes)
    new = [r for r in results if r.finnkode not in previous]
    union = sorted({r.finnkode for r in results} | previous)
    await _cache.update_search_state(name, now_utc(), union)
    return new


def configure_http() -> None:
    """Apply FINN_MCP_HOST / FINN_MCP_PORT and Host validation to ``mcp``.

    The endpoint is ``/mcp``. Sessions are stateless so the server can be
    restarted or redeployed behind a proxy without clients holding stale
    session ids.
    """
    mcp.settings.host = config.http_host()
    mcp.settings.port = config.http_port()
    mcp.settings.streamable_http_path = "/mcp"
    mcp.settings.stateless_http = True
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.allowed_hosts(),
        allowed_origins=config.allowed_origins(),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    transport = config.transport()
    if transport == "streamable-http":
        configure_http()
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
