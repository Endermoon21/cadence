"""Test doubles and ledger seeding for the fetch tests. No network."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from types import SimpleNamespace as NS

import requests

from cadence.ledger import db

FFMPEG = shutil.which("ffmpeg")


def ledger():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def add_artist(conn, name="Neil Young", tidal_id=1, match="auto", dest="server") -> int:
    return conn.execute("INSERT INTO artist(name, norm_name, tidal_id, tidal_match, default_dest) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (name, name.lower(), tidal_id, match, dest)).lastrowid


def add_release(conn, artist_id, title, tidal_ref, type="album", date="2000-01-01", role="own") -> int:
    rid = conn.execute("INSERT INTO release(artist_id, title, norm_title, type, role, release_date) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (artist_id, title, title.lower(), type, role, date)).lastrowid
    conn.execute("INSERT INTO release_source(release_id, provider, provider_ref) VALUES (?, 'tidal', ?)",
                 (rid, str(tidal_ref)))
    return rid


def add_recording(conn, artist_id, title, isrc=None, variant=None, canon_id=None, dur=4) -> int:
    return conn.execute("INSERT INTO recording(isrc, artist_id, title, norm_title, variant, duration_s, canon_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (isrc, artist_id, title, title.lower(), variant, dur, canon_id)
                        ).lastrowid


def add_track(conn, release_id, recording_id, number, title, tidal_id, dur=4, disc=1, version=None) -> int:
    return conn.execute("INSERT INTO track(release_id, recording_id, tidal_id, disc, number, title, version, "
                        "duration_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (release_id, recording_id, tidal_id, disc, number, title, version, dur)).lastrowid


def add_file(conn, path, artist_id, recording_id=None) -> None:
    conn.execute("INSERT INTO file(path, artist_id, recording_id, match) VALUES (?, ?, ?, ?)",
                 (path, artist_id, recording_id, "isrc" if recording_id else "unmatched"))


# ---- Tidal session and CDN ----

def bts_body(url="https://cdn.example/t.flac", codec="flac", **kw) -> dict:
    manifest = {"mimeType": "audio/flac", "codecs": codec, "encryptionType": "NONE", "urls": [url]}
    manifest.update(kw.pop("manifest", {}))
    body = {"trackId": 1, "audioMode": "STEREO", "audioQuality": "LOSSLESS", "assetPresentation": "FULL",
            "manifestMimeType": "application/vnd.tidal.bts", "bitDepth": 16, "sampleRate": 44100,
            "manifest": base64.b64encode(json.dumps(manifest).encode()).decode()}
    body.update(kw)
    return body


def mpd_body(xml: str, **kw) -> dict:
    body = {"trackId": 1, "audioMode": "STEREO", "audioQuality": "HI_RES_LOSSLESS", "assetPresentation": "FULL",
            "manifestMimeType": "application/dash+xml", "manifest": base64.b64encode(xml.encode()).decode()}
    body.update(kw)
    return body


def http_error(status: int, body: dict | None = None) -> requests.HTTPError:
    resp = NS(status_code=status, json=lambda: body or {}, headers={})
    return requests.HTTPError(f"{status} error", response=resp)


class FakeTidal:
    """Looks like tidalapi.Session for session.request.request. `by_track` maps track id -> body or exception."""

    def __init__(self, by_track: dict | None = None, default=None):
        self.by_track = by_track or {}
        self.default = default
        self.calls: list[tuple[str, dict]] = []
        self.request = NS(request=self._request)

    def _request(self, method, path, params=None):
        self.calls.append((path, dict(params or {})))
        track = int(path.split("/")[1])
        out = self.by_track.get(track, self.default)
        if isinstance(out, BaseException):
            raise out
        return NS(json=lambda: out)


class FakeResp:
    def __init__(self, data: bytes):
        self.data = data
        self.headers = {"Content-Length": str(len(data))}

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]

    def close(self):
        pass


class FakeHttp:
    """requests.Session stand-in. `files` maps url -> bytes or an exception to raise."""

    def __init__(self, files: dict | None = None, default: bytes | None = None):
        self.files = files or {}
        self.default = default
        self.urls: list[str] = []

    def get(self, url, stream=False, timeout=None):
        self.urls.append(url)
        out = self.files.get(url, self.default)
        if isinstance(out, BaseException):
            raise out
        return FakeResp(out)


def make_flac(path, seconds: int = 4) -> bytes:
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}:sample_rate=44100",
                    "-c:a", "flac", "-sample_fmt", "s16", "-map_metadata", "-1", "-fflags", "+bitexact",
                    "-flags:a", "+bitexact", str(path)], check=True)
    return path.read_bytes()


def make_aac(path, seconds: int = 4) -> bytes:
    """A lossy AAC-in-m4a stand-in for a HIGH stream (or a lossless request that came back lossy)."""
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}:sample_rate=44100",
                    "-c:a", "aac", "-b:a", "128k", "-map_metadata", "-1", str(path)], check=True)
    return path.read_bytes()


def split_fmp4(path, tmp) -> list[bytes]:
    """A fragmented MP4 holding FLAC, split like a DASH stream: [init (ftyp+moov), media fragment ...]."""
    frag = tmp / "frag.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(path), "-c:a", "copy", "-movflags",
                    "frag_keyframe+empty_moov+default_base_moof", str(frag)], check=True)
    data, boxes, pos = frag.read_bytes(), [], 0
    while pos < len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        boxes.append((data[pos + 4:pos + 8], data[pos:pos + size]))
        pos += size
    init = b"".join(b for kind, b in boxes if kind in (b"ftyp", b"moov"))
    rest = [b for kind, b in boxes if kind not in (b"ftyp", b"moov")]
    return [init, *rest]
