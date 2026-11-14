"""Resolve place names to finn.no job-search area codes."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .job_locations import JOB_LOCATIONS

_CODE_RE = re.compile(r"^\d+(\.\d+)+$")
_QUALIFIED_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<parent>[^()]+)\)$")
_LEVELS = ("country", "county", "municipality")


@dataclass(frozen=True)
class JobLocation:
    name: str
    code: str
    parent: str | None

    @property
    def level(self) -> str:
        return _LEVELS[min(self.code.count(".") - 1, len(_LEVELS) - 1)]

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "code": self.code,
            "parent": self.parent,
            "level": self.level,
        }


class UnknownLocation(ValueError):
    def __init__(self, text: str, suggestions: list[str], ambiguous: bool = False):
        if ambiguous:
            message = f"job location {text!r} is ambiguous; use one of: {', '.join(suggestions)}."
        else:
            hint = f"; did you mean {', '.join(suggestions)}?" if suggestions else "."
            message = f"unknown job location {text!r}{hint}"
        super().__init__(f"{message} Use job_locations to look up valid names.")
        self.text = text
        self.suggestions = suggestions


def _norm(text: str) -> str:
    text = text.casefold().replace("–", "-")
    return re.sub(r"\s+", " ", text).strip()


ALL: tuple[JobLocation, ...] = tuple(JobLocation(*row) for row in JOB_LOCATIONS)
_BY_CODE = {loc.code: loc for loc in ALL}

# Exact names first; then the parts of combined areas such as
# "Bygdøy - Frogner" or "Sagene - Torshov", so "Frogner" finds its district.
_BY_NAME: dict[str, list[JobLocation]] = {}
_BY_PART: dict[str, list[JobLocation]] = {}
for _loc in ALL:
    _BY_NAME.setdefault(_norm(_loc.name), []).append(_loc)
    _parts = [p for p in re.split(r"\s+-\s+|/", _loc.name) if p.strip()]
    if len(_parts) > 1:
        for _part in _parts:
            _BY_PART.setdefault(_norm(_part), []).append(_loc)
    # finn.no qualifies duplicate municipality names itself, e.g.
    # "Våler (Innlandet)" and "Våler (Østfold)"; index the bare name too.
    _own = _QUALIFIED_RE.match(_loc.name)
    if _own:
        _BY_PART.setdefault(_norm(_own["name"]), []).append(_loc)


def _pick(
    text: str, candidates: list[JobLocation], parent: str | None
) -> JobLocation | None:
    if parent is not None:
        candidates = [c for c in candidates if c.parent and _norm(c.parent) == parent]
    if not candidates:
        return None
    # A county wins over a same-named municipality (broader area).
    top = min(c.code.count(".") for c in candidates)
    best = [c for c in candidates if c.code.count(".") == top]
    if len(best) > 1:
        raise UnknownLocation(text, [c.name for c in best], ambiguous=True)
    return best[0]


def resolve_one(text: str) -> JobLocation:
    """Resolve one place name (or a raw finn.no area code) to a location.

    Accepts ``"Bergen"``, ``"Vestland"``, ``"Frogner"``, a qualified
    ``"Frogner (Oslo)"`` or ``"Oslo/Frogner"``, or a code like
    ``"1.20001.20061"``. Raises ``UnknownLocation`` with suggestions.
    """
    raw = (text or "").strip()
    if _CODE_RE.match(raw):
        return _BY_CODE.get(raw) or JobLocation(raw, raw, None)

    name, parent = _norm(raw), None
    qualified = _QUALIFIED_RE.match(raw)
    if name in _BY_NAME:
        pass  # finn.no's own name, possibly already qualified
    elif qualified:
        name, parent = _norm(qualified["name"]), _norm(qualified["parent"])
    elif "/" in raw and name not in _BY_NAME:
        head, _, tail = raw.rpartition("/")
        name, parent = _norm(tail), _norm(head)

    for index in (_BY_NAME, _BY_PART):
        found = _pick(raw, index.get(name, []), parent)
        if found is not None:
            return found

    names = {loc.name for loc in ALL}
    close = difflib.get_close_matches(raw, names, n=5, cutoff=0.6)
    lowered = {n.casefold(): n for n in names}
    close += [
        lowered[c]
        for c in difflib.get_close_matches(name, lowered, n=5, cutoff=0.6)
        if lowered[c] not in close
    ]
    contains = sorted(n for n in names if name and name in _norm(n) and n not in close)
    raise UnknownLocation(raw, (close + contains)[:8])


def resolve(location: str | list[str]) -> list[str]:
    """Resolve one or several place names to a de-duplicated list of codes."""
    items = [location] if isinstance(location, str) else list(location)
    codes: list[str] = []
    for item in items:
        if not item or not str(item).strip():
            continue
        code = resolve_one(str(item)).code
        if code not in codes:
            codes.append(code)
    return codes


def search(query: str | None = None) -> list[JobLocation]:
    """Look up locations.

    Without a query: the countries and counties. With a query: every
    location whose name contains it, plus the areas inside an exact match
    (e.g. ``"Oslo"`` lists the Oslo districts).
    """
    if not query or not query.strip():
        return [loc for loc in ALL if loc.code.count(".") <= 2]
    q = _norm(query)
    matches = [loc for loc in ALL if q in _norm(loc.name)]
    exact = {loc.name for loc in matches if _norm(loc.name) == q}
    children = [loc for loc in ALL if loc.parent in exact and loc not in matches]
    return matches + children
