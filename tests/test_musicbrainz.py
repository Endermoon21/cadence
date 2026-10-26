import json
import urllib.parse

import pytest

from cadence import cli
from cadence.catalog.candidates import format_result, score_candidates
from cadence.sources import tidal
from cadence.sources.musicbrainz import MAX_PAGES, USER_AGENT, MusicBrainz
from tests.fakes import FakeSession, FakeTime, raw_album, raw_artist, rel
from tests.test_candidates import A, B, CANDS, add_artist, catalog, mem, seed_arsenal
from tests.test_cli import env  # noqa: F401  (fixture)

MBID = "11111111-1111-1111-1111-111111111111"


class FakeOpener:
    """Stands in for urlopen: serves canned JSON by path, records every request. Never touches the network."""

    def __init__(self, artists=None, groups=(), fail=None):
        self.artists = artists if artists is not None else [{"id": MBID, "name": "Arsenal", "score": 100}]
        self.groups = list(groups)
        self.fail = fail
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.fail:
            raise self.fail
        url = urllib.parse.urlparse(req.full_url)
        q = urllib.parse.parse_qs(url.query)
        assert q["fmt"] == ["json"]
        if url.path == "/ws/2/artist/":
            body = {"artists": self.artists}
        else:
            assert url.path == "/ws/2/release-group" and q["artist"] == [MBID]
            off, lim = int(q["offset"][0]), int(q["limit"][0])
            body = {"release-group-count": len(self.groups), "release-group-offset": off,
                    "release-groups": [{"title": t} for t in self.groups[off:off + lim]]}
        return _Resp(json.dumps(body).encode())


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


def client(opener, conn=None, t=None):
    t = t or FakeTime()
    return MusicBrainz(conn, opener=opener, sleep=t.sleep, clock=t.clock)


# ---------------------------------------------------------------- client

def test_sends_the_contact_user_agent_and_a_timeout():
    op = FakeOpener()
    client(op).search_artists("Arsenal")
    req, timeout = op.requests[0]
    assert req.get_header("User-agent") == USER_AGENT == \
        "Cadence/0.1 (personal use; russell.alexander.bennett@gmail.com)"
    assert timeout == 20
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
    assert q["query"] == ['artist:"Arsenal"'] and req.full_url.startswith("https://musicbrainz.org/ws/2/artist/?")


def test_search_returns_mbid_name_disambiguation():
    op = FakeOpener(artists=[{"id": MBID, "name": "Arsenal", "disambiguation": "UK band", "score": 100}])
    assert client(op).search_artists("Arsenal") == [
        {"mbid": MBID, "name": "Arsenal", "disambiguation": "UK band", "score": 100}]


def test_requests_are_paced_at_least_1_1_seconds_apart():
    t, op = FakeTime(), FakeOpener(groups=["a"] * 250)
    mb = client(op, t=t)
    mb.search_artists("Arsenal")
    mb.release_group_titles(MBID)                       # 3 pages
    assert len(op.requests) == 4 and len(t.sleeps) == 3
    assert all(s >= 1.1 - 1e-9 for s in t.sleeps)


def test_release_groups_paginate_and_stop_at_the_cap():
    op = FakeOpener(groups=[f"T{i}" for i in range(250)])
    assert client(op).release_group_titles(MBID) == {f"T{i}" for i in range(250)}
    assert len(op.requests) == 3
    op = FakeOpener(groups=[f"T{i}" for i in range(100 * (MAX_PAGES + 5))])
    assert len(client(op).release_group_titles(MBID)) == 100 * MAX_PAGES
    assert len(op.requests) == MAX_PAGES


def test_cache_hit_makes_no_request():
    conn = mem()
    op = FakeOpener(groups=["Alpha", "Beta"])
    first = client(op, conn)
    assert first.search_artists("Arsenal")[0]["mbid"] == MBID and first.release_group_titles(MBID) == {"Alpha", "Beta"}
    assert len(op.requests) == 2
    keys = {r[0] for r in conn.execute("SELECT key FROM catalog_cache")}
    assert keys == {"mb:artist:arsenal", f"mb:rg:{MBID}"}
    again = FakeOpener()
    second = client(again, conn)
    assert second.search_artists("ARSENAL")[0]["mbid"] == MBID and second.release_group_titles(MBID) == {"Alpha", "Beta"}
    assert again.requests == []


# ---------------------------------------------------------------- tiebreak

# Both profiles own "Alpha" (the one album we have, 3 files): a library tie. Only profile 12 also has Beta and Delta.
TIED = {11: [rel(1, "Alpha", [A], "album"), rel(2, "Zeta", [A], "album"), rel(3, "Eta", [A], "album")],
        12: [rel(4, "Alpha", [B], "album"), rel(5, "Beta (Remastered 2011)", [B], "album"), rel(6, "Delta", [B], "album")]}


def tied_artist(conn):
    return add_artist(conn, candidates=CANDS[:2], library=[("Alpha", 3)])


def test_musicbrainz_breaks_a_library_tie():
    conn = mem()
    op = FakeOpener(groups=["Alpha", "Beta", "Delta", "Other"])
    res = score_candidates(conn, catalog(TIED), tied_artist(conn), mb=client(op, conn))
    assert [c["matched_files"] for c in res["candidates"]] == [3, 3]
    assert res["recommendation"] == 12
    assert {c["id"]: (c["mb_overlap"], c["mb_total"]) for c in res["candidates"]} == {11: (1, 3), 12: (3, 3)}
    text = format_result(res)
    assert "mb-overlap 3/3" in text and "mb-overlap 1/3" in text
    assert "musicbrainz: Arsenal (" in text and text.splitlines()[-1] == \
        '  -> suggest: cadence artist set-tidal "Arsenal" 12 (musicbrainz tiebreak)'


def test_without_mb_the_same_tie_has_no_winner():
    conn = mem()
    res = score_candidates(conn, catalog(TIED), tied_artist(conn))
    assert res["recommendation"] is None and res["mb"] is None and "mb-overlap" not in format_result(res)


def test_a_clear_library_winner_is_never_overridden_or_even_questioned():
    conn = mem()
    op = FakeOpener(groups=["Zeta", "Eta"])             # MusicBrainz would favour profile 11
    res = score_candidates(conn, catalog(), add_artist(conn), mb=client(op, conn))
    assert res["recommendation"] == 12 and res["mb"] is None
    assert op.requests == []


def test_a_weak_library_lead_is_not_overridden_either():
    conn = mem()
    one_each = {11: [rel(1, "Alpha", [A], "album"), rel(2, "Gamma", [A], "album")],
                12: [rel(3, "Beta", [B], "album"), rel(4, "Delta", [B], "album"), rel(5, "Eps", [B], "album")]}
    art = add_artist(conn, candidates=CANDS[:2], library=[("Alpha", 3), ("Beta", 2)])   # 3 vs 2 files: no clear winner
    op = FakeOpener(groups=["Beta", "Delta", "Eps"])
    res = score_candidates(conn, catalog(one_each), art, mb=client(op, conn))
    assert res["recommendation"] is None
    assert {c["id"]: c["mb_overlap"] for c in res["candidates"]} == {11: 0, 12: 3}      # shown, but not acted on


def test_no_exact_artist_match_skips_musicbrainz_and_says_so():
    conn = mem()
    op = FakeOpener(artists=[{"id": MBID, "name": "Arsenal Fan Club", "score": 100}])
    res = score_candidates(conn, catalog(TIED), tied_artist(conn), mb=client(op, conn))
    assert res["recommendation"] is None and res["mb"]["skipped"] and all("mb_overlap" not in c for c in res["candidates"])
    assert len(op.requests) == 1                                     # the artist search only
    assert "musicbrainz: skipped, no exact artist name match" in format_result(res)


def test_the_exact_match_with_the_highest_score_is_used():
    conn = mem()
    other = "22222222-2222-2222-2222-222222222222"
    op = FakeOpener(artists=[{"id": other, "name": "ARSENAL", "score": 80},
                             {"id": MBID, "name": "Arsénal", "score": 95},
                             {"id": "x", "name": "Arsenal Two", "score": 100}], groups=["Alpha", "Beta", "Delta"])
    res = score_candidates(conn, catalog(TIED), tied_artist(conn), mb=client(op, conn))
    assert res["mb"]["mbid"] == MBID and res["recommendation"] == 12


def test_a_musicbrainz_failure_only_skips_the_tiebreak():
    conn = mem()
    res = score_candidates(conn, catalog(TIED), tied_artist(conn), mb=client(FakeOpener(fail=OSError("down")), conn))
    assert res["recommendation"] is None and "OSError" in res["mb"]["skipped"]


def test_not_enough_musicbrainz_evidence_gives_no_winner():
    conn = mem()
    res = score_candidates(conn, catalog(TIED), tied_artist(conn), mb=client(FakeOpener(groups=["Alpha", "Beta"]), conn))
    assert res["recommendation"] is None            # 12 shares 2 titles but leads 11 (1 title) by only 1


# ---------------------------------------------------------------- CLI

def test_cli_flag_is_off_by_default_and_wires_the_tiebreak(env, monkeypatch, capsys):  # noqa: F811
    seed_arsenal(env, candidates=CANDS[:2], library=[("Alpha", 3)])

    def album(i, title, owner):
        return raw_album(i, title, releaseDate="2001-01-01", artists=[raw_artist(owner, "Arsenal")])
    session = FakeSession({("artists/11/albums", None, 0): [album(1, "Alpha", 11), album(2, "Zeta", 11), album(3, "Eta", 11)],
                           ("artists/12/albums", None, 0): [album(4, "Alpha", 12), album(5, "Beta", 12), album(6, "Delta", 12)]})
    monkeypatch.setattr(tidal, "load_session", lambda path: session)
    built = []
    op = FakeOpener(groups=["Alpha", "Beta", "Delta"])
    monkeypatch.setattr(cli, "MusicBrainz", lambda conn: built.append(1) or MusicBrainz(conn, opener=op,
                        sleep=lambda s: None))
    assert cli.main(["candidates", "--interval", "0.01"]) == 0
    out = capsys.readouterr().out
    assert built == [] and op.requests == [] and "no clear winner" in out and "mb-overlap" not in out
    assert cli.main(["candidates", "--musicbrainz", "--interval", "0.01"]) == 0
    out = capsys.readouterr().out
    assert built == [1] and "mb-overlap 3/3" in out
    assert 'set-tidal "Arsenal" 12 (musicbrainz tiebreak)' in out
