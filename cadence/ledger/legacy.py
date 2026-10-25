"""Import the legacy library scan (m1/scan_library.py JSONL) into the `file` and `artist` tables."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cadence.identity.normalize import is_various, norm_artist

# scan key -> file column
_FIELDS = {
    "dur": "duration_s", "sr": "sample_rate", "bits": "bit_depth", "br": "bitrate",
    "art": "has_art", "codec": "codec", "size": "size", "mtime": "mtime",
    "title": "tag_title", "artist": "tag_artist", "albumartist": "tag_albumartist",
    "album": "tag_album", "track": "tag_track", "disc": "tag_disc", "date": "tag_date",
    "isrc": "tag_isrc", "mb_track": "tag_mb_track",
}
_COLS = list(_FIELDS.values())
# recording_id / match / match_score are deliberately absent from the UPDATE list (see the reset in
# import_scan for the one case where a changed artist makes an existing link stale).
_UPSERT = (
    f"INSERT INTO file(path, artist_id, {', '.join(_COLS)}, seen_at) "
    f"VALUES (?, ?, {', '.join('?' * len(_COLS))}, ?) "
    "ON CONFLICT(path) DO UPDATE SET artist_id = excluded.artist_id, "
    + ", ".join(f"{c} = excluded.{c}" for c in _COLS) + ", seen_at = excluded.seen_at"
)


def _artist_id(conn: sqlite3.Connection, row: dict, stats: dict) -> int | None:
    name = (row.get("albumartist") or row.get("artist") or "").strip()
    if not name or is_various(name):
        stats["files_without_artist"] += 1
        return None
    norm = norm_artist(name)
    if not norm:
        stats["files_without_artist"] += 1
        return None
    found = conn.execute("SELECT artist_id FROM artist_alias WHERE alias_norm = ?", (norm,)).fetchone() \
        or conn.execute("SELECT id FROM artist WHERE norm_name = ?", (norm,)).fetchone()
    if found:
        return found[0]
    stats["artists_created"] += 1
    return conn.execute("INSERT INTO artist(name, norm_name) VALUES (?, ?)", (name, norm)).lastrowid


def _value(row: dict, key: str):
    v = row.get(key)
    return int(v) if key == "art" and v is not None else v


def import_scan(conn: sqlite3.Connection, jsonl_path: str | Path) -> dict:
    stats = {"rows": 0, "files_new": 0, "files_updated": 0, "artists_created": 0,
             "files_without_artist": 0}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            stats["rows"] += 1
            existed = conn.execute("SELECT artist_id, match FROM file WHERE path = ?", (row["path"],)).fetchone()
            artist_id = _artist_id(conn, row, stats)
            conn.execute(_UPSERT, (row["path"], artist_id,
                                   *(_value(row, k) for k in _FIELDS), now))
            if existed and existed["artist_id"] != artist_id and existed["match"] != "manual":
                # a link to another artist's recording is stale now (manual links are never touched)
                conn.execute("UPDATE file SET recording_id = NULL, match = 'unmatched', match_score = NULL "
                             "WHERE path = ?", (row["path"],))
            stats["files_updated" if existed else "files_new"] += 1
    conn.commit()
    return stats
