"""link never guesses between two recordings: an ambiguous file stays unmatched and goes to link_review."""
import json
import shutil

import pytest

from cadence import cli
from cadence.catalog.alias import add_alias
from cadence.catalog.link import link_files
from cadence.identity.normalize import norm_title
from cadence.ledger import db
from cadence.ledger.legacy import import_scan
from tests.test_legacy import row, scan


def mem():
    conn = db.connect(":memory:")
    db.migrate(conn)
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('Will', 'will')")
    conn.commit()
    return conn


def add_rec(conn, title, dur, album=None):
    rid = conn.execute("INSERT INTO recording(artist_id, title, norm_title, duration_s) VALUES (1, ?, ?, ?)",
                       (title, norm_title(title), dur)).lastrowid
    if album:
        rel = conn.execute("INSERT INTO release(artist_id, title, norm_title, type) VALUES (1, ?, ?, 'album')",
                           (album, norm_title(album))).lastrowid
        conn.execute("INSERT INTO track(release_id, recording_id, disc, number, title) VALUES (?, ?, 1, 1, ?)",
                     (rel, rid, title))
    conn.commit()
    return rid


def add_file(conn, path, title, dur, album=None, **cols):
    conn.execute("INSERT INTO file(path, artist_id, tag_title, duration_s, tag_album) VALUES (?, 1, ?, ?, ?)",
                 (path, title, dur, album))
    if cols:
        conn.execute("UPDATE file SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE path = ?",
                     (*cols.values(), path))
    conn.commit()


def file_row(conn, path):
    return conn.execute("SELECT * FROM file WHERE path = ?", (path,)).fetchone()


def reviews(conn):
    return conn.execute("SELECT f.path, r.reason, r.candidates FROM link_review r JOIN file f ON f.id = r.file_id "
                        "ORDER BY f.path").fetchall()


def two_way_tie():
    conn = mem()
    a, b = add_rec(conn, "Song", 199), add_rec(conn, "Song", 201)
    add_file(conn, "s.flac", "Song", 200)
    return conn, a, b


def test_tie_between_two_recordings_is_unmatched_with_one_review_row():
    conn, a, b = two_way_tie()
    stats = link_files(conn)
    assert (stats["ambiguous"], stats["unmatched"], stats["matched"]) == (1, 0, 0)
    f = file_row(conn, "s.flac")
    assert (f["match"], f["recording_id"], f["match_score"]) == ("unmatched", None, None)
    (r,) = reviews(conn)
    assert (r["path"], r["reason"]) == ("s.flac", "ambiguous")
    assert json.loads(r["candidates"]) == [
        {"recording_id": a, "norm_title": "song", "duration_s": 199, "score": 0.8},
        {"recording_id": b, "norm_title": "song", "duration_s": 201, "score": 0.8}]


def test_clear_winner_links_with_no_review_row():
    conn = mem()
    near, _far = add_rec(conn, "Song", 200), add_rec(conn, "Song", 201.5)
    add_file(conn, "s.flac", "Song", 200)
    stats = link_files(conn)
    assert (stats["ambiguous"], stats["matched"]) == (0, 1)
    assert file_row(conn, "s.flac")["recording_id"] == near
    assert reviews(conn) == []


def test_no_candidate_is_unmatched_with_no_review_row():
    conn = mem()
    add_rec(conn, "Other", 200)
    add_file(conn, "s.flac", "Song", 200)
    stats = link_files(conn)
    assert (stats["ambiguous"], stats["unmatched"]) == (0, 1)
    assert file_row(conn, "s.flac")["match"] == "unmatched"
    assert reviews(conn) == []


def test_still_ambiguous_keeps_exactly_one_current_row():
    conn, a, b = two_way_tie()
    link_files(conn)
    link_files(conn)
    link_files(conn, rematch=True)
    assert len(reviews(conn)) == 1
    c = add_rec(conn, "Song", 199)                      # a third tied recording changes the candidate list
    link_files(conn)
    (r,) = reviews(conn)
    assert [x["recording_id"] for x in json.loads(r["candidates"])] == [a, b, c]


@pytest.mark.parametrize("rematch", [False, True])
def test_ambiguous_file_that_becomes_unique_links_and_loses_its_review_row(rematch):
    conn = mem()
    add_rec(conn, "Song", 199), add_rec(conn, "Song", 201)
    add_file(conn, "s.flac", "Song", 200, album="My Album")
    link_files(conn)
    assert len(reviews(conn)) == 1
    on_album = add_rec(conn, "Song", 202, album="My Album")      # the claimed album now breaks the tie
    stats = link_files(conn, rematch=rematch)
    assert (stats["ambiguous"], stats["matched"]) == (0, 1)
    assert file_row(conn, "s.flac")["recording_id"] == on_album
    assert reviews(conn) == []


def test_ambiguous_file_that_no_longer_matches_loses_its_review_row():
    conn, _a, _b = two_way_tie()
    link_files(conn)
    conn.execute("UPDATE file SET duration_s = 300")
    assert link_files(conn)["unmatched"] == 1
    assert reviews(conn) == []


def test_rematch_keeps_the_link_of_a_linked_file_with_a_new_tie_and_queues_a_review():
    conn = mem()
    a = add_rec(conn, "Song", 198)
    add_file(conn, "s.flac", "Song", 200)
    link_files(conn)
    before = file_row(conn, "s.flac")
    assert before["recording_id"] == a
    b = add_rec(conn, "Song", 202)                               # now a tie, 2 s either side, two clusters
    assert link_files(conn)["files"] == 0                        # linked files are not re-evaluated
    assert file_row(conn, "s.flac")["recording_id"] == a and reviews(conn) == []
    assert link_files(conn, rematch=True)["ambiguous"] == 1
    f = file_row(conn, "s.flac")
    assert (f["match"], f["recording_id"], f["match_score"]) == (before["match"], a, before["match_score"])
    (r,) = reviews(conn)
    assert (r["path"], r["reason"]) == ("s.flac", "ambiguous")
    assert [x["recording_id"] for x in json.loads(r["candidates"])] == [a, b]
    link_files(conn, rematch=True)
    assert file_row(conn, "s.flac")["recording_id"] == a and len(reviews(conn)) == 1


def test_linked_file_that_stops_matching_keeps_its_link_and_queues_a_no_longer_matches_review():
    conn = mem()
    a = add_rec(conn, "Song", 200)
    add_file(conn, "s.flac", "Song", 200)
    link_files(conn)
    before = file_row(conn, "s.flac")
    assert before["recording_id"] == a
    conn.execute("UPDATE file SET duration_s = 300")
    stats = link_files(conn, rematch=True)
    assert (stats["no_longer_matches"], stats["unmatched"], stats["matched"]) == (1, 0, 0)
    f = file_row(conn, "s.flac")
    assert (f["match"], f["recording_id"], f["match_score"]) == (before["match"], a, before["match_score"])
    (r,) = reviews(conn)
    assert (r["path"], r["reason"]) == ("s.flac", "no_longer_matches")
    assert [x["recording_id"] for x in json.loads(r["candidates"])] == [a]    # same title, but out of tolerance
    link_files(conn, rematch=True)
    assert file_row(conn, "s.flac")["recording_id"] == a and len(reviews(conn)) == 1
    conn.execute("UPDATE file SET duration_s = 200")                           # fits again: the review row goes
    assert link_files(conn, rematch=True)["matched"] == 1 and reviews(conn) == []


def test_never_linked_file_that_matches_nothing_on_rematch_has_no_review_row():
    conn = mem()
    add_rec(conn, "Other", 200)
    add_file(conn, "s.flac", "Song", 200)
    assert link_files(conn, rematch=True)["unmatched"] == 1
    assert reviews(conn) == [] and file_row(conn, "s.flac")["recording_id"] is None


def test_link_reason_check_allows_only_the_two_known_reasons():
    conn, _a, _b = two_way_tie()
    fid = file_row(conn, "s.flac")["id"]
    for reason in ("ambiguous", "no_longer_matches"):
        conn.execute("INSERT OR REPLACE INTO link_review(file_id, reason, candidates) VALUES (?, ?, '[]')", (fid, reason))
    with pytest.raises(Exception):
        conn.execute("INSERT OR REPLACE INTO link_review(file_id, reason, candidates) VALUES (?, 'other', '[]')", (fid,))


def test_manual_rows_are_never_touched_or_reviewed():
    conn, a, _b = two_way_tie()
    add_file(conn, "m.flac", "Song", 200, match="manual", recording_id=a, match_score=1.0)
    add_file(conn, "n.flac", "Song", 200, match="manual")
    for rematch in (False, True):
        link_files(conn, rematch=rematch)
    m, n = file_row(conn, "m.flac"), file_row(conn, "n.flac")
    assert (m["match"], m["recording_id"], m["match_score"]) == ("manual", a, 1.0)
    assert (n["match"], n["recording_id"]) == ("manual", None)
    assert [r["path"] for r in reviews(conn)] == ["s.flac"]


def test_deleting_a_file_deletes_its_review_row():
    conn, _a, _b = two_way_tie()
    link_files(conn)
    conn.execute("DELETE FROM file")
    assert reviews(conn) == []


def test_migration_006_adds_link_review_to_a_ledger_at_version_5(tmp_path):
    for f in db.MIGRATIONS_DIR.glob("00[1-5]_*.sql"):
        shutil.copy(f, tmp_path / f.name)
    conn = db.connect(":memory:")
    assert db.migrate(conn, tmp_path) == [1, 2, 3, 4, 5]
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('A', 'a')")
    conn.execute("INSERT INTO file(path, artist_id) VALUES ('x.flac', 1)")
    conn.commit()
    assert "link_review" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert db.migrate(conn) == [6, 7]
    assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 1
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(link_review)")}
    assert cols == {"file_id", "reason", "candidates", "created_at"}


# ---------------------------------------------------------------- cadence link --review

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CADENCE_DB", str(tmp_path / "c.db"))
    return tmp_path


def counts(conn):
    return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("artist", "recording", "file", "link_review", "event_log")]


def test_link_review_prints_the_queue_and_changes_nothing(env, capsys):
    conn = db.connect(env / "c.db")
    db.migrate(conn)
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('Will', 'will')")
    a, b = add_rec(conn, "Song", 199), add_rec(conn, "Song", 201)
    add_file(conn, "Will/s.flac", "Song", 200)
    add_file(conn, "Will/ok.flac", "Nope", 200)
    link_files(conn)
    before = counts(conn)
    rows_before = [tuple(r) for r in conn.execute("SELECT * FROM file ORDER BY id")]
    conn.close()
    assert cli.main(["link", "--review"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"Will/s.flac  ambiguous  unlinked  {a}  song  199  0.8 | {b}  song  201  0.8"
    assert out[-1] == "link --review: 1 file(s) need review"
    assert len(out) == 2
    conn = db.connect(env / "c.db")
    assert counts(conn) == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM file ORDER BY id")] == rows_before


def test_link_review_says_whether_the_file_is_linked_and_to_which_recording(env, capsys):
    conn = db.connect(env / "c.db")
    db.migrate(conn)
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('Will', 'will')")
    a = add_rec(conn, "Song", 200)
    add_file(conn, "Will/s.flac", "Song", 200)
    link_files(conn)
    conn.execute("UPDATE file SET duration_s = 300")
    link_files(conn, rematch=True)
    conn.close()
    assert cli.main(["link", "--review"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"Will/s.flac  no_longer_matches  linked to {a}  {a}  song  200  -"
    assert out[-1] == "link --review: 1 file(s) need review"


def test_link_review_with_an_empty_queue_exits_0(env, capsys):
    assert cli.main(["link", "--review"]) == 0
    assert capsys.readouterr().out.splitlines() == ["link --review: 0 file(s) need review"]


def reissue_pair(conn):
    a, b = add_rec(conn, "Song", 200, album="Alpha"), add_rec(conn, "Song", 200, album="Alpha")  # R1 and its reissue R2
    conn.execute("UPDATE recording SET canon_id = ?", (a,))  # one cluster, canonical = a
    conn.commit()
    return a, b


def test_reissues_of_one_recording_link_to_the_canonical_id_and_are_not_reviewed():
    conn = mem()
    a, _b = reissue_pair(conn)
    add_file(conn, "Artist 1/Alpha/01 - Song.flac", "Song", 200, album="Alpha")
    stats = link_files(conn)
    assert (stats["ambiguous"], stats["fuzzy"], stats["matched"]) == (0, 1, 1)
    f = file_row(conn, "Artist 1/Alpha/01 - Song.flac")
    assert (f["recording_id"], f["match"], f["match_score"]) == (a, "fuzzy", 0.9)
    assert reviews(conn) == []


def test_a_tie_between_two_clusters_still_goes_to_review_when_one_has_reissues():
    conn = mem()
    a, b = reissue_pair(conn)
    c = add_rec(conn, "Song", 200, album="Alpha")  # its own cluster
    add_file(conn, "s.flac", "Song", 200, album="Alpha")
    stats = link_files(conn)
    assert (stats["ambiguous"], stats["matched"]) == (1, 0)
    assert file_row(conn, "s.flac")["recording_id"] is None
    (r,) = reviews(conn)
    assert [x["recording_id"] for x in json.loads(r["candidates"])] == [a, c]


def test_alias_reset_also_drops_the_files_link_review_row():
    conn, _a, _b = two_way_tie()
    link_files(conn)
    assert len(reviews(conn)) == 1
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('Bill', 'bill')")
    conn.execute("UPDATE file SET tag_albumartist = 'Trio'")
    add_alias(conn, "Trio", "Bill")
    assert file_row(conn, "s.flac")["artist_id"] == 2 and reviews(conn) == []


def test_import_scan_artist_change_also_drops_the_files_link_review_row(tmp_path):
    conn = db.connect(":memory:")
    db.migrate(conn)
    import_scan(conn, scan(tmp_path, row("a.flac", albumartist="Old"), row("b.flac", albumartist="Old")))
    fid = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM file")}
    for p in ("a.flac", "b.flac"):
        conn.execute("INSERT INTO link_review(file_id, reason, candidates) VALUES (?, 'ambiguous', '[]')", (fid[p],))
    conn.commit()
    import_scan(conn, scan(tmp_path, row("a.flac", albumartist="New"), row("b.flac", albumartist="Old")))
    assert [r["file_id"] for r in conn.execute("SELECT file_id FROM link_review")] == [fid["b.flac"]]  # only the moved one
