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
PROBE = ".cadence-linkprobe-"  # the preflight's own hard-link probe in the library root; the link hooks let it through


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
    res = fetch_items(conn, session, kw.pop("gate", make_gate()), kw.pop("items", None) or plan_missing(conn, scope="all").items, stg, lib,
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
    assert plan_missing(conn, scope="all").items == []  # linked now, so no longer missing


def cross_device(monkeypatch, lib, seen):
    """Make only the link of the staged file into the library fail with EXDEV (the .part link, which is within the
    library, still works); record the staged bytes it was about to place."""
    real = os.link

    def link(src, dst, *a, **kw):
        if (os.fspath(dst).startswith(os.fspath(lib)) and not os.fspath(src).endswith(".part")
                and PROBE not in os.fspath(dst)):
            seen.append(open(src, "rb").read())
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(fetch_run.os, "link", link)


@needs_ffmpeg
def test_cross_device_placement_copies_then_links_and_leaves_no_part_file(flac, dirs, monkeypatch):
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
    for disc, tid in ((1, 1), (2, 2)):  # same number and title, two different Tidal tracks, told apart by the disc
        add_track(conn, rel, add_recording(conn, a, "Song", isrc=f"{ISRC[:-1]}{tid}"), 1, "Song", tid, disc=disc)
    conn.commit()
    items = plan_missing(conn, scope="all").items
    assert [i.target_rel for i in items] == ["Neil Young/Harvest/D1-01 - Song.flac", "Neil Young/Harvest/D2-01 - Song.flac"]
    stg, lib = dirs
    held = stg / "Neil Young/Harvest/D1-01 - Song [tidal 1].m4a"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"held")
    session = tidal(2)
    res, _ = run(conn, session, FakeHttp({URL.format(2): flac}), dirs, items=items)
    assert (res["held_lossy"], res["placed"], res["failed"]) == (1, 1, 0)
    assert [c[0].split("/")[1] for c in session.calls] == ["2"]  # only track 2 was asked for
    assert held.read_bytes() == b"held"
    assert (lib / "Neil Young/Harvest/D2-01 - Song.flac").is_file()
    assert [p for p in stg.rglob("*") if p.is_file()] == [held]


@needs_ffmpeg
def test_two_discs_with_the_same_track_and_title_are_both_fetched_under_distinct_names(flac, dirs):
    conn = ledger()
    a = add_artist(conn)
    rel = add_release(conn, a, "Harvest", 100)
    for disc, tid in ((1, 1), (2, 2)):
        add_track(conn, rel, add_recording(conn, a, "Intro", isrc=f"{ISRC[:-1]}{tid}"), 1, "Intro", tid, disc=disc)
    conn.commit()
    stg, lib = dirs
    res, _ = run(conn, tidal(2), FakeHttp({URL.format(1): flac, URL.format(2): flac}), dirs)
    assert (res["placed"], res["exists"], res["failed"]) == (2, 0, 0)
    assert sorted(str(p.relative_to(lib)) for p in lib.rglob("*.flac")) == \
        ["Neil Young/Harvest/D1-01 - Intro.flac", "Neil Young/Harvest/D2-01 - Intro.flac"]
    files = rows(conn)
    assert sorted((f["path"], f["tag_disc"]) for f in files) == \
        [("Neil Young/Harvest/D1-01 - Intro.flac", "1"), ("Neil Young/Harvest/D2-01 - Intro.flac", "2")]
    assert len({f["recording_id"] for f in files}) == 2
    assert plan_missing(conn, scope="all").items == []


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
    assert (res["placed"], res["conflict"], res["failed"]) == (0, 1, 0)
    assert dest.read_bytes() == b"precious" and not list(lib.rglob("*.part"))
    assert (stg / f"{STAGED1}.flac").is_file() and rows(conn) == []


def link_hook(monkeypatch, lib, before=None, error=None, seen=None):
    """Wrap os.link for destinations in the library: record the source bytes, run before(dst), maybe raise error."""
    real = os.link

    def link(src, dst, *a, **kw):
        if os.fspath(dst).startswith(os.fspath(lib)) and PROBE not in os.fspath(dst):
            if seen is not None:
                seen.append(open(src, "rb").read())
            if before:
                before(dst)
            if error:
                raise error
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(fetch_run.os, "link", link)


@needs_ffmpeg
def test_a_library_file_that_appears_just_before_the_link_is_not_replaced(flac, dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    dest = lib / "Neil Young/Harvest/01 - Song 1.flac"
    link_hook(monkeypatch, lib, before=lambda dst: dest.write_bytes(b"precious"))  # lands after every lexists check
    res, _ = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["conflict"], res["failed"]) == (0, 1, 0)
    assert dest.read_bytes() == b"precious" and not list(lib.rglob("*.part"))
    assert (stg / f"{STAGED1}.flac").is_file() and rows(conn) == []


@needs_ffmpeg
def test_same_filesystem_placement_links_the_staged_bytes_and_leaves_no_source(flac, dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    seen: list[bytes] = []
    link_hook(monkeypatch, lib, seen=seen)
    res, _ = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    dest = lib / "Neil Young/Harvest/01 - Song 1.flac"
    assert (res["placed"], res["failed"]) == (1, 0) and len(seen) == 1
    assert dest.read_bytes() == seen[0] and dest.stat().st_nlink == 1
    assert [f["path"] for f in rows(conn)] == ["Neil Young/Harvest/01 - Song 1.flac"]
    assert not any(p.is_file() for p in stg.rglob("*")) and not list(lib.rglob("*.part"))


@needs_ffmpeg
@pytest.mark.parametrize("cross", [False, True])
def test_hard_links_unsupported_fails_the_track_without_an_overwriting_fallback(flac, dirs, monkeypatch, cross):
    conn, a = seed(1)
    stg, lib = dirs
    link_hook(monkeypatch, lib, error=OSError(errno.EPERM, "Operation not permitted"))
    if cross:
        cross_device(monkeypatch, lib, [])  # staged link is EXDEV, so a .part is copied, then its link is refused
    res, out = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["failed"]) == (0, 1) and "hard links" in "\n".join(out)
    assert not any(p.is_file() for p in lib.rglob("*")) and list(lib.iterdir()) == []  # no file, no .part, no dirs
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


def test_a_dangling_symlink_at_the_destination_is_a_conflict_and_is_not_replaced(dirs):
    conn, a = seed(1)
    stg, lib = dirs
    target = lib / "Neil Young/Harvest/01 - Song 1.flac"
    target.parent.mkdir(parents=True)
    target.symlink_to(lib / "nowhere.flac")  # dangling
    session, http = tidal(1), FakeHttp()
    res, out = run(conn, session, http, dirs)
    assert (res["placed"], res["exists"], res["conflict"]) == (0, 0, 1)
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
    assert (res["placed"], res["exists"], res["conflict"]) == (0, 0, 2)  # neither path is linked to its track
    assert target.read_bytes() == b"precious" and not (lib / "Neil Young/Harvest/02 - Song 2.flac").exists()
    assert session.calls == [] and http.urls == []  # decided before any Tidal call
    assert len(rows(conn)) == 1


def test_an_unlinked_file_at_the_target_is_a_conflict_that_is_logged_and_fetches_nothing(dirs):
    conn, a = seed(1)
    stg, lib = dirs
    rel = "Neil Young/Harvest/01 - Song 1.flac"
    (lib / rel).parent.mkdir(parents=True)
    (lib / rel).write_bytes(b"placeholder")
    add_file(conn, rel, a)  # unlinked legacy file with the same title, number and length: the planner ignores all that
    conn.execute("UPDATE file SET tag_title = 'Song 1', tag_track = '1', duration_s = 4")
    items = plan_missing(conn, scope="all").items
    assert [i.target_rel for i in items] == [rel]
    session, http = tidal(1), FakeHttp()
    res, out = run(conn, session, http, dirs, items=items, verify=False)
    assert (res["placed"], res["exists"], res["conflict"], res["failed"]) == (0, 0, 1, 0)
    assert session.calls == [] and http.urls == [] and not any(p.is_file() for p in stg.rglob("*"))
    assert (lib / rel).read_bytes() == b"placeholder" and len(rows(conn)) == 1
    [ev] = rows(conn, "event_log")
    assert (ev["kind"], ev["subject"]) == ("fetch.conflict", rel) and '"tidal_track_id": 1' in ev["data_json"]
    assert any(line.startswith("  conflict") and rel in line for line in out) and "1 conflict" in out[-1]


def test_a_target_the_ledger_links_to_this_recording_is_exists_not_a_conflict(dirs):
    conn, a = seed(1)
    items = plan_missing(conn, scope="all").items
    add_file(conn, "Neil Young/Harvest/01 - Song 1.flac", a, conn.execute("SELECT id FROM recording").fetchone()[0])
    res, _ = run(conn, tidal(1), FakeHttp(), dirs, items=items, verify=False)
    assert (res["exists"], res["conflict"]) == (1, 0) and rows(conn, "event_log") == []


@needs_ffmpeg
def test_dry_run_is_the_default_and_makes_no_calls_and_no_writes(flac, dirs):
    conn, a = seed(2)
    stg, lib = dirs
    (lib / "Neil Young/Harvest").mkdir(parents=True)
    (lib / "Neil Young/Harvest/01 - Song 1.flac").write_bytes(b"x")
    session, http, out = tidal(2), FakeHttp(default=flac), []
    before = (len(rows(conn)), len(rows(conn, "event_log")), sorted(p for p in lib.rglob("*")))
    res = fetch_items(conn, session, make_gate(), plan_missing(conn, scope="all").items, stg, lib, out=out.append)
    assert res["dry_run"] and (res["would_fetch"], res["exists"], res["conflict"]) == (1, 0, 1)
    assert session.calls == [] and http.urls == []
    assert not stg.exists()
    assert before == (len(rows(conn)), len(rows(conn, "event_log")), sorted(p for p in lib.rglob("*")))
    text = "\n".join(out)
    assert "would fetch" in text and "Song 2.flac" in text and "conflict" in text


@needs_ffmpeg
def test_429_stops_cleanly_with_no_partial_file(flac, dirs):
    conn, a = seed(3)
    session = tidal(3, t2=TooManyRequests(120))
    http = FakeHttp({URL.format(1): flac, URL.format(3): flac})
    gate = make_gate()
    items = plan_missing(conn, scope="all").items
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


def skippy(n=2):
    """Harvest is partial (Song 1 is owned, linked), Song 2 is planned; 'Lonely' is a release nobody owns (skipped)."""
    conn, a = seed(n)
    add_file(conn, "Neil Young/Harvest/01 - Song 1.flac", a, 1)
    lonely = add_release(conn, a, "Untouched", 200, date="2020-01-01")
    add_track(conn, lonely, add_recording(conn, a, "Lonely"), 1, "Lonely", 99)
    conn.commit()
    return conn


def test_dry_run_and_execute_print_the_same_skip_lines_and_plan_counts(dirs):
    conn = skippy()
    plan = plan_missing(conn)
    assert [i.title for i in plan.items] == ["Song 2"] and plan.owned == 1 and len(plan.skipped) == 1
    stg, lib = dirs
    dry: list[str] = []
    fetch_items(conn, None, None, plan.items, stg, lib, out=dry.append, plan=plan)
    live: list[str] = []
    res = fetch_items(conn, tidal(0), make_gate(), [], stg, lib, dry_run=False, verify=False, out=live.append, plan=plan)
    assert res["stopped"] is None

    def skips(lines):
        return [x for x in lines if x.lstrip().startswith("skip ")]

    def counts(lines):
        return [x for x in lines if x.startswith("plan:") or "skipped:" in x]
    assert skips(dry) == skips(live) == [plan.skipped[0].line()] and len(skips(dry)) == 1
    assert "Untouched / 01 - Lonely  [tidal 99]  (partial scope: release has no owned track)" in skips(dry)[0]
    assert counts(dry) == counts(live) == plan.summary_lines()
    assert counts(dry)[0] == "plan: 1 planned, 1 owned, 1 skipped"
    assert not any("owned under another title" in x or "owned elsewhere" in x for x in dry + live)


def test_a_run_without_a_plan_prints_no_plan_lines(dirs):
    conn, a = seed(1)
    stg, lib = dirs
    out: list[str] = []
    fetch_items(conn, None, None, plan_missing(conn, scope="all").items, stg, lib, out=out.append)
    assert not any(x.startswith("plan:") or x.lstrip().startswith("skip ") for x in out)


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
    res = fetch_items(conn, session, make_gate(), plan_missing(conn, scope="all").items, stg, lib, dry_run=False,
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
    assert "Harvest/02 - Song 2.flac" in out and "WARNING" not in out
    assert [x for x in out.splitlines() if "would fetch" in x and "Lonely" in x] == []  # not planned...
    [skip] = [x for x in out.splitlines() if x.lstrip().startswith("skip ")]  # ...but said so, with reason and id
    assert "Lonely" in skip and "[tidal 99]" in skip and "partial scope: release has no owned track" in skip
    assert "plan: 1 planned, 1 owned, 1 skipped" in out
    assert cli.main(["fetch", "--artist", "neil young", "--scope", "all"]) == 0
    out = capsys.readouterr().out
    assert "Lonely" in out and "WARNING: --scope all plans 2 track(s) across 2 release(s)" in out


@needs_ffmpeg
def test_cli_dry_run_and_execute_print_the_same_skip_lines(cli_env, monkeypatch, capsys):
    from cadence.ledger import db
    c = db.connect(cli_env / "c.db")
    c.execute("INSERT INTO release(artist_id, title, norm_title, type) SELECT 1, 'Untouched', 'untouched', 'album'")
    add_track(c, c.execute("SELECT last_insert_rowid()").fetchone()[0], add_recording(c, 1, "Lonely"), 1, "Lonely", 99)
    add_file(c, "Neil Young/Harvest/01 - Song 1.flac", 1, 1)
    c.commit()
    c.close()
    album = cli_env / "lib" / "Neil Young" / "Harvest"
    album.mkdir(parents=True)
    (album / "02 - Song 2.flac").write_bytes(b"not ours")  # unlinked: Song 2 is planned and ends as a conflict
    monkeypatch.setattr(cli, "_login", lambda gate, token: FakeTidal())
    assert cli.main(["fetch", "--artist", "neil young"]) == 0
    dry = capsys.readouterr().out.splitlines()
    assert cli.main(["fetch", "--execute", "--artist", "neil young"]) == 1  # conflict: exit code unchanged
    live = capsys.readouterr().out.splitlines()

    def skips(lines):
        return [x for x in lines if x.lstrip().startswith("skip ")]
    assert len(skips(dry)) == 1 and skips(dry) == skips(live)
    assert [x for x in dry if x.startswith("plan:")] == [x for x in live if x.startswith("plan:")] == \
        ["plan: 1 planned, 1 owned, 1 skipped"]
    assert "conflict" in "\n".join(live)


def test_cli_limit_says_how_many_planned_items_were_not_attempted(cli_env, capsys):
    assert cli.main(["fetch", "--artist", "neil young", "--scope", "all", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "--limit 1: 1 planned item(s) not attempted" in out and "plan: 2 planned, 0 owned, 0 skipped" in out


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


def test_a_library_mount_without_hard_links_stops_at_setup_before_any_tidal_call(dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    session, http = tidal(1), FakeHttp()

    def no_link(src, dst, *a, **kw):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(fetch_run.os, "link", no_link)
    monkeypatch.setattr("cadence.fetch.run.shutil.which", lambda name: "/usr/bin/ffmpeg")
    res, out = run(conn, session, http, dirs)
    assert res["stopped"] == "setup" and len(res["remaining"]) == 1 and "hard links are required" in out[0]
    assert session.calls == [] and http.urls == [] and list(lib.iterdir()) == []  # the probe files are gone


@needs_ffmpeg
def test_cli_reports_a_conflict_counted_before_a_pause_and_still_exits_3(cli_env, monkeypatch, capsys):
    from cadence.ledger import db
    c = db.connect(cli_env / "c.db")
    add_file(c, "Neil Young/Harvest/01 - Song 1.flac", 1)  # unlinked: Song 1 is a conflict, then Song 2 hits a 429
    c.commit()
    c.close()
    monkeypatch.setattr(cli, "_login", lambda gate, token: FakeTidal({2: TooManyRequests(300)}))
    assert cli.main(["fetch", "--execute", "--artist", "neil young", "--scope", "all"]) == cli.EXIT_PAUSED
    cap = capsys.readouterr()
    assert "gate paused until" in cap.out and "1 conflict" in cap.out
    assert "1 target(s) taken by files the ledger does not link to the track" in cap.err


@needs_ffmpeg
def test_cli_execute_exits_3_on_429_and_persists_the_pause(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_login", lambda gate, token: FakeTidal(default=TooManyRequests(300)))
    assert cli.main(["fetch", "--execute", "--quality", "LOSSLESS", "--scope", "all"]) == cli.EXIT_PAUSED
    assert "gate paused until" in capsys.readouterr().out
    assert not list((cli_env / "lib").rglob("*.flac"))
    monkeypatch.setattr(cli, "_login", lambda *a: pytest.fail("a paused gate must not log in"))
    assert cli.main(["fetch", "--execute", "--scope", "all"]) == cli.EXIT_PAUSED


@needs_ffmpeg
def test_cli_execute_exits_1_when_every_track_is_a_conflict(cli_env, monkeypatch, capsys):
    session = FakeTidal()
    monkeypatch.setattr(cli, "_login", lambda gate, token: session)
    album = cli_env / "lib" / "Neil Young" / "Harvest"
    album.mkdir(parents=True)
    for n in (1, 2):
        (album / f"0{n} - Song {n}.flac").write_bytes(b"not ours")  # on disk, unlinked: the target is taken
    assert cli.main(["fetch", "--execute", "--scope", "all"]) == 1
    cap = capsys.readouterr()
    assert "0 placed" in cap.out and "2 conflict" in cap.out and "stopped=None" in cap.out
    assert "2 target(s) taken" in cap.err and "exit 1" in cap.err
    assert session.calls == []  # a taken target costs no Tidal call


@needs_ffmpeg
def test_a_target_taken_after_the_download_is_a_conflict_that_keeps_and_names_the_staged_copy(flac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    rel = "Neil Young/Harvest/01 - Song 1.flac"
    staged = stg / f"{STAGED1}.flac"

    class RacingHttp(FakeHttp):
        def get(self, url, **kw):
            (lib / rel).parent.mkdir(parents=True, exist_ok=True)
            (lib / rel).write_bytes(b"precious")  # someone else takes the path while the download runs
            return super().get(url, **kw)
    res, out = run(conn, tidal(1), RacingHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["conflict"], res["failed"]) == (0, 1, 0)
    assert (lib / rel).read_bytes() == b"precious" and staged.is_file() and len(rows(conn)) == 0
    [ev] = rows(conn, "event_log")
    assert ev["kind"] == "fetch.conflict" and str(staged) in ev["message"] and str(staged) in ev["data_json"]
    assert "Not fetched" not in ev["message"] and "Downloaded and verified" in ev["message"]
    [line] = [x for x in out if x.startswith("  conflict")]
    assert str(staged) in line and "nothing fetched" not in line


@needs_ffmpeg
def test_a_target_that_appears_between_the_check_and_the_link_is_a_conflict_that_names_the_staged_copy(flac, dirs, monkeypatch):
    conn, a = seed(1)
    stg, lib = dirs
    rel = "Neil Young/Harvest/01 - Song 1.flac"
    staged = stg / f"{STAGED1}.flac"

    def raced(src, dst):
        dst.write_bytes(b"precious")  # lands after the last check, so the no-clobber link is refused
        raise FileExistsError(errno.EEXIST, "library file already exists", str(dst))
    monkeypatch.setattr(fetch_run, "_move_into_library", raced)
    res, out = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["conflict"], res["failed"]) == (0, 1, 0)
    assert (lib / rel).read_bytes() == b"precious" and staged.is_file() and rows(conn) == []
    [ev] = rows(conn, "event_log")
    assert ev["kind"] == "fetch.conflict" and str(staged) in ev["message"] and str(staged) in ev["data_json"]
    [line] = [x for x in out if x.startswith("  conflict")]
    assert str(staged) in line


@needs_ffmpeg
def test_a_file_exists_error_with_nothing_at_the_target_is_still_a_failure(flac, dirs, monkeypatch):
    conn, a = seed(1)

    def refused(src, dst):
        raise FileExistsError(errno.EEXIST, "something else", str(dst))
    monkeypatch.setattr(fetch_run, "_move_into_library", refused)
    res, _ = run(conn, tidal(1), FakeHttp({URL.format(1): flac}), dirs)
    assert (res["placed"], res["conflict"], res["failed"]) == (0, 0, 1)


# ---- collisions: planned, listed up front, a conflict at execute ----

COLLIDING = "Neil Young/Harvest/01 - Song 1.flac"


def hidden_twin_holds_song_1(conn):
    """Song 1 (tidal 1) is planned, but its target path is the path of a file linked to its hidden twin."""
    hidden = add_release(conn, 1, "Harvest", 101)
    conn.execute("UPDATE release SET hidden_reason = 'duplicate' WHERE id = ?", (hidden,))
    add_track(conn, hidden, add_recording(conn, 1, "Song 1 twin"), 1, "Song 1", 90)
    add_file(conn, COLLIDING, 1, conn.execute("SELECT id FROM recording WHERE title = 'Song 1 twin'").fetchone()[0])
    conn.commit()


def test_dry_run_and_execute_print_the_collision_line_before_the_summary_and_the_track_stays_planned(dirs):
    conn, a = seed(2)
    hidden_twin_holds_song_1(conn)
    plan = plan_missing(conn, scope="all")
    assert [i.tidal_track_id for i in plan.items] == [1, 2] and len(plan.collisions) == 1
    line = (f"  collision  {COLLIDING}  planned tidal 1, file already linked to another recording  "
            "(may conflict at execute)")
    stg, lib = dirs
    dry: list[str] = []
    fetch_items(conn, None, None, plan.items, stg, lib, out=dry.append, plan=plan)
    live: list[str] = []
    # the path is only in the ledger, not on disk: the executor still sees it taken (it checks the ledger too)
    res = fetch_items(conn, tidal(0), make_gate(), plan.items[:1], stg, lib, dry_run=False, verify=False,
                      out=live.append, plan=plan)
    assert line in dry and line in live
    for lines in (dry, live):
        assert lines.index(line) < [x.startswith("plan:") for x in lines].index(True)
        assert [x for x in lines if x.startswith("plan:")] == ["plan: 2 planned, 1 owned, 0 skipped, 1 collision"]
    assert any("conflict" in x and COLLIDING in x and "would fetch" not in x for x in dry)
    assert res["conflict"] == 1 and res["placed"] == 0 and not (lib / COLLIDING).exists()


def test_a_collision_line_is_not_printed_for_an_item_that_is_not_attempted(dirs):
    conn, a = seed(2)
    hidden_twin_holds_song_1(conn)
    plan = plan_missing(conn, scope="all")
    out: list[str] = []
    fetch_items(conn, None, None, plan.items[1:], dirs[0], dirs[1], out=out.append, plan=plan)
    assert not any(x.lstrip().startswith("collision") for x in out)


@needs_ffmpeg
def test_cli_collision_is_listed_in_the_dry_run_and_the_execute_exits_1_only_through_the_conflict(cli_env, monkeypatch,
                                                                                                capsys):
    from cadence.ledger import db
    c = db.connect(cli_env / "c.db")
    hidden_twin_holds_song_1(c)
    c.close()
    monkeypatch.setattr(cli, "_login", lambda gate, token: FakeTidal())
    assert cli.main(["fetch", "--artist", "neil young", "--scope", "all", "--limit", "1"]) == 0  # dry run: exit code unchanged
    dry = capsys.readouterr().out
    assert cli.main(["fetch", "--execute", "--artist", "neil young", "--scope", "all", "--limit", "1"]) == 1
    live = capsys.readouterr().out
    for out in (dry, live):
        assert [x for x in out.splitlines() if x.lstrip().startswith("collision")] == [
            f"  collision  {COLLIDING}  planned tidal 1, file already linked to another recording  "
            "(may conflict at execute)"]
    assert "1 conflict" in live and not (cli_env / "lib" / "Neil Young").exists()


# ---- duplicate targets inside one plan: the later item is a conflict in the dry run and at execute ----

def twin_aaa(dest="server"):
    """Two releases titled 'Aaa LP', track 1 'Song' on different recordings: one target path, tidal ids 1 and 2."""
    conn = ledger()
    a = add_artist(conn, dest=dest)
    for tid, date in ((1, "2000-01-01"), (2, "2001-01-01")):
        rel = add_release(conn, a, "Aaa LP", 100 + tid, date=date)
        add_track(conn, rel, add_recording(conn, a, "Song", isrc=f"{ISRC[:-1]}{tid}"), 1, "Song", tid)
    conn.commit()
    return conn


@needs_ffmpeg
def test_dry_run_counts_the_later_duplicate_target_as_a_conflict_like_execute(flac, dirs):
    conn = twin_aaa()
    plan = plan_missing(conn, scope="all")
    path = "Neil Young/Aaa LP/01 - Song.flac"
    assert [(i.tidal_track_id, i.target_rel) for i in plan.items] == [(1, path), (2, path)]
    assert [(c.tidal_track_id, c.kind) for c in plan.collisions] == [(2, "duplicate")]
    stg, lib = dirs
    dry_out: list[str] = []
    dry = fetch_items(conn, None, None, plan.items, stg, lib, dry_run=True, quality="LOSSLESS", out=dry_out.append,
                      plan=plan)
    states = [x.split("  ")[1].strip() for x in dry_out if x.startswith("  ") and path in x and "collision" not in x]
    assert states == ["would fetch", "conflict"]
    assert (dry["would_fetch"], dry["conflict"]) == (1, 1)
    assert "1 would be fetched, 0 already exist, 1 conflict" in dry_out[-1]
    res, _ = run(conn, tidal(2), FakeHttp({URL.format(1): flac, URL.format(2): flac}), dirs, items=plan.items, plan=plan)
    assert (res["placed"], res["conflict"]) == (dry["would_fetch"], dry["conflict"])


def test_dry_run_duplicate_conflict_needs_the_earlier_item_to_be_fetched(dirs):
    stg, lib = dirs
    conn = twin_aaa()
    plan = plan_missing(conn, scope="all")
    later = fetch_items(conn, None, None, plan.items[1:], stg, lib, out=lambda x: None, plan=plan)  # --limit-like subset
    assert (later["would_fetch"], later["conflict"]) == (1, 0)
    conn = twin_aaa("fiio")                                  # nothing is placed for fiio-only items: no conflict
    plan = plan_missing(conn, scope="all")
    both = fetch_items(conn, None, None, plan.items, stg, lib, out=lambda x: None, plan=plan)
    assert (both["skipped_dest"], both["conflict"], both["would_fetch"]) == (2, 0, 0)
    res, _ = run(conn, tidal(0), FakeHttp(), dirs, items=plan.items, plan=plan, verify=False)
    assert (res["skipped_dest"], res["conflict"]) == (2, 0)


# ---- fiio-only items: the occupied-path check still runs at execute ----

def fiio_seed(n=2):
    conn, a = seed(n)
    conn.execute("UPDATE artist SET default_dest = 'fiio'")
    conn.commit()
    return conn, a


def test_a_fiio_item_whose_target_is_held_by_a_linked_foreign_file_is_a_conflict_at_execute(dirs):
    conn, a = fiio_seed(2)
    hidden_twin_holds_song_1(conn)
    plan = plan_missing(conn, scope="all")
    assert [i.dest for i in plan.items] == ["fiio", "fiio"] and len(plan.collisions) == 1
    session, http = tidal(0), FakeHttp()
    res, out = run(conn, session, http, dirs, items=plan.items, plan=plan, verify=False)
    assert (res["conflict"], res["skipped_dest"], res["placed"]) == (1, 1, 0)
    [ev] = [r for r in rows(conn, "event_log") if r["kind"] == "fetch.conflict"]
    assert ev["subject"] == COLLIDING and '"tidal_track_id": 1' in ev["data_json"]
    assert f"  collision  {COLLIDING}  planned tidal 1, file already linked to another recording  " \
           "(fiio-only, checked in dry run and at execute)" in out
    assert "1 conflict" in out[-1] and "1 fiio-skipped" in out[-1]
    assert session.calls == [] and http.urls == []


@needs_ffmpeg
def test_cli_execute_exits_1_for_a_fiio_item_colliding_with_a_linked_file(cli_env, monkeypatch, capsys):
    from cadence.ledger import db
    c = db.connect(cli_env / "c.db")
    c.execute("UPDATE artist SET default_dest = 'fiio'")
    hidden_twin_holds_song_1(c)
    c.close()
    monkeypatch.setattr(cli, "_login", lambda gate, token: FakeTidal())
    assert cli.main(["fetch", "--execute", "--artist", "neil young", "--scope", "all", "--limit", "1"]) == 1
    cap = capsys.readouterr()
    assert "1 conflict" in cap.out and "exit 1" in cap.err


def test_dry_run_and_execute_agree_on_a_fiio_item_whose_target_is_held_by_an_unlinked_file(dirs):
    conn, a = fiio_seed(2)
    stg, lib = dirs
    rel = "Neil Young/Harvest/01 - Song 1.flac"
    add_file(conn, rel, a)  # unlinked: the planner ignores it, the executor and the dry run must not
    items = plan_missing(conn, scope="all").items
    before = rows(conn, "file"), rows(conn, "event_log")
    dry_out: list[str] = []
    dry = fetch_items(conn, None, None, items, stg, lib, dry_run=True, quality="LOSSLESS", out=dry_out.append)
    assert (dry["conflict"], dry["skipped_dest"]) == (1, 1) and (rows(conn, "file"), rows(conn, "event_log")) == before
    assert any(x.split()[0] == "conflict" and rel in x for x in dry_out) and "1 conflict" in dry_out[-1]
    res, out = run(conn, tidal(0), FakeHttp(), dirs, items=items, verify=False)
    assert (res["conflict"], res["skipped_dest"]) == (dry["conflict"], dry["skipped_dest"]) and "1 conflict" in out[-1]


def test_fiio_items_without_a_collision_are_skipped_and_counted_in_the_execute_summary(dirs):
    conn, a = fiio_seed(2)
    items = plan_missing(conn, scope="all").items
    add_file(conn, "Neil Young/Harvest/02 - Song 2.flac", a, 2)  # linked to its own track since: not a conflict
    res, out = run(conn, tidal(0), FakeHttp(), dirs, items=items, verify=False)
    assert (res["skipped_dest"], res["conflict"], res["exists"]) == (2, 0, 0)
    assert "0 conflict" in out[-1] and "2 fiio-skipped" in out[-1] and rows(conn, "event_log") == []


# ---- early exits still print what the plan decided ----

def orphan_skip(conn):
    """A track with no Tidal id: the plan skips it ('no tidal id')."""
    add_track(conn, 1, add_recording(conn, 1, "Orphan"), 9, "Orphan", None)
    conn.commit()


def test_a_setup_error_prints_the_plan_skip_lines_before_the_error(dirs):
    conn, a = seed(1)
    orphan_skip(conn)
    plan = plan_missing(conn, scope="all")
    stg, lib = dirs
    out: list[str] = []
    res = fetch_items(conn, tidal(1), make_gate(), plan.items, stg, lib / "missing", dry_run=False, out=out.append,
                      plan=plan)
    assert res["stopped"] == "setup" and len(res["remaining"]) == 1
    [skip] = [x for x in out if x.lstrip().startswith("skip ")]
    assert "Orphan" in skip and "no tidal id" in skip
    assert out.index(skip) < [i for i, x in enumerate(out) if "does not exist" in x][0]
    assert "plan: 1 planned, 0 owned, 1 skipped" in out


@pytest.mark.parametrize("failure", ["error", "paused"])
def test_cli_login_failure_after_the_plan_still_prints_the_skip_lines(cli_env, monkeypatch, capsys, failure):
    from datetime import datetime, timedelta, timezone
    from cadence.gate.bucket import GatePaused
    from cadence.ledger import db
    c = db.connect(cli_env / "c.db")
    orphan_skip(c)
    c.close()

    def login(gate, token):
        raise GatePaused(datetime.now(timezone.utc) + timedelta(minutes=5), "test") if failure == "paused" \
            else RuntimeError("no token")
    monkeypatch.setattr(cli, "_login", login)
    assert cli.main(["fetch", "--execute", "--artist", "neil young", "--scope", "all"]) == \
        (cli.EXIT_PAUSED if failure == "paused" else 1)
    cap = capsys.readouterr()
    [skip] = [x for x in cap.out.splitlines() if x.lstrip().startswith("skip ")]
    assert "Orphan" in skip and "no tidal id" in skip
    assert "plan: 2 planned, 0 owned, 1 skipped" in cap.out


def test_cli_paused_gate_after_the_plan_still_prints_the_skip_lines(cli_env, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from cadence.ledger import db
    c = db.connect(cli_env / "c.db")
    orphan_skip(c)
    c.close()
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    monkeypatch.setattr(cli, "_gate", lambda conn, *a: SimpleNamespace(paused_until=lambda: until))
    monkeypatch.setattr(cli, "_login", lambda *a: pytest.fail("a paused gate must not log in"))
    assert cli.main(["fetch", "--execute", "--artist", "neil young", "--scope", "all"]) == cli.EXIT_PAUSED
    out = capsys.readouterr().out
    assert any(x.lstrip().startswith("skip ") and "no tidal id" in x for x in out.splitlines())
    assert out.index("no tidal id") < out.index("gate paused until")
