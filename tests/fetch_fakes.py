"""Test doubles and ledger seeding for the fetch tests. No network."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from datetime import timedelta
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


# ---- fallback sources ----

class FakeFallback:
    """A Fallback double. `result` is an Acquired, an exception to raise, or a callable (item, workdir) -> either."""

    def __init__(self, name="orpheus", result=None, problem=None, owns_workdir=False, disable_on_check=None):
        self.name, self.result, self.problem, self.owns_workdir = name, result, problem, owns_workdir
        self.disable_on_check = disable_on_check  # like a Soulseek whose VPN is down at preflight
        self.checked = 0
        self.calls: list[tuple] = []

    def check(self):
        self.checked += 1
        if self.disable_on_check:
            self.disabled = self.disable_on_check
        return self.problem

    def acquire(self, item, workdir, gate):
        self.calls.append((item, workdir, gate))
        out = self.result(item, workdir) if callable(self.result) else self.result
        if isinstance(out, BaseException):
            raise out
        return out


class FakeApiResp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data if data is not None else {}

    def json(self):
        return self._data


def sl_file(name, length=187, size=30_000_000, bit=16, locked=False, ext="flac") -> dict:
    return {"filename": name, "size": size, "length": length, "bitDepth": bit, "sampleRate": 44100,
            "extension": ext, "isLocked": locked}


def sl_resp(user, *files, free=True, queue=0, speed=1000) -> dict:
    return {"username": user, "hasFreeUploadSlot": free, "queueLength": queue, "uploadSpeed": speed,
            "files": list(files)}


class FakeSlskd:
    """requests.Session stand-in for the slskd REST API and the gluetun control server (one fake, routed by URL).
    `states` maps a remote filename to the list of transfer states served on successive polls (the last repeats); `files`
    maps a remote filename to the bytes slskd writes into `downloads/<last remote dir>/<basename>` once it reports
    'Completed, Succeeded' (`appear_after` seconds later). `sleep` is the injectable sleep: it advances the fake clock and
    lets pending files appear. `enqueue_ids`: the enqueue answer names the transfer id (a string names a wrong one; False names none). `stale` maps a
    filename to old transfer records served next to ours. `vpn` is the list of gluetun statuses served on successive
    /v1/vpn/status calls (the last repeats); `vpn_code` a non-200 answer for it; `redirect` path prefixes answered 302."""

    def __init__(self, ft, downloads, responses=(), states=None, files=None, appear_after=0.0, app_status=200,
                 raise_exc=None, enqueue_ids=True, stale=None, vpn=("running",), vpn_code=200, public_ip="203.0.113.7",
                 redirect=(), clock_skew=0.0):
        self.ft, self.downloads = ft, downloads
        self.responses, self.states, self.files = list(responses), states or {}, files or {}
        self.appear_after, self.app_status, self.raise_exc = appear_after, app_status, raise_exc
        self.enqueue_ids, self.stale, self.redirect = enqueue_ids, stale or {}, tuple(redirect)
        self.clock_skew = clock_skew  # seconds slskd's clock runs behind ours (requestedAt is stamped this far back)
        self.vpn, self.vpn_code, self.public_ip = list(vpn), vpn_code, public_ip
        self.calls: list[tuple] = []  # (method, url, json, headers, timeout, allow_redirects)
        self.polls: dict[str, int] = {}
        self.vpn_polls = 0
        self.posted: dict[str, str] = {}
        self.pending: list[tuple] = []

    def sleep(self, s):
        self.ft.sleep(s)
        self._flush()

    def _flush(self):
        for item in list(self.pending):
            ready, path, data, _fn = item
            if self.ft.t >= ready:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self.pending.remove(item)

    def _gluetun(self, method, path):
        if method != "get":
            return FakeApiResp(405)
        if path == "/vpn/status":
            if self.vpn_code != 200:
                return FakeApiResp(self.vpn_code)
            i, self.vpn_polls = self.vpn_polls, self.vpn_polls + 1
            return FakeApiResp(200, {"status": self.vpn[min(i, len(self.vpn) - 1)]})
        if path == "/publicip/ip":
            return FakeApiResp(200, {"public_ip": self.public_ip})
        return FakeApiResp(404)

    def _do(self, method, url, json=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((method, url, json, headers, timeout, allow_redirects))
        if self.raise_exc:
            raise self.raise_exc
        if "/v1/" in url:
            return self._gluetun(method, url.split("/v1", 1)[1])
        path = url.split("/api/v0", 1)[1]
        if any(path.startswith(p) for p in self.redirect):
            return FakeApiResp(302)
        if method == "get" and path == "/application":
            return FakeApiResp(self.app_status)
        if method == "post" and path == "/searches":
            return FakeApiResp(200, {})
        if method == "get" and path.endswith("/responses"):
            return FakeApiResp(200, self.responses)
        if method == "get" and path.startswith("/searches/"):
            return FakeApiResp(200, {"isComplete": True})
        if method == "post" and path.startswith("/transfers/downloads/"):
            for b in json:
                self.posted[b["filename"]] = (self.ft.now() - timedelta(seconds=self.clock_skew)).isoformat()
            ident = (lambda b: self.enqueue_ids) if isinstance(self.enqueue_ids, str) else (lambda b: f"id-{b['filename']}")
            return FakeApiResp(201, {"enqueued": [{"id": ident(b), "filename": b["filename"]} for b in json]
                                     if self.enqueue_ids else [], "failed": []})
        if method == "get" and path.startswith("/transfers/downloads/"):
            data = self._transfers(path.rsplit("/", 1)[1])
            return FakeApiResp(404) if data is None else FakeApiResp(200, data)
        if method == "delete":
            return FakeApiResp(204)
        return FakeApiResp(404)

    def _transfers(self, user):
        enq = {b["filename"] for m, u, j, *_ in self.calls if m == "post" and u.endswith("/transfers/downloads/" + user)
               for b in j}
        out = [r for recs in self.stale.values() for r in recs]  # records slskd already had, before any POST
        if not out and not enq:
            return None  # slskd answers 404 for a user it has no transfers with
        for fn in sorted(enq):
            seq = self.states.get(fn, ["Queued, Remotely"])
            i = self.polls.get(fn, 0)
            self.polls[fn] = i + 1
            state = seq[min(i, len(seq) - 1)]
            if state == "Completed, Succeeded" and fn in self.files and not any(fn == p[3] for p in self.pending):
                base = fn.replace("\\", "/").split("/")
                dest = self.downloads / base[-2] / base[-1]
                if not dest.exists():
                    self.pending.append((self.ft.t + self.appear_after, dest, self.files[fn], fn))
                    self._flush()
            out.append({"id": f"id-{fn}", "filename": fn, "state": state, "size": 1, "bytesTransferred": 0,
                        "requestedAt": self.posted[fn]})
        return {"username": user, "directories": [{"directory": "d", "files": out}]}

    def get(self, url, **kw):
        return self._do("get", url, **kw)

    def post(self, url, **kw):
        return self._do("post", url, **kw)

    def delete(self, url, **kw):
        return self._do("delete", url, **kw)
