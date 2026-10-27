"""What to fetch: one item per unowned recording cluster, using the canonical recording's Tidal track.

Read-only over the ledger. A cluster is owned when any file is linked to a recording in it (library-wide), the
same rule report.py uses. The canonical recording is `COALESCE(canon_id, id)`, which `cadence regroup` sets
with the latest-remaster rule (identity/grouping.py).

Scope (default 'partial'): only release groups where something is already owned (a linked cluster, or a folder
in the library that carries the group's name). Folder naming: an existing library folder of the same release
group is reused as is; otherwise the album title without remaster/edition markers is used. Near-duplicates of
files the user already has (same release group, similar title, duration within 3 s) are skipped and reported.
A linked file is a near-duplicate only of a related but different title, never of the identical one."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from typing import NamedTuple

from cadence.identity.normalize import _GROUP_RE, _classify, _peel_suffixes, norm_title, split_title
from cadence.ledger import naming

_TYPE_RANK = {"album": 0, "ep": 1, "single": 2, "compilation": 3, "other": 4}
_NO_DATE = "9999-99-99"
_MARKERS = {"remaster", "deluxe", "edition"}
SAME_DURATION_S = 3
_NUMBERED_NAME = re.compile(r"^\s*(\d+)\s*-\s*(.+)$")  # 'NN - Title' (extension already removed)

_REL_FILTER = "rel.artist_id = ?1 AND ((rel.role = 'own' AND rel.hidden_reason IS NULL) OR rel.id = ?2)"
_ALL_TRACKS = (
    "SELECT t.tidal_id AS tidal_track_id, t.disc, t.number, t.title, t.version, t.duration_s AS t_dur, "
    "rec.id AS rec_id, COALESCE(rec.canon_id, rec.id) AS cid, rec.variant, rec.isrc, rec.duration_s AS r_dur, "
    "rel.id AS release_id, rel.title AS release_title, rel.type, rel.release_date, "
    "(SELECT s.provider_ref FROM release_source s WHERE s.release_id = rel.id AND s.provider = 'tidal') AS tidal_release "
    "FROM track t JOIN release rel ON rel.id = t.release_id JOIN recording rec ON rec.id = t.recording_id "
    f"WHERE {_REL_FILTER}")
_TRACKS = _ALL_TRACKS + " AND t.tidal_id IS NOT NULL AND t.available IS NOT 0"


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


class _Track(NamedTuple):
    disc: int
    number: int
    title: str                 # norm_title of title plus version
    duration_s: int | None
    multi_disc: bool
    cid: int
    exact: str                 # what the legacy rule compares: `title`, or the raw title when that normalises to nothing


def _exact(title: str, version: str | None = None) -> str:
    """norm_title, or for a title that is only symbols ('?') the lowercased raw title, so it still matches itself."""
    return norm_title(title, version) or title.strip().lower()


def _track(r: sqlite3.Row, discs: dict) -> _Track:
    return _Track(r["disc"], r["number"], norm_title(r["title"], r["version"]),
                  r["t_dur"] if r["t_dur"] is not None else r["r_dur"], discs.get(r["release_id"], 1) > 1, r["cid"],
                  _exact(r["title"], r["version"]))


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


def _candidates(rows: list, own: dict) -> list:
    """Rows `_pick` chooses from: the non-variant ones (all rows if there are none), own-release rows first."""
    rows = [r for r in rows if r["variant"] is None] or rows
    return [r for r in rows if r["release_id"] in own] or rows


def _title_and_number(path: str, tag_title: str | None, tag_track: str | None) -> tuple[set[str], set[int]]:
    """Normalised titles (raw ones for symbol-only titles) and track numbers a file claims, from its tags and from an `NN - Title` file name."""
    name = _NUMBERED_NAME.match(path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    tag_no = re.match(r"\s*(\d+)", tag_track or "")
    titles = {_exact(t) for t in (tag_title, name and name.group(2)) if t}
    numbers = {int(m.group(1)) for m in (name, tag_no) if m}
    return titles, numbers


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
        self.files: dict[str, list[tuple[str, float, str, int | None]]] = {}  # (title, duration, path, cluster)
        self.unlinked: dict[str, list[tuple[set[str], set[int], str]]] = {}  # (titles, track numbers, path)
        for f in conn.execute("SELECT f.path, f.recording_id, f.tag_title, f.tag_track, f.duration_s, "
                              "COALESCE(rec.canon_id, rec.id) AS cid FROM file f "
                              "LEFT JOIN recording rec ON rec.id = f.recording_id WHERE f.artist_id = ?", (artist_id,)):
            if "/" not in f["path"]:
                continue
            folder = f["path"].rsplit("/", 1)[0]
            key = _name_key(folder.rsplit("/", 1)[-1])
            # a folder named like a known group belongs to that group; an oddly named one to the groups it is linked to
            groups = name_to_groups.get(key) or rec_groups.get(f["recording_id"], set())
            for g in groups:
                self.dirs.setdefault(g, Counter())[folder] += 1
                if f["tag_title"] and f["duration_s"] is not None:
                    self.files.setdefault(g, []).append((norm_title(f["tag_title"]), f["duration_s"], f["path"], f["cid"]))
                if f["recording_id"] is None:
                    titles, numbers = _title_and_number(f["path"], f["tag_title"], f["tag_track"])
                    self.unlinked.setdefault(g, []).append((titles, numbers, f["path"]))

    def folder(self, group: str) -> str | None:
        votes = self.dirs.get(group)
        return max(sorted(votes), key=votes.__getitem__) if votes else None

    def fits(self, group: str, t: _Track) -> list[str]:
        """Paths of the group's files that could be track `t`, by duration or by the legacy rule. A file linked to
        t's own cluster is its owner, not a match, and a linked file with the identical title is another track of the
        same name (an owned `Intro` does not stand in for an unowned `Intro`), so only unlinked files match on that."""
        paths = []
        if t.duration_s is not None:
            paths += [p for nt, dur, p, cid in self.files.get(group, ())
                      if cid != t.cid and abs(dur - t.duration_s) <= SAME_DURATION_S and _related(nt, t.title)
                      and (cid is None or nt != t.title)]
        if t.multi_disc:
            # Legacy files on a multi-disc album are named `NN - Title` with no disc prefix, and their length can
            # differ from Tidal's by more than the tolerance (other master), so the duration rule misses them and
            # the track would be fetched again as `D1-NN - Title`. The same title (exactly: a live or other variant
            # is another recording) and the same track number is enough here. Only unlinked files count: a linked one
            # already owns its own track (disc 2 may repeat title and number).
            paths += [p for titles, numbers, p in self.unlinked.get(group, ()) if t.number in numbers and t.exact and t.exact in titles]
        return list(dict.fromkeys(paths))

    def fits_in(self, groups: set[str], t: _Track) -> list[str]:
        """`fits` over every group the cluster has a track in: a file sits in each group its folder name matches."""
        return list(dict.fromkeys(p for g in sorted(groups) for p in self.fits(g, t)))

    def claims(self, groups: set[str], ts: list[_Track]) -> list[str]:
        """Paths of the files that fit any of one cluster's tracks `ts`, available or not."""
        return list(dict.fromkeys(p for t in ts for p in self.fits_in(groups, t)))

    def candidates(self, tracks: dict[int, list[_Track]], groups: dict[int, set[str]]) -> dict[str, set[int]]:
        """For each file, the clusters it could be (through any of their tracks), whatever release group they are in."""
        out: dict[str, set[int]] = {}
        for cid, ts in tracks.items():
            for p in self.claims(groups[cid], ts):
                out.setdefault(p, set()).add(cid)
        return out

    def owned_under_another_title(self, groups: set[str], ts: list[_Track], cands: dict[str, set[int]]) -> str | None:
        """The file that is the cluster of `ts` under another name. A file that could be another cluster too cannot
        tell which one it is, so it claims none."""
        return next((p for p in self.claims(groups, ts) if cands.get(p, set()) <= {ts[0].cid}), None)


def plan_missing(conn: sqlite3.Connection, artist_id: int | None = None, release_id: int | None = None,
                 include_variants: bool = False, ext: str = ".flac", scope: str = "partial",
                 skipped: list | None = None) -> list[FetchItem]:
    """Ledger ids in, items out. `release_id` limits the plan to the clusters on that release (an explicit request,
    so `scope` does not apply; the chosen track may still sit on another edition). Only artists with tidal_match
    auto/manual are planned. Near-duplicates of owned files are left out and appended to `skipped` as
    (FetchItem, owned path) when a list is given. What a file claims is worked out over the artist's own releases
    only, whatever `release_id` or `scope` say: they choose what is planned, never what is owned."""
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
        claim_lib = _Library(conn, a["id"], None) if release_id is not None else lib  # not widened by the request
        rows = [r for r in conn.execute(_TRACKS, (a["id"], release_id))]
        discs = {r[0]: r[1] for r in conn.execute(  # Tidal's volume count (else 1) or the top disc of any row
            "SELECT rel.id, MAX(CASE WHEN rel.num_volumes >= 1 THEN rel.num_volumes ELSE 1 END, "
            "COALESCE((SELECT MAX(t.disc) FROM track t WHERE t.release_id = rel.id), 1)) "
            "FROM release rel WHERE rel.artist_id = ?", (a["id"],))}
        by_cluster: dict[int, list] = {}  # every non-variant track of each cluster, owned or not, in scope or not
        cluster_groups: dict[int, set[str]] = {}
        for r in conn.execute(_ALL_TRACKS, (a["id"], None)):
            cluster_groups.setdefault(r["cid"], set()).add(claim_lib.group_of[r["release_id"]])
            if r["variant"] is None:
                by_cluster.setdefault(r["cid"], []).append(r)
        # every track of a cluster can claim a file, the representative (chosen over all its rows) first
        tracks = {cid: [_track(_pick(rs), discs), *(_track(r, discs) for r in rs)] for cid, rs in by_cluster.items()}
        cands = claim_lib.candidates(tracks, cluster_groups)
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
        # a row of one of the artist's own releases is preferred, so asking for an appears-on release (where the file
        # may sit) does not move the target the default run would give
        # and a non-variant row is preferred to any variant one, so a variant on an own release cannot hide it
        picked = sorted((_pick(_candidates(rs, claim_lib.group_of)) for rs in clusters.values()),
                        key=lambda r: (r["release_date"] or _NO_DATE, r["release_id"], r["disc"], r["number"]))
        planned = [r for r in picked if r["variant"] is None or include_variants]
        for r in planned:
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
            ts = tracks.get(r["cid"]) or [_track(r, discs)]
            if r["cid"] in cluster_groups:
                twin = claim_lib.owned_under_another_title(cluster_groups[r["cid"]], ts, cands)
            else:  # only on an appears-on release: no own group, so the file in that release's own folder claims it
                twin = next((p for p in lib.claims({g}, ts) if not cands.get(p)), None)  # unless an own cluster claims it
            if twin:
                if skipped is not None:
                    skipped.append((item, twin))
                continue
            items.append(item)
    return items
