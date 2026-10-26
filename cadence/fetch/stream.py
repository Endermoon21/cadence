"""Turn a Tidal track id into plain-HTTP download URLs. Never decrypts anything.

Reads the playbackinfo endpoint through the session's own request layer (`session.request.request`), so this
module does not import tidalapi (only cadence/sources may). tidalapi 0.8.11's Stream.get_stream_manifest is not
used because it (a) takes the quality from the session-wide config, (b) hard-codes encryption "NONE" for DASH
manifests, (c) counts DASH segments wrongly unless exactly one <S> carries @r, and (d) splits the MPD text on one
exact XML declaration. The manifest is parsed here instead; the JSON field names are the ones Stream.parse reads.

Two manifest kinds exist:
  application/vnd.tidal.bts  JSON with one direct file URL (LOSSLESS, HIGH).
  application/dash+xml       MPD: an init segment plus numbered fragmented-MP4 segments (HI_RES_LOSSLESS);
                             plain GETs, joined by the caller (ffmpeg remux), no DRM."""
from __future__ import annotations

import base64
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

QUALITIES = ("HI_RES_LOSSLESS", "LOSSLESS", "HIGH")  # tidalapi.Quality values; LOW is not offered
MAX_SEGMENTS = 5000
_BTS = "application/vnd.tidal.bts"
_MPD = "application/dash+xml"
_OTHER_TEMPLATE_VARS = re.compile(r"\$(?!Number\$)\w")


class StreamRefused(Exception):
    """The stream cannot be fetched safely with plain HTTP (encrypted, preview only, unknown layout)."""


@dataclass(frozen=True)
class StreamInfo:
    urls: list[str]                     # BTS: [file]; MPD: [init segment, segment 1, ...] in order
    codec: str                          # 'flac' | 'aac' | 'alac'
    sample_rate: int | None
    bit_depth: int | None
    bitrate: int | None                 # bits/s, only known for some lossy streams
    is_encrypted: bool
    segmented: bool = False
    quality_requested: str = ""         # the quality name we asked for
    audio_quality: str | None = None    # what Tidal served (HI_RES_LOSSLESS may come back as LOSSLESS)

    @property
    def ext(self) -> str:
        return ".flac" if self.codec == "flac" else ".m4a"


def ext_for_quality(quality: str) -> str:
    """Expected file extension before any network call: lossless is FLAC, HIGH is AAC in an m4a."""
    return ".m4a" if quality == "HIGH" else ".flac"


def _codec(raw: str) -> str:
    c = (raw or "").strip().lower()
    if c.startswith("mp4a") or c == "aac":
        return "aac"
    if c in ("flac", "alac"):
        return c
    raise StreamRefused(f"unsupported codec '{raw}'")


def _opt_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _bts(manifest: bytes, body: dict, quality: str) -> StreamInfo:
    m = json.loads(manifest)
    enc = str(m.get("encryptionType") or "NONE").upper()
    urls = list(m.get("urls") or [])
    if len(urls) != 1:
        raise StreamRefused(f"expected one direct URL, manifest has {len(urls)}")
    codec = _codec(m.get("codecs", ""))
    return StreamInfo(urls, codec, _opt_int(body.get("sampleRate")), _opt_int(body.get("bitDepth")), None,
                      enc != "NONE" or bool(m.get("keyId")), False, quality, body.get("audioQuality"))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _mpd(manifest: bytes, body: dict, quality: str) -> StreamInfo:
    root = ET.fromstring(manifest)
    encrypted = any(_local(e.tag) == "ContentProtection" for e in root.iter())
    periods = _kids(root, "Period")
    if len(periods) != 1:
        raise StreamRefused(f"expected one Period, manifest has {len(periods)}")
    sets = _kids(periods[0], "AdaptationSet")
    reps = _kids(sets[0], "Representation") if sets else []
    if len(reps) != 1:
        raise StreamRefused(f"expected one Representation, manifest has {len(reps)}")
    rep = reps[0]
    templates = _kids(rep, "SegmentTemplate") or _kids(sets[0], "SegmentTemplate")
    timelines = _kids(templates[0], "SegmentTimeline") if templates else []
    if not timelines:
        raise StreamRefused("manifest has no SegmentTemplate/SegmentTimeline")
    tpl = templates[0]
    init, media = tpl.get("initialization"), tpl.get("media")
    if not init or not media or "$Number$" not in media or _OTHER_TEMPLATE_VARS.search(media):
        raise StreamRefused("unsupported segment template")
    count = 0
    for s in _kids(timelines[0], "S"):
        r = int(s.get("r", 0))
        if r < 0:
            raise StreamRefused("open-ended segment repeat")
        count += r + 1
    if not 0 < count <= MAX_SEGMENTS:
        raise StreamRefused(f"implausible segment count {count}")
    start = int(tpl.get("startNumber", 1))
    codec = _codec(rep.get("codecs") or sets[0].get("codecs") or "")
    rate = _opt_int(body.get("sampleRate")) or _opt_int(rep.get("audioSamplingRate") or sets[0].get("audioSamplingRate"))
    urls = [init, *(media.replace("$Number$", str(n)) for n in range(start, start + count))]
    bitrate = _opt_int(rep.get("bandwidth")) if codec == "aac" else None
    return StreamInfo(urls, codec, rate, _opt_int(body.get("bitDepth")), bitrate, encrypted, True, quality,
                      body.get("audioQuality"))


def parse_playbackinfo(body: dict, quality: str) -> StreamInfo:
    """StreamInfo from a playbackinfo JSON body. An encrypted stream is returned flagged, not raised."""
    if body.get("assetPresentation", "FULL") != "FULL":
        raise StreamRefused(f"Tidal served '{body.get('assetPresentation')}' instead of the full track")
    if body.get("audioMode", "STEREO") != "STEREO":
        raise StreamRefused(f"audio mode {body.get('audioMode')} is not stereo")
    mime = body.get("manifestMimeType") or ""
    try:
        manifest = base64.b64decode(body.get("manifest") or "")
    except ValueError as exc:
        raise StreamRefused("manifest is not valid base64") from exc
    if _BTS in mime:
        return _bts(manifest, body, quality)
    if _MPD in mime:
        return _mpd(manifest, body, quality)
    raise StreamRefused(f"unknown manifest type '{mime}'")


def resolve_stream(session, track_id: int, quality: str) -> StreamInfo:
    """One playbackinfo request (one gate call for the caller). Raises StreamRefused for anything we won't fetch;
    HTTP errors from the request layer propagate untouched (the caller classifies 401/429)."""
    if quality not in QUALITIES:
        raise ValueError(f"quality must be one of {', '.join(QUALITIES)}, not '{quality}'")
    resp = session.request.request("GET", f"tracks/{int(track_id)}/playbackinfopostpaywall",
                                   {"playbackmode": "STREAM", "audioquality": quality,
                                    "assetpresentation": "FULL"})
    try:
        info = parse_playbackinfo(resp.json(), quality)
    except (ValueError, KeyError, ET.ParseError) as exc:
        raise StreamRefused(f"unreadable manifest ({type(exc).__name__})") from exc
    if info.is_encrypted:
        raise StreamRefused("stream is encrypted; refusing (no decryption)")
    return info
