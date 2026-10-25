"""`python -m cadence ...` command line."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from cadence import report
from cadence.catalog.candidates import format_result, pending_artists, run_candidates
from cadence.catalog.groups import regroup_all, regroup_artist
from cadence.catalog.link import link_files
from cadence.catalog.sync import clear_cache, pending_work, sync_all
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
                + (f", {e['hidden']} no longer listed" if e["hidden"] else "")
                + (f", {e['release_missing']} release(s) without tracks (404)" if e["release_missing"] else ""))
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


def _tidal_catalog(conn, args, cmd: str):
    """(catalog, None) once logged in, or (None, exit code) when the gate is paused or the login fails."""
    gate = _gate(conn, args.interval, args.burst)
    until = gate.paused_until()
    if until:
        print(f"gate paused until {until.isoformat()}; not touching Tidal")
        return None, EXIT_PAUSED
    from cadence.sources.tidal import CatalogCache, TidalCatalog  # imports tidalapi
    token = os.path.expanduser(os.environ.get("CADENCE_TIDAL_TOKEN", "~/.config/tidal_dl_ng/token.json"))
    try:
        return TidalCatalog(_login(gate, token), gate, CatalogCache(conn), args.max_calls), None
    except GatePaused as p:
        print(f"gate paused until {p.until.isoformat()}: {p.reason}")
        return None, EXIT_PAUSED
    except Exception as exc:
        print(f"{cmd}: cannot start Tidal session: {exc}", file=sys.stderr)
        return None, 1


def cmd_sync_catalog(args, conn) -> int:
    if not args.all and not args.artist:
        print("sync-catalog: give --artist NAME (repeatable) or --all", file=sys.stderr)
        return 2
    work = pending_work(conn, None if args.all else args.artist, args.force)
    if work["due"] == 0:  # decide before logging in: no work means no Tidal calls at all
        for n in work["unknown"]:
            print(f"{n}: not in the ledger (import-legacy first)")
        print(f"sync-catalog: nothing due ({work['skipped']} synced within 7 days, {work['ambiguous']} "
              f"ambiguous, {work['unresolved']} unresolved); not contacting Tidal")
        return 0
    catalog, code = _tidal_catalog(conn, args, "sync-catalog")
    if catalog is None:
        return code
    res = sync_all(conn, catalog, None if args.all else args.artist, args.max_calls, args.force,
                   progress=lambda e: print(_line(e), flush=True))
    for n in res["unknown"]:
        print(f"{n}: not in the ledger (import-legacy first)")
    print(f"sync-catalog: {res['synced']} synced, {res['skipped']} skipped, {res['unresolved']} unresolved, "
          f"{res['ambiguous']} ambiguous, {res['errors']} errors, {res['release_missing']} release(s) "
          f"without tracks, {res['calls']} gate calls, "
          f"stopped={res['stopped']}")
    if res["stopped"] == "paused":
        print(f"gate paused until {res['until']}")
        return EXIT_PAUSED
    if res["stopped"] == "auth":
        print("Tidal rejected our login (401). Refresh it by running tidal-dl-ng once, then re-run.",
              file=sys.stderr)
    return 1 if res["stopped"] in ("network", "auth") else 0


def cmd_candidates(args, conn) -> int:
    rows, unknown, other = pending_artists(conn, args.artist)
    notes = [f"{n}: not in the ledger (import-legacy first)" for n in unknown] + \
            [f"{r['name']}: not ambiguous (tidal_match={r['tidal_match']}); skipped" for r in other]
    if not args.json:
        print(*notes, sep="\n", end="\n" if notes else "")
    if not rows:  # decide before logging in: no work means no Tidal calls at all
        print(json.dumps({"artists": [], "notes": notes, "stopped": None}, indent=2) if args.json
              else "candidates: no ambiguous artists to compare; not contacting Tidal")
        return 0
    catalog, code = _tidal_catalog(conn, args, "candidates")
    if catalog is None:
        return code
    res = run_candidates(conn, catalog, rows, progress=None if args.json else
                         lambda r: print(format_result(r) + "\n", flush=True))
    if args.json:
        print(json.dumps({**res, "notes": notes}, indent=2))
    else:
        print(f"candidates: {len(res['artists'])} artist(s), "
              f"{sum(r['recommendation'] is not None for r in res['artists'])} with a suggestion, "
              f"{res['calls']} gate calls, stopped={res['stopped']}")
    if res["stopped"] == "paused":
        print(f"gate paused until {res['until']}", file=sys.stderr if args.json else sys.stdout)
        return EXIT_PAUSED
    if res["stopped"] == "auth":
        print("Tidal rejected our login (401). Refresh it by running tidal-dl-ng once, then re-run.",
              file=sys.stderr)
    return 1 if res["stopped"] in ("network", "auth") else 0


def cmd_cache_clear(args, conn) -> int:
    row = None
    if args.artist:
        row = _artist_by_name(conn, args.artist)
        if row is None or row["tidal_id"] is None:
            print(f"cache-clear: '{args.artist}' is not in the ledger or has no Tidal id", file=sys.stderr)
            return 1
    n = clear_cache(conn, row)
    print(f"cache-clear: {n} cached response(s) deleted" + (f" for {row['name']}" if row else ""))
    return 0


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


def cmd_regroup(args, conn) -> int:
    row = _artist_by_name(conn, args.artist) if args.artist else None
    if args.artist and row is None:
        print(f"regroup: no artist '{args.artist}'", file=sys.stderr)
        return 1
    s = regroup_artist(conn, row["id"]) if row else regroup_all(conn)
    conn.commit()
    print(f"regroup: {s['recordings']} recordings -> {s['clusters']} clusters, {s['groups']} release groups")
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
    s.add_argument("--force", action="store_true", help="re-sync artists synced within 7 days, ignoring the cache")
    s.set_defaults(fn=cmd_sync_catalog)

    s = sub.add_parser("candidates", help="compare ambiguous artists' Tidal profiles with owned albums (network)")
    s.add_argument("--artist", action="append", default=[], metavar="NAME")
    s.add_argument("--max-calls", type=int, default=150)
    s.add_argument("--interval", type=_positive_float, default=2.0)
    s.add_argument("--burst", type=_at_least_one, default=3)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("cache-clear", help="forget cached Tidal responses")
    s.add_argument("--artist", metavar="NAME")
    s.set_defaults(fn=cmd_cache_clear)

    s = sub.add_parser("link", help="match files to recordings")
    s.add_argument("--artist")
    s.add_argument("--rematch", action="store_true")
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("regroup", help="recompute release groups and recording clusters (no network)")
    s.add_argument("--artist")
    s.set_defaults(fn=cmd_regroup)

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
