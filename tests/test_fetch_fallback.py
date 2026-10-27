"""Fallback sources in the fetch run: tried only where a lossless request would otherwise be held lossy."""
import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import mutagen.flac
import pytest

from cadence import cli
from cadence.fetch.fallback import (Acquired, FallbackMiss, OrpheusFallback, SoulseekFallback, fallbacks_from_env)
from cadence.fetch.plan import plan_missing
from cadence.gate.bucket import GatePaused
from tests.fetch_fakes import FakeFallback, FakeHttp, add_artist, add_recording, add_release, add_track, ledger, make_flac
from tests.test_fetch_run import (AAC_URL, ISRC, STAGED1, aac, aac_tidal, cli_env, dirs, flac, needs_ffmpeg, rows,  # noqa: F401
                                  run, seed, tidal)

REL = "Neil Young/Harvest/01 - Song 1.flac"


def events(conn, kind=None):
    return [(e["kind"], json.loads(e["data_json"] or "null")) for e in rows(conn, "event_log")
            if kind in (None, e["kind"])]


def gives(data: bytes, name="x.flac", ref="fake: ref"):
    def make(item, workdir):
        p = workdir / name
        p.write_bytes(data)
        return Acquired(p, ref, {})
    return make


def lossy_run(conn, aac_bytes, dirs, fbs, **kw):
    return run(conn, aac_tidal(), FakeHttp({AAC_URL.format(1): aac_bytes}), dirs, fallbacks=fbs, **kw)


@needs_ffmpeg
def test_an_orpheus_flac_replaces_the_lossy_hold(flac, aac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    fb = FakeFallback("orpheus", gives(flac), owns_workdir=True)
    res, out = lossy_run(conn, aac, dirs, [fb])
    dest = lib / REL
    assert (res["placed"], res["held_lossy"], res["failed"], res["via_fallback"]) == (1, 0, 0, 1)
    assert dest.is_file() and not any(p.is_file() for p in stg.rglob("*"))  # held .m4a and staged copy are gone
    assert not list((stg / ".fallback").glob("*"))  # Cadence's own workdir is removed
    [f] = rows(conn)
    assert (f["path"], f["source"], f["codec"], f["lossy"], f["quality_requested"]) == (REL, "orpheus", "flac", 0, "LOSSLESS")
    tags = mutagen.File(dest)
    assert tags["albumartist"] == ["Neil Young"] and tags["isrc"] == [f"{ISRC[:-1]}1"]
    [(_, data)] = events(conn, "fetch.placed")
    assert data["source"] == "orpheus" and data["fallback_ref"] == "fake: ref" and data["planned_tidal_track_id"] == 1
    assert [k for k, _ in events(conn)].count("fetch.stale_held_removed") == 1
    assert "via orpheus (fake: ref)" in "\n".join(out) and "1 via fallback" in "\n".join(out)


@needs_ffmpeg
def test_wrong_duration_misses_then_soulseek_places_and_leaves_its_file(flac, aac, dirs, tmp_path):
    stg, lib = dirs
    long_ = make_flac(tmp_path / "long.flac", 10)
    dl = tmp_path / "slskd"
    dl.mkdir()
    (dl / "ok.flac").write_bytes(flac)
    (dl / "long.flac").write_bytes(long_)
    orph = FakeFallback("orpheus", lambda i, w: Acquired(dl / "long.flac", "x", {}))
    soul = FakeFallback("soulseek", lambda i, w: Acquired(dl / "ok.flac", "bob:Music\\ok.flac", {"username": "bob"}))
    for verify in (True, False):  # verification is mandatory for fallbacks even with verify=False
        conn, a = seed(1)
        shutil.rmtree(stg, ignore_errors=True)
        shutil.rmtree(lib / "Neil Young", ignore_errors=True)
        res, _ = lossy_run(conn, aac, dirs, [orph, soul], verify=verify)
        assert (res["placed"], res["via_fallback"]) == (1, 1)
        [f] = rows(conn)
        assert f["source"] == "soulseek"
        [(_, miss)] = events(conn, "fetch.fallback_miss")
        assert miss["source"] == "orpheus" and "duration" in miss["reason"]
        assert (dl / "ok.flac").read_bytes() == flac and (dl / "long.flac").read_bytes() == long_
        assert not list(stg.rglob("*.part"))
        assert (events(conn, "fetch.placed")[0][1]["fallback_ref"]) == "bob:Music\\ok.flac"


@needs_ffmpeg
def test_all_misses_hold_lossy_and_name_the_fallbacks_tried(aac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    fbs = [FakeFallback("orpheus", FallbackMiss("nothing there")), FakeFallback("soulseek", RuntimeError("boom"))]
    res, out = lossy_run(conn, aac, dirs, fbs)
    assert (res["held_lossy"], res["placed"], res["failed"], res["via_fallback"]) == (1, 0, 0, 0)
    assert (stg / f"{STAGED1}.m4a").is_file() and rows(conn) == [] and list(lib.iterdir()) == []
    misses = events(conn, "fetch.fallback_miss")
    assert [(d["source"], d["reason"]) for _, d in misses] == [("orpheus", "nothing there"), ("soulseek", "RuntimeError: boom")]
    [(_, held)] = events(conn, "fetch.held_lossy")
    assert held["fallbacks_tried"] == ["orpheus", "soulseek"]
    assert "0 via fallback" in "\n".join(out)


@needs_ffmpeg
def test_without_a_known_duration_no_fallback_is_called(aac, dirs):
    conn = ledger()
    a = add_artist(conn)
    add_track(conn, add_release(conn, a, "Harvest", 100), add_recording(conn, a, "Song 1", isrc=ISRC, dur=None), 1,
              "Song 1", 1, dur=None)
    conn.commit()
    fb = FakeFallback("orpheus", FallbackMiss("never"))
    res, _ = lossy_run(conn, aac, dirs, [fb])
    assert fb.calls == [] and res["held_lossy"] == 1
    [(_, d)] = events(conn, "fetch.fallback_skipped")
    assert d["reason"] and events(conn, "fetch.fallback_miss") == []
    assert "fallbacks_tried" not in events(conn, "fetch.held_lossy")[0][1]


def tagged(flac_bytes, tmp_path, isrc):
    p = tmp_path / "tagged.flac"
    p.write_bytes(flac_bytes)
    t = mutagen.flac.FLAC(p)
    t["ISRC"] = [isrc]
    t.save()
    return p.read_bytes()


@needs_ffmpeg
def test_isrc_mismatch_is_a_miss_and_a_case_variant_is_not(flac, aac, dirs, tmp_path):
    conn, a = seed(1)
    fb = FakeFallback("orpheus", gives(tagged(flac, tmp_path, "GBXXX0000001")))
    res, _ = lossy_run(conn, aac, dirs, [fb])
    assert res["held_lossy"] == 1 and "ISRC mismatch" in events(conn, "fetch.fallback_miss")[0][1]["reason"]
    conn, a = seed(1)
    shutil.rmtree(dirs[0], ignore_errors=True)
    fb = FakeFallback("orpheus", gives(tagged(flac, tmp_path, f" {ISRC[:-1].lower()}1 ")))
    res, _ = lossy_run(conn, aac, dirs, [fb])
    assert res["placed"] == 1


@needs_ffmpeg
@pytest.mark.parametrize("name", ["x.m4a", "x.flac"], ids=["m4a", "aac-bytes-named-flac"])
def test_non_flac_output_is_a_miss(flac, aac, dirs, name):
    conn, a = seed(1)
    res, _ = lossy_run(conn, aac, dirs, [FakeFallback("orpheus", gives(aac, name))])
    assert (res["held_lossy"], res["placed"]) == (1, 0)
    assert "not FLAC" in events(conn, "fetch.fallback_miss")[0][1]["reason"]


@needs_ffmpeg
def test_an_already_held_track_tries_fallbacks_without_asking_tidal_again(flac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    held = stg / f"{STAGED1}.m4a"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"held")
    session, http = tidal(1), FakeHttp()
    res, _ = run(conn, session, http, dirs, fallbacks=[FakeFallback("orpheus", gives(flac))])
    assert (res["placed"], res["held_lossy"]) == (1, 0) and session.calls == [] and http.urls == []
    assert not held.exists() and (lib / REL).is_file()
    held.parent.joinpath(held.name).write_bytes(b"held")  # and when all miss the hold stays as it was
    conn, a = seed(1)
    shutil.rmtree(lib / "Neil Young")
    res, _ = run(conn, session, http, dirs, fallbacks=[FakeFallback("orpheus", FallbackMiss("no"))])
    assert res["held_lossy"] == 1 and held.read_bytes() == b"held" and session.calls == []


@needs_ffmpeg
def test_high_never_calls_a_fallback(aac, dirs):
    conn, a = seed(1)
    fb = FakeFallback("orpheus", FallbackMiss("never"))
    res, _ = lossy_run(conn, aac, dirs, [fb], quality="HIGH")
    assert res["placed"] == 1 and fb.calls == [] and fb.checked == 0 and "via_fallback" in res


def test_a_failed_preflight_check_is_a_setup_stop(dirs):
    conn, a = seed(1)
    ok, bad = FakeFallback("orpheus"), FakeFallback("soulseek", problem="slskd rejected the API key")
    session = tidal(1)
    res, out = run(conn, session, FakeHttp(), dirs, fallbacks=[ok, bad])
    assert res["stopped"] == "setup" and len(res["remaining"]) == 1 and session.calls == []
    assert (ok.checked, bad.checked) == (1, 1) and "slskd rejected the API key" in "\n".join(out)
    assert "soulseek" in "\n".join(out)


@needs_ffmpeg
def test_a_taken_target_found_before_acquiring_is_a_conflict(aac, dirs):
    conn, a = seed(1)
    stg, lib = dirs
    item = replace(plan_missing(conn, scope="all").items[0], target_rel="Neil Young/Harvest/01 - Song 1.m4a")
    (lib / "Neil Young/Harvest").mkdir(parents=True)
    (lib / REL).write_bytes(b"mine")
    fb = FakeFallback("orpheus", FallbackMiss("never"))
    res, _ = lossy_run(conn, aac, dirs, [fb], items=[item])
    assert res["conflict"] == 1 and fb.calls == [] and (lib / REL).read_bytes() == b"mine" and rows(conn) == []


@needs_ffmpeg
def test_a_target_taken_while_acquiring_is_a_conflict_and_the_copy_is_kept(flac, aac, dirs):
    conn, a = seed(1)
    stg, lib = dirs

    def racing(item, workdir):
        (lib / "Neil Young/Harvest").mkdir(parents=True)
        (lib / REL).write_bytes(b"mine")
        return gives(flac)(item, workdir)
    res, _ = lossy_run(conn, aac, dirs, [FakeFallback("orpheus", racing)])
    assert (res["conflict"], res["placed"]) == (1, 0) and (lib / REL).read_bytes() == b"mine" and rows(conn) == []
    [(_, d)] = events(conn, "fetch.conflict")
    assert d["staged"].endswith("01 - Song 1 [orpheus].flac") and (stg / "Neil Young/Harvest/01 - Song 1 [orpheus].flac").is_file()


@needs_ffmpeg
def test_a_paused_gate_inside_a_fallback_stops_the_run(aac, dirs):
    conn, a = seed(1)
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    res, _ = lossy_run(conn, aac, dirs, [FakeFallback("orpheus", GatePaused(until, "429"))])
    assert res["stopped"] == "paused" and res["remaining"] and events(conn, "fetch.fallback_miss") == []


@needs_ffmpeg
def test_no_fallbacks_means_nothing_new_is_logged_or_counted(aac, dirs):
    conn, a = seed(1)
    res, out = run(conn, aac_tidal(), FakeHttp({AAC_URL.format(1): aac}), dirs)
    assert "via_fallback" not in res and "via fallback" not in "\n".join(out)
    assert [k for k, _ in events(conn)] == ["fetch.held_lossy"] and "fallbacks_tried" not in events(conn)[0][1]


# ---- configuration ----

FULL = {"CADENCE_ORPHEUS_DIR": "/opt/orpheus", "CADENCE_ORPHEUS_MODULE": "qobuz", "CADENCE_ORPHEUS_TIMEOUT": "60",
        "CADENCE_SLSKD_URL": "http://10.0.0.48:5030", "CADENCE_SLSKD_API_KEY": "KEYVALUE", "CADENCE_SLSKD_DOWNLOADS": "/dl",
        "CADENCE_SLSKD_TIMEOUT": "70", "CADENCE_SLSKD_MAX_CANDIDATES": "2",
        "CADENCE_GLUETUN_URL": "http://10.0.0.48:8010/", "CADENCE_GLUETUN_API_KEY": "GLUEKEY"}


def test_fallbacks_from_env_builds_in_the_order_given():
    fbs, err = fallbacks_from_env(["soulseek", "orpheus"], FULL)
    assert err is None and [type(f) for f in fbs] == [SoulseekFallback, OrpheusFallback]
    assert [f.name for f in fbs] == ["soulseek", "orpheus"]
    assert (fbs[0].url, fbs[0].timeout, fbs[0].max_candidates) == ("http://10.0.0.48:5030", 70, 2)
    assert (fbs[1].module, fbs[1].python, fbs[1].timeout) == ("qobuz", "python3", 60)
    assert fallbacks_from_env([], {}) == ([], None)


@pytest.mark.parametrize("missing", ["CADENCE_ORPHEUS_DIR", "CADENCE_ORPHEUS_MODULE", "CADENCE_SLSKD_URL",
                                     "CADENCE_SLSKD_API_KEY", "CADENCE_SLSKD_DOWNLOADS", "CADENCE_GLUETUN_URL",
                                     "CADENCE_GLUETUN_API_KEY"])
def test_a_missing_variable_is_named_in_the_error(missing):
    env = {k: v for k, v in FULL.items() if k != missing}
    fbs, err = fallbacks_from_env(["orpheus", "soulseek"], env)
    assert fbs == [] and missing in err and "KEYVALUE" not in err and "GLUEKEY" not in err


def test_soulseek_gets_the_gluetun_settings_and_the_vpn_opt_out_is_explicit():
    [soul], err = fallbacks_from_env(["soulseek"], FULL)
    assert err is None and soul.gluetun_url == "http://10.0.0.48:8010"
    gone = {k: v for k, v in FULL.items() if not k.startswith("CADENCE_GLUETUN")}
    assert "CADENCE_GLUETUN_URL" in fallbacks_from_env(["soulseek"], gone)[1]
    [soul], err = fallbacks_from_env(["soulseek"], {**gone, "CADENCE_SLSKD_VPN": "off"})
    assert err is None and soul.gluetun_url is None
    for bad in ("on", "OFF", "false", "", "0"):
        fbs, err = fallbacks_from_env(["soulseek"], {**FULL, "CADENCE_SLSKD_VPN": bad})
        assert fbs == [] and "CADENCE_SLSKD_VPN" in err and "off" in err
    assert fallbacks_from_env(["orpheus"], {k: v for k, v in FULL.items() if "GLUETUN" not in k})[1] is None  # not needed


@pytest.mark.parametrize("slskd, gluetun", [("http://10.0.0.48:5030", "http://10.0.0.49:8010"),
                                            ("http://unraid.lan:5030", "http://10.0.0.48:8010"),
                                            ("http://slskd.lan", "http://gluetun.lan")])
def test_slskd_and_gluetun_must_be_on_the_same_host(slskd, gluetun):
    env = {**FULL, "CADENCE_SLSKD_URL": slskd, "CADENCE_GLUETUN_URL": gluetun}
    fbs, err = fallbacks_from_env(["soulseek"], env)
    assert fbs == [] and "CADENCE_SLSKD_URL" in err and "CADENCE_GLUETUN_URL" in err
    assert slskd not in err and gluetun not in err and "10.0.0" not in err
    fbs, err = fallbacks_from_env(["soulseek"], {**env, "CADENCE_SLSKD_VPN": "off"})  # the opt-out skips the pairing
    assert err is None and len(fbs) == 1


def test_the_host_comparison_ignores_port_scheme_case_and_path():
    env = {**FULL, "CADENCE_SLSKD_URL": "http://Unraid.LAN:5030/", "CADENCE_GLUETUN_URL": "https://unraid.lan:8010/x"}
    assert fallbacks_from_env(["soulseek"], env)[1] is None
    bad = {**FULL, "CADENCE_SLSKD_URL": "not a url"}
    assert "CADENCE_SLSKD_URL" in fallbacks_from_env(["soulseek"], bad)[1]


def test_unknown_names_and_bad_numbers_are_errors():
    assert "tidalfake" in fallbacks_from_env(["tidalfake"], FULL)[1]
    err = fallbacks_from_env(["soulseek"], {**FULL, "CADENCE_SLSKD_TIMEOUT": "soon"})[1]
    assert "CADENCE_SLSKD_TIMEOUT" in err and "soon" not in err


# ---- the CLI ----

@pytest.fixture
def fb_env(cli_env, monkeypatch, tmp_path):
    for k, v in {**FULL, "CADENCE_SLSKD_DOWNLOADS": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CADENCE_FALLBACKS", raising=False)
    return cli_env


def test_cli_dry_run_names_the_fallbacks_in_the_order_given(fb_env, capsys):
    assert cli.main(["fetch", "--scope", "all", "--fallback", "soulseek", "--fallback", "orpheus"]) == 0
    assert "fallbacks: soulseek, orpheus (only for tracks Tidal serves lossy; not contacted in a dry run)" \
        in capsys.readouterr().out


def test_cli_reads_cadence_fallbacks_and_the_flag_wins(fb_env, monkeypatch, capsys):
    monkeypatch.setenv("CADENCE_FALLBACKS", "orpheus, soulseek")
    assert cli.main(["fetch", "--scope", "all"]) == 0
    assert "fallbacks: orpheus, soulseek (" in capsys.readouterr().out
    assert cli.main(["fetch", "--scope", "all", "--fallback", "soulseek"]) == 0
    assert "fallbacks: soulseek (" in capsys.readouterr().out


def test_cli_without_fallbacks_says_none(fb_env, capsys):
    assert cli.main(["fetch", "--scope", "all"]) == 0
    assert "fallbacks: none" in capsys.readouterr().out


def test_cli_bad_config_is_exit_2_even_in_a_dry_run(fb_env, monkeypatch, capsys):
    monkeypatch.delenv("CADENCE_SLSKD_API_KEY")
    assert cli.main(["fetch", "--scope", "all", "--fallback", "soulseek"]) == 2
    err = capsys.readouterr().err
    assert "CADENCE_SLSKD_API_KEY" in err
    monkeypatch.setenv("CADENCE_FALLBACKS", "napster")
    assert cli.main(["fetch", "--scope", "all"]) == 2 and "napster" in capsys.readouterr().err


@pytest.mark.parametrize("var, value", [("CADENCE_GLUETUN_URL", None), ("CADENCE_GLUETUN_API_KEY", None),
                                        ("CADENCE_SLSKD_VPN", "maybe")])
def test_cli_vpn_config_mistakes_are_exit_2(fb_env, monkeypatch, capsys, var, value):
    if value is None:
        monkeypatch.delenv(var)
    else:
        monkeypatch.setenv(var, value)
    assert cli.main(["fetch", "--scope", "all", "--fallback", "soulseek"]) == 2
    err = capsys.readouterr().err
    assert var in err and "GLUEKEY" not in err and "KEYVALUE" not in err


def test_cli_vpn_opt_out_needs_no_gluetun_settings(fb_env, monkeypatch, capsys):
    for k in ("CADENCE_GLUETUN_URL", "CADENCE_GLUETUN_API_KEY"):
        monkeypatch.delenv(k)
    monkeypatch.setenv("CADENCE_SLSKD_VPN", "off")
    assert cli.main(["fetch", "--scope", "all", "--fallback", "soulseek"]) == 0
    assert "fallbacks: soulseek (" in capsys.readouterr().out


def test_cli_high_says_fallbacks_are_unused(fb_env, capsys):
    assert cli.main(["fetch", "--scope", "all", "--quality", "HIGH", "--fallback", "orpheus"]) == 0
    assert "unused at HIGH" in capsys.readouterr().out


def test_cli_execute_hands_the_fallbacks_to_the_run(fb_env, monkeypatch):
    from tests.fetch_fakes import FakeTidal
    seen = {}

    def spy(*a, **kw):
        seen.update(kw)
        return {"stopped": None, "failed": 0, "conflict": 0}
    monkeypatch.setattr(cli, "fetch_items", spy)
    monkeypatch.setattr(cli, "_login", lambda gate, token: FakeTidal())
    assert cli.main(["fetch", "--execute", "--scope", "all", "--fallback", "soulseek", "--fallback", "orpheus"]) == 0
    assert [f.name for f in seen["fallbacks"]] == ["soulseek", "orpheus"]


# ---- review fixes ----

@needs_ffmpeg
@pytest.mark.parametrize("what", ["tag", "facts", "lossless"])
def test_a_failure_after_staging_removes_the_staged_copy_and_nothing_else(flac, aac, dirs, tmp_path, monkeypatch, what):
    from cadence.fetch import run as fetch_run
    conn, a = seed(1)
    stg, lib = dirs
    src = tmp_path / "slskd.flac"
    src.write_bytes(flac)
    real_tag, real_facts = fetch_run._tag, fetch_run._file_facts

    def tag(path, item):  # only the fallback copy fails; the Tidal download is tagged as usual
        if "[soulseek]" in str(path) and what == "tag":
            raise OSError("disk full")
        return real_tag(path, item)

    def facts(path, stream):
        if "[soulseek]" in str(path) and what == "facts":
            raise ValueError("odd")
        out = real_facts(path, stream)
        return {**out, "codec": "aac"} if "[soulseek]" in str(path) and what == "lossless" else out
    monkeypatch.setattr(fetch_run, "_tag", tag)
    monkeypatch.setattr(fetch_run, "_file_facts", facts)
    fb = FakeFallback("soulseek", lambda i, w: Acquired(src, "bob:x", {}))
    res, _ = lossy_run(conn, aac, dirs, [fb])
    assert (res["held_lossy"], res["placed"], res["failed"]) == (1, 0, 0)
    assert not (stg / "Neil Young/Harvest/01 - Song 1 [soulseek].flac").exists() and not list(stg.rglob("*.part"))
    assert src.read_bytes() == flac and (stg / f"{STAGED1}.m4a").is_file() and list(lib.iterdir()) == []
    assert len(events(conn, "fetch.fallback_miss")) == 1


@needs_ffmpeg
@pytest.mark.parametrize("tag", ["US-RE1-72-00001", "us re1 72 00001", " usre17200001 "])
def test_isrc_compare_ignores_hyphens_spaces_and_case(flac, aac, dirs, tmp_path, tag):
    conn, a = seed(1)
    res, _ = lossy_run(conn, aac, dirs, [FakeFallback("orpheus", gives(tagged(flac, tmp_path, tag)))])
    assert res["placed"] == 1


@needs_ffmpeg
def test_vpn_down_from_a_fallback_is_logged_once_and_that_fallback_is_skipped_afterwards(aac, dirs):
    from cadence.fetch.fallback import VpnDown
    conn, a = seed(2)
    from tests.fetch_fakes import FakeTidal, bts_body
    session = FakeTidal({i: bts_body(url=AAC_URL.format(i), codec="mp4a.40.2", audioQuality="HIGH", bitDepth=None)
                         for i in (1, 2)})
    soul = FakeFallback("soulseek", VpnDown("VPN status is 'stopped'"))
    other = FakeFallback("orpheus", FallbackMiss("no"))
    res, out = run(conn, session, FakeHttp({AAC_URL.format(i): aac for i in (1, 2)}), dirs, fallbacks=[soul, other])
    assert res["held_lossy"] == 2 and res["vpn_down"] == 1 and len(soul.calls) == 1 and len(other.calls) == 2
    [(_, d)] = events(conn, "fetch.vpn_down")
    assert d["source"] == "soulseek" and "stopped" in d["reason"]
    assert events(conn, "fetch.fallback_miss") != [] and all(m["source"] == "orpheus"
                                                             for _, m in events(conn, "fetch.fallback_miss"))
    assert [h["fallbacks_tried"] for _, h in events(conn, "fetch.held_lossy")] == [["soulseek", "orpheus"], ["orpheus"]]
    assert "1 vpn down" in "\n".join(out)


@needs_ffmpeg
def test_a_vpn_down_at_preflight_disables_only_soulseek(flac, aac, dirs):
    conn, a = seed(1)
    soul = FakeFallback("soulseek", FallbackMiss("never"), disable_on_check="VPN is not running (status: stopped)")
    other = FakeFallback("orpheus", gives(flac), owns_workdir=True)
    res, out = lossy_run(conn, aac, dirs, [soul, other])
    assert res["stopped"] is None and (res["placed"], res["via_fallback"], res["vpn_down"]) == (1, 1, 1)
    assert soul.calls == [] and (soul.checked, other.checked) == (1, 1)
    [(_, d)] = events(conn, "fetch.vpn_down")
    assert d["source"] == "soulseek" and "stopped" in d["reason"]
    text = "\n".join(out)
    assert text.count("soulseek") >= 1 and "VPN is not running" in text and "1 vpn down" in text
    assert [ln for ln in out if "VPN is not running" in ln and ln.startswith("fallback")] and len(
        [ln for ln in out if ln.startswith("fallback soulseek")]) == 1


@needs_ffmpeg
def test_another_check_problem_still_stops_setup_even_beside_a_down_vpn(dirs):
    conn, a = seed(1)
    soul = FakeFallback("soulseek", disable_on_check="VPN is not running")
    other = FakeFallback("orpheus", problem="no orpheus.py")
    res, out = run(conn, tidal(1), FakeHttp(), dirs, fallbacks=[soul, other])
    assert res["stopped"] == "setup" and "no orpheus.py" in "\n".join(out)
