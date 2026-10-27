"""What to fetch: one item per unowned recording cluster, using the canonical recording's Tidal track.

Reads the ledger only. A cluster (`COALESCE(canon_id, id)`, set by `cadence regroup` with the latest-remaster rule,
identity/grouping.py) is owned when at least one `file` row has `recording_id` set to a recording in it, library-wide.
Nothing else counts: `cadence link` is the only identity resolver, so a file it did not link (including one it put
in `link_review` as ambiguous) owns nothing here. The planner never matches files by tags, names, durations or
folders. The policy is duplicate-or-conflict: a track with only an unlinked file is planned, and if its target path is
taken the executor (run.py) reports `conflict`; the planner does not look at the disk. The one path check it makes is
on the ledger: a planned target equal to the path of a file linked to another recording stays planned and is listed
in `Plan.collisions`, so the plan says up front what the executor will report as a conflict. Never skipped, never overwritten.

Every recording cluster of the artist ends up in exactly one bucket of the returned `Plan`: owned (counted), planned
(`items`) or skipped with a reason (`skipped`); none disappears silently. A cluster is counted once however many
editions carry it, but `owned` is counted per artist that carries the cluster, not per cluster. One shared by several artists is planned under one only (the recording row's artist, else the first);
the others skip it with R_SHARED.

Scope (default 'partial'): only release groups where something is already owned: a linked file whose recording is
itself a track of the release (not merely in the same cluster as one, which would pull in a box set). The album
folder is the release title without remaster/edition markers."""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field, replace

from cadence.identity.normalize import _GROUP_RE, _classify, _peel_suffixes
from cadence.ledger import naming

_TYPE_RANK = {"album": 0, "ep": 1, "single": 2, "compilation": 3, "other": 4}
_NO_DATE = "9999-99-99"
_MARKERS = {"remaster", "deluxe", "edition"}

# why a recording cluster was not planned (printed next to the track and counted in the summary)
R_PARTIAL = "partial scope: release has no owned track"
R_VARIANT = "variant (use --include-variants)"
R_UNAVAILABLE = "unavailable on Tidal"
R_NO_TIDAL_ID = "no tidal id"
R_HIDDEN = "hidden release"
R_APPEARS_ON = "appears-on release (name it with --release)"
R_NOT_IN_RELEASE = "release not in --release"
R_ARTIST_UNMATCHED = "artist not matched to Tidal (set it with cadence artist set-tidal)"
R_SHARED = "shared recording, planned under"  # followed by the artist's name

# every track of the artist, whatever its release's role or visibility: the classification happens in Python
_ROWS = (
    "SELECT t.tidal_id AS tidal_track_id, t.available, t.disc, t.number, t.title, t.version, t.duration_s AS t_dur, "
    "rec.id AS rec_id, COALESCE(rec.canon_id, rec.id) AS cid, rec.variant, rec.isrc, rec.duration_s AS r_dur, "
    "rel.id AS release_id, rel.title AS release_title, rel.type, rel.release_date, rel.role, rel.hidden_reason, "
    "COALESCE(rel.group_key, 'release:' || rel.id) AS gk, "
    "(SELECT s.provider_ref FROM release_source s WHERE s.release_id = rel.id AND s.provider = 'tidal') AS tidal_release "
    "FROM track t JOIN release rel ON rel.id = t.release_id JOIN recording rec ON rec.id = t.recording_id "
    "WHERE rel.artist_id = ?")


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


@dataclass(frozen=True)
class Skip:
    """A recording cluster that was not planned, shown as one of its tracks (the one that would have been chosen)."""
    artist: str
    release: str
    disc: int
    number: int
    title: str
    tidal_track_id: int | None
    recording_id: int
    reason: str

    def line(self) -> str:
        pos = (f"D{self.disc}-" if self.disc != 1 else "") + f"{self.number:02d}"
        tid = self.tidal_track_id if self.tidal_track_id is not None else "-"
        return f"  {'skip':<16} {self.artist} / {self.release} / {pos} - {self.title}  [tidal {tid}]  ({self.reason})"


@dataclass(frozen=True)
class Collision:
    """A planned item whose target path is already spoken for. `kind` says by what:
    'linked': the path of a file the ledger links to a different recording (`file_path` is that file's path);
    'duplicate': the target of an earlier planned item of another recording (`file_path` is that item's target, and
    `tidal_track_id` is the later item's: the one the executor reports as a conflict, and the dry run too). Paths
    compare case-insensitively in both kinds. In both kinds `file_path` equals `target`."""
    tidal_track_id: int
    target: str
    file_path: str
    kind: str = "linked"       # 'linked' | 'duplicate'

    def line(self, fiio: bool = False) -> str:
        what = ("duplicate target, another planned track has the same path" if self.kind == "duplicate"
                else "file already linked to another recording")
        return (f"  collision  {self.target}  planned tidal {self.tidal_track_id}, {what}  "
                + ("(fiio-only, checked in dry run and at execute)" if fiio else "(may conflict at execute)"))


@dataclass
class Plan:
    """Where every recording cluster of the planned artists went: `items` (to fetch), `owned` (a linked file exists;
    only counted), `skipped` (each with a reason). planned + owned + skipped covers each cluster exactly once.
    `collisions` annotates items (they stay in `items`), sorted by target, Tidal id, kind."""
    items: list[FetchItem] = field(default_factory=list)
    owned: int = 0
    skipped: list[Skip] = field(default_factory=list)
    collisions: list[Collision] = field(default_factory=list)

    def skip_counts(self) -> list[tuple[str, int]]:
        return sorted(Counter(s.reason for s in self.skipped).items(), key=lambda kv: (-kv[1], kv[0]))

    def summary_lines(self) -> list[str]:
        n = len({c.tidal_track_id for c in self.collisions})  # an item with two collisions counts once
        return [f"plan: {len(self.items)} planned, {self.owned} owned, {len(self.skipped)} skipped"
                + (f", {n} collision{'s' * (n != 1)}" if n else ""),
                *(f"  {n:>6} skipped: {reason}" for reason, n in self.skip_counts())]


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


def _preferred_rows(rows: list) -> list:
    """Rows `_pick` chooses from: the non-variant ones (all rows if there are none), own-visible-release rows first."""
    rows = [r for r in rows if r["variant"] is None] or rows
    return [r for r in rows if _own_visible(r)] or rows


def _own_visible(r) -> bool:
    return r["role"] == "own" and r["hidden_reason"] is None


def plan_missing(conn: sqlite3.Connection, artist_id: int | None = None, release_id: int | None = None,
                 include_variants: bool = False, ext: str = ".flac", scope: str = "partial") -> Plan:
    """Ledger ids in, a `Plan` out. `release_id` limits the plan to the clusters on that release (an explicit request,
    so `scope` does not apply; the chosen track may still sit on another edition). Only artists with tidal_match
    auto/manual are planned; the clusters of any other artist are owned or skipped (R_ARTIST_UNMATCHED).
    Ownership is the ledger's links and nothing else (see the module docstring)."""
    if scope not in ("partial", "all"):
        raise ValueError("scope must be 'partial' or 'all'")
    owned = _owned_clusters(conn)
    linked_recs = {r[0] for r in conn.execute("SELECT DISTINCT recording_id FROM file WHERE recording_id IS NOT NULL")}
    only: set[int] | None = None
    if release_id is not None:
        rel = conn.execute("SELECT artist_id FROM release WHERE id = ?", (release_id,)).fetchone()
        if rel is None:
            return Plan()
        artist_id = rel["artist_id"]
        only = {r[0] for r in conn.execute(
            "SELECT DISTINCT COALESCE(rec.canon_id, rec.id) FROM track t JOIN recording rec "
            "ON rec.id = t.recording_id WHERE t.release_id = ?", (release_id,))}
    artists = conn.execute("SELECT * FROM artist WHERE (?1 IS NULL OR id = ?1) ORDER BY norm_name", (artist_id,)).fetchall()
    plan = Plan()
    claims: dict[int, list[FetchItem]] = {}  # cluster -> the items that would plan it, one per artist, in plan order

    def visible(r) -> bool:  # a row of an own, unhidden release, or of the release asked for
        return _own_visible(r) or r["release_id"] == release_id

    for a in artists:
        discs = {r[0]: r[1] for r in conn.execute(  # Tidal's volume count (else 1) or the top disc of any row
            "SELECT rel.id, MAX(CASE WHEN rel.num_volumes >= 1 THEN rel.num_volumes ELSE 1 END, "
            "COALESCE((SELECT MAX(t.disc) FROM track t WHERE t.release_id = rel.id), 1)) "
            "FROM release rel WHERE rel.artist_id = ?", (a["id"],))}
        rows = conn.execute(_ROWS, (a["id"],)).fetchall()

        by_cluster: dict[int, list] = {}
        for r in rows:
            by_cluster.setdefault(r["cid"], []).append(r)
        partial = {r["gk"] for r in rows if visible(r) and r["rec_id"] in linked_recs}  # groups with a linked own track
        picked: list[tuple] = []  # (row, FetchItem | Skip), sorted below by the row

        def skip(rs: list, reason: str) -> None:
            r = _pick(rs)
            picked.append((r, Skip(a["name"], r["release_title"], r["disc"], r["number"],
                                   display_title(r["title"], r["version"]), r["tidal_track_id"], r["rec_id"], reason)))

        for cid, rs in by_cluster.items():
            if cid in owned:
                plan.owned += 1
                continue
            if a["tidal_match"] not in ("auto", "manual") or a["tidal_id"] is None:
                skip(rs, R_ARTIST_UNMATCHED)
                continue
            if only is not None and cid not in only:
                skip(rs, R_NOT_IN_RELEASE)
                continue
            with_id = [r for r in rs if r["tidal_track_id"] is not None]
            usable = [r for r in with_id if r["available"] != 0]
            if not usable:
                skip(with_id or rs, R_UNAVAILABLE if with_id else R_NO_TIDAL_ID)
                continue
            shown = [r for r in usable if visible(r)]
            if not shown:
                hidden = [r for r in usable if r["hidden_reason"] is not None]
                skip(hidden or usable, R_HIDDEN if hidden else R_APPEARS_ON)
                continue
            if only is None and scope == "partial":
                in_scope = [r for r in shown if r["gk"] in partial]
                if not in_scope:
                    skip(shown, R_PARTIAL)
                    continue
                shown = in_scope
            r = _pick(_preferred_rows(shown))
            if r["variant"] is not None and not include_variants:
                skip([r], R_VARIANT)
                continue
            album = strip_markers(r["release_title"], release=True)
            item = FetchItem(
                a["id"], a["name"], r["release_id"], int(r["tidal_release"]) if r["tidal_release"] else None,
                album, naming.album_dir(a["name"], album), r["disc"], r["number"],
                display_title(r["title"], r["version"]), r["tidal_track_id"], r["rec_id"],
                r["t_dur"] if r["t_dur"] is not None else r["r_dur"], r["isrc"], r["variant"],
                _dest(conn, a, r["release_id"]), "", discs.get(r["release_id"], 1))
            picked.append((r, replace(item, target_rel=item.target(ext))))
            claims.setdefault(cid, []).append(picked[-1][1])
        picked.sort(key=lambda p: (p[0]["release_date"] or _NO_DATE, p[0]["release_id"], p[0]["disc"], p[0]["number"]))
        for _, x in picked:
            (plan.skipped if isinstance(x, Skip) else plan.items).append(x)
    dropped: set[int] = set()  # a recording shared by artists is planned under one: the recording's, else the first
    for cid, its in claims.items():
        if len(its) > 1:
            home = conn.execute("SELECT artist_id FROM recording WHERE id = ?", (cid,)).fetchone()[0]
            keep = next((i for i in its if i.artist_id == home), its[0])
            for i in its:
                if i is not keep:
                    dropped.add(id(i))
                    plan.skipped.append(Skip(i.artist, i.release, i.disc, i.number, i.title, i.tidal_track_id,
                                             i.recording_id, f"{R_SHARED} {keep.artist}"))
    plan.items = [i for i in plan.items if id(i) not in dropped]
    linked = {r[0].casefold() for r in conn.execute("SELECT path FROM file WHERE recording_id IS NOT NULL")}
    first: dict[str, str] = {}  # casefolded target (the library is case-insensitive on macOS) -> the first item's
    dups = []
    for i in plan.items:  # items sharing a target: the first keeps it, each later one is listed
        if (key := i.target_rel.casefold()) in first:
            dups.append(Collision(i.tidal_track_id, first[key], first[key], "duplicate"))
        first.setdefault(key, i.target_rel)
    plan.collisions = sorted([*(Collision(i.tidal_track_id, i.target_rel, i.target_rel) for i in plan.items
                                if i.target_rel.casefold() in linked), *dups], key=lambda c: (c.target, c.tidal_track_id, c.kind))
    return plan
