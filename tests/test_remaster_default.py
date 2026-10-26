"""Editions policy (decided 2026-10-26): the latest remaster is the default; originals are secondary, never deleted."""
from cadence import report
from cadence.catalog.groups import regroup_all
from cadence.catalog.link import link_files
from cadence.catalog.sync import sync_artist
from cadence.identity.grouping import RecordingItem, cluster_recordings, is_remaster
from cadence.identity.normalize import remaster_year
from tests.fakes import rel, trk
from tests.test_groups import snapshot
from tests.test_sync_link_report import WILL, add_file, row, will_setup


def canon_isrcs(conn):
    return {r["isrc"]: conn.execute("SELECT isrc FROM recording WHERE id = ?", (r["canon_id"],)).fetchone()[0]
            for r in conn.execute("SELECT isrc, canon_id FROM recording")}


def synced(releases, tracks):
    conn, aid, cat = will_setup(releases, tracks)
    sync_artist(conn, cat, row(conn, aid))
    return conn, aid


# ---------------------------------------------------------------- recording clusters

def test_a_latest_remaster_is_canonical_over_an_older_remaster_and_the_original():
    conn, _ = synced(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (Remastered)", [WILL], "album", "2003-01-01"),
         rel(12, "Night (2016 Remaster)", [WILL], "album", "2016-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha", 200, "J1")], 12: [trk(120, "Alpha", 200, "K1")]})
    assert canon_isrcs(conn) == {"I1": "K1", "J1": "K1", "K1": "K1"}
    assert conn.execute("SELECT COUNT(*) FROM recording").fetchone()[0] == 3         # the originals are kept


def test_a_remaster_is_recognised_from_the_track_title_too():
    conn, _ = synced(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night", [WILL], "album", "2011-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha (2011 Remaster)", 200, "J1")]})
    assert canon_isrcs(conn) == {"I1": "J1", "J1": "J1"}


def test_b_only_plain_originals_keep_the_earliest_as_canonical():
    conn, _ = synced(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (Deluxe Edition)", [WILL], "album", "2019-01-01"),
         rel(12, "Night (Expanded)", [WILL], "album", "1990-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha", 200, "J1")], 12: [trk(120, "Alpha", 200, "K1")]})
    assert canon_isrcs(conn) == {"I1": "I1", "J1": "I1", "K1": "I1"}


def test_pure_rule_latest_date_then_highest_id_and_undated_remasters_lose():
    def r(id, date, remaster=True, rd="same"):
        return RecordingItem(id, "song", 200, None, date, None, frozenset(), remaster, date if rd == "same" else rd)
    assert cluster_recordings([r(1, "1980-01-01", False), r(2, "2003-01-01"), r(3, "2016-01-01")]) == [[3, 1, 2]]
    assert cluster_recordings([r(4, "2016-01-01"), r(7, "2016-01-01"), r(5, "2016-01-01")]) == [[7, 4, 5]]
    assert cluster_recordings([r(1, "2010-01-01", rd=None), r(2, "2003-01-01")]) == [[2, 1]]
    assert cluster_recordings([r(1, "2010-01-01", rd=None), r(2, "2010-01-01", rd=None)]) == [[2, 1]]


# ---------------------------------------------------------------- edition shown by default

def test_c_the_remaster_edition_is_displayed_when_nothing_is_owned():
    conn, aid = synced(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (Remastered)", [WILL], "album", "2003-01-01"),
         rel(12, "Night (2016 Remaster)", [WILL], "album", "2016-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha", 200, "J1")], 12: [trk(120, "Alpha", 200, "K1")]})
    (night,) = report.artist_report(conn, aid)["releases"]
    assert (night["title"], night["date"], night["editions"], night["extras_missing"]) == (
        "Night (2016 Remaster)", "2016-01-01", 3, 0)


def test_c_a_plain_only_group_still_shows_the_smallest_then_earliest_edition():
    conn, aid = synced(
        [rel(10, "Night (Deluxe)", [WILL], "album", "1980-01-01"), rel(11, "Night", [WILL], "album", "1981-01-01")],
        {10: [trk(100, "Alpha", 200, "I1"), trk(101, "Beta", 180, "I2", 2)], 11: [trk(110, "Alpha", 200, "J1")]})
    (night,) = report.artist_report(conn, aid)["releases"]
    assert (night["title"], night["editions"], night["extras_missing"]) == ("Night", 2, 1)


def test_d_ownership_beats_the_remaster_preference():
    conn, aid = synced(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (2016 Remaster)", [WILL], "album", "2016-01-01")],
        {10: [trk(100, "Alpha", 200, "I1"), trk(101, "Beta", 180, "I2", 2)], 11: [trk(110, "Alpha", 200, "J1")]})
    add_file(conn, aid, "alpha.flac", "Alpha", 200, isrc="J1")
    add_file(conn, aid, "beta.flac", "Beta", 180, isrc="I2")
    link_files(conn)
    (night,) = report.artist_report(conn, aid)["releases"]
    assert (night["title"], night["owned"], night["total"], night["state"]) == ("Night", 2, 2, "owned")
    assert (night["editions"], night["extras_missing"]) == (2, 0)


def test_an_edition_without_tracks_never_wins_even_as_the_latest_remaster():
    conn, aid = synced(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (2016 Remaster)", [WILL], "album", "2016-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: []})
    (night,) = report.artist_report(conn, aid)["releases"]
    assert (night["title"], night["total"], night["editions"]) == ("Night", 1, 2)


# ---------------------------------------------------------------- safety

def test_e_variants_still_do_not_merge_across_release_groups_remaster_or_not():
    conn, _ = synced(
        [rel(10, "Show 1", [WILL], "album", "2021-01-01"), rel(11, "Show 2 (Remastered)", [WILL], "album", "2022-01-01"),
         rel(12, "Night", [WILL], "album", "1980-01-01")],
        {10: [trk(100, "Song (Live)", 342, "L1")], 11: [trk(110, "Song (Live)", 342, "L2")],
         12: [trk(120, "Song", 342, "M1"), trk(121, "Song (Remix)", 342, "M2", 2)]})
    assert canon_isrcs(conn) == {"L1": "L1", "L2": "L2", "M1": "M1", "M2": "M2"}


def test_f_regroup_is_idempotent_with_remasters():
    conn, _ = synced(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (2016 Remaster)", [WILL], "album", "2016-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha", 200, "J1")]})
    before = snapshot(conn)
    assert regroup_all(conn)["clusters"] == 1
    assert snapshot(conn) == before
    regroup_all(conn)
    assert snapshot(conn) == before and canon_isrcs(conn) == {"I1": "J1", "J1": "J1"}


# ---------------------------------------------------------------- the year on the remaster marker ranks first

def test_remaster_year_is_read_only_from_a_remaster_marker():
    from datetime import date
    for title, version, want in [
            ("Giant Steps (Mono) [2020 Remaster]", None, 2020), ("Song (2016 Remaster)", None, 2016),
            ("Song (Remastered 2009)", None, 2009), ("Song [2008 Remaster]", None, 2008),
            ("Song (2020 Digital Remaster)", None, 2020), ("Song (Remastered Stereo 2025 / Take 3)", None, 2025),
            ("Song - 2011 Remaster", None, 2011), ("Song", "Remastered 2013", 2013),
            ("Blues (1987 Original)", None, None), ("Song [1987] (Remastered)", None, None),
            ("Song (Cat. 1234 Remaster)", None, None), ("Song (Remastered 1850)", None, None),
            ("Song (Remastered 2999)", None, None), ("Song (Remastered)", None, None),
            ("Song (Live 1987)", None, None), ("Song (Remastered 1999 / 2011)", None, 2011)]:
        assert remaster_year(title, version) == want, title
    assert remaster_year(f"Song (Remastered {date.today().year})") == date.today().year


def test_blues_1987_original_is_not_a_remaster():
    assert not is_remaster("Blues (1987 Original)") and not is_remaster("Blues [1987]", release=True)
    assert is_remaster("Giant Steps (Mono) [2020 Remaster]")


def test_a_2020_remaster_dated_1960_beats_a_2010_remaster_dated_2026_for_canonical_and_edition():
    conn, aid = synced(
        [rel(10, "Night (2010 Remaster)", [WILL], "album", "2026-01-01"),
         rel(11, "Night (2020 Remaster)", [WILL], "album", "1960-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha", 200, "J1")]})
    assert canon_isrcs(conn) == {"I1": "J1", "J1": "J1"}
    (night,) = report.artist_report(conn, aid)["releases"]
    assert (night["title"], night["date"], night["editions"]) == ("Night (2020 Remaster)", "1960-01-01", 2)


def test_without_a_year_the_release_date_ranks_and_a_year_beats_no_year():
    def r(id, year, date):
        return RecordingItem(id, "song", 200, None, date, None, frozenset(), True, date, year)
    assert cluster_recordings([r(1, None, "2010-01-01"), r(2, None, "2020-01-01")]) == [[2, 1]]
    assert cluster_recordings([r(1, 2005, "1999-01-01"), r(2, None, "2020-01-01")]) == [[1, 2]]
    assert cluster_recordings([r(1, 2016, "2016-01-01"), r(2, 2016, "2018-01-01")]) == [[2, 1]]
    assert cluster_recordings([r(3, 2016, "2016-01-01"), r(2, 2016, "2016-01-01")]) == [[3, 2]]


def test_a_remaster_without_a_year_uses_its_release_date_year():
    conn, aid = synced(
        [rel(10, "Night (2010 Remaster)", [WILL], "album", "2009-01-01"),
         rel(11, "Night (Remastered)", [WILL], "album", "2024-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha", 200, "J1")]})
    assert canon_isrcs(conn) == {"I1": "J1", "J1": "J1"}
    (night,) = report.artist_report(conn, aid)["releases"]
    assert (night["title"], night["date"]) == ("Night (Remastered)", "2024-01-01")
    # and the other way round: an undated-tag remaster released before the tagged one loses to it
    conn, aid = synced(
        [rel(10, "Night (2010 Remaster)", [WILL], "album", "2009-01-01"),
         rel(11, "Night (Remastered)", [WILL], "album", "2005-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha", 200, "J1")]})
    assert canon_isrcs(conn) == {"I1": "I1", "J1": "I1"}
    assert report.artist_report(conn, aid)["releases"][0]["title"] == "Night (2010 Remaster)"
