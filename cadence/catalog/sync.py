"""Pull an artist's Tidal catalog into releases / tracks / recordings."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import requests  # transitive dependency of tidalapi; only used to recognise a dead connection

from cadence.gate.bucket import GatePaused
from cadence.identity.normalize import norm_artist, norm_title, split_title
from cadence.ledger.db import log_event
from cadence.sources import base
from cadence.sources.base import BudgetExceeded, CatalogSource, ReleaseDTO, TrackDTO

NOT_IN_LISTING = "not_in_tidal_listing"

DURATION_TOLERANCE_S = 2
RESYNC_AFTER = timedelta(days=7)
NETWORK_BACKOFF_S = (30, 120)  # pause before the next artist after the 1st / 2nd network failure in a row
MAX_NETWORK_FAILURES = 3  # this many artists in a row failing on the network ends the run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _b(v: bool | None) -> int | None:
    return None if v is None else int(v)


def resolve_artist(conn: sqlite3.Connection, catalog: CatalogSource, artist_row) -> int | None:
    """Tidal artist id for a ledger artist, searching if we don't have a trusted one yet."""
    if artist_row["tidal_id"] is not None and artist_row["tidal_match"] in ("auto", "manual"):
        return artist_row["tidal_id"]
    found = catalog.search_artists(artist_row["name"])
    exact = [r for r in found if norm_artist(r.name) == artist_row["norm_name"]]
    if len(exact) == 1:
        state, tid, cands = "auto", exact[0].id, None
    elif exact:
        state, tid, cands = "ambiguous", exact[0].id, exact
    else:
        state, tid, cands = "none", None, found[:5]
    if tid is not None and conn.execute("SELECT 1 FROM artist WHERE tidal_id = ? AND id != ?",
                                        (tid, artist_row["id"])).fetchone():
        state, tid, cands = "ambiguous", None, exact
    conn.execute("UPDATE artist SET tidal_id = ?, tidal_match = ?, tidal_candidates = ? WHERE id = ?",
                 (tid, state, json.dumps([{"id": c.id, "name": c.name} for c in cands])
                  if cands is not None else None, artist_row["id"]))
    log_event(conn, "artist.resolve", artist_row["name"],
              f"Tidal artist search for '{artist_row['name']}': {len(found)} result(s), "
              f"{len(exact)} exact name match(es) -> {state}"
              + (f" (tidal id {tid})" if tid is not None else ""),
              {"state": state, "tidal_id": tid})
    conn.commit()
    return tid


def _find_recording(conn: sqlite3.Connection, artist_id: int, t: TrackDTO, norm: str) -> int | None:
    if t.isrc:
        row = conn.execute("SELECT id FROM recording WHERE isrc = ?", (t.isrc,)).fetchone()
        return row[0] if row else None
    # No ISRC: the same Tidal track keeps its recording on re-sync; otherwise fuzzy-dedupe on
    # (artist, norm_title incl. variant, duration within tolerance). Unknown duration never dedupes.
    row = conn.execute("SELECT recording_id FROM track WHERE tidal_id = ?", (t.tidal_id,)).fetchone()
    if row:
        return row[0]
    if t.duration_s is None:
        return None
    for r in conn.execute("SELECT id, duration_s FROM recording WHERE artist_id = ? AND norm_title = ? "
                          "AND duration_s IS NOT NULL ORDER BY id", (artist_id, norm)):
        if abs(r["duration_s"] - t.duration_s) <= DURATION_TOLERANCE_S:
            return r["id"]
    return None


def _upsert_recording(conn: sqlite3.Connection, artist_id: int, t: TrackDTO, stats: dict) -> int:
    norm = norm_title(t.title, t.version)
    found = _find_recording(conn, artist_id, t, norm)
    if found is not None:
        return found
    stats["recordings_new"] += 1
    return conn.execute(
        "INSERT INTO recording(isrc, artist_id, title, norm_title, version, variant, duration_s) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (t.isrc, artist_id, t.title, norm, t.version, split_title(t.title, t.version).variant,
         t.duration_s)).lastrowid


def _upsert_track(conn: sqlite3.Connection, release_id: int, recording_id: int, t: TrackDTO) -> None:
    conn.execute("DELETE FROM track WHERE release_id = ? AND disc = ? AND number = ? "
                 "AND (tidal_id IS NULL OR tidal_id != ?)", (release_id, t.disc, t.number, t.tidal_id))
    conn.execute(
        "INSERT INTO track(release_id, recording_id, tidal_id, disc, number, title, version, "
        "duration_s, explicit, available) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tidal_id) DO UPDATE SET release_id = excluded.release_id, "
        "recording_id = excluded.recording_id, disc = excluded.disc, number = excluded.number, "
        "title = excluded.title, version = excluded.version, duration_s = excluded.duration_s, "
        "explicit = excluded.explicit, available = excluded.available",
        (release_id, recording_id, t.tidal_id, t.disc, t.number, t.title, t.version,
         t.duration_s, _b(t.explicit), _b(t.available)))


def _upsert_release(conn: sqlite3.Connection, artist_id: int, r: ReleaseDTO, role: str) -> int | None:
    """Release id, or None when another artist already owns this Tidal release."""
    values = (r.title, norm_title(r.title, r.version), r.version, r.type, r.release_date,
              _b(r.explicit), r.num_tracks, r.upc, _b(r.available))
    ref = str(r.tidal_id)
    row = conn.execute("SELECT r.id, r.artist_id, r.role FROM release_source s JOIN release r "
                       "ON r.id = s.release_id WHERE s.provider = 'tidal' AND s.provider_ref = ?",
                       (ref,)).fetchone()
    if row is None:
        rid = conn.execute(
            "INSERT INTO release(artist_id, role, title, norm_title, version, type, release_date, "
            "explicit, num_tracks, upc, available) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artist_id, role, *values)).lastrowid
        conn.execute("INSERT INTO release_source(release_id, provider, provider_ref) "
                     "VALUES (?, 'tidal', ?)", (rid, ref))
        return rid
    if row["artist_id"] != artist_id and not (role == "own" and row["role"] == "appears_on"):
        return None
    conn.execute(
        "UPDATE release SET artist_id = ?, role = ?, title = ?, norm_title = ?, version = ?, type = ?, "
        "release_date = ?, explicit = ?, num_tracks = ?, upc = ?, available = ?, "
        "hidden_reason = CASE WHEN hidden_reason = ? THEN NULL ELSE hidden_reason END, "
        "updated_at = datetime('now') WHERE id = ?", (artist_id, role, *values, NOT_IN_LISTING, row["id"]))
    conn.execute("UPDATE release_source SET seen_at = datetime('now') "
                 "WHERE provider = 'tidal' AND provider_ref = ?", (ref,))
    return row["id"]


def _hide_unlisted(conn: sqlite3.Connection, artist_id: int, listed_refs: set[str]) -> int:
    """Hide (never delete) tidal-sourced own releases that Tidal's current listing no longer contains."""
    stale = [r["id"] for r in conn.execute(
        "SELECT rel.id, s.provider_ref FROM release rel JOIN release_source s ON s.release_id = rel.id "
        "AND s.provider = 'tidal' WHERE rel.artist_id = ? AND rel.role = 'own' AND rel.hidden_reason IS NULL",
        (artist_id,)) if r["provider_ref"] not in listed_refs]
    conn.executemany("UPDATE release SET hidden_reason = ?, updated_at = datetime('now') WHERE id = ?",
                     [(NOT_IN_LISTING, i) for i in stale])
    return len(stale)


def sync_artist(conn: sqlite3.Connection, catalog: CatalogSource, artist_row) -> dict:
    """Fetch first, then write everything for the artist in one commit. A GatePaused leaves no partial artist."""
    artist_id, tid = artist_row["id"], artist_row["tidal_id"]
    releases = catalog.artist_releases(tid)
    own = {r.tidal_id: bool(r.artists) and r.artists[0].id == tid for r in releases}
    tracks: dict[int, list[TrackDTO]] = {}
    gone: list[ReleaseDTO] = []
    for r in releases:
        if not own[r.tidal_id]:
            continue
        try:
            tracks[r.tidal_id] = catalog.release_tracks(r.tidal_id)
        except (GatePaused, BudgetExceeded):
            raise
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            tracks[r.tidal_id] = []      # the failure was not cached by the source; a later sync retries it
            gone.append(r)

    if gone and len(gone) == len(tracks):
        ids = ", ".join(str(r.tidal_id) for r in gone[:5]) + (" ..." if len(gone) > 5 else "")
        raise ReleaseTracksMissing(f"Tidal returned 404 for the tracks of all {len(gone)} own release(s) "
                                   f"({ids}); not marking the artist as synced")
    stats = {"releases": 0, "own": 0, "appears_on": 0, "tracks": 0, "recordings_new": 0, "shared": 0,
             "hidden": 0, "release_missing": len(gone)}
    for r in gone:
        log_event(conn, "catalog.release_missing", artist_row["name"],
                  f"Tidal returned 404 for the tracks of release {r.tidal_id} '{r.title}'; "
                  "kept it without tracks", {"release_id": r.tidal_id})
    for r in releases:
        role = "own" if own[r.tidal_id] else "appears_on"
        rid = _upsert_release(conn, artist_id, r, role)
        if rid is None:
            stats["shared"] += 1
            continue
        stats["releases"] += 1
        stats[role] += 1
        for t in tracks.get(r.tidal_id, []):
            _upsert_track(conn, rid, _upsert_recording(conn, artist_id, t, stats), t)
            stats["tracks"] += 1
    stats["hidden"] = _hide_unlisted(conn, artist_id, {str(r.tidal_id) for r in releases})
    conn.execute("UPDATE artist SET catalog_synced_at = ? WHERE id = ?",
                 (_now().isoformat(timespec="seconds"), artist_id))
    log_event(conn, "catalog.sync", artist_row["name"],
              f"Synced Tidal catalog for '{artist_row['name']}': {stats['own']} own release(s), "
              f"{stats['appears_on']} appears-on, {stats['tracks']} track(s), "
              f"{stats['recordings_new']} new recording(s), {stats['hidden']} no longer listed", stats)
    conn.commit()
    return stats


def _recently_synced(row) -> bool:
    if not row["catalog_synced_at"]:
        return False
    at = datetime.fromisoformat(row["catalog_synced_at"])
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return _now() - at < RESYNC_AFTER


def work_status(row, force: bool = False) -> str:
    """'due' if this artist needs Tidal calls now; otherwise why not: ambiguous | unresolved | skipped.

    ambiguous / none artists wait for `cadence artist set-tidal`, even with --force."""
    if row["tidal_match"] == "ambiguous":
        return "ambiguous"
    if row["tidal_match"] == "none":
        return "unresolved"
    if not force and _recently_synced(row):
        return "skipped"
    return "due"


def select_artists(conn: sqlite3.Connection, names: list[str] | None) -> tuple[list, list[str]]:
    """(artist rows, names that are not in the ledger)"""
    rows = conn.execute("SELECT * FROM artist ORDER BY norm_name").fetchall()
    if names is None:
        return rows, []
    wanted = {norm_artist(n): n for n in names}
    rows = [r for r in rows if r["norm_name"] in wanted]
    have = {r["norm_name"] for r in rows}
    return rows, [n for k, n in wanted.items() if k not in have]


def pending_work(conn: sqlite3.Connection, names: list[str] | None, force: bool = False) -> dict:
    """What sync_all would do, without touching Tidal: counts per work_status plus unknown names."""
    rows, unknown = select_artists(conn, names)
    counts = {"due": 0, "skipped": 0, "ambiguous": 0, "unresolved": 0}
    for r in rows:
        counts[work_status(r, force)] += 1
    return {**counts, "unknown": unknown}


def _chain(exc: BaseException):
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _named(exc: BaseException, name: str) -> bool:
    return any(c.__name__ == name for e in _chain(exc) for c in type(e).__mro__)


def _statuses(exc: BaseException) -> set[int]:
    return {getattr(getattr(e, "response", None), "status_code", None) for e in _chain(exc)}


def _is_not_found(exc: BaseException) -> bool:
    return _named(exc, "ObjectNotFound") or 404 in _statuses(exc)


def _is_auth_failure(exc: BaseException) -> bool:
    """Our login is rejected: HTTP 401 or an AuthenticationError, unless a 429 caused it (that is the gate's
    business). A 403 is *not* auth: Tidal also uses it for single restricted items."""
    statuses = _statuses(exc)
    return (_named(exc, "AuthenticationError") or 401 in statuses) and 429 not in statuses


class ReleaseTracksMissing(RuntimeError):
    """Every own release of an artist answered 404 for its tracks; the listing itself is suspect."""


_QUERY_OR_SECRET = re.compile(r"(?i)(sessionid\S*|bearer\s+\S+)")


def _safe_message(exc: Exception) -> str:
    """One short line for the event log; never a URL query string, session id or token."""
    msg = str(exc).splitlines()[0] if str(exc) else ""
    return _QUERY_OR_SECRET.sub("[redacted]", msg.split("?")[0])[:200]


def _is_dead_connection(exc: Exception) -> bool:
    """No usable network: refused / dropped connections and timeouts."""
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                            ConnectionError, TimeoutError))


def sync_all(conn: sqlite3.Connection, catalog: CatalogSource, names: list[str] | None = None,
             max_calls: int = 400, force: bool = False,
             progress: Callable[[dict], None] | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Sync artists by name.

    Stops cleanly when the gate pauses (stopped='paused'), the call budget is spent ('budget') or Tidal
    rejects our login ('auth'). Any other error, including a timeout or dropped connection, is logged against
    that artist and the run continues; after a network failure it waits NETWORK_BACKOFF_S before the next
    artist, and MAX_NETWORK_FAILURES artists in a row failing that way stops the run ('network')."""
    catalog.max_calls = max_calls
    catalog.use_cache = not force
    rows, unknown = select_artists(conn, names)
    result: dict = {"artists": [], "synced": 0, "skipped": 0, "unresolved": 0, "ambiguous": 0, "errors": 0,
                    "release_missing": 0, "unknown": unknown, "stopped": None, "until": None}

    def record(row, status: str, **extra) -> None:
        entry = {"artist": row["name"], "status": status, **extra}
        result["artists"].append(entry)
        if progress:
            progress(entry)

    network_failures = 0  # consecutive artists that failed on the network
    backoff = 0
    for row in rows:
        waiting = work_status(row, force)
        if waiting != "due":
            result[waiting] += 1
            if waiting == "ambiguous":
                record(row, waiting, candidates=len(json.loads(row["tidal_candidates"] or "[]")))
            else:
                record(row, waiting)
            continue
        if backoff:
            sleep(backoff)
            backoff = 0
        try:
            tid = resolve_artist(conn, catalog, row)
            fresh = conn.execute("SELECT * FROM artist WHERE id = ?", (row["id"],)).fetchone()
            if fresh["tidal_match"] == "ambiguous":
                n = len(json.loads(fresh["tidal_candidates"] or "[]"))
                result["ambiguous"] += 1
                record(row, "ambiguous", candidates=n)
            elif tid is None:
                result["unresolved"] += 1
                record(row, "unresolved")
            else:
                stats = sync_artist(conn, catalog, fresh)
                record(row, "synced", **stats)
                result["synced"] += 1
                result["release_missing"] += stats["release_missing"]
            network_failures = 0
        except GatePaused as p:
            conn.rollback()
            result["stopped"] = "paused"
            result["until"] = p.until.isoformat()
            log_event(conn, "gate.pause", row["name"],
                      f"Tidal gate paused while syncing '{row['name']}' until {p.until.isoformat()}: "
                      f"{p.reason}", {"until": p.until.isoformat()})
            conn.commit()
            break
        except BudgetExceeded:
            conn.rollback()
            result["stopped"] = "budget"
            log_event(conn, "catalog.stop", row["name"],
                      f"Stopped while syncing '{row['name']}': call budget {max_calls} reached")
            conn.commit()
            break
        except Exception as exc:
            conn.rollback()
            msg = f"{type(exc).__name__}: {_safe_message(exc)}"
            result["errors"] += 1
            log_event(conn, "catalog.error", row["name"], msg)
            conn.commit()
            record(row, "error", error=msg)
            if _is_auth_failure(exc):
                result["stopped"] = "auth"      # every further artist would fail the same way
                break
            if not _is_dead_connection(exc):
                network_failures = 0
                continue
            network_failures += 1
            if network_failures >= MAX_NETWORK_FAILURES:
                result["stopped"] = "network"
                break
            backoff = NETWORK_BACKOFF_S[min(network_failures, len(NETWORK_BACKOFF_S)) - 1]
    result["calls"] = catalog.gate.calls
    return result


def clear_cache(conn: sqlite3.Connection, artist_row=None) -> int:
    """Delete cached Tidal responses; returns rows deleted.

    No artist: every `tidal:%` key. With an artist: its release listing plus the track lists of its releases
    (from the ledger and from the cached listing, which covers releases an interrupted sync never wrote)."""
    if artist_row is None:
        n = conn.execute("DELETE FROM catalog_cache WHERE key LIKE 'tidal:%'").rowcount
    else:
        listing = f"tidal:releases:{artist_row['tidal_id']}"
        ids = {int(r[0]) for r in conn.execute(
            "SELECT s.provider_ref FROM release_source s JOIN release r ON r.id = s.release_id "
            "WHERE s.provider = 'tidal' AND r.artist_id = ?", (artist_row["id"],))}
        cached = conn.execute("SELECT body FROM catalog_cache WHERE key = ?", (listing,)).fetchone()
        if cached:
            ids |= {r.tidal_id for r in base.from_json(cached[0])}
        keys = [listing, *(f"tidal:tracks:{i}" for i in sorted(ids))]
        n = sum(conn.execute("DELETE FROM catalog_cache WHERE key = ?", (k,)).rowcount for k in keys)
    conn.commit()
    return n
