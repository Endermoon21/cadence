"""Fetch the planned tracks: Tidal gate -> staging -> verify -> tag -> place -> ledger row.

Writes only under `staging_dir` and `library_root` and never deletes or overwrites anything. A dry run (the
default) prints the plan and touches neither the network nor the disk. Tracks are committed one at a time."""
from __future__ import annotations

import errno
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import mutagen
import mutagen.flac
import mutagen.mp4
import requests

from cadence.catalog.sync import _is_dead_connection, _named, _safe_message, _statuses
from cadence.fetch.fallback import Acquired, FallbackMiss, VpnDown
from cadence.fetch.plan import FetchItem, Plan
from cadence.fetch.stream import StreamInfo, StreamRefused, ext_for_quality, resolve_stream
from cadence.gate.bucket import Gate, GatePaused
from cadence.identity.normalize import norm_title
from cadence.ledger import quality as quality_facts
from cadence.ledger.db import log_event

DEFAULT_QUALITY = "HI_RES_LOSSLESS"
DURATION_TOLERANCE_S = 3
MAX_NETWORK_FAILURES = 3
MAX_EDITION_PROBES = 4  # other Tidal tracks of the same recording asked for per item when the planned one is served lossy
HTTP_TIMEOUT = (10, 60)
CHUNK = 1 << 20
LOSSLESS = ("LOSSLESS", "HI_RES_LOSSLESS")
VIA = "via "  # a note starting with this marks an item placed from a fallback source
_NO_STREAM = SimpleNamespace(sample_rate=None, bit_depth=None, bitrate=None)  # a fallback file has no Tidal stream facts


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
    return os.path.lexists(library_root / rel) or _in_ledger(conn, rel)


def _occupied(conn: sqlite3.Connection, library_root: Path, item: FetchItem, rel: str) -> str | None:
    """None when `rel` is free. 'exists' when the ledger links this track's cluster to it, else 'conflict': something
    else holds the path, so the track is not in the library and is not fetched under another name."""
    if not _exists(conn, library_root, rel):
        return None
    linked = conn.execute("SELECT 1 FROM file f JOIN recording r ON r.id = f.recording_id WHERE f.path = ? AND "
                          "COALESCE(r.canon_id, r.id) = (SELECT COALESCE(canon_id, id) FROM recording WHERE id = ?)",
                          (rel, item.recording_id)).fetchone()
    return "exists" if linked else "conflict"


def _taken(conn: sqlite3.Connection, library_root: Path, item: FetchItem, rel: str,
           staged: Path | None = None) -> tuple[str, str] | None:
    """`staged` is the verified download when the path was found taken only after it: that copy is kept, and said so."""
    state = _occupied(conn, library_root, item, rel)
    if state == "conflict":
        data = {"tidal_track_id": item.tidal_track_id, "recording_id": item.recording_id, "path": rel}
        if staged is None:
            msg = (f"Not fetched: '{item.title}' by {item.artist} has no file of its own, "
                   f"but {rel} is taken by a file the ledger does not link to it")
            note = "target path is occupied by an unlinked file; nothing fetched"
        else:
            data["staged"] = str(staged)
            msg = (f"Downloaded and verified '{item.title}' by {item.artist}, but {rel} became occupied by a file the "
                   f"ledger does not link to it; the copy is kept in staging at {staged}")
            note = f"downloaded and verified, but the target path became occupied; copy kept in staging at {staged}"
        log_event(conn, "fetch.conflict", rel, msg, data)
        conn.commit()
        return state, note
    return (state, "") if state else None


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
    lib_st = os.stat(library_root)
    resolved = staging_dir.resolve()  # follows symlinks, even dangling ones, to where mkdir would land
    for d in (resolved, *resolved.parents):  # same (dev, inode) catches case variants on case-insensitive volumes
        try:
            st = os.stat(d)
        except OSError:
            continue  # does not exist yet; keep walking up to the nearest existing parent
        if (st.st_dev, st.st_ino) == (lib_st.st_dev, lib_st.st_ino):
            return f"staging {staging_dir} is the library ({library_root}) or inside it; it must be outside"
    staging_dir.mkdir(parents=True, exist_ok=True)
    if os.stat(staging_dir).st_dev != os.stat(library_root).st_dev:
        return f"staging {staging_dir} and library {library_root} are on different filesystems; placing is a rename"
    if verify and not ffmpeg:
        return "ffmpeg not found on PATH (needed to verify downloads)"
    return _probe_hard_links(library_root)


def _probe_hard_links(library_root: Path) -> str | None:
    """Placing needs os.link in the library root; find out now, before any Tidal call. Only the probe's own files."""
    probe = link = None
    try:
        fd, name = tempfile.mkstemp(dir=library_root, prefix=".cadence-linkprobe-")
        os.close(fd)
        probe = Path(name)
        link = probe.with_name(probe.name + ".lnk")
        os.link(probe, link)
    except OSError as exc:
        return (f"hard links are required to place files and are unsupported or not permitted on the library mount "
                f"{library_root} ({exc.strerror or exc})")
    finally:
        for p in (link, probe):
            if p is not None:
                p.unlink(missing_ok=True)
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
        raise FetchError(f"duration {audio.info.length:.1f}s is not within {DURATION_TOLERANCE_S}s of "
                         f"Tidal's {expected_s}s")


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

def _insert_file(conn: sqlite3.Connection, item: FetchItem, rel: str, dest: Path, facts: dict, requested: str,
                 source: str = "tidal") -> str:
    lossy, received = quality_facts.facts(facts["codec"], facts["sample_rate"], facts["bit_depth"], facts["bitrate"])
    st = dest.stat()
    conn.execute(
        "INSERT INTO file(path, artist_id, recording_id, match, match_score, source, codec, sample_rate, bit_depth, "
        "bitrate, duration_s, size, mtime, tag_title, tag_albumartist, tag_album, tag_track, tag_disc, tag_isrc, "
        "lossy, quality_requested, quality_received, seen_at) VALUES (?, ?, ?, 'manual', 1.0, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rel, item.artist_id, item.recording_id, source, facts["codec"], facts["sample_rate"], facts["bit_depth"],
         facts["bitrate"], facts["duration_s"], st.st_size, int(st.st_mtime), item.title, item.artist, item.release,
         str(item.number), str(item.disc), item.isrc, lossy, requested, received,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return received or ""


def _move_into_library(src: Path, dst: Path) -> None:
    """Place src at dst; an existing dst is never replaced, even if it appears at the last instant. dst is only ever
    created by os.link, which fails atomically with FileExistsError when dst exists (rename/replace are not used,
    as they would overwrite). A lexists pre-check gives the plain error in the normal case. Same filesystem: link
    src to dst, then unlink src (a failure to unlink is left for the caller, which retries after its commit).
    Cross device (EXDEV): copy to a uniquely named .part beside dst, fsync, check the size, link it to dst, remove
    the .part, so dst never appears half-written; src is left for the caller to remove after commit. If the
    target cannot hard link there is no overwriting fallback: FetchError, and no .part is left behind."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "library file already exists", str(dst))
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return _copy_then_link(src, dst)
        raise _no_hard_links(dst, exc) if exc.errno in _NO_LINK else exc
    try:
        os.unlink(src)
    except OSError:
        pass  # dst is placed; the caller removes the staged copy after commit and logs it if that fails too


_NO_LINK = (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP)


def _no_hard_links(dst: Path, exc: OSError) -> FetchError:
    return FetchError(f"cannot place {dst}: hard links are unsupported on this target ({exc.strerror or exc}); "
                      "refusing to fall back to an overwriting move")


def _copy_then_link(src: Path, dst: Path) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=dst.name + ".", suffix=".part")  # unique: never clobbers
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        with open(tmp, "r+b") as fh:
            os.fsync(fh.fileno())
        if tmp.stat().st_size != src.stat().st_size:
            raise FetchError(f"copy to {tmp} is {tmp.stat().st_size} bytes, the source is {src.stat().st_size}")
        try:
            os.link(tmp, dst)  # atomic no-clobber: FileExistsError if dst appeared while copying
        except OSError as exc:
            raise _no_hard_links(dst, exc) if exc.errno in (*_NO_LINK, errno.EXDEV) else exc
    finally:
        tmp.unlink(missing_ok=True)


def _staged(staging: Path, item: FetchItem, rel: str, ext: str) -> Path:
    """Staging path for this Tidal track: the library name plus the track id, so same-titled tracks never share one."""
    p = Path(rel)
    return staging / p.with_name(f"{p.stem} [tidal {item.tidal_track_id}]{ext}")


def _resolve(gate: Gate, session, tidal_id: int, quality: str) -> StreamInfo:
    try:
        return gate.call(resolve_stream, session, tidal_id, quality)
    except (GatePaused, StreamRefused):
        raise
    except Exception as exc:
        if _named(exc, "AuthenticationError") or _token_401(exc):
            raise AuthStop(_safe_message(exc)) from exc
        if 401 in _statuses(exc):
            raise Blocked(_safe_message(exc)) from exc
        raise


def _same_length(planned_s: int | None, alt_s: int | None) -> bool:
    """The alternate's ledger duration is within DURATION_TOLERANCE_S of the planned track's. Planned unknown: the
    alternate's own is all there is, so it passes if it has one and fails if neither is known. Alternate unknown: nothing
    to compare here; the downloaded file is checked against the planned duration afterwards."""
    if planned_s is None:
        return alt_s is not None
    return alt_s is None or abs(alt_s - planned_s) <= DURATION_TOLERANCE_S


def _probe_editions(conn: sqlite3.Connection, session, gate: Gate, item: FetchItem,
                    quality: str) -> tuple[tuple[int, int | None, StreamInfo] | None, list[int]]:
    """Tidal licenses lossless per release, so the planned track can be served lossy while another Tidal track of the
    recording is served lossless. Candidates: same recording_id, same norm_title as the planned track (the same version,
    not a live cut merged in by ISRC or length), a ledger duration within tolerance of the planned one, available, albums
    first, then the lowest id. Asks for up to MAX_EDITION_PROBES of them, one resolve_stream each, and returns
    (tidal id, its duration, stream) of the first lossless one, else None, with the ids tried. Every probe is logged and committed. A probe that fails is a lossy
    probe, except a paused gate and a rejected login, which stop the run."""
    want = norm_title(item.title)
    cands = [(tid, dur) for tid, dur, title, version in conn.execute(
        "SELECT t.tidal_id, t.duration_s, t.title, t.version FROM track t JOIN release rel ON rel.id = t.release_id "
        "WHERE t.recording_id = ? AND t.tidal_id IS NOT NULL AND t.tidal_id != ? AND t.available IS NOT 0 "
        "ORDER BY rel.type != 'album', t.tidal_id", (item.recording_id, item.tidal_track_id))
        if norm_title(title, version) == want and _same_length(item.duration_s, dur)][:MAX_EDITION_PROBES]
    tried: list[int] = []
    for tid, dur in cands:
        tried.append(tid)
        stream = error = None
        try:
            stream = _resolve(gate, session, tid, quality)
        except (GatePaused, AuthStop):
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {_safe_message(exc)}"
        lossless = stream is not None and not quality_facts.is_lossy(stream.codec)
        served = stream.audio_quality if stream else None
        log_event(conn, "fetch.edition_probe", item.target_rel,
                  f"Planned Tidal track {item.tidal_track_id} of '{item.title}' by {item.artist} was served lossy; Tidal "
                  f"track {tid} of the same recording: " + (f"{served} ({stream.codec})" if stream else error), {
                      "planned_tidal_track_id": item.tidal_track_id, "tried_tidal_track_id": tid,
                      "quality_requested": quality, "audio_quality": served, "codec": stream.codec if stream else None,
                      "lossless": lossless, **({"error": error} if error else {})})
        conn.commit()
        if lossless:
            return (tid, dur, stream), tried
    return None, tried


def _place(conn, library: Path, staging: Path, item: FetchItem, rel: str, staged: Path, facts: dict, quality: str,
           tidal_id: int, audio_quality: str | None, extra: dict, source: str = "tidal",
           note: str = "") -> tuple[str, str]:
    """Link the verified, tagged `staged` file into the library at `rel`, record it and clean up staging: the tail of a
    fetch, shared by the Tidal path and the fallback sources. (status, note): 'placed', or the `_taken` result when the
    path is occupied (the staged copy then stays). Nothing existing is overwritten; the held lossy sibling of a FLAC is
    removed only after the commit. `extra` is added to the fetch.placed data."""
    dest = library / rel
    if taken := _taken(conn, library, item, rel, staged):
        return taken
    new_dirs = [d for d in (dest.parent, *dest.parent.parents) if not os.path.lexists(d)]  # deepest first
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _move_into_library(staged, dest)
    except BaseException as exc:
        for d in new_dirs:  # only directories this call created, and only while empty
            try:
                d.rmdir()
            except OSError:
                break
        if isinstance(exc, FileExistsError) and (taken := _taken(conn, library, item, rel, staged)):
            return taken  # the path was taken after the check above: the staged copy stays, and the conflict names it
        raise
    try:
        received = _insert_file(conn, item, rel, dest, facts, quality, source)
        log_event(conn, "fetch.placed", rel, f"Placed '{item.title}' by {item.artist} as {received or facts['codec']}"
                  + ("" if source == "tidal" else f" via {source}"),
                  {"tidal_track_id": tidal_id, "recording_id": item.recording_id, "path": rel,
                   "quality_requested": quality, "quality_received": received, "audio_quality": audio_quality,
                   **extra, "source": source})
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise FetchError(f"placed {rel} but the ledger insert failed ({exc}); import-legacy will pick it up") from exc
    try:  # only after the commit; a same-filesystem placement already unlinked the source, a cross-device one did not
        staged.unlink(missing_ok=True)  # already gone after a same-filesystem link
    except OSError as exc:
        log_event(conn, "fetch.staged_cleanup_failed", rel, f"Placed '{item.title}' by {item.artist} and recorded it, "
                  f"but could not remove the staged copy {staged} ({_safe_message(exc)}); remove it by hand",
                  {"path": rel, "staged": str(staged)})
        conn.commit()
    held = _staged(staging, item, rel, ".m4a")  # only this exact staged sibling, never a library file
    if os.path.lexists(held):
        try:
            held.unlink()
            log_event(conn, "fetch.stale_held_removed", rel, f"Removed the lossy staged copy of '{item.title}' by "
                      f"{item.artist} because a FLAC was placed", {"path": rel, "staged": str(held)})
        except Exception as exc:  # the placement stands either way
            log_event(conn, "fetch.stale_held_remove_failed", rel, f"Could not remove the lossy staged copy {held}: "
                      f"{_safe_message(exc)}", {"path": rel, "staged": str(held)})
        conn.commit()
    return "placed", note


def _isrc(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def _accept(item: FetchItem, name: str, acq: Acquired, staging: Path, frel: str, ffmpeg: str | None) -> tuple[Path, dict]:
    """Vet what a fallback produced: a FLAC mutagen can open, whose ISRC tag (if it has one and the plan has one)
    agrees, that decodes and has the planned length. Verification is mandatory here, whatever `verify` says. The file is
    copied to staging (never moved) before it is tagged. Returns (staged path, file facts) or raises FallbackMiss/FetchError."""
    if acq.path.suffix.lower() != ".flac":
        raise FallbackMiss("not FLAC")
    try:
        found = (mutagen.flac.FLAC(acq.path).get("isrc") or [""])[0].strip()  # read before _tag overwrites it
    except Exception:
        raise FallbackMiss("not FLAC") from None
    if found and item.isrc and _isrc(found) != _isrc(item.isrc):
        raise FallbackMiss(f"ISRC mismatch (file {found}, planned {item.isrc})")
    if not ffmpeg:
        raise FallbackMiss("ffmpeg not found: fallback files are always verified")
    p = Path(frel)
    staged = staging / p.with_name(f"{p.stem} [{name}].flac")
    staged.parent.mkdir(parents=True, exist_ok=True)
    part = staged.with_name(staged.name + ".part")
    try:
        shutil.copy2(acq.path, part)
        _verify(part, ffmpeg, item.duration_s)
    except BaseException:
        part.unlink(missing_ok=True)  # only our own .part, never the source
        raise
    os.replace(part, staged)
    try:
        _tag(staged, item)
        facts = _file_facts(staged, _NO_STREAM)
        if quality_facts.facts(facts["codec"], facts["sample_rate"], facts["bit_depth"], facts["bitrate"])[0]:
            raise FallbackMiss("not lossless")
    except BaseException:
        staged.unlink(missing_ok=True)  # Cadence's own staged copy of the fallback file; the source is never touched
        raise
    return staged, facts


def _try_one(conn, gate: Gate, item: FetchItem, fb, staging: Path, library: Path, quality: str, ffmpeg: str | None,
             frel: str) -> tuple[str, str] | None:
    """One fallback: None on a miss (logged and committed), else the `_place` result. GatePaused and AuthStop pass up."""
    root = staging / ".fallback"
    root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(dir=root, prefix=f"{fb.name}-{item.tidal_track_id}-"))
    try:
        acq = fb.acquire(item, workdir, gate)
        staged, facts = _accept(item, fb.name, acq, staging, frel, ffmpeg)
    except (GatePaused, AuthStop):
        raise
    except VpnDown as exc:  # this source is off for the rest of the run (_try_fallbacks skips it from now on)
        fb.disabled = reason = str(exc)
        log_event(conn, "fetch.vpn_down", frel, f"{fb.name} stopped for this run: {reason}",
                  {"source": fb.name, "reason": reason, "tidal_track_id": item.tidal_track_id, "path": frel})
        conn.commit()
        return None
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (FallbackMiss, FetchError)) else f"{type(exc).__name__}: {_safe_message(exc)}"
        log_event(conn, "fetch.fallback_miss", frel, f"{fb.name} had nothing usable for '{item.title}' by {item.artist}: "
                  f"{reason}", {"source": fb.name, "reason": reason, "tidal_track_id": item.tidal_track_id,
                                "recording_id": item.recording_id, "path": frel})
        conn.commit()
        return None
    finally:  # Cadence made the workdir; an Orpheus one holds only its output, a Soulseek one is empty
        if getattr(fb, "owns_workdir", False):
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            try:
                workdir.rmdir()
            except OSError:
                pass
    return _place(conn, library, staging, item, frel, staged, facts, quality, item.tidal_track_id, None,
                  {"fallback_ref": acq.ref, "planned_tidal_track_id": item.tidal_track_id, **acq.detail}, fb.name,
                  f"{VIA}{fb.name} ({acq.ref})")


def _try_fallbacks(conn, gate: Gate, item: FetchItem, fallbacks: list, staging: Path, library: Path, quality: str,
                   ffmpeg: str | None) -> tuple[tuple[str, str] | None, list[str]]:
    """Where Tidal served only lossy, ask each fallback in order for a lossless copy of the same recording. Returns
    (result, names tried): the `_place` result of the first that works (or a conflict/exists from the target check),
    else None and the caller holds the lossy copy as before. Without a planned duration nothing is called: sources are
    matched by search and the length is the anchor."""
    frel = item.target(".flac")
    if item.duration_s is None:
        log_event(conn, "fetch.fallback_skipped", frel, f"No fallback tried for '{item.title}' by {item.artist}: its "
                  "duration is unknown, so a search result cannot be checked", {"tidal_track_id": item.tidal_track_id,
                                                                               "path": frel, "reason": "duration unknown"})
        conn.commit()
        return None, []
    if taken := _taken(conn, library, item, frel):
        return taken, []
    tried: list[str] = []
    for fb in fallbacks:
        if getattr(fb, "disabled", None):
            continue
        tried.append(fb.name)
        if done := _try_one(conn, gate, item, fb, staging, library, quality, ffmpeg, frel):
            return done, tried
    return None, tried


def _fetch_one(conn, session, gate: Gate, http, item: FetchItem, staging: Path, library: Path, quality: str,
               verify: bool, ffmpeg: str | None, fallbacks: list = ()) -> tuple[str, str]:
    """(status, note): status is 'placed', 'exists', 'conflict' (the target is taken by a file that is not this track's)
    or 'held_lossy' (a lossy file stays in staging unless HIGH was requested; `fallbacks` are tried first). Raises GatePaused, AuthStop, Blocked, FetchError or StreamRefused."""
    rel = item.target_rel
    if not _safe_rel(rel):
        raise FetchError(f"unsafe target path '{rel}'")
    if taken := _taken(conn, library, item, rel):
        return taken
    held = _staged(staging, item, rel, ".m4a")  # where a lossy result for this track is held
    already_held = quality in ("LOSSLESS", "HI_RES_LOSSLESS") and os.path.lexists(held)
    stream = None if already_held else _resolve(gate, session, item.tidal_track_id, quality)  # a held track is not asked again
    src, tried = item, []  # src: the Tidal track the bytes come from, the planned one unless another edition is lossless
    if quality in ("LOSSLESS", "HI_RES_LOSSLESS") and (already_held or quality_facts.is_lossy(stream.codec)):
        alt, tried = _probe_editions(conn, session, gate, item, quality)
        if alt:
            src = replace(item, tidal_track_id=alt[0], duration_s=alt[1] if item.duration_s is None else item.duration_s)
            stream = alt[2]
        elif already_held:  # no other edition is lossless: the held copy stays as it is
            if fallbacks and (done := _try_fallbacks(conn, gate, item, fallbacks, staging, library, quality, ffmpeg)[0]):
                return done
            return "held_lossy", f"already held in staging, not fetched again: {held}"
    if stream.ext != Path(rel).suffix:
        rel = item.target(stream.ext)
        if taken := _taken(conn, library, item, rel):
            return taken
    staged = _staged(staging, src, rel, stream.ext)
    staged.parent.mkdir(parents=True, exist_ok=True)
    part = staged.with_name(staged.name + ".part")
    if stream.segmented:
        gate.call(_download_segments, http, stream, part, ffmpeg)
    else:
        gate.call(_download_direct, http, stream.urls[0], part)
    if verify:  # the new bytes are checked as the .part file, so a bad download never touches a staged or held copy
        try:
            _verify(part, ffmpeg, src.duration_s)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    os.replace(part, staged)  # staging only; a leftover from an earlier failed try is overwritten
    _tag(staged, item)
    facts = _file_facts(staged, stream)
    lossy, received = quality_facts.facts(facts["codec"], facts["sample_rate"], facts["bit_depth"], facts["bitrate"])
    # lossless-only placement: keep it in staging, no library file, no file row
    if lossy and quality in LOSSLESS:
        done, ftried = _try_fallbacks(conn, gate, item, fallbacks, staging, library, quality, ffmpeg) if fallbacks else (None, [])
        if done:
            return done  # a FLAC was placed (or its target is taken); the existing tail removes the held .m4a
        note = f"{staged} ({received or facts['codec']})"
        log_event(conn, "fetch.held_lossy", rel, f"Held '{item.title}' by {item.artist} in staging: got lossy "
                  f"{received or facts['codec']} but {quality} was requested", {
                      "tidal_track_id": item.tidal_track_id, "recording_id": item.recording_id, "path": rel,
                      "staged": str(staged), "quality_requested": quality, "quality_received": received,
                      "audio_quality": stream.audio_quality, "alternates_tried": tried,
                      **({"fallbacks_tried": ftried} if ftried else {})})
        conn.commit()
        return "held_lossy", note
    return _place(conn, library, staging, item, rel, staged, facts, quality, src.tidal_track_id, stream.audio_quality,
                  {"planned_tidal_track_id": item.tidal_track_id} if src is not item else {})


# ---- the run ----

def _report_plan(plan: Plan | None, items: list[FetchItem], out) -> None:
    """The planner's skipped tracks (one line each, with reason and Tidal id), the collisions of the items about to be
    attempted, and its planned/owned/skipped/collision counts."""
    if plan is not None:
        for s in plan.skipped:
            out(s.line())
        ids = {i.tidal_track_id for i in items}
        fiio = {i.tidal_track_id for i in items if i.dest == "fiio"}
        for c in plan.collisions:
            if c.tidal_track_id in ids:
                out(c.line(c.tidal_track_id in fiio))
        for line in plan.summary_lines():
            out(line)


def _dry_run(conn, items: list[FetchItem], library_root: Path, quality: str, out, plan: Plan | None) -> dict:
    res = {"dry_run": True, "would_fetch": 0, "exists": 0, "conflict": 0, "skipped_dest": 0, "placed": 0, "held_lossy": 0,
           "blocked": 0, "failed": 0, "stopped": None, "until": None, "remaining": []}
    ext = ext_for_quality(quality)
    out(f"fetch (dry run, quality {quality}): {len(items)} item(s); nothing is downloaded or written. "
        "Re-run with --execute to fetch.")
    later_dups = {c.tidal_track_id for c in plan.collisions if c.kind == "duplicate"} if plan else set()
    fetching: set[str] = set()  # casefolded targets of the items above that would be fetched
    for it in items:
        rel = it.target(ext)
        taken = _occupied(conn, library_root, it, rel)
        if not taken and it.tidal_track_id in later_dups and rel.casefold() in fetching:
            taken = "conflict"  # execute has placed the earlier item by now, so it finds the path taken
        if it.dest == "fiio" and taken != "conflict":  # as at execute: a taken path is a conflict whatever the destination
            state, key = "skip (fiio-only)", "skipped_dest"
        elif taken:
            state, key = ("exists, skip" if taken == "exists" else "conflict"), taken
        else:
            state, key = "would fetch", "would_fetch"
            fetching.add(rel.casefold())
        res[key] += 1
        out(f"  {state:<16} {rel}  [tidal {it.tidal_track_id}, {_fmt_dur(it.duration_s)}, dest {it.dest}]")
    _report_plan(plan, items, out)
    out(f"fetch: {res['would_fetch']} would be fetched, {res['exists']} already exist, {res['conflict']} conflict, "
        f"{res['skipped_dest']} fiio-only skipped")
    return res


def fetch_items(conn: sqlite3.Connection, session, gate: Gate | None, items: list[FetchItem], staging_dir,
                library_root, dry_run: bool = True, quality: str = DEFAULT_QUALITY, verify: bool = True, *,
                http=None, ffmpeg: str | None = None, out=print, plan: Plan | None = None,
                fallbacks: list | None = None) -> dict:
    """Result keys: placed, exists, conflict, held_lossy, blocked, failed, skipped_dest, stopped (None | paused | auth | network |
    setup), until, remaining (items not attempted, including the one a stop happened on). `fallbacks` (fallback.py) are
    tried, in order, for tracks Tidal serves lossy under a lossless request; when any are given the result also has
    via_fallback (placed items whose file came from one), and their check() runs once in the preflight."""
    library_root = Path(library_root)
    if dry_run:  # `plan` is the planner's result: its skipped tracks and counts are printed with the items
        return _dry_run(conn, items, library_root, quality, out, plan)
    staging_dir = Path(staging_dir)
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    res = {"dry_run": False, "placed": 0, "exists": 0, "conflict": 0, "held_lossy": 0, "blocked": 0, "failed": 0,
           "skipped_dest": 0, "stopped": None, "until": None, "remaining": []}
    active = list(fallbacks or []) if quality in LOSSLESS else []  # HIGH never calls a fallback
    if fallbacks:
        res.update(via_fallback=0, vpn_down=0)
    problem = _preflight(staging_dir, library_root, verify or bool(active), ffmpeg)  # fallback files are always verified
    for fb in active if not problem else ():
        if found := fb.check():
            problem = f"fallback {fb.name}: {found}"
            break
        if reason := getattr(fb, "disabled", None):  # its VPN is down: only this source is off, the run goes on
            out(f"fallback {fb.name}: off for this run ({reason})")
            log_event(conn, "fetch.vpn_down", fb.name, f"{fb.name} stopped for this run: {reason}",
                      {"source": fb.name, "reason": reason, "stage": "preflight"})
            conn.commit()
        elif ip := getattr(fb, "public_ip", None):  # the VPN's exit address is fine to show; the keys never are
            out(f"fallback {fb.name}: VPN running, public IP {ip}")
            log_event(conn, "fetch.vpn_up", fb.name, f"{fb.name} VPN is running, public IP {ip}",
                      {"source": fb.name, "public_ip": ip})
            conn.commit()
    if problem:
        _report_plan(plan, items, out)  # the plan is already built: say what it decided before the setup error
        out(f"fetch: {problem}")
        return {**res, "stopped": "setup", "remaining": list(items)}
    http = http or requests.Session()
    network_failures = 0
    for i, it in enumerate(items):
        if it.dest == "fiio":
            if (taken := _taken(conn, library_root, it, it.target_rel)) and taken[0] == "conflict":
                res["conflict"] += 1  # not fetched here, but a taken path is a conflict whatever the destination
                out(f"  {'conflict':<10} {it.target_rel}  ({taken[1]})")
                continue
            res["skipped_dest"] += 1
            out(f"  skip (fiio-only, not handled yet) {it.target_rel}")
            continue
        try:
            status, note = _fetch_one(conn, session, gate, http, it, staging_dir, library_root, quality, verify, ffmpeg,
                                      active)
            res[status] += 1
            if status == "placed" and note.startswith(VIA):
                res["via_fallback"] += 1
            out(f"  {status:<10} {it.target_rel}" + (f"  ({note})" if note else ""))
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
            out(f"  blocked    {it.target_rel}")
            network_failures = 0
        except Exception as exc:
            conn.rollback()
            msg = f"{type(exc).__name__}: {_safe_message(exc)}"
            res["failed"] += 1
            log_event(conn, "fetch.failed", str(it.tidal_track_id), f"'{it.title}' by {it.artist}: {msg}",
                      {"tidal_track_id": it.tidal_track_id, "path": it.target_rel})
            conn.commit()
            out(f"  failed     {it.target_rel}  ({msg})")
            network_failures = network_failures + 1 if _is_dead_connection(exc) else 0
            if network_failures >= MAX_NETWORK_FAILURES:
                res.update(stopped="network", remaining=list(items[i + 1:]))
                break
    if fallbacks:
        res["vpn_down"] = sum(1 for fb in active if getattr(fb, "disabled", None))
    _report_plan(plan, items, out)
    out(f"fetch: {res['placed']} placed, {res['exists']} already existed, {res['conflict']} conflict, "
        f"{res['held_lossy']} held (lossy), " + (f"{res['via_fallback']} via fallback, {res['vpn_down']} vpn down, " if fallbacks else "") +
        f"{res['blocked']} blocked, "
        f"{res['failed']} failed, {res['skipped_dest']} fiio-skipped, {len(res['remaining'])} not attempted, "
        f"stopped={res['stopped']}")
    for it in res["remaining"]:
        out(f"  remaining {it.target_rel}")
    return res
