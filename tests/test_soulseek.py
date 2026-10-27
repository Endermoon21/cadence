"""SoulseekFallback against a fake slskd. No network; the clock and sleep are fake."""
import json
from dataclasses import replace

import pytest
import requests

from cadence.fetch.fallback import FallbackMiss, SoulseekFallback, VpnDown, rank_candidates
from cadence.fetch.plan import FetchItem
from tests.fakes import FakeTime, make_gate
from tests.fetch_fakes import FakeSlskd, sl_file, sl_resp

KEY = "SECRET-KEY-123"
URL = "http://slskd.local:5030"
ITEM = FetchItem(1, "Neil Young", 1, 100, "Harvest", "Neil Young/Harvest", 1, 1, "Heart of Gold", 77, 5, 187, None, None,
                 "server", "Neil Young/Harvest/01 - Heart of Gold.flac")
GOOD = "Music\\Neil Young\\Harvest\\01 Heart of Gold.flac"


def names(item, *resps):
    return [(r["username"], f["filename"]) for r, f in rank_candidates(item, list(resps))]


# ---- ranking and filters ----

def test_ranking_free_slot_then_queue_then_bit_depth_then_speed():
    resps = [sl_resp("busy", sl_file(GOOD), free=False, queue=0),
             sl_resp("q5", sl_file(GOOD), queue=5),
             sl_resp("q1_16", sl_file(GOOD, bit=16), queue=1, speed=9),
             sl_resp("q1_24", sl_file(GOOD, bit=24), queue=1, speed=1),
             sl_resp("q1_24_fast", sl_file(GOOD, bit=24), queue=1, speed=5)]
    assert [u for u, _ in names(ITEM, *resps)] == ["q1_24_fast", "q1_24", "q1_16", "q5", "busy"]


@pytest.mark.parametrize("f", [
    sl_file(GOOD, locked=True),
    sl_file("Music\\Neil Young\\Harvest\\01 Heart of Gold.mp3", ext="mp3"),
    sl_file(GOOD, length=191),
    sl_file(GOOD, length=183),
    sl_file(GOOD, length=None),
    sl_file("Music\\Neil Young\\Harvest\\01 Old Man.flac"),                      # title tokens missing
    sl_file("Music\\Other Band\\Harvest\\01 Heart of Gold.flac"),                # artist tokens missing
    sl_file("Music\\Neil Young\\Harvest\\01 Heart of Gold (Remix).flac"),        # variant word not in the title
    sl_file("Music\\Neil Young\\Harvest\\01 Heart of Gold live.flac"),
    sl_file("Music\\Neil Young\\Harvest\\01 Heart of Gold - Karaoke.flac"),
], ids=["locked", "mp3", "long", "short", "no-length", "no-title", "no-artist", "remix", "live", "karaoke"])
def test_filters_reject(f):
    assert names(ITEM, sl_resp("u", f)) == []


@pytest.mark.parametrize("f", [
    sl_file(GOOD, ext=""),                                                       # extension may be empty
    sl_file(GOOD.upper().replace("HEART OF GOLD", "heart of gold"), ext="FLAC"),
    sl_file("Music/Neil Young/Harvest/01 Heart of Gold.flac"),                  # forward slashes
    sl_file(GOOD, length=190),                                                   # 3 s off is fine
    sl_file(GOOD, length=184),
    sl_file("Neil Young - Harvest\\Heart of Gold.flac"),                         # artist only in the folder
], ids=["no-ext", "upper", "slashes", "plus3", "minus3", "artist-in-folder"])
def test_filters_accept(f):
    assert len(names(ITEM, sl_resp("u", f))) == 1


def test_a_variant_word_in_the_planned_title_is_allowed():
    live = replace(ITEM, title="Heart of Gold (Live)")
    f = sl_file("Music\\Neil Young\\Heart of Gold (Live).flac")
    assert len(names(live, sl_resp("u", f))) == 1


# ---- the flow ----

def sk(tmp_path, ft, api, **kw):
    return SoulseekFallback(URL, KEY, tmp_path / "dl", timeout=kw.pop("timeout", 100), http=api, sleep=api.sleep,
                            clock=ft.clock, **kw)


def rig(tmp_path, responses, **kw):
    ft = FakeTime()
    (tmp_path / "dl").mkdir(exist_ok=True)
    api = FakeSlskd(ft, tmp_path / "dl", responses, **kw)
    return ft, api


def posts(api, suffix):
    return [c for c in api.calls if c[0] == "post" and c[1].endswith(suffix)]


def test_succeeded_download_is_located_and_returned(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"},
                  states={GOOD: ["Queued, Remotely", "InProgress", "Completed, Succeeded"]})
    got = sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert got.path == tmp_path / "dl" / "Harvest" / "01 Heart of Gold.flac"
    assert got.path.read_bytes() == b"audio"
    assert got.ref == f"bob:{GOOD}" and got.detail == {"username": "bob", "filename": GOOD, "size": 5}
    [(_, url, body, hdr, timeout, _r)] = posts(api, "/searches")
    assert url == f"{URL}/api/v0/searches" and timeout == (10, 30)
    assert body["searchText"] == "Neil Young Heart of Gold" and body["searchTimeout"] == 15000
    assert body["filterResponses"] is True and body["responseLimit"] == 100 and body["fileLimit"] == 10000
    assert isinstance(body["id"], str) and len(body["id"]) == 36
    [(_, url, body, _h, _t, _r)] = posts(api, "/transfers/downloads/bob")
    assert url == f"{URL}/api/v0/transfers/downloads/bob" and body == [{"filename": GOOD, "size": 5}]


def test_the_api_key_goes_in_a_header_on_every_call_and_nowhere_else(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"},
                  states={GOOD: ["Completed, Succeeded"]})
    sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert api.calls and all(c[3] == {"X-API-Key": KEY} for c in api.calls)
    assert not any(KEY in c[1] or KEY in json.dumps(c[2]) for c in api.calls)


def test_completed_errored_moves_on_to_the_next_candidate_without_cancelling(tmp_path):
    g2 = "Music\\Neil Young\\Harvest\\02 Heart of Gold.flac"
    ft, api = rig(tmp_path, [sl_resp("a", sl_file(GOOD, size=5), queue=0), sl_resp("b", sl_file(g2, size=5), queue=1)],
                  files={g2: b"audio"}, states={GOOD: ["Completed, Errored"], g2: ["Completed, Succeeded"]})
    got = sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert got.ref == f"b:{g2}" and not [c for c in api.calls if c[0] == "delete"]


def test_a_timeout_cancels_the_transfer_then_tries_the_next(tmp_path):
    g2 = "Music\\Neil Young\\Harvest\\02 Heart of Gold.flac"
    ft, api = rig(tmp_path, [sl_resp("a", sl_file(GOOD, size=5), queue=0), sl_resp("b", sl_file(g2, size=5), queue=1)],
                  files={g2: b"audio"}, states={GOOD: ["InProgress"], g2: ["Completed, Succeeded"]})
    got = sk(tmp_path, ft, api, timeout=30).acquire(ITEM, tmp_path / "wd", make_gate())
    deletes = [c for c in api.calls if c[0] == "delete"]
    assert [c[1] for c in deletes] == [f"{URL}/api/v0/transfers/downloads/a/id-{GOOD}"]
    assert got.ref == f"b:{g2}" and ft.t >= 30
    assert api.calls.index(deletes[0]) < api.calls.index(posts(api, "/transfers/downloads/b")[0])


def test_the_file_may_land_in_the_downloads_dir_a_little_after_success(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"},
                  states={GOOD: ["Completed, Succeeded"]}, appear_after=3)
    got = sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert got.path.read_bytes() == b"audio" and ft.t >= 3


def test_completed_but_never_found_is_a_miss_after_30_seconds(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], states={GOOD: ["Completed, Succeeded"]})
    with pytest.raises(FallbackMiss, match="completed but not found in downloads dir"):  # inside "no candidate completed"
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert 30 <= ft.t < 60


def test_a_size_mismatch_does_not_count_as_found(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio!!"},
                  states={GOOD: ["Completed, Succeeded"]})
    with pytest.raises(FallbackMiss, match="not found"):
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())


def test_two_matching_files_are_ambiguous(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"},
                  states={GOOD: ["Completed, Succeeded"]})
    other = tmp_path / "dl" / "elsewhere" / "01 Heart of Gold.flac"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"audio")
    with pytest.raises(FallbackMiss, match="ambiguous"):
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())


def test_no_candidate_is_a_miss_and_nothing_is_enqueued(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, length=999))])
    with pytest.raises(FallbackMiss, match="no matching FLAC on Soulseek"):
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert not posts(api, "/transfers/downloads/bob")


def test_only_max_candidates_are_tried(tmp_path):
    resps = [sl_resp(f"u{i}", sl_file(GOOD, size=5), queue=i) for i in range(4)]
    ft, api = rig(tmp_path, resps, states={GOOD: ["Completed, Cancelled"]})
    with pytest.raises(FallbackMiss):
        sk(tmp_path, ft, api, max_candidates=3).acquire(ITEM, tmp_path / "wd", make_gate())
    assert [c[1].rsplit("/", 1)[1] for c in api.calls if c[0] == "post" and "/transfers/" in c[1]] == ["u0", "u1", "u2"]


def test_usernames_are_url_quoted(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("a b/c", sl_file(GOOD, size=5))], states={GOOD: ["Completed, Errored"]})
    with pytest.raises(FallbackMiss):
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert posts(api, "/transfers/downloads/a%20b%2Fc")


# ---- errors never carry the key ----

def test_http_failures_are_misses_that_do_not_contain_the_key(tmp_path):
    ft, api = rig(tmp_path, [], raise_exc=requests.ConnectionError(f"boom {KEY} http://x/?key={KEY}"))
    with pytest.raises(FallbackMiss) as e:
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert KEY not in str(e.value) and "ConnectionError" in str(e.value)


def test_a_non_2xx_answer_is_a_miss_naming_the_status(tmp_path):
    ft, api = rig(tmp_path, [])
    api.post = lambda url, **kw: type("R", (), {"status_code": 500, "json": lambda s: {}})()
    with pytest.raises(FallbackMiss, match="500") as e:
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert KEY not in str(e.value)


# ---- check() ----

def test_check_needs_the_downloads_dir_and_an_answering_slskd(tmp_path):
    ft, api = rig(tmp_path, [])
    assert sk(tmp_path, ft, api).check() is None
    assert api.calls[0][1] == f"{URL}/api/v0/application" and api.calls[0][3] == {"X-API-Key": KEY}
    api.app_status = 401
    assert "rejected the API key" in sk(tmp_path, ft, api).check()
    api.app_status = 503
    assert "503" in sk(tmp_path, ft, api).check()
    gone = SoulseekFallback(URL, KEY, tmp_path / "nope", http=api, sleep=api.sleep, clock=ft.clock)
    assert "nope" in gone.check()
    api.raise_exc = requests.ConnectionError(KEY)
    problem = sk(tmp_path, ft, api).check()
    assert problem and KEY not in problem


# ---- end to end through the fetch run ----

from tests.fetch_fakes import FakeHttp  # noqa: E402
from tests.test_fetch_run import AAC_URL, aac, aac_tidal, dirs, flac, needs_ffmpeg, rows, run, seed  # noqa: E402,F401


@needs_ffmpeg
def test_a_soulseek_flac_is_placed_copied_and_the_key_is_in_no_event(flac, aac, dirs, tmp_path):
    conn, _a = seed(1)
    g = "Music\\Neil Young\\Harvest\\01 Song 1.flac"
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(g, length=4, size=len(flac)))], files={g: flac},
                  states={g: ["Completed, Succeeded"]})
    fb = sk(tmp_path, ft, api)
    res, out = run(conn, aac_tidal(), FakeHttp({AAC_URL.format(1): aac}), dirs, fallbacks=[fb])
    assert (res["placed"], res["via_fallback"]) == (1, 1) and (dirs[1] / "Neil Young/Harvest/01 - Song 1.flac").is_file()
    assert (tmp_path / "dl" / "Harvest" / "01 Song 1.flac").read_bytes() == flac  # slskd's copy is untouched
    [f] = rows(conn)
    assert f["source"] == "soulseek"
    dump = json.dumps([dict(r) for r in rows(conn, "event_log")]) + "\n".join(out)
    assert KEY not in dump and "bob" in dump


# ---- review fixes ----

QUEEN = replace(ITEM, artist="Queen", title="Love of My Life", duration_s=218)
SONG = replace(ITEM, artist="Queen", title="Song", duration_s=218)


def kept(item, filename, length=218):
    return len(rank_candidates(item, [sl_resp("u", sl_file(filename, length=length))]))


@pytest.mark.parametrize("name", [
    "Music\\Queen\\Live Killers (1979)\\05 Love of My Life.flac",          # variant word in the folder, not the file
    "Music/Queen/Love of My Life (Live at Wembley)/05 Love of My Life.flac",
    "Music\\Queen\\Demos\\05 Love of My Life.flac",
], ids=["live-folder", "live-in-parens-folder", "demos-folder"])
def test_variant_words_count_in_the_folder_too(name):
    assert kept(QUEEN, name) == 0
    assert kept(QUEEN, "Music\\Queen\\A Night at the Opera\\05 Love of My Life.flac") == 1


@pytest.mark.parametrize("word", ["Remix", "Remixed", "Remixes", "Live", "Instrumental", "Instrumentals", "Karaoke",
                                  "Acoustic", "Demo", "Demos", "Edit", "Edited", "Slowed", "Sped"])
def test_variant_stems_are_matched_not_just_exact_words(word):
    assert kept(SONG, f"Music\\Queen\\Album\\01 Song ({word}).flac") == 0
    assert kept(SONG, f"Music\\Queen\\Album {word}\\01 Song.flac") == 0


def test_a_variant_the_planned_title_has_is_exempt_even_as_a_stem():
    remixed = replace(SONG, title="Song (Remixed)")
    assert kept(remixed, "Music\\Queen\\Album\\01 Song (Remixed).flac") == 1
    assert kept(remixed, "Music\\Queen\\Album\\01 Song (Live).flac") == 0


def test_a_band_named_after_a_variant_word_still_matches():
    band = replace(ITEM, artist="Live", title="Lightning Crashes")
    assert kept(band, "Music\\Live\\Throwing Copper\\01 Lightning Crashes.flac", 187) == 1


def test_the_title_must_match_exactly_not_as_a_subset():
    drake = replace(ITEM, artist="Drake", title="Hold On")
    assert kept(drake, "Music\\Drake\\Nothing Was the Same\\01 Hold On We're Going Home.flac", 187) == 0
    for ok in ("01 Hold On.flac", "Hold On.flac", "Drake - Hold On.flac", "1-05 Hold On.flac", "01. Hold On.flac",
               "01 - Drake - Hold On.flac", "Drake - 01 - Hold On.flac"):
        assert kept(drake, "Music\\Drake\\Album\\" + ok, 187) == 1, ok


def test_a_title_that_starts_with_a_number_still_matches_with_or_without_a_track_number():
    item = replace(ITEM, artist="Jay-Z", title="99 Problems")
    assert kept(item, "Music\\Jay-Z\\The Black Album\\99 Problems.flac", 187) == 1
    assert kept(item, "Music\\Jay-Z\\The Black Album\\12 99 Problems.flac", 187) == 1
    assert kept(item, "Music\\Jay-Z\\The Black Album\\12 Problems.flac", 187) == 0


@pytest.mark.parametrize("title, artist", [("!!!", "Neil Young"), ("♥", "Neil Young"), ("Heart of Gold", "!!!"),
                                           ("Heart of Gold", "♥")])
def test_a_title_or_artist_without_words_never_matches_and_is_not_searched(tmp_path, title, artist):
    item = replace(ITEM, title=title, artist=artist)
    assert rank_candidates(item, [sl_resp("u", sl_file("Music\\x\\!!!.flac"), sl_file("Music\\♥\\♥.flac"))]) == []
    ft, api = rig(tmp_path, [sl_resp("u", sl_file(GOOD))])
    with pytest.raises(FallbackMiss, match="no searchable words"):
        sk(tmp_path, ft, api).acquire(item, tmp_path / "wd", make_gate())
    assert api.calls == []


def stale_record(fn, when, state="Completed, Succeeded", tid="old-1"):
    return {"id": tid, "filename": fn, "state": state, "size": 1, "bytesTransferred": 1, "requestedAt": when.isoformat()}


def test_a_stale_succeeded_record_is_ignored_when_the_enqueue_names_our_transfer_id(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"},
                  states={GOOD: ["Completed, Errored"]}, stale={})
    api.stale = {GOOD: [stale_record(GOOD, ft.now())]}  # ancient success with another id
    with pytest.raises(FallbackMiss, match="Completed, Errored"):
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert not (tmp_path / "dl" / "Harvest").exists()  # nothing was waited for on the old record


def test_without_ids_a_record_that_was_not_there_before_the_post_is_ours(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"}, enqueue_ids=False,
                  states={GOOD: ["InProgress", "Completed, Succeeded"]})
    api.stale = {GOOD: [stale_record(GOOD, ft.now())]}  # an earlier success for the same file, id old-1
    got = sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert got.path.read_bytes() == b"audio" and api.polls[GOOD] == 2  # waited for our record, not the old success


def test_the_before_listing_is_taken_before_the_post_and_a_404_there_is_fine(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"}, enqueue_ids=False,
                  states={GOOD: ["Completed, Succeeded"]})
    sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    kinds = [(c[0], c[1].rsplit("/", 1)[1]) for c in api.calls if "/transfers/" in c[1]]
    assert kinds[:2] == [("get", "bob"), ("post", "bob")]


def test_a_slskd_clock_that_runs_behind_does_not_hide_our_transfer(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"}, enqueue_ids=False,
                  states={GOOD: ["Completed, Succeeded"]}, clock_skew=3600)  # requestedAt is an hour before our POST
    api.stale = {GOOD: [stale_record(GOOD, ft.now(), state="Completed, Errored")]}
    assert sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate()).path.read_bytes() == b"audio"


def test_without_ids_an_old_record_alone_is_never_ours(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], enqueue_ids=False, states={GOOD: ["Queued"]})
    api.stale = {GOOD: [stale_record(GOOD, ft.now())]}
    real = api._transfers

    def only_old(user):  # our own record never shows up; only the old success does
        out = real(user)
        out["directories"][0]["files"] = [r for r in out["directories"][0]["files"] if r["id"] == "old-1"]
        return out
    api._transfers = only_old
    with pytest.raises(FallbackMiss, match="timed out"):
        sk(tmp_path, ft, api, timeout=10).acquire(ITEM, tmp_path / "wd", make_gate())
    assert not [c for c in api.calls if c[0] == "delete"]


def test_on_timeout_a_transfer_that_was_never_matched_is_found_by_its_new_id_and_cancelled(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], enqueue_ids="bogus", states={GOOD: ["InProgress"]})
    with pytest.raises(FallbackMiss, match="timed out"):
        sk(tmp_path, ft, api, timeout=10).acquire(ITEM, tmp_path / "wd", make_gate())
    assert [c[1] for c in api.calls if c[0] == "delete"] == [f"{URL}/api/v0/transfers/downloads/bob/id-{GOOD}"]


def test_every_request_forbids_redirects_and_a_3xx_is_a_miss_naming_the_status(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"},
                  states={GOOD: ["Completed, Succeeded"]})
    sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert api.calls and all(c[5] is False for c in api.calls)
    ft, api = rig(tmp_path, [], redirect=("/searches",))
    with pytest.raises(FallbackMiss, match="302"):
        sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    ft, api = rig(tmp_path, [])
    api.app_status = 301
    assert "301" in sk(tmp_path, ft, api).check() and api.calls[-1][5] is False


def test_a_candidate_whose_file_never_shows_up_moves_on_to_the_next(tmp_path):
    g2 = "Music\\Neil Young\\Harvest\\02 Heart of Gold.flac"
    ft, api = rig(tmp_path, [sl_resp("a", sl_file(GOOD, size=5), queue=0), sl_resp("b", sl_file(g2, size=5), queue=1)],
                  files={g2: b"audio"}, states={GOOD: ["Completed, Succeeded"], g2: ["Completed, Succeeded"]})
    got = sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert got.ref == f"b:{g2}"


def test_an_ambiguous_file_moves_on_to_the_next_candidate(tmp_path):
    g2 = "Music\\Neil Young\\Other\\02 Heart of Gold.flac"
    ft, api = rig(tmp_path, [sl_resp("a", sl_file(GOOD, size=5), queue=0), sl_resp("b", sl_file(g2, size=5), queue=1)],
                  files={GOOD: b"audio", g2: b"audio"}, states={GOOD: ["Completed, Succeeded"], g2: ["Completed, Succeeded"]})
    twin = tmp_path / "dl" / "twin" / "01 Heart of Gold.flac"  # a's basename now exists twice with the same size
    twin.parent.mkdir(parents=True)
    twin.write_bytes(b"audio")
    assert sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate()).ref == f"b:{g2}"


def test_a_malformed_record_moves_on_to_the_next_candidate(tmp_path):
    g2 = "Music\\Neil Young\\Harvest\\02 Heart of Gold.flac"
    broken = sl_file(GOOD)
    del broken["size"]
    ft, api = rig(tmp_path, [sl_resp("a", broken, queue=0), sl_resp("b", sl_file(g2, size=5), queue=1)],
                  files={g2: b"audio"}, states={g2: ["Completed, Succeeded"]})
    assert sk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate()).ref == f"b:{g2}"


# ---- the VPN gate ----

GURL, GKEY = "http://gluetun.local:8010", "GLUETUN-KEY-456"


def vsk(tmp_path, ft, api, **kw):
    return SoulseekFallback(URL, KEY, tmp_path / "dl", gluetun_url=GURL, gluetun_key=GKEY, http=api, sleep=api.sleep,
                            clock=ft.clock, **kw)


def vpn_calls(api):
    return [c for c in api.calls if "/v1/" in c[1]]


def test_check_passes_when_the_vpn_is_running_and_reports_the_public_ip(tmp_path):
    ft, api = rig(tmp_path, [])
    fb = vsk(tmp_path, ft, api)
    assert fb.check() is None and fb.public_ip == "203.0.113.7"
    assert [c[1] for c in vpn_calls(api)] == [f"{GURL}/v1/vpn/status", f"{GURL}/v1/publicip/ip"]
    assert all(c[3] == {"X-API-Key": GKEY} and c[4] == (10, 30) and c[5] is False for c in vpn_calls(api))
    assert all(c[3] == {"X-API-Key": KEY} for c in api.calls if "/v1/" not in c[1])  # keys are never swapped


@pytest.mark.parametrize("kw, text", [({"vpn": ["stopped"]}, "not running"), ({"vpn_code": 302}, "302"),
                                      ({"vpn_code": 500}, "500"),
                                      ({"raise_exc": requests.ConnectionError(f"x {GKEY} {KEY}")}, "not reachable")])
def test_a_vpn_that_is_down_at_preflight_disables_soulseek_instead_of_failing_setup(tmp_path, kw, text):
    ft, api = rig(tmp_path, [], **kw)
    fb = vsk(tmp_path, ft, api)
    assert fb.check() is None and text in fb.disabled and GKEY not in fb.disabled and KEY not in fb.disabled
    if "vpn" in kw:
        assert "stopped" in fb.disabled
    with pytest.raises(VpnDown):
        fb.acquire(ITEM, tmp_path / "wd", make_gate())


def test_a_rejected_gluetun_key_is_a_setup_problem(tmp_path):
    ft, api = rig(tmp_path, [], vpn_code=401)
    fb = vsk(tmp_path, ft, api)
    problem = fb.check()
    assert "rejected the API key" in problem and GKEY not in problem and fb.disabled is None


def test_other_check_problems_still_fail_setup_with_the_vpn_up(tmp_path):
    ft, api = rig(tmp_path, [], app_status=401)
    assert "slskd rejected the API key" in vsk(tmp_path, ft, api).check()
    gone = SoulseekFallback(URL, KEY, tmp_path / "nope", http=api, sleep=api.sleep, clock=ft.clock,
                            gluetun_url=GURL, gluetun_key=GKEY)
    assert "nope" in gone.check()


def test_the_opt_out_never_contacts_gluetun(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"},
                  states={GOOD: ["Completed, Succeeded"]}, vpn=["stopped"])
    fb = sk(tmp_path, ft, api)
    assert fb.check() is None
    fb.acquire(ITEM, tmp_path / "wd", make_gate())
    assert vpn_calls(api) == [] and fb.public_ip is None and getattr(fb, "disabled", None) is None


def test_the_status_is_rechecked_before_every_acquire_and_a_drop_stops_soulseek_for_good(tmp_path):
    ft, api = rig(tmp_path, [sl_resp("bob", sl_file(GOOD, size=5))], files={GOOD: b"audio"},
                  states={GOOD: ["Completed, Succeeded"]}, vpn=["running", "running", "stopped"])
    fb = vsk(tmp_path, ft, api)
    assert fb.check() is None
    fb.acquire(ITEM, tmp_path / "wd", make_gate())            # status call 2: running
    seen = len(api.calls)
    with pytest.raises(VpnDown) as e:
        fb.acquire(ITEM, tmp_path / "wd", make_gate())        # status call 3: stopped
    assert "stopped" in str(e.value) and GKEY not in str(e.value)
    assert [c[1] for c in api.calls[seen:]] == [f"{GURL}/v1/vpn/status"]  # no search, no download
    seen = len(api.calls)
    with pytest.raises(VpnDown):
        fb.acquire(ITEM, tmp_path / "wd", make_gate())        # stays down without asking again
    assert len(api.calls) == seen


def test_an_unreachable_gluetun_at_acquire_time_is_a_vpn_down_without_the_key(tmp_path):
    ft, api = rig(tmp_path, [], raise_exc=requests.ConnectionError(f"x {GKEY}"))
    with pytest.raises(VpnDown) as e:
        vsk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert GKEY not in str(e.value) and "ConnectionError" in str(e.value)


def lossy3():
    from tests.fetch_fakes import FakeTidal, bts_body
    return FakeTidal({i: bts_body(url=AAC_URL.format(i), codec="mp4a.40.2", audioQuality="HIGH", bitDepth=None)
                      for i in (1, 2, 3)})


@needs_ffmpeg
def test_a_vpn_drop_mid_run_stops_soulseek_but_tidal_and_the_other_fallbacks_carry_on(flac, aac, dirs, tmp_path):
    from tests.fetch_fakes import FakeFallback
    from cadence.fetch.fallback import Acquired
    conn, _a = seed(3)
    names = {i: f"Music\\Neil Young\\Harvest\\0{i} Song {i}.flac" for i in (1, 2, 3)}
    ft, api = rig(tmp_path, [sl_resp("bob", *(sl_file(n, length=4, size=len(flac)) for n in names.values()))],
                  files={names[1]: flac}, states={n: ["Completed, Succeeded"] for n in names.values()},
                  vpn=["running", "running", "stopped"])  # check, track 1, then the tunnel drops
    soul = vsk(tmp_path, ft, api)

    def second(item, workdir):
        p = workdir / "o.flac"
        p.write_bytes(flac)
        return Acquired(p, "orpheus: ok", {})
    orph = FakeFallback("orpheus", second, owns_workdir=True)
    files = {AAC_URL.format(i): aac for i in (1, 2, 3)}
    res, out = run(conn, lossy3(), FakeHttp(files), dirs, fallbacks=[soul, orph])
    assert (res["placed"], res["via_fallback"], res["held_lossy"], res["vpn_down"]) == (3, 3, 0, 1)
    assert [r["source"] for r in rows(conn)] == ["soulseek", "orpheus", "orpheus"]
    down = [json.loads(e["data_json"]) for e in rows(conn, "event_log") if e["kind"] == "fetch.vpn_down"]
    assert len(down) == 1 and down[0]["source"] == "soulseek"
    assert [c[1] for c in vpn_calls(api)].count(f"{GURL}/v1/vpn/status") == 3  # not asked again after the drop
    text = "\n".join(out)
    assert "1 vpn down" in text and "203.0.113.7" in text
    dump = json.dumps([dict(r) for r in rows(conn, "event_log")]) + text
    assert GKEY not in dump and KEY not in dump


def test_the_vpn_is_rechecked_between_candidates(tmp_path):
    g2 = "Music\\Neil Young\\Harvest\\02 Heart of Gold.flac"
    ft, api = rig(tmp_path, [sl_resp("a", sl_file(GOOD, size=5), queue=0), sl_resp("b", sl_file(g2, size=5), queue=1)],
                  files={g2: b"audio"}, states={GOOD: ["Completed, Errored"], g2: ["Completed, Succeeded"]},
                  vpn=["running", "stopped"])  # up when acquire starts, down once the first candidate has failed
    with pytest.raises(VpnDown):
        vsk(tmp_path, ft, api).acquire(ITEM, tmp_path / "wd", make_gate())
    assert not posts(api, "/transfers/downloads/b") and len(posts(api, "/transfers/downloads/a")) == 1


# ---- wrong-track bypasses ----

def test_every_variant_word_normalize_knows_and_the_added_ones_reject_a_file():
    from cadence.identity.normalize import VARIANT_WORDS
    for w in (*VARIANT_WORDS, "tribute", "cover", "covers", "unplugged", "session", "sessions", "concert", "karaoke"):
        assert kept(SONG, f"Music\\Queen\\Album ({w})\\01 Song.flac") == 0, w
        assert kept(SONG, f"Music\\Queen\\Album\\01 Song ({w}).flac") == 0, w


@pytest.mark.parametrize("name", [
    "A Tribute to Queen\\05 Love of My Life.flac",
    "Music\\Various\\A Tribute to Queen\\05 Love of My Life.flac",
    "Music\\Queen Latifah\\Black Reign\\05 Love of My Life.flac",
    "Queen Latifah - Black Reign\\05 Love of My Life.flac",
    "Music\\Queens of the Stone Age\\Songs\\05 Love of My Life.flac",
    "Music\\Queen Mary\\Album\\05 Love of My Life.flac",
    "Music\\Other\\Queen Songs\\05 Love of My Life.flac",
], ids=["tribute", "tribute-nested", "latifah", "latifah-prefix", "stone-age", "mary", "queen-songs"])
def test_the_artist_must_be_a_whole_folder_name_or_the_file_prefix(name):
    assert kept(QUEEN, name) == 0


@pytest.mark.parametrize("name", [
    "Music\\Queen\\A Night at the Opera\\09 Love of My Life.flac",
    "Queen\\09 Love of My Life.flac",
    "Queen - A Night at the Opera (1975)\\09 Love of My Life.flac",
    "Music\\Queen (1973-1980)\\09 Love of My Life.flac",
    "Music\\Various\\Hits\\Queen - Love of My Life.flac",
    "Music\\Various\\Hits\\09 - Queen - Love of My Life.flac",
])
def test_the_artist_forms_that_are_accepted(name):
    assert kept(QUEEN, name) == 1


def test_scattered_artist_words_do_not_count():
    ny = replace(ITEM)
    assert kept(ny, "Music\\Neil Sedaka\\Young Love\\01 Heart of Gold.flac", 187) == 0
    assert kept(ny, "Music\\Neil\\Young\\01 Heart of Gold.flac", 187) == 0
    assert kept(ny, "Music\\The Neil Young Tribute\\01 Heart of Gold.flac", 187) == 0
    assert kept(replace(ITEM, artist="The Beatles", title="Help!"), "Music\\The Beatles\\Help\\01 Help.flac", 187) == 1
