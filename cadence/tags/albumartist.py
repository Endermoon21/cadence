"""Rule for a clean album-artist tag: the primary artist of a multi-artist string. Audit only, no writes.

A name is split only when that is safe: at "feat." / "ft." / " with " always, and at ", " / " & " / " and " /
" x " only when the part before the separator is an artist we already know. A string that is itself a known
single artist ("Earth, Wind & Fire") is kept whole. When unsure the original is returned."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection

from cadence.identity.normalize import is_various, norm_artist

_FEAT = re.compile(r"\s*[(\[]?\s*\b(?:feat|ft|featuring)\b\.?\s+", re.I)
_WITH = re.compile(r"\s+with\s+")
_SEP = re.compile(r", | & | and | x ")
SHOWN_EXAMPLES = 20


def primary_album_artist(value: str, known: Collection[str] = ()) -> str:
    """`known` holds norm_artist() names of artists known to be single acts."""
    if not value or norm_artist(value) in known:
        return value
    v = value.strip()
    cuts = [m.start() for m in (_FEAT.search(v), _WITH.search(v)) if m]
    cut = min((c for c in cuts if c > 0), default=None)
    if cut:
        v = v[:cut].strip()
        if norm_artist(v) in known:
            return v
    for m in _SEP.finditer(v):
        first = v[:m.start()].strip()
        if first and norm_artist(first) in known:
            return first
    return v if cut else value


def _known_artists(conn: sqlite3.Connection) -> set[str]:
    """Ledger artists we can call single acts: resolved on Tidal, or with no separator in the name."""
    return {r["norm_name"] for r in conn.execute("SELECT name, norm_name, tidal_match FROM artist")
            if r["tidal_match"] in ("auto", "manual") or not (_SEP.search(r["name"]) or _FEAT.search(r["name"])
                                                              or _WITH.search(r["name"]))}


def audit_albumartist(conn: sqlite3.Connection, examples: int = SHOWN_EXAMPLES) -> dict:
    """Read-only: files whose tag_albumartist would change, per ledger artist, plus example rewrites."""
    known = _known_artists(conn)
    per_artist: dict[str, int] = {}
    pairs: dict[tuple[str, str], int] = {}
    total = changed = 0
    for r in conn.execute("SELECT a.name AS artist, f.tag_albumartist AS tag FROM file f "
                          "LEFT JOIN artist a ON a.id = f.artist_id WHERE TRIM(COALESCE(f.tag_albumartist, '')) != ''"):
        total += 1
        if is_various(r["tag"]):
            continue
        new = primary_album_artist(r["tag"], known)
        if new != r["tag"]:
            changed += 1
            per_artist[r["artist"] or "(no artist)"] = per_artist.get(r["artist"] or "(no artist)", 0) + 1
            pairs[(r["tag"], new)] = pairs.get((r["tag"], new), 0) + 1
    ranked = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"files_with_tag": total, "would_change": changed,
            "per_artist": sorted(per_artist.items(), key=lambda kv: (-kv[1], kv[0])),
            "examples": [(old, new, n) for (old, new), n in ranked[:examples]]}


def format_audit(a: dict) -> str:
    lines = [f"audit-tags: {a['would_change']} of {a['files_with_tag']} file(s) with an album-artist tag "
             "would change (nothing written)"]
    if a["per_artist"]:
        lines.append("per artist:")
        lines += [f"  {n:>5}  {name}" for name, n in a["per_artist"]]
        lines.append(f"examples (up to {SHOWN_EXAMPLES}):")
        lines += [f'  "{old}" -> "{new}"  ({n} file(s))' for old, new, n in a["examples"]]
    return "\n".join(lines)
