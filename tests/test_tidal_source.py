import json
from types import SimpleNamespace as NS

import pytest
import requests

from cadence.gate.bucket import GatePaused
from cadence.ledger import db
from cadence.sources import base, tidal
from cadence.sources.base import ArtistRef, BudgetExceeded, ReleaseDTO, TrackDTO
from cadence.sources.tidal import CatalogCache, TidalCatalog
from tests.fakes import FakeSession, TooManyRequests, make_gate, raw_album, raw_artist, raw_track

WILL = (ArtistRef(7, "Will"),)


def catalog(session, conn=None, max_calls=None):
    conn = conn or db.connect(":memory:")
    if not conn.execute("SELECT name FROM sqlite_master WHERE name='catalog_cache'").fetchone():
        db.migrate(conn)
    gate = make_gate()
    return TidalCatalog(session, gate, CatalogCache(conn), max_calls), gate, conn


def test_release_paging_filters_and_dedupe():
    s = FakeSession({
        ("artists/7/albums", None, 0): [raw_album(i) for i in range(1, 51)],       # a full page, total says 51
        ("artists/7/albums", None, 50): [raw_album(51)],
        ("artists/7/albums", "EPSANDSINGLES", 0): [raw_album(3, type="EP"), raw_album(60, type="SINGLE")],
        ("artists/7/albums", "COMPILATIONS", 0): [raw_album(60), raw_album(70, type="COMPILATION")],
    })
    cat, gate, _ = catalog(s)
    out = cat.artist_releases(7)
    assert [(p, q) for p, q, _ in s.requests] == [
        ("artists/7/albums", {"limit": 50, "offset": 0}),
        ("artists/7/albums", {"limit": 50, "offset": 50}),
        ("artists/7/albums", {"limit": 50, "offset": 0, "filter": "EPSANDSINGLES"}),
        ("artists/7/albums", {"limit": 50, "offset": 0, "filter": "COMPILATIONS"}),
    ]
    assert all(parse is None for *_, parse in s.requests)         # raw JSON: tidalapi's parsers are bypassed
    assert gate.calls == 4
    assert [r.tidal_id for r in out] == list(range(1, 52)) + [60, 70]     # deduped, first bucket wins
    assert next(r for r in out if r.tidal_id == 3).type == "album"        # not the EPSANDSINGLES copy
    assert next(r for r in out if r.tidal_id == 70).type == "compilation"


def test_paging_stops_at_total_number_of_items():
    s = FakeSession({("artists/7/albums", None, 0): [raw_album(i) for i in range(1, 51)]})   # total = 50
    cat, _, _ = catalog(s)
    assert len(cat.artist_releases(7)) == 50
    assert [q["offset"] for _, q, _ in s.requests if "filter" not in q] == [0]      # no pointless empty page


def test_paging_without_total_falls_back_to_short_page():
    s = FakeSession({("artists/7/albums", None, 0): [raw_album(i) for i in range(1, 51)]}, report_total=False)
    cat, _, _ = catalog(s)
    assert len(cat.artist_releases(7)) == 50
    assert [q["offset"] for _, q, _ in s.requests if "filter" not in q] == [0, 50]


def test_paging_stops_on_short_page_even_if_total_claims_more():
    s = FakeSession({("artists/7/albums", None, 0): [raw_album(1)]})
    s.report_total = False
    orig = s._map_request
    s._map_request = lambda path, params, parse=None: {**orig(path, params, parse), "totalNumberOfItems": 999}
    s.request = NS(map_request=s._map_request)
    cat, _, _ = catalog(s)
    cat.artist_releases(7)
    assert [q["offset"] for _, q, _ in s.requests if "filter" not in q] == [0]


def test_hard_page_cap_logs_and_stops():
    class Endless(FakeSession):
        def _map_request(self, path, params, parse=None):
            self.requests.append((path, dict(params), parse))
            base_id = params["offset"] * 10
            return {"items": [raw_track(base_id + i) for i in range(100)], "totalNumberOfItems": 10 ** 6}

    s = Endless()
    s.request = NS(map_request=s._map_request)
    cat, gate, conn = catalog(s)
    assert len(cat.release_tracks(1)) == 40 * 100
    assert gate.calls == 40
    ev = conn.execute("SELECT subject, message FROM event_log WHERE kind = 'catalog.truncated'").fetchone()
    assert ev["subject"] == "albums/1/tracks" and "40 pages" in ev["message"]


def test_wrapped_items_are_unwrapped():
    s = FakeSession({("albums/9/tracks", None, 0): [{"item": raw_track(1), "type": "track"}, raw_track(2)]})
    cat, _, _ = catalog(s)
    assert [t.tidal_id for t in cat.release_tracks(9)] == [1, 2]


def test_release_dto_mapping_from_raw_json():
    s = FakeSession({("artists/7/albums", None, 0): [
        raw_album(1, version="Deluxe", type="EP", releaseDate="2021-02-03T00:00:00.000+0000", explicit=None,
                  upc="00197207651569", artists=[raw_artist(7, "Will"), raw_artist(8, "X")]),
        raw_album(2, type="WEIRD", releaseDate=None, upc=None, numberOfTracks=None, streamReady=None),
        raw_album(3, type=None, upc="", version=""),
    ]})
    cat, _, _ = catalog(s)
    a, b, c = cat.artist_releases(7)
    assert a == ReleaseDTO(1, "Album 1", "Deluxe", "ep", "2021-02-03", None, 3, "00197207651569", True,
                           (ArtistRef(7, "Will"), ArtistRef(8, "X")), 1)
    assert (b.type, b.release_date, b.upc, b.num_tracks, b.available) == ("other", None, None, None, None)
    assert (c.type, c.upc, c.version) == ("other", None, None)


def test_release_dto_num_volumes_from_raw_json():
    assert tidal.release_dto(raw_album(1, numberOfVolumes=3)).num_volumes == 3
    assert tidal.release_dto(raw_album(1, numberOfVolumes=None)).num_volumes is None
    assert tidal.release_dto(raw_album(1, numberOfVolumes=0)).num_volumes is None
    assert tidal.release_dto({k: v for k, v in raw_album(1).items() if k != "numberOfVolumes"}).num_volumes is None


def test_track_paging_and_dto_mapping_from_raw_json():
    s = FakeSession({("albums/9/tracks", None, 0): [raw_track(i) for i in range(1, 101)],
                     ("albums/9/tracks", None, 100): [
                         raw_track(101, isrc=None, version="Live", duration=None, volumeNumber=2, explicit=None,
                                   allowStreaming=False)]})
    cat, gate, _ = catalog(s)
    out = cat.release_tracks(9)
    assert [(p, q) for p, q, _ in s.requests] == [("albums/9/tracks", {"limit": 100, "offset": 0}),
                                                  ("albums/9/tracks", {"limit": 100, "offset": 100})]
    assert gate.calls == 2 and len(out) == 101
    assert out[0] == TrackDTO(1, "QM24S2009107", "Track 1", None, 127, 1, 1, True, True, WILL)
    last = out[-1]
    assert (last.isrc, last.version, last.duration_s, last.disc, last.explicit, last.available) == (
        None, "Live", None, 2, None, False)


def test_unavailable_track_keeps_isrc_and_version():
    """tidalapi's Track.parse_track drops both for unavailable tracks; the raw-JSON path must not."""
    s = FakeSession({("albums/9/tracks", None, 0): [
        raw_track(1, "Chandelier", isrc="QM24S2009107"),
        raw_track(2, "Chandelier", version="Instrumental Version", isrc="qm24s2009206", allowStreaming=False,
                  streamReady=False, trackNumber=2)]})
    cat, _, _ = catalog(s)
    vocal, inst = cat.release_tracks(9)
    assert (inst.available, inst.version, inst.isrc) == (False, "Instrumental Version", "QM24S2009206")
    assert vocal.version is None and vocal.available is True


def test_search_uses_artist_model_limit_10():
    s = FakeSession(search_result=[NS(id=1, name="Will Paquin"), NS(id=2, name="Will")])
    cat, gate, _ = catalog(s)
    assert cat.search_artists("Will Paquin") == [ArtistRef(1, "Will Paquin"), ArtistRef(2, "Will")]
    name, models, limit = s.searches[0]
    assert name == "Will Paquin" and limit == 10
    assert [m.__name__ for m in models] == ["Artist"]
    assert gate.calls == 1


def test_cache_hits_spend_no_gate_calls():
    s = FakeSession({("artists/7/albums", None, 0): [raw_album(1)], ("albums/1/tracks", None, 0): [raw_track(1)]},
                    search_result=[NS(id=7, name="Will")])
    cat, gate, conn = catalog(s)
    first = (cat.search_artists("Will"), cat.artist_releases(7), cat.release_tracks(1))
    spent, requests = gate.calls, len(s.requests)
    assert spent == 1 + 3 + 1
    # same catalog, and a brand new catalog over the same DB, both read from the cache
    again = (cat.search_artists("WILL"), cat.artist_releases(7), cat.release_tracks(1))
    fresh, fresh_gate, _ = catalog(FakeSession(), conn)
    third = (fresh.search_artists("will"), fresh.artist_releases(7), fresh.release_tracks(1))
    assert first == again == third
    assert gate.calls == spent and len(s.requests) == requests and fresh_gate.calls == 0
    keys = {r[0] for r in conn.execute("SELECT key FROM catalog_cache")}
    assert keys == {"tidal:search:will", "tidal:releases:7", "tidal:tracks:1"}


def test_empty_results_are_cached_too_and_stale_entries_expire():
    s = FakeSession()
    cat, gate, conn = catalog(s)
    assert cat.release_tracks(5) == [] and cat.release_tracks(5) == []
    assert gate.calls == 1
    conn.execute("UPDATE catalog_cache SET fetched_at = datetime('now', '-8 days')")
    assert cat.release_tracks(5) == []
    assert gate.calls == 2


def test_cache_body_is_plain_json():
    s = FakeSession({("artists/7/albums", None, 0): [raw_album(1)]})
    cat, _, conn = catalog(s)
    cat.artist_releases(7)
    body = json.loads(conn.execute("SELECT body FROM catalog_cache").fetchone()[0])
    assert body["dto"] == "ReleaseDTO" and body["items"][0]["artists"] == [{"id": 7, "name": "Will"}]
    assert base.from_json(base.to_json([])) == []


def test_too_many_requests_surfaces_as_gate_paused_and_persists():
    saved = {}
    gate = make_gate(save_state=lambda k, v: saved.update({k: v}))
    s = FakeSession(raise_exc=TooManyRequests(300))
    conn = db.connect(":memory:")
    db.migrate(conn)
    cat = TidalCatalog(s, gate, CatalogCache(conn))
    with pytest.raises(GatePaused) as ei:
        cat.artist_releases(7)
    assert isinstance(ei.value.__cause__, TooManyRequests)
    assert "gate.tidal.paused_until" in saved
    n = len(s.requests)
    with pytest.raises(GatePaused):                      # paused: nothing reaches the session
        cat.release_tracks(1)
    assert len(s.requests) == n
    assert conn.execute("SELECT COUNT(*) FROM catalog_cache").fetchone()[0] == 0   # failures aren't cached


def test_other_errors_propagate_unchanged():
    cat, _, _ = catalog(FakeSession(raise_exc=ValueError("boom")))
    with pytest.raises(ValueError):
        cat.artist_releases(7)


def test_budget_is_enforced_before_each_gated_call_and_cache_keeps_fetched_pages():
    s = FakeSession({("artists/7/albums", None, 0): [raw_album(1)]})
    cat, gate, conn = catalog(s, max_calls=2)
    with pytest.raises(BudgetExceeded):
        cat.artist_releases(7)                           # buckets: call 1 ok, call 2 ok, call 3 refused
    assert gate.calls == 2 and len(s.requests) == 2
    assert conn.execute("SELECT COUNT(*) FROM catalog_cache").fetchone()[0] == 0   # incomplete listing: not cached
    cat.max_calls = None
    assert len(cat.artist_releases(7)) == 1
    spent = gate.calls
    cat.max_calls = spent                                # budget exhausted, but cache hits need no call
    assert len(cat.artist_releases(7)) == 1 and gate.calls == spent


def test_load_session_unwraps_values_and_never_writes(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token_type": {"data": "Bearer"}, "access_token": {"data": "AT"},
                                 "refresh_token": "RT"}))
    before = (token.read_text(), token.stat().st_mtime_ns)
    seen = []

    class FakeLogin:
        request_session = requests.Session()

        def load_oauth_session(self, *args):
            seen.append(args)
            return True

    monkeypatch.setattr(tidal.tidalapi, "Session", FakeLogin)
    assert isinstance(tidal.load_session(token), FakeLogin)
    assert seen == [("Bearer", "AT", "RT")]
    assert (token.read_text(), token.stat().st_mtime_ns) == before


def test_load_session_rejected_or_incomplete_raises(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token_type": "Bearer", "access_token": "AT", "refresh_token": "RT"}))
    monkeypatch.setattr(tidal.tidalapi, "Session",
                        lambda: NS(request_session=requests.Session(),
                                                      load_oauth_session=lambda *a: False))
    with pytest.raises(RuntimeError, match="rejected"):
        tidal.load_session(token)
    token.write_text(json.dumps({"token_type": "Bearer"}))
    with pytest.raises(RuntimeError, match="access_token"):
        tidal.load_session(token)


def test_timeout_adapter_sets_a_default_and_respects_explicit_timeouts(monkeypatch):
    sent = []
    ok = NS(status_code=200)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda self, request, **kw: sent.append(kw.get("timeout")) or ok)
    adapter = tidal._TimeoutAdapter()
    assert adapter.send(NS(), timeout=None) is ok
    adapter.send(NS())
    adapter.send(NS(), timeout=5)
    assert sent == [tidal.TIMEOUT, tidal.TIMEOUT, 5]
    assert tidal.TIMEOUT == (10, 30)


def test_install_timeout_covers_requests_made_the_way_tidalapi_makes_them(monkeypatch):
    sent = []
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda self, request, **kw: sent.append(kw["timeout"]) or (_ for _ in ()).throw(
                            requests.exceptions.ReadTimeout("slow")))
    s = NS(request_session=requests.Session())
    tidal.install_timeout(s)
    for method in (lambda: s.request_session.request("GET", "https://api.tidal.com/v1/x", params={"a": 1}),
                   lambda: s.request_session.post("https://auth.tidal.com/v1/oauth2/token", {"b": 2}),
                   lambda: s.request_session.get("http://example.invalid/")):
        with pytest.raises(requests.exceptions.ReadTimeout):
            method()
    assert sent == [(10, 30)] * 3                                  # no network: the adapter was stubbed


def test_load_session_installs_the_timeout_before_logging_in(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token_type": "Bearer", "access_token": "AT", "refresh_token": "RT"}))
    adapters = []

    class FakeLogin:
        request_session = requests.Session()

        def load_oauth_session(self, *args):
            adapters.append(type(self.request_session.get_adapter("https://api.tidal.com")).__name__)
            return True

    monkeypatch.setattr(tidal.tidalapi, "Session", FakeLogin)
    tidal.load_session(token)
    assert adapters == ["_TimeoutAdapter"]


def test_use_cache_false_refetches_but_still_refreshes_the_cache():
    s = FakeSession({("albums/1/tracks", None, 0): [raw_track(1)]})
    cat, gate, conn = catalog(s)
    cat.release_tracks(1)
    cat.use_cache = False
    cat.release_tracks(1)
    assert gate.calls == 2
    conn.execute("UPDATE catalog_cache SET body = '{\"dto\": \"TrackDTO\", \"items\": []}'")
    cat.use_cache = True
    assert len(cat.release_tracks(1)) == 0                       # served from cache again
    cat.use_cache = False
    assert len(cat.release_tracks(1)) == 1
    assert len(cat.release_tracks(1)) == 1 and gate.calls == 4
    cat.use_cache = True
    assert len(cat.release_tracks(1)) == 1                       # the forced fetch refreshed the entry


def response(status, body=b"{}", headers=None, url="https://auth.tidal.com/v1/oauth2/token"):
    r = requests.Response()
    r.status_code, r._content, r.url = status, body, url
    r.headers.update(headers or {})
    return r


def test_adapter_raises_http_error_on_429_only(monkeypatch):
    answers = iter([response(200), response(401), response(404), response(500), response(429, headers={"Retry-After": "120"})])
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", lambda self, request, **kw: next(answers))
    adapter = tidal._TimeoutAdapter()
    for status in (200, 401, 404, 500):
        assert adapter.send(NS()).status_code == status          # everything else is left to tidalapi
    with pytest.raises(requests.HTTPError) as ei:
        adapter.send(NS())
    assert ei.value.response.status_code == 429 and ei.value.response.headers["Retry-After"] == "120"
    assert "?" not in str(ei.value)


def expired_token_session(monkeypatch, auth_status):
    """A real tidalapi.Session (no network: the transport is stubbed) whose API answers 'token expired'
    and whose auth server answers `auth_status` to the refresh POST."""
    import tidalapi
    calls = []

    def send(self, request, **kw):
        calls.append((request.method, request.url.split("?")[0]))
        if request.method == "POST":                                   # token_refresh
            return response(auth_status, b'{"error": "x"}', {"Retry-After": "300"})
        return response(401, b'{"userMessage": "The token has expired."}', url=request.url)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    session = tidalapi.Session()
    session.token_type, session.access_token, session.refresh_token = "Bearer", "AT", "RT"
    session.session_id, session.country_code = "sid", "US"
    tidal.install_timeout(session)
    return session, calls


def test_429_from_the_auth_server_during_a_token_refresh_trips_the_gate(monkeypatch):
    session, calls = expired_token_session(monkeypatch, 429)
    saved = {}
    gate = make_gate(save_state=lambda k, v: saved.update({k: v}))
    conn = db.connect(":memory:")
    db.migrate(conn)
    cat = TidalCatalog(session, gate, CatalogCache(conn))
    with pytest.raises(GatePaused) as ei:
        cat.artist_releases(7)
    assert isinstance(ei.value.__cause__, requests.HTTPError)
    assert [m for m, _ in calls] == ["GET", "POST"]                    # the refresh really was attempted
    assert "gate.tidal.paused_until" in saved and (ei.value.until - gate._now()).total_seconds() == 300
    assert conn.execute("SELECT COUNT(*) FROM catalog_cache").fetchone()[0] == 0


def test_without_the_transport_hook_the_same_429_would_not_trip_the_gate(monkeypatch):
    """Documents why the adapter raises: tidalapi alone turns this 429 into a KeyError."""
    session, _ = expired_token_session(monkeypatch, 429)
    monkeypatch.setattr(tidal._TimeoutAdapter, "send", requests.adapters.HTTPAdapter.send)
    cat = TidalCatalog(session, make_gate(), CatalogCache(db.connect(":memory:")))
    cat.cache.conn.executescript("CREATE TABLE catalog_cache(key TEXT PRIMARY KEY, fetched_at TEXT, body TEXT)")
    with pytest.raises(Exception) as ei:
        cat.artist_releases(7)
    assert not isinstance(ei.value, GatePaused)
