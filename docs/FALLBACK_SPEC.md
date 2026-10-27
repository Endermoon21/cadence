# Fallback sources: OrpheusDL and Soulseek (slskd)

Status: spec for implementation on branch `fetch-ledger-planner` after `6968b87`.

## Why and where

Tidal licenses lossless per release. Today a track that Tidal serves lossy (after the edition probes) is **held** in
staging as AAC. Fallbacks are tried at exactly that point, in the order configured, to get a lossless copy of the same
recording from somewhere else. Nothing else in the pipeline changes:

- Only for `LOSSLESS` / `HI_RES_LOSSLESS` requests. `HIGH` never calls a fallback.
- Only when `item.duration_s` is known. Fallback sources are matched by search, so the planned length is the anchor.
  Unknown duration: log `fetch.fallback_skipped` and hold as today.
- Dry run unchanged except one line naming the configured fallbacks.
- Never overwrite, never delete a user file. The slskd download is **copied** into staging and left where slskd put it.
  The OrpheusDL temp dir is created by Cadence inside staging and may be removed by Cadence after the copy.

## Hook points in `cadence/fetch/run.py` `_fetch_one`

1. The `elif already_held:` branch (no lossless edition, held copy already in staging): try fallbacks before returning
   `held_lossy`.
2. The lossy-held branch (`if lossy and quality in (...)`): try fallbacks before logging `fetch.held_lossy`.
   If a fallback places a FLAC, the existing tail removes the held `.m4a` after the commit (same rules as now).
   If all miss, hold as today, and add `"fallbacks_tried": [names]` to the `fetch.held_lossy` data.

Refactor the placement tail (dest / `_taken` / mkdir / `_move_into_library` / `_insert_file` / `fetch.placed` / staged
cleanup / stale held removal) into one helper used by both the Tidal path and the fallback path, so behaviour stays
identical. `_insert_file` takes a `source` argument (`'tidal'` default). `fetch.placed` data gains `"source"` always and,
for fallbacks, `"fallback_ref"` and `"planned_tidal_track_id"`.

The fallback target is `item.target(".flac")`; re-check `_taken` for it (conflict / exists rules unchanged).

## New module `cadence/fetch/fallback.py`

```python
@dataclass(frozen=True)
class Acquired:
    path: Path         # a local audio file the source produced
    ref: str           # human/audit reference, e.g. "qobuz luckysearch: Artist Title" or "user123:Music\\A\\01 x.flac"
    detail: dict       # extra event data (no secrets)

class FallbackMiss(Exception): ...  # this source has nothing usable for this item; try the next

class Fallback(Protocol):
    name: str                                  # 'orpheus' | 'soulseek' (also the file.source value)
    def check(self) -> str | None: ...         # preflight problem or None (called once per --execute run)
    def acquire(self, item: FetchItem, workdir: Path, gate: Gate) -> Acquired: ...  # raises FallbackMiss
```

`_try_fallbacks(...)` in run.py, per fallback in order:
- workdir = `tempfile.mkdtemp(dir=staging/".fallback", prefix=f"{name}-{tidal_track_id}-")`
- `acquire`; `FallbackMiss` or any other Exception (except `GatePaused`, `AuthStop`) -> log `fetch.fallback_miss`
  (source, reason) and commit, then next fallback.
- Accept only a `.flac` file (suffix, and `mutagen.flac.FLAC` must open it). Otherwise miss "not FLAC".
- ISRC guard: if the file already has an ISRC tag and `item.isrc` is set and they differ (case-insensitive, stripped),
  miss "ISRC mismatch". Read this **before** `_tag` overwrites it.
- Copy (`shutil.copy2`) to `staged.with_name(staged.name + ".part")` where
  `staged = staging / Path(rel).with_name(f"{stem} [{name}].flac")`; verify with `_verify(part, ffmpeg,
  item.duration_s)` (verify is mandatory for fallbacks even if `verify=False`; requires ffmpeg). A failed verify
  deletes only the `.part` and is a miss. Then `os.replace(part, staged)`, `_tag`, `_file_facts`; lossy -> miss.
- Place with the shared helper, `source=name`. Status `placed`, note `via {name} ({ref})`.
- Orpheus workdir: `shutil.rmtree` after the copy (Cadence created it). Soulseek: nothing is removed.

### OrpheusDL (`OrpheusFallback`)

Runs the user's own OrpheusDL checkout as a subprocess. Cadence never reads OrpheusDL's `config/settings.json`
(credentials stay there; `download_quality` must be set to `hifi` or `lossless` there by the user).

Config (env): `CADENCE_ORPHEUS_DIR` (required; must contain `orpheus.py`), `CADENCE_ORPHEUS_PYTHON` (default
`python3`), `CADENCE_ORPHEUS_MODULE` (required, e.g. `qobuz`, `deezer`, `tidal`), `CADENCE_ORPHEUS_TIMEOUT`
(seconds, default 900).

Command, run with `cwd=CADENCE_ORPHEUS_DIR`, stdin `DEVNULL`, captured output, timeout:
- module `tidal`: `[python, "orpheus.py", "-o", workdir, "download", "tidal", "track", str(item.tidal_track_id)]`,
  executed through `gate.call` (it is Tidal traffic and must respect the shared Tidal gate).
- any other module: `[python, "orpheus.py", "-o", workdir, "luckysearch", module, "track", f"{item.artist} {item.title}"]`
  (luckysearch takes the first result and never prompts; correctness is enforced by the duration/ISRC/lossless checks).

OrpheusDL's exit code is unreliable (several failure paths call bare `exit()` = 0). Ignore it for success; success is
"exactly one `*.flac` file under workdir after the run". Zero -> miss (include last 200 chars of stderr/stdout).
More than one -> miss "ambiguous output". Timeout -> miss. The subprocess runner is injectable for tests.

### Soulseek via slskd (`SoulseekFallback`)

Talks to a running slskd over its REST API. Base `f"{url}/api/v0"`, header `X-API-Key: <key>`. The key is never
logged, printed or put in event data or exception messages.

Config (env): `CADENCE_SLSKD_URL` (required), `CADENCE_SLSKD_API_KEY` (required), `CADENCE_SLSKD_DOWNLOADS`
(required: slskd's downloads directory as seen from where Cadence runs), `CADENCE_SLSKD_TIMEOUT` (seconds per file,
default 900), `CADENCE_SLSKD_MAX_CANDIDATES` (default 3).

VPN gate: **slskd must run behind gluetun** (Mullvad; see `deploy/slskd/run-slskd.sh`), so Soulseek traffic never leaves
the home IP. Cadence asks gluetun's control server, never the VPN itself. Extra env:
`CADENCE_GLUETUN_URL` (e.g. `http://10.0.0.48:8010`) and `CADENCE_GLUETUN_API_KEY` (sent as `X-API-Key`; never logged or
printed), both required when soulseek is enabled. The only opt-out is `CADENCE_SLSKD_VPN=off`, set explicitly; any other
value of that variable is a config error (exit 2). `GET /v1/vpn/status` must answer `{"status": "running"}` (`GET
/v1/publicip/ip` gives the exit address, which `check()` prints and logs as `fetch.vpn_up`). It is re-checked before every
`acquire()`; if the tunnel is not running (or gluetun cannot be asked) `acquire()` raises `VpnDown`, `fetch.vpn_down` is
logged once, Soulseek is skipped for the rest of the run (Tidal and the other fallbacks carry on) and the summary line
shows `{n} vpn down`. Timeouts are `(10, 30)` and redirects are never followed (also for slskd). `CADENCE_SLSKD_URL` and `CADENCE_GLUETUN_URL` must have the same hostname (slskd runs inside gluetun's network) unless `CADENCE_SLSKD_VPN=off`; a mismatch is a config error (exit 2). The status is also re-checked between candidates. A VPN that is down at preflight switches off only Soulseek (`fetch.vpn_down`, one printed line); a rejected gluetun key or any other Soulseek `check()` problem still stops setup. The shared network namespace (the kill switch) is the real guarantee; Cadence's check only catches an obvious outage.

`check()`: downloads dir exists; `GET /api/v0/application` answers 200 (401 -> "slskd rejected the API key").

Flow (http session, `sleep`, `clock` injectable):
1. `POST /searches` json `{"id": str(uuid4()), "searchText": f"{artist} {title}", "searchTimeout": 15000,
   "filterResponses": true, "responseLimit": 100, "fileLimit": 10000}`.
2. Poll `GET /searches/{id}` until `isComplete` true (cap 60 s), then `GET /searches/{id}/responses`
   (list of `{username, hasFreeUploadSlot, queueLength, uploadSpeed, files: [{filename, size, length, bitDepth,
   sampleRate, extension, isLocked}]}`).
3. Candidates: file not locked; filename ends with `.flac` (case-insensitive; `extension` may be empty); `length`
   present and within 3 s of `item.duration_s`; the basename (split on both `\\` and `/`) contains every token of
   `norm_title(item.title)`; the full remote path contains every token of the normalized artist; reject if the
   basename contains any of `live, remix, instrumental, karaoke, acoustic, demo, edit, slowed, sped` that the planned
   title does not contain. Rank: `hasFreeUploadSlot` desc, `queueLength` asc, `bitDepth` desc, `uploadSpeed` desc.
   None -> miss "no matching FLAC on Soulseek".
4. For the top `MAX_CANDIDATES`: `POST /transfers/downloads/{quote(username)}` json `[{"filename": f, "size": s}]`.
   Poll `GET /transfers/downloads/{quote(username)}` (shape `{username, directories: [{directory, files: [transfer]}]}`;
   transfer has `id, filename, state, size, bytesTransferred`). Find the transfer whose `filename` equals ours.
   - `state == "Completed, Succeeded"` -> done.
   - any other state starting with `"Completed"` (Errored, Cancelled, TimedOut, Rejected) -> next candidate.
   - per-file timeout -> `DELETE /transfers/downloads/{user}/{id}` (cancel) and next candidate.
5. Locate the file: search `CADENCE_SLSKD_DOWNLOADS` recursively for the basename with `st_size == size`. slskd can
   report Succeeded before it moves the file out of its incomplete dir, so retry up to 30 s. Exactly one match ->
   `Acquired(path=..., ref=f"{username}:{filename}", detail={"username":..., "filename":..., "size":...})`.
   Zero -> miss "completed but not found in downloads dir"; several -> miss "ambiguous".

All HTTP calls use timeouts `(10, 30)`. Non-2xx -> miss with status (never the key).

### Building from config

`fallbacks_from_env(names: list[str], env=os.environ) -> tuple[list[Fallback], str | None]`: unknown name or a
missing required variable -> error message naming the variable (not its value).

## CLI (`cadence fetch`)

- `--fallback {orpheus,soulseek}` (repeatable, order kept). Default: `CADENCE_FALLBACKS` (comma list), else none.
- Bad config -> exit 2 (also in a dry run, so mistakes show before `--execute`).
- Dry run prints `fallbacks: orpheus, soulseek (only for tracks Tidal serves lossy; not contacted in a dry run)`
  or `fallbacks: none`.
- `--execute`: `check()` for each fallback in the preflight; a problem -> `stopped="setup"` like other setup errors.
- With `--quality HIGH` and fallbacks set: print that fallbacks are unused at HIGH.
- Summary line gains `{n} via fallback` (count of placed items whose source was a fallback).

## Ledger: migration `007_fallback_sources.sql`

`file.source` CHECK becomes `('legacy', 'tidal', 'ytm', 'soulseek', 'orpheus')`. SQLite cannot alter a CHECK, so
rebuild `file` inside the runner's transaction. `PRAGMA foreign_keys` cannot be changed inside a transaction and is
ON, and `DROP TABLE file` cascades into `link_review` (ON DELETE CASCADE). So: copy `link_review` to a temp table,
create `file_new` with the identical column list/order/defaults (001 + 004 columns), `INSERT INTO file_new SELECT`
every column explicitly, `DROP TABLE file`, `ALTER TABLE file_new RENAME TO file`, recreate `file_recording`,
`file_artist`, `file_lossy`, restore `link_review` from the temp copy, drop the temp table. Check for any other table,
trigger or view that references `file` and handle it the same way.

## Tests (new `tests/test_fetch_fallback.py`, `tests/test_soulseek.py`, `tests/test_orpheus.py`, migration test)

No real network, no real subprocess. Fakes go in `tests/fetch_fakes.py` style.
- Lossy Tidal + orpheus returns a matching FLAC -> placed, `file.source='orpheus'`, held `.m4a` removed after commit,
  `fetch.placed` has `source`, `fallback_ref`, `planned_tidal_track_id`.
- Orpheus wrong duration -> miss; soulseek succeeds -> placed with `source='soulseek'`; the slskd file still exists.
- All miss -> `held_lossy`, event has `fallbacks_tried`.
- `duration_s` None -> no fallback called, `fetch.fallback_skipped` logged.
- ISRC mismatch -> miss. Non-FLAC output -> miss. Lossy FLAC impossible, but an `.m4a` from orpheus -> miss.
- Already-held path -> fallbacks tried, Tidal playback not requested again.
- `HIGH` -> fallbacks never called.
- Fallback target path taken by an unlinked file -> conflict, nothing placed.
- Soulseek unit: ranking and filters (locked, non-flac, wrong length, variant words, missing title tokens), enqueue body,
  "Completed, Succeeded" path, "Completed, Errored" -> next candidate, timeout -> DELETE then next, move race (file
  appears on 2nd look), API key sent as header and absent from every logged event and exception text.
- Orpheus unit: tidal module command + goes through gate; other module luckysearch command; exit code 0 with no file
  -> miss; two files -> miss; timeout -> miss.
- Migration 007: existing rows (and `link_review` rows) survive; `source` accepts `soulseek`/`orpheus`, rejects
  `bogus`; indexes exist; `PRAGMA foreign_key_check` is clean.
- CLI: `--fallback` order, `CADENCE_FALLBACKS`, missing env var -> exit 2 naming the variable.
