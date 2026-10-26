import errno
import os

import mutagen
import pytest

from cadence import cli
from cadence.fetch import run as fetch_run
from cadence.fetch.plan import plan_missing
from cadence.fetch.run import DEFAULT_QUALITY, fetch_items
from tests.fakes import TooManyRequests, make_gate
from tests.fetch_fakes import (FFMPEG, FakeHttp, FakeTidal, add_artist, add_file, add_recording, add_release,
                               add_track, bts_body, http_error, ledger, make_aac, make_flac, mpd_body,
                               split_fmp4)

needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed: decode check and FLAC fixture unavailable")
URL = "https://cdn.example/{}.flac"
STAGED1 = "Neil Young/Harvest/01 - Song 1 [tidal 1]"  # staging name of track 1 (add .flac / .m4a)
ISRC = "USRE17200001"


@pytest.fixture(scope="module")
def flac(tmp_path_factory):
    return make_flac(tmp_path_factory.mktemp("fx") / "t.flac")


@pytest.fixture(scope="module")
def aac(tmp_path_factory):
    return make_aac(tmp_path_factory.mktemp("fx") / "t.m4a")


@pytest.fixture
def dirs(tmp_path):
    lib, stg = tmp_path / "lib", tmp_path / "staging"
    lib.mkdir()
    return stg, lib


def seed(n=2):
    """Neil Young / Harvest with n tracks (Tidal ids 1..n, 4 s each). Returns (conn, artist_id)."""
    conn = ledger()
    a = add_artist(conn)
    rel = add_release(conn, a, "Harvest", 100)
    for i in range(1, n + 1):
        add_track(conn, rel, add_recording(conn, a, f"Song {i}", isrc=f"{ISRC[:-1]}{i}"), i, f"Song {i}", i)
    conn.commit()  # the run rolls back on a stop; the plan must already be durable
    return conn, a


def tidal(n=2, **special):
    return FakeTidal({i: special.get(f"t{i}", bts_body(url=URL.format(i))) for i in range(1, n + 1)})


def rows(conn, table="file"):
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def run(conn, session, http, dirs, **kw):
    stg, lib = dirs
    out: list[str] = []
    res = fetch_items(conn, session, kw.pop("gate", make_gate()), kw.pop("items", None) or plan_missing(conn, scope="all"), stg, lib,
                      dry_run=False, quality=kw.pop("quality", "LOSSLESS"), http=http,
                      out=out.append, **kw)
    return res, out


@needs_ffmpeg
def test_places_a_tagged_flac_and_records_the_file_row(flac, dirs):
    conn, a = seed(1)
    session, http = tidal(1), FakeHttp({URL.format(1): flac})
    res, _ = run(conn, session, http, dirs)
    stg, lib = dirs
    dest = lib / "Neil Young/Harvest/01 - Song 1.flac"
    assert (res["placed"], res["failed"], res["stopped"]) == (1, 0, None)
    assert dest.is_file() and not any(p.is_file() for p in stg.rglob("*"))  # nothing left in staging
    tags = mutagen.File(dest)
    assert {k: tags[k] for k in tags.keys()} == {"albumartist": ["Neil Young"], "title": ["Song 1"], "album": ["Harvest"],
                                                 "tracknumber": ["1"], "discnumber": ["1"], "isrc": [f"{ISRC[:-1]}1"]}
    [f] = rows(conn)
    assert (f["path"], f["source"], f["match"], f["match_score"], f["artist_id"]) == \
        ("Neil Young/Harvest/01 - Song 1.flac", "tidal", "manual", 1.0, a)
    assert f["recording_id"] == conn.execute("SELECT id FROM recording WHERE title = 'Song 1'").fetchone()[0]
    assert (f["codec"], f["sample_rate"], f["bit_depth"], f["lossy"]) == ("flac", 44100, 16, 0)
    assert res["held_lossy"] == 0
    assert (f["quality_requested"], f["quality_received"]) == ("LOSSLESS", "FLAC 16/44.1")
    assert (f["size"], f["mtime"]) == (dest.stat().st_size, int(dest.stat().st_mtime))
    assert abs(f["duration_s"] - 4) < 0.1
    [ev] = rows(conn, "event_log")
    assert ev["kind"] == "fetch.placed" and ev["subject"] == f["path"]
    assert [c[1]["audioquality"] for c in session.calls] == ["LOSSLESS"] and http.urls == [URL.format(1)]
    assert plan_missing(conn, scope="all") == []  # linked now, so no longer missing


def cross_device(monkeypatch, lib, seen):
    """Make only the rename into the library fail with EXDEV; record the staged bytes it was about to move."""
    real = os.rename

    def rename(src, dst, *a, **kw):
        if os.fspath(dst).startswith(os.fspath(lib)):
            seen.append(open(src, "rb").read())
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(fetch_run.os, "rename", rename)


@needs_ffmpeg
def test_cross_device_placement_copies_then_replaces_and_leaves_no_part_file(flac, dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    seen: list[bytes] = []
    cross_device(monkeypatch, lib, seen)
    res, _ = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    dest = lib / "Neil Young/Harvest/01 - Song 1.flac"
    assert (res["placed"], res["failed"]) == (1, 0) and len(seen) == 1
    assert dest.read_bytes() == seen[0]
    assert [f["path"] for f in rows(conn)] == ["Neil Young/Harvest/01 - Song 1.flac"]
    assert not any(p.is_file() for p in stg.rglob("*"))  # staged source is gone
    assert not list(lib.rglob("*.part"))


@needs_ffmpeg
def test_cross_device_copy_failure_fails_the_track_and_leaves_nothing_half_placed(flac, dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    cross_device(monkeypatch, lib, [])

    def broken_copy(src, dst, *a, **kw):
        open(dst, "wb").write(b"half")  # a partial copy that must be cleaned up
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(fetch_run.shutil, "copy2", broken_copy)
    res, _ = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["failed"]) == (0, 1)
    assert not any(p.is_file() for p in lib.rglob("*"))  # no library file, no .part
    assert (stg / f"{STAGED1}.flac").is_file()  # source untouched
    assert rows(conn) == []


AAC_URL = "https://cdn.example/{}.mp4"


def aac_tidal():
    return FakeTidal({1: bts_body(url=AAC_URL.format(1), codec="mp4a.40.2", audioQuality="HIGH", bitDepth=None)})


@needs_ffmpeg
def test_lossy_result_under_a_lossless_request_is_held_in_staging(aac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    res, out = run(conn, aac_tidal(), FakeHttp({AAC_URL.format(1): aac}), dirs)
    staged = stg / f"{STAGED1}.m4a"
    assert (res["held_lossy"], res["placed"], res["failed"], res["stopped"]) == (1, 0, 0, None)
    assert staged.is_file() and not any(p.is_file() for p in lib.rglob("*")) and rows(conn) == []
    assert list(lib.iterdir()) == []  # no library directory was created either
    assert list(stg.rglob("*.part")) == []
    [ev] = rows(conn, "event_log")
    assert ev["kind"] == "fetch.held_lossy" and ev["subject"] == "Neil Young/Harvest/01 - Song 1.m4a"
    text = "\n".join(out)
    assert "held_lossy" in text and str(staged) in text and "AAC" in text and "1 held (lossy)" in text


@needs_ffmpeg
def test_lossy_result_under_the_default_quality_is_held_not_placed(aac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    res, _ = run(conn, aac_tidal(), FakeHttp({AAC_URL.format(1): aac}), dirs, quality=DEFAULT_QUALITY)
    assert DEFAULT_QUALITY == "HI_RES_LOSSLESS"
    assert (res["held_lossy"], res["placed"], res["failed"], res["stopped"]) == (1, 0, 0, None)
    assert (stg / f"{STAGED1}.m4a").is_file() and list(lib.iterdir()) == [] and rows(conn) == []
    assert list(stg.rglob("*.part")) == []
    [ev] = rows(conn, "event_log")
    assert ev["kind"] == "fetch.held_lossy"


@needs_ffmpeg
def test_lossy_result_under_a_high_request_is_placed_as_before(aac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    res, _ = run(conn, aac_tidal(), FakeHttp({AAC_URL.format(1): aac}), dirs, quality="HIGH")
    dest = lib / "Neil Young/Harvest/01 - Song 1.m4a"
    assert (res["placed"], res["held_lossy"], res["failed"]) == (1, 0, 0)
    assert dest.is_file() and not any(p.is_file() for p in stg.rglob("*"))
    [f] = rows(conn)
    assert (f["path"], f["codec"], f["lossy"], f["quality_requested"]) == \
        ("Neil Young/Harvest/01 - Song 1.m4a", "aac", 1, "HIGH")
    assert f["quality_received"].startswith("AAC")


def test_an_already_held_lossy_file_is_not_fetched_again(dirs):
    conn, a = seed(1)
    stg, lib = dirs
    held = stg / f"{STAGED1}.m4a"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"held")
    session, http, gate = tidal(1), FakeHttp(), make_gate()
    res, out = run(conn, session, http, dirs, gate=gate)
    assert (res["held_lossy"], res["placed"], res["failed"], res["stopped"]) == (1, 0, 0, None)
    assert session.calls == [] and http.urls == [] and held.read_bytes() == b"held"
    assert rows(conn) == [] and rows(conn, "event_log") == [] and list(lib.iterdir()) == []
    assert "held_lossy" in "\n".join(out) and str(held) in "\n".join(out)


@needs_ffmpeg
def test_placing_a_flac_removes_the_held_lossy_sibling(flac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    held = stg / f"{STAGED1}.m4a"
    other = stg / "Neil Young/Harvest/02 - Song 2.m4a"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other")

    class HeldAppearsMidRun(FakeHttp):  # a held file already there would skip the track, so it shows up after the check
        def get(self, url, stream=False, timeout=None):
            held.write_bytes(b"held")
            return super().get(url, stream, timeout)

    res, _ = run(conn, tidal(1), HeldAppearsMidRun({URL.format(1): flac}), dirs)
    dest = lib / "Neil Young/Harvest/01 - Song 1.flac"
    assert (res["placed"], res["held_lossy"], res["failed"]) == (1, 0, 0)
    assert dest.is_file() and not held.exists() and other.read_bytes() == b"other"
    [f] = rows(conn)
    assert f["path"] == "Neil Young/Harvest/01 - Song 1.flac"
    assert [e["kind"] for e in rows(conn, "event_log")] == ["fetch.placed", "fetch.stale_held_removed"]
    ev = rows(conn, "event_log")[1]
    assert ev["subject"] == f["path"] and "FLAC was placed" in ev["message"]


@needs_ffmpeg
def test_a_held_lossy_file_does_not_stop_a_high_request_from_fetching_and_placing(aac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    held = stg / f"{STAGED1}.m4a"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"held")
    session = aac_tidal()
    res, _ = run(conn, session, FakeHttp({AAC_URL.format(1): aac}), dirs, quality="HIGH")
    assert (res["placed"], res["held_lossy"], res["failed"]) == (1, 0, 0) and len(session.calls) == 1
    assert (lib / "Neil Young/Harvest/01 - Song 1.m4a").is_file() and not any(p.is_file() for p in stg.rglob("*"))


@needs_ffmpeg
def test_a_held_track_does_not_skip_or_overwrite_the_same_titled_track_on_another_disc(flac, dirs):
    conn = ledger()
    a = add_artist(conn)
    rel = add_release(conn, a, "Harvest", 100)
    for disc, tid in ((1, 1), (2, 2)):  # same number and title: the same library name, two different Tidal tracks
        add_track(conn, rel, add_recording(conn, a, "Song", isrc=f"{ISRC[:-1]}{tid}"), 1, "Song", tid, disc=disc)
    conn.commit()
    items = plan_missing(conn, scope="all")
    assert [i.target_rel for i in items] == ["Neil Young/Harvest/01 - Song.flac"] * 2
    stg, lib = dirs
    held = stg / "Neil Young/Harvest/01 - Song [tidal 1].m4a"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"held")
    session = tidal(2)
    res, _ = run(conn, session, FakeHttp({URL.format(2): flac}), dirs, items=items)
    assert (res["held_lossy"], res["placed"], res["failed"]) == (1, 1, 0)
    assert [c[0].split("/")[1] for c in session.calls] == ["2"]  # only track 2 was asked for
    assert held.read_bytes() == b"held"
    assert (lib / "Neil Young/Harvest/01 - Song.flac").is_file()
    assert [p for p in stg.rglob("*") if p.is_file()] == [held]


@needs_ffmpeg
def test_cross_device_copy_with_a_size_mismatch_fails_the_track_and_keeps_the_source(flac, dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    cross_device(monkeypatch, lib, [])
    real = fetch_run.shutil.copy2

    def short_copy(src, dst, *a, **kw):
        real(src, dst, *a, **kw)
        os.truncate(dst, 10)  # the copy "succeeded" but is short
    monkeypatch.setattr(fetch_run.shutil, "copy2", short_copy)
    res, out = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["failed"]) == (0, 1) and "10 bytes" in "\n".join(out)
    assert not any(p.is_file() for p in lib.rglob("*")) and list(lib.iterdir()) == []  # no .part, no empty album dir
    assert (stg / f"{STAGED1}.flac").is_file() and rows(conn) == []


@needs_ffmpeg
def test_a_staged_copy_that_cannot_be_removed_after_commit_still_reports_placed(flac, dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    cross_device(monkeypatch, lib, [])
    real = fetch_run.Path.unlink

    def unlink(self, *a, **kw):
        if "[tidal" in self.name:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(self, *a, **kw)
    monkeypatch.setattr(fetch_run.Path, "unlink", unlink)
    res, _ = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["failed"]) == (1, 0)
    assert (lib / "Neil Young/Harvest/01 - Song 1.flac").is_file() and (stg / f"{STAGED1}.flac").is_file()
    assert [f["path"] for f in rows(conn)] == ["Neil Young/Harvest/01 - Song 1.flac"]
    placed, failed = rows(conn, "event_log")
    assert (placed["kind"], failed["kind"]) == ("fetch.placed", "fetch.staged_cleanup_failed")
    assert failed["subject"] == "Neil Young/Harvest/01 - Song 1.flac" and "by hand" in failed["message"]


@needs_ffmpeg
def test_a_library_file_that_appears_during_the_cross_device_copy_is_not_replaced(flac, dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    cross_device(monkeypatch, lib, [])
    real = fetch_run.shutil.copy2
    dest = lib / "Neil Young/Harvest/01 - Song 1.flac"

    def copy_then_race(src, dst, *a, **kw):
        real(src, dst, *a, **kw)
        dest.write_bytes(b"precious")  # someone else puts the file there while we copy
    monkeypatch.setattr(fetch_run.shutil, "copy2", copy_then_race)
    res, _ = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["failed"]) == (0, 1)
    assert dest.read_bytes() == b"precious" and not list(lib.rglob("*.part"))
    assert (stg / f"{STAGED1}.flac").is_file() and rows(conn) == []


def test_move_into_library_refuses_an_existing_destination_and_keeps_both_files(tmp_path):
    src, dst = tmp_path / "src.flac", tmp_path / "dst.flac"
    src.write_bytes(b"new")
    dst.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        fetch_run._move_into_library(src, dst)
    assert (src.read_bytes(), dst.read_bytes()) == (b"new", b"precious")


def test_staging_inside_or_equal_to_the_library_is_refused_before_any_call(tmp_path):
    conn, a = seed(1)
    lib = tmp_path / "lib"
    lib.mkdir()
    (tmp_path / "link").symlink_to(lib / "stg")
    for stg in (lib / "staging", lib, tmp_path / "link"):
        session, http = tidal(1), FakeHttp()
        res, out = run(conn, session, http, (stg, lib))
        assert res["stopped"] == "setup" and len(res["remaining"]) == 1 and "inside it" in out[0]
        assert session.calls == [] and http.urls == [] and list(lib.iterdir()) == []


def test_staging_with_a_different_letter_case_than_the_library_is_refused(tmp_path):
    conn, a = seed(1)
    lib = tmp_path / "lib"
    lib.mkdir()
    if not (tmp_path / "LIB").exists():
        pytest.skip("case-sensitive filesystem: a case-variant path cannot name the library here")
    session, http = tidal(1), FakeHttp()
    res, out = run(conn, session, http, (tmp_path / "LIB" / "staging", lib))
    assert res["stopped"] == "setup" and "inside it" in out[0]
    assert session.calls == [] and http.urls == [] and list(lib.iterdir()) == []


def test_a_dangling_symlink_at_the_destination_counts_as_existing_and_is_not_replaced(dirs):
    conn, a = seed(1)
    stg, lib = dirs
    target = lib / "Neil Young/Harvest/01 - Song 1.flac"
    target.parent.mkdir(parents=True)
    target.symlink_to(lib / "nowhere.flac")  # dangling
    session, http = tidal(1), FakeHttp()
    res, out = run(conn, session, http, dirs)
    assert (res["placed"], res["exists"]) == (0, 1)
    assert target.is_symlink() and not target.exists() and os.readlink(target) == str(lib / "nowhere.flac")
    assert session.calls == [] and http.urls == [] and rows(conn) == []


def test_move_into_library_refuses_a_dangling_symlink_destination(tmp_path):
    src, dst = tmp_path / "src.flac", tmp_path / "dst.flac"
    src.write_bytes(b"new")
    dst.symlink_to(tmp_path / "nowhere.flac")
    with pytest.raises(FileExistsError):
        fetch_run._move_into_library(src, dst)
    assert src.read_bytes() == b"new" and dst.is_symlink() and not dst.exists()


@needs_ffmpeg
def test_never_overwrites_a_file_on_disk_or_in_the_ledger(flac, dirs):
    conn, a = seed(2)
    stg, lib = dirs
    target = lib / "Neil Young/Harvest/01 - Song 1.flac"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"precious")
    add_file(conn, "Neil Young/Harvest/02 - Song 2.flac", a)  # known to the ledger only
    session, http = tidal(2), FakeHttp({URL.format(1): flac, URL.format(2): flac})
    res, out = run(conn, session, http, dirs)
    assert (res["placed"], res["exists"]) == (0, 2)
    assert target.read_bytes() == b"precious" and not (lib / "Neil Young/Harvest/02 - Song 2.flac").exists()
    assert session.calls == [] and http.urls == []  # decided before any Tidal call
    assert len(rows(conn)) == 1


@needs_ffmpeg
def test_dry_run_is_the_default_and_makes_no_calls_and_no_writes(flac, dirs):
    conn, a = seed(2)
    stg, lib = dirs
    (lib / "Neil Young/Harvest").mkdir(parents=True)
    (lib / "Neil Young/Harvest/01 - Song 1.flac").write_bytes(b"x")
    session, http, out = tidal(2), FakeHttp(default=flac), []
    before = (len(rows(conn)), len(rows(conn, "event_log")), sorted(p for p in lib.rglob("*")))
    res = fetch_items(conn, session, make_gate(), plan_missing(conn, scope="all"), stg, lib, out=out.append)
    assert res["dry_run"] and (res["would_fetch"], res["exists"]) == (1, 1)
    assert session.calls == [] and http.urls == []
    assert not stg.exists()
    assert before == (len(rows(conn)), len(rows(conn, "event_log")), sorted(p for p in lib.rglob("*")))
    text = "\n".join(out)
    assert "would fetch" in text and "Song 2.flac" in text and "exists, skip" in text


@needs_ffmpeg
def test_429_stops_cleanly_with_no_partial_file(flac, dirs):
    conn, a = seed(3)
    session = tidal(3, t2=TooManyRequests(120))
    http = FakeHttp({URL.format(1): flac, URL.format(3): flac})
    gate = make_gate()
    items = plan_missing(conn, scope="all")
    res, _ = run(conn, session, http, dirs, gate=gate, items=items)
    stg, lib = dirs
    assert (res["placed"], res["stopped"]) == (1, "paused") and res["until"]
    assert res["remaining"] == items[1:]
    assert gate.paused_until() is not None
    assert sorted(p.name for p in lib.rglob("*.flac")) == ["01 - Song 1.flac"]
    assert not list(stg.rglob("*.part")) and len(session.calls) == 2  # item 3 was never tried
    assert [r["kind"] for r in rows(conn, "event_log")] == ["fetch.placed", "gate.pause"]


@needs_ffmpeg
def test_429_from_the_cdn_download_also_pauses_and_places_nothing(dirs):
    conn, a = seed(1)
    res, _ = run(conn, tidal(1), FakeHttp({URL.format(1): http_error(429, {})}), dirs)
    assert (res["stopped"], res["placed"]) == ("paused", 0)
    assert not list(dirs[1].rglob("*.flac")) and rows(conn) == []


@needs_ffmpeg
def test_401_on_one_tracks_playback_blocks_that_track_and_continues(flac, dirs):
    conn, a = seed(2)
    session = tidal(2, t1=http_error(401, {"userMessage": "Asset is not ready for playback"}))
    res, _ = run(conn, session, FakeHttp({URL.format(2): flac}), dirs)
    assert (res["blocked"], res["placed"], res["stopped"], res["failed"]) == (1, 1, None, 0)
    [blocked] = [r for r in rows(conn, "event_log") if r["kind"] == "fetch.blocked"]
    assert blocked["subject"] == "1"
    assert [f["path"] for f in rows(conn)] == ["Neil Young/Harvest/02 - Song 2.flac"]


def test_a_401_about_the_token_stops_the_run(dirs):
    conn, a = seed(2)
    session = tidal(2, t1=http_error(401, {"userMessage": "The token has expired."}))
    res, _ = run(conn, session, FakeHttp(), dirs, verify=False)
    assert (res["stopped"], res["placed"], res["blocked"], len(res["remaining"])) == ("auth", 0, 0, 2)


@needs_ffmpeg
def test_bad_audio_or_wrong_duration_is_not_placed(flac, dirs):
    conn, a = seed(2)
    conn.execute("UPDATE track SET duration_s = 100 WHERE tidal_id = 2")  # Tidal says 100 s, the file is 4 s
    http = FakeHttp({URL.format(1): b"this is not audio", URL.format(2): flac})
    res, out = run(conn, tidal(2), http, dirs)
    assert (res["placed"], res["failed"]) == (0, 2)
    assert not list(dirs[1].rglob("*.flac")) and rows(conn) == []
    assert "decode check failed" in "\n".join(out) and "not within 3s" in "\n".join(out)


@needs_ffmpeg
def test_segmented_hi_res_stream_is_remuxed_into_a_flac(flac, dirs, tmp_path):
    conn, a = seed(1)
    src = tmp_path / "src.flac"
    src.write_bytes(flac)
    parts = split_fmp4(src, tmp_path)
    xml = ("<?xml version='1.0' encoding='UTF-8'?><MPD xmlns='urn:mpeg:dash:schema:mpd:2011'><Period><AdaptationSet "
           "contentType='audio'><Representation codecs='flac' audioSamplingRate='44100'><SegmentTemplate "
           "initialization='https://cdn.example/init' media='https://cdn.example/seg$Number$' startNumber='1'>"
           f"<SegmentTimeline><S d='1' r='{len(parts) - 2}'/></SegmentTimeline></SegmentTemplate></Representation>"
           "</AdaptationSet></Period></MPD>")
    http = FakeHttp({"https://cdn.example/init": parts[0],
                     **{f"https://cdn.example/seg{n}": p for n, p in enumerate(parts[1:], 1)}})
    session = FakeTidal({1: mpd_body(xml, bitDepth=16, sampleRate=44100)})
    stg, lib = dirs
    res = fetch_items(conn, session, make_gate(), plan_missing(conn, scope="all"), stg, lib, dry_run=False,
                      quality="HI_RES_LOSSLESS", http=http, out=lambda s: None)
    assert (res["placed"], res["failed"]) == (1, 0)
    [f] = rows(conn)
    assert (f["codec"], f["quality_requested"], f["quality_received"]) == ("flac", "HI_RES_LOSSLESS", "FLAC 16/44.1")
    assert mutagen.File(lib / f["path"])["albumartist"] == ["Neil Young"]


# ---- the CLI around it ----

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from cadence.ledger import db
    c = db.connect(tmp_path / "c.db")
    db.migrate(c)
    a = add_artist(c)
    rel = add_release(c, a, "Harvest", 100)
    for i in (1, 2):
        add_track(c, rel, add_recording(c, a, f"Song {i}"), i, f"Song {i}", i)
    c.commit()
    c.close()
    (tmp_path / "lib").mkdir()
    monkeypatch.setenv("CADENCE_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("CADENCE_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("CADENCE_STAGING", str(tmp_path / "staging"))
    return tmp_path


def test_cli_without_execute_is_a_dry_run_that_never_logs_in(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_login", lambda *a: pytest.fail("dry run must not touch Tidal"))
    assert cli.main(["fetch", "--artist", "neil young", "--scope", "all"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and out.count("would fetch") == 2
    assert not (cli_env / "staging").exists() and not list((cli_env / "lib").iterdir())
    assert cli.main(["fetch", "--release", "100", "--limit", "1"]) == 0
    assert capsys.readouterr().out.count("would fetch") == 2 - 1  # one line plus the summary mention
    assert cli.main(["fetch", "--release", "999"]) == 1


def test_cli_default_scope_is_partial_and_all_warns(cli_env, monkeypatch, capsys):
    from cadence.ledger import db
    c = db.connect(cli_env / "c.db")
    c.execute("INSERT INTO release(artist_id, title, norm_title, type) SELECT 1, 'Untouched', 'untouched', 'album'")
    rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    add_track(c, rid, add_recording(c, 1, "Lonely"), 1, "Lonely", 99)
    add_file(c, "Neil Young/Harvest/01 - Song 1.flac", 1, 1)  # Song 1 is owned: Harvest is partial, Untouched is not
    c.commit()
    c.close()
    assert cli.main(["fetch", "--artist", "neil young"]) == 0
    out = capsys.readouterr().out
    assert "Harvest/02 - Song 2.flac" in out and "Lonely" not in out and "WARNING" not in out
    assert cli.main(["fetch", "--artist", "neil young", "--scope", "all"]) == 0
    out = capsys.readouterr().out
    assert "Lonely" in out and "WARNING: --scope all plans 2 track(s) across 2 release(s)" in out


def test_cli_requires_paths_from_the_environment(cli_env, monkeypatch, capsys):
    monkeypatch.delenv("CADENCE_LIBRARY")
    assert cli.main(["fetch"]) == 2
    assert "CADENCE_LIBRARY" in capsys.readouterr().err


def test_setup_problems_stop_before_any_call(tmp_path, monkeypatch):
    conn, a = seed(1)
    session, http = tidal(1), FakeHttp()
    stg, lib = tmp_path / "staging", tmp_path / "missing-lib"
    res, out = run(conn, session, http, (stg, lib))
    assert res["stopped"] == "setup" and len(res["remaining"]) == 1 and "does not exist" in out[0]
    assert not stg.exists() and not lib.exists()
    lib.mkdir()
    monkeypatch.setattr("cadence.fetch.run.shutil.which", lambda name: None)
    res, out = run(conn, session, http, (stg, lib))
    assert res["stopped"] == "setup" and "ffmpeg not found" in out[0]
    assert session.calls == [] and http.urls == []


@needs_ffmpeg
def test_cli_execute_exits_3_on_429_and_persists_the_pause(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_login", lambda gate, token: FakeTidal(default=TooManyRequests(300)))
    assert cli.main(["fetch", "--execute", "--quality", "LOSSLESS", "--scope", "all"]) == cli.EXIT_PAUSED
    assert "gate paused until" in capsys.readouterr().out
    assert not list((cli_env / "lib").rglob("*.flac"))
    monkeypatch.setattr(cli, "_login", lambda *a: pytest.fail("a paused gate must not log in"))
    assert cli.main(["fetch", "--execute", "--scope", "all"]) == cli.EXIT_PAUSED
