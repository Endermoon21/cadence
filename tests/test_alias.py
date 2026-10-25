import json

from cadence import cli
from cadence.catalog.alias import add_alias, list_aliases, remove_alias
from cadence.ledger import db
from cadence.ledger.legacy import import_scan
from tests.test_legacy import row, scan


def mem():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def artist(conn, name, **cols) -> int:
    cur = conn.execute("INSERT INTO artist(name, norm_name) VALUES (?, ?)", (name, name.lower()))
    for k, v in cols.items():
        conn.execute(f"UPDATE artist SET {k} = ? WHERE id = ?", (v, cur.lastrowid))
    return cur.lastrowid


def file(conn, path, artist_id, albumartist, artist_tag="x", match="unmatched", recording_id=None):
    conn.execute("INSERT INTO file(path, artist_id, tag_albumartist, tag_artist, match, recording_id, match_score) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (path, artist_id, albumartist, artist_tag, match, recording_id, 0.9 if recording_id else None))


def state(conn, path):
    return conn.execute("SELECT artist_id, match, recording_id, match_score FROM file WHERE path = ?",
                        (path,)).fetchone()


def test_add_alias_moves_files_and_resets_stale_links_but_not_manual_ones():
    conn = mem()
    target, trio = artist(conn, "Bill Evans"), artist(conn, "Bill Evans Trio", tidal_match="ambiguous")
    conn.execute("INSERT INTO recording(artist_id, title, norm_title) VALUES (?, 'T', 't')", (trio,))
    file(conn, "a.flac", trio, "Bill Evans Trio", match="fuzzy", recording_id=1)
    file(conn, "m.flac", trio, "Bill Evans Trio", match="manual", recording_id=1)
    file(conn, "c.flac", trio, None, artist_tag="the bill evans trio!")       # tag_artist fallback, same norm
    file(conn, "v.flac", trio, "Various Artists", artist_tag="Bill Evans Trio")  # various: never assigned
    file(conn, "o.flac", target, "Bill Evans")
    r = add_alias(conn, "Bill Evans Trio", "bill evans", note="trio credit")
    assert (r["files"], r["reset"], r["artist"]) == (3, 2, "Bill Evans")
    assert tuple(state(conn, "a.flac")) == (target, "unmatched", None, None)
    assert tuple(state(conn, "m.flac")) == (target, "manual", 1, 0.9)
    assert state(conn, "c.flac")["artist_id"] == target
    assert state(conn, "v.flac")["artist_id"] == trio and state(conn, "o.flac")["artist_id"] == target
    ev = conn.execute("SELECT * FROM event_log WHERE kind = 'alias.add'").fetchone()
    assert ev["subject"] == "Bill Evans" and json.loads(ev["data_json"])["reset"] == 2
    assert add_alias(conn, "Bill Evans Trio", "Bill Evans")["reset"] == 0       # idempotent, upserts the row
    assert conn.execute("SELECT COUNT(*) FROM artist_alias").fetchone()[0] == 1


def test_add_alias_refuses_bad_targets_and_real_artists():
    conn = mem()
    artist(conn, "Bill Evans")
    artist(conn, "Real", tidal_id=7, tidal_match="auto")
    artist(conn, "Maybe", tidal_id=8, tidal_match="ambiguous")                    # provisional id only
    artist(conn, "Pinned", tidal_id=9, tidal_match="manual")
    assert "no artist 'Nobody'" in add_alias(conn, "X", "Nobody")["error"]
    assert "same name" in add_alias(conn, "THE bill evans", "Bill Evans")["error"]
    assert "real artist" in add_alias(conn, "Real", "Bill Evans")["error"]
    assert "real artist" in add_alias(conn, "Pinned", "Bill Evans")["error"]
    assert "usable name" in add_alias(conn, "  ", "Bill Evans")["error"]
    assert "error" not in add_alias(conn, "Maybe", "Bill Evans")                  # unresolved: fine
    assert [a["alias_norm"] for a in list_aliases(conn)] == ["maybe"]


def test_list_and_remove():
    conn = mem()
    artist(conn, "TV Girl")
    assert list_aliases(conn) == []
    assert "error" in remove_alias(conn, "nope")
    add_alias(conn, "TV Girl, George Clanton", "TV Girl", note="collab")
    [a] = list_aliases(conn)
    assert (a["alias_norm"], a["artist"], a["note"]) == ("tv girl george clanton", "TV Girl", "collab")
    assert remove_alias(conn, "TV Girl, George Clanton")["artist"] == "TV Girl"
    assert list_aliases(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'alias.remove'").fetchone()[0] == 1


def test_import_scan_resolves_aliases(tmp_path):
    conn = mem()
    target = artist(conn, "Bill Evans")
    add_alias(conn, "Bill Evans Trio", "Bill Evans")
    stats = import_scan(conn, scan(tmp_path, row("1.flac", albumartist="Bill Evans Trio"),
                                   row("2.flac", albumartist=None, artist="Bill Evans Trio")))
    assert stats["artists_created"] == 0
    assert [r["artist_id"] for r in conn.execute("SELECT artist_id FROM file")] == [target, target]
    assert conn.execute("SELECT COUNT(*) FROM artist").fetchone()[0] == 1


def test_reimport_after_an_alias_appears_resets_a_stale_link(tmp_path):
    conn = mem()
    import_scan(conn, scan(tmp_path, row("a.flac", albumartist="Trio")))
    target = artist(conn, "Bill Evans")
    conn.execute("INSERT INTO recording(artist_id, title, norm_title) VALUES (1, 'T', 't')")
    conn.execute("UPDATE file SET recording_id = 1, match = 'fuzzy', match_score = 0.8")
    conn.execute("INSERT INTO artist_alias(alias_norm, artist_id) VALUES ('trio', ?)", (target,))  # alias added later
    import_scan(conn, scan(tmp_path, row("a.flac", albumartist="Trio")))
    assert tuple(state(conn, "a.flac")) == (target, "unmatched", None, None)


def test_cli_alias_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CADENCE_DB", str(tmp_path / "c.db"))
    conn = db.connect(tmp_path / "c.db")
    db.migrate(conn)
    target = artist(conn, "TV Girl")
    file(conn, "a.flac", None, "TV Girl, George Clanton")
    conn.commit()
    conn.close()
    assert cli.main(["alias", "add", "TV Girl, George Clanton", "TV Girl", "--note", "collab"]) == 0
    assert "1 file(s) tagged so" in capsys.readouterr().out
    assert cli.main(["alias", "list"]) == 0
    assert "tv girl george clanton -> TV Girl  (collab)" in capsys.readouterr().out
    assert cli.main(["alias", "add", "X", "Nobody"]) == 1
    assert "no artist 'Nobody'" in capsys.readouterr().err
    conn = db.connect(tmp_path / "c.db")
    assert state(conn, "a.flac")["artist_id"] == target
    conn.close()
    assert cli.main(["alias", "remove", "TV Girl, George Clanton"]) == 0
    assert "import-legacy" in capsys.readouterr().out
    assert cli.main(["alias", "remove", "TV Girl, George Clanton"]) == 1
    capsys.readouterr()
    assert cli.main(["alias", "list"]) == 0
    assert "alias: none" in capsys.readouterr().out
