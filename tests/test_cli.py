import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import tidalapi.exceptions  # tests may import tidalapi; only cadence/ may not outside sources/

from cadence import cli
from cadence.ledger import db
from cadence.sources import tidal
from tests.fakes import FakeSession, raw_artist


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CADENCE_DB", str(tmp_path / "c.db"))
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token_type": "Bearer", "access_token": "AT", "refresh_token": "RT"}))
    monkeypatch.setenv("CADENCE_TIDAL_TOKEN", str(token))
    return tmp_path


def settings(env) -> dict:
    conn = db.connect(env / "c.db")
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
    finally:
        conn.close()


def test_429_during_login_pauses_the_gate_and_exits_3(env, monkeypatch, capsys):
    logins = []

    class Login:
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
    monkeypatch.setattr(tidal.tidalapi, "Session", lambda: NS(load_oauth_session=lambda *a: False))
    assert cli.main(["sync-catalog", "--all"]) == 1
    assert "gate.tidal.paused_until" not in settings(env)
    assert "cannot start Tidal session" in capsys.readouterr().err


def test_ambiguous_artist_line_tells_you_what_to_do(env, monkeypatch, capsys):
    conn = db.connect(env / "c.db")
    db.migrate(conn)
    conn.execute("INSERT INTO artist(name, norm_name) VALUES ('Will', 'will')")
    conn.commit()
    conn.close()
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
