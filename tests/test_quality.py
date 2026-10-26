import json

import pytest

from cadence import cli, report
from cadence.ledger import db
from cadence.ledger.legacy import import_scan
from cadence.ledger.quality import backfill_quality, describe, is_lossy, quality_summary
from tests.test_cli import env  # noqa: F401  (fixture)
from tests.test_legacy import row, scan


def mem():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


@pytest.mark.parametrize("codec", ["aac", "mp4a.40.2", "MP4A.40.5", "mp3", "opus", "vorbis", "ogg", "Opus"])
def test_lossy_codecs(codec):
    assert is_lossy(codec)


@pytest.mark.parametrize("codec", ["flac", "FLAC", "alac", "pcm", "unknown", "", None])
def test_lossless_and_unknown_codecs_are_not_flagged_lossy(codec):
    assert not is_lossy(codec)


def test_describe():
    assert describe("flac", 44100, 16, None) == "FLAC 16/44.1"
    assert describe("flac", 48000, 24, None) == "FLAC 24/48"
    assert describe("flac", 192000, 24, None) == "FLAC 24/192"
    assert describe("flac", 88200, 24, None) == "FLAC 24/88.2"
    assert describe("alac", 44100, 16, None) == "ALAC 16/44.1"
    assert describe("flac", None, None, None) == "FLAC"
    assert describe("mp4a.40.2", 44100, 16, 320075) == "AAC 320"        # bit depth means nothing for lossy
    assert describe("mp4a.40.2", 44100, 16, 255999) == "AAC 256"
    assert describe("mp3", 44100, None, 320000) == "MP3 320"
    assert describe("opus", 48000, None, None) == "Opus"
    assert describe("weird", 44100, 16, None) == "" and describe(None, None, None, None) == ""


def mixed(conn):
    a = conn.execute("INSERT INTO artist(name, norm_name) VALUES ('A', 'a')").lastrowid
    b = conn.execute("INSERT INTO artist(name, norm_name) VALUES ('B', 'b')").lastrowid
    for path, artist, codec, sr, bits, br in [
            ("a1", a, "flac", 44100, 16, None), ("a2", a, "mp4a.40.2", 44100, 16, 256000),
            ("a3", a, "mp4a.40.2", 44100, 16, 320000), ("b1", b, "mp3", 44100, None, 192000),
            ("b2", b, "flac", 96000, 24, None), ("b3", b, None, None, None, None), ("b4", b, "weird", None, None, None)]:
        conn.execute("INSERT INTO file(path, artist_id, codec, sample_rate, bit_depth, bitrate) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (path, artist, codec, sr, bits, br))
    conn.commit()


def test_backfill_fills_known_codecs_only_and_is_idempotent():
    conn = mem()
    mixed(conn)
    assert backfill_quality(conn) == 5
    got = {r["path"]: (r["lossy"], r["quality_received"]) for r in conn.execute("SELECT * FROM file")}
    assert got == {"a1": (0, "FLAC 16/44.1"), "a2": (1, "AAC 256"), "a3": (1, "AAC 320"),
                   "b1": (1, "MP3 192"), "b2": (0, "FLAC 24/96"), "b3": (None, None), "b4": (None, None)}
    assert backfill_quality(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM file WHERE quality_requested IS NOT NULL").fetchone()[0] == 0


def test_summary_counts_and_top_lossy_artists():
    conn = mem()
    mixed(conn)
    backfill_quality(conn)
    assert quality_summary(conn) == {"total": 7, "lossless": 2, "lossy": 3, "unknown": 2,
                                     "top_lossy_artists": [("A", 2), ("B", 1)]}


def test_import_sets_quality_on_new_and_existing_rows_and_leaves_requested_alone(tmp_path):
    conn = mem()
    import_scan(conn, scan(tmp_path, row("x.flac"), row("y.m4a", codec="mp4a.40.2", br=256000)))
    q = {r["path"]: (r["lossy"], r["quality_received"], r["quality_requested"]) for r in conn.execute("SELECT * FROM file")}
    assert q == {"x.flac": (0, "FLAC 16/44.1", None), "y.m4a": (1, "AAC 256", None)}
    conn.execute("UPDATE file SET lossy = NULL, quality_received = 'stale'")
    import_scan(conn, scan(tmp_path, row("x.flac", sr=96000, bits=24), row("y.m4a", codec="mp4a.40.2", br=256000)))
    q = {r["path"]: (r["lossy"], r["quality_received"]) for r in conn.execute("SELECT * FROM file")}
    assert q == {"x.flac": (0, "FLAC 24/96"), "y.m4a": (1, "AAC 256")}


def test_cli_quality_prints_counts_and_top_artists(env, capsys):  # noqa: F811
    conn = db.connect(env / "c.db")
    db.migrate(conn)
    mixed(conn)
    conn.close()
    assert cli.main(["quality"]) == 0
    out = capsys.readouterr().out
    assert "quality: 5 file(s) filled in" in out
    assert "files: 7  lossless: 2  lossy: 3  unknown: 2" in out
    assert out.splitlines()[-2:] == ["      2  A", "      1  B"]
    assert cli.main(["quality"]) == 0
    assert "quality: nothing to fill in" in capsys.readouterr().out


def test_reports_show_lossy_counts_and_paths_only_when_present():
    conn = mem()
    mixed(conn)
    backfill_quality(conn)
    rows = {r["artist"]: r for r in report.library_summary(conn)}
    assert (rows["A"]["lossy"], rows["B"]["lossy"]) == (2, 1)
    assert "lossy" in report.format_summary(list(rows.values())).splitlines()[0]
    a = report.artist_report(conn, conn.execute("SELECT id FROM artist WHERE name = 'A'").fetchone()[0])
    assert a["lossy_files"] == ["a2", "a3"] and "lossy files (2):" in report.format_artist_report(a)
    conn.execute("UPDATE file SET lossy = 0")
    a = report.artist_report(conn, 1)
    assert "lossy_files" not in a
    json.dumps(a)
