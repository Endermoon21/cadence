import json

import requests

from cadence import report
from cadence.catalog.link import link_files
from cadence.catalog.sync import clear_cache, resolve_artist, sync_all, sync_artist, work_status
from cadence.ledger import db
from cadence.sources.base import ArtistRef
from cadence.sources.tidal import CatalogCache, TidalCatalog
from tests.fakes import (FakeCatalog, FakeSession, make_gate, raw_album, raw_artist, raw_track, rel, trk)

WILL = ArtistRef(1, "Will")
OTHER = ArtistRef(2, "Other")


def mem():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def add_artist(conn, name, **cols):
    from cadence.identity.normalize import norm_artist
    cur = conn.execute("INSERT INTO artist(name, norm_name) VALUES (?, ?)", (name, norm_artist(name)))
    if cols:
        conn.execute("UPDATE artist SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE id = ?",
                     (*cols.values(), cur.lastrowid))
    conn.commit()
    return cur.lastrowid


def row(conn, artist_id):
    return conn.execute("SELECT * FROM artist WHERE id = ?", (artist_id,)).fetchone()


def will_setup(releases, tracks):
    conn = mem()
    aid = add_artist(conn, "Will", tidal_id=1, tidal_match="auto")
    return conn, aid, FakeCatalog(make_gate(), releases={1: releases}, tracks=tracks)


# ---------------------------------------------------------------- sync

def test_own_vs_appears_on_by_first_artist_and_tracks_only_for_own():
    conn, aid, cat = will_setup(
        [rel(10, "Mine", [WILL]), rel(11, "Collab", [OTHER, WILL]), rel(12, "Comp", [OTHER], "compilation")],
        {10: [trk(100, "Song", 100, "ISRC1")], 11: [trk(110, "Other Song", 100, "ISRC2")]})
    stats = sync_artist(conn, cat, row(conn, aid))
    roles = {r["title"]: r["role"] for r in conn.execute("SELECT title, role FROM release")}
    assert roles == {"Mine": "own", "Collab": "appears_on", "Comp": "appears_on"}
    assert cat.track_calls == [10]
    assert conn.execute("SELECT COUNT(*) FROM track").fetchone()[0] == 1
    assert (stats["own"], stats["appears_on"]) == (1, 2)
    assert conn.execute("SELECT provider_ref FROM release_source WHERE provider = 'tidal' "
                        "ORDER BY provider_ref").fetchall()[0][0] == "10"
    assert row(conn, aid)["catalog_synced_at"]


def test_recording_deduped_by_isrc_across_single_and_album():
    conn, aid, cat = will_setup(
        [rel(10, "Hahaha", [WILL]), rel(11, "Hahaha", [WILL], "album")],
        {10: [trk(100, "Hahaha", 160, "ISRC1")],
         11: [trk(110, "Hahaha", 161, "ISRC1", number=1), trk(111, "Other", 90, "ISRC2", number=2)]})
    sync_artist(conn, cat, row(conn, aid))
    assert conn.execute("SELECT COUNT(*) FROM recording").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM track WHERE recording_id = "
                        "(SELECT id FROM recording WHERE isrc = 'ISRC1')").fetchone()[0] == 2
    sync_artist(conn, cat, row(conn, aid))                   # re-sync is idempotent
    assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("release", "track", "recording")] \
        == [2, 3, 2]


def test_recording_without_isrc_deduped_by_title_and_duration():
    conn, aid, cat = will_setup(
        [rel(10, "Song", [WILL]), rel(11, "Album", [WILL], "album")],
        {10: [trk(100, "Song", 200)],
         11: [trk(110, "Song", 202, number=1),                # within 2 s -> same recording
              trk(111, "Song", 210, number=2),                # too far -> different recording
              trk(112, "Song (Live)", 200, number=3, version=None)]})   # different variant
    sync_artist(conn, cat, row(conn, aid))
    recs = conn.execute("SELECT norm_title, variant, isrc FROM recording ORDER BY id").fetchall()
    assert [(r["norm_title"], r["variant"], r["isrc"]) for r in recs] == [
        ("song", None, None), ("song", None, None), ("song [live]", "live", None)]
    assert conn.execute("SELECT COUNT(DISTINCT recording_id) FROM track WHERE tidal_id IN (100, 110)"
                        ).fetchone()[0] == 1


def test_shared_release_is_not_stolen_by_appears_on_but_is_claimed_by_owner():
    conn = mem()
    a = add_artist(conn, "Other", tidal_id=2, tidal_match="auto")
    b = add_artist(conn, "Will", tidal_id=1, tidal_match="auto")
    collab = rel(10, "Collab", [OTHER, WILL])
    tracks = {10: [trk(100, "Collab Song", 100, "ISRC1")]}
    # Will syncs first and sees it as appears_on; Other syncs second and owns it
    sync_artist(conn, FakeCatalog(make_gate(), releases={1: [collab]}, tracks=tracks), row(conn, b))
    assert conn.execute("SELECT role, artist_id FROM release").fetchone()[:] == ("appears_on", b)
    sync_artist(conn, FakeCatalog(make_gate(), releases={2: [collab]}, tracks=tracks), row(conn, a))
    assert conn.execute("SELECT role, artist_id FROM release").fetchone()[:] == ("own", a)
    # Will re-syncs: must not take it back
    stats = sync_artist(conn, FakeCatalog(make_gate(), releases={1: [collab]}, tracks=tracks), row(conn, b))
    assert stats["shared"] == 1
    assert conn.execute("SELECT role, artist_id FROM release").fetchone()[:] == ("own", a)


# ---------------------------------------------------------------- resolve

def resolve(search, **artist_cols):
    conn = mem()
    aid = add_artist(conn, "Will", **artist_cols)
    cat = FakeCatalog(make_gate(), search={"Will": search})
    return conn, aid, cat, resolve_artist(conn, cat, row(conn, aid))


def test_resolve_single_exact_match_is_auto():
    conn, aid, cat, tid = resolve([ArtistRef(5, "Will Smith"), ArtistRef(6, "will")])
    assert tid == 6
    r = row(conn, aid)
    assert (r["tidal_id"], r["tidal_match"], r["tidal_candidates"]) == (6, "auto", None)
    assert conn.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'artist.resolve'").fetchone()[0] == 1


def test_resolve_ambiguous_takes_first_and_stores_candidates():
    conn, aid, cat, tid = resolve([ArtistRef(5, "Will"), ArtistRef(6, "WILL"), ArtistRef(7, "Willy")])
    assert tid == 5
    r = row(conn, aid)
    assert (r["tidal_id"], r["tidal_match"]) == (5, "ambiguous")
    assert json.loads(r["tidal_candidates"]) == [{"id": 5, "name": "Will"}, {"id": 6, "name": "WILL"}]


def test_resolve_none_stores_top_five_candidates():
    found = [ArtistRef(i, f"Willa {i}") for i in range(10, 17)]
    conn, aid, cat, tid = resolve(found)
    assert tid is None
    r = row(conn, aid)
    assert (r["tidal_id"], r["tidal_match"]) == (None, "none")
    assert [c["id"] for c in json.loads(r["tidal_candidates"])] == [10, 11, 12, 13, 14]


def test_resolve_keeps_existing_auto_and_manual_without_searching():
    for kind in ("auto", "manual"):
        conn, aid, cat, tid = resolve([ArtistRef(9, "Will")], tidal_id=42, tidal_match=kind)
        assert tid == 42 and cat.gate.calls == 0 and row(conn, aid)["tidal_match"] == kind


def test_resolve_unique_clash_is_ambiguous_without_id():
    conn = mem()
    add_artist(conn, "Someone Else", tidal_id=5, tidal_match="auto")
    aid = add_artist(conn, "Will")
    cat = FakeCatalog(make_gate(), search={"Will": [ArtistRef(5, "Will")]})
    assert resolve_artist(conn, cat, row(conn, aid)) is None
    r = row(conn, aid)
    assert (r["tidal_id"], r["tidal_match"]) == (None, "ambiguous")
    assert json.loads(r["tidal_candidates"]) == [{"id": 5, "name": "Will"}]


def test_sync_all_skips_unresolved_artists():
    conn = mem()
    add_artist(conn, "Nobody")
    res = sync_all(conn, FakeCatalog(make_gate()))
    assert (res["synced"], res["unresolved"], res["stopped"]) == (0, 1, None)


# ---------------------------------------------------------------- sync_all stops

def three_artists(trip_on_tracks=None, fail_on_releases=None, fail_on_tracks=None):
    """Aaa: 1 release; Bbb: 2 releases; Ccc: 1 release. A full sync costs 3 + 4 + 3 gate calls."""
    conn = mem()
    names = ["Aaa", "Bbb", "Ccc"]
    for n in names:
        add_artist(conn, n)
    refs = {n: ArtistRef(i, n) for i, n in enumerate(names, start=1)}
    search = {n: [r] for n, r in refs.items()}
    releases = {1: [rel(10, "Aaa single", [refs["Aaa"]])],
                2: [rel(20, "Bbb one", [refs["Bbb"]]), rel(21, "Bbb two", [refs["Bbb"]])],
                3: [rel(30, "Ccc single", [refs["Ccc"]])]}
    tracks = {10: [trk(100, "Aaa song", 100, "ISRC1")], 20: [trk(200, "Bbb song", 100, "ISRC2")],
              21: [trk(210, "Bbb other", 100, "ISRC3")], 30: [trk(300, "Ccc song", 100, "ISRC4")]}
    return conn, FakeCatalog(make_gate(), search, releases, tracks, trip_on_tracks=trip_on_tracks,
                             fail_on_releases=fail_on_releases, fail_on_tracks=fail_on_tracks)


def test_sync_all_budget_stop_is_enforced_inside_the_source():
    conn, cat = three_artists()
    res = sync_all(conn, cat, max_calls=4)
    # Aaa costs 3 calls; Bbb's search is call 4; its release listing would be call 5 -> refused
    assert res["stopped"] == "budget" and res["synced"] == 1
    assert cat.gate.calls == 4
    done = {r["name"]: r["catalog_synced_at"] for r in conn.execute("SELECT name, catalog_synced_at FROM artist")}
    assert done["Aaa"] and not done["Bbb"] and not done["Ccc"]
    assert conn.execute("SELECT tidal_match FROM artist WHERE name = 'Bbb'").fetchone()[0] == "auto"   # kept
    assert conn.execute("SELECT COUNT(*) FROM release WHERE artist_id = 2").fetchone()[0] == 0
    res = sync_all(conn, cat, max_calls=100)         # resume: Aaa is skipped for free, Bbb doesn't search again
    assert (res["stopped"], res["synced"], res["skipped"]) == (None, 2, 1)
    assert cat.gate.calls == 10


def test_budget_zero_stops_before_any_call():
    conn, cat = three_artists()
    res = sync_all(conn, cat, max_calls=0)
    assert res["stopped"] == "budget" and cat.gate.calls == 0 and res["synced"] == 0


def test_sync_all_names_filter_and_unknown():
    conn, cat = three_artists()
    res = sync_all(conn, cat, names=["bbb", "Nope"])
    assert [a["artist"] for a in res["artists"]] == ["Bbb"] and res["unknown"] == ["Nope"]


def test_sync_all_force_resyncs():
    conn, cat = three_artists()
    sync_all(conn, cat, names=["Aaa"])
    calls = cat.gate.calls
    sync_all(conn, cat, names=["Aaa"])
    assert cat.gate.calls == calls
    sync_all(conn, cat, names=["Aaa"], force=True)
    assert cat.gate.calls > calls


def test_paused_stop_keeps_earlier_artists_and_writes_nothing_of_the_interrupted_one(tmp_path):
    path = tmp_path / "c.db"
    conn = db.connect(path)
    db.migrate(conn)
    for n in ("Aaa", "Bbb", "Ccc"):
        add_artist(conn, n)
    cat = three_artists(trip_on_tracks=21)[1]        # Bbb: listing ok, tracks of release 20 ok, 2nd tracks call 429s
    res = sync_all(conn, cat)
    assert res["stopped"] == "paused" and res["until"] and res["synced"] == 1
    assert cat.track_calls == [10, 20, 21]
    other = db.connect(path)                         # a second connection only sees committed data
    synced = {r["name"]: r["catalog_synced_at"] for r in other.execute("SELECT name, catalog_synced_at FROM artist")}
    assert synced["Aaa"] and not synced["Bbb"] and not synced["Ccc"]
    for table in ("release", "recording"):
        assert other.execute(f"SELECT COUNT(*) FROM {table} WHERE artist_id = 2").fetchone()[0] == 0
    assert other.execute("SELECT COUNT(*) FROM track WHERE release_id IN (SELECT id FROM release "
                         "WHERE artist_id = 2)").fetchone()[0] == 0
    assert other.execute("SELECT COUNT(*) FROM release").fetchone()[0] == 1          # only Aaa's
    assert other.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'gate.pause'").fetchone()[0] == 1
    calls = cat.gate.calls
    again = sync_all(conn, cat, force=True)          # still paused: no further calls
    assert again["stopped"] == "paused" and cat.gate.calls == calls


def test_unexpected_error_is_logged_without_secrets_and_the_run_continues():
    secret = RuntimeError("boom at https://api.tidal.com/v1/artists/2/albums?sessionId=SECRET&countryCode=US")
    conn, cat = three_artists(fail_on_releases={2: secret})
    res = sync_all(conn, cat)
    assert (res["stopped"], res["synced"], res["errors"]) == (None, 2, 1)
    assert [(a["artist"], a["status"]) for a in res["artists"]] == [("Aaa", "synced"), ("Bbb", "error"),
                                                                    ("Ccc", "synced")]
    ev = conn.execute("SELECT subject, message FROM event_log WHERE kind = 'catalog.error'").fetchall()
    assert [(e["subject"], e["message"]) for e in ev] == [("Bbb", "RuntimeError: boom at https://api.tidal.com/v1/artists/2/albums")]
    assert conn.execute("SELECT COUNT(*) FROM release WHERE artist_id = 2").fetchone()[0] == 0
    assert not conn.execute("SELECT catalog_synced_at FROM artist WHERE name = 'Bbb'").fetchone()[0]


def test_error_message_redacts_session_ids_even_without_a_query_string():
    conn, cat = three_artists(fail_on_releases={1: ValueError("bad sessionId=abc123 Bearer tok123")})
    sync_all(conn, cat, names=["Aaa"])
    msg = conn.execute("SELECT message FROM event_log WHERE kind = 'catalog.error'").fetchone()[0]
    assert "abc123" not in msg and "tok123" not in msg and msg.startswith("ValueError: bad ")


def test_connection_error_stops_the_whole_run():
    err = requests.exceptions.ConnectionError("Max retries exceeded with url: /v1/x?sessionId=SECRET")
    conn, cat = three_artists(fail_on_releases={2: err})
    res = sync_all(conn, cat)
    assert (res["stopped"], res["synced"], res["errors"]) == ("network", 1, 1)
    assert [a["artist"] for a in res["artists"]] == ["Aaa", "Bbb"]                  # Ccc never attempted
    msg = conn.execute("SELECT message FROM event_log WHERE kind = 'catalog.error'").fetchone()[0]
    assert msg.startswith("ConnectionError:") and "SECRET" not in msg and "?" not in msg


# ---------------------------------------------------------------- ambiguous artists / vanished releases

def test_ambiguous_artist_is_not_synced_until_set_manually():
    conn = mem()
    aid = add_artist(conn, "Will")
    cat = FakeCatalog(make_gate(), search={"Will": [ArtistRef(5, "Will"), ArtistRef(6, "Will")]},
                      releases={5: [rel(10, "Mine", [ArtistRef(5, "Will")])], 6: []},
                      tracks={10: [trk(100, "Song", 100, "I1")]})
    res = sync_all(conn, cat)
    assert (res["synced"], res["ambiguous"]) == (0, 1)
    assert res["artists"][0] == {"artist": "Will", "status": "ambiguous", "candidates": 2}
    assert cat.gate.calls == 1                                  # the search only
    assert conn.execute("SELECT COUNT(*) FROM release").fetchone()[0] == 0
    r = row(conn, aid)
    assert (r["tidal_match"], r["catalog_synced_at"]) == ("ambiguous", None)
    assert len(json.loads(r["tidal_candidates"])) == 2
    assert conn.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'artist.resolve'").fetchone()[0] == 1

    conn.execute("UPDATE artist SET tidal_id = 5, tidal_match = 'manual', tidal_candidates = NULL, "
                 "catalog_synced_at = NULL WHERE id = ?", (aid,))                   # what `artist set-tidal` does
    res = sync_all(conn, cat)
    assert (res["synced"], res["ambiguous"]) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM release").fetchone()[0] == 1


def test_releases_missing_from_the_current_listing_are_hidden_not_deleted():
    conn = mem()
    aid = add_artist(conn, "Will", tidal_id=1, tidal_match="auto")
    me = ArtistRef(1, "Will")
    a, b, c = rel(10, "A", [me]), rel(11, "B", [me]), rel(12, "C", [me])
    appears = rel(13, "Feat", [OTHER, me])
    tracks = {10: [trk(100, "a", 100, "I1")], 11: [trk(110, "b", 100, "I2")], 12: [trk(120, "c", 100, "I3")]}
    cat = FakeCatalog(make_gate(), releases={1: [a, b, c, appears]}, tracks=tracks)
    sync_artist(conn, cat, row(conn, aid))
    conn.execute("UPDATE release SET hidden_reason = 'user' WHERE title = 'C'")

    cat._releases[1] = [a, appears]                              # B and C vanished from Tidal's listing
    stats = sync_artist(conn, cat, row(conn, aid))
    hidden = {r["title"]: r["hidden_reason"] for r in conn.execute("SELECT title, hidden_reason FROM release")}
    assert hidden == {"A": None, "B": "not_in_tidal_listing", "C": "user", "Feat": None}
    assert stats["hidden"] == 1
    assert conn.execute("SELECT COUNT(*) FROM track").fetchone()[0] == 3                  # nothing deleted
    assert [r["title"] for r in report.artist_report(conn, aid)["releases"]] == ["A"]

    cat._releases[1] = [a, b, appears]                           # B is listed again: un-hidden; user's C stays hidden
    sync_artist(conn, cat, row(conn, aid))
    hidden = {r["title"]: r["hidden_reason"] for r in conn.execute("SELECT title, hidden_reason FROM release")}
    assert hidden["B"] is None and hidden["C"] == "user"


def test_changing_the_tidal_id_rehides_releases_of_the_old_artist():
    conn = mem()
    aid = add_artist(conn, "Will", tidal_id=1, tidal_match="auto")
    old, new = ArtistRef(1, "Will"), ArtistRef(9, "Will")
    cat = FakeCatalog(make_gate(), releases={1: [rel(10, "Old", [old]), rel(11, "Both", [old])],
                                             9: [rel(11, "Both", [new])]},
                      tracks={10: [trk(100, "o", 100, "I1")], 11: [trk(110, "b", 100, "I2")]})
    sync_all(conn, cat)
    conn.execute("UPDATE artist SET tidal_id = 9, tidal_match = 'manual', catalog_synced_at = NULL")
    sync_all(conn, cat)
    hidden = {r["title"]: r["hidden_reason"] for r in conn.execute("SELECT title, hidden_reason FROM release")}
    assert hidden == {"Old": "not_in_tidal_listing", "Both": None}


# ---------------------------------------------------------------- real-shaped Tidal JSON regressions

def test_unavailable_instrumental_stays_a_separate_recording_from_the_vocal():
    for with_isrc in (True, False):
        me = raw_artist(1, "Will")
        isrc = (lambda x: x) if with_isrc else (lambda x: None)
        session = FakeSession({
            ("artists/1/albums", "EPSANDSINGLES", 0): [raw_album(10, "Chandelier", type="SINGLE", artists=[me]),
                                                       raw_album(11, "Chandelier", type="SINGLE", artists=[me])],
            ("albums/10/tracks", None, 0): [raw_track(100, "Chandelier", isrc=isrc("QM24S2009107"), duration=127,
                                                      trackNumber=1)],
            ("albums/11/tracks", None, 0): [raw_track(110, "Chandelier", version="Instrumental Version",
                                                      isrc=isrc("QM24S2009206"), duration=127, trackNumber=1,
                                                      allowStreaming=False, streamReady=False)],
        })
        conn = mem()
        aid = add_artist(conn, "Will", tidal_id=1, tidal_match="manual")
        cat = TidalCatalog(session, make_gate(), CatalogCache(conn))
        sync_artist(conn, cat, row(conn, aid))
        recs = conn.execute("SELECT norm_title, variant FROM recording ORDER BY id").fetchall()
        assert [(r["norm_title"], r["variant"]) for r in recs] == [
            ("chandelier", None), ("chandelier [instrumental]", "instrumental")], with_isrc
        assert conn.execute("SELECT COUNT(DISTINCT recording_id) FROM track").fetchone()[0] == 2


def test_unknown_duration_never_dedupes_but_resync_is_stable():
    conn, aid, cat = will_setup(
        [rel(10, "Song", [WILL]), rel(11, "Album", [WILL], "album")],
        {10: [trk(100, "Song", None)], 11: [trk(110, "Song", None), trk(111, "Song", 200, number=2)]})
    sync_artist(conn, cat, row(conn, aid))
    assert conn.execute("SELECT COUNT(*) FROM recording").fetchone()[0] == 3
    sync_artist(conn, cat, row(conn, aid))                       # same Tidal tracks keep their recordings
    assert conn.execute("SELECT COUNT(*) FROM recording").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM track").fetchone()[0] == 3


# ---------------------------------------------------------------- link

def add_file(conn, artist_id, path, title, dur, album=None, isrc=None, **cols):
    conn.execute("INSERT INTO file(path, artist_id, tag_title, duration_s, tag_album, tag_isrc) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (path, artist_id, title, dur, album, isrc))
    if cols:
        conn.execute("UPDATE file SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE path = ?",
                     (*cols.values(), path))
    conn.commit()


def file_row(conn, path):
    return conn.execute("SELECT * FROM file WHERE path = ?", (path,)).fetchone()


def test_link_isrc_fuzzy_and_unmatched():
    conn, aid, cat = will_setup(
        [rel(10, "Album", [WILL], "album")],
        {10: [trk(100, "Song", 100, "ISRC1", number=1), trk(101, "Other (Remastered 2011)", 200, "ISRC2", number=2)]})
    sync_artist(conn, cat, row(conn, aid))
    add_file(conn, aid, "a.flac", "Wrong Title", 5, isrc="isrc1 ")
    add_file(conn, aid, "b.flac", "Other", 201.2, album="Album")
    add_file(conn, aid, "c.flac", "Other", 230)                 # too long -> never guess
    add_file(conn, None, "d.flac", "Song", 100)                  # no artist -> not considered
    stats = link_files(conn)
    assert stats == {"files": 3, "isrc": 1, "fuzzy": 1, "unmatched": 1, "matched": 2}
    a, b, c = (file_row(conn, p) for p in ("a.flac", "b.flac", "c.flac"))
    assert (a["match"], a["match_score"], b["match"], b["match_score"], c["match"]) == (
        "isrc", 1.0, "fuzzy", 0.9, "unmatched")
    assert a["recording_id"] != b["recording_id"] and c["recording_id"] is None
    assert file_row(conn, "d.flac")["match"] == "unmatched"


def test_manual_matches_are_never_overwritten():
    conn, aid, cat = will_setup([rel(10, "Single", [WILL])], {10: [trk(100, "Song", 100, "ISRC1")]})
    sync_artist(conn, cat, row(conn, aid))
    rec = conn.execute("SELECT id FROM recording").fetchone()[0]
    add_file(conn, aid, "m.flac", "Song", 100, isrc="ISRC1", match="manual", recording_id=None)
    add_file(conn, aid, "n.flac", "Song", 100, isrc="ISRC1")
    add_file(conn, aid, "x.flac", "Song", 100, isrc="ISRC1", match="manual", recording_id=rec, match_score=1.0)
    for rematch in (False, True):
        link_files(conn, aid, rematch=rematch)
    m, x = file_row(conn, "m.flac"), file_row(conn, "x.flac")
    assert (m["match"], m["recording_id"]) == ("manual", None)
    assert (x["match"], x["recording_id"], x["match_score"]) == ("manual", rec, 1.0)
    assert file_row(conn, "n.flac")["recording_id"] == rec


def test_rematch_only_touches_non_manual_and_can_unlink():
    conn, aid, cat = will_setup([rel(10, "Single", [WILL])], {10: [trk(100, "Song", 100, "ISRC1")]})
    sync_artist(conn, cat, row(conn, aid))
    add_file(conn, aid, "a.flac", "Song", 100)
    link_files(conn)
    assert file_row(conn, "a.flac")["match"] == "fuzzy"
    assert link_files(conn)["files"] == 0                         # already matched: not re-evaluated
    conn.execute("UPDATE file SET duration_s = 300")              # the tag was fixed to something that no longer fits
    assert link_files(conn, rematch=True)["unmatched"] == 1
    f = file_row(conn, "a.flac")
    assert (f["match"], f["recording_id"], f["match_score"]) == ("unmatched", None, None)


# ---------------------------------------------------------------- report

def test_report_states_missing_duplicates_unmatched_and_hidden():
    conn, aid, cat = will_setup(
        [rel(10, "Done", [WILL], date="2020-01-01"), rel(11, "Half", [WILL], "ep", date="2021-01-01"),
         rel(12, "None", [WILL], date="2022-01-01"), rel(13, "Hidden", [WILL], date="2019-01-01"),
         rel(14, "Feat", [OTHER, WILL])],
        {10: [trk(100, "One", 100, "I1")],
         11: [trk(110, "One", 100, "I1", number=1), trk(111, "Two", 100, "I2", number=2)],
         12: [trk(120, "Three", 100, "I3")], 13: [trk(130, "Four", 100, "I4")]})
    sync_artist(conn, cat, row(conn, aid))
    conn.execute("UPDATE release SET hidden_reason = 'user' WHERE title = 'Hidden'")
    add_file(conn, aid, "one-a.flac", "One", 100)
    add_file(conn, aid, "one-b.flac", "One", 100)
    add_file(conn, aid, "stray.flac", "Not On Tidal", 100)
    link_files(conn)
    d = report.artist_report(conn, aid)
    assert [(r["title"], r["state"], r["owned"], r["total"]) for r in d["releases"]] == [
        ("Done", "owned", 1, 1), ("Half", "partial", 1, 2), ("None", "missing", 0, 1)]   # ordered by date, no Hidden
    assert [(m["title"], m["releases"]) for m in d["missing_recordings"]] == [("Two", ["Half"]), ("Three", ["None"])]
    assert (d["recordings_total"], d["recordings_owned"], d["appears_on"], d["tidal_match"]) == (3, 1, 1, "auto")
    assert [f["path"] for f in d["unmatched_files"]] == ["stray.flac"]
    assert [(x["title"], x["paths"]) for x in d["duplicates"]] == [("One", ["one-a.flac", "one-b.flac"])]

    summary = report.library_summary(conn)
    assert summary == [{"artist_id": aid, "artist": "Will", "tidal_match": "auto", "own_releases": 3,
                        "recordings_owned": 1, "recordings_total": 3, "missing": 2, "unmatched_files": 1}]
    text = report.format_artist_report(d)
    assert "Two" in text and "stray.flac" in text and "partial" in text and "one-b.flac" in text
    assert "Will" in report.format_summary(summary)
    assert report.format_summary([]) == "no artists in the ledger"


def test_shared_isrc_across_artists_links_and_is_not_missing_for_the_second_artist():
    conn = mem()
    alpha = add_artist(conn, "Alpha", tidal_id=1, tidal_match="auto")
    beta = add_artist(conn, "Beta", tidal_id=2, tidal_match="auto")
    A, B = ArtistRef(1, "Alpha"), ArtistRef(2, "Beta")
    shared = trk(100, "Collab", 200, "SHARED1")
    sync_artist(conn, FakeCatalog(make_gate(), releases={1: [rel(10, "Collab", [A])]}, tracks={10: [shared]}),
                row(conn, alpha))                                        # alpha first: recording belongs to alpha
    sync_artist(conn, FakeCatalog(make_gate(), releases={2: [rel(20, "Beta LP", [B], "album")]},
                                  tracks={20: [trk(200, "Collab", 200, "SHARED1", number=1),
                                               trk(201, "Own Song", 100, "OWN1", number=2)]}),
                row(conn, beta))
    assert conn.execute("SELECT COUNT(*) FROM recording").fetchone()[0] == 2
    assert conn.execute("SELECT artist_id FROM recording WHERE isrc = 'SHARED1'").fetchone()[0] == alpha

    add_file(conn, beta, "beta/collab.flac", "Collab", 200, isrc="SHARED1")
    add_file(conn, beta, "beta/own.flac", "Own Song", 100, album="Beta LP")
    stats = link_files(conn, beta)
    assert (stats["isrc"], stats["fuzzy"], stats["unmatched"]) == (1, 1, 0)
    rep = report.artist_report(conn, beta)
    assert rep["missing_recordings"] == [] and rep["unmatched_files"] == []
    assert [r["state"] for r in rep["releases"]] == ["owned"]

    add_file(conn, alpha, "alpha/collab.flac", "Collab", 200, isrc="SHARED1")
    link_files(conn, alpha)
    for who in (alpha, beta):                       # the same recording owned twice shows up for both artists
        d = report.artist_report(conn, who)
        assert [(x["title"], len(x["paths"])) for x in d["duplicates"]] == [("Collab", 2)]


def test_release_with_no_fetched_tracks_is_unknown_not_missing():
    conn, aid, cat = will_setup([rel(10, "Empty", [WILL]), rel(11, "Real", [WILL])],
                                {10: [], 11: [trk(110, "Song", 100, "I1")]})
    sync_artist(conn, cat, row(conn, aid))
    states = {r["title"]: r["state"] for r in report.artist_report(conn, aid)["releases"]}
    assert states == {"Empty": "unknown", "Real": "missing"}


# ---------------------------------------------------------------- hardening for a long run

class ObjectNotFound(Exception):
    """Same class name as tidalapi's."""


class AuthenticationError(Exception):
    """Same class name as tidalapi's."""


def http_error(status, cls=requests.HTTPError):
    e = cls(f"{status} Client Error: for url: https://api.tidal.com/v1/x?sessionId=SECRET")
    e.response = type("R", (), {"status_code": status})()
    return e


def test_404_on_one_release_leaves_it_without_tracks_and_syncs_the_rest():
    for gone in (ObjectNotFound("Object not found"), http_error(404)):
        conn = mem()
        aid = add_artist(conn, "Will", tidal_id=1, tidal_match="auto")
        me = raw_artist(1, "Will")
        pages = {("artists/1/albums", "EPSANDSINGLES", 0): [raw_album(10, "Ok", artists=[me], type="SINGLE"),
                                                            raw_album(11, "Gone", artists=[me], type="SINGLE"),
                                                            raw_album(12, "Fine", artists=[me], type="SINGLE")],
                 ("albums/10/tracks", None, 0): [raw_track(100, "Ok", isrc="I1")],
                 ("albums/12/tracks", None, 0): [raw_track(120, "Fine", isrc="I3")]}

        class Session(FakeSession):
            def _map_request(self, path, params, parse=None):
                if path == "albums/11/tracks":
                    self.requests.append((path, dict(params), parse))
                    raise gone
                return super()._map_request(path, params, parse)

        session = Session(pages)
        session.request.map_request = session._map_request
        cat = TidalCatalog(session, make_gate(), CatalogCache(conn))
        stats = sync_artist(conn, cat, row(conn, aid))
        assert (stats["own"], stats["tracks"], stats["release_missing"]) == (3, 2, 1)
        states = {r["title"]: r["state"] for r in report.artist_report(conn, aid)["releases"]}
        assert states == {"Ok": "missing", "Gone": "unknown", "Fine": "missing"}
        ev = conn.execute("SELECT subject, message, data_json FROM event_log "
                          "WHERE kind = 'catalog.release_missing'").fetchall()
        assert len(ev) == 1 and ev[0]["subject"] == "Will" and json.loads(ev[0]["data_json"]) == {"release_id": 11}
        assert "SECRET" not in ev[0]["message"]
        assert row(conn, aid)["catalog_synced_at"]
        keys = {r[0] for r in conn.execute("SELECT key FROM catalog_cache")}
        assert "tidal:tracks:11" not in keys and {"tidal:tracks:10", "tidal:tracks:12"} <= keys   # failure not cached
        before = len([r for r in session.requests if r[0] == "albums/11/tracks"])
        sync_artist(conn, cat, row(conn, aid))                       # a later sync asks again
        assert len([r for r in session.requests if r[0] == "albums/11/tracks"]) == before + 1


def test_other_errors_on_release_tracks_still_fail_the_artist():
    conn, cat = three_artists(fail_on_tracks={20: http_error(500)})
    res = sync_all(conn, cat)
    assert [(a["artist"], a["status"]) for a in res["artists"]] == [("Aaa", "synced"), ("Bbb", "error"),
                                                                    ("Ccc", "synced")]


def test_auth_failures_stop_the_whole_run():
    chained = RuntimeError("wrapped")
    chained.__cause__ = http_error(401)
    for err in (AuthenticationError("bad token"), http_error(401), chained):
        conn, cat = three_artists(fail_on_releases={2: err})
        res = sync_all(conn, cat)
        assert (res["stopped"], res["synced"], res["errors"]) == ("auth", 1, 1), repr(err)
        assert [a["artist"] for a in res["artists"]] == ["Aaa", "Bbb"]            # Ccc never attempted
        msg = conn.execute("SELECT message FROM event_log WHERE kind = 'catalog.error'").fetchone()[0]
        assert "SECRET" not in msg and "?" not in msg


def test_403_is_a_per_artist_error_not_an_auth_stop():
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = http_error(403)
    for err in (http_error(403), wrapped):
        conn, cat = three_artists(fail_on_releases={2: err})
        res = sync_all(conn, cat)
        assert (res["stopped"], res["synced"], res["errors"]) == (None, 2, 1), repr(err)
        assert [(a["artist"], a["status"]) for a in res["artists"]] == [("Aaa", "synced"), ("Bbb", "error"),
                                                                        ("Ccc", "synced")]


def test_the_gate_decides_before_auth_classification():
    """An AuthenticationError that was caused by a 429 (tidalapi's token_refresh) is a pause, never 'auth'."""
    for err in (AuthenticationError("Authentication failed"), http_error(401)):
        err.__cause__ = http_error(429)
        conn, cat = three_artists(fail_on_releases={2: err})
        res = sync_all(conn, cat)
        assert (res["stopped"], res["errors"]) == ("paused", 0), repr(err)
        assert conn.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'catalog.error'").fetchone()[0] == 0
    # and even if such an exception ever got past the gate, it is not an auth failure
    from cadence.catalog.sync import _is_auth_failure
    e = AuthenticationError("x")
    e.__cause__ = http_error(429)
    assert not _is_auth_failure(e) and _is_auth_failure(AuthenticationError("x"))


def test_timeouts_stop_the_run_like_a_dead_connection():
    for err in (requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)"),
                requests.exceptions.ConnectTimeout("Connect timed out"), TimeoutError("timed out")):
        conn, cat = three_artists(fail_on_releases={2: err})
        res = sync_all(conn, cat)
        assert (res["stopped"], res["synced"], res["errors"]) == ("network", 1, 1), repr(err)
        assert [a["artist"] for a in res["artists"]] == ["Aaa", "Bbb"]


def test_ambiguous_and_none_artists_cost_nothing_on_later_runs_even_with_force():
    conn = mem()
    add_artist(conn, "Twin")
    add_artist(conn, "Ghost")
    cat = FakeCatalog(make_gate(), search={"Twin": [ArtistRef(5, "Twin"), ArtistRef(6, "Twin")],
                                           "Ghost": [ArtistRef(7, "Someone")]})
    first = sync_all(conn, cat)
    assert (first["ambiguous"], first["unresolved"], cat.gate.calls) == (1, 1, 2)
    for force in (False, True):
        again = sync_all(conn, cat, force=force)
        assert (again["ambiguous"], again["unresolved"], cat.gate.calls) == (1, 1, 2)
        assert {a["artist"]: a for a in again["artists"]} == {
            "Ghost": {"artist": "Ghost", "status": "unresolved"},
            "Twin": {"artist": "Twin", "status": "ambiguous", "candidates": 2}}
    assert [work_status(r, True) for r in conn.execute("SELECT * FROM artist ORDER BY norm_name")] == [
        "unresolved", "ambiguous"]


def test_force_bypasses_the_cache_for_that_run():
    conn = mem()
    aid = add_artist(conn, "Will", tidal_id=1, tidal_match="manual")
    session = FakeSession({("artists/1/albums", None, 0): [raw_album(10, artists=[raw_artist(1, "Will")])],
                           ("albums/10/tracks", None, 0): [raw_track(100)]})
    cat = TidalCatalog(session, make_gate(), CatalogCache(conn))
    sync_all(conn, cat)
    n = len(session.requests)
    assert n > 0 and cat.use_cache is True
    assert sync_all(conn, cat)["skipped"] == 1 and len(session.requests) == n          # recently synced
    sync_all(conn, cat, force=True)
    assert cat.use_cache is False and len(session.requests) == 2 * n                   # not served from cache
    sync_all(conn, cat, force=True)
    assert len(session.requests) == 3 * n


def test_clear_cache_all_and_per_artist():
    conn = mem()
    will = add_artist(conn, "Will", tidal_id=1, tidal_match="manual")
    add_artist(conn, "Other", tidal_id=2, tidal_match="manual")
    me, other = raw_artist(1, "Will"), raw_artist(2, "Other")
    session = FakeSession({("artists/1/albums", None, 0): [raw_album(10, artists=[me])],
                           ("albums/10/tracks", None, 0): [raw_track(100)],
                           ("artists/2/albums", None, 0): [raw_album(20, artists=[other])],
                           ("albums/20/tracks", None, 0): [raw_track(200)]})
    cat = TidalCatalog(session, make_gate(), CatalogCache(conn))
    sync_all(conn, cat)
    conn.execute("INSERT INTO catalog_cache(key, body) VALUES ('keep:me', '{}')")
    keys = lambda: sorted(r[0] for r in conn.execute("SELECT key FROM catalog_cache"))
    assert clear_cache(conn, row(conn, will)) == 2                    # releases:1 + tracks:10
    assert "tidal:releases:1" not in keys() and "tidal:tracks:10" not in keys()
    assert {"tidal:releases:2", "tidal:tracks:20", "keep:me"} <= set(keys())
    n = clear_cache(conn)
    assert n == 2 and keys() == ["keep:me"]


def test_clear_cache_per_artist_also_covers_releases_cached_but_never_written():
    conn = mem()
    will = add_artist(conn, "Will", tidal_id=1, tidal_match="manual")
    session = FakeSession({("artists/1/albums", None, 0): [raw_album(10, artists=[raw_artist(1, "Will")]),
                                                            raw_album(11, artists=[raw_artist(1, "Will")])],
                           ("albums/10/tracks", None, 0): [raw_track(100)],
                           ("albums/11/tracks", None, 0): [raw_track(110)]})
    cat = TidalCatalog(session, make_gate(), CatalogCache(conn))
    cat.artist_releases(1)                  # an interrupted sync: listing + one track list cached, nothing in the ledger
    cat.release_tracks(10)
    cat.release_tracks(11)
    cat.release_tracks(99)                  # someone else's
    assert clear_cache(conn, row(conn, will)) == 3
    assert [r[0] for r in conn.execute("SELECT key FROM catalog_cache")] == ["tidal:tracks:99"]


def test_every_own_release_404_is_an_artist_error_not_a_sync():
    conn, cat = three_artists(fail_on_tracks={20: http_error(404), 21: ObjectNotFound("gone")})
    res = sync_all(conn, cat)
    assert [(a["artist"], a["status"]) for a in res["artists"]] == [("Aaa", "synced"), ("Bbb", "error"),
                                                                    ("Ccc", "synced")]
    assert (res["synced"], res["errors"], res["stopped"]) == (2, 1, None)
    ev = conn.execute("SELECT subject, message FROM event_log WHERE kind = 'catalog.error'").fetchall()
    assert len(ev) == 1 and ev[0]["subject"] == "Bbb"
    assert "all 2 own release(s)" in ev[0]["message"] and "not marking the artist as synced" in ev[0]["message"]
    assert "SECRET" not in ev[0]["message"] and "?" not in ev[0]["message"]
    assert not conn.execute("SELECT catalog_synced_at FROM artist WHERE name = 'Bbb'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM release WHERE artist_id = 2").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'catalog.release_missing'").fetchone()[0] == 0


def test_some_404s_are_counted_in_the_run_result():
    conn, cat = three_artists(fail_on_tracks={21: http_error(404)})
    res = sync_all(conn, cat)
    assert (res["synced"], res["errors"], res["release_missing"]) == (3, 0, 1)
    assert next(a for a in res["artists"] if a["artist"] == "Bbb")["release_missing"] == 1


def test_gate_pause_and_budget_during_release_tracks_are_never_swallowed_as_404():
    conn, cat = three_artists(trip_on_tracks=20)
    assert sync_all(conn, cat)["stopped"] == "paused"
    conn, cat = three_artists()
    res = sync_all(conn, cat, max_calls=5)         # Aaa: 3 calls; Bbb: search (4), releases (5), tracks refused
    assert res["stopped"] == "budget" and res["synced"] == 1
    assert conn.execute("SELECT COUNT(*) FROM release WHERE artist_id = 2").fetchone()[0] == 0
