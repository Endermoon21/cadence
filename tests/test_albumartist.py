import pytest

from cadence import cli
from cadence.ledger import db
from cadence.tags.albumartist import audit_albumartist, format_audit, primary_album_artist
from tests.test_cli import env  # noqa: F401  (fixture)

KNOWN = {"tv girl", "taylor swift", "kendrick lamar"}


@pytest.mark.parametrize("value, expected", [
    ("TV Girl, George Clanton", "TV Girl"),
    ("Taylor Swift & Ed Sheeran", "Taylor Swift"),
    ("Taylor Swift and Ed Sheeran", "Taylor Swift"),
    ("Kendrick Lamar x SZA", "Kendrick Lamar"),
    ("Kendrick Lamar, Taylor Swift & Others", "Kendrick Lamar"),
    ("Anyone feat. Somebody", "Anyone"),
    ("Anyone ft. Somebody", "Anyone"),
    ("Anyone (feat. Somebody)", "Anyone"),
    ("Anyone with Somebody", "Anyone"),
    ("Unknown One, Unknown Two", "Unknown One, Unknown Two"),     # first part unknown: unsure, keep
    ("Earth, Wind & Fire", "Earth, Wind & Fire"),
    ("Idris Ackamoor & The Pyramids", "Idris Ackamoor & The Pyramids"),
    ("Simon & Garfunkel", "Simon & Garfunkel"),
    ("Tyler, The Creator", "Tyler, The Creator"),
    ("Crosby, Stills, Nash & Young", "Crosby, Stills, Nash & Young"),
    ("Sleeping With Sirens", "Sleeping With Sirens"),
    ("TV Girl", "TV Girl"),
    ("", ""),
])
def test_primary_album_artist(value, expected):
    assert primary_album_artist(value, KNOWN) == expected


def test_a_known_single_artist_is_kept_whole_even_if_its_first_part_is_known():
    known = KNOWN | {"crosby stills nash and young", "crosby", "earth wind and fire"}
    assert primary_album_artist("Crosby, Stills, Nash & Young", known) == "Crosby, Stills, Nash & Young"
    assert primary_album_artist("Crosby, Someone", known) == "Crosby"
    assert primary_album_artist("Earth, Wind & Fire", known) == "Earth, Wind & Fire"


def test_no_known_artists_only_splits_on_feat_and_with():
    assert primary_album_artist("TV Girl, George Clanton") == "TV Girl, George Clanton"
    assert primary_album_artist("A & B ft. C") == "A & B"


def ledger(conn):
    for name, tm in [("TV Girl", "auto"), ("Earth, Wind & Fire", "auto"), ("TV Girl, George Clanton", "none"),
                     ("Taylor Swift", "auto"), ("Various Artists", "none")]:
        conn.execute("INSERT INTO artist(name, norm_name, tidal_match) VALUES (?, ?, ?)", (name, name.lower(), tm))
    for i, (aid, tag) in enumerate([(1, "TV Girl"), (2, "Earth, Wind & Fire"), (2, "Earth, Wind & Fire"),
                                    (3, "TV Girl, George Clanton"), (3, "TV Girl, George Clanton"),
                                    (4, "Taylor Swift feat. Zayn"), (5, "Various Artists"), (4, None), (4, " ")]):
        conn.execute("INSERT INTO file(path, artist_id, tag_albumartist) VALUES (?, ?, ?)", (f"f{i}", aid, tag))
    conn.commit()


def dump(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM file ORDER BY id")], \
           [tuple(r) for r in conn.execute("SELECT * FROM artist ORDER BY id")]


def test_audit_counts_per_artist_and_changes_nothing():
    conn = db.connect(":memory:")
    db.migrate(conn)
    ledger(conn)
    before = dump(conn)
    a = audit_albumartist(conn)
    assert (a["files_with_tag"], a["would_change"]) == (7, 3)
    assert a["per_artist"] == [("TV Girl, George Clanton", 2), ("Taylor Swift", 1)]
    assert a["examples"] == [("TV Girl, George Clanton", "TV Girl", 2), ("Taylor Swift feat. Zayn", "Taylor Swift", 1)]
    assert "would change (nothing written)" in format_audit(a)
    assert dump(conn) == before


def test_audit_shows_at_most_20_examples():
    conn = db.connect(":memory:")
    db.migrate(conn)
    conn.execute("INSERT INTO artist(name, norm_name, tidal_match) VALUES ('Solo', 'solo', 'auto')")
    for i in range(30):
        conn.execute("INSERT INTO file(path, artist_id, tag_albumartist) VALUES (?, 1, ?)", (f"f{i}", f"Solo feat. X{i}"))
    assert len(audit_albumartist(conn)["examples"]) == 20


def test_cli_audit_tags(env, capsys):  # noqa: F811
    conn = db.connect(env / "c.db")
    db.migrate(conn)
    ledger(conn)
    before = dump(conn)
    conn.close()
    assert cli.main(["audit-tags"]) == 0
    out = capsys.readouterr().out
    assert '"TV Girl, George Clanton" -> "TV Girl"  (2 file(s))' in out
    conn = db.connect(env / "c.db")
    assert dump(conn) == before
