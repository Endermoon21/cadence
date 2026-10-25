import json

from cadence.ledger import db
from cadence.ledger.legacy import import_scan
from tests.fakes import FIXTURES


def mem():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def scan(tmp_path, *rows) -> str:
    p = tmp_path / "scan.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n")  # trailing blank line is tolerated
    return str(p)


def row(path, **kw):
    base = {"path": path, "size": 10, "mtime": 5, "codec": "flac", "dur": 100.5, "sr": 44100, "bits": 16,
            "br": 900000, "art": True, "title": "T", "artist": "A", "albumartist": "A", "album": "Alb",
            "track": "1", "disc": "1", "date": "2020", "isrc": "XX1", "mb_track": "mb"}
    base.update(kw)
    return base


def test_field_mapping(tmp_path):
    conn = mem()
    stats = import_scan(conn, scan(tmp_path, row("A/x.flac")))
    assert stats["rows"] == 1 and stats["files_new"] == 1 and stats["artists_created"] == 1
    f = conn.execute("SELECT * FROM file").fetchone()
    assert (f["duration_s"], f["sample_rate"], f["bit_depth"], f["bitrate"], f["has_art"]) == (
        100.5, 44100, 16, 900000, 1)
    assert (f["codec"], f["size"], f["mtime"]) == ("flac", 10, 5)
    assert (f["tag_title"], f["tag_artist"], f["tag_albumartist"], f["tag_album"]) == ("T", "A", "A", "Alb")
    assert (f["tag_track"], f["tag_disc"], f["tag_date"], f["tag_isrc"], f["tag_mb_track"]) == (
        "1", "1", "2020", "XX1", "mb")
    assert f["match"] == "unmatched" and f["recording_id"] is None and f["seen_at"]
    assert f["artist_id"] == conn.execute("SELECT id FROM artist").fetchone()[0]


def test_artist_from_albumartist_else_artist_and_skips_various(tmp_path):
    conn = mem()
    stats = import_scan(conn, scan(
        tmp_path,
        row("1.flac", albumartist="Band", artist="Band feat. X"),
        row("2.flac", albumartist=None, artist="Solo"),
        row("3.flac", albumartist="", artist="Solo"),
        row("4.flac", albumartist="Various Artists", artist="Someone"),
        row("5.flac", albumartist=None, artist=None)))
    assert {r["name"] for r in conn.execute("SELECT name FROM artist")} == {"Band", "Solo"}
    by_path = {r["path"]: r["artist_id"] for r in conn.execute("SELECT path, artist_id FROM file")}
    assert by_path["2.flac"] == by_path["3.flac"] is not None
    assert by_path["4.flac"] is None and by_path["5.flac"] is None
    assert stats["files_without_artist"] == 2


def test_artist_upsert_by_norm_name_keeps_first_display(tmp_path):
    conn = mem()
    import_scan(conn, scan(tmp_path, row("1.flac", albumartist="Her's"), row("2.flac", albumartist="Her’s"),
                           row("3.flac", albumartist="The Beatles"), row("4.flac", albumartist="Beatles")))
    names = [r["name"] for r in conn.execute("SELECT name FROM artist ORDER BY id")]
    assert names == ["Her's", "The Beatles"]


def test_reimport_updates_but_keeps_matches(tmp_path):
    conn = mem()
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('A', 'a')")
    conn.execute("INSERT INTO recording(artist_id, title, norm_title) VALUES (1, 'T', 't')")
    path = scan(tmp_path, row("a.flac"), row("b.flac"))
    import_scan(conn, path)
    conn.execute("UPDATE file SET recording_id = 1, match = 'fuzzy', match_score = 0.8 WHERE path = 'a.flac'")
    conn.execute("UPDATE file SET recording_id = 1, match = 'manual', match_score = 1 WHERE path = 'b.flac'")
    conn.commit()
    stats = import_scan(conn, scan(tmp_path, row("a.flac", size=99, title="T2"), row("b.flac", size=98)))
    assert stats["files_new"] == 0 and stats["files_updated"] == 2 and stats["artists_created"] == 0
    rows = {r["path"]: r for r in conn.execute("SELECT * FROM file")}
    assert (rows["a.flac"]["match"], rows["a.flac"]["recording_id"], rows["a.flac"]["match_score"]) == (
        "fuzzy", 1, 0.8)
    assert (rows["b.flac"]["match"], rows["b.flac"]["recording_id"]) == ("manual", 1)
    assert rows["a.flac"]["size"] == 99 and rows["a.flac"]["tag_title"] == "T2"
    assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 2


def test_will_paquin_fixture_import():
    conn = mem()
    stats = import_scan(conn, FIXTURES / "library_will_paquin.jsonl")
    assert stats["rows"] == 36 and stats["artists_created"] == 2
    counts = {r["name"]: r["n"] for r in conn.execute(
        "SELECT a.name, COUNT(*) n FROM file f JOIN artist a ON a.id = f.artist_id GROUP BY a.id")}
    assert counts == {"Will Paquin": 30, "Her's": 6}


def test_reimport_with_a_changed_artist_resets_a_stale_link_but_not_a_manual_one(tmp_path):
    conn = mem()
    import_scan(conn, scan(tmp_path, row("a.flac", albumartist="Old"), row("m.flac", albumartist="Old"),
                           row("s.flac", albumartist="Old")))
    old = conn.execute("SELECT id FROM artist WHERE name = 'Old'").fetchone()[0]
    conn.execute("INSERT INTO recording(artist_id, title, norm_title) VALUES (?, 'T', 't')", (old,))
    conn.execute("UPDATE file SET recording_id = 1, match = 'fuzzy', match_score = 0.8 WHERE path IN ('a.flac', 's.flac')")
    conn.execute("UPDATE file SET recording_id = 1, match = 'manual', match_score = 1 WHERE path = 'm.flac'")
    conn.commit()
    import_scan(conn, scan(tmp_path, row("a.flac", albumartist="New"), row("m.flac", albumartist="New"),
                           row("s.flac", albumartist="Old")))              # s.flac keeps its artist
    rows = {r["path"]: r for r in conn.execute("SELECT * FROM file")}
    new = conn.execute("SELECT id FROM artist WHERE name = 'New'").fetchone()[0]
    assert (rows["a.flac"]["artist_id"], rows["a.flac"]["match"], rows["a.flac"]["recording_id"],
            rows["a.flac"]["match_score"]) == (new, "unmatched", None, None)
    assert (rows["m.flac"]["artist_id"], rows["m.flac"]["match"], rows["m.flac"]["recording_id"]) == (new, "manual", 1)
    assert (rows["s.flac"]["match"], rows["s.flac"]["recording_id"]) == ("fuzzy", 1)
