"""Shared test doubles. No network, no tidalapi."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cadence.gate.bucket import Gate
from cadence.sources.base import ArtistRef, ReleaseDTO, TrackDTO, gated_call, release_type

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TooManyRequests(Exception):
    """Same class name as tidalapi's; the gate duck-types on the name."""

    def __init__(self, retry_after: int = 120):
        super().__init__("429")
        self.retry_after = retry_after


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.t)


def make_gate(**kw) -> Gate:
    ft = FakeTime()
    return Gate("tidal", 1.0, kw.pop("burst", 100), clock=ft.clock, sleep=ft.sleep, now=ft.now, **kw)


def rel(tid: int, title: str, artists: list[ArtistRef], type: str = "single", date: str | None = "2024-01-01",
        version: str | None = None) -> ReleaseDTO:
    return ReleaseDTO(tid, title, version, type, date, False, 1, None, True, tuple(artists))


def trk(tid: int, title: str, dur: int | None, isrc: str | None = None, number: int = 1, disc: int = 1,
        version: str | None = None, artists: list[ArtistRef] = ()) -> TrackDTO:
    return TrackDTO(tid, isrc, title, version, dur, disc, number, False, True, tuple(artists))


class FakeCatalog:
    """CatalogSource over in-memory dicts; every operation spends one real-Gate call (and the call budget).

    `trip_on_releases` / `trip_on_tracks`: Tidal artist id / release id whose fetch raises TooManyRequests.
    `fail_on_releases` / `fail_on_tracks`: {artist id / release id: exception} raised from that fetch."""

    def __init__(self, gate: Gate, search: dict[str, list[ArtistRef]] | None = None,
                 releases: dict[int, list[ReleaseDTO]] | None = None,
                 tracks: dict[int, list[TrackDTO]] | None = None, trip_on_releases: int | None = None,
                 trip_on_tracks: int | None = None, fail_on_releases: dict[int, Exception] | None = None,
                 fail_on_tracks: dict[int, Exception] | None = None):
        self.gate = gate
        self.max_calls: int | None = None
        self._search = {k.lower(): v for k, v in (search or {}).items()}
        self._releases = releases or {}
        self._tracks = tracks or {}
        self.trip_on_releases = trip_on_releases
        self.trip_on_tracks = trip_on_tracks
        self.fail_on_releases = fail_on_releases or {}
        self.fail_on_tracks = fail_on_tracks or {}
        self.use_cache = True
        self.track_calls: list[int] = []

    def search_artists(self, name: str) -> list[ArtistRef]:
        return gated_call(self, lambda: list(self._search.get(name.lower(), [])))

    def artist_releases(self, tidal_artist_id: int) -> list[ReleaseDTO]:
        def go():
            if tidal_artist_id == self.trip_on_releases:
                raise TooManyRequests(120)
            if tidal_artist_id in self.fail_on_releases:
                raise self.fail_on_releases[tidal_artist_id]
            return list(self._releases.get(tidal_artist_id, []))
        return gated_call(self, go)

    def release_tracks(self, tidal_release_id: int) -> list[TrackDTO]:
        self.track_calls.append(tidal_release_id)

        def go():
            if tidal_release_id == self.trip_on_tracks:
                raise TooManyRequests(120)
            if tidal_release_id in self.fail_on_tracks:
                raise self.fail_on_tracks[tidal_release_id]
            return list(self._tracks.get(tidal_release_id, []))
        return gated_call(self, go)


def fixture_catalog(gate: Gate, path: Path = FIXTURES / "tidal_will_paquin.json") -> FakeCatalog:
    """FakeCatalog from the s1 spike dump. Artist ids are assigned by name; Will Paquin = 1."""
    rows = json.loads(path.read_text())
    ids: dict[str, int] = {"Will Paquin": 1}

    def ref(name: str) -> ArtistRef:
        return ArtistRef(ids.setdefault(name, len(ids) + 1), name)

    releases, tracks = [], {}
    for r in rows:
        releases.append(ReleaseDTO(r["id"], r["title"], r["version"], release_type(r["type"]), r["date"],
                                   r["explicit"], r["n"], r["upc"], r["available"],
                                   tuple(ref(a) for a in r["artists"])))
        tracks[r["id"]] = [TrackDTO(t["id"], t["isrc"], t["title"], t["version"], t["dur"], t["disc"], t["n"],
                                    t["explicit"], None, tuple(ref(a) for a in t["artists"]))
                           for t in r["tracks"]]
    return FakeCatalog(gate, search={"Will Paquin": [ArtistRef(1, "Will Paquin")]},
                       releases={1: releases}, tracks=tracks)


# ---- raw JSON in the shape of real Tidal v1 responses, and a session that serves it ----

def raw_artist(i: int = 7, name: str = "Will") -> dict:
    return {"id": i, "name": name, "type": "MAIN", "picture": None}


def raw_album(i: int, title: str | None = None, **kw) -> dict:
    d = {"id": i, "title": title or f"Album {i}", "duration": 300, "streamReady": True, "allowStreaming": True,
         "numberOfTracks": 3, "numberOfVolumes": 1, "releaseDate": "2020-09-25", "type": "ALBUM",
         "version": None, "upc": "123", "explicit": False, "cover": "abc-def", "popularity": 10,
         "artists": [raw_artist()], "artist": raw_artist()}
    d.update(kw)
    return d


def raw_track(i: int, title: str | None = None, **kw) -> dict:
    d = {"id": i, "title": title or f"Track {i}", "duration": 127, "trackNumber": i, "volumeNumber": 1,
         "isrc": "QM24S2009107", "version": None, "explicit": True, "allowStreaming": True, "streamReady": True,
         "popularity": 5, "copyright": "(c) x", "artists": [raw_artist()], "artist": raw_artist(),
         "album": {"id": 1, "title": "Album 1", "cover": "abc-def"}}
    d.update(kw)
    return d


class FakeSession:
    """Looks like tidalapi.Session for what the source uses.

    `pages` maps (path, filter, offset) -> raw items; map_request returns the raw JSON body that real Tidal
    returns for a paged listing (items + totalNumberOfItems) because no `parse` is passed."""

    def __init__(self, pages=None, search_result=None, raise_exc=None, report_total: bool = True):
        self.pages = pages or {}
        self.search_result = search_result or []
        self.raise_exc = raise_exc
        self.report_total = report_total
        self.requests: list[tuple[str, dict, object]] = []
        self.searches: list[tuple] = []
        self.request = NS(map_request=self._map_request)

    def _total(self, path, flt) -> int:
        return max((off + len(items) for (p, f, off), items in self.pages.items() if (p, f) == (path, flt)),
                   default=0)

    def _map_request(self, path, params, parse=None):
        self.requests.append((path, dict(params), parse))
        if self.raise_exc:
            raise self.raise_exc
        flt = params.get("filter")
        body = {"limit": params["limit"], "offset": params["offset"],
                "items": self.pages.get((path, flt, params["offset"]), [])}
        if self.report_total:
            body["totalNumberOfItems"] = self._total(path, flt)
        return body

    def search(self, name, models=None, limit=50, offset=0):
        self.searches.append((name, models, limit))
        return {"artists": self.search_result}
