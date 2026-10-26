"""Read-only MusicBrainz client (stdlib only), used by `cadence candidates --musicbrainz` as a tiebreaker.

MusicBrainz asks for a contact User-Agent and at most one request per second; both are enforced here.
Responses are cached in `catalog_cache` (`mb:artist:{norm}`, `mb:rg:{mbid}`). The opener, sleep and clock
are injectable so tests never touch the network."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

from cadence.identity.normalize import norm_artist

USER_AGENT = "Cadence/0.1 (personal use; russell.alexander.bennett@gmail.com)"
BASE_URL = "https://musicbrainz.org/ws/2"
MIN_INTERVAL = 1.1
TIMEOUT = 20
PAGE_SIZE = 100
MAX_PAGES = 10


class MusicBrainz:
    def __init__(self, conn: sqlite3.Connection | None = None, *, opener: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 interval: float = MIN_INTERVAL, max_age_days: int = 30):
        self.conn = conn
        self._opener = opener or urllib.request.urlopen
        self._sleep, self._clock = sleep, clock
        self.interval, self.max_age_days = interval, max_age_days
        self._last: float | None = None
        self.requests = 0

    def _get(self, path: str, params: dict) -> dict:
        if self._last is not None:
            wait = self._last + self.interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        url = f"{BASE_URL}/{path}?{urllib.parse.urlencode({**params, 'fmt': 'json'})}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._last = self._clock()
        self.requests += 1
        resp = self._opener(req, timeout=TIMEOUT)
        try:
            return json.loads(resp.read().decode("utf-8"))
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()

    def _cached(self, key: str):
        if self.conn is None:
            return None
        row = self.conn.execute("SELECT body FROM catalog_cache WHERE key = ? AND fetched_at >= datetime('now', ?)",
                                (key, f"-{self.max_age_days} days")).fetchone()
        return json.loads(row[0]) if row else None

    def _store(self, key: str, obj) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT INTO catalog_cache(key, fetched_at, body) VALUES (?, datetime('now'), ?) "
            "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, body = excluded.body",
            (key, json.dumps(obj)))
        self.conn.commit()

    def search_artists(self, name: str) -> list[dict]:
        """[{mbid, name, disambiguation, score}] best score first (MusicBrainz's own order)."""
        key = f"mb:artist:{norm_artist(name)}"
        hit = self._cached(key)
        if hit is not None:
            return hit
        phrase = name.replace("\\", "\\\\").replace('"', '\\"')
        data = self._get("artist/", {"query": f'artist:"{phrase}"', "limit": 25})
        out = [{"mbid": a["id"], "name": a.get("name", ""), "disambiguation": a.get("disambiguation", ""),
                "score": int(a.get("score", 0))} for a in data.get("artists", [])]
        self._store(key, out)
        return out

    def release_group_titles(self, mbid: str) -> set[str]:
        key = f"mb:rg:{mbid}"
        hit = self._cached(key)
        if hit is not None:
            return set(hit)
        titles: set[str] = set()
        for page in range(MAX_PAGES):
            data = self._get("release-group", {"artist": mbid, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE})
            groups = data.get("release-groups", [])
            titles |= {g["title"] for g in groups if g.get("title")}
            if not groups or (page + 1) * PAGE_SIZE >= data.get("release-group-count", 0):
                break
        self._store(key, sorted(titles))
        return titles
