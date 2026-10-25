"""`python -m cadence ...` command line."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from cadence import report
from cadence.catalog.link import link_files
from cadence.catalog.sync import sync_all
from cadence.gate.bucket import Gate, GatePaused
from cadence.identity.normalize import norm_artist
from cadence.ledger import db
from cadence.ledger.legacy import import_scan

EXIT_PAUSED = 3
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "cadence.db"  # project root, not the cwd


def _db_path() -> str:
    return os.environ.get("CADENCE_DB") or str(DEFAULT_DB)


def _open() -> sqlite3.Connection:
    conn = db.connect(_db_path())
    db.migrate(conn)
    return conn


def _gate(conn: sqlite3.Connection, interval: float = 2.0, burst: int = 3) -> Gate:
    def save(key: str, value: str) -> None:
        db.set_setting(conn, key, value)
        conn.commit()  # a pause must survive even if the run dies right after
    return Gate("tidal", interval, burst, load_state=lambda k: db.get_setting(conn, k), save_state=save)


def _artist_by_name(conn: sqlite3.Connection, name: str):
    return conn.execute("SELECT * FROM artist WHERE norm_name = ?", (norm_artist(name),)).fetchone()


def cmd_init(args, conn) -> int:
    print("ledger ready:", _db_path())
    return 0


def cmd_import_legacy(args, conn) -> int:
    s = import_scan(conn, args.path)
    print(f"import-legacy: {s['rows']} rows, {s['files_new']} new files, {s['files_updated']} updated, "
          f"{s['artists_created']} new artists, {s['files_without_artist']} without artist")
    return 0


def _line(e: dict) -> str:
    if e["status"] == "synced":
        return (f"{e['artist']}: synced, {e['own']} own + {e['appears_on']} appears-on release(s), "
                f"{e['tracks']} track(s), {e['recordings_new']} new recording(s)"
                + (f", {e['hidden']} no longer listed" if e["hidden"] else ""))
    if e["status"] == "ambiguous":
        return (f"{e['artist']}: ambiguous: {e['candidates']} candidates, "
                f"run `cadence artist set-tidal \"{e['artist']}\" ID`")
    if e["status"] == "error":
        return f"{e['artist']}: error, {e['error']}"
    return f"{e['artist']}: {e['status']}"


def _login(gate: Gate, token: str):
    """Log in through the gate: load_oauth_session makes a few Tidal calls, and a 429 there must pause us."""
    from cadence.sources.tidal import load_session  # imports tidalapi
    return gate.call(load_session, token)


def cmd_sync_catalog(args, conn) -> int:
    if not args.all and not args.artist:
        print("sync-catalog: give --artist NAME (repeatable) or --all", file=sys.stderr)
        return 2
    gate = _gate(conn, args.interval, args.burst)
    until = gate.paused_until()
    if until:
        print(f"gate paused until {until.isoformat()}; not touching Tidal")
        return EXIT_PAUSED
    from cadence.sources.tidal import CatalogCache, TidalCatalog  # imports tidalapi
    token = os.path.expanduser(os.environ.get("CADENCE_TIDAL_TOKEN", "~/.config/tidal_dl_ng/token.json"))
    try:
        catalog = TidalCatalog(_login(gate, token), gate, CatalogCache(conn), args.max_calls)
    except GatePaused as p:
        print(f"gate paused until {p.until.isoformat()}: {p.reason}")
        return EXIT_PAUSED
    except Exception as exc:
        print(f"sync-catalog: cannot start Tidal session: {exc}", file=sys.stderr)
        return 1
    res = sync_all(conn, catalog, None if args.all else args.artist, args.max_calls, args.force,
                   progress=lambda e: print(_line(e), flush=True))
    for n in res["unknown"]:
        print(f"{n}: not in the ledger (import-legacy first)")
    print(f"sync-catalog: {res['synced']} synced, {res['skipped']} skipped, {res['unresolved']} unresolved, "
          f"{res['ambiguous']} ambiguous, {res['errors']} errors, {res['calls']} gate calls, "
          f"stopped={res['stopped']}")
    if res["stopped"] == "paused":
        print(f"gate paused until {res['until']}")
        return EXIT_PAUSED
    return 1 if res["stopped"] == "network" else 0


def cmd_link(args, conn) -> int:
    artist_id = None
    if args.artist:
        row = _artist_by_name(conn, args.artist)
        if row is None:
            print(f"link: no artist '{args.artist}'", file=sys.stderr)
            return 1
        artist_id = row["id"]
    s = link_files(conn, artist_id, args.rematch)
    db.log_event(conn, "match", args.artist, f"link: {s['matched']} of {s['files']} file(s) matched "
                 f"({s['isrc']} by ISRC, {s['fuzzy']} by title+duration), {s['unmatched']} unmatched", s)
    conn.commit()
    print(f"link: {s['files']} files, {s['matched']} matched ({s['isrc']} isrc, {s['fuzzy']} fuzzy), "
          f"{s['unmatched']} unmatched")
    return 0


def cmd_report(args, conn) -> int:
    if args.artist:
        row = _artist_by_name(conn, args.artist)
        if row is None:
            print(f"report: no artist '{args.artist}'", file=sys.stderr)
            return 1
        data = report.artist_report(conn, row["id"])
        print(json.dumps(data, indent=2) if args.json else report.format_artist_report(data))
    else:
        rows = report.library_summary(conn)
        print(json.dumps(rows, indent=2) if args.json else report.format_summary(rows))
    return 0


def cmd_set_tidal(args, conn) -> int:
    row = _artist_by_name(conn, args.name)
    if row is None:
        print(f"artist set-tidal: no artist '{args.name}'", file=sys.stderr)
        return 1
    try:
        conn.execute("UPDATE artist SET tidal_id = ?, tidal_match = 'manual', tidal_candidates = NULL, "
                     "catalog_synced_at = NULL WHERE id = ?", (args.tidal_id, row["id"]))
    except sqlite3.IntegrityError:
        conn.rollback()
        print(f"artist set-tidal: tidal id {args.tidal_id} already belongs to another artist", file=sys.stderr)
        return 1
    db.log_event(conn, "artist.resolve", row["name"],
                 f"Tidal id for '{row['name']}' set manually to {args.tidal_id}", {"tidal_id": args.tidal_id})
    conn.commit()
    print(f"{row['name']}: tidal id {args.tidal_id} (manual)")
    return 0


def cmd_gate_status(args, conn) -> int:
    until = _gate(conn).paused_until()
    print(f"tidal gate: paused until {until.isoformat()}" if until else "tidal gate: open")
    return 0


def _positive_float(v: str) -> float:
    f = float(v)
    if f <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return f


def _at_least_one(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cadence", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create / migrate the ledger").set_defaults(fn=cmd_init)

    s = sub.add_parser("import-legacy", help="import a library scan JSONL")
    s.add_argument("path")
    s.set_defaults(fn=cmd_import_legacy)

    s = sub.add_parser("sync-catalog", help="pull Tidal catalogs (network)")
    s.add_argument("--artist", action="append", default=[], metavar="NAME")
    s.add_argument("--all", action="store_true")
    s.add_argument("--max-calls", type=int, default=400)
    s.add_argument("--interval", type=_positive_float, default=2.0)
    s.add_argument("--burst", type=_at_least_one, default=3)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_sync_catalog)

    s = sub.add_parser("link", help="match files to recordings")
    s.add_argument("--artist")
    s.add_argument("--rematch", action="store_true")
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("report", help="what is owned / missing")
    s.add_argument("--artist")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_report)

    art = sub.add_parser("artist", help="artist maintenance").add_subparsers(dest="sub", required=True)
    s = art.add_parser("set-tidal", help="pin an artist to a Tidal id")
    s.add_argument("name")
    s.add_argument("tidal_id", type=int)
    s.set_defaults(fn=cmd_set_tidal)

    sub.add_parser("gate-status", help="show whether the Tidal gate is paused").set_defaults(fn=cmd_gate_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = _open()
    try:
        return args.fn(args, conn)
    finally:
        conn.close()
