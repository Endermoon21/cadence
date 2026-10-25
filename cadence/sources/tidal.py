"""Tidal catalog source. The only module in cadence allowed to import tidalapi."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import tidalapi
import tidalapi.artist

from cadence.gate.bucket import Gate
from cadence.identity.normalize import norm_artist
from cadence.ledger.db import log_event
from cadence.sources import base
from cadence.sources.base import ArtistRef, ReleaseDTO, TrackDTO

RELEASE_PAGE = 50
TRACK_PAGE = 100
MAX_PAGES = 40  # hard cap per listing
# Unfiltered bucket first: when a release shows up in several buckets the first one wins.
RELEASE_FILTERS = (None, "EPSANDSINGLES", "COMPILATIONS")


def load_session(token_path: str | Path) -> tidalapi.Session:
    """Log in from a tidal-dl-ng token file. Read-only: refreshed tokens are never written back."""
    raw = json.loads(Path(token_path).expanduser().read_text())

    def get(key: str):
        if key not in raw:
            raise RuntimeError(f"Tidal token file {token_path} has no '{key}'")
        v = raw[key]
        return v["data"] if isinstance(v, dict) and "data" in v else v

    session = tidalapi.Session()
    ok = session.load_oauth_session(get("token_type"), get("access_token"), get("refresh_token"))
    if not ok:
        raise RuntimeError(f"Tidal rejected the saved session in {token_path} "
                           "(expired or revoked); log in again with tidal-dl-ng")
    return session


class CatalogCache:
    """JSON cache of plain DTOs in `catalog_cache`.

    `put` commits the (shared) connection so a later pause keeps what was fetched. That is safe because
    sync is fetch-then-write: while an artist is being fetched nothing of that artist is pending, and
    resolve_artist has already committed its own writes."""

    def __init__(self, conn: sqlite3.Connection, max_age_days: int = 7):
        self.conn = conn
        self.max_age_days = max_age_days

    def get(self, key: str) -> list | None:
        row = self.conn.execute(
            "SELECT body FROM catalog_cache WHERE key = ? AND fetched_at >= datetime('now', ?)",
            (key, f"-{self.max_age_days} days")).fetchone()
        return base.from_json(row[0]) if row else None

    def put(self, key: str, obj: list) -> None:
        self.conn.execute(
            "INSERT INTO catalog_cache(key, fetched_at, body) VALUES (?, datetime('now'), ?) "
            "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, body = excluded.body",
            (key, base.to_json(obj)))
        self.conn.commit()


def _refs(artists) -> tuple[ArtistRef, ...]:
    """ArtistRefs from tidalapi Artist objects (search results)."""
    return tuple(ArtistRef(int(a.id), a.name) for a in (artists or []))


def _json_refs(artists) -> tuple[ArtistRef, ...]:
    return tuple(ArtistRef(int(a["id"]), a["name"]) for a in (artists or []))


def _opt_bool(v) -> bool | None:
    return None if v is None else bool(v)


def _date(v) -> str | None:
    if not v:
        return None
    return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)[:10]


def _upc(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s if s and s != "-1" else None


def _num_tracks(v) -> int | None:
    return int(v) if v is not None and int(v) >= 0 else None


# tidalapi's Track.parse_track only keeps isrc/version when the track is streamable, which would merge
# unavailable variants into the main recording. So DTOs are built from the raw JSON, not parsed objects.
def release_dto(j: dict) -> ReleaseDTO:
    return ReleaseDTO(
        tidal_id=int(j["id"]), title=j["title"], version=j.get("version") or None,
        type=base.release_type(j.get("type")), release_date=_date(j.get("releaseDate")),
        explicit=_opt_bool(j.get("explicit")), num_tracks=_num_tracks(j.get("numberOfTracks")),
        upc=_upc(j.get("upc")), available=_opt_bool(j.get("streamReady")),
        artists=_json_refs(j.get("artists")))


def track_dto(j: dict) -> TrackDTO:
    available = j.get("allowStreaming", j.get("streamReady"))
    return TrackDTO(
        tidal_id=int(j["id"]), isrc=(j.get("isrc") or "").strip().upper() or None, title=j["title"],
        version=j.get("version") or None,
        duration_s=int(j["duration"]) if j.get("duration") is not None else None,
        disc=int(j.get("volumeNumber") or 1), number=int(j.get("trackNumber") or 1),
        explicit=_opt_bool(j.get("explicit")), available=_opt_bool(available),
        artists=_json_refs(j.get("artists")))


def _unwrap(item: dict) -> dict:
    """Some listings wrap entries as {"item": {...}, "type": ...}."""
    return item["item"] if "item" in item and "id" not in item else item


class TidalCatalog:
    """CatalogSource over a tidalapi session. Every network call goes through the gate and the call budget."""

    def __init__(self, session, gate: Gate, cache: CatalogCache, max_calls: int | None = None):
        self.session = session
        self.gate = gate
        self.cache = cache
        self.max_calls = max_calls

    def _cached(self, key: str, fetch) -> list:
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        value = fetch()
        self.cache.put(key, value)
        return value

    def _paged(self, path: str, extra: dict, size: int, convert) -> list:
        """All entries of a paged listing as DTOs. Stops at totalNumberOfItems, a short page, or MAX_PAGES."""
        out: list = []
        offset = 0
        for _ in range(MAX_PAGES):
            params = {"limit": size, "offset": offset, **extra}
            body = base.gated_call(self, self.session.request.map_request, path, params)
            items = body.get("items") or []
            out.extend(convert(_unwrap(i)) for i in items)
            offset += size
            total = body.get("totalNumberOfItems")
            if len(items) < size or (total is not None and offset >= total):
                return out
        log_event(self.cache.conn, "catalog.truncated", path,
                  f"Stopped paging {path} after {MAX_PAGES} pages ({len(out)} entries)")
        return out

    def search_artists(self, name: str) -> list[ArtistRef]:
        def fetch() -> list[ArtistRef]:
            res = base.gated_call(self, self.session.search, name, models=[tidalapi.artist.Artist], limit=10)
            return list(_refs(res["artists"]))
        return self._cached(f"tidal:search:{norm_artist(name)}", fetch)

    def artist_releases(self, tidal_artist_id: int) -> list[ReleaseDTO]:
        def fetch() -> list[ReleaseDTO]:
            seen: dict[int, ReleaseDTO] = {}
            for flt in RELEASE_FILTERS:
                extra = {"filter": flt} if flt else {}
                for r in self._paged(f"artists/{tidal_artist_id}/albums", extra, RELEASE_PAGE, release_dto):
                    seen.setdefault(r.tidal_id, r)
            return list(seen.values())
        return self._cached(f"tidal:releases:{tidal_artist_id}", fetch)

    def release_tracks(self, tidal_release_id: int) -> list[TrackDTO]:
        return self._cached(f"tidal:tracks:{tidal_release_id}", lambda: self._paged(
            f"albums/{tidal_release_id}/tracks", {}, TRACK_PAGE, track_dto))
