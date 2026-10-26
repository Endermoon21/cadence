"""Fetch the planned tracks: Tidal gate -> staging -> verify -> tag -> place -> ledger row.

Writes only under `staging_dir` and `library_root` and never deletes or overwrites anything. A dry run (the
default) prints the plan and touches neither the network nor the disk. Tracks are committed one at a time."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import mutagen
import mutagen.flac
import mutagen.mp4
import requests

from cadence.catalog.sync import _is_dead_connection, _named, _safe_message, _statuses
from cadence.fetch.plan import FetchItem
from cadence.fetch.stream import StreamInfo, StreamRefused, ext_for_quality, resolve_stream
from cadence.gate.bucket import Gate, GatePaused
from cadence.ledger import quality as quality_facts
from cadence.ledger.db import log_event

DEFAULT_QUALITY = "HI_RES_LOSSLESS"
DURATION_TOLERANCE_S = 3
MAX_NETWORK_FAILURES = 3
HTTP_TIMEOUT = (10, 60)
CHUNK = 1 << 20


class FetchError(Exception):
    """This track failed; the run goes on with the next one."""


class Blocked(Exception):
    """Tidal answered 401 to the playback request for this track: not allowed for this account."""


class AuthStop(Exception):
    """Our login is rejected: every further track would fail the same way."""


# ---- helpers ----

def _safe_rel(rel: str) -> bool:
    return all(p not in ("", ".", "..") for p in rel.split("/"))


def _in_ledger(conn: sqlite3.Connection, rel: str) -> bool:
    return conn.execute("SELECT 1 FROM file WHERE path = ?", (rel,)).fetchone() is not None


def _exists(conn: sqlite3.Connection, library_root: Path, rel: str) -> bool:
    return (library_root / rel).exists() or _in_ledger(conn, rel)


def _token_401(exc: BaseException) -> bool:
    """A 401 whose body says the token/session is the problem (as opposed to this one asset)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        resp = getattr(exc, "response", None)
        if getattr(resp, "status_code", None) == 401:
            try:
                body = resp.json()
            except Exception:
                body = {}
            text = " ".join(str(body.get(k, "")) for k in ("userMessage", "description", "detail")).lower()
            return "token" in text or "session" in text or body.get("subStatus") in (11002, 11003)
        exc = exc.__cause__ or exc.__context__
    return False


def _fmt_dur(s: int | None) -> str:
    return f"{s // 60}:{s % 60:02d}" if s is not None else "?:??"


def _preflight(staging_dir: Path, library_root: Path, verify: bool, ffmpeg: str | None) -> str | None:
    if not library_root.is_dir():
        return f"library {library_root} does not exist (create it first; it is never created here)"
    staging_dir.mkdir(parents=True, exist_ok=True)
    if os.stat(staging_dir).st_dev != os.stat(library_root).st_dev:
        return f"staging {staging_dir} and library {library_root} are on different filesystems; placing is a rename"
    if verify and not ffmpeg:
        return "ffmpeg not found on PATH (needed to verify downloads)"
    return None


# ---- download / verify / tag ----

def _get(http, url: str):
    resp = http.get(url, stream=True, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()  # a 429 here is an HTTPError with .response, which the gate turns into a pause
    return resp


def _download_direct(http, url: str, part: Path) -> None:
    resp = _get(http, url)
    try:
        written = 0
        with open(part, "wb") as fh:
            for chunk in resp.iter_content(CHUNK):
                fh.write(chunk)
                written += len(chunk)
    finally:
        resp.close()
    expected = (getattr(resp, "headers", None) or {}).get("Content-Length")
    if expected is not None and int(expected) != written:
        raise FetchError(f"truncated download ({written} of {expected} bytes)")


def _download_segments(http, stream: StreamInfo, part: Path, ffmpeg: str | None) -> None:
    """Pipe init + segments (fragmented MP4) into ffmpeg, which copies the audio into a plain .flac / .m4a."""
    if not ffmpeg:
        raise FetchError("segmented stream needs ffmpeg, which was not found")
    fmt = "flac" if stream.codec == "flac" else "ipod"
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([ffmpeg, "-v", "error", "-y", "-i", "pipe:0", "-map", "0:a:0", "-c", "copy",
                                 "-f", fmt, str(part)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        ok = False
        try:
            for url in stream.urls:
                resp = _get(http, url)
                try:
                    for chunk in resp.iter_content(CHUNK):
                        proc.stdin.write(chunk)
                finally:
                    resp.close()
            ok = True
        except BrokenPipeError:
            pass  # ffmpeg quit early; its message is below
        finally:
            if not ok and proc.poll() is None:
                proc.kill()
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
            rc = proc.wait()
        if ok and rc == 0:
            return
        err.seek(0)
        raise FetchError("ffmpeg remux failed: " + err.read().decode(errors="replace").strip()[:200])


def _verify(path: Path, ffmpeg: str, expected_s: int | None) -> None:
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    if r.returncode != 0 or r.stderr.strip():
        raise FetchError("decode check failed: " + (r.stderr.strip().splitlines() or ["ffmpeg error"])[0][:200])
    audio = mutagen.File(path)
    if audio is None:
        raise FetchError("not a readable audio file")
    if expected_s is not None and abs(audio.info.length - expected_s) > DURATION_TOLERANCE_S:
        raise FetchError(f"duration {audio.info.length:.1f}s is not within {DURATION_TOLERANCE_S}s of Tidal's {expected_s}s")


def _tag(path: Path, item: FetchItem) -> None:
    """ALBUMARTIST (primary artist), TITLE, TRACKNUMBER, DISCNUMBER, ALBUM, ISRC; nothing else is added."""
    if path.suffix == ".flac":
        audio = mutagen.flac.FLAC(path)
        audio["ALBUMARTIST"], audio["TITLE"], audio["ALBUM"] = [item.artist], [item.title], [item.release]
        audio["TRACKNUMBER"], audio["DISCNUMBER"] = [str(item.number)], [str(item.disc)]
        if item.isrc:
            audio["ISRC"] = [item.isrc]
    else:
        audio = mutagen.mp4.MP4(path)
        audio["aART"], audio["\xa9nam"], audio["\xa9alb"] = [item.artist], [item.title], [item.release]
        audio["trkn"], audio["disk"] = [(item.number, 0)], [(item.disc, 0)]
        if item.isrc:
            audio["----:com.apple.iTunes:ISRC"] = [mutagen.mp4.MP4FreeForm(item.isrc.encode())]
    audio.save()


def _file_facts(path: Path, stream: StreamInfo) -> dict:
    audio = mutagen.File(path)
    if audio is None:
        raise FetchError("not a readable audio file")
    info = audio.info
    codec = "flac" if isinstance(audio, mutagen.flac.FLAC) else \
        ("alac" if str(getattr(info, "codec", "")).lower() == "alac" else "aac")
    lossless = codec in ("flac", "alac")
    return {"codec": codec, "sample_rate": getattr(info, "sample_rate", None) or stream.sample_rate,
            "bit_depth": (getattr(info, "bits_per_sample", None) or stream.bit_depth) if lossless else None,
            "bitrate": getattr(info, "bitrate", None) or stream.bitrate, "duration_s": float(info.length)}


# ---- one track ----

def _insert_file(conn: sqlite3.Connection, item: FetchItem, rel: str, dest: Path, facts: dict, requested: str) -> str:
    lossy, received = quality_facts.facts(facts["codec"], facts["sample_rate"], facts["bit_depth"], facts["bitrate"])
    st = dest.stat()
    conn.execute(
        "INSERT INTO file(path, artist_id, recording_id, match, match_score, source, codec, sample_rate, bit_depth, "
        "bitrate, duration_s, size, mtime, tag_title, tag_albumartist, tag_album, tag_track, tag_disc, tag_isrc, "
        "lossy, quality_requested, quality_received, seen_at) VALUES (?, ?, ?, 'manual', 1.0, 'tidal', ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rel, item.artist_id, item.recording_id, facts["codec"], facts["sample_rate"], facts["bit_depth"],
         facts["bitrate"], facts["duration_s"], st.st_size, int(st.st_mtime), item.title, item.artist, item.release,
         str(item.number), str(item.disc), item.isrc, lossy, requested, received,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return received or ""


def _fetch_one(conn, session, gate: Gate, http, item: FetchItem, staging: Path, library: Path, quality: str,
               verify: bool, ffmpeg: str | None) -> str:
    """'placed' or 'exists'. Raises GatePaused, AuthStop, Blocked, FetchError or StreamRefused."""
    rel = item.target_rel
    if not _safe_rel(rel):
        raise FetchError(f"unsafe target path '{rel}'")
    if _exists(conn, library, rel):
        return "exists"
    try:
        stream = gate.call(resolve_stream, session, item.tidal_track_id, quality)
    except (GatePaused, StreamRefused):
        raise
    except Exception as exc:
        if _named(exc, "AuthenticationError") or _token_401(exc):
            raise AuthStop(_safe_message(exc)) from exc
        if 401 in _statuses(exc):
            raise Blocked(_safe_message(exc)) from exc
        raise
    if stream.ext != Path(rel).suffix:
        rel = item.target(stream.ext)
        if _exists(conn, library, rel):
            return "exists"
    staged = staging / rel
    staged.parent.mkdir(parents=True, exist_ok=True)
    part = staged.with_name(staged.name + ".part")
    if stream.segmented:
        gate.call(_download_segments, http, stream, part, ffmpeg)
    else:
        gate.call(_download_direct, http, stream.urls[0], part)
    os.replace(part, staged)  # staging only; a leftover from an earlier failed try is overwritten
    if verify:
        _verify(staged, ffmpeg, item.duration_s)
    _tag(staged, item)
    facts = _file_facts(staged, stream)
    dest = library / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return "exists"
    os.rename(staged, dest)
    try:
        received = _insert_file(conn, item, rel, dest, facts, quality)
        log_event(conn, "fetch.placed", rel, f"Placed '{item.title}' by {item.artist} as {received or facts['codec']}",
                  {"tidal_track_id": item.tidal_track_id, "recording_id": item.recording_id, "path": rel,
                   "quality_requested": quality, "quality_received": received,
                   "audio_quality": stream.audio_quality})
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise FetchError(f"placed {rel} but the ledger insert failed ({exc}); import-legacy will pick it up") from exc
    return "placed"


# ---- the run ----

def _dry_run(conn, items: list[FetchItem], library_root: Path, quality: str, out, skipped) -> dict:
    res = {"dry_run": True, "would_fetch": 0, "exists": 0, "skipped_dest": 0, "placed": 0, "blocked": 0,
           "failed": 0, "owned_elsewhere": len(skipped), "stopped": None, "until": None, "remaining": []}
    ext = ext_for_quality(quality)
    out(f"fetch (dry run, quality {quality}): {len(items)} item(s); nothing is downloaded or written. "
        "Re-run with --execute to fetch.")
    for it in items:
        rel = it.target(ext)
        if it.dest == "fiio":
            state, key = "skip (fiio-only)", "skipped_dest"
        elif _exists(conn, library_root, rel):
            state, key = "exists, skip", "exists"
        else:
            state, key = "would fetch", "would_fetch"
        res[key] += 1
        out(f"  {state:<16} {rel}  [tidal {it.tidal_track_id}, {_fmt_dur(it.duration_s)}, dest {it.dest}]")
    for it, owned_path in skipped:
        out(f"  {'owned elsewhere':<16} {it.target_rel}  (owned under another title: {owned_path})")
    out(f"fetch: {res['would_fetch']} would be fetched, {res['exists']} already exist, "
        f"{res['skipped_dest']} fiio-only skipped, {len(skipped)} owned under another title")
    return res


def fetch_items(conn: sqlite3.Connection, session, gate: Gate | None, items: list[FetchItem], staging_dir,
                library_root, dry_run: bool = True, quality: str = DEFAULT_QUALITY, verify: bool = True, *,
                http=None, ffmpeg: str | None = None, out=print, skipped: list | None = None) -> dict:
    """Result keys: placed, exists, blocked, failed, skipped_dest, stopped (None | paused | auth | network |
    setup), until, remaining (items not attempted, including the one a stop happened on)."""
    library_root = Path(library_root)
    skipped = skipped or []  # (item, owned path) pairs the planner left out as owned under another title
    if dry_run:
        return _dry_run(conn, items, library_root, quality, out, skipped)
    staging_dir = Path(staging_dir)
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    res = {"dry_run": False, "placed": 0, "exists": 0, "blocked": 0, "failed": 0, "skipped_dest": 0,
           "stopped": None, "until": None, "remaining": []}
    problem = _preflight(staging_dir, library_root, verify, ffmpeg)
    if problem:
        out(f"fetch: {problem}")
        return {**res, "stopped": "setup", "remaining": list(items)}
    http = http or requests.Session()
    network_failures = 0
    for i, it in enumerate(items):
        if it.dest == "fiio":
            res["skipped_dest"] += 1
            out(f"  skip (fiio-only, not handled yet) {it.target_rel}")
            continue
        try:
            status = _fetch_one(conn, session, gate, http, it, staging_dir, library_root, quality, verify, ffmpeg)
            res[status] += 1
            out(f"  {status:<8} {it.target_rel}")
            network_failures = 0
        except GatePaused as p:
            conn.rollback()
            res.update(stopped="paused", until=p.until.isoformat(), remaining=list(items[i:]))
            log_event(conn, "gate.pause", it.target_rel, f"Tidal gate paused during fetch until {p.until.isoformat()}: "
                      f"{p.reason}", {"until": p.until.isoformat()})
            conn.commit()
            break
        except AuthStop as exc:
            conn.rollback()
            res.update(stopped="auth", remaining=list(items[i:]))
            log_event(conn, "fetch.error", str(it.tidal_track_id), f"Tidal rejected our login: {exc}")
            conn.commit()
            break
        except Blocked as exc:
            conn.rollback()
            res["blocked"] += 1
            log_event(conn, "fetch.blocked", str(it.tidal_track_id),
                      f"Tidal refused playback (401) for '{it.title}' by {it.artist}: {exc}",
                      {"tidal_track_id": it.tidal_track_id, "path": it.target_rel})
            conn.commit()
            out(f"  blocked  {it.target_rel}")
            network_failures = 0
        except Exception as exc:
            conn.rollback()
            msg = f"{type(exc).__name__}: {_safe_message(exc)}"
            res["failed"] += 1
            log_event(conn, "fetch.failed", str(it.tidal_track_id), f"'{it.title}' by {it.artist}: {msg}",
                      {"tidal_track_id": it.tidal_track_id, "path": it.target_rel})
            conn.commit()
            out(f"  failed   {it.target_rel}  ({msg})")
            network_failures = network_failures + 1 if _is_dead_connection(exc) else 0
            if network_failures >= MAX_NETWORK_FAILURES:
                res.update(stopped="network", remaining=list(items[i + 1:]))
                break
    out(f"fetch: {res['placed']} placed, {res['exists']} already existed, {res['blocked']} blocked, "
        f"{res['failed']} failed, {len(res['remaining'])} not attempted, {len(skipped)} owned under another title, "
        f"stopped={res['stopped']}")
    for it in res["remaining"]:
        out(f"  remaining {it.target_rel}")
    return res
