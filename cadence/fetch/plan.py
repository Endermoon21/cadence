"""What to fetch: one item per unowned recording cluster, using the canonical recording's Tidal track.

Read-only over the ledger. A cluster is owned when any file is linked to a recording in it (library-wide), the
same rule report.py uses. The canonical recording is `COALESCE(canon_id, id)`, which `cadence regroup` sets
with the latest-remaster rule (identity/grouping.py)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from cadence.ledger import naming

_TYPE_RANK = {"album": 0, "ep": 1, "single": 2, "compilation": 3, "other": 4}
_NO_DATE = "9999-99-99"

_TRACKS = (
    "SELECT t.tidal_id AS tidal_track_id, t.disc, t.number, t.title, t.version, t.duration_s AS t_dur, "
    "rec.id AS rec_id, COALESCE(rec.canon_id, rec.id) AS cid, rec.variant, rec.isrc, rec.duration_s AS r_dur, "
    "rel.id AS release_id, rel.title AS release_title, rel.type, rel.release_date, "
    "(SELECT s.provider_ref FROM release_source s WHERE s.release_id = rel.id AND s.provider = 'tidal') AS tidal_release "
    "FROM track t JOIN release rel ON rel.id = t.release_id JOIN recording rec ON rec.id = t.recording_id "
    "WHERE rel.artist_id = ?1 AND t.tidal_id IS NOT NULL AND t.available IS NOT 0 "
    "AND ((rel.role = 'own' AND rel.hidden_reason IS NULL) OR rel.id = ?2)")


@dataclass(frozen=True)
class FetchItem:
    artist_id: int
    artist: str
    release_id: int
    tidal_release_id: int | None
    release: str
    disc: int
    number: int
    title: str                 # display title: Tidal title plus its version, as tidal-dl-ng names it
    tidal_track_id: int
    recording_id: int
    duration_s: int | None
    isrc: str | None
    variant: str | None
    dest: str                  # 'server' | 'fiio' | 'both'
    target_rel: str            # library-relative path for the expected extension (.flac unless planned otherwise)

    def target(self, ext: str) -> str:
        return f"{naming.album_dir(self.artist, self.release)}/{naming.track_file(self.number, self.title, ext)}"


def display_title(title: str, version: str | None) -> str:
    return f"{title} ({version})" if version and version.lower() not in title.lower() else title


def _owned_clusters(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT DISTINCT COALESCE(rec.canon_id, rec.id) FROM file f "
                                       "JOIN recording rec ON rec.id = f.recording_id")}


def _dest(conn: sqlite3.Connection, artist, release_id: int) -> str:
    row = conn.execute("SELECT dest FROM want WHERE release_id = ? ORDER BY id DESC LIMIT 1", (release_id,)).fetchone() \
        or conn.execute("SELECT dest FROM want WHERE artist_id = ? ORDER BY id DESC LIMIT 1", (artist["id"],)).fetchone()
    return row[0] if row else artist["default_dest"]


def _pick(rows: list) -> sqlite3.Row:
    """Canonical recording's track first, then album over single over compilation, then the earliest release."""
    return min(rows, key=lambda r: (r["rec_id"] != r["cid"], _TYPE_RANK.get(r["type"], 9),
                                    r["release_date"] or _NO_DATE, r["release_id"], r["disc"], r["number"]))


def plan_missing(conn: sqlite3.Connection, artist_id: int | None = None, release_id: int | None = None,
                 include_variants: bool = False, ext: str = ".flac") -> list[FetchItem]:
    """Ledger ids in, items out. `release_id` limits the plan to the clusters on that release (the chosen track
    may still sit on another edition of it). Only artists with tidal_match auto/manual are planned."""
    owned = _owned_clusters(conn)
    scope: set[int] | None = None
    if release_id is not None:
        rel = conn.execute("SELECT artist_id FROM release WHERE id = ?", (release_id,)).fetchone()
        if rel is None:
            return []
        artist_id = rel["artist_id"]
        scope = {r[0] for r in conn.execute(
            "SELECT DISTINCT COALESCE(rec.canon_id, rec.id) FROM track t JOIN recording rec "
            "ON rec.id = t.recording_id WHERE t.release_id = ?", (release_id,))}
    artists = conn.execute("SELECT * FROM artist WHERE tidal_match IN ('auto', 'manual') AND tidal_id IS NOT NULL "
                           "AND (?1 IS NULL OR id = ?1) ORDER BY norm_name", (artist_id,)).fetchall()
    items: list[FetchItem] = []
    for a in artists:
        clusters: dict[int, list] = {}
        for r in conn.execute(_TRACKS, (a["id"], release_id)):
            if r["cid"] not in owned and (scope is None or r["cid"] in scope):
                clusters.setdefault(r["cid"], []).append(r)
        picked = [_pick(rows) for rows in clusters.values()]
        picked.sort(key=lambda r: (r["release_date"] or _NO_DATE, r["release_id"], r["disc"], r["number"]))
        for r in picked:
            if r["variant"] is not None and not include_variants:
                continue
            item = FetchItem(
                a["id"], a["name"], r["release_id"], int(r["tidal_release"]) if r["tidal_release"] else None,
                r["release_title"], r["disc"], r["number"], display_title(r["title"], r["version"]),
                r["tidal_track_id"], r["rec_id"], r["t_dur"] if r["t_dur"] is not None else r["r_dur"],
                r["isrc"], r["variant"], _dest(conn, a, r["release_id"]), "")
            items.append(_with_target(item, ext))
    return items


def _with_target(item: FetchItem, ext: str) -> FetchItem:
    return replace(item, target_rel=item.target(ext))
