"""Read-only reports over the ledger: what is owned, what is missing.

Counting is done on release groups (editions of one release) and recording clusters (one song across
editions); see catalog/groups.py. A cluster is owned if a file is linked to any recording in it."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

_NO_DATE = "9999-99-99"
_OWN_RELEASES = "artist_id = ? AND role = 'own' AND hidden_reason IS NULL"
_CLUSTER = "COALESCE(rec.canon_id, rec.id)"  # an ungrouped recording is its own cluster


@dataclass
class _Edition:
    id: int
    title: str
    type: str
    date: str | None
    clusters: set[int] = field(default_factory=set)


@dataclass
class _Own:
    """An artist's own, non-hidden releases: editions per group, and what is known about each cluster."""
    groups: dict[str, list[_Edition]]
    variant: dict[int, str | None]       # cluster id -> variant label (None = main version)
    on_releases: dict[int, list[str]]    # cluster id -> titles of the releases it is on

    @property
    def clusters(self) -> set[int]:
        return set(self.variant)


def _state(owned: int, total: int) -> str:
    if total == 0:
        return "unknown"  # no tracks fetched for this release: can't say it's missing
    if owned == total:
        return "owned"
    return "partial" if owned else "missing"


def _files_by_cluster(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """Paths of every linked file, per cluster, library-wide (ownership does not stop at one artist)."""
    out: dict[int, list[str]] = {}
    for r in conn.execute(f"SELECT {_CLUSTER} AS cid, f.path FROM file f JOIN recording rec "
                          "ON rec.id = f.recording_id ORDER BY cid, f.path"):
        out.setdefault(r["cid"], []).append(r["path"])
    return out


def _load_own(conn: sqlite3.Connection, artist_id: int) -> _Own:
    groups: dict[str, list[_Edition]] = {}
    editions: dict[int, _Edition] = {}
    for r in conn.execute("SELECT id, title, type, release_date, COALESCE(group_key, 'release:' || id) AS gkey "
                          f"FROM release WHERE {_OWN_RELEASES} ORDER BY release_date IS NULL, release_date, id",
                          (artist_id,)):
        editions[r["id"]] = _Edition(r["id"], r["title"], r["type"], r["release_date"])
        groups.setdefault(r["gkey"], []).append(editions[r["id"]])
    own = _Own(groups, {}, {})
    for t in conn.execute(f"SELECT t.release_id, {_CLUSTER} AS cid, rec.variant FROM track t "
                          "JOIN recording rec ON rec.id = t.recording_id WHERE t.release_id IN "
                          f"(SELECT id FROM release WHERE {_OWN_RELEASES}) ORDER BY t.release_id, t.id",
                          (artist_id,)):
        ed = editions[t["release_id"]]
        ed.clusters.add(t["cid"])
        own.variant.setdefault(t["cid"], t["variant"])
        titles = own.on_releases.setdefault(t["cid"], [])
        if ed.title not in titles:
            titles.append(ed.title)
    return own


def _edition_rank(e: _Edition, owned: set[int]) -> tuple:
    """Best edition: most owned clusters, then the standard (smallest) one, then the earliest."""
    return (-len(e.clusters & owned), not e.clusters, len(e.clusters), e.date or _NO_DATE, e.id)


def _release_rows(own: _Own, owned: set[int]) -> list[dict]:
    ranked = []
    for eds in own.groups.values():
        best = min(eds, key=lambda e: _edition_rank(e, owned))
        others = set().union(*(e.clusters for e in eds if e is not best)) - best.clusters
        n = len(best.clusters & owned)
        ranked.append(((best.date is None, best.date or "", best.id),
                       {"title": best.title, "type": best.type, "date": best.date,
                        "total": len(best.clusters), "owned": n, "state": _state(n, len(best.clusters)),
                        "editions": len(eds), "extras_missing": len(others - owned)}))
    return [row for _, row in sorted(ranked, key=lambda x: x[0])]


def _counts(own: _Own, owned: set[int]) -> dict:
    clusters = own.clusters
    missing = clusters - owned
    variants = sum(1 for c in missing if own.variant[c] is not None)
    return {"recordings_total": len(clusters), "recordings_owned": len(clusters & owned),
            "missing_main": len(missing) - variants, "missing_variant": variants}


def artist_report(conn: sqlite3.Connection, artist_id: int) -> dict:
    artist = conn.execute("SELECT * FROM artist WHERE id = ?", (artist_id,)).fetchone()
    files = _files_by_cluster(conn)
    owned = set(files)
    own = _load_own(conn, artist_id)

    missing = []
    for cid in sorted(own.clusters - owned):
        rec = conn.execute("SELECT title, isrc, duration_s FROM recording WHERE id = ?", (cid,)).fetchone()
        missing.append({"recording_id": cid, "releases": own.on_releases[cid], "title": rec["title"],
                        "isrc": rec["isrc"], "duration_s": rec["duration_s"], "variant": own.variant[cid]})

    unmatched = [dict(r) for r in conn.execute(
        "SELECT path, tag_title AS title, tag_album AS album FROM file "
        "WHERE artist_id = ? AND match = 'unmatched' ORDER BY path", (artist_id,))]
    scope = [r[0] for r in conn.execute(
        f"SELECT DISTINCT {_CLUSTER} FROM recording rec WHERE rec.artist_id = ?1 OR rec.id IN "
        "(SELECT t.recording_id FROM track t JOIN release rel ON rel.id = t.release_id "
        "WHERE rel.artist_id = ?1) ORDER BY 1", (artist_id,))]
    dups = [{"recording_id": cid, "paths": files[cid],
             "title": conn.execute("SELECT title FROM recording WHERE id = ?", (cid,)).fetchone()[0]}
            for cid in scope if len(files.get(cid, ())) > 1]
    appears_on = conn.execute("SELECT COUNT(*) FROM release WHERE artist_id = ? AND role = 'appears_on'",
                              (artist_id,)).fetchone()[0]
    return {"artist_id": artist_id, "artist": artist["name"], "tidal_id": artist["tidal_id"],
            "tidal_match": artist["tidal_match"], "releases": _release_rows(own, owned),
            "release_editions": sum(len(eds) for eds in own.groups.values()),
            **_counts(own, owned), "missing_recordings": missing,
            "unmatched_files": unmatched, "duplicates": dups, "appears_on": appears_on}


def library_summary(conn: sqlite3.Connection) -> list[dict]:
    owned = set(_files_by_cluster(conn))
    rows = []
    for a in conn.execute("SELECT id, name, tidal_match FROM artist ORDER BY norm_name").fetchall():
        own = _load_own(conn, a["id"])
        c = _counts(own, owned)
        unmatched = conn.execute("SELECT COUNT(*) FROM file WHERE artist_id = ? AND match = 'unmatched'",
                                 (a["id"],)).fetchone()[0]
        rows.append({"artist_id": a["id"], "artist": a["name"], "tidal_match": a["tidal_match"],
                     "own_releases": len(own.groups), "own_editions": sum(len(e) for e in own.groups.values()),
                     "recordings_owned": c["recordings_owned"], "recordings_total": c["recordings_total"],
                     "missing": c["recordings_total"] - c["recordings_owned"],
                     "missing_main": c["missing_main"], "missing_variant": c["missing_variant"],
                     "unmatched_files": unmatched})
    return rows


def _release_line(r: dict) -> str:
    extra = ""
    if r["editions"] > 1:
        more = f", {r['extras_missing']} extra missing" if r["extras_missing"] else ""
        extra = f"  (+{r['editions'] - 1} ed.{more})"
    return (f"    {r['date'] or '????-??-??'}  {r['type']:<11} {r['state']:<8} "
            f"{r['owned']}/{r['total']}  {r['title']}{extra}")


def format_artist_report(d: dict) -> str:
    variants = f" (+{d['missing_variant']} variants not owned)" if d["missing_variant"] else ""
    lines = [f"{d['artist']}  [tidal: {d['tidal_match']}"
             + (f" {d['tidal_id']}" if d["tidal_id"] else "") + "]",
             f"  recordings owned {d['recordings_owned']}/{d['recordings_total']}{variants}, "
             f"appears on {d['appears_on']} other release(s)"]
    lines.append("  releases:")
    lines += [_release_line(r) for r in d["releases"]]
    for label, wanted in (("missing recordings", None), ("missing variants", True)):
        ms = [m for m in d["missing_recordings"] if (m["variant"] is not None) == bool(wanted)]
        if ms:
            lines.append(f"  {label} ({len(ms)}):")
            lines += [f"    {m['title']}  ({', '.join(m['releases'])})" for m in ms]
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
    lines = [f"{'artist':<{w}}  {'tidal':<9} {'rel':>4} {'owned':>11} {'missing':>7} {'+var':>5} {'unmatched':>9}"]
    for r in rows:
        lines.append(f"{r['artist'][:w]:<{w}}  {r['tidal_match']:<9} {r['own_releases']:>4} "
                     f"{r['recordings_owned']:>5}/{r['recordings_total']:<5} {r['missing_main']:>7} "
                     f"{r['missing_variant']:>5} {r['unmatched_files']:>9}")
    return "\n".join(lines)
