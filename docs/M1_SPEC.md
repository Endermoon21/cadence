# Cadence M1 — implementation spec

Read-only milestone: build the ledger, pull Tidal catalogs, link existing library files to recordings, and report what is missing.
Context: `DESIGN.md` §4 (data model), §6 (Tidal gate), §8/§8b (findings), §11 (backend). Schema: `cadence/ledger/migrations/001_init.sql` (authoritative; do not change it without saying so).

**Hard rules**
- Python 3.13 venv at `.venv` (`.venv/bin/python -m pytest -q`). Runtime deps: `tidalapi==0.8.11`, `mutagen`. Dev: `pytest`. No other dependencies.
- Nothing in M1 writes to the music library or to `~/.config/tidal_dl_ng/token.json`. Read-only everywhere outside `data/`.
- Tests make **zero network calls**. Tidal is always faked in tests.
- Only `cadence/sources/*` may import `tidalapi`.
- Style: stdlib `sqlite3`, `dataclasses`, `argparse`, type hints, small functions, docstrings only where behaviour isn't obvious.

## Layout
```
pyproject.toml                       # name "cadence", requires-python >=3.12, deps above, [project.scripts] cadence = "cadence.cli:main"
cadence/__init__.py
cadence/__main__.py                  # python -m cadence -> cli.main()
cadence/identity/normalize.py        # part A
cadence/identity/matcher.py          # part A
cadence/gate/bucket.py               # part A
cadence/ledger/db.py                 # part B
cadence/ledger/legacy.py             # part B
cadence/sources/base.py              # part B: DTOs
cadence/sources/tidal.py             # part B
cadence/catalog/sync.py              # part B
cadence/catalog/link.py              # part B
cadence/report.py                    # part B
cadence/cli.py                       # part B
tests/...                            # both parts
tests/fixtures/tidal_will_paquin.json      # s1 spike dump: list of releases {id,title,version,type,upc,date,explicit,n,available,artists:[names],tracks:[{id,isrc,title,version,dur,n,disc,explicit,artists}]}
tests/fixtures/library_will_paquin.jsonl   # 30 Will Paquin files + 6 other-artist files, rows from m1/scan_library.py
```

## Part A — identity and gate (pure, no DB)

### `identity/normalize.py`
- `fold(s: str) -> str`: NFKC; map ’ ‘ ʼ ´ ` to `'` and “ ” to `"`; map – — to `-`; `&` -> ` and `; strip diacritics (NFKD, drop combining marks); casefold; **delete apostrophes** (so `Her's` and `Her’s` both -> `hers`); replace every other char that is not alphanumeric (`str.isalnum`, unicode-aware so Japanese survives) with a space; collapse whitespace; strip.
- `norm_artist(name: str) -> str`: `fold(name)`, then drop a leading `the `.
- `@dataclass(frozen=True) TitleParts(base: str, variant: str | None, flags: frozenset[str])`
- `split_title(title: str, version: str | None = None) -> TitleParts`
  - Groups are `(...)` and `[...]` anywhere (also with no space before them: `romantico(2022 Remaster)`), plus a trailing ` - suffix` **only if** the suffix contains a known keyword (remaster, live, remix, mix, edit, version, demo, instrumental, acoustic, mono, stereo). `version` (Tidal's separate field) is treated as one more group unless it already appears in the title.
  - Classify each group (after `fold`):
    - **feat**: starts with `feat`, `ft`, `featuring`, `with ` -> flag `feat`, removed.
    - **neutral**: remaster/remastered (with or without a year, either order), `explicit`, `clean`, `album version`, `original mix`, `original version`, `single version`, `mono`, `stereo`, `bonus track`, deluxe/expanded/anniversary/special edition variants -> flag (`remaster`, `explicit`, `clean`, `deluxe`, or `neutral`), removed.
    - **variant**: contains any of `VARIANT_WORDS` = instrumental, remix, mix, live, demo, acoustic, edit, extended, slowed, sped up, reverb, acapella, a cappella, 12inch, 12 inch, 7inch, rework, bootleg, stripped, piano version, orchestral, session, nightcore. Label = the folded group with the words `version`/`ver` removed and `12inch` -> `12 inch`. Several variant groups join with a single space in order of appearance.
    - **anything else** stays part of the base title, in place (e.g. `(Don't Fear) The Reaper`).
  - `base = fold(remaining text)`.
- `norm_title(title, version=None) -> str`: `base` if no variant else `f"{base} [{variant}]"`.
- `is_various(name) -> bool`: folded name in {`various artists`, `various`, `va`}.

Golden cases (must be tests):
| input (title, version) | norm_title |
|---|---|
| `Chandelier (Instrumental Version)` | `chandelier [instrumental]` |
| `Chandelier`, version `Instrumental Version` | `chandelier [instrumental]` |
| `Her's` / `Her’s` (fold) | `hers` |
| `Jibby’s Theme` | `jibbys theme` |
| `Gum (Do You Want Some?)` | `gum do you want some` |
| `romantico(2022 Remaster)` | `romantico` |
| `Song (feat. X) [Remastered 2011]` | `song` |
| `Song - Remastered 2011` | `song` |
| `Pretty Dark (Demo)` | `pretty dark [demo]` |
| `We Appreciate Power (BloodPop® Remix)` | `we appreciate power [bloodpop remix]` |
| `Kiss Of Life (feat. Giovanca & Benny Sings)` | `kiss of life` |
| `D.T.F.N. (12inch Ver.)` | `d t f n [12 inch]` |
| `(Don't Fear) The Reaper` | `dont fear the reaper` |
| `Roll The Dice` vs `Roll the Dice` | equal |
| `Song (Live at Wembley)` | `song [live at wembley]` |
| `Song (Original Mix)` | `song` |
Plus: `norm_artist("The Beatles") == "beatles"`, `norm_artist("Earth, Wind & Fire") == "earth wind and fire"`, Japanese text survives `fold`.

### `identity/matcher.py`
```python
@dataclass(frozen=True)
class FileFacts: title: str; duration_s: float | None; isrc: str | None = None; album: str | None = None
@dataclass(frozen=True)
class RecCandidate: recording_id: int; isrc: str | None; norm_title: str; duration_s: int | None; release_norm_titles: frozenset[str]
@dataclass(frozen=True)
class MatchResult: recording_id: int; method: str  # 'isrc' | 'fuzzy'
                   score: float
def match_file(f: FileFacts, cands: Sequence[RecCandidate], tolerance_s: float = 2.0) -> MatchResult | None
```
1. ISRC equal (strip + upper) -> `('isrc', 1.0)`.
2. Else candidates with `norm_title(f.title) == cand.norm_title` and both durations known and `abs(diff) <= tolerance_s`. Pick: album match first (`norm_title(f.album) in cand.release_norm_titles`), then smallest duration diff, then lowest `recording_id`. Score 0.9 with album match, else 0.8.
3. Else `None`. Never guess on title alone.

### `gate/bucket.py`
```python
class GatePaused(Exception):  # .until: datetime (UTC), .reason: str
class Gate:
    def __init__(self, name: str, min_interval: float = 2.0, burst: int = 3, *,
                 clock=time.monotonic, sleep=time.sleep, now=lambda: datetime.now(timezone.utc),
                 load_state: Callable[[str], str | None] | None = None,
                 save_state: Callable[[str, str], None] | None = None): ...
    calls: int
    def call(self, fn, *args, **kwargs): ...
    def paused_until(self) -> datetime | None: ...
    def trip(self, retry_after: int | None, reason: str) -> datetime: ...
```
- Token bucket: capacity `burst`, refills one token per `min_interval` seconds. `call` sleeps until a token is available, then calls `fn`, increments `calls`.
- If paused (`paused_until() > now()`) `call` raises `GatePaused` without calling `fn`.
- If `fn` raises an exception whose class name is `TooManyRequests` (do **not** import tidalapi; duck-type, read `retry_after` attribute if present): `trip(...)` then raise `GatePaused` (chain the original).
- `trip`: if `retry_after` > 0 pause that long (minimum 60 s); else use the ladder 15 min, 1 h, 4 h, 4 h... indexed by consecutive trips (a successful call resets the counter). Persist ISO-8601 `paused_until` via `save_state(f"gate.{name}.paused_until", iso)` and read it back via `load_state` on init, so a new process respects an earlier pause.
- Tests use a fake clock/sleep/now; no real sleeping.

## Part B — ledger, Tidal source, sync, link, report, CLI

### `ledger/db.py`
`connect(path) -> sqlite3.Connection` (creates parent dir; `row_factory=sqlite3.Row`; `PRAGMA foreign_keys=ON`, `journal_mode=WAL` for files, `busy_timeout=5000`). `migrate(conn)`: creates `schema_version(version INTEGER PRIMARY KEY, applied_at TEXT)` if absent, applies `migrations/NNN_*.sql` in order that aren't recorded, each in one transaction. `log_event(conn, kind, subject, message, data=None)`. `get_setting(conn, key)`, `set_setting(conn, key, value)`. `":memory:"` must work for tests.

### `ledger/legacy.py`
`import_scan(conn, jsonl_path) -> dict` stats. Upsert `file` by `path`: `dur->duration_s, sr->sample_rate, bits->bit_depth, br->bitrate, art->has_art, codec, size, mtime, title->tag_title, artist->tag_artist, albumartist->tag_albumartist, album->tag_album, track->tag_track, disc->tag_disc, date->tag_date, isrc->tag_isrc, mb_track->tag_mb_track`, `seen_at=now`. Artist name = albumartist, else artist; skip empty or `is_various`. Upsert `artist` by `norm_artist(name)` (keep the first display name seen). Set `file.artist_id`. Re-import must not reset `recording_id`/`match` of existing rows, and must never clobber `match='manual'`.

### `sources/base.py`
```python
@dataclass(frozen=True) class ArtistRef: id: int; name: str
@dataclass(frozen=True) class TrackDTO: tidal_id: int; isrc: str | None; title: str; version: str | None; duration_s: int | None; disc: int; number: int; explicit: bool | None; available: bool | None; artists: tuple[ArtistRef, ...]
@dataclass(frozen=True) class ReleaseDTO: tidal_id: int; title: str; version: str | None; type: str; release_date: str | None; explicit: bool | None; num_tracks: int | None; upc: str | None; available: bool | None; artists: tuple[ArtistRef, ...]
class CatalogSource(Protocol):
    gate: Gate
    def search_artists(self, name: str) -> list[ArtistRef]: ...
    def artist_releases(self, tidal_artist_id: int) -> list[ReleaseDTO]: ...
    def release_tracks(self, tidal_release_id: int) -> list[TrackDTO]: ...
```
`type` is lower-cased Tidal type mapped into {`album`,`ep`,`single`,`compilation`,`other`}. Plus `to_json`/`from_json` helpers for the cache.

### `sources/tidal.py`
- `load_session(token_path) -> tidalapi.Session`: tidal-dl-ng token JSON; each value may be wrapped as `{"data": value}` (unwrap). `session.load_oauth_session(token_type, access_token, refresh_token)`; raise a clear error if it returns False. Never write the token file.
- `TidalCatalog(session, gate, cache)`. Every network call goes through `gate.call`. Use `session.request.map_request(path, params, parse=...)` directly to avoid tidalapi's extra lookup calls:
  - releases: `artists/{id}/albums` with params `{"limit": 50, "offset": off}` plus `"filter": "EPSANDSINGLES"` and `"filter": "COMPILATIONS"` variants, `parse=session.parse_album`; page while a page returns 50. Dedupe by id (first bucket wins).
  - tracks: `albums/{id}/tracks`, `{"limit": 100, "offset": off}`, `parse=session.parse_track`; page while 100 returned.
  - search: `session.search(name, models=[tidalapi.artist.Artist], limit=10)["artists"]`.
  - Album fields: `id, name, version, type, release_date (datetime|None -> YYYY-MM-DD), explicit, num_tracks, universal_product_number, available, artists[*].id/.name`. Track fields: `id, isrc, name, version, duration, volume_num, track_num, explicit, available, artists`.
- `CatalogCache(conn, max_age_days=7)`: `get(key) -> obj | None`, `put(key, obj)` on the `catalog_cache` table (JSON). Keys: `tidal:search:{norm}`, `tidal:releases:{artist_id}`, `tidal:tracks:{release_id}`. Cache hits spend no gate calls.

### `catalog/sync.py`
- `resolve_artist(conn, catalog, artist_row) -> int | None`: keep existing `auto`/`manual` ids. Else search; exact = results whose `norm_artist(name) == artist.norm_name`. One -> `auto`; several -> first one, `ambiguous`, store candidates JSON; none -> `none`, store top 5 candidates. A UNIQUE clash on `tidal_id` -> `ambiguous` with no id. Log an event either way.
- `sync_artist(conn, catalog, artist_row) -> dict`: releases upserted via `release_source(provider='tidal', provider_ref=str(id))`; `role='own'` iff `release.artists[0].id == artist.tidal_id`, else `appears_on`. Tracks fetched **only for own releases**. Recording upsert: by ISRC when present, else by `(artist_id, norm_title, |duration| <= 2)`; `recording.norm_title = norm_title(title, version)`, `variant = split_title(...).variant`. Track upsert by `tidal_id`. Commit once per artist (so a pause mid-run keeps finished artists). Set `artist.catalog_synced_at`.
- `sync_all(conn, catalog, names=None, max_calls=400, force=False) -> dict`: artists by name (or only `names`), skipping ones synced within 7 days unless `force`. Stop cleanly on `GatePaused` (log it; return `stopped='paused'` and the `until` time) or when `catalog.gate.calls >= max_calls` (`stopped='budget'`).

### `catalog/link.py`
`link_files(conn, artist_id=None, rematch=False) -> dict`: for files with an `artist_id` and `match='unmatched'` (all non-manual if `rematch`), build `RecCandidate`s from that artist's recordings (release norm titles via track->release), run `match_file`, store `recording_id`, `match`, `match_score`. Never touch `match='manual'`.

### `report.py`
- `artist_report(conn, artist_id) -> dict`: own, non-hidden releases ordered by date, each `{title, type, date, total, owned, state: owned|partial|missing}` where a track is owned if its recording has >= 1 file; `missing_recordings` (unique, not owned, on any own release); `unmatched_files`; `duplicates` (recordings with > 1 file, with paths); `appears_on` count; `tidal_match`.
- `library_summary(conn) -> list[dict]` one row per artist: tidal_match, own releases, recordings owned/total, missing count, unmatched files.
- `format_artist_report(d) -> str`, `format_summary(rows) -> str`: plain text.

### `cli.py` (`python -m cadence ...`)
Env: `CADENCE_DB` (default `data/cadence.db`), `CADENCE_TIDAL_TOKEN` (default `~/.config/tidal_dl_ng/token.json`).
- `init` -> migrate
- `import-legacy PATH`
- `sync-catalog [--artist NAME]... [--all] [--max-calls 400] [--interval 2.0] [--burst 3] [--force]` (prints one line per artist; exit code 3 if paused; the gate persists its pause in `setting`)
- `link [--artist NAME] [--rematch]`
- `report [--artist NAME] [--json]`
- `artist set-tidal NAME TIDAL_ID` (sets `manual`)
- `gate-status`

### Acceptance test (`tests/test_will_paquin_e2e.py`)
In-memory DB -> `migrate` -> `import_scan(library_will_paquin.jsonl)` -> a `FakeCatalog` built from `tidal_will_paquin.json` (assign stable fake artist ids by name, Will Paquin = 1; `search_artists` returns Will; gate is a real `Gate` with a fake clock) -> `sync_all(names=["Will Paquin"])` -> `link_files` -> `artist_report`. Expected:
- 18 own releases, 2 `appears_on` (COMEBACK by Noty, Songs To Come Home To by Ta-ku)
- 29 unique recordings for Will
- **0 missing recordings; every own release `owned`**
- all 30 Will files matched; exactly 1 duplicate recording (*Hahaha*, two files)
- the 6 other-artist files belong to other artists and do not appear in Will's report
- a second `sync_all` without `force` spends **0** gate calls (cache + recently-synced skip)
If real data contradicts an expectation, report the exact files/titles rather than loosening the assertion.
