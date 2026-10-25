import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import requests
import tidalapi.exceptions  # tests may import tidalapi; only cadence/ may not outside sources/

from cadence import cli
from cadence.ledger import db
from cadence.sources import tidal
from tests.fakes import FakeSession, raw_album, raw_artist, raw_track


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CADENCE_DB", str(tmp_path / "c.db"))
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token_type": "Bearer", "access_token": "AT", "refresh_token": "RT"}))
    monkeypatch.setenv("CADENCE_TIDAL_TOKEN", str(token))
    return tmp_path


def seed(env, *names, **cols):
    conn = db.connect(env / "c.db")
    db.migrate(conn)
    for n in names:
        conn.execute("INSERT INTO artist(name, norm_name) VALUES (?, ?)", (n, n.lower()))
        if cols:
            conn.execute("UPDATE artist SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE norm_name = ?",
                         (*cols.values(), n.lower()))
    conn.commit()
    conn.close()


def settings(env) -> dict:
    conn = db.connect(env / "c.db")
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
    finally:
        conn.close()


def test_429_during_login_pauses_the_gate_and_exits_3(env, monkeypatch, capsys):
    logins = []
    seed(env, "Will")

    class Login:
        request_session = requests.Session()

        def load_oauth_session(self, *args):
            logins.append(args)
            raise tidalapi.exceptions.TooManyRequests("Too many requests", retry_after=300)

    monkeypatch.setattr(tidal.tidalapi, "Session", Login)
    assert cli.main(["sync-catalog", "--artist", "Will"]) == cli.EXIT_PAUSED
    s = settings(env)
    assert "gate.tidal.paused_until" in s and s["gate.tidal.trips"] == "1"
    assert "gate paused until" in capsys.readouterr().out

    # the next run sees the persisted pause and never builds a session
    monkeypatch.setattr(tidal.tidalapi, "Session", lambda: pytest.fail("must not log in while paused"))
    assert cli.main(["sync-catalog", "--artist", "Will"]) == cli.EXIT_PAUSED
    assert len(logins) == 1
    assert cli.main(["gate-status"]) == 0
    assert "paused until" in capsys.readouterr().out


def test_rejected_login_exits_1_without_pausing(env, monkeypatch, capsys):
    seed(env, "Will")
    monkeypatch.setattr(tidal.tidalapi, "Session",
                        lambda: NS(request_session=requests.Session(), load_oauth_session=lambda *a: False))
    assert cli.main(["sync-catalog", "--all"]) == 1
    assert "gate.tidal.paused_until" not in settings(env)
    assert "cannot start Tidal session" in capsys.readouterr().err


def test_ambiguous_artist_line_tells_you_what_to_do(env, monkeypatch, capsys):
    seed(env, "Will")
    session = FakeSession(search_result=[NS(**raw_artist(5, "Will")), NS(**raw_artist(6, "Will"))])
    monkeypatch.setattr(tidal, "load_session", lambda path: session)
    assert cli.main(["sync-catalog", "--all", "--interval", "0.01"]) == 0
    out = capsys.readouterr().out
    assert 'Will: ambiguous: 2 candidates, run `cadence artist set-tidal "Will" ID`' in out
    assert "1 ambiguous" in out
    assert session.requests == []                        # no release listing was fetched


def test_interval_and_burst_are_validated(env, capsys):
    for bad in (["--interval", "0"], ["--interval", "-1"], ["--burst", "0"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(["sync-catalog", "--all", *bad])
        assert ei.value.code == 2


def test_default_db_is_anchored_to_the_project_root(monkeypatch):
    monkeypatch.delenv("CADENCE_DB", raising=False)
    root = Path(cli.__file__).resolve().parents[1]
    assert cli._db_path() == str(root / "data" / "cadence.db")


def never_login(monkeypatch):
    monkeypatch.setattr(tidal.tidalapi, "Session", lambda: pytest.fail("must not log in"))
    monkeypatch.setattr(tidal, "load_session", lambda path: pytest.fail("must not log in"))


def test_nothing_due_means_no_login(env, monkeypatch, capsys):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    seed(env, "Fresh", catalog_synced_at=now, tidal_id=1, tidal_match="auto")
    seed(env, "Twice", tidal_match="ambiguous")
    seed(env, "Nobody", tidal_match="none")
    never_login(monkeypatch)
    for argv in (["sync-catalog", "--all"],
                 ["sync-catalog", "--artist", "Fresh", "--artist", "Ghost"],
                 ["sync-catalog", "--artist", "Twice", "--artist", "Nobody", "--force"]):   # force can't help these
        assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.count("nothing due") == 3 and "not contacting Tidal" in out
    assert "Ghost: not in the ledger" in out
    assert "1 synced within 7 days, 1 ambiguous, 1 unresolved" in out


def test_due_work_still_logs_in_and_force_makes_fresh_artists_due(env, monkeypatch, capsys):
    from datetime import datetime, timezone
    seed(env, "Fresh", catalog_synced_at=datetime.now(timezone.utc).isoformat(), tidal_id=1, tidal_match="auto")
    logins = []
    session = FakeSession({("artists/1/albums", None, 0): [raw_album(10, artists=[raw_artist(1, "Fresh")])],
                           ("albums/10/tracks", None, 0): [raw_track(100)]})
    monkeypatch.setattr(tidal, "load_session", lambda path: logins.append(path) or session)
    assert cli.main(["sync-catalog", "--all", "--interval", "0.01"]) == 0
    assert logins == []                                  # not due without --force
    assert cli.main(["sync-catalog", "--all", "--force", "--interval", "0.01"]) == 0
    assert len(logins) == 1
    assert "Fresh: synced" in capsys.readouterr().out


def test_auth_failure_stops_the_run_with_exit_1_and_a_hint(env, monkeypatch, capsys):
    seed(env, "Aaa", tidal_id=1, tidal_match="manual")
    seed(env, "Bbb", tidal_id=2, tidal_match="manual")
    err = requests.HTTPError("401 Client Error")
    err.response = NS(status_code=401)

    class Session(FakeSession):
        def _map_request(self, path, params, parse=None):
            self.requests.append((path, dict(params), parse))
            raise err

    session = Session()
    session.request = NS(map_request=session._map_request)
    monkeypatch.setattr(tidal, "load_session", lambda path: session)
    assert cli.main(["sync-catalog", "--all", "--interval", "0.01"]) == 1
    io = capsys.readouterr()
    assert "Aaa: error" in io.out and "Bbb" not in io.out and "stopped=auth" in io.out
    assert "running tidal-dl-ng once" in io.err
    assert "gate.tidal.paused_until" not in settings(env)


def test_cache_clear_command(env, capsys):
    seed(env, "Will", tidal_id=1, tidal_match="manual")
    conn = db.connect(env / "c.db")
    for k in ("tidal:releases:1", "tidal:tracks:5", "tidal:search:will", "other:thing"):
        conn.execute("INSERT INTO catalog_cache(key, body) VALUES (?, '{\"dto\": \"ReleaseDTO\", \"items\": []}')", (k,))
    conn.commit()
    conn.close()
    assert cli.main(["cache-clear", "--artist", "Nobody"]) == 1
    assert cli.main(["cache-clear"]) == 0
    assert "3 cached response(s) deleted" in capsys.readouterr().out
    conn = db.connect(env / "c.db")
    assert [r[0] for r in conn.execute("SELECT key FROM catalog_cache")] == ["other:thing"]


def test_release_missing_is_shown_per_artist_and_in_the_summary(env, monkeypatch, capsys):
    seed(env, "Will", tidal_id=1, tidal_match="manual")
    me = raw_artist(1, "Will")

    class Session(FakeSession):
        def _map_request(self, path, params, parse=None):
            if path == "albums/11/tracks":
                err = requests.HTTPError("404 Client Error")
                err.response = NS(status_code=404)
                raise err
            return super()._map_request(path, params, parse)

    session = Session({("artists/1/albums", None, 0): [raw_album(10, artists=[me]), raw_album(11, artists=[me])],
                        ("albums/10/tracks", None, 0): [raw_track(100)]})
    session.request.map_request = session._map_request
    monkeypatch.setattr(tidal, "load_session", lambda path: session)
    assert cli.main(["sync-catalog", "--all", "--interval", "0.01"]) == 0
    out = capsys.readouterr().out
    assert "Will: synced, 2 own + 0 appears-on release(s), 1 track(s)" in out
    assert "1 release(s) without tracks (404)" in out.splitlines()[0]
    assert "1 release(s) without tracks, " in out.splitlines()[-1]
