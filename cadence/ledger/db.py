"""SQLite ledger: connection, migrations, event log, settings.

Helpers here never commit; the caller owns the transaction.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    in_memory = path == ":memory:"
    if not in_memory:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        path = str(Path(path).expanduser())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if not in_memory:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[int]:
    """Apply unrecorded `NNN_*.sql` files in order, one transaction each. Returns applied versions."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version("
                 "version INTEGER PRIMARY KEY, applied_at TEXT)")
    conn.commit()
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    applied: list[int] = []
    for f in sorted((migrations_dir or MIGRATIONS_DIR).glob("[0-9][0-9][0-9]_*.sql")):
        version = int(f.name[:3])
        if version in done:
            continue
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        script = (f"BEGIN;\n{f.read_text()}\n;\n"
                  f"INSERT INTO schema_version(version, applied_at) VALUES ({version}, '{stamp}');\n"
                  "COMMIT;")
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        applied.append(version)
    return applied


def log_event(conn: sqlite3.Connection, kind: str, subject: str | None, message: str,
              data: dict | list | None = None) -> None:
    conn.execute("INSERT INTO event_log(kind, subject, message, data_json) VALUES (?, ?, ?, ?)",
                 (kind, subject, message, json.dumps(data, default=str) if data is not None else None))


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO setting(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
