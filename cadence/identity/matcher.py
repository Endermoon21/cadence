"""Match a library file to a catalog recording. Never guesses on title alone."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cadence.identity.normalize import norm_title


@dataclass(frozen=True)
class FileFacts:
    title: str
    duration_s: float | None
    isrc: str | None = None
    album: str | None = None


@dataclass(frozen=True)
class RecCandidate:
    recording_id: int
    isrc: str | None
    norm_title: str
    duration_s: int | None
    release_norm_titles: frozenset[str]


@dataclass(frozen=True)
class MatchResult:
    recording_id: int
    method: str  # 'isrc' | 'fuzzy'
    score: float


def _clean_isrc(v: str | None) -> str:
    return v.strip().upper() if v else ""


def match_file(f: FileFacts, cands: Sequence[RecCandidate],
               tolerance_s: float = 2.0) -> MatchResult | None:
    isrc = _clean_isrc(f.isrc)
    if isrc:
        ids = sorted(c.recording_id for c in cands if _clean_isrc(c.isrc) == isrc)
        if ids:
            return MatchResult(ids[0], "isrc", 1.0)

    if f.duration_s is None:
        return None
    title = norm_title(f.title)
    album = norm_title(f.album) if f.album else ""
    best: tuple[tuple[bool, float, int], bool, int] | None = None
    for c in cands:
        if c.duration_s is None or c.norm_title != title:
            continue
        diff = abs(f.duration_s - c.duration_s)
        if diff > tolerance_s:
            continue
        album_match = bool(album) and album in c.release_norm_titles
        key = (not album_match, diff, c.recording_id)
        if best is None or key < best[0]:
            best = (key, album_match, c.recording_id)
    if best is None:
        return None
    return MatchResult(best[2], "fuzzy", 0.9 if best[1] else 0.8)
