"""OrpheusFallback: the subprocess runner is injected; nothing real is started."""
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import mutagen.flac
import pytest

from cadence.fetch.fallback import FallbackMiss, OrpheusFallback, _flac_tags
from cadence.fetch.plan import FetchItem
from cadence.gate.bucket import GatePaused
from tests.fakes import make_gate
from tests.fetch_fakes import FFMPEG, make_flac

ITEM = FetchItem(1, "Neil Young", 1, 100, "Harvest", "Neil Young/Harvest", 1, 1, "Heart of Gold", 77, 5, 187, None, None,
                 "server", "Neil Young/Harvest/01 - Heart of Gold.flac")


class Runner:
    """subprocess.run stand-in; `act(cmd, workdir)` writes files and returns (returncode, stdout, stderr)."""

    def __init__(self, act=None, exc=None):
        self.act, self.exc, self.calls = act, exc, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        rc, out, err = self.act(cmd, Path(cmd[cmd.index("-o") + 1])) if self.act else (0, "", "")
        return NS(returncode=rc, stdout=out, stderr=err)


def writes(*names, rc=0, out="", err=""):
    def act(cmd, workdir):
        for n in names:
            (workdir / n).parent.mkdir(parents=True, exist_ok=True)
            (workdir / n).write_bytes(b"x")
        return rc, out, err
    return act


GOOD_TAGS = {"title": ["Heart of Gold"], "artist": ["Neil Young"], "albumartist": ["Neil Young"], "album": ["Harvest"]}


def tags(**over):
    return lambda path: {**GOOD_TAGS, **over}


def orpheus(runner, module="qobuz", **kw):
    return OrpheusFallback("/opt/orpheus", module, python="py3", timeout=50, run=runner,
                           read_tags=kw.pop("read_tags", tags()), **kw)


def test_tidal_module_downloads_by_id_through_the_gate(tmp_path):
    r, gate = Runner(writes("Artist/Album/01 x.flac")), make_gate()
    got = orpheus(r, "tidal").acquire(ITEM, tmp_path, gate)
    [(cmd, kw)] = r.calls
    assert cmd == ["py3", "orpheus.py", "-o", str(tmp_path), "download", "tidal", "track", "77"]
    assert kw["cwd"] == "/opt/orpheus" and kw["stdin"] == subprocess.DEVNULL and kw["timeout"] == 50
    assert kw["capture_output"] is True
    assert gate.calls == 1
    assert got.path == tmp_path / "Artist/Album/01 x.flac" and "tidal" in got.ref


def test_other_modules_use_luckysearch_and_skip_the_tidal_gate(tmp_path):
    r, gate = Runner(writes("a.flac")), make_gate()
    got = orpheus(r, "qobuz").acquire(ITEM, tmp_path, gate)
    assert r.calls[0][0] == ["py3", "orpheus.py", "-o", str(tmp_path), "luckysearch", "qobuz", "track",
                             "Neil Young Heart of Gold"]
    assert gate.calls == 0 and got.ref == "qobuz luckysearch: Neil Young Heart of Gold"


def test_the_exit_code_is_ignored_when_exactly_one_flac_appeared(tmp_path):
    got = orpheus(Runner(writes("a.flac", rc=1))).acquire(ITEM, tmp_path, make_gate())
    assert got.path.name == "a.flac"


def test_exit_zero_without_a_file_is_a_miss_with_a_sanitized_last_line(tmp_path):
    err = "first line\n" + "E" * 500 + "login failed"
    with pytest.raises(FallbackMiss) as e:
        orpheus(Runner(writes(rc=0, err=err))).acquire(ITEM, tmp_path, make_gate())
    assert str(e.value) == "orpheus produced no FLAC: … failed"


def test_stderr_in_a_miss_loses_url_queries_and_long_tokens(tmp_path):
    err = ("ignored\nGET https://api.example.com/track/1?token=abc&user=me failed, key "
           "sk_live_AbCdEfGhIjKlMnOpQrStUvWx and ABCDEFGHIJKLMNOPQRST1+/= ok\n\n")
    with pytest.raises(FallbackMiss) as e:
        orpheus(Runner(writes(err=err))).acquire(ITEM, tmp_path, make_gate())
    msg = str(e.value)
    assert "?token" not in msg and "user=me" not in msg and "sk_live" not in msg and "ABCDEFGHIJKLMNOPQRST" not in msg
    assert "https://api.example.com/track/1…" in msg and "ignored" not in msg and "\n" not in msg
    short = Runner(writes(err="short ok_token-19chars_x"))  # 19 chars: kept
    with pytest.raises(FallbackMiss, match="short ok_token-19chars_x"):
        orpheus(short).acquire(ITEM, tmp_path, make_gate())


def test_stdout_is_used_when_stderr_is_empty(tmp_path):
    with pytest.raises(FallbackMiss, match="nothing found"):
        orpheus(Runner(writes(out="nothing found\n"))).acquire(ITEM, tmp_path, make_gate())


def test_a_non_flac_output_does_not_count(tmp_path):
    with pytest.raises(FallbackMiss, match="no FLAC"):
        orpheus(Runner(writes("a.mp3", "b.m4a"))).acquire(ITEM, tmp_path, make_gate())


def test_two_flacs_are_ambiguous(tmp_path):
    with pytest.raises(FallbackMiss, match="ambiguous output"):
        orpheus(Runner(writes("a.flac", "sub/b.flac"))).acquire(ITEM, tmp_path, make_gate())


def test_a_timeout_is_a_miss(tmp_path):
    r = Runner(exc=subprocess.TimeoutExpired("orpheus.py", 50))
    with pytest.raises(FallbackMiss, match="timed out after 50"):
        orpheus(r).acquire(ITEM, tmp_path, make_gate())


def test_a_missing_python_is_a_miss(tmp_path):
    with pytest.raises(FallbackMiss, match="could not start"):
        orpheus(Runner(exc=FileNotFoundError("py3"))).acquire(ITEM, tmp_path, make_gate())


def test_check_wants_orpheus_py_and_a_python(tmp_path):
    assert "orpheus.py" in OrpheusFallback(str(tmp_path), "qobuz", python=sys.executable).check()
    (tmp_path / "orpheus.py").write_text("")
    assert OrpheusFallback(str(tmp_path), "qobuz", python=sys.executable).check() is None
    assert "no-such-python" in OrpheusFallback(str(tmp_path), "qobuz", python="no-such-python").check()


# ---- luckysearch results are checked against the planned track; download-by-id is exempt ----

def miss(tmp_path, reader, item=ITEM):
    with pytest.raises(FallbackMiss) as e:
        orpheus(Runner(writes("a.flac")), read_tags=reader).acquire(item, tmp_path, make_gate())
    return str(e.value)


def test_a_matching_tag_set_is_accepted(tmp_path):
    assert orpheus(Runner(writes("a.flac")), read_tags=tags(title=["Heart Of Gold"], artist=["Neil Young & Crazy Horse"],
                                                            albumartist=[])).acquire(ITEM, tmp_path, make_gate())


@pytest.mark.parametrize("over, text", [
    ({"title": ["Old Man"]}, "title"),
    ({"title": ["Heart of Gold (Live)"]}, "title"),
    ({"title": []}, "missing title"),
    ({"title": [" "]}, "missing title"),
    ({"artist": ["Someone Else"], "albumartist": ["Someone Else"]}, "artist"),
    ({"artist": [], "albumartist": []}, "artist"),
    ({"album": ["Live at Massey Hall"]}, "live"),
    ({"title": ["Heart of Gold"], "version": ["Remix"]}, "title"),
], ids=["other-song", "live-title", "no-title", "blank-title", "other-artist", "no-artist", "live-album", "version"])
def test_wrong_tags_are_a_miss(tmp_path, over, text):
    assert text in miss(tmp_path, tags(**over))


def test_a_variant_in_the_planned_title_is_allowed_in_the_tags(tmp_path):
    live = ITEM.__class__(**{**ITEM.__dict__, "title": "Heart of Gold (Live)"})
    got = orpheus(Runner(writes("a.flac")), read_tags=tags(title=["Heart of Gold"], version=["Live"])).acquire(
        live, tmp_path, make_gate())
    assert got.path.name == "a.flac"


def test_unreadable_tags_are_a_miss(tmp_path):
    def broken(path):
        raise ValueError("not a flac")
    assert "tags" in miss(tmp_path, broken)


def test_a_title_without_words_is_a_miss_for_luckysearch(tmp_path):
    odd = ITEM.__class__(**{**ITEM.__dict__, "title": "♥"})
    assert "searchable" in miss(tmp_path, tags(title=["♥"]), odd)


def test_the_tidal_module_skips_the_tag_check(tmp_path):
    def never(path):
        raise AssertionError("tags must not be read for a download by id")
    got = orpheus(Runner(writes("a.flac")), "tidal", read_tags=never).acquire(ITEM, tmp_path, make_gate())
    assert got.path.name == "a.flac"


# ---- a Tidal 429 inside orpheus pauses the shared gate ----

@pytest.mark.parametrize("text", ["429 Client Error: Too Many Requests for url", "Too Many Requests", "too many requests, slow down",
                                  "tidalapi.exceptions.TooManyRequests", "request failed with status code 429",
                                  "HTTP 429", "http 429 from api"])
def test_a_429_error_shape_with_no_flac_pauses_the_gate(tmp_path, text):
    gate = make_gate()
    with pytest.raises(GatePaused):
        orpheus(Runner(writes(err=text)), "tidal").acquire(ITEM, tmp_path, gate)
    assert gate.paused_until() is not None


@pytest.mark.parametrize("text", ["Room 429", "Downloading 429 KB", "track 1429001", "429"])
def test_a_bare_429_is_not_a_rate_limit(tmp_path, text):
    gate = make_gate()
    with pytest.raises(FallbackMiss):  # no FLAC: a plain miss
        orpheus(Runner(writes(err=text)), "tidal").acquire(ITEM, tmp_path, gate)
    assert gate.paused_until() is None
    got = orpheus(Runner(writes("a.flac", out=text)), "tidal").acquire(ITEM, tmp_path, make_gate())
    assert got.path.name == "a.flac"


def test_a_flac_that_was_produced_wins_over_error_looking_output(tmp_path):
    gate = make_gate()
    got = orpheus(Runner(writes("a.flac", err="429 Client Error: Too Many Requests (a retry then worked)")), "tidal").acquire(
        ITEM, tmp_path, gate)
    assert got.path.name == "a.flac" and gate.paused_until() is None


def test_a_429_from_another_module_is_only_a_miss(tmp_path):
    gate = make_gate()
    with pytest.raises(FallbackMiss):
        orpheus(Runner(writes(err="429 Client Error: Too Many Requests")), "qobuz").acquire(ITEM, tmp_path, gate)
    assert gate.paused_until() is None


# ---- the real tag reader, on a real FLAC ----

needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
QUEEN = ITEM.__class__(**{**ITEM.__dict__, "artist": "Queen", "title": "Love of My Life"})


def tagged_flac(tmp_path, **tags):
    src = tmp_path / "src.flac"
    make_flac(src, 2)
    t = mutagen.flac.FLAC(src)
    for k, v in tags.items():
        t[k] = v
    t.save()
    return src


def copying(src):
    def act(cmd, workdir):
        shutil.copy(src, workdir / "out.flac")
        return 0, "", ""
    return act


@needs_ffmpeg
def test_the_real_reader_returns_lists_of_strings(tmp_path):
    src = tagged_flac(tmp_path, TITLE=["Love of My Life (2011 Remaster)"], ARTIST=["Queen", "Brian May"])
    assert _flac_tags(src) == {"title": ["Love of My Life (2011 Remaster)"], "artist": ["Queen", "Brian May"]}


@needs_ffmpeg
@pytest.mark.parametrize("title, item, artist", [
    ("Love of My Life", QUEEN, ["Queen"]),
    ("Love of My Life (2011 Remaster)", QUEEN, ["Queen"]),
    ("Heart of Gold (feat. Crazy Horse)", ITEM, ["Neil Young"]),
    ("Heart of Gold", ITEM, ["Neil Young", "Crazy Horse"]),
])
def test_a_real_flac_whose_plain_title_matches_passes_the_real_reader(tmp_path, title, item, artist):
    src = tagged_flac(tmp_path, TITLE=[title], ARTIST=artist, ALBUM=["Album"])
    (tmp_path / "w").mkdir()
    fb = OrpheusFallback("/opt/orpheus", "qobuz", python="py3", run=Runner(copying(src)))
    assert fb.acquire(item, tmp_path / "w", make_gate()).path.name == "out.flac"


@needs_ffmpeg
@pytest.mark.parametrize("tags", [{"TITLE": ["Old Man"], "ARTIST": ["Neil Young"]},
                                  {"ARTIST": ["Neil Young"]},
                                  {"TITLE": ["Heart of Gold"], "ARTIST": ["Someone"]}], ids=["title", "no-title", "artist"])
def test_a_real_flac_with_wrong_tags_is_a_miss_with_the_real_reader(tmp_path, tags):
    src = tagged_flac(tmp_path, **tags)
    (tmp_path / "w").mkdir()
    fb = OrpheusFallback("/opt/orpheus", "qobuz", python="py3", run=Runner(copying(src)))
    with pytest.raises(FallbackMiss):
        fb.acquire(ITEM, tmp_path / "w", make_gate())


# ---- wrong-track bypasses in the tags ----

@pytest.mark.parametrize("over", [
    {"artist": ["Queen Latifah"], "albumartist": ["Queen Latifah"]},
    {"album": ["A Tribute to Queen"]},
    {"album": ["Queen Covers"]},
    {"title": ["Love of My Life (Unplugged)"]},
    {"album": ["Greatest Hits (Concert)"]},
    {"album": ["The BBC Sessions"]},
])
def test_tag_bypasses_are_misses(tmp_path, over):
    base = {"title": ["Love of My Life"], "artist": ["Queen"], "albumartist": ["Queen"], "album": ["A Night at the Opera"]}
    with pytest.raises(FallbackMiss):
        orpheus(Runner(writes("a.flac")), read_tags=lambda p: {**base, **over}).acquire(QUEEN, tmp_path, make_gate())
    ok = orpheus(Runner(writes("a.flac")), read_tags=lambda p: base).acquire(QUEEN, tmp_path, make_gate())
    assert ok.path.name == "a.flac"


@pytest.mark.parametrize("artist", [["Queen & Adam Lambert"], ["Queen", "Adam Lambert"], ["Queen feat. Adam Lambert"],
                                    ["Adam Lambert; Queen"]])
def test_a_credited_group_of_artists_still_names_the_artist(tmp_path, artist):
    got = orpheus(Runner(writes("a.flac")), read_tags=tags(title=["Love of My Life"], artist=artist, albumartist=[])
                  ).acquire(QUEEN, tmp_path, make_gate())
    assert got.path.name == "a.flac"
