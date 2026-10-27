> **SUPERSEDED.** This is the 2026-10-27 snapshot taken before the OrpheusDL/Soulseek/Mullvad fallbacks (commit `ab49bb5`) and the koi UI design. It still says those were "not built". **Use `docs/PROJECT.md`**, which is current. Kept for history.

# Cadence: full reference

Cadence is a private music ledger and fetch system. It replaces Aurral and Lidarr. Tidal's catalogue is the source of truth, a SQLite ledger records what is owned, and a fetch planner works out what is missing and downloads it.

This document is a snapshot as of **2026-10-27**, at commit `6968b87` on branch `fetch-ledger-planner`. Where it disagrees with the code, the code wins. `DESIGN.md`, `docs/M1_SPEC.md` and `docs/M2_SPEC.md` are the longer, older design records. `DESIGN.md` still says Python 3.12 and tiddl, which is out of date (see section 16).

---

## 1. Status at a glance

| Area | State |
|------|-------|
| Ledger, legacy import, catalogue sync | Built. Migrations 001 to 006. |
| Identity (grouping, matcher, `cadence link`) | Built and reviewed. Album-prefix and remaster-year merges are deployed. |
| Fetch planner (`cadence fetch` dry run) | Built. Ledger-only ownership, three-bucket plan, partial scope by default. |
| Fetch executor (`cadence fetch --execute`) | Built. Lossless-only gate, held AAC copies, edition fallback. |
| Edition fallback | Committed (`6968b87`) and deployed to Unraid. |
| Full fetch run on Unraid | **Stopped, not finished.** Last recorded state: 7 placed, 89 held as lossy, 1 failed. Not restarted. |
| Tests | 566 passing locally at `6968b87` (`pytest -q`, 26 s). |
| Web UI, card sync, scheduler, notifications | Not built (they are in the design, milestones 3 to 6). |
| M2 exit test (a real release seen by Music Assistant, Jellyfin and Navidrome) | **Not met.** |

The library is almost all lossless, and that is the core constraint. Tidal licenses lossless per release, and about 23% of a random planned sample served lossless. Lidarr's own Tidal listing lands at about 27% FLAC, so most of the gap is the catalogue.

---

## 2. Goals and scope

**In scope**
1. Request an artist or album from Tidal's catalogue.
2. Auto-grab new releases from followed artists (milestone 4).
3. Discovery and suggestions (milestone 5).
4. Quality upgrades and a lossy fallback (milestones 4 and 5).
5. FiiO Echo Snowsky Mini card sync (milestone 2b, not built).

**Hard rules**
- Never delete a user file. Replacements go through a trash directory.
- Dry run is the default for every pipeline stage.
- Cadence runs beside Lidarr until cutover. Lidarr is stopped, not deleted, at cutover.

---

## 3. Where things live

**On the Mac:** `~/music-manager` (git repo, branch `fetch-ledger-planner`, local only as far as this session knows).

```
cadence/
  cli.py                 every subcommand (argparse)
  __main__.py            python -m cadence
  report.py              owned / missing report; edition choice for a release group
  sources/
    base.py              DTOs: ArtistRef, ReleaseDTO, TrackDTO; release_type; gated_call
    tidal.py             the only module that imports tidalapi (session, timeouts, paging, cache)
    musicbrainz.py       optional enrichment (tiebreak only)
  gate/
    bucket.py            Tidal gate: token bucket, 429 handling, pause ladder
  identity/
    normalize.py         norm_title, norm_artist (+ version handling)
    matcher.py           match_file, AmbiguousMatch, 1e-6 tie tolerance, cluster collapse
    grouping.py          cluster_recordings: exact title, ISRC union, album-prefix pass, remaster-year pass
  catalog/
    sync.py              catalogue sync into the ledger
    link.py              link_files: the only identity resolver; link_review writes
    groups.py            _albums, _regroup_recordings (writes canon_id only)
    candidates.py        ambiguous-artist candidate lists
    alias.py             artist aliases (tag name -> artist)
  fetch/
    plan.py              Plan(items, owned, skipped, collisions); plan_missing; partial scope
    run.py               fetch_items, _fetch_one, _probe_editions, placement, verify
    stream.py            resolve_stream (playbackinfopostpaywall), DASH/direct manifests, ffmpeg remux
  ledger/
    db.py                connection, migrations runner, log_event
    migrations/          001_init ... 006_link_review (plain numbered .sql)
    naming.py            album_dir, track_file (the only folder/file naming template)
    quality.py           lossless / lossy classification, quality counts
    legacy.py            import-legacy from a library scan JSONL
  tags/
    albumartist.py       album-artist rules (audit-tags)
docs/                    DESIGN.md (older), M1_SPEC.md, M2_SPEC.md, this file
m1/scan_library.py       read-only library scan (produces library_scan.jsonl)
spikes/                  throwaway experiments (s1_catalog, s4 YouTube Music, FiiO helper scripts)
tests/                   pytest suite (566 tests), fakes, seeded generator (ledger_gen.py)
data/                    local working files and an older DB snapshot (see section 13)
```

**On Unraid (Tower, `100.69.34.110` over Tailscale; `10.0.0.48` on the LAN):**

| Path | Purpose |
|------|---------|
| `/mnt/user/Movies_TV/Music` | the live library (the array; mounted as `/library`) |
| `/mnt/user/Movies_TV/_fetch_staging` | staging for fetches (on the cache pool; mounted as `/staging`) |
| `/mnt/cache/appdata/cadence` | app code, `cadence.db`, `data/` backups and run logs (mounted as `/app`) |
| `/app/secrets/token.json` | Tidal token file (inside the appdata mount). **Never print its contents.** |

Containers use the image `ytdlp-fallback:local`, run as `--user 99:100`, with `HOME=/tmp`, `PYTHONPATH=/app/.pydeps:/app` and `CADENCE_TIDAL_TOKEN=/app/secrets/token.json`. Staging and library are on different filesystems, so cross-device placement is used there (section 8).

---

## 4. Ledger data model

Plain SQLite in WAL mode. Migrations are numbered `.sql` files applied in order and recorded in `schema_version`.

| Migration | Adds |
|-----------|------|
| 001_init | `artist`, `release`, `track`, `recording`, `file`, `want`, `card`, `card_state`, `job`, `event_log`, `catalog_cache`, `release_source`, `suggestion` |
| 002_groups | release groups (`group_key`) and recording clusters (`canon_id`) |
| 003_alias | `artist_alias` |
| 004_quality | quality tracking on `file` |
| 005_num_volumes | `release.num_volumes` (from Tidal `numberOfVolumes`) |
| 006_link_review | `link_review` (reason CHECK allows `ambiguous` and `no_longer_matches`; candidates as JSON) |

Key rules:
- **Recording clusters.** A cluster is `COALESCE(recording.canon_id, recording.id)`. Ownership and planning are per cluster, not per recording row.
- **Ownership.** A cluster is owned when at least one `file` row has `recording_id` set to a recording in it, library-wide. Nothing else counts. Files matched by tags, names, durations or folders do not count.
- **Track identity.** ISRC is the strongest key. Files without ISRC match on normalised (artist, title, duration).
- **`file` has no Tidal id column.** The Tidal track id a fetch used is recorded only in the `fetch.placed` event.

The local `data/cadence.db` is an older snapshot without `link_review`. Use a read-only copy of the live DB for current numbers (section 12).

---

## 5. Identity

**Principle:** a track is identified by recording, and recordings are clustered. "Do I own it?" is a track-level question. Releases are views onto tracks.

### 5.1 Clustering (`identity/grouping.py`)
Recordings collapse into one cluster when:
- Exact `norm_title` matches within **3 s** (`CLUSTER_TOLERANCE_S`), or
- They share an ISRC (ISRC union).
- Variant scope key and remaster preference are respected: live, instrumental, slowed and similar stay separate.

Two extra passes were added and narrowed after review:
- **Album-prefix pass.** A title like `Giant Steps (Alternate Take 8)` can merge with a plain same-album track only if the titles match after cutting the album prefix, within **1 s** (`ALBUM_TITLE_TOLERANCE_S`), and the track is at least **60 s** (`ALBUM_TITLE_MIN_DURATION_S`). It only merges within the same album group, and only singletons. The first version over-merged, so it was narrowed.
- **Remaster-year pass.** `Song (2009 Remaster)` merges into `Song` within **3 s**, singletons only, with the same 60 s floor.

`cadence regroup` recomputes groups and clusters without network. It writes `canon_id` only.

### 5.2 Canonical recording
The canonical recording of a cluster is the remaster with the latest year (year from the remaster tag, else the release date year; ties go to the latest date, then the highest id). Otherwise it is the earliest original.

### 5.3 Matching files (`identity/matcher.py`)
`match_file` scores a file against candidate recordings. Ties are compared with **1e-6** tolerance (float ties were being missed without it). Candidates in the same cluster collapse to one.

### 5.4 Linking (`catalog/link.py`, `cadence link`)
`cadence link` is the **only** identity resolver. The planner never matches files.

Rules:
- A single closest recording links. On an equal distance the lowest id wins.
- Two or more distinct clusters at the best rank is a **tie**. The file stays unmatched and gets a `link_review` row (reason `ambiguous`).
- The same ISRC on recordings of different clusters is also a tie.
- `cadence link --rematch` re-evaluates every file whose `match` is not `manual`. Manual links, including every fetched file, are never touched.
- A file already linked keeps its link when a rematch finds a tie. Only a review row is written, so a tie never takes away something the user owns.
- A linked file that matches nothing on rematch (its tags changed) keeps its link. It gets a review row with reason `no_longer_matches`.
- A unique rematch that moves a linked file to another recording is a correction. It is not reviewed.
- Resetting a file's link (`alias add`, or `import-legacy` seeing a changed artist) also deletes its `link_review` row.
- `cadence link --review` lists review rows and changes nothing.

Run `cadence link --rematch` once after upgrading to this rule.

---

## 6. Catalogue source and the Tidal gate

### 6.1 Source (`sources/tidal.py`)
- The only module allowed to import `tidalapi` (0.8.11).
- `load_session(token_path)` logs in from a tidal-dl-ng style token file. **Read-only**: refreshed tokens are never written back.
- `install_timeout` mounts a transport adapter. Connect/read timeout is `(10, 30)`, because tidalapi's session has none. The adapter turns a 429 into `requests.HTTPError` so the gate sees it on every path, including token refresh.
- Releases come from `artists/{id}/albums` with filters `None`, `EPSANDSINGLES` and `COMPILATIONS`. The first bucket wins on duplicates. Pages are 50 (albums) and 100 (tracks), capped at 40 pages per listing.
- Responses are cached as JSON in `catalog_cache` with a 7-day age limit.
- DTOs are built from raw JSON, not tidalapi objects. tidalapi drops `isrc` and `version` for non-streamable tracks, which would merge unavailable variants into the main recording.
- Only **album artist == the artist** counts as the artist's own release. `get_other` also returns "appears on" releases.

### 6.2 Gate (`gate/bucket.py`)
One token bucket for every Tidal call (sync, search, playback, download). Defaults: 1 request per 2 s, burst 3, from the `sync-catalog` flags.

On 429 the gate pauses **all** Tidal work. The back-off ladder is 15 min, then 1 h, then 4 h. `Retry-After` is honoured if present, with a 60 s minimum. One notification per pause, not per job. Exit code 3 means paused.

---

## 7. Fetch planner (`fetch/plan.py`)

`cadence fetch` with no `--execute` is a dry run. It prints the plan and makes no network call, no login and no write.

### 7.1 Buckets
`plan_missing` returns a `Plan` with three buckets. Each cluster of the artist sits in exactly one:
- `owned`: counted, not listed.
- `items`: planned for download.
- `skipped`: listed with a reason and the Tidal id, and counted per reason.

### 7.2 Scope
- **Default `partial`**: a release group is in scope only if a linked file's recording is a track on a release of that group. Owning one song does not pull in a box set that also carries it.
- **`--scope all`**: everything.
- **`--release ID`**: an explicit request.

### 7.3 Which track is planned
A missing cluster becomes one item, using the canonical recording's Tidal track. Album beats EP beats single beats compilation.

### 7.4 Skip reasons
`partial scope: release has no owned track`, `variant (use --include-variants)`, `unavailable on Tidal`, `no tidal id`, `hidden release`, `appears-on release (name it with --release)`, `release not in --release`, `artist not matched to Tidal (set it with cadence artist set-tidal)`, `shared recording, planned under <artist>`.

Only artists with `tidal_match` of `auto` or `manual` are planned.

### 7.5 Naming
Target is `Artist/Album/NN - Title.ext`, from `ledger/naming.py`. Edition and remaster markers are stripped from the album folder. The title keeps Tidal's version (`Song (2009 Remaster)`). Multi-disc releases get a `D{disc}-` prefix. The disc count is Tidal's `numberOfVolumes`, floored by the highest disc number seen on the release.

Keep this layout. The FiiO card scripts depend on it.

### 7.6 Collisions
A **collision** is a planned target whose path equals the path of a file the ledger links to a different recording. It is listed, not hidden, and it is a `conflict` at execute. It is never overwritten.

A **duplicate target** (two planned tracks of different recordings with the same path, compared case-insensitively) is listed too. The later one is the conflict.

Ambiguous unlinked files own nothing. Their clusters are planned. A candidate whose target equals the file's own path is a conflict. Any other candidate is a second copy.

---

## 8. Fetch executor (`fetch/run.py`, `fetch/stream.py`)

### 8.1 Per item
1. If the target path is already linked to this recording, the item is `exists`. No Tidal call.
2. If the target path is occupied by anything else, the item is a `conflict`. Nothing is fetched.
3. Gate call: playback request (`playbackinfopostpaywall`, with the requested `audioquality`).
4. Gate call: download to `staging/<target>.part`, then an atomic rename.
5. Verify: ffmpeg decodes the file, and its duration is within **3 s** (`DURATION_TOLERANCE_S`) of Tidal's. Verify happens before any replace.
6. Tag with mutagen: FLAC gets ALBUMARTIST (primary artist), TITLE, TRACKNUMBER, DISCNUMBER, ALBUM and ISRC. M4A gets the equivalent atoms.
7. Place into the library.
8. Insert the `file` row (`source=tidal`, `match=manual`, `match_score=1`, `quality_requested`, `quality_received`). Write the `fetch.placed` event. Commit.

### 8.2 Placement
- **Same filesystem:** `os.link` (no-clobber), then unlink the staged copy.
- **Different filesystem (Unraid staging vs library):** copy to a `.part` file in the library, fsync, check the size, `os.link` into place (no-clobber), and unlink the source only after the link is committed.
- Preflight probes hard links. It refuses a staging directory inside or equal to the library.
- The executor never overwrites and never deletes.

### 8.3 Stream resolution (`fetch/stream.py`)
- `resolve_stream` returns `StreamInfo`. It refuses encrypted streams, previews, non-stereo audio and manifest layouts it does not understand. It **never decrypts**.
- Direct-URL manifests are downloaded as is.
- DASH (hi-res) manifests are fetched as init plus numbered segments by plain GET (capped at 5000 segments), then remuxed by ffmpeg with stream copy into `.flac` or `.m4a`.
- Quality ladder: `HI_RES_LOSSLESS`, `LOSSLESS`, `HIGH`. LOW is not offered.

### 8.4 Quality gate
- For LOSSLESS and HI_RES_LOSSLESS requests, a lossy (AAC) result is **held**: staged as `<stem> [tidal <id>].m4a`, logged as `fetch.held_lossy`, with no `file` row and no library write.
- HIGH requests are exempt and place lossy files.
- Once a FLAC replaces a held copy, the held AAC is removed (`fetch.stale_held_removed`), but only after the FLAC is in the library and its row is committed.

### 8.5 Other editions (the fallback, `6968b87`)
Tidal licenses lossless per release. The same recording can be lossless on one edition and AAC on another.

When a lossless request is served lossy, the executor asks the other Tidal tracks of the same recording:
- Same version: `norm_title(title, version)` equals the planned one's, so a live cut that was merged in is never asked.
- Available on Tidal.
- Duration within 3 s of the planned track's (or the alternate's own duration if the planned one is unknown). If neither is known, no duration check is applied.
- Albums first, then the lowest Tidal id.
- At most **`MAX_EDITION_PROBES` = 4** per item, one playback request each through the gate.

The first lossless alternate is downloaded. The file keeps the planned path and ledger row. `fetch.placed` carries `tidal_track_id` (used) and `planned_tidal_track_id`. Each probe logs `fetch.edition_probe`. If none is lossless, the planned track is held and `fetch.held_lossy` lists `alternates_tried`.

HIGH requests never probe. A track already held in staging is probed again only for other editions; its own playback is not requested again.

### 8.6 Stops and exit codes
| Exit | Meaning |
|------|---------|
| 0 | Normal finish. Dry run always returns 0, even with conflicts (deliberate: it is a preview). |
| 1 | Execute finished with failures or conflicts. Also a stop on an auth failure. |
| 2 | Configuration error. |
| 3 | Paused: 429, or the gate is paused. |

Other rules:
- A 401 on one track's playback request logs `fetch.blocked` and continues, unless the body names the token or session, which stops the run (exit 1).
- Three dead connections in a row stop the run.
- `--limit N` cuts attempts after planning. The CLI says how many planned items were not attempted.

---

## 9. Quality and the account

- **Default quality:** `HI_RES_LOSSLESS`. Lossy results are held for LOSSLESS and HI_RES_LOSSLESS requests.
- **Account:** the token's account reports `PREMIUM` in the API. The owner says they have HiFi, and one screenshot of the Tidal app showed a Student plan. This is unresolved, so treat plan details as unverified until the owner confirms the subscription.
- **Measured:** famous tracks serve `HI_RES_LOSSLESS`. A random 30-track planned sample served lossless for 7 (23%). Lidarr's Tidal listing is about 27% FLAC and 73% AAC.
- **Conclusion so far:** most of the lossy gap is Tidal's licensing.
- **Dolby Atmos:** 4 tracks failed the duration check in the canary. Recommendation: skip Atmos as a variant. **Not built.**
- **AAC 320 per-album opt-in:** proposed, **not built**. Lossless-only remains the policy.

---

## 10. CLI reference

Every subcommand takes `--help`. The main ones:

| Command | What it does | Network |
|---------|--------------|---------|
| `cadence init` | Create or migrate the ledger | no |
| `cadence import-legacy PATH` | Import a library scan JSONL | no |
| `cadence sync-catalog [--artist NAME \| --all] [--max-calls N] [--interval S] [--burst N] [--force]` | Pull Tidal catalogues into the ledger | yes |
| `cadence candidates [--artist NAME] [--json] [--musicbrainz]` | Compare ambiguous artists' Tidal profiles with owned albums | yes |
| `cadence link [--artist NAME] [--rematch] [--review]` | Match files to recordings; `--review` lists `link_review` only | no |
| `cadence regroup [--artist NAME]` | Recompute release groups and clusters | no |
| `cadence report [--artist NAME] [--json]` | Owned and missing report | no |
| `cadence artist set-tidal NAME TIDAL_ID` | Pin an artist to a Tidal id | no |
| `cadence alias add ALIAS TARGET [--note]`, `alias list`, `alias remove ALIAS` | Map a tag name to an artist | no |
| `cadence fetch [--artist NAME \| --release ID] [--scope partial\|all] [--quality Q] [--staging DIR] [--library DIR] [--execute] [--limit N] [--include-variants]` | Plan (dry run) or execute the fetch | only with `--execute` |
| `cadence cache-clear [--artist NAME]` | Forget cached Tidal responses | no |
| `cadence quality` | Lossless and lossy file counts | no |
| `cadence audit-tags` | Album-artist tags a primary-artist rule would change (read-only) | no |
| `cadence gate-status` | Show whether the Tidal gate is paused | no |

Environment variables: `CADENCE_DB`, `CADENCE_LIBRARY` (default for `--library`), `CADENCE_STAGING` (default for `--staging`), `CADENCE_TIDAL_TOKEN` (path to the token file). **No paths are built in.**

---

## 11. Tests

- **Suite:** 566 tests at `6968b87` (`.venv/bin/python -m pytest -q`, about 26 s). Zero live Tidal calls in tests.
- **Fakes:** `tests/fakes.py` (gate and sources), `tests/fetch_fakes.py` (fetch sources).
- **Seeded generator:** `tests/ledger_gen.py` builds random ledgers. `tests/test_plan_invariants.py` checks planner invariants over them (every cluster in exactly one bucket, collisions listed, no silent skips, and so on).
- **Key files:** `test_grouping.py`, `test_groups.py`, `test_matcher.py`, `test_link_review.py`, `test_fetch_plan.py`, `test_fetch_run.py`, `test_fetch_stream.py`, `test_tidal_source.py`, `test_gate.py`, `test_will_paquin_e2e.py`.
- **Real-library checks:** run read-only against `m1/data/library_scan.jsonl`. Nothing is written to the library.

Development workflow: edits are test-first, and a reviewer pass runs after each non-trivial change. Earlier review rounds found silent drops, owned-track duplicates, false merges, deletion ordering and held-copy overwrite. Each has a regression test.

---

## 12. Deployment and runbook

### 12.1 Deploy pattern (used on 2026-10-27)
1. Back up the code: `data/code-pre-<rev>.tgz` on Unraid.
2. Back up the DB with `cp -p`: `data/cadence.db.bak-pre-<tag>`.
3. Deploy with a tar from the Mac.
4. Run migrations (`cadence init`), then `cadence link --rematch` once when identity rules changed.
5. Confirm the suite on the Mac first.

### 12.2 Reading the live DB safely
Copy with `scp` to a local snapshot, then open read-only and immutable (WAL mode):

```python
sqlite3.connect("file:<path>?mode=ro&immutable=1", uri=True)
```

Do not open the live file in write mode while a fetch is running.

### 12.3 Running a fetch on Unraid
- Dry run first: `cadence fetch` with `--library`/`--staging`, no `--execute`. Check the collisions and the conflict count.
- Execute only with the owner's explicit go-ahead.
- Run detached, with output to a log under `data/`. The last run used `data/fetch-full2-2026-10-27.txt`, and its container was stopped at exit 137 (SIGKILL) before it finished.
- To restart, first confirm the current settings: lossless only, held AAC kept, edition fallback on. **The owner has not yet confirmed a restart.**

### 12.4 Stopping safely
Stopping a run leaves it resumable: placed files are committed and held copies stay in staging. Check the staging directory and the `fetch.*` events before restarting.

---

## 13. Local data

`data/` on the Mac holds working files, not authoritative data:
- `cadence.db` and its WAL/SHM files: an **older snapshot** (no `link_review` table, 30 linked files). Refresh from Unraid before using it for numbers.
- `unraid_snapshot.db`: a copy taken from Unraid for read-only checks.
- `fetch-list-2026-10-26.csv`, `gaps-2026-10-26.csv`, `one_short-2026-10-26.csv`: planning exports.
- `sync_all.log`, `sync_all_2.log`: catalogue sync logs.

---

## 14. Known issues and open items

**Fallback (`6968b87`), unfixed**
- A commit that fails after the file is placed is reported as failed. The file is in the library, so the report is wrong.
- An auth stop during an edition probe is reported as "not attempted".
- Some note text says "not fetched again" where it should say something else.

**Conflicts and review**
- About **38 to 42 conflicts** from duplicate-target and linked-collision cases need human review.
- Held files from before the `[tidal <id>]` rename (unprefixed `.m4a`) are not recognised as held.
- A track Tidal blocks (401) prints `blocked`, but the exit code is unchanged (pre-existing).

**Not built**
- `--retry-held` flag, needed before any upgrade or any change in what Tidal serves.
- Skip Dolby Atmos tracks as variants (recommended, section 9).
- AAC 320 per-album opt-in (proposed, section 9).
- Import path for FLAC files the owner buys or rips (offered, not confirmed).
- The entire web UI, card sync, scheduler, notifications and fallback worker (milestones 2b to 6).

**Cleanup**
- Smoke leftovers on Unraid (`_fetch_smoke`, `data/smoke.db`) await the owner's OK to delete. **Do not delete without that OK.**

**Known limitations**
- The dry run lists every skipped track on its own line. It is long for an artist with many releases the owner does not own.
- A library folder spelled differently (`DECADE` vs `Decade`) is not reused. New files go to the ledger-named folder.
- An ambiguous unlinked file owns nothing, so every candidate cluster is planned. Run `cadence link` and check `cadence link --review` before `fetch --execute`.

---

## 15. Standing constraints

- **Never print** the contents of `/app/secrets/token.json` or any other secret value.
- **No `fetch --execute`** without the owner's explicit go-ahead in that conversation.
- **Never delete** a user file. Smoke leftovers need an explicit OK.
- **No deletes or overwrites** in the executor, ever.

---

## 16. Decisions

| Decision | Date | Notes |
|----------|------|-------|
| Tidal is the source of truth; YouTube Music is a fallback and gap-finder only | 2026-10-25 | MusicBrainz and Discogs are optional enrichment. |
| `cadence link` is the only identity resolver; the planner never matches files | 2026-10 | Option C. Invariant tests (option D). |
| Folder naming stays `Artist/Album/NN - Title.ext` | 2026-10-26 | The FiiO card scripts depend on it. `[Year]` prefix deferred. |
| Default edition is the latest remaster | 2026-10-26 | Originals are kept, never deleted. |
| Partial scope by default | 2026-10-27 | Release-level rule; box sets do not pull in. |
| Lossless-only placement; AAC held | 2026-10 | Fallback probes other editions first. |
| Own in-house fetch (`stream.py` and `run.py`); no tiddl or tidal-dl import in the code. Only the tidal-dl-ng token file format is read (`CADENCE_TIDAL_TOKEN`) | 2026-10 | `DESIGN.md` §8b (tiddl as the executor) is superseded. tiddl's skip logic is path-based, so the ledger has to decide what to fetch. |
| Soulseek integration, Telegram-bot sources and OrpheusDL as a replacement for the Tidal fetch: **not built** | 2026-10-27 | This was the assistant's decision, and the owner disagreed. Revisit only with an explicit decision by the owner. |

**Superseded in `DESIGN.md`:** Python 3.12 is now the 3.13 `.venv` in use; the downloader is the in-house fetch, not tiddl (§8b) or tidal-dl; the "Gap sweep" and "Fallback" milestones stay as designed.

---

## 17. Milestones

| M | Deliverable | Status |
|---|-------------|--------|
| 0 | Spikes (catalogue, tiddl, rate limits, YT Music) | Done |
| 1 | Ledger, legacy import, catalogue sync, matching | Built. Will Paquin report was the exit test. |
| 1b | Gap sweep (YouTube Music vs ledger) | Spike done (`s4_*.py`). Full report not built. |
| 2 | Fetch pipeline (plan, gate, verify, tag, place, hooks, dry run) | Built, hooks not built. Exit test (MA, Jellyfin, Navidrome) **not met**. |
| 2b | Card profile and Mac agent | Not built |
| 3 | Web UI skeleton and PWA | Not built |
| 4 | Auto-grab, upgrade, follow, digest notifications | Not built |
| 5 | Fallback worker (YouTube Music) and FiiO and Library screens | Not built |
| 6 | Cutover beside Lidarr and Aurral | Not built |

---

## 18. Git history (branch `fetch-ledger-planner`)

| Commit | Summary |
|--------|---------|
| `6968b87` | Fetch: try other Tidal editions when a track serves lossy (the edition fallback, plus later hardening) |
| `600a9bb` | Partial scope: a release is partial only with a linked track of its own |
| `30999c8` | Merge remaster-year marked recordings with their plain twins |
| `639564a` | Merge same-album recording pairs whose titles differ only by album prefix |
| `1d8e618` | Ledger-only fetch planner, link as sole identity resolver, invariant tests |
| `8fde791` | WIP: fetch planner hardening (known open findings, not for `--execute`) |
| `98acd03` | Disc counts from Tidal volumes, stricter legacy-file matching |
| `52333ec` | Fetch: disc-aware names, atomic no-clobber placement |
| `f342577` | Fetch: lossless-only placement, held files, cross-device safe placement |
| `d2a54e8` | Fetch: partial scope by default, reuse library album folders, duplicate guard |
| `70c9c5b` | M2 fetch pipeline: plan, dry run, guarded real run |
| `6ea82f9` | Default to the latest remaster edition |
| `af53704` | Quality tracking, MusicBrainz tiebreaker, naming and album-artist rules |
| `928d25d` | Matcher: album-prefix, live-variant and claimed-album tolerance tiers |
| `2d879ea` | Add artist aliases: map tag names to existing artists |

Working tree was clean at `6968b87`. The hardening changes that were pending earlier are part of that commit.

---

## 19. Glossary

- **Recording:** one performance of a song, identified by ISRC where Tidal provides one.
- **Cluster (canon):** recordings that are the same song across editions. `COALESCE(canon_id, id)`.
- **Release group:** the albums and editions that are the same release (`group_key`).
- **Owned:** a cluster with at least one `file` row linked to it.
- **Held:** a lossy result kept in staging, not placed in the library.
- **Conflict:** a target path taken by something the ledger does not link to this recording.
- **Collision:** a planned target that equals another recording's linked path, or another planned track's path (case-insensitive).
- **Partial scope:** only release groups where the user already owns a track of that release.
