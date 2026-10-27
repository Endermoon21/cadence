"""Random small ledgers for the invariant tests (tests/test_plan_invariants.py).

`build(seed)` makes a throw-away in-memory SQLite ledger at the current migration head (db.connect + db.migrate, the
same way every other test does it). All randomness comes from `random.Random(seed)` and nothing iterates a set, so a
seed always gives the same ledger. Titles, durations and folder names come from small pools on purpose, so ties and
near-collisions (two recordings with the same normalised title and a duration within the linker's tolerance, '?' and
'!' both normalising to '', a linked file sitting at the path the planner would use for another recording) happen
often. The generator never touches cadence/ code other than the schema helpers, naming and normalisation it needs
to look like real data; it does not call the planner or the linker.
"""
from __future__ import annotations

import itertools
import json
import random
import sqlite3
from dataclasses import dataclass, field

from cadence.fetch.plan import strip_markers
from cadence.identity.normalize import norm_title, split_title
from cadence.ledger import db, naming

TITLES = ["Intro", "Intro (Live)", "?", "!", "Song", "Song (Remaster)", "Song (Instrumental)"]
RELEASE_TITLES = ["Alpha", "Alpha (Deluxe Edition)", "Beta", "Beta (2009 Remaster)", "Gamma"]
FOLDERS = ["Misc", "Downloads", "Unknown Album", "Best Of"]
TYPES = ["album", "album", "album", "ep", "single", "compilation", "other"]
DATES = ["1990-01-01", "2005-06-01", "2009-01-01", "2009-01-01", None]
EXTS = [".flac", ".flac", ".flac", ".m4a", ".mp3"]


@dataclass
class GenLedger:
    seed: int
    conn: sqlite3.Connection
    artists: list[int]
    names: dict[int, str]
    releases: dict[int, list[int]]       # artist id -> every release id of that artist (role own and appears_on)
    role: dict[int, str] = field(default_factory=dict)       # release id -> 'own' | 'appears_on'
    manual: list[int] = field(default_factory=list)          # ids of the files seeded with match='manual'


def build(seed: int, path: str = ":memory:") -> GenLedger:
    rng = random.Random(seed)
    conn = db.connect(path)
    db.migrate(conn)
    counter = itertools.count(1)
    n_art = rng.randint(1, 3)
    names = {}
    for i in range(n_art):
        name = f"Artist {i + 1}"
        aid = conn.execute("INSERT INTO artist(name, norm_name, tidal_id, tidal_match) VALUES (?, ?, ?, 'auto')",
                           (name, name.lower(), 900 + i)).lastrowid
        names[aid] = name
    artists = list(names)
    led = GenLedger(seed, conn, artists, names, {a: [] for a in artists})

    pool: dict[int, list[int]] = {a: [] for a in artists}     # recording.artist_id -> recording ids
    rec_info: dict[int, tuple] = {}                           # recording id -> (artist, title, duration, isrc)
    cands: dict[int, set[int]] = {a: set() for a in artists}  # recordings the linker may pick for an artist
    on_tracks: dict[int, list[tuple]] = {}                    # recording id -> (owner, release title, disc, no, title)
    release_titles: dict[int, list[str]] = {a: [] for a in artists}

    def new_rec(a: int) -> int:
        title = rng.choice(TITLES)
        isrc = f"USAAA{next(counter):07d}" if rng.random() < 0.5 else None
        dur = None if rng.random() < 0.05 else rng.randint(200, 204)
        rid = conn.execute(
            "INSERT INTO recording(isrc, artist_id, title, norm_title, variant, duration_s) VALUES (?, ?, ?, ?, ?, ?)",
            (isrc, a, title, norm_title(title), split_title(title).variant, dur)).lastrowid
        pool[a].append(rid)
        cands[a].add(rid)
        rec_info[rid] = (a, title, dur, isrc)
        return rid

    # releases and tracks
    for a in artists:
        for k in range(rng.randint(1, 4)):
            role = "appears_on" if k > 0 and rng.random() < 0.2 else "own"
            title = rng.choice(RELEASE_TITLES)
            rel = conn.execute(
                "INSERT INTO release(artist_id, title, norm_title, type, role, release_date, hidden_reason, group_key, "
                "num_volumes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (a, title, norm_title(title), rng.choice(TYPES), role, rng.choice(DATES),
                 rng.choice(["user", "rule: junk"]) if rng.random() < 0.15 else None,
                 f"g{a}-{rng.randint(1, 2)}" if rng.random() < 0.3 else None,
                 rng.choice([None, 1, 2]))).lastrowid
            led.releases[a].append(rel)
            led.role[rel] = role
            release_titles[a].append(title)
            if rng.random() < 0.9:
                conn.execute("INSERT INTO release_source(release_id, provider, provider_ref) VALUES (?, 'tidal', ?)",
                             (rel, str(next(counter))))
            n_tracks = rng.randint(1, 8)
            discs = sorted(rng.randint(1, 2 if rng.random() < 0.2 else 1) for _ in range(n_tracks))
            numbers: dict[int, int] = {}
            for disc in discs:
                number = numbers[disc] = numbers.get(disc, 0) + 1
                src = a
                if role == "appears_on" and n_art > 1 and rng.random() < 0.6:
                    src = rng.choice([x for x in artists if x != a])
                rid = rng.choice(pool[src]) if pool[src] and rng.random() < 0.45 else new_rec(src)
                cands[a].add(rid)
                rec_title = rec_info[rid][1]
                conn.execute(
                    "INSERT INTO track(release_id, recording_id, tidal_id, disc, number, title, version, duration_s, "
                    "available) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rel, rid, None if rng.random() < 0.12 else 5000 + next(counter), disc, number, rec_title,
                     rng.choice([None, None, None, None, "Live", "Remastered 2011"]),
                     rec_info[rid][2], 0 if rng.random() < 0.15 else 1))
                on_tracks.setdefault(rid, []).append((a, title, disc, number, rec_title))

    # canon_id merges (editions of one recording): mostly inside a family of equal base title and variant
    for a in artists:
        fams: dict[tuple, list[int]] = {}
        for rid in pool[a]:
            p = split_title(rec_info[rid][1])
            fams.setdefault((p.base, p.variant), []).append(rid)
        groups = [m for _, m in sorted(fams.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")) if len(m) > 1
                  and rng.random() < 0.6]
        if len(pool[a]) > 1 and rng.random() < 0.1:
            groups.append(rng.sample(pool[a], 2))
        for members in groups:
            canon = rng.choice(members)
            for m in members:
                if m != canon or rng.random() < 0.5:
                    conn.execute("UPDATE recording SET canon_id = ? WHERE id = ?", (canon, m))

    # library files
    used: set[str] = set()

    def insert_file(path_: str, a: int, rec: int | None, match: str, score: float | None, **tags) -> int | None:
        if path_ in used:
            return None
        used.add(path_)
        return conn.execute(
            "INSERT INTO file(path, artist_id, recording_id, match, match_score, tag_title, tag_album, tag_isrc, "
            "duration_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path_, a, rec, match, score, tags.get("title"), tags.get("album"), tags.get("isrc"),
             tags.get("dur"))).lastrowid

    for _ in range(rng.randint(0, 12)):
        a = rng.choice(artists)
        kind = rng.choices(["linked", "unlinked", "manual"], [0.4, 0.5, 0.1])[0]
        ext = rng.choice(EXTS)
        if kind == "unlinked":
            title = rng.choice(TITLES)
            folder = (rng.choice(release_titles[a]) if rng.random() < 0.5 else rng.choice(FOLDERS))
            if rng.random() < 0.5:
                folder = strip_markers(folder, release=True)
            fname = (f"{rng.randint(1, 8):02d} - " if rng.random() < 0.5 else "") + naming.clean(title) + ext
            fid = insert_file(f"{names[a]}/{naming.clean(folder)}/{fname}", a, None, "unmatched", None,
                              title=title, dur=rng.randint(198, 206) + rng.choice([0, 0, 0.5]),
                              album=folder if rng.random() < 0.6 else None,
                              isrc=(rec_info[rng.choice(sorted(cands[a]))][3] if rng.random() < 0.1 else None))
            if fid is not None and rng.random() < 0.3:
                two = rng.sample(sorted(cands[a]), min(2, len(cands[a])))
                conn.execute("INSERT INTO link_review(file_id, reason, candidates) VALUES (?, 'ambiguous', ?)",
                             (fid, json.dumps([{"recording_id": r, "norm_title": "", "duration_s": None,
                                                "score": 0.8} for r in two])))
            continue
        rid = rng.choice(sorted(cands[a]))
        _, title, dur, isrc = rec_info[rid]
        if rng.random() < 0.7 and on_tracks.get(rid):  # where cadence itself would have put the file
            owner, rel_title, disc, number, t_title = rng.choice(on_tracks[rid])
            album = strip_markers(rel_title, release=True) if rng.random() < 0.6 else rel_title
            p = f"{naming.album_dir(names[owner], album)}/{naming.track_file(number, t_title, ext, disc)}"
        else:
            p = f"{names[a]}/{rng.choice(FOLDERS)}/{rng.randint(1, 8):02d} - {naming.clean(title)}{ext}"
        if kind == "linked":
            method = "isrc" if isrc and rng.random() < 0.5 else "fuzzy"
            insert_file(p, a, rid, method, 1.0 if method == "isrc" else 0.8, title=title,
                        dur=dur if dur is not None else 200, isrc=isrc if method == "isrc" else None)
        else:  # manual: tags may point at some other recording, so a rematch would move it if it were allowed to
            other = rec_info[rng.choice(sorted(cands[a]))]
            fid = insert_file(p, a, rid, "manual", 1.0, title=other[1], dur=other[2] or 200, isrc=other[3])
            if fid is not None:
                led.manual.append(fid)
    conn.commit()
    return led
