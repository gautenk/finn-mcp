from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Vertical = Literal[
    "bap",
    "homes",
    "lettings",
    "cars_used",
    "cars_new",
    "jobs",
]

ALL_VERTICALS: tuple[Vertical, ...] = (
    "bap",
    "homes",
    "lettings",
    "cars_used",
    "cars_new",
    "jobs",
)


# Extra finn.no query parameters; a list repeats the parameter.
Filters = dict[str, str | list[str]]

JobType = Literal["fulltime", "parttime", "all"]

ALL_JOB_TYPES: tuple[JobType, ...] = ("fulltime", "parttime", "all")


class SearchResult(BaseModel):
    finnkode: str
    vertical: Vertical
    title: str
    price: int | None = None
    currency: str = "NOK"
    location: str | None = None
    thumbnail_url: str | None = None
    url: str
    extra: dict[str, Any] = Field(default_factory=dict)


class Listing(BaseModel):
    finnkode: str
    vertical: Vertical
    url: str
    title: str
    description: str | None = None
    price: int | None = None
    currency: str = "NOK"
    location: str | None = None
    images: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    seller: dict[str, Any] | None = None
    published_at: datetime | None = None
    fetched_at: datetime


class SavedSearch(BaseModel):
    name: str
    vertical: Vertical
    query: str
    filters: Filters = Field(default_factory=dict)
    job_type: JobType = "all"
    last_checked_at: datetime | None = None
    last_finnkodes: list[str] = Field(default_factory=list)
