"""Artist aliases: tags that are another name for an artist already in the ledger (`cadence alias`).

Helpers here never commit; the caller owns the transaction.
"""
from __future__ import annotations

import sqlite3

from cadence.identity.normalize import is_various, norm_artist
from cadence.ledger import db


def _artist(conn: sqlite3.Connection, name: str):
    return conn.execute("SELECT * FROM artist WHERE norm_name = ?", (norm_artist(name),)).fetchone()


def _tag_norm(albumartist: str | None, artist: str | None) -> str:
    """The normalized artist name import_scan would derive from these tags ('' when it would assign none)."""
    name = (albumartist or artist or "").strip()
    return "" if not name or is_various(name) else norm_artist(name)


def _repoint_files(conn: sqlite3.Connection, alias_norm: str, artist_id: int) -> tuple[int, int]:
    """(files now owned by the target, of which had their link reset). Manual links are never reset."""
    moved = reset = 0
    for f in conn.execute("SELECT id, artist_id, match, tag_albumartist, tag_artist FROM file"):
        if _tag_norm(f["tag_albumartist"], f["tag_artist"]) != alias_norm:
            continue
        moved += 1
        if f["artist_id"] == artist_id:
            continue
        conn.execute("UPDATE file SET artist_id = ? WHERE id = ?", (artist_id, f["id"]))
        if f["match"] != "manual":  # a link to the old artist's recording is stale now
            conn.execute("UPDATE file SET recording_id = NULL, match = 'unmatched', match_score = NULL "
                         "WHERE id = ?", (f["id"],))
            conn.execute("DELETE FROM link_review WHERE file_id = ?", (f["id"],))  # its candidates were the old artist's
            reset += 1
    return moved, reset


def add_alias(conn: sqlite3.Connection, alias_name: str, target_name: str, note: str | None = None) -> dict:
    """Make files tagged `alias_name` belong to the existing artist `target_name`. {'error': ...} on refusal."""
    alias_norm = norm_artist(alias_name)
    target = _artist(conn, target_name)
    if not alias_norm:
        return {"error": f"'{alias_name}' has no usable name"}
    if target is None:
        return {"error": f"no artist '{target_name}' in the ledger (import-legacy first)"}
    if alias_norm == target["norm_name"]:
        return {"error": f"'{alias_name}' is the same name as '{target['name']}'"}
    other = conn.execute("SELECT name, tidal_id FROM artist WHERE norm_name = ? "  # 'ambiguous' ids are guesses
                         "AND tidal_match IN ('auto', 'manual')", (alias_norm,)).fetchone()
    if other is not None:
        return {"error": f"'{alias_name}' is a real artist in the ledger ('{other['name']}', Tidal id "
                         f"{other['tidal_id']}); an alias would hide it"}
    conn.execute("INSERT INTO artist_alias(alias_norm, artist_id, note) VALUES (?, ?, ?) "
                 "ON CONFLICT(alias_norm) DO UPDATE SET artist_id = excluded.artist_id, note = excluded.note",
                 (alias_norm, target["id"], note))
    moved, reset = _repoint_files(conn, alias_norm, target["id"])
    db.log_event(conn, "alias.add", target["name"],
                 f"'{alias_name}' is now an alias of '{target['name']}': {moved} file(s) pointed at it, "
                 f"{reset} link(s) reset", {"alias_norm": alias_norm, "artist_id": target["id"],
                                            "files": moved, "reset": reset})
    return {"alias": alias_name, "alias_norm": alias_norm, "artist": target["name"],
            "artist_id": target["id"], "files": moved, "reset": reset}


def remove_alias(conn: sqlite3.Connection, alias_name: str) -> dict:
    """Delete the alias row only; files stay where they are until the next import-legacy."""
    alias_norm = norm_artist(alias_name)
    row = conn.execute("SELECT a.name FROM artist_alias al JOIN artist a ON a.id = al.artist_id "
                       "WHERE al.alias_norm = ?", (alias_norm,)).fetchone()
    if row is None:
        return {"error": f"no alias '{alias_name}'"}
    conn.execute("DELETE FROM artist_alias WHERE alias_norm = ?", (alias_norm,))
    db.log_event(conn, "alias.remove", row["name"], f"alias '{alias_name}' of '{row['name']}' removed",
                 {"alias_norm": alias_norm})
    return {"alias": alias_name, "alias_norm": alias_norm, "artist": row["name"]}


def list_aliases(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT al.alias_norm, a.name AS artist, al.note, al.created_at FROM artist_alias al "
        "JOIN artist a ON a.id = al.artist_id ORDER BY al.alias_norm")]
