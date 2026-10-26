"""Help pick the right Tidal profile for an ambiguous artist by comparing each candidate's releases
with the albums we actually own. Read-only: it never changes an artist row, it only suggests a command."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from cadence.catalog.sync import _is_auth_failure, _is_dead_connection, _safe_message, select_artists
from cadence.gate.bucket import GatePaused
from cadence.identity.normalize import fold, split_title
from cadence.sources.base import BudgetExceeded, CatalogSource, ReleaseDTO
from cadence.sources.musicbrainz import MusicBrainz

TIDAL_ARTIST_URL = "https://tidal.com/artist/{}"
MIN_FILE_LEAD = 2     # a winner must lead the runner-up by this many owned files ...
MIN_ALBUM_LEAD = 1    # ... or by this many owned albums
MB_MIN_OVERLAP = 2    # MusicBrainz tiebreak: the pick must share this many titles ...
MB_MIN_LEAD = 2       # ... and lead the other tied candidates by this many
SHOWN_LIBRARY_ALBUMS = 12
SHOWN_TITLES = 3


@dataclass(frozen=True)
class LibraryAlbum:
    title: str  # display spelling: the most common tag among the files
    files: int


def library_albums(conn: sqlite3.Connection, artist_id: int) -> dict[str, LibraryAlbum]:
    """Albums we own for an artist, keyed by split_title(...).base so remaster/deluxe tags don't matter."""
    tags = conn.execute("SELECT tag_album, COUNT(*) AS n FROM file WHERE artist_id = ? "
                        "AND tag_album IS NOT NULL AND TRIM(tag_album) != '' GROUP BY tag_album "
                        "ORDER BY n DESC, tag_album", (artist_id,)).fetchall()
    out: dict[str, LibraryAlbum] = {}
    for t in tags:  # most common spelling first, so it becomes the display title
        base = split_title(t["tag_album"]).base
        if not base:
            continue
        seen = out.get(base)
        out[base] = (LibraryAlbum(t["tag_album"], t["n"]) if seen is None
                     else LibraryAlbum(seen.title, seen.files + t["n"]))
    return out


def parse_candidates(raw: str | None) -> list[dict]:
    """Usable [{id, name}] from artist.tidal_candidates; malformed entries and repeated ids are dropped."""
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    out: list[dict] = []
    for c in data if isinstance(data, list) else []:
        try:
            cid = int(c["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if all(o["id"] != cid for o in out):
            out.append({"id": cid, "name": str(c.get("name") or "")})
    return out


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and re.fullmatch(r"\d{4}", date[:4]) else None


def _score(cand: dict, releases: list[ReleaseDTO], lib: dict[str, LibraryAlbum], taken_by: str | None) -> dict:
    bases = {split_title(r.title, r.version).base for r in releases}
    hit = [b for b in lib if b in bases]
    years = [y for y in (_year(r.release_date) for r in releases) if y is not None]
    newest: list[str] = []
    for r in sorted(releases, key=lambda r: r.release_date or "", reverse=True):
        if r.title not in newest:
            newest.append(r.title)
    return {"id": cand["id"], "name": cand["name"], "releases": len(releases),
            "matched_library_albums": len(hit), "matched_files": sum(lib[b].files for b in hit),
            "matched_titles": [lib[b].title for b in hit], "sample_titles": newest[:5],
            "first_year": min(years, default=None), "last_year": max(years, default=None),
            "url": TIDAL_ARTIST_URL.format(cand["id"]), "taken_by": taken_by, "error": None}


def _recommend(ranked: list[dict]) -> int | None:
    """Id of the clear winner among candidates sorted best-first, else None."""
    top = ranked[0] if ranked else None
    if top is None or top["matched_files"] < 1:
        return None
    runner = ranked[1] if len(ranked) > 1 else {"matched_files": 0, "matched_library_albums": 0}
    lead_files = top["matched_files"] - runner["matched_files"]
    lead_albums = top["matched_library_albums"] - runner["matched_library_albums"]
    if lead_files > 0 and (lead_files >= MIN_FILE_LEAD or lead_albums >= MIN_ALBUM_LEAD):
        return top["id"]
    return None


def _mb_tiebreak(res: dict, mb: MusicBrainz, bases_by_id: dict[int, set[str]]) -> int | None:
    """Add `mb_overlap`/`mb_total` to every candidate and sets res['mb']. Returns a winner only among the
    candidates tied with the best one on library evidence, so MusicBrainz can never outvote owned albums."""
    res["mb"] = {"artist": None, "mbid": None, "release_groups": 0, "skipped": None, "decided": False}
    try:
        want = fold(res["artist"])
        exact = [a for a in mb.search_artists(res["artist"]) if fold(a["name"]) == want]
        if not exact:
            res["mb"]["skipped"] = "no exact artist name match on MusicBrainz"
            return None
        artist = max(exact, key=lambda a: a["score"])  # max() keeps the first of equal scores
        titles = mb.release_group_titles(artist["mbid"])
    except Exception as exc:
        res["mb"]["skipped"] = f"lookup failed: {type(exc).__name__}: {_safe_message(exc)}"
        return None
    res["mb"].update(artist=artist["name"], mbid=artist["mbid"], release_groups=len(titles))
    mb_bases = {split_title(t).base for t in titles}
    for c in res["candidates"]:
        c["mb_overlap"], c["mb_total"] = len(bases_by_id[c["id"]] & mb_bases), len(bases_by_id[c["id"]])
    if not res["candidates"]:
        return None
    top = res["candidates"][0]
    tied = sorted((c for c in res["candidates"] if (c["matched_files"], c["matched_library_albums"])
                   == (top["matched_files"], top["matched_library_albums"])), key=lambda c: -c["mb_overlap"])
    if len(tied) > 1 and tied[0]["mb_overlap"] >= MB_MIN_OVERLAP \
            and tied[0]["mb_overlap"] - tied[1]["mb_overlap"] >= MB_MIN_LEAD:
        res["mb"]["decided"] = True
        return tied[0]["id"]
    return None


def score_candidates(conn: sqlite3.Connection, catalog: CatalogSource, artist_row, max_candidates: int = 8,
                     mb: MusicBrainz | None = None) -> dict:
    """Score each stored candidate of one artist. Uses catalog.artist_releases only (gated and cached like sync).

    `stopped` is 'paused' | 'budget' | 'auth' | 'network' when the run had to end early; what was gathered is
    still returned, but with no recommendation because the missing candidates might have been the better match."""
    lib = library_albums(conn, artist_row["id"])
    res: dict = {"artist": artist_row["name"], "library_albums": [{"title": a.title, "files": a.files}
                                                                  for a in lib.values()],
                 "library_files": sum(a.files for a in lib.values()), "candidates": [], "truncated": 0,
                 "recommendation": None, "note": None, "skipped": None, "stopped": None, "until": None,
                 "mb": None}
    cands = parse_candidates(artist_row["tidal_candidates"])
    if not cands:
        res["skipped"] = "no usable candidates stored for this artist"
        return res
    res["truncated"] = max(0, len(cands) - max_candidates)
    bases_by_id: dict[int, set[str]] = {}
    for cand in cands[:max_candidates]:
        try:
            releases = catalog.artist_releases(cand["id"])
        except GatePaused as p:
            res["stopped"], res["until"] = "paused", p.until.isoformat()
            break
        except BudgetExceeded:
            res["stopped"] = "budget"
            break
        except Exception as exc:
            if _is_auth_failure(exc):
                res["stopped"] = "auth"
                break
            res["candidates"].append(_score(cand, [], lib, None)
                                     | {"error": f"{type(exc).__name__}: {_safe_message(exc)}"})
            if _is_dead_connection(exc):
                res["stopped"] = "network"
                break
            continue
        other = conn.execute("SELECT name FROM artist WHERE tidal_id = ? AND id != ?",
                             (cand["id"], artist_row["id"])).fetchone()
        res["candidates"].append(_score(cand, releases, lib, other["name"] if other else None))
        bases_by_id[cand["id"]] = {split_title(r.title, r.version).base for r in releases}
    res["candidates"].sort(key=lambda c: (-c["matched_files"], -c["matched_library_albums"]))
    if res["stopped"] or any(c["error"] for c in res["candidates"]):
        return res
    winner = _recommend(res["candidates"])
    if winner is None and mb is not None:  # tiebreaker only: a clear library winner is never second-guessed
        winner = _mb_tiebreak(res, mb, bases_by_id)
    taken = next((c["taken_by"] for c in res["candidates"] if c["id"] == winner), None)
    if taken:
        res["note"] = f"best match {winner} already belongs to '{taken}'"
    else:
        res["recommendation"] = winner
    return res


def pending_artists(conn: sqlite3.Connection, names: list[str] | None) -> tuple[list, list[str], list]:
    """(ambiguous artist rows to compare, names not in the ledger, named artists that are not ambiguous)"""
    rows, unknown = select_artists(conn, names or None)
    return ([r for r in rows if r["tidal_match"] == "ambiguous"], unknown,
            [r for r in rows if r["tidal_match"] != "ambiguous"] if names else [])


def run_candidates(conn: sqlite3.Connection, catalog: CatalogSource, rows: list, max_candidates: int = 8,
                   progress: Callable[[dict], None] | None = None, mb: MusicBrainz | None = None) -> dict:
    """score_candidates for each row; ends the run at the first stop (paused / budget / auth / network)."""
    out: dict = {"artists": [], "stopped": None, "until": None}
    for row in rows:
        res = score_candidates(conn, catalog, row, max_candidates, mb)
        out["artists"].append(res)
        if progress:
            progress(res)
        if res["stopped"]:
            out["stopped"], out["until"] = res["stopped"], res["until"]
            break
    out["calls"] = catalog.gate.calls
    return out


def _quote(s: str) -> str:
    return '"' + re.sub(r'(["\\$`])', r"\\\1", s) + '"'


def _titles(titles: list[str]) -> str:
    return ", ".join(f'"{t}"' for t in titles[:SHOWN_TITLES])


def format_result(res: dict) -> str:
    """Header with the library albums, one line per candidate (best first), then the verdict."""
    n_albums = len(res["library_albums"])
    lines = [f"{res['artist']}: library has {n_albums} album(s), {res['library_files']} file(s)"]
    if res["skipped"]:
        return "\n".join([*lines, f"  skipped: {res['skipped']}"])
    shown = res["library_albums"][:SHOWN_LIBRARY_ALBUMS]
    if shown:
        more = f", +{n_albums - len(shown)} more" if n_albums > len(shown) else ""
        lines.append("  library: " + ", ".join(f'"{a["title"]}" ({a["files"]})' for a in shown) + more)
    for c in res["candidates"]:
        head = f"  [id {c['id']}] {c['name']}"
        if c["error"]:
            lines.append(f"{head}  error: {c['error']}  {c['url']}")
            continue
        eg = _titles(c["matched_titles"] or c["sample_titles"])
        lines.append(f"{head}  releases={c['releases']}  library-albums matched {c['matched_library_albums']}/"
                     f"{n_albums} (files {c['matched_files']}/{res['library_files']})"
                     + (f"  e.g. {eg}" if eg else "")
                     + (f"  mb-overlap {c['mb_overlap']}/{c['mb_total']}" if "mb_overlap" in c else "")
                     + f"  {c['url']}"
                     + (f"  (id already used by '{c['taken_by']}')" if c["taken_by"] else ""))
    if res["mb"]:
        m = res["mb"]
        lines.append(f"  musicbrainz: skipped, {m['skipped']}" if m["skipped"] else
                     f"  musicbrainz: {m['artist']} ({m['mbid']}), {m['release_groups']} release group(s)")
    if res["truncated"]:
        lines.append(f"  ({res['truncated']} more candidate(s) not checked; raise max_candidates)")
    if res["stopped"]:
        lines.append(f"  -> incomplete (stopped={res['stopped']}), no suggestion")
    elif any(c["error"] for c in res["candidates"]):
        lines.append("  -> some candidates failed to load, no suggestion")
    elif res["recommendation"] is not None:
        lines.append(f"  -> suggest: cadence artist set-tidal {_quote(res['artist'])} {res['recommendation']}"
                     + (" (musicbrainz tiebreak)" if res["mb"] and res["mb"]["decided"] else ""))
    else:
        lines.append(f"  -> {res['note'] or 'no clear winner'}, check the links")
    return "\n".join(lines)
