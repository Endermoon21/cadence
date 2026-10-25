"""Match a library file to a catalog recording. Never guesses on title alone."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cadence.identity.normalize import fold, norm_title

ALBUM_TOLERANCE_S = 5.0
_HEADS = {"live", "demo", "acoustic", "instrumental", "remix"}
_LIVE_MARK_RE = re.compile(r"[(\[]\s*live\b")  # on the casefolded raw title: fold() drops the brackets
_LIVE_WORDS_RE = re.compile(r"\blive (?:at|in|from|recordings|sessions)\b")  # on the folded title


def _is_live_album(album: str | None) -> bool:
    """'Live in Texas', 'Album (Live)', 'X: Live Recordings' yes; 'Live Through This', 'Live and Let Die' no."""
    if not album:
        return False
    return bool(_LIVE_MARK_RE.search(album.casefold()) or _LIVE_WORDS_RE.search(fold(album)))


def _split(norm: str) -> tuple[str, str]:
    """'base [variant]' -> (base, variant); 'base' -> (base, '')."""
    if norm.endswith("]") and " [" in norm:
        base, _, var = norm[:-1].rpartition(" [")
        return base, var
    return norm, ""


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
    # The tiers below only run when the exact tier found nothing; they never change an existing match.
    if best is None and album:
        # Tidal titles like "Adrenaline - 7 Words" / tags like "The Original Album - Moondance": drop the
        # album-name prefix. Only when the prefix is the album the file itself claims, so "Love" + "Love Me Do"
        # can never alias to "Me Do".
        forms = {title}
        for pre in ("the original album ", album + " "):
            if title.startswith(pre) and len(title) > len(pre):
                forms.add(title[len(pre):])
        for c in cands:
            if c.duration_s is None or abs(f.duration_s - c.duration_s) > tolerance_s:
                continue
            cforms = {c.norm_title}
            if album in c.release_norm_titles and c.norm_title.startswith(album + " "):
                cforms.add(c.norm_title[len(album) + 1:])
            if not (forms & cforms) or (len(forms) == 1 and len(cforms) == 1):
                continue
            key = (False, abs(f.duration_s - c.duration_s), c.recording_id)
            if best is None or key < best[0]:
                best = (key, True, c.recording_id)
        if best is not None:
            return MatchResult(best[2], "fuzzy", 0.85)
    if best is None:
        # Variant labels that differ only in their tail: "[live]" vs "[live at the la forum usa 1983]",
        # "[live nottingham university 1975 10 11]" vs "[live nottingham university 1975]".
        fb, fv = _split(title)
        head = fv.split()[0] if fv else ""
        if not head and _is_live_album(f.album):
            head = "live"  # live album: tag has no marker, Tidal labels each track "[live ...]"
        if head in _HEADS:
            for c in cands:
                if c.duration_s is None or abs(f.duration_s - c.duration_s) > tolerance_s:
                    continue
                cb, cv = _split(c.norm_title)
                if cb != fb or not cv or cv.split()[0] != head or cv == fv:
                    continue
                key = (not (bool(album) and album in c.release_norm_titles),
                       abs(f.duration_s - c.duration_s), c.recording_id)
                if best is None or key < best[0]:
                    best = (key, key[0] is False, c.recording_id)
            if best is not None:
                return MatchResult(best[2], "fuzzy", 0.7)
    if best is None and album:
        # Nothing within tolerance: accept a same-title recording up to 5s off if it sits on the album the file
        # claims (remaster/fade/trim differences). Never replaces a within-tolerance match.
        for c in cands:
            if c.duration_s is None or c.norm_title != title or album not in c.release_norm_titles:
                continue
            diff = abs(f.duration_s - c.duration_s)
            if diff > ALBUM_TOLERANCE_S:
                continue
            key = (False, diff, c.recording_id)
            if best is None or key < best[0]:
                best = (key, True, c.recording_id)
        if best is not None:
            return MatchResult(best[2], "fuzzy", 0.7)
    if best is None:
        return None
    return MatchResult(best[2], "fuzzy", 0.9 if best[1] else 0.8)
