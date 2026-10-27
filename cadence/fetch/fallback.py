"""Fallback sources for tracks Tidal serves lossy: OrpheusDL (a subprocess) and Soulseek (a running slskd).

A fallback only produces a local audio file; run.py checks it (FLAC, ISRC, decode, duration) and places it. Sources are
matched by search, so the planned length is the anchor. Nothing here writes outside the workdir it is given, and nothing
deletes a file. The slskd and gluetun API keys are only ever sent as headers: they are never logged, printed or put in an
exception. slskd must run behind gluetun (a Mullvad VPN); Soulseek is refused unless the tunnel is up."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol
from urllib.parse import quote, urlparse

import mutagen.flac
import requests

from cadence.fetch.plan import FetchItem
from cadence.gate.bucket import Gate
from cadence.identity.normalize import VARIANT_WORDS, fold, norm_artist, norm_title

DURATION_TOLERANCE_S = 3
HTTP_TIMEOUT = (10, 30)
SEARCH_CAP_S = 60
LOCATE_CAP_S = 30
POLL_S = 2
# Variant words: normalize's own list plus the ones that mark a different recording of the same song. Each matches with
# its usual endings (remix/remixed/remixes, demo/demos ...) as whole words; "live" and a few others only as they are.
_EXTRA_VARIANTS = ("tribute", "cover", "unplugged", "session", "concert", "karaoke", "slowed", "sped")
_ENDINGS = {"remix": "(?:e[sd])?", "edit": "(?:s|ed)?", "mix": "(?:es)?", "live": "", "sped": "", "slowed": "",
            "extended": "", "reverb": "", "stripped": "", "orchestral": "", "unplugged": "", "karaoke": "", "acoustic": ""}
_VARIANTS = {w: re.compile(r"\b" + r"\s+".join(map(re.escape, w.split())) + _ENDINGS.get(w, "s?") + r"\b")
             for w in dict.fromkeys((*VARIANT_WORDS, *_EXTRA_VARIANTS))}
_ARTIST_SPLIT = re.compile(r"\s*(?:&|,|;|/|\band\b|\bfeat\b\.?|\bft\b\.?|\bwith\b|\bx\b|\bvs\b\.?)\s*", re.I)
_RATE_LIMITED = re.compile(r"429 client error|too ?many ?requests|status code:? 429|http:? ?429", re.I)
NAMES = ("orpheus", "soulseek")


@dataclass(frozen=True)
class Acquired:
    path: Path         # a local audio file the source produced
    ref: str           # human/audit reference, e.g. "qobuz luckysearch: Artist Title" or "user123:Music\\A\\01 x.flac"
    detail: dict = field(default_factory=dict)  # extra event data (no secrets)


class FallbackMiss(Exception):
    """This source has nothing usable for this item; try the next."""


class VpnDown(Exception):
    """The VPN in front of slskd is not running (or cannot be asked): Soulseek is off for the rest of the run."""


class Fallback(Protocol):
    name: str                                  # 'orpheus' | 'soulseek' (also the file.source value)

    def check(self) -> str | None: ...         # preflight problem or None (called once per --execute run)

    def acquire(self, item: FetchItem, workdir: Path, gate: Gate) -> Acquired: ...  # raises FallbackMiss


# ---- matching helpers ----

def _tokens(text: str) -> list[str]:
    return fold(text).split()


def _variants(tokens) -> set[str]:
    """The variant words (remix, live, tribute ...) among these words, endings allowed."""
    text = " ".join(tokens)
    return {k for k, rx in _VARIANTS.items() if rx.search(text)}


def _allowed_variants(item: FetchItem) -> set[str]:
    """Variants the planned title (or the artist's own name, e.g. the band Live) already has."""
    return _variants(_tokens(item.title) + _tokens(item.artist))


def _planned(item: FetchItem) -> tuple[list[str], list[str]]:
    return _tokens(norm_title(item.title)), _tokens(norm_artist(item.artist))


def _last_line(text: str) -> str:
    """At most the last line of tool output, for events and stdout: URL query strings and long token-like runs
    (keys, ids) become '…'."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    line = re.sub(r"(https?://[^\s?]*)\?\S*", r"\1…", lines[-1] if lines else "")
    return re.sub(r"[A-Za-z0-9+/=_-]{20,}", "…", line)[:200]


def _flac_tags(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in (mutagen.flac.FLAC(path).tags or {}).items():  # mutagen gives each key its list of values
        out.setdefault(k.lower(), []).extend([v] if isinstance(v, str) else (str(x) for x in v))
    return out


def _tag_problem(item: FetchItem, tags: dict) -> str | None:
    """Why a search-found file is not the planned track, judged by its own tags (None when it is): the title (with its
    version) equals the planned one, the artist tags name the planned artist, and no variant word the plan lacks."""
    want_t, want_a = _planned(item)
    if not want_t or not want_a:
        return "planned title or artist has no searchable words"
    title = ((tags.get("title") or [""])[0] or "").strip()
    if not title:
        return "missing title tag"
    version = ((tags.get("version") or [""])[0] or "").strip() or None
    if norm_title(title, version) != norm_title(item.title):
        return "title tag does not match the planned title"
    credited = [*tags.get("artist", []), *tags.get("albumartist", []), *tags.get("artists", [])]
    if not any(_tokens(norm_artist(part)) == want_a for v in credited for part in (v, *_ARTIST_SPLIT.split(v))):
        return "artist tag does not name the planned artist"
    stray = _variants(_tokens(" ".join([title, version or "", *tags.get("album", [])]))) - _allowed_variants(item)
    return f"tags mention {', '.join(sorted(stray))}, which the planned track does not" if stray else None


# ---- OrpheusDL ----

def _flacs(workdir: Path) -> list[Path]:
    return sorted(p for p in workdir.rglob("*") if p.is_file() and p.suffix.lower() == ".flac")


class _RateLimited(Exception):
    """Orpheus reported a Tidal 429; shaped like a requests error so the shared gate pauses on it."""
    response = SimpleNamespace(status_code=429, headers={})


class OrpheusFallback:
    """The user's own OrpheusDL checkout, run as a subprocess. Its config/settings.json (credentials, download_quality)
    is never read here. Its exit code is unreliable (several failure paths call a bare exit()), so success is "exactly one
    .flac under the workdir after the run". Module 'tidal' downloads the planned track id through the shared Tidal gate
    (an error-shaped 429 in its output, with no FLAC made, pauses that gate); any other module uses luckysearch (first hit, never prompts), so the file's
    own tags must then agree with the plan before it is accepted (`read_tags` is injectable)."""
    name = "orpheus"
    owns_workdir = True  # Cadence made the workdir, so Cadence removes it

    def __init__(self, directory: str, module: str, python: str = "python3", timeout: int = 900, run=subprocess.run,
                 read_tags=_flac_tags):
        self.dir, self.module, self.python, self.timeout, self.run = str(directory), module, python, timeout, run
        self.read_tags = read_tags

    def check(self) -> str | None:
        if not (Path(self.dir) / "orpheus.py").is_file():
            return f"no orpheus.py in {self.dir} (CADENCE_ORPHEUS_DIR)"
        if not (os.path.isfile(self.python) or shutil.which(self.python)):
            return f"python '{self.python}' not found (CADENCE_ORPHEUS_PYTHON)"
        return None

    def _command(self, item: FetchItem, workdir: Path) -> tuple[list[str], str]:
        base = [self.python, "orpheus.py", "-o", str(workdir)]
        if self.module == "tidal":
            return [*base, "download", "tidal", "track", str(item.tidal_track_id)], f"tidal track {item.tidal_track_id}"
        query = f"{item.artist} {item.title}"
        return [*base, "luckysearch", self.module, "track", query], f"{self.module} luckysearch: {query}"

    def _run(self, cmd: list[str], workdir: Path):
        r = self.run(cmd, cwd=self.dir, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=self.timeout)
        if self.module == "tidal" and not _flacs(workdir) and _RATE_LIMITED.search(f"{r.stderr or ''}\n{r.stdout or ''}"):
            raise _RateLimited("orpheus reported HTTP 429 from Tidal")  # inside gate.call: the gate pauses
        return r

    def acquire(self, item: FetchItem, workdir: Path, gate: Gate) -> Acquired:
        cmd, ref = self._command(item, workdir)
        try:
            r = gate.call(self._run, cmd, workdir) if self.module == "tidal" else self._run(cmd, workdir)
        except subprocess.TimeoutExpired:
            raise FallbackMiss(f"orpheus timed out after {self.timeout}s") from None
        except OSError as exc:
            raise FallbackMiss(f"could not start orpheus ({type(exc).__name__})") from None
        found = _flacs(workdir)
        if not found:
            tail = _last_line(r.stderr) or _last_line(r.stdout)
            raise FallbackMiss("orpheus produced no FLAC" + (f": {tail}" if tail else ""))
        if len(found) > 1:
            raise FallbackMiss(f"ambiguous output: orpheus produced {len(found)} FLAC files")
        if self.module != "tidal":  # a search result: believe its tags, not the search
            try:
                tags = self.read_tags(found[0])
            except Exception:
                raise FallbackMiss("could not read the tags of the downloaded file") from None
            if problem := _tag_problem(item, tags):
                raise FallbackMiss(problem)
        return Acquired(found[0], ref, {"module": self.module, "returncode": r.returncode})


# ---- Soulseek (slskd) ----

def _basename(filename: str) -> str:
    return filename.replace("\\", "/").rsplit("/", 1)[-1]


def _drop_number(t: tuple) -> tuple:
    return t[1:] if t and t[0].isdigit() and len(t[0]) <= 3 else t


def _title_forms(stem: str, artist: list[str]) -> set[tuple]:
    """What a file's name may say the title is: its words as they are, with a leading track (or disc and track) number
    dropped, and with a leading "<artist> - " dropped, in any order of those."""
    forms = {tuple(_tokens(stem)), tuple(_tokens(norm_title(stem)))}
    for _ in range(2):
        for t in list(forms):
            forms.add(_drop_number(t))
            if artist and t[:len(artist)] == tuple(artist):
                forms.add(t[len(artist):])
    return forms


def _names_artist(name: str, want_a: list[str]) -> bool:
    """The artist's words are a whole folder name (a trailing " - album" and years allowed) or the "<artist> - " part
    of the file name. Words merely present somewhere in the path ("Queen Latifah", "A Tribute to Queen") do not count."""
    *folders, base = re.split(r"[\\/]", name)

    def is_artist(text: str) -> bool:
        return [t for t in _tokens(norm_artist(text)) if not (len(t) == 4 and t.isdigit())] == want_a

    if any(is_artist(re.split(r"\s+-\s+", f)[0]) for f in folders):
        return True
    return any(is_artist(_strip_number(seg)) for seg in re.split(r"\s+-\s+", base.rsplit(".", 1)[0])[:-1])


def _strip_number(text: str) -> str:
    return re.sub(r"^\s*\d{1,3}(?:\s*[-.]\s*\d{1,3})?\s*[-._)]*\s*", "", text)


def _usable(item: FetchItem, f: dict, want_t: list[str], want_a: list[str]) -> bool:
    """An unlocked FLAC whose length is within tolerance, whose file name is the title and nothing more (bar a track
    number and the artist), whose folder or file prefix is the artist, and whose whole path has no variant word (live, remix ...) the
    plan lacks."""
    name = f.get("filename") or ""
    if f.get("isLocked") or not name.lower().endswith(".flac") or f.get("length") is None:
        return False
    if abs(f["length"] - item.duration_s) > DURATION_TOLERANCE_S:
        return False
    if not _names_artist(name, want_a) or _variants(_tokens(name)) - _allowed_variants(item):
        return False
    return tuple(want_t) in _title_forms(_basename(name)[:-5], want_a)


def rank_candidates(item: FetchItem, responses: list[dict]) -> list[tuple[dict, dict]]:
    """(response, file) pairs that pass the filters, best first: free upload slot, short queue, higher bit depth, speed.
    A planned title or artist with no words in it, or no planned duration, matches nothing."""
    want_t, want_a = _planned(item)
    if item.duration_s is None or not want_t or not want_a:
        return []
    pairs = [(r, f) for r in responses for f in r.get("files", []) if _usable(item, f, want_t, want_a)]
    return sorted(pairs, key=lambda p: (not p[0].get("hasFreeUploadSlot"), p[0].get("queueLength") or 0,
                                        -(p[1].get("bitDepth") or 0), -(p[0].get("uploadSpeed") or 0)))


class SoulseekFallback:
    """slskd's REST API: search, rank, enqueue the best few, wait, then find the finished file in slskd's downloads
    directory (as seen from here). The file is left where slskd put it; run.py copies it. slskd must run behind gluetun
    (Mullvad): `gluetun_url`/`gluetun_key` name its control server, which is asked for the VPN status in check() and
    before every acquire(); a tunnel that is not running raises VpnDown and Soulseek stays off for the run.
    `gluetun_url=None` is the explicit opt-out (CADENCE_SLSKD_VPN=off)."""
    name = "soulseek"
    owns_workdir = False

    def __init__(self, url: str, api_key: str, downloads, timeout: int = 900, max_candidates: int = 3, *, http=None,
                 sleep=time.sleep, clock=time.monotonic, gluetun_url: str | None = None, gluetun_key: str | None = None):
        self.url, self.downloads, self.timeout, self.max_candidates = url.rstrip("/"), Path(downloads), timeout, max_candidates
        self._key, self.http, self.sleep, self.clock = api_key, http or requests.Session(), sleep, clock
        self.gluetun_url, self._gkey = (gluetun_url or "").rstrip("/") or None, gluetun_key
        self.public_ip: str | None = None
        self.disabled: str | None = None  # why Soulseek is off for this run (the VPN is down), else None
        self._vpn_down: str | None = None

    # -- the VPN gate --

    def _gluetun(self, path: str) -> tuple[dict | None, str | None]:
        """(answer, problem) for a gluetun control-server route. Problems name the status or exception type only."""
        try:
            resp = self.http.get(f"{self.gluetun_url}/v1{path}", headers={"X-API-Key": self._gkey}, timeout=HTTP_TIMEOUT,
                                 allow_redirects=False)
        except requests.RequestException as exc:
            return None, f"gluetun at {self.gluetun_url} is not reachable ({type(exc).__name__})"
        if resp.status_code == 401:
            return None, self._KEY_REJECTED
        if resp.status_code != 200:
            return None, f"gluetun answered {resp.status_code} to /v1{path}"
        try:
            return dict(resp.json()), None
        except Exception:
            return None, f"gluetun sent an unreadable answer to /v1{path}"

    _KEY_REJECTED = "gluetun rejected the API key"

    def _vpn_problem(self) -> str | None:
        data, problem = self._gluetun("/vpn/status")
        if problem:
            return problem
        status = data.get("status")
        return None if status == "running" else f"VPN is not running (status: {str(status)[:30]})"

    def _require_vpn(self) -> None:
        if not self.gluetun_url:
            return
        if self._vpn_down is None:
            self._vpn_down = self._vpn_problem()
        if self._vpn_down:
            self.disabled = self._vpn_down
            raise VpnDown(self._vpn_down)

    # -- slskd --

    def _call(self, method: str, path: str, **kw):
        """One API call. Failures become FallbackMiss naming the call and the status or exception type, never the key.
        Redirects are not followed: a 3xx is a failure like any other non-2xx."""
        try:
            resp = getattr(self.http, method)(f"{self.url}/api/v0{path}", headers={"X-API-Key": self._key},
                                              timeout=HTTP_TIMEOUT, allow_redirects=False, **kw)
        except requests.RequestException as exc:
            raise FallbackMiss(f"slskd {method.upper()} {path} failed ({type(exc).__name__})") from None
        if not 200 <= resp.status_code < 300:
            raise FallbackMiss(f"slskd {method.upper()} {path} answered {resp.status_code}")
        return resp

    def check(self) -> str | None:
        if not self.downloads.is_dir():
            return f"slskd downloads directory {self.downloads} does not exist (CADENCE_SLSKD_DOWNLOADS)"
        if self.gluetun_url:
            if problem := self._vpn_problem():
                if problem == self._KEY_REJECTED:
                    return problem  # a configuration mistake: stop setup
                self.disabled = self._vpn_down = problem  # the tunnel is down: only Soulseek is off, the run goes on
                return None
            data, _ = self._gluetun("/publicip/ip")
            self.public_ip = str(data.get("public_ip"))[:64] if data and data.get("public_ip") else None
        try:
            resp = self.http.get(f"{self.url}/api/v0/application", headers={"X-API-Key": self._key}, timeout=HTTP_TIMEOUT,
                                 allow_redirects=False)
        except requests.RequestException as exc:
            return f"slskd at {self.url} is not reachable ({type(exc).__name__})"
        if resp.status_code == 401:
            return "slskd rejected the API key"
        return None if resp.status_code == 200 else f"slskd answered {resp.status_code} to /application"

    def _search(self, item: FetchItem) -> list[dict]:
        sid = str(uuid.uuid4())
        self._call("post", "/searches", json={"id": sid, "searchText": f"{item.artist} {item.title}",
                                              "searchTimeout": 15000, "filterResponses": True, "responseLimit": 100,
                                              "fileLimit": 10000})
        deadline = self.clock() + SEARCH_CAP_S
        while not self._call("get", f"/searches/{sid}").json().get("isComplete") and self.clock() < deadline:
            self.sleep(1)
        found = self._call("get", f"/searches/{sid}/responses").json()
        if not isinstance(found, list):
            raise FallbackMiss("malformed slskd search response")
        return found

    def _records(self, base: str) -> list[dict]:
        return [t for d in self._call("get", base).json().get("directories", []) for t in d.get("files", [])]

    @staticmethod
    def _ours(records: list, filename: str, ids: set[str], before: set[str]) -> dict | None:
        """Our transfer among slskd's records: the id the enqueue answer gave, else the record of this filename whose id
        was not there before the POST (the newest if several). Times are never compared: slskd's clock may differ."""
        if ids:
            return next((t for t in records if str(t.get("id")) in ids), None)
        fresh = [t for t in records if t.get("filename") == filename and str(t.get("id")) not in before]
        return fresh[-1] if fresh else None

    def _download(self, user: str, f: dict) -> str | None:
        """Enqueue and wait. None when the transfer succeeded, else why it did not (a timed-out one is cancelled)."""
        base = f"/transfers/downloads/{quote(user, safe='')}"
        try:  # slskd answers 404 for a user it has no transfers with yet: nothing was there before
            before = {str(t.get("id")) for t in self._records(base) if t.get("filename") == f["filename"]}
        except FallbackMiss:
            before = set()
        answer = self._call("post", base, json=[{"filename": f["filename"], "size": f["size"]}])
        try:
            body = answer.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        ids = {str(e["id"]) for e in body.get("enqueued") or [] if isinstance(e, dict) and e.get("id") is not None
               and e.get("filename", f["filename"]) == f["filename"]}
        if body.get("failed") and not ids:
            return "slskd refused to enqueue the file"
        deadline = self.clock() + self.timeout
        while True:
            t = self._ours(self._records(base), f["filename"], ids, before)
            state = (t or {}).get("state", "")
            if state == "Completed, Succeeded":
                return None
            if state.startswith("Completed"):
                return state
            if self.clock() >= deadline:
                if t is None:  # never matched: look once more, by new id whatever the enqueue answer said
                    t = self._ours(self._records(base), f["filename"], set(), before)
                if t and t.get("id"):
                    try:
                        self._call("delete", f"{base}/{t['id']}")
                    except FallbackMiss:
                        pass  # best effort: the next candidate goes ahead either way
                return f"timed out after {self.timeout}s"
            self.sleep(POLL_S)

    def _locate(self, filename: str, size: int) -> Path:
        """slskd can report Succeeded before it moves the file out of its incomplete dir, so look again for a while."""
        base, deadline = _basename(filename), self.clock() + LOCATE_CAP_S
        while True:
            hits = [Path(root) / base for root, _, names in os.walk(self.downloads) if base in names
                    and (Path(root) / base).stat().st_size == size]
            if len(hits) > 1:
                raise FallbackMiss(f"ambiguous: {len(hits)} files named {base} in the slskd downloads directory")
            if hits:
                return hits[0]
            if self.clock() >= deadline:
                raise FallbackMiss("completed but not found in downloads dir")
            self.sleep(1)

    def acquire(self, item: FetchItem, workdir: Path, gate: Gate) -> Acquired:
        want_t, want_a = _planned(item)
        if not want_t or not want_a:
            raise FallbackMiss("planned title or artist has no searchable words")
        self._require_vpn()
        try:
            cands = rank_candidates(item, self._search(item))[:self.max_candidates]
        except (KeyError, TypeError, AttributeError):
            raise FallbackMiss("malformed slskd search response") from None
        if not cands:
            raise FallbackMiss("no matching FLAC on Soulseek")
        why: list[str] = []
        for n, (resp, f) in enumerate(cands):
            if n:
                self._require_vpn()  # the tunnel can drop while an earlier candidate was being waited for
            user = resp.get("username", "?")
            try:
                failed = self._download(resp["username"], f)
                if not failed:
                    path = self._locate(f["filename"], f["size"])
                    return Acquired(path, f"{user}:{f['filename']}",
                                    {"username": user, "filename": f["filename"], "size": f["size"]})
            except FallbackMiss as exc:  # this candidate only: the next one may still work
                failed = str(exc)
            except (KeyError, TypeError, AttributeError, ValueError):
                failed = "malformed slskd record"
            why.append(f"{user}: {failed}")
        raise FallbackMiss("no candidate completed (" + "; ".join(why) + ")")


# ---- from the environment ----

def fallbacks_from_env(names: list[str], env=os.environ) -> tuple[list[Fallback], str | None]:
    """(fallbacks in the order named, error). An unknown name, a missing required variable or a non-numeric number is
    an error naming the variable, never its value. Soulseek also needs CADENCE_GLUETUN_URL and CADENCE_GLUETUN_API_KEY
    (slskd runs behind a VPN); the only way around that is CADENCE_SLSKD_VPN=off, and any other value of it is an error."""
    out: list[Fallback] = []
    for name in names:
        if name not in NAMES:
            return [], f"unknown fallback '{name}' (known: {', '.join(NAMES)})"
        vpn = name == "soulseek"
        if vpn and "CADENCE_SLSKD_VPN" in env:
            if env["CADENCE_SLSKD_VPN"] != "off":
                return [], "CADENCE_SLSKD_VPN must be exactly 'off' (the only opt-out) or unset"
            vpn = False
        need = {"orpheus": ("CADENCE_ORPHEUS_DIR", "CADENCE_ORPHEUS_MODULE"),
                "soulseek": ("CADENCE_SLSKD_URL", "CADENCE_SLSKD_API_KEY", "CADENCE_SLSKD_DOWNLOADS")}[name]
        if vpn:
            need += ("CADENCE_GLUETUN_URL", "CADENCE_GLUETUN_API_KEY")
        if missing := [v for v in need if not env.get(v)]:
            return [], f"fallback {name} needs {', '.join(missing)} to be set"
        if vpn and (not _host(env["CADENCE_SLSKD_URL"]) or _host(env["CADENCE_SLSKD_URL"]) != _host(env["CADENCE_GLUETUN_URL"])):
            return [], ("CADENCE_SLSKD_URL and CADENCE_GLUETUN_URL must name the same host: slskd runs inside gluetun's "
                        "network, so both are reached at one address")
        try:
            if name == "orpheus":
                out.append(OrpheusFallback(env["CADENCE_ORPHEUS_DIR"], env["CADENCE_ORPHEUS_MODULE"],
                                           env.get("CADENCE_ORPHEUS_PYTHON") or "python3",
                                           _number(env, "CADENCE_ORPHEUS_TIMEOUT", 900)))
            else:
                out.append(SoulseekFallback(env["CADENCE_SLSKD_URL"], env["CADENCE_SLSKD_API_KEY"],
                                            os.path.expanduser(env["CADENCE_SLSKD_DOWNLOADS"]),
                                            _number(env, "CADENCE_SLSKD_TIMEOUT", 900),
                                            _number(env, "CADENCE_SLSKD_MAX_CANDIDATES", 3),
                                            gluetun_url=env["CADENCE_GLUETUN_URL"] if vpn else None,
                                            gluetun_key=env["CADENCE_GLUETUN_API_KEY"] if vpn else None))
        except ValueError as exc:
            return [], str(exc)
    return out, None


def _host(url: str) -> str | None:
    try:
        return (urlparse(url).hostname or "").lower() or None
    except ValueError:
        return None


def _number(env, var: str, default: int) -> int:
    raw = env.get(var)
    if raw in (None, ""):
        return default
    if not raw.strip().isdigit() or int(raw) < 1:
        raise ValueError(f"{var} must be a positive whole number")
    return int(raw)
