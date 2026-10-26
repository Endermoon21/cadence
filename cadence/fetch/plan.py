"""What to fetch: one item per unowned recording cluster, using the canonical recording's Tidal track.

Read-only over the ledger. A cluster is owned when any file is linked to a recording in it (library-wide), the
same rule report.py uses. The canonical recording is `COALESCE(canon_id, id)`, which `cadence regroup` sets
with the latest-remaster rule (identity/grouping.py).

Scope (default 'partial'): only release groups where something is already owned (a linked cluster, or a folder
in the library that carries the group's name). Folder naming: an existing library folder of the same release
group is reused as is; otherwise the album title without remaster/edition markers is used. Near-duplicates of
files the user already has (same release group, similar title, duration within 3 s) are skipped and reported."""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, replace

from cadence.identity.normalize import _GROUP_RE, _classify, _peel_suffixes, norm_title, split_title
from cadence.ledger import naming

_TYPE_RANK = {"album": 0, "ep": 1, "single": 2, "compilation": 3, "other": 4}
_NO_DATE = "9999-99-99"
_MARKERS = {"remaster", "deluxe", "edition"}
SAME_DURATION_S = 3

_REL_FILTER = "rel.artist_id = ?1 AND ((rel.role = 'own' AND rel.hidden_reason IS NULL) OR rel.id = ?2)"
_TRACKS = (
    "SELECT t.tidal_id AS tidal_track_id, t.disc, t.number, t.title, t.version, t.duration_s AS t_dur, "
    "rec.id AS rec_id, COALESCE(rec.canon_id, rec.id) AS cid, rec.variant, rec.isrc, rec.duration_s AS r_dur, "
    "rel.id AS release_id, rel.title AS release_title, rel.type, rel.release_date, "
    "(SELECT s.provider_ref FROM release_source s WHERE s.release_id = rel.id AND s.provider = 'tidal') AS tidal_release "
    "FROM track t JOIN release rel ON rel.id = t.release_id JOIN recording rec ON rec.id = t.recording_id "
    f"WHERE {_REL_FILTER} AND t.tidal_id IS NOT NULL AND t.available IS NOT 0")


@dataclass(frozen=True)
class FetchItem:
    artist_id: int
    artist: str
    release_id: int
    tidal_release_id: int | None
    release: str               # album name for the ALBUM tag: the reused folder's name, else the cleaned title
    album_dir: str             # library-relative 'Artist/Album' directory
    disc: int
    number: int
    title: str                 # file / TITLE name: Tidal title plus version, remaster markers stripped
    tidal_track_id: int
    recording_id: int
    duration_s: int | None
    isrc: str | None
    variant: str | None
    dest: str                  # 'server' | 'fiio' | 'both'
    target_rel: str            # library-relative path for the expected extension (.flac unless planned otherwise)
    discs: int = 1             # number of discs on the release; more than one adds the disc prefix to the file name

    def target(self, ext: str) -> str:
        return f"{self.album_dir}/{naming.track_file(self.number, self.title, ext, self.disc, self.discs)}"


# ---- names ----

def _is_marker(group: str, release: bool) -> bool:
    c = _classify(group, release)
    return c is not None and c[0] == "flag" and c[1] in _MARKERS


def strip_markers(title: str, release: bool = False) -> str:
    """The title without remaster / edition markers; case, punctuation and other brackets are kept."""
    head, suffixes = _peel_suffixes(title)
    head = _GROUP_RE.sub(lambda m: " " if _is_marker(m.group(1) if m.group(1) is not None else m.group(2), release)
                         else m.group(0), head)
    out = " - ".join([" ".join(head.split()), *(s for s in suffixes if not _is_marker(s, release))]).strip()
    return out or title


def display_title(title: str, version: str | None) -> str:
    full = f"{title} ({version})" if version and version.lower() not in title.lower() else title
    return strip_markers(full)


def _name_key(title: str, version: str | None = None) -> str:
    """Folded release name without edition markers: what a release group is called, and what a folder matches."""
    p = split_title(title, version, release=True)
    return p.base + (f" [{p.variant}]" if p.variant else "")


def _related(a: str, b: str) -> bool:
    """Equal normalised titles, or one is the other plus extra whole words ('hitchhiker' / 'hitchhiker like an inca 1')."""
    return bool(a and b) and (a == b or a.startswith(b + " ") or b.startswith(a + " "))


# ---- ledger views ----

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


class _Library:
    """What the artist's files say about each release group: their folders and their (title, duration)."""

    def __init__(self, conn: sqlite3.Connection, artist_id: int, release_id: int | None):
        rels = conn.execute(f"SELECT rel.id, rel.title, rel.version, COALESCE(rel.group_key, 'release:' || rel.id) AS gk "
                            f"FROM release rel WHERE {_REL_FILTER}", (artist_id, release_id)).fetchall()
        self.group_of = {r["id"]: r["gk"] for r in rels}
        name_to_groups: dict[str, set[str]] = {}
        for r in rels:
            name_to_groups.setdefault(_name_key(r["title"], r["version"]), set()).add(r["gk"])
        rec_groups: dict[int, set[str]] = {}
        for t in conn.execute(f"SELECT t.recording_id AS rid, rel.id AS relid FROM track t JOIN release rel "
                              f"ON rel.id = t.release_id WHERE {_REL_FILTER}", (artist_id, release_id)):
            rec_groups.setdefault(t["rid"], set()).add(self.group_of[t["relid"]])
        self.dirs: dict[str, Counter] = {}
        self.files: dict[str, list[tuple[str, float, str]]] = {}
        for f in conn.execute("SELECT path, recording_id, tag_title, duration_s FROM file WHERE artist_id = ?", (artist_id,)):
            if "/" not in f["path"]:
                continue
            folder = f["path"].rsplit("/", 1)[0]
            key = _name_key(folder.rsplit("/", 1)[-1])
            # a folder named like a known group belongs to that group; an oddly named one to the groups it is linked to
            groups = name_to_groups.get(key) or rec_groups.get(f["recording_id"], set())
            for g in groups:
                self.dirs.setdefault(g, Counter())[folder] += 1
                if f["tag_title"] and f["duration_s"] is not None:
                    self.files.setdefault(g, []).append((norm_title(f["tag_title"]), f["duration_s"], f["path"]))

    def folder(self, group: str) -> str | None:
        votes = self.dirs.get(group)
        return max(sorted(votes), key=votes.__getitem__) if votes else None

    def owned_under_another_title(self, group: str, title: str, duration_s: int | None) -> str | None:
        if duration_s is None:
            return None
        for nt, dur, path in self.files.get(group, ()):
            if abs(dur - duration_s) <= SAME_DURATION_S and _related(nt, title):
                return path
        return None


def plan_missing(conn: sqlite3.Connection, artist_id: int | None = None, release_id: int | None = None,
                 include_variants: bool = False, ext: str = ".flac", scope: str = "partial",
                 skipped: list | None = None) -> list[FetchItem]:
    """Ledger ids in, items out. `release_id` limits the plan to the clusters on that release (an explicit request,
    so `scope` does not apply; the chosen track may still sit on another edition). Only artists with tidal_match
    auto/manual are planned. Near-duplicates of owned files are left out and appended to `skipped` as
    (FetchItem, owned path) when a list is given."""
    if scope not in ("partial", "all"):
        raise ValueError("scope must be 'partial' or 'all'")
    owned = _owned_clusters(conn)
    only: set[int] | None = None
    if release_id is not None:
        rel = conn.execute("SELECT artist_id FROM release WHERE id = ?", (release_id,)).fetchone()
        if rel is None:
            return []
        artist_id = rel["artist_id"]
        only = {r[0] for r in conn.execute(
            "SELECT DISTINCT COALESCE(rec.canon_id, rec.id) FROM track t JOIN recording rec "
            "ON rec.id = t.recording_id WHERE t.release_id = ?", (release_id,))}
    artists = conn.execute("SELECT * FROM artist WHERE tidal_match IN ('auto', 'manual') AND tidal_id IS NOT NULL "
                           "AND (?1 IS NULL OR id = ?1) ORDER BY norm_name", (artist_id,)).fetchall()
    items: list[FetchItem] = []
    for a in artists:
        lib = _Library(conn, a["id"], release_id)
        rows = [r for r in conn.execute(_TRACKS, (a["id"], release_id))]
        discs = {r[0]: r[1] for r in conn.execute(  # every track of the release counts, fetchable or not
            "SELECT t.release_id, COUNT(DISTINCT t.disc) FROM track t JOIN release rel ON rel.id = t.release_id "
            "WHERE rel.artist_id = ? GROUP BY t.release_id", (a["id"],))}
        in_group: dict[str, set[int]] = {}
        for r in rows:
            in_group.setdefault(lib.group_of[r["release_id"]], set()).add(r["cid"])
        partial = {g for g, cids in in_group.items() if cids & owned} | set(lib.dirs)
        clusters: dict[int, list] = {}
        for r in rows:
            g = lib.group_of[r["release_id"]]
            if r["cid"] in owned or (only is not None and r["cid"] not in only):
                continue
            if only is None and scope == "partial" and g not in partial:
                continue
            clusters.setdefault(r["cid"], []).append(r)
        picked = sorted((_pick(rs) for rs in clusters.values()),
                        key=lambda r: (r["release_date"] or _NO_DATE, r["release_id"], r["disc"], r["number"]))
        for r in picked:
            if r["variant"] is not None and not include_variants:
                continue
            g = lib.group_of[r["release_id"]]
            folder = lib.folder(g)
            album = folder.rsplit("/", 1)[-1] if folder else strip_markers(r["release_title"], release=True)
            item = FetchItem(
                a["id"], a["name"], r["release_id"], int(r["tidal_release"]) if r["tidal_release"] else None,
                album, folder or naming.album_dir(a["name"], album), r["disc"], r["number"],
                display_title(r["title"], r["version"]), r["tidal_track_id"], r["rec_id"],
                r["t_dur"] if r["t_dur"] is not None else r["r_dur"], r["isrc"], r["variant"],
                _dest(conn, a, r["release_id"]), "", discs.get(r["release_id"], 1))
            item = replace(item, target_rel=item.target(ext))
            twin = lib.owned_under_another_title(g, norm_title(r["title"], r["version"]), item.duration_s)
            if twin:
                if skipped is not None:
                    skipped.append((item, twin))
                continue
            items.append(item)
    return items
