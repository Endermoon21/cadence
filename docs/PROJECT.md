# Cadence: complete project documentation

Self-contained reference for putting Cadence into another Claude chat or project. Snapshot: **2026-10-27**, repo `~/music-manager` at commit `ab49bb5` on branch `fetch-ledger-planner`. Where this disagrees with the code, the code wins. No secrets are in this file; secrets are only ever named by variable.

---

## 0. TL;DR

Cadence is a **private music ledger and fetcher for one person** (the owner, "Salt Father"). It replaces Lidarr + Aurral. Tidal's catalogue is the source of truth, a SQLite ledger records which recordings are owned, and a planner works out what is missing and downloads it. The library is **lossless-only**: when Tidal serves a track as lossy AAC, Cadence now tries other Tidal editions, then OrpheusDL and Soulseek (behind a Mullvad VPN), and only then holds the AAC in staging.

| Area | State |
|---|---|
| Ledger, legacy import, Tidal catalogue sync | Built (migrations 001-006) |
| Identity (grouping, matcher, `cadence link`) | Built, reviewed, deployed |
| Fetch planner (dry run) | Built |
| Fetch executor (`--execute`), lossless gate, held AAC, edition fallback | Built, deployed to Unraid at `6968b87` |
| **OrpheusDL + Soulseek fallbacks, Mullvad VPN gate** | **Built and committed (`ab49bb5`), 767 tests pass, NOT deployed, never run against a real slskd/OrpheusDL** |
| Koi-pond web UI | Designed only (canvas + handoff doc); no web code exists |
| FiiO card sync, scheduler, notifications, post-placement hooks | Not built |
| M2 exit test (a fetched release visible in Music Assistant, Jellyfin and Navidrome) | Not met |
| Full fetch run on Unraid | Stopped early: 7 placed, 89 held as AAC, 1 failed. Not restarted. |

---

## 1. How to access the code

### 1.1 Where it lives

| What | Where | Notes |
|---|---|---|
| **Main repo (Mac)** | `/Users/Ender/music-manager` | The working copy. Python 3.13.9 venv at `.venv/`. |
| **GitHub remote** | `https://github.com/Endermoon21/cadence` (`origin`) | **Private** (an unauthenticated API call returns 404). Only `main` is pushed, and `origin/main` is at `d2a54e8`, **10 commits behind** the current branch. |
| **Current branch** | `fetch-ledger-planner` (HEAD `ab49bb5`) | **Local only. Never pushed.** Also local: `fetch-planner-wip` (`8fde791`, an abandoned WIP). |
| **UI design repo** | `/Users/Ender/cadence-koi-ui` and `https://github.com/Endermoon21/cadence-koi-ui` | Private. One commit, `fd97a25`. Design source only. |
| **Live design canvas** | https://claude.ai/artifact/6YfnW56BrZAwfcMyhB8yPh | Anyone-with-the-link. Same files as `cadence-koi-ui/design/`. |
| **Production (Unraid)** | host `Tower`: LAN `10.0.0.48`, Tailscale `100.69.34.110` | Runs Cadence in containers. Code there is at `6968b87` (before the fallbacks). |

> **Consequence:** the only complete copy of the latest code is the Mac working tree. If the Mac is lost, `ab49bb5` and 9 earlier commits are gone. Pushing `fetch-ledger-planner` to `origin` is the fix (not done yet, because it was not requested).

### 1.2 Get a working copy

```bash
# from GitHub (gets main only, 10 commits old; the branch must be pushed first to get the latest)
gh auth login -h github.com          # the saved gh token is currently invalid
git clone https://github.com/Endermoon21/cadence.git && cd cadence

# on the Mac, the real thing
cd ~/music-manager
git switch fetch-ledger-planner
```

### 1.3 Run it

```bash
cd ~/music-manager
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # deps: tidalapi==0.8.11, mutagen; dev: pytest
.venv/bin/python -m pytest -q                                   # 767 tests, ~33 s, zero live network
.venv/bin/python -m cadence --help
```

`pyproject.toml` requires Python >= 3.12 and exposes the console script `cadence = cadence.cli:main`. Pytest config: `testpaths = ["tests"]`, `pythonpath = ["."]`. External tools needed at runtime: **ffmpeg** (verify + DASH remux), and for fallbacks a separate OrpheusDL checkout and a slskd + gluetun stack.

### 1.4 Reaching Unraid (production)

- SSH as `root` to `100.69.34.110` (Tailscale; it may ask for a browser check) or `10.0.0.48` (LAN). The password and all other secrets are in `~/.claude-secrets/credentials.env` (chmod 600) and referenced as `$VARIABLES`. **Never paste their values into chat or a file.** The owner's Obsidian vault `~/.claude/context/` (`unraid.md`, `infrastructure.md`) has the host reference.
- Paths on Unraid: library `/mnt/user/Movies_TV/Music` (mounted as `/library`), staging `/mnt/user/Movies_TV/_fetch_staging` (`/staging`), app + DB + backups `/mnt/cache/appdata/cadence` (`/app`), Tidal token file `/app/secrets/token.json` (**never print it**).
- Containers use image `ytdlp-fallback:local`, run `--user 99:100`, with `HOME=/tmp`, `PYTHONPATH=/app/.pydeps:/app`, `CADENCE_TIDAL_TOKEN=/app/secrets/token.json`. Staging and library are on different filesystems, so placement is cross-device (copy + link).
- Reading the live DB safely (WAL mode): `scp` it to the Mac, then `sqlite3.connect("file:<path>?mode=ro&immutable=1", uri=True)`. Never open the live file for writing while a fetch runs.

### 1.5 What to give another Claude chat

Give: this file, `DESIGN.md`, `docs/FALLBACK_SPEC.md`, `cadence-koi-ui/HANDOFF.md`, `cadence-koi-ui/tokens.css`, and optionally `cadence/` + `tests/` (about 14k lines of Python, no secrets).
Do **not** give: `data/` (copies of the library database and logs), `.venv/`, `~/.claude-secrets/`, any `token.json`, any `*.env`, `m1/data/`, `spikes/out/`. These are all in `.gitignore` except the secrets folder, which is outside the repo.

### 1.6 Related documents

| File | What it is | Status |
|---|---|---|
| `DESIGN.md` | Original design: goals, architecture, data model, FiiO card sync, backend plan (§11), UI plan (§12), milestones (§13) | Header says "no code yet" and §3 says Python 3.12 + tiddl: **both out of date**. The visual-direction decision (§12.4) is now settled: koi pond. |
| `docs/CADENCE.md` | Earlier (2026-10-27) reference snapshot | **Untracked in git.** Predates the fallbacks and UI work. This file supersedes it. |
| `docs/M1_SPEC.md`, `docs/M2_SPEC.md` | Milestone specs | Historical. |
| `docs/FALLBACK_SPEC.md` | Spec for the OrpheusDL/Soulseek/Mullvad work | Current, matches the code. |
| `cadence-koi-ui/HANDOFF.md` | Full UI design handoff | Current. |

---

## 2. Goals, scope and hard rules

**In scope:** request an artist/album from Tidal's catalogue; auto-grab new releases from followed artists (milestone 4); discovery (5); quality upgrades and a lossy fallback (4-5); FiiO Echo Snowsky Mini card sync (2b). Interface planned as a phone-first web app plus ntfy notifications.

**Out of scope for now:** Usenet/torrents, multi-user accounts, video, audiobooks/podcasts, a Lidarr-compatible API, playback (Music Assistant, Navidrome and Jellyfin keep playing).

**Hard rules (never break these):**
1. Never delete or overwrite a user file. Replacements go through a trash directory; the executor never overwrites and never deletes.
2. Dry run is the default for every pipeline stage.
3. **No `fetch --execute` without the owner's explicit go-ahead in that conversation.**
4. Never print secrets (the Tidal token file, API keys, the slskd/gluetun keys).
5. Cadence runs beside Lidarr until cutover; Lidarr is stopped, not deleted, at cutover.
6. Smoke-test leftovers on Unraid (`_fetch_smoke`, `data/smoke.db`) are deleted only with the owner's explicit OK.

**Why it exists:** Lidarr only sees MusicBrainz, so about 8 of 20 Will Paquin releases on Tidal were invisible; Lidarr and Aurral drifted onto wrong quality profiles; imports got stuck at "album match 76.5%"; Lidarr searches got the Tidal account 429'd; and there was no track-level identity (duplicates, remixes, slowed versions).

---

## 3. Architecture and code map

```
Tidal catalogue --sync--> SQLite ledger <--link-- legacy library files (tags, ISRC, length)
                              |
                          fetch planner (what is missing?)
                              |
                      Tidal gate (one token bucket)
                              |
   resolve stream -> download to staging -> verify (ffmpeg) -> tag (mutagen) -> place in library
                              |
        lossy? -> other Tidal editions -> OrpheusDL -> Soulseek (VPN gate) -> else hold AAC in staging
```

```
cadence/                               (~6k lines)
  cli.py              491   argparse, every subcommand, env handling
  report.py           218   owned/missing report; edition choice for a release group
  sources/
    base.py            92   DTOs (ArtistRef, ReleaseDTO, TrackDTO), release_type, gated_call
    tidal.py          209   the ONLY module that imports tidalapi (session, timeouts, paging, cache)
    musicbrainz.py     95   optional tiebreak enrichment
  gate/bucket.py      151   Tidal gate: token bucket, 429 handling, pause ladder
  identity/
    normalize.py      183   norm_title, norm_artist, VARIANT_WORDS
    matcher.py        180   match_file, AmbiguousMatch, 1e-6 tie tolerance
    grouping.py       184   recording clusters + release groups
  catalog/
    sync.py           396   catalogue sync into the ledger
    link.py            79   link_files: the ONLY identity resolver; link_review
    groups.py         107   _albums, _regroup_recordings (writes canon_id only)
    candidates.py     251   ambiguous-artist candidates
    alias.py           84   artist aliases (tag name -> artist)
  fetch/
    plan.py           292   Plan(items, owned, skipped, collisions); plan_missing; partial scope
    run.py            755   executor: _fetch_one, edition probes, _place, _try_fallbacks
    stream.py         157   resolve_stream (playbackinfopostpaywall), DASH/direct manifests, ffmpeg remux
    fallback.py       503   OrpheusFallback, SoulseekFallback, VpnDown, fallbacks_from_env
  ledger/
    db.py              68   connect, migrate (numbered .sql), log_event, settings
    naming.py          29   album_dir, track_file: the ONLY folder/file naming template
    quality.py         77   lossless/lossy classification, quality counts
    legacy.py          75   import-legacy from a library scan JSONL
    migrations/            001_init ... 007_fallback_sources
  tags/albumartist.py 75    album-artist rules (audit-tags)
tests/                (~7.5k lines, 767 tests) plus fakes.py, fetch_fakes.py, ledger_gen.py
deploy/slskd/run-slskd.sh   slskd behind gluetun/Mullvad (Unraid)
m1/scan_library.py          read-only library scan -> library_scan.jsonl
spikes/                     throwaway M0 experiments
docs/                       CADENCE.md, PROJECT.md, FALLBACK_SPEC.md, M1_SPEC.md, M2_SPEC.md
```

**Design rule:** `sources/*` are the only modules allowed to import tidalapi (or any downloader). `cadence link` is the only thing that matches files to recordings; the planner never matches files.

---

## 4. Data model

Plain SQLite in WAL mode. Migrations are numbered `.sql` files applied in order inside a transaction and recorded in `schema_version`.

| Migration | Adds |
|---|---|
| 001_init | `artist`, `release`, `track`, `recording`, `file`, `want`, `card`, `card_state`, `job`, `event_log`, `catalog_cache`, `release_source`, `suggestion`, `setting` |
| 002_groups | release groups (`group_key`) and recording clusters (`canon_id`) |
| 003_alias | `artist_alias` |
| 004_quality | `file.quality_requested`, `file.lossy`, `file.quality_received` |
| 005_num_volumes | `release.num_volumes` (Tidal `numberOfVolumes`) |
| 006_link_review | `link_review` (reason `ambiguous` / `no_longer_matches`; candidates as JSON; `ON DELETE CASCADE` from `file`) |
| **007_fallback_sources** | widens `file.source` CHECK to `('legacy','tidal','ytm','soulseek','orpheus')`. SQLite cannot alter a CHECK, so it rebuilds `file` (same columns/order/defaults, three indexes recreated) and backs up/restores `link_review`, which `DROP TABLE file` would otherwise cascade-delete. |

Key rules:
- **Cluster** = `COALESCE(recording.canon_id, recording.id)`. Ownership and planning are per cluster.
- **Owned** = at least one `file` row has `recording_id` set to a recording in the cluster. Nothing else counts (tags, names, durations and folders do not).
- `file.path` is relative to the library root. `file.match` is `isrc`, `fuzzy`, `manual` or `unmatched`; every fetched file is `manual`, score 1.0.
- `file` has **no Tidal id column**: the track id a fetch used is only in the `fetch.placed` event.

**DB state:** the Unraid DB was migrated to v6 in the 2026-10-27 deploy. **007 has not been applied anywhere.** The local `data/cadence.db` is an old snapshot at v1 (5,210 files, no `link_review`). Always back up the Unraid DB (`cp -p`) before migrating.

---

## 5. Identity (`identity/`, `catalog/`)

A track is identified by **recording**, and recordings are clustered; "do I own it?" is a track-level question.

**Clustering (`grouping.py`)** merges recordings when they have the same `norm_title` within 3 s (`CLUSTER_TOLERANCE_S`) or share an ISRC. Variants (live, instrumental, slowed, ...) stay separate. Two extra passes, both singleton-only and within one album group: an *album-prefix* pass (`Giant Steps (Alternate Take 8)` vs `Giant Steps`, 1 s tolerance, >=60 s long) and a *remaster-year* pass (`Song (2009 Remaster)` into `Song`, 3 s). `cadence regroup` recomputes groups offline and writes `canon_id` only.

**Canonical recording** of a cluster: the remaster with the latest year (year from the tag, else the release date; tie: latest date, then highest id), otherwise the earliest original. Latest remaster is the default edition; originals are kept, never deleted.

**Matching (`matcher.py`)**: ties compare with 1e-6 tolerance; candidates in the same cluster collapse to one.

**Linking (`cadence link`)**: the single resolver.
- One closest recording links; equal distance with different clusters is a tie: the file stays unmatched and gets a `link_review` row.
- `--rematch` re-evaluates every non-`manual` file. A linked file that now ties or matches nothing **keeps its link** (a review row is written instead), so a tie never takes away something owned.
- Resetting a file's link (`alias add`, or `import-legacy` seeing a changed artist) deletes its `link_review` row.
- `cadence link --review` lists review rows and changes nothing. Run `cadence link --rematch` once after identity rules change.

---

## 6. Tidal source and gate

**`sources/tidal.py`**: `load_session(token_path)` logs in from a tidal-dl-ng style token file and is **read-only** (refreshed tokens are never written back). A transport adapter adds (10, 30) s timeouts and turns 429 into `requests.HTTPError` so the gate sees it everywhere. Releases come from `artists/{id}/albums` with filters None, EPSANDSINGLES, COMPILATIONS (first bucket wins on duplicates); pages are 50/100, capped at 40 pages; responses cache as JSON in `catalog_cache` for 7 days. DTOs are built from raw JSON because tidalapi drops `isrc`/`version` for non-streamable tracks. **Only album artist == the artist counts as the artist's own release** (`get_other` also returns "appears on" releases).

**Gate (`gate/bucket.py`)**: one token bucket for every Tidal call (sync, search, playback, download), default 1 request / 2 s, burst 3. On a 429 it pauses **all** Tidal work: back-off 15 min, then 1 h, then 4 h, honouring `Retry-After` (minimum 60 s). One notification per pause. `cadence gate-status` shows it.

---

## 7. Fetch planner (`fetch/plan.py`)

`cadence fetch` without `--execute` is a dry run: no network, no login, no write.

- **Buckets:** every cluster of the artist lands in exactly one of `owned` (counted), `items` (planned) or `skipped` (listed with a reason and the Tidal id).
- **Scope:** `partial` (default) plans a release group only if a linked file's recording is a track on a release of that group, so a box set that merely contains an owned song is not pulled in. `--scope all` plans everything (it warns). `--release ID` is an explicit request.
- **Which track:** the canonical recording's Tidal track; album beats EP beats single beats compilation.
- **Skip reasons:** partial scope: release has no owned track; variant (`--include-variants`); unavailable on Tidal; no tidal id; hidden release; appears-on release (name it with `--release`); release not in `--release`; artist not matched to Tidal (`cadence artist set-tidal`); shared recording planned under another artist. Only artists with `tidal_match` of `auto` or `manual` are planned.
- **Naming:** `Artist/Album/NN - Title.ext` from `ledger/naming.py`. Edition/remaster markers are stripped from the album folder; the title keeps Tidal's version; multi-disc releases get a `D{disc}-` prefix (disc count = Tidal `numberOfVolumes`, floored by the highest disc seen). **Do not change this layout:** the FiiO card scripts depend on it.
- **Collisions:** a planned target whose path equals a file the ledger links to a *different* recording, or that duplicates another planned target (case-insensitive), is listed and becomes a `conflict` at execute. It is never overwritten. Ambiguous unlinked files own nothing, so run `cadence link` and check `cadence link --review` before executing.

---

## 8. Fetch executor (`fetch/run.py`, `fetch/stream.py`)

### 8.1 Per track (`_fetch_one`)
1. Target already linked to this recording -> `exists` (no Tidal call). Target occupied by anything else -> `conflict` (nothing fetched).
2. Gate: `resolve_stream` (`playbackinfopostpaywall` at the requested quality). Refuses encrypted streams, previews, non-stereo audio and unknown manifests; **never decrypts**. Quality ladder: HI_RES_LOSSLESS, LOSSLESS, HIGH (LOW is never offered).
3. Gate: download to `staging/<target>.part` (direct URL, or DASH init+segments by plain GET, up to 5000 segments, remuxed by ffmpeg stream-copy to `.flac`/`.m4a`), then atomic rename.
4. **Verify** (before anything is replaced): ffmpeg decodes the file with no errors, and the duration is within **3 s** of Tidal's.
5. **Tag** with mutagen (FLAC: ALBUMARTIST, TITLE, TRACKNUMBER, DISCNUMBER, ALBUM, ISRC; M4A equivalents).
6. **Place** into the library, then insert the `file` row (`match=manual`, `quality_requested/received`) and a `fetch.placed` event, commit. One track per transaction.

### 8.2 Placement (`_place` / `_move_into_library`)
Same filesystem: `os.link` (no-clobber) then unlink the staged copy. Different filesystem (Unraid): copy to a unique `.part` beside the target, fsync, check size, `os.link` into place (no-clobber), remove the `.part`, and unlink the source only after the commit. If hard links are unsupported there is **no overwriting fallback**: it fails. The preflight refuses a staging directory inside/equal to the library and probes hard links first.

### 8.3 Lossless-only gate
For LOSSLESS and HI_RES_LOSSLESS requests a lossy (AAC) result is **held**: kept in staging as `<stem> [tidal <id>].m4a`, logged `fetch.held_lossy`, with no `file` row and no library write. HIGH requests are exempt. When a FLAC is later placed, the held AAC is removed (`fetch.stale_held_removed`) only after the FLAC row is committed.

### 8.4 Other Tidal editions (`_probe_editions`)
Tidal licenses lossless per release, so the same recording can be FLAC on one edition and AAC on another. When a lossless request is served lossy, Cadence asks other Tidal tracks of the **same recording**: same version (`norm_title`), available, duration within 3 s, albums first then lowest id, at most **4** probes (`MAX_EDITION_PROBES`), each through the gate and logged as `fetch.edition_probe`. The first lossless alternate is downloaded and keeps the planned path; `fetch.placed` carries `tidal_track_id` (used) and `planned_tidal_track_id`.

### 8.5 Fallback sources (new, `fetch/fallback.py`, commit `ab49bb5`)
Tried, in the order given, **only** when a LOSSLESS/HI_RES request is still lossy after the edition probes (or a held copy already exists), and **only** if `item.duration_s` is known (otherwise `fetch.fallback_skipped` and the AAC is held as before). HIGH never calls a fallback. With no fallback configured, behaviour is byte-for-byte the old behaviour.

**Acceptance of any fallback file** (all must hold; any failure is a "miss" and the next source is tried):
- it is a `.flac` that mutagen can open and ffmpeg decodes cleanly (verify is mandatory for fallbacks even if `verify=False`);
- its duration is within 3 s of the planned track's;
- if it carries an ISRC and the plan has one, they match (hyphens/spaces/case ignored; checked *before* tagging overwrites it);
- it is copied (`shutil.copy2`) to `staging/<stem> [name].flac.part`, then renamed, tagged, and placed with the shared `_place` helper with `file.source` = `orpheus` or `soulseek`. The `fetch.placed` event gains `source`, `fallback_ref` and `planned_tidal_track_id`. A held `.m4a` is removed after the commit.
- a taken target path is a conflict, and the source is not even contacted.

**OrpheusDL (`OrpheusFallback`)** runs the owner's own OrpheusDL checkout as a subprocess (list args, no shell, stdin DEVNULL, timeout). Cadence never reads OrpheusDL's `config/settings.json` (credentials stay there; its `download_quality` must be `hifi` or `lossless`).
- module `tidal`: `orpheus.py -o <workdir> download tidal track <tidal_track_id>`, executed **through the Tidal gate**; a rate-limit pattern in the output (only when no FLAC was produced) raises a 429-shaped error so the gate pauses.
- any other module (e.g. `qobuz`, `deezer`): `orpheus.py -o <workdir> luckysearch <module> track "<artist> <title>"`. Because luckysearch is fuzzy, the downloaded FLAC's **tags** must match: title (with version) equals the plan's, artist/albumartist names the planned artist (credits split on `&`, `,`, `;`, `/`, `and`, `feat`, `with`, `x`, `vs`), and no variant word appears in the title/album unless the planned title has it. A missing title tag is a miss.
- OrpheusDL's exit code is unreliable (several failure paths `exit()` with 0), so **success = exactly one `.flac` under the workdir**. The workdir (created by Cadence under `staging/.fallback`) is removed afterwards.

**Soulseek (`SoulseekFallback`)** talks to a running **slskd** over REST (`<url>/api/v0`, header `X-API-Key`).
1. `POST /searches` for "artist title", poll `GET /searches/{id}` until complete, `GET /searches/{id}/responses`.
2. **Candidate filter:** not locked; `.flac`; `length` present and within 3 s; the **whole remote path** contains no variant word (the shared variant list plus tribute, cover(s), unplugged, session(s), concert, karaoke, slowed, sped) unless the planned title/artist has it; the file name (minus track number and an optional `<artist> - ` prefix) must **equal** the title; the artist must be a **whole folder name** or the `<artist> - ` prefix (so "Queen Latifah" and "A Tribute to Queen" fail). A title/artist that normalizes to nothing matches nothing. Rank: free upload slot, short queue, higher bit depth, faster peer. Top `CADENCE_SLSKD_MAX_CANDIDATES` (default 3) are tried.
3. `POST /transfers/downloads/{user}` `[{"filename","size"}]`; the transfer is identified by the ids in the answer, else by a new id for that filename that did not exist before the POST (no clock comparison). Poll until `Completed, Succeeded` (any other `Completed, ...` = next candidate); on per-file timeout it `DELETE`s the transfer and moves on.
4. Locate the file in `CADENCE_SLSKD_DOWNLOADS` by basename + exact size (retries up to 30 s because slskd can report success before moving the file). The slskd file is **copied, never moved or removed**. A locate failure or malformed record moves to the next candidate.
5. All calls use timeouts (10, 30), `allow_redirects=False`, and the key only as a header: it is never logged, printed, put in events, or in exception text.

**Mullvad VPN gate (Soulseek only).** slskd must run in gluetun's network namespace (see 11.3), which is the real kill switch. On top of that Cadence queries gluetun's control server (`GET /v1/vpn/status` must say `running`, `GET /v1/publicip/ip` for display):
- required env: `CADENCE_GLUETUN_URL` and `CADENCE_GLUETUN_API_KEY`; **the slskd URL and gluetun URL must have the same hostname** (a mismatch is a config error, exit 2); the only opt-out is an explicit `CADENCE_SLSKD_VPN=off`;
- checked at preflight and **again before every acquire and every candidate after the first**; "up" is never cached. If it is down (unreachable, 3xx/5xx, or not `running`) a `VpnDown` disables Soulseek for the rest of the run (`fetch.vpn_down` logged once, shown in the summary) while Tidal and the other fallbacks continue. A gluetun 401 is a config error and stops setup.

### 8.6 Stops and exit codes
| Exit | Meaning |
|---|---|
| 0 | Normal finish (a dry run always returns 0, even with conflicts). |
| 1 | Execute finished with failures or conflicts; also an auth stop. |
| 2 | Configuration error (including bad fallback config, in a dry run too). |
| 3 | Paused: 429, or the gate is paused. |

A 401 on one track's playback logs `fetch.blocked` and continues unless the body names the token/session (then the run stops, exit 1). Three dead connections in a row stop the run. `--limit N` cuts attempts after planning and says how many were not attempted. The summary line shows placed / exists / conflict / held / blocked / failed / fiio-skipped / not attempted, plus `via fallback` and `vpn down` when fallbacks are configured.

---

## 9. Quality and the Tidal account

- Default quality `HI_RES_LOSSLESS`; lossy results are held for LOSSLESS/HI_RES_LOSSLESS.
- Measured: famous tracks serve `HI_RES_LOSSLESS`; a random 30-track planned sample served lossless for 7 (23%). Lidarr's own Tidal listing is ~27% FLAC / 73% AAC. **Most of the gap is Tidal's per-release licensing**, which is why the edition probes and fallbacks exist.
- The token's account reports `PREMIUM` in the API; the owner says HiFi, one Tidal app screenshot showed a Student plan. Treat plan details as unverified.
- Dolby Atmos: 4 tracks failed the duration check in the canary. Recommendation: skip Atmos as a variant (**not built**). AAC 320 per-album opt-in: proposed, **not built**.

---

## 10. CLI and configuration

`python -m cadence <command>` (or the `cadence` script). Every command takes `--help`.

| Command | Does | Network |
|---|---|---|
| `init` | create/migrate the ledger | no |
| `import-legacy PATH` | import a library scan JSONL | no |
| `sync-catalog [--artist NAME \| --all] [--max-calls N] [--interval S] [--burst N] [--force]` | pull Tidal catalogues | yes |
| `candidates [--artist NAME] [--json] [--musicbrainz]` | ambiguous artists' Tidal profiles vs owned albums | yes |
| `link [--artist NAME] [--rematch] [--review]` | match files to recordings | no |
| `regroup [--artist NAME]` | recompute groups/clusters | no |
| `report [--artist NAME] [--json]` | owned / missing | no |
| `artist set-tidal NAME TIDAL_ID` | pin an artist to a Tidal id | no |
| `alias add ALIAS TARGET [--note]` / `alias list` / `alias remove ALIAS` | map a tag name to an artist | no |
| `fetch [--artist NAME \| --release ID] [--scope partial\|all] [--quality Q] [--staging DIR] [--library DIR] [--execute] [--limit N] [--include-variants] [--fallback orpheus\|soulseek]...` | plan (dry run) or execute | only with `--execute` |
| `cache-clear [--artist NAME]` | forget cached Tidal responses | no |
| `quality` | lossless/lossy file counts | no |
| `audit-tags` | album-artist tags a primary-artist rule would change (read-only) | no |
| `gate-status` | is the Tidal gate paused | no |

**Environment variables** (no paths are built in):

| Variable | Meaning |
|---|---|
| `CADENCE_DB` | ledger path (default `<project>/data/cadence.db`) |
| `CADENCE_LIBRARY`, `CADENCE_STAGING` | defaults for `--library` / `--staging` |
| `CADENCE_TIDAL_TOKEN` | Tidal token file (default `~/.config/tidal_dl_ng/token.json`) |
| `CADENCE_FALLBACKS` | comma list, default for `--fallback` (e.g. `orpheus,soulseek`) |
| `CADENCE_ORPHEUS_DIR`, `CADENCE_ORPHEUS_MODULE` | required for orpheus: checkout path, module (`tidal`/`qobuz`/`deezer`) |
| `CADENCE_ORPHEUS_PYTHON`, `CADENCE_ORPHEUS_TIMEOUT` | default `python3`, 900 s |
| `CADENCE_SLSKD_URL`, `CADENCE_SLSKD_API_KEY`, `CADENCE_SLSKD_DOWNLOADS` | required for soulseek (downloads dir as seen by Cadence) |
| `CADENCE_SLSKD_TIMEOUT`, `CADENCE_SLSKD_MAX_CANDIDATES` | default 900 s per file, 3 |
| `CADENCE_GLUETUN_URL`, `CADENCE_GLUETUN_API_KEY` | required for soulseek (VPN gate) |
| `CADENCE_SLSKD_VPN` | only `off` is valid: explicit opt-out of the VPN gate |

A bad or missing variable gives exit 2 naming the variable (never its value).

---

## 11. Deployment and runbook

### 11.1 Deploy pattern (used 2026-10-27)
1. Confirm the suite is green on the Mac.
2. On Unraid back up the code (`data/code-pre-<rev>.tgz`) and the DB (`cp -p cadence.db data/cadence.db.bak-pre-<tag>`).
3. Deploy with a tar from the Mac.
4. `cadence init` (migrations), then `cadence link --rematch` once if identity rules changed.

### 11.2 Running a fetch
Dry run first (`--library`/`--staging`, no `--execute`) and check the collision/conflict counts. Execute **only with the owner's go-ahead**, detached, with output to a log under `data/` (the last run used `data/fetch-full2-2026-10-27.txt` and its container was SIGKILLed, exit 137, before finishing). Stopping is safe: placed files are committed and held copies stay in staging; check staging and the `fetch.*` events before restarting. Before any restart confirm the policy: lossless only, held AAC kept, edition fallback on, and (after deploy) which fallbacks are enabled.

### 11.3 slskd behind Mullvad (`deploy/slskd/run-slskd.sh`, **not yet run**)
Script for Unraid (no compose plugin; same pattern as `run-frigate.sh`). It starts `qmcgaw/gluetun:v3.41.3` (Mullvad WireGuard, `SERVER_COUNTRIES`, control server on host port 8010 behind an API-key role allowing only `GET /v1/vpn/status` and `GET /v1/publicip/ip`), waits for gluetun's healthcheck, then `slskd/slskd:0.26.1` with `--network container:gluetun-slskd` (web UI on 5030). Both image tags were confirmed to exist on Docker Hub (2026-10-27). Secrets are in three chmod-600 files under `/mnt/cache/appdata/slskd/`, one per consumer: `gluetun.env` (`WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`), `gluetun-api.key` (output of `docker run --rm qmcgaw/gluetun genkey`), `slskd.env` (`SLSKD_SLSK_USERNAME/PASSWORD`, `SLSKD_USERNAME/PASSWORD`, `SLSKD_API_KEY=role=ReadWrite;cidr=...;<32+ chars>`). It only replaces containers it created (label `cadence.managed=slskd`).
Limits: Mullvad has no port forwarding, so slskd cannot accept incoming peer connections (searches/downloads still work, firewalled peers are unreachable). After a reboot, if Docker starts slskd before gluetun, slskd stays down (fails closed); rerun the script. The script passes `bash -n` but has **never been executed**.

### 11.4 To get the fallbacks live (all outstanding)
1. Back up and migrate the Unraid DB (007), deploy code `ab49bb5`.
2. Create the three secret files and run `run-slskd.sh`; confirm the exit IP in `docker logs gluetun-slskd`.
3. Install OrpheusDL with a Qobuz (or Deezer) module and its own credentials, and mount slskd's downloads dir (read-only) into the Cadence container.
4. Set the `CADENCE_*` variables from section 10; do a dry run; then `--fallback orpheus --fallback soulseek` with an explicit go-ahead.

---

## 12. Tests

767 tests, ~33 s, **no live Tidal, slskd or subprocess calls** (everything external is injectable). Fakes: `tests/fakes.py` (gate, sources, fake clock), `tests/fetch_fakes.py` (fake Tidal session/HTTP, in-memory slskd, fake OrpheusDL runner), `tests/ledger_gen.py` (seeded random ledgers). `tests/test_plan_invariants.py` checks planner invariants over them (every cluster in exactly one bucket, collisions listed, no silent skips). Key files: `test_fetch_run.py`, `test_fetch_fallback.py`, `test_soulseek.py`, `test_orpheus.py`, `test_migration_007.py`, `test_grouping.py`, `test_link_review.py`, `test_matcher.py`, `test_gate.py`, `test_tidal_source.py`, `test_will_paquin_e2e.py`. Real-library checks run read-only against `m1/data/library_scan.jsonl`. Several tests assert the exact migration list (`[1..7]`); adding migration 008 means updating `test_db.py`, `test_groups.py`, `test_link_review.py`.

Process used: tests first, then a reviewer pass after each non-trivial change. The fallback work had three review rounds (wrong-track bypasses, VPN ordering, key leakage, clock skew); each finding has a regression test.

---

## 13. UI design ("Koi Pond")

Settled direction: a quiet koi pond at night, heavy teal/green highlights, laid-back tones, **16-bit pixel art** with audiophile touches. Design files only; there is **no web code**. Full spec in `cadence-koi-ui/HANDOFF.md`; live canvas https://claude.ai/artifact/6YfnW56BrZAwfcMyhB8yPh.
- **Tokens:** pond-deep `#0B1F26`, pond-base `#0F2B33`, pond-shallow `#163C46`, pond-reed `#1F5560`, teal `#4FE0C1`, lily `#8AD47A`, koi-gold `#F4A640` (reserved for "needs you"), foam `#DDF3EC`. Fonts: Pixelify Sans (display), Silkscreen (labels), Atkinson Hyperlegible (body). Pixel corners via a 2-step clip-path, bevels, dither, stepped animations, reduced-motion respected.
- **Animated sprites** (SVG, integer scale): 48x24 koi in 4 varieties (kohaku = Tidal, yamabuki = OrpheusDL, kumonryu = Soulseek, showa = alert), spinning turntable, bouncing VU meter, cassette reels, pond ripples, bobbing lotus.
- **Screens:** style tile; phone Inbox, Artist, Activity, FiiO card; desktop Home, Artist, Library (fluid pages that stack at phone width).
- **Planned stack:** FastAPI + Jinja + HTMX 2.x (pinned) + a little Alpine, SSE with a full-state snapshot on every reconnect, plain CSS, installable PWA over Tailscale HTTPS (a PWA needs HTTPS), SortableJS island for the FiiO drag-order list. Bind to the Tailscale/LAN address, no login; the card agent uses a static Bearer key; ntfy action buttons must carry single-use signed tokens.
- **Not designed yet:** Add/search, Settings, conflict review, error states (no Tidal auth, gate cooling, VPN down, card read-only, unknown card), light theme.

---

## 14. Milestones

| M | Deliverable | Status |
|---|---|---|
| 0 | Spikes (catalogue, tiddl, rate limits, YT Music) | Done |
| 1 | Ledger, legacy import, catalogue sync, matching | Built (Will Paquin report was the exit test) |
| 1b | Gap sweep (YouTube Music vs ledger) | Spike only (`spikes/s4_*.py`) |
| 2 | Fetch pipeline: plan, gate, verify, tag, place, dry run | Built; **hooks (Music Assistant sync, Jellyfin path scan, Navidrome rescan, ntfy, HA webhook) not built**; exit test not met |
| 2b | FiiO card profile + Mac `cadence-card` agent (launchd `StartOnMount`, volume-UUID, manifest, never deletes) | Not built |
| 3 | Web UI skeleton + PWA | Designed |
| 4 | Auto-grab, upgrades, follows, digest notifications | Not built |
| 5 | Fallback worker (YT Music), FiiO and Library screens | Partly superseded by the Soulseek/OrpheusDL fallbacks |
| 6 | Cutover beside Lidarr/Aurral | Not built |

**Open decisions for the owner** (`DESIGN.md` §13): whether gap suggestions may ever auto-grab (default never), web access model (default Tailscale/LAN, no login), Opus vs AAC for a lossy fallback after a FiiO playback test. (Visual direction is now decided.)

---

## 15. Known issues and open items

**Code**
- A commit that fails after the file is placed is reported as failed although the file is in the library; an auth stop during an edition probe is reported as "not attempted"; some note text is wrong ("not fetched again").
- `--retry-held` is not built, so held AAC tracks are only retried when they hit the "already held" path.
- Held files from before the `[tidal <id>]` rename (plain `.m4a`) are not recognised as held.
- A Tidal-blocked track (401) prints `blocked` but the exit code is unchanged.
- Dolby Atmos tracks are not skipped as variants; the dry run prints one line per skipped track (long for artists with many unowned releases); a library folder spelled differently (`DECADE` vs `Decade`) is not reused.
- Fallback strictness trade-off: albums whose names contain a variant word ("The Sessions") are rejected by the Soulseek filter; a folder like "Neil Young & Crazy Horse" does not satisfy the Soulseek artist rule for "Neil Young".
- Soulseek/OrpheusDL integration is verified only against fakes: real slskd response shapes, transfer ids on enqueue, slskd's on-disk layout and OrpheusDL's real output have not been exercised.

**Data**
- About 38-42 duplicate-target / linked-collision conflicts need the owner's review.
- 89 tracks held as AAC in staging (the main target of the fallbacks).
- Smoke leftovers on Unraid await the owner's OK to delete.

**Repo hygiene**
- `fetch-ledger-planner` and its 10 commits are unpushed; `docs/CADENCE.md` is untracked; `DESIGN.md` is partly stale.

---

## 16. Decisions log

| Decision | Date | Notes |
|---|---|---|
| Tidal is the source of truth; YouTube Music is a fallback and gap-finder only | 2026-10-25 | MusicBrainz/Discogs optional enrichment |
| `cadence link` is the only identity resolver; the planner never matches files | 2026-10 | Invariant tests |
| Folder naming stays `Artist/Album/NN - Title.ext` | 2026-10-26 | FiiO scripts depend on it; `[Year]` prefix deferred |
| Default edition = latest remaster; originals kept | 2026-10-26 | |
| Partial scope by default | 2026-10-27 | |
| Lossless-only placement; AAC held | 2026-10 | Edition probes first |
| Own in-house fetch (`stream.py`/`run.py`); no tiddl/tidal-dl import (only the tidal-dl-ng token *file format* is read) | 2026-10 | Supersedes DESIGN.md §8b: tiddl's skip logic is path-based, the ledger must decide |
| **Soulseek (slskd) and OrpheusDL added as lossless fallbacks, Soulseek only behind Mullvad via gluetun** | 2026-10-27 | Reverses the earlier "not built" call, at the owner's explicit request. Soulseek is peer-to-peer; what is downloaded is the owner's legal responsibility. OrpheusDL is a *fallback*, not a replacement for the Tidal fetch. |
| Koi-pond visual direction, no existing design system reused | 2026-10-27 | Settles DESIGN.md §12.4 |

---

## 17. Git history (branch `fetch-ledger-planner`)

```
ab49bb5 Fetch: OrpheusDL and Soulseek fallbacks for lossless, behind Mullvad
6968b87 Fetch: try other Tidal editions when a track serves lossy
600a9bb Partial scope: a release is partial only with a linked track of its own
30999c8 Merge remaster-year marked recordings with their plain twins
639564a Merge same-album recording pairs whose titles differ only by album prefix
1d8e618 Ledger-only fetch planner, link as sole identity resolver, invariant tests
8fde791 WIP: fetch planner hardening (known open findings, not for --execute)
98acd03 Disc counts from Tidal volumes, stricter legacy-file matching
52333ec Fetch: disc-aware names, atomic no-clobber placement
f342577 Fetch: lossless-only placement, held files, cross-device safe placement
d2a54e8 Fetch: partial scope by default, reuse library album folders, duplicate guard   <- origin/main
70c9c5b M2 fetch pipeline: plan, dry run, guarded real run
6ea82f9 Default to the latest remaster edition
af53704 Quality tracking, MusicBrainz tiebreaker, naming and album-artist rules
928d25d Matcher: album-prefix, live-variant and claimed-album tolerance tiers
2d879ea Add artist aliases: map tag names to existing artists
9fb7710 Group editions and recordings; add candidates command
a81ac71 Sync: tolerate single network failures (backoff 30s/120s, stop after 3 in a row)
e2538f0 Harden catalog sync for the full-library run
97dbddf Cadence M1: ledger, Tidal catalog sync, legacy linking, reports
0c382d5 Cadence: design doc and M0/M1 spikes
```

---

## 18. Glossary

- **Recording:** one performance of a song, identified by ISRC where Tidal gives one.
- **Cluster (canon):** recordings that are the same song across editions, `COALESCE(canon_id, id)`.
- **Release group:** the albums/editions that are the same release (`group_key`).
- **Owned:** a cluster with at least one `file` row linked to it.
- **Held:** a lossy result kept in staging, not placed in the library.
- **Conflict:** a target path taken by something the ledger does not link to this recording.
- **Collision:** a planned target equal to another recording's linked path, or to another planned target (case-insensitive).
- **Partial scope:** only release groups where the owner already owns a track of that release.
- **Fallback:** an alternate lossless source (OrpheusDL, Soulseek) tried when Tidal serves lossy.
- **Miss:** a fallback source had nothing acceptable for this track; the next source is tried.
- **Gate:** the shared Tidal rate limiter (not to be confused with the **VPN gate**, the gluetun status check before Soulseek use).
- **gluetun:** a VPN client container (here Mullvad WireGuard) whose network namespace slskd shares; **slskd:** the Soulseek daemon with a REST API; **OrpheusDL:** a modular streaming-service downloader the owner runs separately.
