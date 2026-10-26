"""What quality a file actually is: a short label and a lossy flag, derived from the scanned codec facts."""
from __future__ import annotations

import sqlite3

_LOSSY = {"aac": "AAC", "mp3": "MP3", "opus": "Opus", "vorbis": "Vorbis", "ogg": "Vorbis"}
_LOSSLESS = {"flac": "FLAC", "alac": "ALAC", "pcm": "PCM"}


def _key(codec: str | None) -> str:
    c = (codec or "").strip().lower()
    return "aac" if c.startswith("mp4a") else c


def classify(codec: str | None) -> bool | None:
    """True = lossy, False = lossless, None = unknown codec."""
    k = _key(codec)
    if k in _LOSSY:
        return True
    return False if k in _LOSSLESS else None


def is_lossy(codec: str | None) -> bool:
    return classify(codec) is True


def _khz(sample_rate: int) -> str:
    return f"{sample_rate / 1000:.1f}".removesuffix(".0")


def describe(codec: str | None, sample_rate: int | None, bit_depth: int | None, bitrate: int | None) -> str:
    """'FLAC 16/44.1', 'FLAC 24/192', 'AAC 320'. Unknown codec -> ''. Bitrate is bits per second."""
    k = _key(codec)
    name = _LOSSY.get(k) or _LOSSLESS.get(k)
    if name is None:
        return ""
    if k in _LOSSY:
        return f"{name} {round(bitrate / 1000)}" if bitrate else name
    if bit_depth and sample_rate:
        return f"{name} {bit_depth}/{_khz(sample_rate)}"
    return name


def facts(codec, sample_rate, bit_depth, bitrate) -> tuple[int | None, str | None]:
    """(lossy, quality_received) as stored in the `file` table; (None, None) for an unknown codec."""
    lossy = classify(codec)
    return (None, None) if lossy is None else (int(lossy), describe(codec, sample_rate, bit_depth, bitrate))


def backfill_quality(conn: sqlite3.Connection) -> int:
    """Fill lossy / quality_received from the stored codec facts where either is NULL. No file IO, no commit."""
    n = 0
    rows = conn.execute("SELECT id, codec, sample_rate, bit_depth, bitrate FROM file "
                        "WHERE codec IS NOT NULL AND (lossy IS NULL OR quality_received IS NULL)").fetchall()
    for r in rows:
        lossy, label = facts(r["codec"], r["sample_rate"], r["bit_depth"], r["bitrate"])
        if lossy is not None:
            conn.execute("UPDATE file SET lossy = ?, quality_received = ? WHERE id = ?", (lossy, label, r["id"]))
            n += 1
    return n


def quality_summary(conn: sqlite3.Connection, top: int = 10) -> dict:
    total, lossless, lossy = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(lossy = 0), 0), COALESCE(SUM(lossy = 1), 0) FROM file").fetchone()
    worst = conn.execute("SELECT a.name, COUNT(*) AS n FROM file f JOIN artist a ON a.id = f.artist_id "
                         "WHERE f.lossy = 1 GROUP BY a.id ORDER BY n DESC, a.name LIMIT ?", (top,)).fetchall()
    return {"total": total, "lossless": lossless, "lossy": lossy, "unknown": total - lossless - lossy,
            "top_lossy_artists": [(r["name"], r["n"]) for r in worst]}


def format_summary(s: dict) -> str:
    lines = [f"files: {s['total']}  lossless: {s['lossless']}  lossy: {s['lossy']}  unknown: {s['unknown']}"]
    if s["top_lossy_artists"]:
        lines.append("top artists by lossy files:")
        lines += [f"  {n:>5}  {name}" for name, n in s["top_lossy_artists"]]
    return "\n".join(lines)
