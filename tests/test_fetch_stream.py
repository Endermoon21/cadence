import pytest

from cadence.fetch.stream import QUALITIES, StreamRefused, ext_for_quality, parse_playbackinfo, resolve_stream
from tests.fetch_fakes import FakeTidal, bts_body, mpd_body

MPD = """<?xml version='1.0' encoding='UTF-8'?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT3M0.5S">
 <Period id="0"><AdaptationSet id="0" contentType="audio" mimeType="audio/mp4">
  <Representation id="FLAC" codecs="flac" bandwidth="1800000" audioSamplingRate="96000">
   <SegmentTemplate timescale="96000" initialization="https://cdn/0.mp4?a=1&amp;b=2" media="https://cdn/$Number$.mp4?a=1&amp;b=2" startNumber="1">
    <SegmentTimeline>{timeline}</SegmentTimeline>
   </SegmentTemplate>
  </Representation>{extra}
 </AdaptationSet></Period>
</MPD>"""


def mpd(timeline='<S d="384000" r="2"/><S d="100000"/>', extra=""):
    return MPD.format(timeline=timeline, extra=extra)


def test_bts_direct_url():
    info = parse_playbackinfo(bts_body(), "LOSSLESS")
    assert info.urls == ["https://cdn.example/t.flac"]
    assert (info.codec, info.sample_rate, info.bit_depth, info.is_encrypted, info.segmented) == ("flac", 44100, 16, False, False)
    assert (info.quality_requested, info.audio_quality, info.ext) == ("LOSSLESS", "LOSSLESS", ".flac")


def test_bts_aac_is_an_m4a():
    info = parse_playbackinfo(bts_body(url="https://cdn.example/t.mp4", codec="mp4a.40.2", audioQuality="HIGH"), "HIGH")
    assert (info.codec, info.ext) == ("aac", ".m4a")


def test_mpd_segments_are_counted_from_the_timeline_not_guessed():
    info = parse_playbackinfo(mpd_body(mpd()), "HI_RES_LOSSLESS")
    assert info.urls == ["https://cdn/0.mp4?a=1&b=2", *(f"https://cdn/{n}.mp4?a=1&b=2" for n in (1, 2, 3, 4))]
    assert (info.segmented, info.codec, info.sample_rate, info.bit_depth, info.is_encrypted) == (True, "flac", 96000, None, False)
    # three <S> without @r: tidalapi 0.8.11 would build one segment too many here
    three = parse_playbackinfo(mpd_body(mpd('<S d="1"/><S d="1"/><S d="1"/>')), "HI_RES_LOSSLESS")
    assert len(three.urls) == 1 + 3


def test_mpd_json_fields_win_for_rate_and_depth():
    info = parse_playbackinfo(mpd_body(mpd(), bitDepth=24, sampleRate=96000), "HI_RES_LOSSLESS")
    assert (info.bit_depth, info.sample_rate) == (24, 96000)


def test_encrypted_streams_are_flagged_and_resolve_refuses():
    enc = bts_body(manifest={"encryptionType": "OLD_AES", "keyId": "abc"})
    assert parse_playbackinfo(enc, "LOSSLESS").is_encrypted
    drm = mpd(extra='<ContentProtection schemeIdUri="urn:uuid:x"/>')
    assert parse_playbackinfo(mpd_body(drm), "HI_RES_LOSSLESS").is_encrypted
    with pytest.raises(StreamRefused, match="encrypted"):
        resolve_stream(FakeTidal(default=enc), 1, "LOSSLESS")


@pytest.mark.parametrize("body,why", [
    (bts_body(assetPresentation="PREVIEW"), "PREVIEW"),
    (bts_body(audioMode="DOLBY_ATMOS"), "stereo"),
    (bts_body(manifestMimeType="application/vnd.tidal.emu"), "unknown manifest"),
    (bts_body(codec="eac3"), "codec"),
    (bts_body(manifest={"urls": ["a", "b"]}), "one direct URL"),
    (mpd_body(mpd('<S d="1" r="-1"/>')), "open-ended"),
    (mpd_body("<MPD"), "unreadable"),
])
def test_unsafe_or_unknown_streams_are_refused(body, why):
    with pytest.raises(StreamRefused, match=why):
        resolve_stream(FakeTidal(default=body), 1, "LOSSLESS")


def test_resolve_asks_for_exactly_the_requested_quality():
    s = FakeTidal(default=bts_body())
    resolve_stream(s, 77, "LOSSLESS")
    assert s.calls == [("tracks/77/playbackinfopostpaywall",
                        {"playbackmode": "STREAM", "audioquality": "LOSSLESS", "assetpresentation": "FULL"})]
    with pytest.raises(ValueError):
        resolve_stream(s, 77, "LOW")
    assert len(s.calls) == 1


def test_extension_for_quality():
    assert [ext_for_quality(q) for q in QUALITIES] == [".flac", ".flac", ".m4a"]
