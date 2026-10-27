"""Release groups / recording clusters in the DB, and the report semantics on top of them."""
import shutil

from cadence import cli, report
from cadence.catalog.groups import regroup_all, regroup_artist
from cadence.catalog.link import link_files
from cadence.catalog.sync import sync_artist
from cadence.ledger import db
from tests.test_sync_link_report import WILL, add_artist, add_file, mem, row, will_setup
from tests.fakes import make_gate, rel, trk


def night_setup():
    """A 1980 album, its 2018 remaster (new ISRCs, same lengths), a 2019 deluxe with extras, and a single."""
    return will_setup(
        [rel(10, "Night", [WILL], "album", "1980-01-01"),
         rel(11, "Night (Remastered)", [WILL], "album", "2018-01-01"),
         rel(12, "Night (Deluxe Edition)", [WILL], "album", "2019-01-01"),
         rel(13, "Alpha", [WILL], "single", "2021-01-01")],
        {10: [trk(100, "Alpha", 200, "I1", 1), trk(101, "Beta", 180, "I2", 2)],
         11: [trk(110, "Alpha", 200, "J1", 1), trk(111, "Beta", 181, "J2", 2)],
         12: [trk(120, "Alpha", 200, "K1", 1), trk(121, "Beta", 180, "K2", 2),
              trk(122, "Bonus Cut", 150, "K3", 3), trk(123, "Alpha (Live)", 210, "K4", 4)],
         13: [trk(130, "Alpha", 201, "S1", 1)]})


def snapshot(conn):
    return ([tuple(r) for r in conn.execute("SELECT id, group_key FROM release ORDER BY id")],
            [tuple(r) for r in conn.execute("SELECT id, canon_id FROM recording ORDER BY id")])


# ---------------------------------------------------------------- regroup

def test_sync_groups_editions_and_clusters_and_regroup_is_idempotent():
    conn, aid, cat = night_setup()
    sync_artist(conn, cat, row(conn, aid))
    keys = {r["title"]: r["group_key"] for r in conn.execute("SELECT title, group_key FROM release")}
    assert keys == {"Night": "album|night", "Night (Remastered)": "album|night",
                    "Night (Deluxe Edition)": "album|night", "Alpha": "single|alpha"}
    canon = {r["isrc"]: conn.execute("SELECT isrc FROM recording WHERE id = ?", (r["canon_id"],)).fetchone()[0]
             for r in conn.execute("SELECT isrc, canon_id FROM recording")}
    assert canon == {"I1": "J1", "J1": "J1", "K1": "J1", "S1": "J1",     # the 2018 remaster wins over the 1980 original
                     "I2": "J2", "J2": "J2", "K2": "J2", "K3": "K3", "K4": "K4"}
    before, counts = snapshot(conn), [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                      for t in ("release", "recording", "track")]
    stats = regroup_artist(conn, aid)
    assert (stats["groups"], stats["recordings"], stats["clusters"]) == (2, 9, 4)
    assert regroup_all(conn)["clusters"] == 4
    assert snapshot(conn) == before                                        # idempotent
    assert counts == [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("release", "recording", "track")]          # nothing deleted


def test_regroup_after_new_data_recomputes_and_other_artists_are_untouched():
    conn, aid, cat = night_setup()
    other = add_artist(conn, "Other", tidal_id=2, tidal_match="auto")
    conn.execute("INSERT INTO recording(artist_id, title, norm_title, duration_s) VALUES (?, 'x', 'x', 100)", (other,))
    conn.commit()
    sync_artist(conn, cat, row(conn, aid))
    assert conn.execute("SELECT canon_id FROM recording WHERE artist_id = ?", (other,)).fetchone()[0] is None
    regroup_artist(conn, other)
    assert conn.execute("SELECT canon_id = id FROM recording WHERE artist_id = ?", (other,)).fetchone()[0] == 1


def test_migration_002_applies_to_a_database_that_already_has_data(tmp_path):
    shutil.copy(db.MIGRATIONS_DIR / "001_init.sql", tmp_path / "001_init.sql")
    conn = db.connect(":memory:")
    assert db.migrate(conn, tmp_path) == [1]
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('A', 'a')")
    conn.execute("INSERT INTO recording(artist_id, title, norm_title, duration_s) VALUES (1, 't', 't', 100)")
    conn.execute("INSERT INTO release(artist_id, title, norm_title, type) VALUES (1, 'T', 't', 'album')")
    conn.commit()
    assert db.migrate(conn) == [2, 3, 4, 5, 6]
    assert conn.execute("SELECT canon_id FROM recording").fetchone()[0] is None
    assert report.artist_report(conn, 1)["recordings_total"] == 0          # NULL canon_id / group_key are fine
    regroup_all(conn)
    assert conn.execute("SELECT canon_id FROM recording").fetchone()[0] == 1
    assert conn.execute("SELECT group_key FROM release").fetchone()[0] == "album|t"


def test_regroup_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CADENCE_DB", str(tmp_path / "c.db"))
    conn = db.connect(tmp_path / "c.db")
    db.migrate(conn)
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('Will', 'will')")
    conn.execute("INSERT INTO recording(artist_id, title, norm_title, duration_s) VALUES (1, 't', 't', 100)")
    conn.commit()
    conn.close()
    assert cli.main(["regroup"]) == 0
    assert "1 recordings -> 1 clusters" in capsys.readouterr().out
    assert cli.main(["regroup", "--artist", "will"]) == 0
    assert cli.main(["regroup", "--artist", "nobody"]) == 1


# ---------------------------------------------------------------- report semantics

def linked_night():
    conn, aid, cat = night_setup()
    sync_artist(conn, cat, row(conn, aid))
    add_file(conn, aid, "alpha-orig.flac", "Alpha", 200, isrc="I1")
    add_file(conn, aid, "alpha-remaster.flac", "Alpha", 200, isrc="J1")
    add_file(conn, aid, "beta-remaster.flac", "Beta", 181, isrc="J2")
    link_files(conn)
    return conn, aid


def test_owned_through_another_editions_recording_and_latest_remaster_is_shown():
    conn, aid = linked_night()
    d = report.artist_report(conn, aid)
    assert [(r["title"], r["type"], r["owned"], r["total"], r["state"], r["editions"], r["extras_missing"])
            for r in d["releases"]] == [("Night (Remastered)", "album", 2, 2, "owned", 3, 2),   # the remaster beats the equal 1980 original
                                        ("Alpha", "single", 1, 1, "owned", 1, 0)]   # owned via the album's recording
    assert conn.execute("SELECT COUNT(*) FROM file WHERE recording_id IS NOT NULL").fetchone()[0] == 3


def test_recording_totals_are_clusters_with_variants_split_out():
    conn, aid = linked_night()
    d = report.artist_report(conn, aid)
    assert (d["recordings_total"], d["recordings_owned"], d["missing_main"], d["missing_variant"]) == (4, 2, 1, 1)
    assert [(m["title"], m["variant"]) for m in d["missing_recordings"]] == [("Bonus Cut", None),
                                                                            ("Alpha (Live)", "live")]
    assert sorted(m["releases"][0] for m in d["missing_recordings"]) == ["Night (Deluxe Edition)"] * 2
    s = report.library_summary(conn)[0]
    assert (s["own_releases"], s["own_editions"], s["recordings_total"], s["recordings_owned"], s["missing"],
            s["missing_main"], s["missing_variant"]) == (2, 4, 4, 2, 2, 1, 1)
    text = report.format_artist_report(d)
    assert "recordings owned 2/4 (+1 variants not owned)" in text
    assert "(+2 ed., 2 extra missing)" in text and "missing variants (1)" in text
    assert "Will" in report.format_summary([s])


def test_duplicates_are_per_cluster_across_editions():
    conn, aid = linked_night()
    d = report.artist_report(conn, aid)
    assert [(x["title"], x["paths"]) for x in d["duplicates"]] == [("Alpha", ["alpha-orig.flac", "alpha-remaster.flac"])]


def test_edition_with_most_owned_clusters_is_displayed():
    conn, aid, cat = night_setup()
    sync_artist(conn, cat, row(conn, aid))
    add_file(conn, aid, "bonus.flac", "Bonus Cut", 150, isrc="K3")
    add_file(conn, aid, "alpha.flac", "Alpha", 200, isrc="I1")
    link_files(conn)
    night = report.artist_report(conn, aid)["releases"][0]
    assert (night["title"], night["owned"], night["total"], night["state"]) == ("Night (Deluxe Edition)", 2, 4, "partial")
    assert (night["editions"], night["extras_missing"]) == (3, 0)     # the deluxe is a superset of the others


def test_nothing_owned_shows_the_standard_edition_and_an_edition_without_tracks_does_not_win():
    conn, aid, cat = will_setup(
        [rel(10, "Night (Deluxe)", [WILL], "album", "1980-01-01"), rel(11, "Night", [WILL], "album", "1981-01-01"),
         rel(12, "Night (Remastered)", [WILL], "album", "1979-01-01")],
        {10: [trk(100, "Alpha", 200, "I1", 1), trk(101, "Beta", 180, "I2", 2)],
         11: [trk(110, "Alpha", 200, "J1", 1)], 12: []})
    sync_artist(conn, cat, row(conn, aid))
    (night,) = report.artist_report(conn, aid)["releases"]
    assert (night["title"], night["owned"], night["total"], night["state"]) == ("Night", 0, 1, "missing")
    assert (night["editions"], night["extras_missing"]) == (3, 1)


def test_group_with_no_fetched_tracks_is_unknown():
    conn, aid, cat = will_setup([rel(10, "Night", [WILL], "album"), rel(11, "Night (Remastered)", [WILL], "album")],
                                {10: [], 11: []})
    sync_artist(conn, cat, row(conn, aid))
    (night,) = report.artist_report(conn, aid)["releases"]
    assert (night["state"], night["total"], night["editions"]) == ("unknown", 0, 2)


def test_hidden_editions_and_other_types_do_not_join_a_group():
    conn, aid, cat = will_setup(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (Remastered)", [WILL], "album", "2018-01-01"),
         rel(12, "Night", [WILL], "single", "1979-01-01")],
        {10: [trk(100, "Alpha", 200, "I1")], 11: [trk(110, "Alpha", 200, "J1"), trk(111, "Beta", 100, "J2", 2)],
         12: [trk(120, "Night", 100, "S1")]})
    sync_artist(conn, cat, row(conn, aid))
    conn.execute("UPDATE release SET hidden_reason = 'user' WHERE title = 'Night (Remastered)'")
    rows = report.artist_report(conn, aid)["releases"]
    assert [(r["title"], r["type"], r["editions"], r["total"]) for r in rows] == [
        ("Night", "single", 1, 1), ("Night", "album", 1, 1)]


# ---------------------------------------------------------------- variant clustering in the DB

def test_live_recordings_cluster_only_within_one_release_group():
    conn, aid, cat = will_setup(
        [rel(10, "Show 1", [WILL], "album", "2021-01-01"), rel(11, "Show 2", [WILL], "album", "2021-01-02"),
         rel(12, "Show 1 (Remastered)", [WILL], "album", "2022-01-01")],
        {10: [trk(100, "Song (Live)", 342, "L1")], 11: [trk(110, "Song (Live)", 343, "L2")],
         12: [trk(120, "Song (Live)", 342, "L3")]})
    sync_artist(conn, cat, row(conn, aid))
    canon = {r["isrc"]: conn.execute("SELECT isrc FROM recording WHERE id = ?", (r["canon_id"],)).fetchone()[0]
             for r in conn.execute("SELECT isrc, canon_id FROM recording")}
    assert canon == {"L1": "L3", "L3": "L3", "L2": "L2"}     # same concert's remaster merges (and leads), the other show not
    assert report.artist_report(conn, aid)["missing_variant"] == 2


# ---------------------------------------------------------------- album-prefixed titles

def prefixed_setup(deluxe_date="1980-11-01"):
    """Night (1980) has 'Night - Alpha'; its deluxe has plain 'Alpha' (other ISRC, same length); Day is another album;
    the two 'Gamma' singles show the prefix is only an album thing."""
    return will_setup(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (Deluxe)", [WILL], "album", deluxe_date),
         rel(12, "Day", [WILL], "album", "1990-01-01"),
         rel(13, "Gamma", [WILL], "single", "2000-01-01"), rel(14, "Gamma", [WILL], "single", "2001-01-01")],
        {10: [trk(100, "Night - Alpha", 200, "I1", 1), trk(101, "Beta", 180, "I2", 2)],
         11: [trk(110, "Alpha", 200, "K1", 1), trk(111, "Beta", 180, "K2", 2)],
         12: [trk(120, "Night - Alpha", 215, "D1", 1)],
         13: [trk(130, "Gamma - Reprise", 100, "G1")], 14: [trk(140, "Reprise", 100, "G2")]})


def canon_by_isrc(conn):
    return {r["isrc"]: conn.execute("SELECT isrc FROM recording WHERE id = ?", (r["canon_id"],)).fetchone()[0]
            for r in conn.execute("SELECT isrc, canon_id FROM recording")}


def test_regroup_merges_the_album_prefixed_pair_and_leaves_the_others_unchanged():
    conn, aid, cat = prefixed_setup()
    sync_artist(conn, cat, row(conn, aid))
    ids = {r["isrc"]: r["id"] for r in conn.execute("SELECT id, isrc FROM recording")}
    conn.execute("UPDATE recording SET canon_id = id")                   # the ledger as the old rule left it:
    conn.execute("UPDATE recording SET canon_id = ? WHERE id = ?", (ids["I2"], ids["K2"]))   # only 'Beta' merged
    conn.commit()
    before = dict(tuple(r) for r in conn.execute("SELECT id, canon_id FROM recording"))
    stats = regroup_artist(conn, aid)
    after = dict(tuple(r) for r in conn.execute("SELECT id, canon_id FROM recording"))
    assert [i for i in before if before[i] != after[i]] == [ids["K1"]]   # only the plain 'Alpha' moved
    assert canon_by_isrc(conn) == {"I1": "I1", "K1": "I1",              # prefixed + plain, ISRCs differ: merged
                                   "I2": "I2", "K2": "I2",              # plain duplicates: merged as before
                                   "D1": "D1",                          # same title, other album, 15 s apart
                                   "G1": "G1", "G2": "G2"}              # singles never give a prefix
    assert (stats["recordings"], stats["clusters"]) == (7, 5)


def test_prefixed_regroup_is_idempotent_and_deletes_nothing():
    conn, aid, cat = prefixed_setup()
    sync_artist(conn, cat, row(conn, aid))
    first = snapshot(conn)
    counts = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("release", "recording", "track")]
    assert regroup_artist(conn, aid)["clusters"] == regroup_all(conn)["clusters"] == 5
    assert snapshot(conn) == first
    assert counts == [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("release", "recording", "track")]


def test_prefixed_pair_on_albums_of_different_or_unknown_years_stays_apart():
    for date in ("2019-01-01", None):
        conn, aid, cat = prefixed_setup(date)
        sync_artist(conn, cat, row(conn, aid))
        assert canon_by_isrc(conn)["K1"] == "K1", date                # 'Night - Alpha' (1980) vs plain 'Alpha' (2019 / no date)
        assert canon_by_isrc(conn)["I2"] == "I2" and canon_by_isrc(conn)["K2"] == "I2"   # old rule untouched


# ---------------------------------------------------------------- remaster-year marker left in the title (R2)

def remaster_year_setup():
    """Night (1980) keeps '(2011 Remaster)' behind the album prefix and a truncated '(2013 Remaste' in the title
    (a plain '(2011 Remaster)' is already a flag and makes no difference); its deluxe has them plain."""
    return will_setup(
        [rel(10, "Night", [WILL], "album", "1980-01-01"), rel(11, "Night (Deluxe)", [WILL], "album", "1980-11-01"),
         rel(12, "Day", [WILL], "album", "1990-01-01")],
        {10: [trk(100, "Night - Alpha (2011 Remaster)", 200, "I1", 1), trk(101, "Beta (2013 Remaste", 180, "I2", 2),
              trk(102, "Night - Gamma (2011 Remaster)", 200, "I3", 3), trk(103, "Night - Skit (2011 Remaster)", 40, "I4", 4),
              trk(104, "Love 1985", 200, "I5", 5)],
         11: [trk(110, "Alpha", 202, "K1", 1), trk(111, "Beta", 182, "K2", 2), trk(112, "Gamma", 206, "K3", 3),
              trk(113, "Skit", 40, "K4", 4), trk(114, "Love", 200, "K5", 5)],
         12: [trk(120, "Beta", 190, "D2", 1)]})


def test_regroup_merges_the_remaster_year_marked_pairs_and_leaves_the_others_apart():
    conn, aid, cat = remaster_year_setup()
    sync_artist(conn, cat, row(conn, aid))
    ids = {r["isrc"]: r["id"] for r in conn.execute("SELECT id, isrc FROM recording")}
    conn.execute("UPDATE recording SET canon_id = id")                   # the ledger as the old rule left it
    conn.commit()
    before = dict(tuple(r) for r in conn.execute("SELECT id, canon_id FROM recording"))
    stats = regroup_artist(conn, aid)
    after = dict(tuple(r) for r in conn.execute("SELECT id, canon_id FROM recording"))
    assert canon_by_isrc(conn) == {"I1": "I1", "K1": "I1",              # 'night alpha 2011 remaster' + 'alpha', 2 s apart
                                   "I2": "I2", "K2": "I2",              # '(2013 Remaste' + 'beta', 2 s apart
                                   "D2": "D2",                          # a 'Beta' on another album group: not a partner
                                   "I3": "I3", "K3": "K3",              # 6 s apart
                                   "I4": "I4", "K4": "K4",              # under 60 s
                                   "I5": "I5", "K5": "K5"}              # a bare year is not a marker
    assert {i for i in before if before[i] != after[i]} == {ids["K1"], ids["K2"]}
    assert (stats["recordings"], stats["clusters"]) == (11, 9)
    assert regroup_artist(conn, aid) == stats and dict(tuple(r) for r in conn.execute("SELECT id, canon_id FROM recording")) == after
