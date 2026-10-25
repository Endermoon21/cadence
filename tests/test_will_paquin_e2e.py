"""Acceptance test from docs/M1_SPEC.md: ledger -> legacy import -> catalog sync -> link -> report."""
import pytest

from cadence import report
from cadence.catalog.link import link_files
from cadence.catalog.sync import sync_all
from cadence.ledger import db
from cadence.ledger.legacy import import_scan
from tests.fakes import FIXTURES, fixture_catalog, make_gate


@pytest.fixture(scope="module")
def world():
    conn = db.connect(":memory:")
    db.migrate(conn)
    import_scan(conn, FIXTURES / "library_will_paquin.jsonl")
    catalog = fixture_catalog(make_gate())
    first = sync_all(conn, catalog, names=["Will Paquin"])
    calls_after_first = catalog.gate.calls
    link = link_files(conn)
    second = sync_all(conn, catalog, names=["Will Paquin"])
    will = conn.execute("SELECT id FROM artist WHERE name = 'Will Paquin'").fetchone()[0]
    return dict(conn=conn, catalog=catalog, first=first, second=second, link=link, will=will,
                calls_after_first=calls_after_first, rep=report.artist_report(conn, will))


def test_sync_resolved_will_and_synced_him(world):
    assert (world["first"]["synced"], world["first"]["stopped"]) == (1, None)
    row = world["conn"].execute("SELECT tidal_id, tidal_match FROM artist WHERE id = ?", (world["will"],)).fetchone()
    assert (row["tidal_id"], row["tidal_match"]) == (1, "auto")


def test_18_own_releases_and_2_appears_on(world):
    rep, conn = world["rep"], world["conn"]
    assert len(rep["releases"]) == 18
    assert rep["appears_on"] == 2
    appears = {r[0] for r in conn.execute("SELECT title FROM release WHERE role = 'appears_on'")}
    assert appears == {"COMEBACK", "Songs To Come Home To"}


def test_29_unique_recordings(world):
    rep = world["rep"]
    assert rep["recordings_total"] == 29
    assert world["conn"].execute("SELECT COUNT(*) FROM recording WHERE artist_id = ?",
                                 (world["will"],)).fetchone()[0] == 29


def test_nothing_missing_and_every_release_owned(world):
    rep = world["rep"]
    assert rep["missing_recordings"] == [], [m["title"] for m in rep["missing_recordings"]]
    not_owned = [(r["title"], r["date"], r["state"], f"{r['owned']}/{r['total']}")
                 for r in rep["releases"] if r["state"] != "owned"]
    assert not_owned == []


def test_all_30_will_files_matched_with_one_duplicate(world):
    conn, rep = world["conn"], world["rep"]
    assert conn.execute("SELECT COUNT(*) FROM file WHERE artist_id = ?", (world["will"],)).fetchone()[0] == 30
    assert rep["unmatched_files"] == [], [f["path"] for f in rep["unmatched_files"]]
    assert conn.execute("SELECT COUNT(*) FROM file WHERE artist_id = ? AND recording_id IS NOT NULL",
                        (world["will"],)).fetchone()[0] == 30
    assert len(rep["duplicates"]) == 1
    dup = rep["duplicates"][0]
    assert dup["title"] == "Hahaha" and len(dup["paths"]) == 2


def test_other_artist_files_stay_with_their_artist(world):
    conn, rep = world["conn"], world["rep"]
    others = conn.execute("SELECT f.path, a.name FROM file f JOIN artist a ON a.id = f.artist_id "
                          "WHERE a.id != ?", (world["will"],)).fetchall()
    assert len(others) == 6 and {o["name"] for o in others} == {"Her's"}
    mentioned = {p for d in rep["duplicates"] for p in d["paths"]} | {f["path"] for f in rep["unmatched_files"]}
    assert not mentioned & {o["path"] for o in others}
    assert all(f["recording_id"] is None for f in conn.execute(
        "SELECT recording_id FROM file WHERE artist_id != ?", (world["will"],)))


def test_second_sync_spends_no_gate_calls(world):
    assert world["second"]["skipped"] == 1 and world["second"]["synced"] == 0
    assert world["catalog"].gate.calls == world["calls_after_first"] > 0
