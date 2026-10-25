import json
from types import SimpleNamespace as NS

import pytest
import tidalapi.exceptions

from cadence import cli
from cadence.catalog.candidates import (format_result, library_albums, parse_candidates, pending_artists,
                                        run_candidates, score_candidates)
from cadence.identity.normalize import norm_artist
from cadence.ledger import db
from cadence.sources import tidal
from cadence.sources.base import ArtistRef
from tests.fakes import FakeCatalog, FakeSession, make_gate, raw_album, raw_artist, rel
from tests.test_cli import env, never_login, seed  # noqa: F401  (env is a fixture)

CANDS = [{"id": 11, "name": "Arsenal"}, {"id": 12, "name": "Arsenal"}, {"id": 13, "name": "Arsenal"}]
A, B, C = (ArtistRef(i, "Arsenal") for i in (11, 12, 13))

# What the user owns: three albums, 6 files. Only profile 12 has Alpha and Beta; 13 has just Gamma.
LIBRARY = [("Alpha (Deluxe Edition)", 3), ("Beta", 2), ("Gamma", 1)]
RELEASES = {
    11: [rel(100, "Zeta", [A], "album", "1999-01-01"), rel(101, "Eta", [A], "album", "2002-02-02")],
    12: [rel(110, "Alpha", [B], "album", "2001-05-01"), rel(111, "Beta (Remastered 2011)", [B], "album", "2003-01-01"),
         rel(112, "Delta", [B], "single", "2010-07-07")],
    13: [rel(120, "Gamma", [C], "album", "2005-05-05")],
}


def mem():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def add_artist(conn, name="Arsenal", candidates=CANDS, library=LIBRARY, **cols):
    cur = conn.execute("INSERT INTO artist(name, norm_name, tidal_match, tidal_candidates) "
                       "VALUES (?, ?, 'ambiguous', ?)", (name, norm_artist(name),
                       candidates if isinstance(candidates, str) or candidates is None else json.dumps(candidates)))
    for album, n in library:
        for i in range(n):
            conn.execute("INSERT INTO file(path, artist_id, tag_album) VALUES (?, ?, ?)",
                         (f"{name}/{album}/{i}.flac", cur.lastrowid, album))
    conn.commit()
    return conn.execute("SELECT * FROM artist WHERE id = ?", (cur.lastrowid,)).fetchone()


def snapshot(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("artist", "file", "release", "recording", "track", "event_log", "want")}


def catalog(releases=RELEASES, **kw):
    return FakeCatalog(make_gate(), releases=releases, **kw)


def test_library_albums_folds_tag_variants_and_counts_files():
    conn = mem()
    a = add_artist(conn, library=[("Alpha (Deluxe Edition)", 3), ("Alpha", 1), ("Beta (2011 Remaster)", 2)])
    conn.execute("INSERT INTO file(path, artist_id, tag_album) VALUES ('x', ?, NULL), ('y', ?, '  ')",
                 (a["id"], a["id"]))
    other = add_artist(conn, "Someone Else", library=[("Alpha", 9)])
    lib = library_albums(conn, a["id"])
    assert {k: (v.title, v.files) for k, v in lib.items()} == {"alpha": ("Alpha (Deluxe Edition)", 4),
                                                              "beta": ("Beta (2011 Remaster)", 2)}
    assert library_albums(conn, other["id"])["alpha"].files == 9


def test_recommends_the_candidate_whose_releases_match_the_library():
    conn = mem()
    art = add_artist(conn)
    before = snapshot(conn)
    cat = catalog()
    res = score_candidates(conn, cat, art)
    assert res["recommendation"] == 12 and res["stopped"] is None and res["skipped"] is None
    assert [c["id"] for c in res["candidates"]] == [12, 13, 11]              # best first
    b = res["candidates"][0]
    assert (b["releases"], b["matched_library_albums"], b["matched_files"]) == (3, 2, 5)
    assert b["matched_titles"] == ["Alpha (Deluxe Edition)", "Beta"]
    assert b["sample_titles"] == ["Delta", "Beta (Remastered 2011)", "Alpha"]   # newest first
    assert (b["first_year"], b["last_year"]) == (2001, 2010)
    assert b["url"] == "https://tidal.com/artist/12"
    assert res["candidates"][2]["matched_files"] == 0
    assert cat.track_calls == []                                             # releases only, never tracks
    assert snapshot(conn) == before                                          # nothing applied automatically


def test_output_has_the_exact_set_tidal_command_and_candidate_lines():
    conn = mem()
    text = format_result(score_candidates(conn, catalog(), add_artist(conn)))
    lines = text.splitlines()
    assert lines[0] == "Arsenal: library has 3 album(s), 6 file(s)"
    assert lines[1] == '  library: "Alpha (Deluxe Edition)" (3), "Beta" (2), "Gamma" (1)'
    assert lines[2] == ('  [id 12] Arsenal  releases=3  library-albums matched 2/3 (files 5/6)  '
                        'e.g. "Alpha (Deluxe Edition)", "Beta"  https://tidal.com/artist/12')
    assert 'e.g. "Eta", "Zeta"' in lines[4]                                 # no match: newest titles instead
    assert lines[-1] == '  -> suggest: cadence artist set-tidal "Arsenal" 12'


def test_set_tidal_command_escapes_shell_characters_in_the_name():
    conn = mem()
    art = add_artist(conn, 'Say "Hi" $x', candidates=[{"id": 12, "name": "x"}])
    text = format_result(score_candidates(conn, catalog(), art))
    assert text.splitlines()[-1] == '  -> suggest: cadence artist set-tidal "Say \\"Hi\\" \\$x" 12'


def test_no_match_gives_no_recommendation():
    conn = mem()
    art = add_artist(conn, library=[("Unrelated", 4)])
    res = score_candidates(conn, catalog(), art)
    assert res["recommendation"] is None and all(c["matched_files"] == 0 for c in res["candidates"])
    assert format_result(res).splitlines()[-1] == "  -> no clear winner, check the links"


def test_no_library_albums_gives_no_recommendation():
    conn = mem()
    res = score_candidates(conn, catalog(), add_artist(conn, library=[]))
    assert res["recommendation"] is None and len(res["candidates"]) == 3


def test_tie_gives_no_recommendation():
    conn = mem()
    both = {11: [rel(1, "Alpha", [A])], 12: [rel(2, "Alpha", [B])]}
    res = score_candidates(conn, catalog(both), add_artist(conn, candidates=CANDS[:2], library=[("Alpha", 3)]))
    assert [c["matched_files"] for c in res["candidates"]] == [3, 3]
    assert res["recommendation"] is None


def test_winner_needs_a_lead_of_two_files_or_one_album():
    one_each = {11: [rel(1, "Alpha", [A])], 12: [rel(2, "Beta", [B])]}
    conn = mem()
    art = add_artist(conn, candidates=CANDS[:2], library=[("Alpha", 3), ("Beta", 2)])
    assert score_candidates(conn, catalog(one_each), art)["recommendation"] is None     # 3 vs 2 files, 1 album each

    conn = mem()
    art = add_artist(conn, candidates=CANDS[:2], library=[("Alpha", 4), ("Beta", 2)])
    assert score_candidates(conn, catalog(one_each), art)["recommendation"] == 11       # 2 files ahead

    conn = mem()
    art = add_artist(conn, candidates=CANDS[:2], library=[("Alpha", 3), ("Beta", 2), ("Gamma", 1)])
    two_vs_one = {11: [rel(1, "Alpha", [A]), rel(3, "Gamma", [A])], 12: [rel(2, "Beta", [B])]}
    assert score_candidates(conn, catalog(two_vs_one), art)["recommendation"] == 11     # 4 vs 2 files

    conn = mem()
    art = add_artist(conn, candidates=CANDS[:2], library=[("Alpha", 1), ("Beta", 1), ("Gamma", 1)])
    lead_by_album = {11: [rel(1, "Alpha", [A]), rel(3, "Gamma", [A])], 12: [rel(2, "Beta", [B])]}
    assert score_candidates(conn, catalog(lead_by_album), art)["recommendation"] == 11  # 2 vs 1: 1 album ahead


def test_single_candidate_with_a_match_is_recommended():
    conn = mem()
    art = add_artist(conn, candidates=CANDS[:1], library=[("Alpha", 1)])
    assert score_candidates(conn, catalog({11: [rel(1, "Alpha", [A])]}), art)["recommendation"] == 11


@pytest.mark.parametrize("raw", [None, "", "[]", "not json", '{"id": 1}', '[{"name": "x"}]', '[1, "a", null]'])
def test_empty_or_malformed_candidates_are_skipped_with_a_reason(raw):
    conn = mem()
    cat = catalog()
    res = score_candidates(conn, cat, add_artist(conn, candidates=raw))
    assert res["skipped"] and res["candidates"] == [] and res["recommendation"] is None
    assert cat.gate.calls == 0
    assert "skipped:" in format_result(res)


def test_parse_candidates_drops_bad_entries_and_repeated_ids():
    raw = json.dumps([{"id": 5, "name": "a"}, {"id": "6"}, {"name": "none"}, {"id": 5, "name": "again"}, "x"])
    assert parse_candidates(raw) == [{"id": 5, "name": "a"}, {"id": 6, "name": ""}]


def test_max_candidates_limits_calls_and_says_so():
    conn = mem()
    cat = catalog()
    res = score_candidates(conn, cat, add_artist(conn), max_candidates=2)
    assert cat.gate.calls == 2 and len(res["candidates"]) == 2 and res["truncated"] == 1
    assert "1 more candidate(s) not checked" in format_result(res)


def test_gate_pause_midway_returns_partial_and_stopped():
    conn = mem()
    art = add_artist(conn)
    before = snapshot(conn)
    res = score_candidates(conn, catalog(trip_on_releases=12), art)
    assert res["stopped"] == "paused" and res["until"]
    assert [c["id"] for c in res["candidates"]] == [11]                      # 12 tripped, 13 never asked
    assert res["recommendation"] is None
    assert "incomplete (stopped=paused)" in format_result(res)
    assert snapshot(conn) == before


def test_budget_stop_returns_partial_and_stopped():
    conn = mem()
    cat = catalog()
    cat.max_calls = 2
    res = score_candidates(conn, cat, add_artist(conn))
    assert res["stopped"] == "budget" and len(res["candidates"]) == 2 and res["recommendation"] is None


def test_candidate_error_is_reported_and_blocks_a_suggestion():
    conn = mem()
    res = score_candidates(conn, catalog(fail_on_releases={13: RuntimeError("boom")}), add_artist(conn))
    assert res["stopped"] is None and res["recommendation"] is None
    assert [c["error"] for c in res["candidates"] if c["error"]] == ["RuntimeError: boom"]
    assert "-> some candidates failed to load, no suggestion" in format_result(res)


def test_auth_and_network_errors_stop_the_run():
    import requests
    err = requests.HTTPError("401 Client Error")
    err.response = NS(status_code=401)
    conn = mem()
    art = add_artist(conn)
    assert score_candidates(conn, catalog(fail_on_releases={11: err}), art)["stopped"] == "auth"
    res = score_candidates(conn, catalog(fail_on_releases={12: requests.ConnectionError("down")}), art)
    assert res["stopped"] == "network" and [c["id"] for c in res["candidates"]] == [11, 12]


def test_winner_already_owned_by_another_artist_is_not_suggested():
    conn = mem()
    art = add_artist(conn)
    other = add_artist(conn, "Arsenal UK", candidates=None, library=[])
    conn.execute("UPDATE artist SET tidal_id = 12 WHERE id = ?", (other["id"],))
    conn.commit()
    res = score_candidates(conn, catalog(), art)
    assert res["recommendation"] is None and "belongs to 'Arsenal UK'" in res["note"]
    text = format_result(res)
    assert "(id already used by 'Arsenal UK')" in text and "set-tidal" not in text


def test_run_candidates_ends_at_the_first_stop_and_reports_it():
    conn = mem()
    add_artist(conn)
    add_artist(conn, "Seatbelts", candidates=[{"id": 21, "name": "Seatbelts"}], library=[("Alpha", 2)])
    rows, unknown, other = pending_artists(conn, None)
    assert [r["name"] for r in rows] == ["Arsenal", "Seatbelts"] and unknown == [] and other == []
    seen = []
    out = run_candidates(conn, catalog(trip_on_releases=11), rows, progress=seen.append)
    assert out["stopped"] == "paused" and out["until"] and len(out["artists"]) == 1 and len(seen) == 1


def test_pending_artists_only_takes_ambiguous_ones():
    conn = mem()
    add_artist(conn)
    conn.execute("INSERT INTO artist(name, norm_name, tidal_match) VALUES ('Fine', 'fine', 'auto')")
    rows, unknown, other = pending_artists(conn, ["Fine", "Arsenal", "Ghost"])
    assert [r["name"] for r in rows] == ["Arsenal"]
    assert [r["name"] for r in other] == ["Fine"] and unknown == ["Ghost"]
    assert [r["name"] for r in pending_artists(conn, [])[0]] == ["Arsenal"]


# ---------------------------------------------------------------- CLI

def seed_arsenal(env_dir, candidates=CANDS, library=LIBRARY, **cols):
    seed(env_dir, "Arsenal", tidal_match="ambiguous", tidal_candidates=json.dumps(candidates), **cols)
    conn = db.connect(env_dir / "c.db")
    aid = conn.execute("SELECT id FROM artist WHERE name = 'Arsenal'").fetchone()[0]
    for album, n in library:
        for i in range(n):
            conn.execute("INSERT INTO file(path, artist_id, tag_album) VALUES (?, ?, ?)",
                         (f"{album}/{i}.flac", aid, album))
    conn.commit()
    conn.close()


def pages():
    def album(i, title, owner, date):
        return raw_album(i, title, releaseDate=date, artists=[raw_artist(owner, "Arsenal")])
    return {("artists/11/albums", None, 0): [album(100, "Zeta", 11, "1999-01-01")],
            ("artists/12/albums", None, 0): [album(110, "Alpha", 12, "2001-05-01"),
                                              album(111, "Beta (Remastered 2011)", 12, "2003-01-01")],
            ("artists/13/albums", None, 0): [album(120, "Gamma", 13, "2005-05-05")]}


def ledger_dump(env_dir):
    conn = db.connect(env_dir / "c.db")
    try:
        return snapshot(conn)
    finally:
        conn.close()


def test_cli_without_ambiguous_artists_never_logs_in(env, monkeypatch, capsys):
    seed(env, "Fine", tidal_id=1, tidal_match="auto")
    never_login(monkeypatch)
    assert cli.main(["candidates"]) == 0
    assert cli.main(["candidates", "--artist", "Fine", "--artist", "Ghost"]) == 0
    out = capsys.readouterr().out
    assert out.count("no ambiguous artists to compare; not contacting Tidal") == 2
    assert "Fine: not ambiguous (tidal_match=auto); skipped" in out and "Ghost: not in the ledger" in out
    assert cli.main(["candidates", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"artists": [], "notes": [], "stopped": None}


def test_cli_prints_evidence_and_the_command_and_changes_nothing(env, monkeypatch, capsys):
    seed_arsenal(env)
    before = ledger_dump(env)
    session = FakeSession(pages())
    monkeypatch.setattr(tidal, "load_session", lambda path: session)
    assert cli.main(["candidates", "--interval", "0.01"]) == 0
    out = capsys.readouterr().out
    assert '  -> suggest: cadence artist set-tidal "Arsenal" 12' in out
    assert "[id 12] Arsenal  releases=2  library-albums matched 2/3 (files 5/6)" in out
    assert "candidates: 1 artist(s), 1 with a suggestion" in out and "stopped=None" in out
    assert {p.split("/")[0] + "/" + p.split("/")[1] for p, _, _ in session.requests} == \
        {"artists/11", "artists/12", "artists/13"}                           # release listings only
    assert ledger_dump(env) == before

    session.requests.clear()                                                 # second run is served from the cache
    assert cli.main(["candidates", "--artist", "arsenal", "--interval", "0.01"]) == 0
    assert session.requests == []


def test_cli_json_output(env, monkeypatch, capsys):
    seed_arsenal(env)
    monkeypatch.setattr(tidal, "load_session", lambda path: FakeSession(pages()))
    assert cli.main(["candidates", "--json", "--interval", "0.01"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stopped"] is None and data["artists"][0]["recommendation"] == 12
    assert data["artists"][0]["candidates"][0]["url"] == "https://tidal.com/artist/12"


def test_cli_gate_pause_midway_exits_3_with_partial_output(env, monkeypatch, capsys):
    seed_arsenal(env)

    class Session(FakeSession):
        def _map_request(self, path, params, parse=None):
            if path == "artists/12/albums":
                raise tidalapi.exceptions.TooManyRequests("slow down", retry_after=300)
            return super()._map_request(path, params, parse)

    session = Session(pages())
    session.request.map_request = session._map_request
    monkeypatch.setattr(tidal, "load_session", lambda path: session)
    assert cli.main(["candidates", "--interval", "0.01"]) == cli.EXIT_PAUSED
    out = capsys.readouterr().out
    assert "[id 11]" in out and "[id 13]" not in out and "stopped=paused" in out and "set-tidal" not in out
    assert "gate paused until" in out


def test_cli_does_not_touch_tidal_while_the_gate_is_paused(env, monkeypatch, capsys):
    seed_arsenal(env)
    conn = db.connect(env / "c.db")
    db.set_setting(conn, "gate.tidal.paused_until", "2999-01-01T00:00:00+00:00")
    conn.commit()
    conn.close()
    never_login(monkeypatch)
    assert cli.main(["candidates"]) == cli.EXIT_PAUSED
    assert "gate paused until" in capsys.readouterr().out


def test_cli_rejected_login_exits_1(env, monkeypatch, capsys):
    seed_arsenal(env)
    monkeypatch.setattr(tidal, "load_session", lambda path: (_ for _ in ()).throw(RuntimeError("rejected")))
    assert cli.main(["candidates"]) == 1
    assert "candidates: cannot start Tidal session: rejected" in capsys.readouterr().err
