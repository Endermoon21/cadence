"""Migration 007: file.source also accepts 'soulseek' and 'orpheus'. SQLite cannot alter a CHECK, so `file` is rebuilt."""
import shutil
import sqlite3

import pytest

from cadence.ledger import db


def at_v6(tmp_path):
    for f in db.MIGRATIONS_DIR.glob("00[1-6]_*.sql"):
        shutil.copy(f, tmp_path / f.name)
    conn = db.connect(":memory:")
    assert db.migrate(conn, tmp_path) == [1, 2, 3, 4, 5, 6]
    return conn


def seed(conn):
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('A', 'a')")
    conn.execute("INSERT INTO recording(artist_id, title, norm_title) VALUES (1, 'T', 't')")
    conn.execute("INSERT INTO file(path, artist_id, recording_id, match, match_score, source, codec, sample_rate, "
                 "bit_depth, bitrate, duration_s, size, mtime, has_art, tag_title, tag_isrc, upgradable, "
                 "quality_requested, lossy, quality_received) VALUES ('a.flac', 1, 1, 'manual', 1.0, 'tidal', 'flac', "
                 "44100, 16, 900, 4.5, 123, 456, 1, 'T', 'XX1', 1, 'LOSSLESS', 0, 'FLAC 16/44.1')")
    conn.execute("INSERT INTO file(path, artist_id) VALUES ('b.flac', 1)")
    conn.execute("INSERT INTO link_review(file_id, reason, candidates) VALUES (2, 'ambiguous', '[]')")
    conn.commit()


def snapshot(conn):
    return ([tuple(r) for r in conn.execute("SELECT * FROM file ORDER BY id")],
            [tuple(r) for r in conn.execute("SELECT * FROM link_review")])


def test_007_keeps_file_and_link_review_rows_and_the_column_layout(tmp_path):
    conn = at_v6(tmp_path)
    seed(conn)
    before, cols = snapshot(conn), [tuple(r) for r in conn.execute("PRAGMA table_info(file)")]
    assert db.migrate(conn) == [7]
    assert snapshot(conn) == before
    assert [tuple(r) for r in conn.execute("PRAGMA table_info(file)")] == cols  # same names, order, defaults
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_007_accepts_the_new_sources_and_still_rejects_others(tmp_path):
    conn = at_v6(tmp_path)
    seed(conn)
    db.migrate(conn)
    for i, src in enumerate(("soulseek", "orpheus", "tidal", "ytm", "legacy")):
        conn.execute("INSERT INTO file(path, source) VALUES (?, ?)", (f"n{i}.flac", src))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO file(path, source) VALUES ('bad.flac', 'bogus')")


def test_007_recreates_the_indexes_and_the_link_review_cascade(tmp_path):
    conn = at_v6(tmp_path)
    seed(conn)
    db.migrate(conn)
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='file'")}
    assert {"file_recording", "file_artist", "file_lossy"} <= idx
    conn.execute("DELETE FROM file WHERE id = 2")
    assert conn.execute("SELECT COUNT(*) FROM link_review").fetchone()[0] == 0  # ON DELETE CASCADE still works
    assert "link_review_bak" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_temp_master")}


def test_007_on_a_fresh_ledger_is_head():
    conn = db.connect(":memory:")
    assert db.migrate(conn)[-1] == 7
