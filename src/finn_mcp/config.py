from __future__ import annotations

import os
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

LISTING_TTL_SECONDS = 24 * 60 * 60

REQUEST_DELAY_MIN = 0.3
REQUEST_DELAY_MAX = 1.2

HTTP_TIMEOUT_SECONDS = 15.0


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / "finn-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_db_path() -> Path:
    override = os.environ.get("FINN_CACHE_DB")
    if override:
        return Path(override)
    return data_dir() / "cache.sqlite"


TRANSPORTS = ("stdio", "streamable-http")

# Host header values accepted by the HTTP transport (DNS-rebinding protection).
# The public name is what nginx forwards; the others are in-cluster names.
DEFAULT_ALLOWED_HOSTS = (
    "finn.gekkomedia.no",
    "nginx",
    "finn-mcp:8000",
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
)

DEFAULT_ALLOWED_ORIGINS = (
    "https://finn.gekkomedia.no",
    "https://claude.ai",
    "https://claude.com",
)


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def transport() -> str:
    value = os.environ.get("FINN_MCP_TRANSPORT", "stdio").strip().lower() or "stdio"
    if value not in TRANSPORTS:
        raise ValueError(
            f"unknown FINN_MCP_TRANSPORT={value!r}; expected one of {TRANSPORTS}"
        )
    return value


def http_host() -> str:
    return os.environ.get("FINN_MCP_HOST", "").strip() or "127.0.0.1"


def http_port() -> int:
    return int(os.environ.get("FINN_MCP_PORT", "").strip() or "8000")


def allowed_hosts() -> list[str]:
    """Default Host allow-list plus comma-separated FINN_MCP_ALLOWED_HOSTS."""
    return [*DEFAULT_ALLOWED_HOSTS, *_env_list("FINN_MCP_ALLOWED_HOSTS")]


def allowed_origins() -> list[str]:
    return [*DEFAULT_ALLOWED_ORIGINS, *_env_list("FINN_MCP_ALLOWED_ORIGINS")]


def backend_name() -> str:
    return os.environ.get("FINN_BACKEND", "scraper").strip().lower()


def get_backend():
    """Return a fresh FinnBackend instance selected by the FINN_BACKEND env var.

    Deferred import so the scraper dependencies aren't loaded when the
    official backend is active (and vice-versa).
    """
    name = backend_name()
    if name == "scraper":
        from .backend.scraper_backend import ScraperBackend

        return ScraperBackend()
    if name == "official":
        from .backend.api_backend import OfficialApiBackend

        return OfficialApiBackend(api_key=os.environ.get("FINN_API_KEY"))
    raise ValueError(
        f"unknown FINN_BACKEND={name!r}; expected 'scraper' or 'official'"
    )
