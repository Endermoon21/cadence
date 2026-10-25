"""Pure grouping rules: editions of one release, and the same song across editions."""
from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from cadence.identity.normalize import split_title

CLUSTER_TOLERANCE_S = 3
_NO_DATE = "9999-99-99"  # sorts after every real date


class RecordingItem(NamedTuple):
    id: int
    norm_title: str
    duration_s: int | None
    isrc: str | None
    first_release_date: str | None
    variant: str | None = None                       # live / remix / demo ...; None = main version
    groups: frozenset[str] = frozenset()             # release group keys of the releases it is on


def release_group_key(title: str, version: str | None, type: str) -> str:
    """Same key for a release and its remaster / deluxe / expanded editions; live, remix... editions differ."""
    p = split_title(title, version, release=True)
    return f"{type}|{p.base}" + (f" [{p.variant}]" if p.variant else "")


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
    """Clusters of ids; the canonical (earliest first_release_date, then lowest id) is first in each list,
    and the lists are ordered by canonical id.

    Same norm_title (which carries the variant label) and duration within CLUSTER_TOLERANCE_S of the cluster's
    first member by duration. Unknown duration or a different title never clusters, except identical ISRCs.
    A variant recording (live, remix...) additionally needs a release group in common: the same live song
    from two different concerts must not merge on duration alone."""
    parent = {i.id: i.id for i in items}
    by_title: dict[tuple[str, str | None], list[tuple[int, int]]] = {}
    by_isrc: dict[str, int] = {}
    for rid, norm, dur, isrc, _, variant, groups in items:
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
    rank = {i.id: (i.first_release_date or _NO_DATE, i.id) for i in items}
    clusters: dict[int, list[int]] = {}
    for rid in parent:
        clusters.setdefault(_find(parent, rid), []).append(rid)
    out = [sorted(ids, key=rank.__getitem__) for ids in clusters.values()]
    return sorted(out, key=lambda ids: ids[0])
