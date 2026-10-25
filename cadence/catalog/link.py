"""Link library files to catalog recordings. Never touches match='manual'."""
from __future__ import annotations

import sqlite3

from cadence.identity.matcher import FileFacts, RecCandidate, match_file


def _candidates(conn: sqlite3.Connection, artist_id: int) -> list[RecCandidate]:
    """Recordings of the artist's own releases or credited to them. Recordings are shared by ISRC across
    artists (a collab single and the other artist's album), so recording.artist_id alone is not enough."""
    releases: dict[int, set[str]] = {}
    for r in conn.execute("SELECT t.recording_id, rel.norm_title FROM track t "
                          "JOIN release rel ON rel.id = t.release_id WHERE t.recording_id IN "
                          "(SELECT id FROM recording WHERE artist_id = ?1 UNION "
                          " SELECT t2.recording_id FROM track t2 JOIN release r2 ON r2.id = t2.release_id "
                          " WHERE r2.artist_id = ?1)", (artist_id,)):
        releases.setdefault(r["recording_id"], set()).add(r["norm_title"])
    return [RecCandidate(r["id"], r["isrc"], r["norm_title"], r["duration_s"],
                         frozenset(releases.get(r["id"], ())))
            for r in conn.execute("SELECT id, isrc, norm_title, duration_s FROM recording WHERE id IN "
                                  "(SELECT id FROM recording WHERE artist_id = ?1 UNION "
                                  " SELECT t.recording_id FROM track t JOIN release r ON r.id = t.release_id "
                                  " WHERE r.artist_id = ?1) ORDER BY id", (artist_id,))]


def link_files(conn: sqlite3.Connection, artist_id: int | None = None, rematch: bool = False) -> dict:
    where = "match != 'manual'" if rematch else "match = 'unmatched'"
    args: list = []
    if artist_id is not None:
        where += " AND artist_id = ?"
        args.append(artist_id)
    files = conn.execute(f"SELECT id, artist_id, tag_title, tag_album, tag_isrc, duration_s FROM file "
                         f"WHERE artist_id IS NOT NULL AND {where} ORDER BY artist_id, id", args).fetchall()
    stats = {"files": len(files), "isrc": 0, "fuzzy": 0, "unmatched": 0}
    cands: dict[int, list[RecCandidate]] = {}
    for f in files:
        if f["artist_id"] not in cands:
            cands[f["artist_id"]] = _candidates(conn, f["artist_id"])
        m = match_file(FileFacts(f["tag_title"] or "", f["duration_s"], f["tag_isrc"], f["tag_album"]),
                       cands[f["artist_id"]])
        if m is None:
            stats["unmatched"] += 1
            conn.execute("UPDATE file SET recording_id = NULL, match = 'unmatched', match_score = NULL "
                         "WHERE id = ?", (f["id"],))
            continue
        stats[m.method] += 1
        conn.execute("UPDATE file SET recording_id = ?, match = ?, match_score = ? WHERE id = ?",
                     (m.recording_id, m.method, m.score, f["id"]))
    stats["matched"] = stats["isrc"] + stats["fuzzy"]
    conn.commit()
    return stats
