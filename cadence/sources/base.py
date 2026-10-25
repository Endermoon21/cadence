"""Provider-neutral DTOs and the catalog source protocol."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Protocol

from cadence.gate.bucket import Gate

RELEASE_TYPES = ("album", "ep", "single", "compilation", "other")


@dataclass(frozen=True)
class ArtistRef:
    id: int
    name: str


@dataclass(frozen=True)
class TrackDTO:
    tidal_id: int
    isrc: str | None
    title: str
    version: str | None
    duration_s: int | None
    disc: int
    number: int
    explicit: bool | None
    available: bool | None
    artists: tuple[ArtistRef, ...]


@dataclass(frozen=True)
class ReleaseDTO:
    tidal_id: int
    title: str
    version: str | None
    type: str
    release_date: str | None
    explicit: bool | None
    num_tracks: int | None
    upc: str | None
    available: bool | None
    artists: tuple[ArtistRef, ...]


class BudgetExceeded(Exception):
    """The per-run Tidal call budget is spent. Raised by a source *before* the call that would exceed it."""


def gated_call(source, fn, *args, **kwargs):
    """`source.gate.call(...)`, but refuse once `source.max_calls` gate calls have been made."""
    if source.max_calls is not None and source.gate.calls >= source.max_calls:
        raise BudgetExceeded(f"call budget of {source.max_calls} reached")
    return source.gate.call(fn, *args, **kwargs)


class CatalogSource(Protocol):
    gate: Gate
    max_calls: int | None  # set by sync_all for the run; sources enforce it via gated_call

    def search_artists(self, name: str) -> list[ArtistRef]: ...
    def artist_releases(self, tidal_artist_id: int) -> list[ReleaseDTO]: ...
    def release_tracks(self, tidal_release_id: int) -> list[TrackDTO]: ...


def release_type(raw: str | None) -> str:
    t = (raw or "").lower()
    return t if t in RELEASE_TYPES else "other"


_KINDS = {c.__name__: c for c in (ArtistRef, TrackDTO, ReleaseDTO)}


def to_json(items: list) -> str:
    """Serialise a list of DTOs (all one kind) to a self-describing JSON string."""
    kind = type(items[0]).__name__ if items else "ArtistRef"
    return json.dumps({"dto": kind, "items": [asdict(i) for i in items]})


def _build(cls, d: dict):
    if "artists" in d:
        d = {**d, "artists": tuple(ArtistRef(**a) for a in d["artists"])}
    return cls(**d)


def from_json(text: str) -> list:
    body = json.loads(text)
    cls = _KINDS[body["dto"]]
    return [_build(cls, d) for d in body["items"]]
