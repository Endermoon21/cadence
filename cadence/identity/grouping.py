"""Pure grouping rules: editions of one release, and the same song across editions.

Canonical recording of a cluster (editions policy, decided 2026-10-26: the latest remaster is the default):
  1. if any member is a remaster, the LATEST remaster wins, ordered by (remaster_year or 0, remaster_date, id):
     the year written on the remaster marker first ('(2016 Remaster)'; Tidal often dates a remaster with the
     original release date, so the date alone cannot rank them), then the release date, then the highest id.
     A remaster whose marker has no year uses the year of its release date (done by the caller, groups.py);
  2. otherwise the earliest first_release_date wins (tie: lowest id).
A recording is a remaster when its own title/version or the release it is on carries the 'remaster' flag of
normalize.split_title (that covers '(Remastered)', '(2011 Remaster)', '- Remastered 2011' ...). Originals stay
in the cluster as secondary members; nothing is merged or dropped by this choice, only who is first."""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from cadence.identity.normalize import remaster_year, split_title

CLUSTER_TOLERANCE_S = 3
ALBUM_TITLE_TOLERANCE_S = 1                          # tighter: the titles only match after the album prefix is cut
ALBUM_TITLE_MIN_DURATION_S = 60                      # intros / skits: a few seconds of equal length mean nothing
_NO_DATE = "9999-99-99"  # sorts after every real date
_VARIANT_TAIL_RE = re.compile(r"\s*\[[^\[\]]*\]$")  # the ' [live]' label norm_title appends for a variant


class RecordingItem(NamedTuple):
    id: int
    norm_title: str
    duration_s: int | None
    isrc: str | None
    first_release_date: str | None
    variant: str | None = None                       # live / remix / demo ...; None = main version
    groups: frozenset[str] = frozenset()             # release group keys of the releases it is on
    remaster: bool = False                           # title or a release title carries the 'remaster' flag
    remaster_date: str | None = None                 # earliest date of the remaster release(s); None = unknown
    remaster_year: int | None = None                 # remaster year (tag, else release-date year); None = unknown
    albums: frozenset[tuple[str, str, int | None]] = frozenset()  # (group key, release norm_title, release year) of the ALBUM releases it is on


def date_year(d: str | None) -> int | None:
    """Year of an ISO-ish date ('2024-01-01', '2024'); None if there is none."""
    return int(d[:4]) if d and d[:4].isdigit() else None


def is_remaster(title: str, version: str | None = None, *, release: bool = False) -> bool:
    return "remaster" in split_title(title, version, release=release).flags


def release_group_key(title: str, version: str | None, type: str) -> str:
    """Same key for a release and its remaster / deluxe / expanded editions; live, remix... editions differ."""
    p = split_title(title, version, release=True)
    return f"{type}|{p.base}" + (f" [{p.variant}]" if p.variant else "")


def album_prefix(norm: str, prefixes) -> str:
    """The longest of the prefixes that norm_title starts with ('<prefix> ') and that leaves a title; '' if none.
    What is left must be more than a variant label: 'night [live]' minus 'night' leaves only '[live]', no title."""
    best = ""
    for p in prefixes:
        if len(p) > len(best) and norm.startswith(p + " ") and _VARIANT_TAIL_RE.sub("", norm[len(p) + 1:]).strip():
            best = p
    return best


def strip_album_prefix(norm: str, prefixes) -> str:
    """norm_title minus the longest leading '<album norm_title> ' that leaves something; unchanged if none does."""
    best = album_prefix(norm, prefixes)
    return norm[len(best) + 1:].strip() if best else norm


def _find(parent: dict[int, int], x: int) -> int:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union(parent: dict[int, int], a: int, b: int) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[max(ra, rb)] = min(ra, rb)


def cluster_recordings(items: Sequence[RecordingItem]) -> list[list[int]]:
    """Clusters of ids; the canonical (see module docstring) is first in each list, the other members follow
    by earliest first_release_date then id, and the lists are ordered by canonical id.

    Same norm_title (which carries the variant label) and duration within CLUSTER_TOLERANCE_S of the cluster's
    first member by duration. Unknown duration or a different title never clusters, except identical ISRCs.
    A variant recording (live, remix...) additionally needs a release group in common: the same live song
    from two different concerts must not merge on duration alone.

    Album-prefixed titles (Tidal sometimes stores 'Saturday Night Wrist - Rats!Rats!Rats!' next to 'Rats!Rats!Rats!'):
    after the rules above, two recordings that the old rule left as singletons also merge when, per album release
    group they share, cutting the norm_title of one of THAT group's releases off the front of at least one of them
    makes the titles equal and non-empty, the variant is the same, and both durations are known and within
    ALBUM_TITLE_TOLERANCE_S of each other. The ISRC need not match. Each singleton joins at most one partner (the
    closest by duration, then id), so a new cluster has exactly two members; a cluster the old rule formed never
    gains a member, changes its head or splits."""
    parent = {i.id: i.id for i in items}
    by_title: dict[tuple[str, str | None], list[tuple[int, int]]] = {}
    by_isrc: dict[str, int] = {}
    for rid, norm, dur, isrc, _, variant, groups, *_ in items:
        if dur is not None:
            for scope in (groups if variant else (None,)):
                by_title.setdefault((norm, scope), []).append((dur, rid))
        key = (isrc or "").strip().upper()
        if key:
            if key in by_isrc:
                _union(parent, by_isrc[key], rid)
            else:
                by_isrc[key] = rid
    for members in by_title.values():
        members.sort()
        head_dur, head = members[0]
        for dur, rid in members[1:]:
            if dur - head_dur <= CLUSTER_TOLERANCE_S:
                _union(parent, head, rid)
            else:
                head_dur, head = dur, rid
    size: dict[int, int] = {}
    for rid in parent:
        size[_find(parent, rid)] = size.get(_find(parent, rid), 0) + 1
    by_album: dict[tuple[str | None, str, str], list[tuple[int, int, bool, frozenset[int]]]] = {}
    for item in items:
        if item.duration_s is None or item.duration_s < ALBUM_TITLE_MIN_DURATION_S or not item.albums \
                or size[_find(parent, item.id)] > 1:
            continue                                         # only recordings the old rule left alone take part
        for gk in {g for g, *_ in item.albums}:
            prefix = album_prefix(item.norm_title, {t for g, t, _ in item.albums if g == gk})
            stripped = item.norm_title[len(prefix) + 1:].strip() if prefix else item.norm_title
            # release years of the album: the prefix release for a prefixed title, any album of the group otherwise
            years = frozenset(y for g, t, y in item.albums if g == gk and (not prefix or t == prefix) and y is not None)
            by_album.setdefault((item.variant, stripped, gk), []).append(
                (item.duration_s, item.id, bool(prefix), years))
    paired: set[int] = set()
    for key in sorted(by_album, key=str):
        members = sorted(by_album[key], key=lambda m: m[:2])
        for n, (dur, rid, prefixed, years) in enumerate(members):
            if rid in paired:
                continue
            for dur2, rid2, prefixed2, years2 in members[n + 1:]:
                if dur2 - dur > ALBUM_TITLE_TOLERANCE_S:
                    break
                if rid2 not in paired and (prefixed or prefixed2) and years & years2:
                    _union(parent, rid, rid2)
                    paired.update((rid, rid2))
                    break
    rank = {i.id: (i.first_release_date or _NO_DATE, i.id) for i in items}
    clusters: dict[int, list[int]] = {}
    for rid in parent:
        clusters.setdefault(_find(parent, rid), []).append(rid)
    remaster = {i.id: (i.remaster_year or 0, i.remaster_date or "", i.id) for i in items if i.remaster}
    out = []
    for ids in clusters.values():
        ids = sorted(ids, key=rank.__getitem__)
        remasters = [r for r in ids if r in remaster]
        if remasters:
            canon = max(remasters, key=remaster.__getitem__)
            ids.remove(canon)
            ids.insert(0, canon)
        out.append(ids)
    return sorted(out, key=lambda ids: ids[0])
