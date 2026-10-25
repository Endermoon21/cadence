"""Derived views over the ledger: release groups (editions) and recording clusters. Never deletes anything.

Helpers here never commit; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from cadence.identity.grouping import RecordingItem, cluster_recordings, release_group_key


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


def _regroup_recordings(conn: sqlite3.Connection, artist_id: int) -> tuple[int, int]:
    groups = _release_groups(conn, artist_id)
    items = [RecordingItem(r["id"], r["norm_title"], r["duration_s"], r["isrc"], r["first_date"], r["variant"],
                           frozenset(groups.get(r["id"], ()))) for r in conn.execute(
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
