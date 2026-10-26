"""Derived views over the ledger: release groups (editions) and recording clusters. Never deletes anything.

Helpers here never commit; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from cadence.identity.grouping import RecordingItem, cluster_recordings, date_year, is_remaster, release_group_key
from cadence.identity.normalize import remaster_year


def _regroup_releases(conn: sqlite3.Connection, artist_id: int) -> int:
    keys = {r["id"]: release_group_key(r["title"], r["version"], r["type"]) for r in conn.execute(
        "SELECT id, title, version, type FROM release WHERE artist_id = ?", (artist_id,))}
    conn.executemany("UPDATE release SET group_key = ? WHERE id = ? AND group_key IS NOT ?",
                     [(k, rid, k) for rid, k in keys.items()])
    return len(set(keys.values()))


def _release_groups(conn: sqlite3.Connection, artist_id: int) -> dict[int, set[str]]:
    """Release group keys (as stored, so call after _regroup_releases) of the releases each recording is on."""
    out: dict[int, set[str]] = {}
    for r in conn.execute(
            "SELECT t.recording_id, rel.artist_id || ':' || COALESCE(rel.group_key, 'release:' || rel.id) AS gk "
            "FROM track t JOIN release rel ON rel.id = t.release_id "
            "WHERE t.recording_id IN (SELECT id FROM recording WHERE artist_id = ?)", (artist_id,)):
        out.setdefault(r["recording_id"], set()).add(r["gk"])
    return out


def _remasters(conn: sqlite3.Connection, artist_id: int) -> dict[int, tuple[int | None, str | None]]:
    """recording id -> (remaster year, earliest remaster release date), for the recordings that are remasters.

    A recording is a remaster if its own title/version says so (then every release it is on counts), or if it
    is on a release whose title/version says so (then only those releases count). The year is the one written
    on the recording's own marker, else the earliest one written on a qualifying release, else the year of the
    earliest qualifying release date (so "latest" stays literal); None only if there is no date either."""
    cache: dict[tuple, tuple[bool, int | None]] = {}

    def flagged(title: str, version: str | None, release: bool) -> tuple[bool, int | None]:
        k = (title, version, release)
        if k not in cache:
            cache[k] = (is_remaster(title, version, release=release), remaster_year(title, version))
        return cache[k]

    own = {r["id"]: flagged(r["title"], r["version"], False) for r in conn.execute(
        "SELECT id, title, version FROM recording WHERE artist_id = ?", (artist_id,))}
    dates: dict[int, str | None] = {}
    years: dict[int, int | None] = {}
    for r in conn.execute(
            "SELECT t.recording_id AS rid, rel.title, rel.version, rel.release_date FROM track t "
            "JOIN release rel ON rel.id = t.release_id "
            "WHERE t.recording_id IN (SELECT id FROM recording WHERE artist_id = ?)", (artist_id,)):
        rid = r["rid"]
        is_rel, rel_year = flagged(r["title"], r["version"], True)
        if own[rid][0] or is_rel:
            dates[rid] = min((x for x in (dates.get(rid), r["release_date"]) if x), default=None)
            if is_rel and rel_year:
                years[rid] = min(filter(None, (years.get(rid), rel_year)))
    return {rid: (own[rid][1] or years.get(rid) or date_year(dates.get(rid)), dates.get(rid))
            for rid in own if own[rid][0] or rid in dates}


def _regroup_recordings(conn: sqlite3.Connection, artist_id: int) -> tuple[int, int]:
    groups = _release_groups(conn, artist_id)
    remasters = _remasters(conn, artist_id)
    items = [RecordingItem(r["id"], r["norm_title"], r["duration_s"], r["isrc"], r["first_date"], r["variant"],
                           frozenset(groups.get(r["id"], ())), r["id"] in remasters,
                           remasters.get(r["id"], (None, None))[1], remasters.get(r["id"], (None, None))[0])
             for r in conn.execute(
        "SELECT rec.id, rec.norm_title, rec.duration_s, rec.isrc, rec.variant, "
        "(SELECT MIN(rel.release_date) FROM track t JOIN release rel ON rel.id = t.release_id "
        " WHERE t.recording_id = rec.id) AS first_date FROM recording rec WHERE rec.artist_id = ?", (artist_id,))]
    clusters = cluster_recordings(items)
    conn.executemany("UPDATE recording SET canon_id = ? WHERE id = ? AND canon_id IS NOT ?",
                     [(ids[0], rid, ids[0]) for ids in clusters for rid in ids])
    return len(items), len(clusters)


def regroup_artist(conn: sqlite3.Connection, artist_id: int) -> dict:
    """Recompute release.group_key and recording.canon_id for one artist. Idempotent."""
    groups = _regroup_releases(conn, artist_id)
    recordings, clusters = _regroup_recordings(conn, artist_id)
    return {"groups": groups, "recordings": recordings, "clusters": clusters}


def regroup_all(conn: sqlite3.Connection) -> dict:
    total = {"artists": 0, "groups": 0, "recordings": 0, "clusters": 0}
    for a in conn.execute("SELECT id FROM artist ORDER BY id").fetchall():
        for k, v in regroup_artist(conn, a["id"]).items():
            total[k] += v
        total["artists"] += 1
    return total
