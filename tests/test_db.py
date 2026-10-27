import json
import shutil
import sqlite3

import pytest

from cadence.ledger import db

TABLES = {"artist", "release", "release_source", "recording", "track", "file", "want", "suggestion",
          "job", "event_log", "catalog_cache", "setting", "card", "card_state", "schema_version",
          "artist_alias"}


def mem():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def test_migrate_creates_schema_and_is_idempotent():
    conn = db.connect(":memory:")
    assert db.migrate(conn) == [1, 2, 3, 4, 5, 6, 7]
    assert db.migrate(conn) == []
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    assert [r["version"] for r in conn.execute("SELECT version FROM schema_version")] == [1, 2, 3, 4, 5, 6, 7]


def test_connect_pragmas_and_row_factory():
    conn = mem()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('A', 'a')")
    assert conn.execute("SELECT name FROM artist").fetchone()["name"] == "A"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO release(artist_id, title, norm_title, type) VALUES (99, 't', 't', 'album')")


def test_connect_file_creates_parent_and_uses_wal(tmp_path):
    path = tmp_path / "nested" / "dir" / "c.db"
    conn = db.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert path.exists()


def test_each_migration_is_one_transaction(tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a(x);")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b(x); CREATE TABLE a(x);")  # second stmt fails
    conn = db.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn, tmp_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in names and "b" not in names
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version")] == [1]
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b(x);")
    assert db.migrate(conn, tmp_path) == [2]


def test_migration_005_adds_a_nullable_num_volumes_to_an_existing_ledger(tmp_path):
    for f in db.MIGRATIONS_DIR.glob("00[1-4]_*.sql"):
        shutil.copy(f, tmp_path / f.name)
    conn = db.connect(":memory:")
    assert db.migrate(conn, tmp_path) == [1, 2, 3, 4]
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('A', 'a')")
    conn.execute("INSERT INTO release(artist_id, title, norm_title, type) VALUES (1, 'T', 't', 'album')")
    conn.commit()
    assert db.migrate(conn) == [5, 6, 7]   # 005 applies, then everything after it up to head
    assert conn.execute("SELECT num_volumes FROM release").fetchone()[0] is None
    conn.execute("UPDATE release SET num_volumes = 2")
    assert conn.execute("SELECT num_volumes FROM release").fetchone()[0] == 2


def test_log_event():
    conn = mem()
    db.log_event(conn, "match", "Will", "why not", {"n": 1})
    db.log_event(conn, "gate.pause", None, "no data")
    rows = conn.execute("SELECT * FROM event_log ORDER BY id").fetchall()
    assert (rows[0]["kind"], rows[0]["subject"], rows[0]["message"]) == ("match", "Will", "why not")
    assert json.loads(rows[0]["data_json"]) == {"n": 1}
    assert rows[1]["subject"] is None and rows[1]["data_json"] is None


def test_settings_roundtrip_and_overwrite():
    conn = mem()
    assert db.get_setting(conn, "k") is None
    db.set_setting(conn, "k", "v1")
    db.set_setting(conn, "k", "v2")
    assert db.get_setting(conn, "k") == "v2"
    assert conn.execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 1
