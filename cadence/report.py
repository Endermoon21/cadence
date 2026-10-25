"""Read-only reports over the ledger: what is owned, what is missing."""
from __future__ import annotations

import sqlite3

# Tracks on the artist's own, non-hidden releases, with the number of files on their recording.
_OWN_TRACKS = (
    "SELECT rel.id AS release_id, rel.title AS release_title, t.recording_id, "
    "(SELECT COUNT(*) FROM file f WHERE f.recording_id = t.recording_id) AS nfiles "
    "FROM release rel JOIN track t ON t.release_id = rel.id "
    "WHERE rel.artist_id = ? AND rel.role = 'own' AND rel.hidden_reason IS NULL")


def _state(owned: int, total: int) -> str:
    if total == 0:
        return "unknown"  # no tracks fetched for this release: can't say it's missing
    if owned == total:
        return "owned"
    return "partial" if owned else "missing"


def _recording_counts(conn: sqlite3.Connection, artist_id: int) -> tuple[int, int]:
    """(unique recordings on own releases, how many of those have a file)"""
    rows = conn.execute(_OWN_TRACKS, (artist_id,)).fetchall()
    owned = {r["recording_id"] for r in rows if r["nfiles"] > 0}
    return len({r["recording_id"] for r in rows}), len(owned)


def artist_report(conn: sqlite3.Connection, artist_id: int) -> dict:
    artist = conn.execute("SELECT * FROM artist WHERE id = ?", (artist_id,)).fetchone()
    tracks = conn.execute(_OWN_TRACKS, (artist_id,)).fetchall()
    per_release: dict[int, list] = {}
    for t in tracks:
        per_release.setdefault(t["release_id"], []).append(t)

    releases = []
    for r in conn.execute("SELECT id, title, type, release_date FROM release WHERE artist_id = ? "
                          "AND role = 'own' AND hidden_reason IS NULL "
                          "ORDER BY release_date IS NULL, release_date, id", (artist_id,)):
        ts = per_release.get(r["id"], [])
        owned = sum(1 for t in ts if t["nfiles"] > 0)
        releases.append({"title": r["title"], "type": r["type"], "date": r["release_date"],
                         "total": len(ts), "owned": owned, "state": _state(owned, len(ts))})

    missing: dict[int, dict] = {}
    for t in tracks:
        if t["nfiles"] == 0:
            m = missing.setdefault(t["recording_id"], {"recording_id": t["recording_id"], "releases": []})
            if t["release_title"] not in m["releases"]:
                m["releases"].append(t["release_title"])
    for rid, m in missing.items():
        rec = conn.execute("SELECT title, isrc, duration_s FROM recording WHERE id = ?", (rid,)).fetchone()
        m.update(title=rec["title"], isrc=rec["isrc"], duration_s=rec["duration_s"])

    total, owned_n = _recording_counts(conn, artist_id)
    unmatched = [dict(r) for r in conn.execute(
        "SELECT path, tag_title AS title, tag_album AS album FROM file "
        "WHERE artist_id = ? AND match = 'unmatched' ORDER BY path", (artist_id,))]
    dups = []
    for r in conn.execute("SELECT rec.id, rec.title FROM recording rec JOIN file f ON f.recording_id = rec.id "
                          "WHERE rec.artist_id = ?1 OR rec.id IN (SELECT t.recording_id FROM track t "
                          "JOIN release rel ON rel.id = t.release_id WHERE rel.artist_id = ?1) "
                          "GROUP BY rec.id HAVING COUNT(*) > 1 ORDER BY rec.id", (artist_id,)):
        paths = [p[0] for p in conn.execute("SELECT path FROM file WHERE recording_id = ? ORDER BY path",
                                            (r["id"],))]
        dups.append({"recording_id": r["id"], "title": r["title"], "paths": paths})
    appears_on = conn.execute("SELECT COUNT(*) FROM release WHERE artist_id = ? AND role = 'appears_on'",
                              (artist_id,)).fetchone()[0]
    return {"artist_id": artist_id, "artist": artist["name"], "tidal_id": artist["tidal_id"],
            "tidal_match": artist["tidal_match"], "releases": releases,
            "recordings_total": total, "recordings_owned": owned_n,
            "missing_recordings": sorted(missing.values(), key=lambda m: m["recording_id"]),
            "unmatched_files": unmatched, "duplicates": dups, "appears_on": appears_on}


def library_summary(conn: sqlite3.Connection) -> list[dict]:
    rows = []
    for a in conn.execute("SELECT id, name, tidal_match FROM artist ORDER BY norm_name").fetchall():
        total, owned = _recording_counts(conn, a["id"])
        own_releases = conn.execute("SELECT COUNT(*) FROM release WHERE artist_id = ? AND role = 'own' "
                                    "AND hidden_reason IS NULL", (a["id"],)).fetchone()[0]
        unmatched = conn.execute("SELECT COUNT(*) FROM file WHERE artist_id = ? AND match = 'unmatched'",
                                 (a["id"],)).fetchone()[0]
        rows.append({"artist_id": a["id"], "artist": a["name"], "tidal_match": a["tidal_match"],
                     "own_releases": own_releases, "recordings_owned": owned, "recordings_total": total,
                     "missing": total - owned, "unmatched_files": unmatched})
    return rows


def format_artist_report(d: dict) -> str:
    lines = [f"{d['artist']}  [tidal: {d['tidal_match']}"
             + (f" {d['tidal_id']}" if d["tidal_id"] else "") + "]",
             f"  recordings owned {d['recordings_owned']}/{d['recordings_total']}, "
             f"appears on {d['appears_on']} other release(s)"]
    lines.append("  releases:")
    for r in d["releases"]:
        lines.append(f"    {r['date'] or '????-??-??'}  {r['type']:<11} {r['state']:<8} "
                     f"{r['owned']}/{r['total']}  {r['title']}")
    if d["missing_recordings"]:
        lines.append(f"  missing recordings ({len(d['missing_recordings'])}):")
        lines += [f"    {m['title']}  ({', '.join(m['releases'])})" for m in d["missing_recordings"]]
    if d["unmatched_files"]:
        lines.append(f"  unmatched files ({len(d['unmatched_files'])}):")
        lines += [f"    {f['path']}" for f in d["unmatched_files"]]
    if d["duplicates"]:
        lines.append(f"  duplicates ({len(d['duplicates'])}):")
        for dup in d["duplicates"]:
            lines.append(f"    {dup['title']}")
            lines += [f"      {p}" for p in dup["paths"]]
    return "\n".join(lines)


def format_summary(rows: list[dict]) -> str:
    if not rows:
        return "no artists in the ledger"
    w = min(40, max(len(r["artist"]) for r in rows))
    lines = [f"{'artist':<{w}}  {'tidal':<9} {'rel':>4} {'owned':>11} {'missing':>7} {'unmatched':>9}"]
    for r in rows:
        lines.append(f"{r['artist'][:w]:<{w}}  {r['tidal_match']:<9} {r['own_releases']:>4} "
                     f"{r['recordings_owned']:>5}/{r['recordings_total']:<5} {r['missing']:>7} "
                     f"{r['unmatched_files']:>9}")
    return "\n".join(lines)
