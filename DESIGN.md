# Cadence — design doc (working title)

A purpose-built replacement for Aurral + Lidarr. Tidal's catalog is the source of truth, one SQLite ledger tracks what you own, and a worker fetches what's missing.
> **Updated 2026-10-27.** This is the original design doc and is partly historical. Code exists now (milestones 0-2 are built) and **`docs/PROJECT.md` is the current, complete reference**. Superseded here: Python 3.12 is now 3.13; the downloader is the in-house fetch (`cadence/fetch/`), not tiddl (§3, §8b, §11.3); Soulseek and OrpheusDL were added as lossless fallbacks behind a Mullvad VPN (§11.6 describes the older YouTube Music fallback); the visual direction (§12.4) is decided: koi pond, see `cadence-koi-ui/HANDOFF.md`.

Original status line: design draft, 2026-10-11. No code yet. Items marked ⚠ were unverified assumptions that Milestone 0 had to settle.

## 1. Why

Pain points in the current stack (all observed 2026-10-05 to 10-11):

| Pain | Cause |
|------|-------|
| Will Paquin missing ~8 of 20 Tidal releases | Lidarr only sees MusicBrainz via a third-party mirror; releases not in MB are invisible |
| 40 artists silently on the wrong metadata profile, 37 on the wrong quality profile | Aurral and Lidarr each kept their own default profile; they drifted |
| Stuck imports ("album match 76.5% vs 80%", "has missing tracks") | Lidarr must map every download onto an MB release |
| Tidal plugin needed a hand-patched DLL pinned to one Lidarr build | Plugin architecture |
| Lidarr searches get the Tidal account 429'd | No global rate budget shared with other tools |
| Hooks to MA/Jellyfin/Navidrome are custom scripts bolted on | Lidarr events are limited (e.g. none fire for ManualImport) |
| Duplicates (Chandelier x2, Infinite Jest x2, remixes, slowed versions) | No track-level identity |

Goal: one tool, one config, one place where "what do I own / what's missing / what is being fetched" is answered from Tidal's real catalog.

## 2. Scope

**In (user-confirmed):**
1. Request an artist or album (search Tidal, pull a full discography or pick releases)
2. Auto-grab new releases from followed artists
3. Discovery / recommendations
4. Quality upgrades plus a lossy fallback (replaces ytdlp-fallback)

5. **FiiO card sync built in** (added 2026-10-11): choose what goes on the Echo Snowsky Mini's SD card, copy it, and format it for the player. Replaces `tidal-card-sync` + `fiio-format-card`. See §5a.

**Interface:** web app (phone + Mac), plus ntfy notifications.

**Out (for now):** Usenet/torrents, multi-user accounts, video, audiobooks/podcasts, a Lidarr-compatible API, playback (Music Assistant, Navidrome and Jellyfin keep doing that).

**Hard rules:** never delete a user file. All destructive-looking operations (replace, move) go through a trash dir. Dry-run mode for every pipeline stage. Runs side by side with Lidarr until cutover.

## 3. Architecture

```
 Web UI (HTMX/FastAPI templates)  ─┐
 ntfy actions                     ─┼─►  API (FastAPI)  ─►  SQLite ledger (appdata/cadence/cadence.db)
                                   │          ▲
                                   │          │ state
        ┌──────────────────────────┴──────────┴─────────────────────────┐
        │  scheduler: refresh followed artists, new-release check,       │
        │             upgrade pass, fallback pass, library rescan        │
        └───────────────┬───────────────────────────────────────────────┘
                        ▼
   job queue ─► Tidal gate (single token bucket + 429 backoff) ─► downloader (tiddl)
                        │                                          │
                        │                      staging dir ─► tagger/placer ─► /music/Artist/Album/NN - Title.flac
                        ▼                                          │
              fallback worker (YouTube Music, AAC/Opus)            ▼
                                                      hooks: Music Assistant sync, Jellyfin path scan,
                                                             Navidrome scan, ntfy, HA webhook
```

Stack proposal: **Python 3.12 + FastAPI + SQLite (WAL) + HTMX**, one container (web + worker as two processes), deployed on Unraid via the same template + `rebuild_container` pattern as your other containers (never plain `docker run`). Python because tidalapi, tiddl, mutagen, ytmusicapi and yt-dlp are all Python, and `ytdlp-fallback` is already Python.

## 4. Data model (identity is the hard part)

Key idea: **a track is identified by ISRC (plus duration tolerance), not by a release.** "Do I own it?" is a track-level question. Releases are views onto tracks. This is what stops Chandelier-as-single, Chandelier-as-instrumental and album versions from being counted as separate things and re-downloaded.

```
artist(id, tidal_id, name, norm_name, mbid?, followed, policy)
release(id, artist_id, tidal_id, upc?, title, norm_title, type[album|ep|single|compilation|other],
        release_date, explicit, num_tracks, group_key, hidden_reason?)
track(id, release_id, tidal_id, isrc, title, norm_title, disc, number, duration_ms, explicit,
      version?)                              -- 'Instrumental', 'Slowed', 'Remix', 'Live'
file(id, track_id?, path, format, bit_depth, sample_rate, bitrate, source[tidal|ytdlp|legacy],
     size, mtime, content_hash, tags_json)
want(artist_id | release_id, rule, dest)     -- rule: 'all', 'new_only', 'albums_only', 'ignore'
                                              -- dest flags: server | fiio | both  (see §5a)
card(uuid, label, capacity, budget, last_seen)
card_state(card_uuid, release_id, state, copied_at)
job(id, kind[fetch|upgrade|fallback|rescan], target, state, attempts, error, not_before)
event_log(...)                                -- what happened and why; powers the UI history
```

**Release grouping (`group_key`)** collapses duplicates: same normalized title + >=80% overlap of ISRC sets, with `version` words (instrumental, slowed, sped up, remix, live, demo) kept distinct so variants stay separate rather than silently merged. Default policy: *own the best version of each group*; variants are opt-in per artist.

**Normalization** (`norm_title`): NFKC, fold curly/straight quotes, strip `(Remastered ####)`, `(Explicit)`, `[Deluxe]` into flags rather than deleting them, case-fold, collapse whitespace/punctuation. This directly addresses Her's / Her’s and the Lidarr "album match 76.5%" class of problem.

**Legacy library import (M1):** scan existing files (~4.6k tracks, ~139 GB), read tags, map to Tidal tracks by ISRC tag when present, else normalized (artist, title, duration ±2 s). Anything unmapped stays `source=legacy, track_id=NULL` and is never touched. Lidarr-written MusicBrainz IDs in tags are kept as hints.

## 5. Pipelines

**Request:** search Tidal (artist/album/track) → pick → write `want` → expand to jobs for tracks not already owned (ledger diff, not file-path guessing).

**Fetch:** job → Tidal gate → tiddl into staging → verify (decode with ffmpeg, check duration within tolerance) → tag (mutagen: ALBUMARTIST always set, ISRC, Tidal IDs, cover) → place with the existing naming scheme `Artist/Album/NN - Title.flac` (keeps `tidal-card-sync` and `fiio-format-card` working unchanged) → record `file` → hooks.

**Auto-grab:** scheduler refreshes followed artists (e.g. every 6 h, jittered), diffs releases, applies `want.rule`, enqueues. Replaces Lidarr's missing RSS (Tidal has none) and the cron'd `lidarr-search.sh`.

**Upgrade:** a file is "upgradable" if a better source exists (lossy → FLAC, 16-bit → 24-bit if Tidal now offers it). Runs slowly in the background through the same gate.

**Fallback (replaces ytdlp-fallback):** if a wanted track has no Tidal stream after N days, match on YouTube Music (the title+duration matcher already proven in `ytdlp-fallback`), download, tag, place as `source=ytdlp`, and mark upgradable. **Format choice (Opus vs AAC) is open (§8): Lidarr's quality list had no Opus, but this tool has no such limit; the FiiO and every consumer must be tested before choosing.**

**Discovery:** ⚠ prefer Tidal's own similar-artists data via tidalapi (needs no extra API key) plus "followed artists' related", shown in the UI as suggestions you can follow in one tap. Last.fm / ListenBrainz are optional later add-ons.

### Folder naming (decided 2026-10-26)

Keep `Artist/Album/NN - Title.ext`. Reasons: the FiiO card scripts (`tidal-card-sync`, `fiio-format-card`) depend on it, the 5,210 existing files already use it, and Jellyfin / Navidrome scan that layout fine. A `[Year]` prefix on the album folder was considered and deferred (it would mean renaming every album folder and touching the card scripts for no scanner benefit). The template lives in one place, `cadence/ledger/naming.py` (`album_dir`, `track_file`); only characters exFAT/macOS cannot hold are changed. Nothing in the library has been renamed.

Editions (decided 2026-10-26): latest remaster is the default edition; originals are secondary and are never deleted. In code: the canonical recording of a cluster is the remaster with the latest year (the year in its remaster tag, else the year of its release date; tie: latest date, then highest id), else the earliest original (`identity/grouping.py`); the edition shown for a release group is the one with the most owned clusters, then a remaster, then the latest remaster, then the fewest tracks, then the earliest (`report.py`).

### 5a. FiiO card sync (built in)

**Destination tags (user requirement, 2026-10-11).** Every album you request carries a destination: **Server**, **FiiO**, or **both**. You choose it when you ask for the album (checkboxes in the UI), or it falls back to a per-artist default; you can change it later, singly or in bulk. Anything tagged FiiO is the **"set aside" list**, and it is copied to the card **automatically whenever the card is plugged in**.
- If no default applies, the "downloaded" ntfy gets three action buttons (Server / FiiO / Both) so you can decide from your phone.
- **FiiO-only** albums are stored in a holding folder (`/music-card/...`) that Jellyfin, Navidrome and Music Assistant don't scan, so they don't clutter the server library. **Server** and **both** albums go in the normal library. A copy of everything FiiO-tagged stays on Unraid after it's on the card (storage is cheap; this lets you re-flash a card or add a second one).
- Un-tagging FiiO never deletes anything from the card by itself; it shows up as a proposed removal you confirm.
- Ledger: `dest` flags on `want` (`server`, `fiio`) plus `card_state(card_uuid, release_id, state[queued|copied|removed_pending], copied_at)`.

**Detecting the plug-in (the tricky part).**
- Use launchd's **`StartOnMount`** key (starts the agent every time any filesystem mounts). That's more reliable than the current `WatchPaths /Volumes`, which fires on any change to `/Volumes` and fired twice per mount (hence the lock file in `tidal-card-sync`). ⚠ To confirm in M0: exact `StartOnMount` behavior on macOS 26 and that it runs for exFAT SD cards.
- On start the agent identifies the volume by **volume UUID** (`diskutil info -plist`), ignores everything that isn't a registered card, debounces (waits until the mount is stable and **writable**; the card sometimes mounts read-only), takes a lock, then runs the sync.
- Fallback if `StartOnMount` proves flaky: a tiny long-running agent using macOS DiskArbitration "disk appeared" callbacks.
- First plug-in of an unknown card: the agent doesn't write anything; it asks via ntfy ("Register this card as a FiiO? [Yes/No]").
- Only the Mac is covered (that's where the card goes). Another computer would need its own agent later.

Facts from the current setup: the card mounts on the **Mac** as `/Volumes/NO NAME` (exFAT, ~119 GiB, ~88 GiB used on 2026-10-11) and the library is ~139 GB, so **the whole library does not fit**. Today `tidal-card-sync` rsyncs everything with `--ignore-existing` (never deletes) and an exclude list from a manual Sep-24 cleanup, then `fiio-format-card` runs.

Design:
- **Two parts.** The *card profile* lives in Cadence on Unraid (what should be on the card). A small **Mac-side agent** (`cadence-card`, launchd `WatchPaths /Volumes`, same trigger as today) does the physical work, because only the Mac sees the mounted card. They talk over the Cadence API on Tailscale with an API key.
- **Card profile:** a size budget (default: card capacity minus 5 GB headroom), include/exclude rules per artist/album/playlist (replaces `exclude.txt`), priorities (e.g. favorites first), and a "pin" flag. The web UI shows "on card / queued / won't fit" and a size meter.
- **Manifest sync:** agent asks `GET /card/manifest?device=<card id>`, compares with files on the card, copies what's missing (parallel, rsync-style resume, writes to a temp name then renames), and reports back `POST /card/state`. The ledger remembers what's on which card.
- **Never delete by default.** Removals (things that no longer fit or that you un-pinned) appear in the UI as a proposed list that you confirm; matches today's "never `--delete`" rule.
- **Formatting folded in** (rules already proven in `fiio-format-card`): baseline (non-progressive) JPEG cover 750x750 under 1 MB embedded plus `cover.jpg`/`folder.jpg`; Vorbis `ALBUMARTIST` always set; exFAT-safe filenames; no `._*` AppleDouble files; the 2-digit track prefix layout. Cadence already tags files on ingest, so most of this is done once at import, not on every card plug-in.
- **Failure handling:** the card sometimes mounts read-only (seen in `tidal-card-sync.log`); the agent checks writability first, retries, and reports through ntfy instead of silently failing. Eject/"safe to unplug" notification when done.
- **Multi-card ready:** cards are identified by volume UUID, each with its own profile (a second card or the car USB later).
- **Car USB (future):** FAT32, 32 GB, MP3 320 with ID3v2.3/v1.1 and track numbering, fatsort. Same agent pattern with a different output profile; out of scope for now.

**Hooks (carried over, one module):** Music Assistant `music/sync` (+ token), Jellyfin `Library/Media/Updated` on the artist path, Navidrome rescan (or just its 1 h schedule), ntfy (topic split: `music`), HA webhook `lidarr_music` equivalent.

## 6. Tidal gate (the most important operational piece)

- One global token bucket for **all** Tidal calls; the worker, scheduler and UI search share it. This is the direct fix for "Lidarr searches 429 the account".
- 429 / 5xx: exponential backoff with jitter, pause the whole queue, ntfy once (not per job).
- Auth: tidalapi OAuth device flow, refresh token stored in appdata, ⚠ verify refresh behavior; if dead, UI banner + ntfy "re-auth needed".
- ⚠ Unofficial API: it can break or be restricted at any time. Mitigation: keep the downloader behind a small interface so tiddl can be swapped, pin versions, and keep Lidarr + the Tidal plugin installed read-only until trust is earned.

## 7. Milestones

| M | Deliverable | Exit test |
|---|-------------|-----------|
| **0 Spikes** | Answer the ⚠ unknowns (below) with throwaway scripts | Written findings in this doc |
| **1 Ledger + diff (read-only)** | Schema, legacy import, "what's missing" report per artist | Will Paquin report lists the 8-9 known missing Tidal releases; zero false "missing" for owned albums |
| **2 Fetch pipeline (CLI)** | Gate, download, verify, tag, place, hooks, dry-run | Download Will's missing releases; Jellyfin/MA/Navidrome see them; card sync copies them |
| **2b FiiO card sync** | Card profile in the ledger plus the Mac agent (manifest, copy, format, report) | Plug in the card: it ends with the same files `tidal-card-sync` would have produced, fits the budget, and nothing is deleted |
| **3 Web UI** | Search/request, artist page, queue, history, settings | Request an artist from the phone end to end |
| **4 Auto-grab + upgrades** | Scheduler, follow, upgrade pass | A new release appears with no manual step; lossy file gets upgraded |
| **5 Fallback + discovery** | Port of ytdlp-fallback, suggestions | Parity with the sidecar's `done` list |
| **6 Cutover** | Run beside Lidarr/Aurral, compare, then retire them | Two weeks of no discrepancies; Lidarr kept stopped (not deleted) for a month |

## 8. M0 findings so far (2026-10-11) and open questions

**CORRECTION (2026-10-12): the Spike 1 numbers below are wrong in one important way.** tidalapi's `Artist.get_other()` bucket returns albums the artist only *appears on* (e.g. Will's "COMEBACK" is a Noty album and "Songs To Come Home To" is a Ta-ku compilation). Always filter releases by **album artist == the artist** (and treat the rest as "appears on", off by default). Will Paquin therefore has **18 own releases and 29 unique recordings**, not 20 and 56; only 6 recordings were missing from the library, not 33. This also **answers open question 1**: appears-on releases *are* reachable via `get_other`, mixed in with real "other" releases, so the filter is required. The mistake caused a wrong 62 MB download (deleted) and three wasted API calls. Everything below is kept for the record.

**Answered:**
- **Spike 1 (catalog completeness), partial pass.** `tidalapi` 0.8.11 returned all 20 of Will Paquin's releases (65 track slots) via `get_albums` + `get_ep_singles` + `get_other` with a throttled 23 calls, and **every track had an ISRC and every release a UPC**. There is no `get_appears_on`; appears-on / compilation coverage is still untested. `Artist.get_similar()` exists (usable for discovery). Script: `spikes/s1_catalog.py`, data: `spikes/out/Will Paquin.json`.
- **Identity model validated.** The 65 slots collapse to **56 unique ISRCs**. Singles like *I Work So Hard* (2 tracks) and *We Really Done It This Time* (3 tracks) are 100% duplicates of album tracks, and *Mando*, *Grass*, *Infinite Jest* repeat on the EP. An ISRC ledger would fetch each recording once; a release-based tool (Lidarr, Tidarr, tiddl on an artist URL) would fetch the dupes. Note: *Chandelier* and *Chandelier (Instrumental Version)* have different ISRCs and are genuinely separate recordings.
- **Spike 4 ("Media not found") solved.** It was never "not found": `tidal-dl-ng` catches every exception in `instantiate_media` and prints that message. Reproduced with a traceback: it was **HTTP 429 `TooManyRequests`** from `api.tidal.com/v1/albums/<id>`. So the 9 failed downloads were rate limiting; tidal-dl-ng is safe to keep as a fallback but its error text is misleading.
- **Spike 3 (rate limits), first data point.** Lidarr's weekly `lidarr-search.sh all` run (Sundays 01:00, 75 s between albums, 264 missing + 157 cutoff-unmet albums this week) was running at the same time as this spike and the earlier downloads; Tidal 429'd the account/IP. A 429 hit on `GET /v1/albums/{id}` (metadata, not a stream). **No numeric limit yet**: do not probe for one while Lidarr is searching. Also: tidalapi silently refreshes its access token; raw HTTP calls expire after about a day.

**Still open:**
1. ⚠ Appears-on / compilation releases: are they reachable at all through tidalapi?
2. ⚠ Is **tiddl** usable as a library (single track by ID, skip-existing behavior)? Blocked until the Lidarr search run is done (needs Tidal calls).
3. ⚠ Real Tidal rate limits: measure only while nothing else is using the account, with a hard stop at the first 429 and `Retry-After` captured through tidalapi's session.
4. Opus vs AAC for the lossy fallback: does the FiiO Echo Mini play Opus? Jellyfin/Navidrome/MA support? (Do not assume.)
5. Discovery source: `get_similar()` quality vs Last.fm vs ListenBrainz.
6. UI: server-rendered HTMX (default) vs a small SPA.
7. Naming: confirm `Artist/Album/NN - Title.flac`.
8. Card: how is "what goes on the card" chosen when the library (139 GB) is bigger than the card (119 GiB)? Does the Echo Mini need anything beyond what `fiio-format-card` already does?
9. Card agent auth and transport: API key over Tailscale to Unraid; rsync daemon vs HTTPS for the bulk copy.

*Original numbered list (kept for reference):*

1. ⚠ Does `tidalapi` return **all** of an artist's releases (albums, EPs/singles, "other", appears-on) and **ISRC + UPC** per track/release reliably? (Will Paquin's 20 releases were found via `get_albums` + `get_ep_singles` + `get_other`; appears-on and "compilations" not tested.)
2. ⚠ Is **tiddl** usable as a library (not just CLI), can it download single tracks by ID, and how does it skip existing files?
3. ⚠ Real Tidal rate limits: how many calls/min before 429, and does the limit differ between metadata calls and stream/download calls?
4. ⚠ Why does `tidal-dl-ng dl https://tidal.com/browse/album/<id>` return "Media not found" for every Will Paquin ID (all 9 failed, still unexplained)? Possibly region/availability or a token scope problem; decide before choosing the downloader.
5. Opus vs AAC for the lossy fallback: does the FiiO Echo Mini play Opus? Jellyfin/Navidrome/MA transcoding and tag support? (Do not assume.)
6. Discovery source: Tidal similar-artists vs Last.fm vs ListenBrainz.
7. UI: pure server-rendered HTMX vs a small SPA. Default: HTMX, because it's less to maintain.
9. Card: how should "what goes on the card" be chosen when the library is bigger than the card (explicit pins, favorites, recently added, listening data from Navidrome/MA)? And does the FiiO Echo Mini require anything beyond the formatting rules above that `fiio-format-card` doesn't already cover?
10. Card agent auth and transport: API key over Tailscale to Unraid, rsync daemon vs HTTPS for the bulk copy (earlier preference: parallel + unencrypted for LAN transfers).
8. Naming: confirm that keeping `Artist/Album/NN - Title.flac` is what you want (it matches the card and Lidarr's current layout).

## 8b. Findings added 2026-10-22

**Spike 2 (tiddl as a library): answered from source (tiddl 3.4.3, no Tidal calls made).**
- `tiddl.core` is a clean, importable layer: `core.api.TidalAPI` (`get_album`, `get_artist_albums`, `get_track`, `get_track_stream(track_id, quality)`, `get_search`, ...), `core.auth`, `core.metadata` (tagging, cover), `core.utils.ffmpeg`. The `Downloader` class lives under `cli/commands/download/` and is tied to the CLI (rich output), so Cadence should call `core` directly or shell out per track (`tiddl download -q max -p <dir> url track/<id>`, which works today).
- **Skip logic is path-based** (`existing_file_path.exists()` under `--scan-path`), not recording-based. It does not know about ISRCs, so Cadence's ledger must decide *whether* to fetch before calling tiddl.
- The API client caches responses (requests-cache) and retries only JSON-decode errors (2 s delay). **There is no 429 backoff**, so the Tidal gate in §6 is Cadence's job.
- CLI syntax gotcha: in 3.4.3 options go on the `download` group, before `url`: `tiddl download -q max -p DIR url <URL>`; `-q` after `url` fails. Quality `max` returned real 16/44.1 FLAC where tidal-dl-ng returned AAC for the same track.

**M1 step 1: read-only library scan** (`m1/scan_library.py`, run in the `ytdlp-fallback` image with the music folder mounted read-only; output `m1/data/library_scan.jsonl`; 5,210 files in about 1 minute, 0 read errors, nothing written to the library).
- **Size:** 5,210 files, 159 GB, 192 top-level artist folders, always `Artist/Album/file` (depth 2). 4,252 FLAC 16-bit, 896 FLAC 24-bit, 62 AAC (the 800 Cherries fallback album).
- **Tags are in good shape:** artist/album/title 100%, albumartist 99.7%, track number 99.7%, embedded art 98.4%, date 93%.
- **ISRC is present on only 5.9% (309 files).** The legacy import therefore cannot key on ISRC; it needs fuzzy matching on normalized (artist, title, duration +-2 s) against Tidal's catalog, with ISRC used only where present. 54.5% carry a MusicBrainz track id (from Lidarr), usable as a secondary hint.
- **Real duplicates exist:** 199 groups (407 files, about 8%) share artist + title + similar duration across different folders, e.g. a single and the album it came from (Arctic Monkeys "Do I Wanna Know" in both `AM/` and its own single folder). They are legitimate files, so the ledger must record "one recording, several files" and never delete.
- **Folder names are not trustworthy as artist names:** 295 files (5.7%) sit in folders that differ from their tags, e.g. `Alex G (American indie singer-songwriter)/` (Lidarr's MusicBrainz disambiguation leaked into the folder name) while the tag says `Alex G`. Cadence should identify artists from tags and normalize folder names only for new files.
- **Two filename styles coexist:** 2,704 `NN - Title.flac` and 2,506 `Artist-Title.flac` (older tidal-dl-ng downloads). The new tool keeps `NN - Title` for new files and leaves old ones alone.

**Sources considered and set aside (2026-10-23):** OMDB (Openmusic Database dump) was evaluated and dropped because the dump was unobtainable; it also has no ISRC/UPC/MBID fields and is a frozen snapshot. RateYourMusic has no public API. Discogs stays an optional enrichment source (public reads work without a token at 25 requests/min). In a one-artist test (Will Paquin) Tidal had 18 own releases, MusicBrainz 14, Discogs 7.

## 8c. Spike 5: YouTube Music as fallback + gap-finder (2026-10-25)

Read-only, unauthenticated `ytmusicapi` 1.12.3 run inside `ytdlp-fallback:local`, 1 request/s. Scripts: `spikes/s4_ytm_catalog.py`, `spikes/s4_compare.py`; data `spikes/out/ytm_*.json`.

- **Will Paquin: YT Music equals Tidal.** 18 of 18 own releases, every recording matched by title and duration (±3 s), no extras, nothing unavailable. Neither service has the two MusicBrainz-only releases (Vol. 1, Slo Loris remix). So for Tidal-complete artists YT Music adds nothing; its value is as a *second opinion* and a *fallback download source*.
- **Where it does find things (vs the library):** Nujabes, 13 own releases on YT Music, 5 not in the library (*Kei Nishikori meets Nujabes* 2016, the *Kiss of Life* single, three 12" EPs). Grimes, 29 own releases, 8 not in the library (remix singles, demos, *You'll miss me when I'm not around (Remixes)*, plus *Miss Anthropocene*, which may be a title variant of what we own, not yet checked).
- **Noise to filter:** some "missing" items are variants of things we own (*Pretty Dark (Demo)*, *We Appreciate Power (Radio Edit)*, "12inch Ver."). The gap-finder must classify at recording level (duration ±3 s) and label variant words (demo, radio edit, 12inch, remix, live) as **variant**, not **missing**. Never count titles alone.
- **Library quirk:** *Kiss of Life* exists in the library but YT Music lists it as a separate single with a longer title (`Kiss Of Life (feat. ...)`). Same class of problem, same fix.
- **API gotcha:** `get_artist_albums(channelId, params)` raises on 1.12.3 (it posts the channel id; the grid now needs the section's own `MPAD...` browse id). Singles shelf on the artist page is a 10-item teaser (Will: 10 shown, 17 real; Grimes: 10 shown, 26 real), so the full-list call is required. Workaround is in `s4_ytm_catalog.py::full_grid`; pin the version.
- **No ISRC/UPC on YT Music.** Matching is by normalized title + duration only, so every YT-sourced file stays `confidence=fuzzy` in the ledger until Tidal can confirm it.
- **Not tested:** Her's (my shell quoting broke on the apostrophe; not an API failure), Opus-vs-AAC on the FiiO, downloads (the sidecar already proves yt-dlp AAC 256k with Premium cookies), appears-on coverage.

## 9. Risks

| Risk | Mitigation |
|------|-----------|
| Tidal API changes or account restrictions | Swappable downloader, pinned versions, global rate gate, personal-use volumes, Lidarr kept as a cold standby |
| Wrong de-dupe deleting or hiding good files | No deletions ever; trash dir; every merge decision logged; "why" shown in the UI |
| Scope creep into rebuilding all of Lidarr | Scope list in §2 is the contract |
| Single-maintainer bus factor | Small codebase, plain SQLite, plain files on disk (library stays usable without the app) |
| Migration mistakes | Read-only milestone first, side-by-side run, backups of Lidarr/Aurral DBs before cutover |

## 10. Existing assets to reuse

- `ytdlp-fallback` matcher and tagging (`/mnt/user/appdata/ytdlp-fallback/build/fallback.py`)
- Hook scripts `ma-sync.sh` and `jellyfin-sync.sh` (`/mnt/user/appdata/lidarr/scripts/`)
- `fiio-format-card`, `tidal-card-sync` (Mac, unchanged if naming is kept)
- Unraid template pattern and `rebuild_container` (see the music stack doc)
- Lidarr's existing library DB as a reference for the legacy import (MusicBrainz IDs in tags)

## 11. Backend and system plan (draft 2026-10-25)

**Catalog decision (settled):** Tidal is the primary catalog and fetch source. YouTube Music is (a) a **fallback fetch source** for tracks Tidal cannot stream, and (b) a **gap-finder** that proposes releases Tidal or the library lack. MusicBrainz and Discogs are optional enrichment only. Every release records which provider(s) saw it.

### 11.1 Process model
One container `cadence` on Unraid, two processes sharing one SQLite file (WAL):
- `web`: FastAPI + uvicorn. Serves the UI, the JSON API and an SSE stream. Never talks to Tidal itself except search/lookup, and those go through the gate.
- `worker`: asyncio loop. Scheduler, job runner, gate, hooks. All writes to the library happen here only.
- Mac `cadence-card` agent (launchd `StartOnMount`) is a separate small program; see §5a.

### 11.2 Code layout
```
cadence/
  sources/    base.py (CatalogSource, FetchSource)  tidal.py  ytm.py  mb.py(optional)
  identity/   normalize.py  matcher.py  grouping.py        # the hard part; heavy tests
  ledger/     db.py  migrations/NNN_*.sql  models.py
  gate/       bucket.py  (one bucket per provider, global pause on 429)
  pipeline/   fetch.py verify.py tag.py place.py upgrade.py fallback.py
  gaps/       finder.py classify.py                          # YT Music vs Tidal vs library
  hooks/      music_assistant.py jellyfin.py navidrome.py ntfy.py ha.py
  card/       profile.py manifest.py api.py
  scheduler/  jobs.py
  web/        routes_ui.py routes_api.py templates/ static/
  cli.py      # every pipeline stage runnable by hand, with --dry-run
```
Rule: `sources/*` are the only modules that import tidalapi, tiddl, ytmusicapi or yt-dlp, so any of them can break or be swapped without touching the ledger.

### 11.3 Source interface
- `CatalogSource`: `search(q)`, `artist_releases(artist_ref)`, `release_tracks(release_ref)`. Returns plain dataclasses (title, date, type, tracks with isrc?/duration/explicit/availability).
- `FetchSource`: `fetch(track_ref, quality) -> staged file path`. Tidal uses `tiddl.core`/CLI per track; YT Music uses yt-dlp (AAC 256k with Premium cookies, Deno for JS).
- Providers have a `confidence`: Tidal rows are `exact` (ISRC/UPC); YT Music rows are `fuzzy` (title+duration) until a Tidal match confirms them.

### 11.4 Jobs and state machine
`queued → gated → fetching → verifying → tagging → placed → hooked → done`, or `failed(attempts, not_before)`. Properties:
- **Idempotent and crash-safe:** each stage writes to staging, then an atomic rename; on restart the worker resumes from the last recorded stage. Nothing is ever deleted; replaced files go to `trash/`.
- **Ledger decides, tiddl only downloads.** tiddl's skip logic is path-based, so Cadence checks the ledger first.
- Kinds: `fetch`, `upgrade`, `fallback`, `gapscan`, `refresh`, `rescan`, `card`.
- Every transition appends to `event_log` with a human reason ("Tidal 429, paused 40 min"), which the UI shows verbatim.

### 11.5 Gates (rate limits)
| Provider | Sustained | Notes |
|----------|-----------|-------|
| Tidal API | start at 1 req / 2 s, burst 3 | On first 429: pause **all** Tidal jobs, honor `Retry-After` if present else back off 15 min → 1 h → 4 h; one ntfy, not one per job. Real limits still unmeasured (spike 3) |
| Tidal downloads | 1 concurrent | tiddl has no backoff of its own |
| YT Music API | 1 req / s | Unauthenticated; used by gap scans and fallback matching |
| yt-dlp downloads | 1 concurrent, 3–8 s sleeps, 3 retries with fresh extraction | Cookie health check before each pass; if 128k or 403s return, flag "cookies expired" in the UI |
Gap scans run at low priority and only when the Tidal gate is idle.

### 11.6 Fallback policy
A wanted track becomes a fallback candidate when Tidal has no stream or no matching release for **3 days** (same grace as today). Match on YT Music: album title similarity ≥85, artist match, ≥80% of tracks mapped by core-title + duration ±3 s (the proven `ytdlp-fallback` matcher; keep its owned-elsewhere guard). Result is stored `source=ytm, confidence=fuzzy, upgradable=1`; the upgrade job retries Tidal every 14 days. Format AAC 256k for now; Opus only after a FiiO playback test.

### 11.7 Gap-finder
Nightly, per followed artist (and on demand from the artist page): pull YT Music + Tidal catalogs, compare at **recording level** against the ledger, and write `suggestion` rows:
- `missing_release`: recordings neither owned nor on Tidal's list, or on Tidal but not wanted.
- `variant`: demo / radio edit / 12inch / remix / live / slowed of something owned (off by default).
- `possible_duplicate`: same recording owned under another title.
- `only_on_ytm`: needs the lossy fallback, labeled as such.
Suggestions are **never auto-downloaded**; they show up in the Inbox with a one-tap Grab (with destination chips). Per-artist setting can turn on auto-grab for `missing_release` only.

### 11.8 Data additions
```
release_source(release_id, provider, provider_ref, seen_at)      -- which service saw it
suggestion(id, artist_id, kind, release_ref, evidence_json, state[new|grabbed|dismissed], created_at)
file.confidence[exact|fuzzy|legacy]    file.upgradable
setting(key, value)                    -- UI-editable config
```
Migrations are plain numbered `.sql` files applied at startup; a nightly online backup of the DB to `appdata/cadence/backups/` keeps 14.

### 11.9 API and realtime
- `/api/*` JSON (artists, releases, want, jobs, suggestions, card, settings, health). `/ui/*` returns HTML fragments for HTMX. `GET /events` is SSE for the queue, gate status and card state.
- **Access:** bind to the Tailscale/LAN address only, no login (single user), same trust model as your other Unraid apps. The card agent and ntfy action buttons use a static API key (`Authorization: Bearer`), stored 0600 in appdata.
- **Paste-a-link:** `POST /api/resolve {url}` accepts Tidal and YouTube Music URLs (you already do this by hand) and returns what it is plus a request sheet.

### 11.10 Notifications (ntfy, topic `music`)
Immediate only when you must act: Tidal re-auth, cookies expired, queue paused > 1 h, unknown card inserted, proposed card removals. Everything else (new releases, gaps found, downloads finished) is a **daily digest** with action buttons. Matches your complaint about noisy automation.

### 11.11 Deployment, safety and testing
- Unraid template `my-cadence.xml` + `rebuild_container`, `--user 99:100`, image built from the repo `Dockerfile` (python 3.12-slim + ffmpeg + deno + pinned tidalapi/tiddl/ytmusicapi/yt-dlp/mutagen). Appdata on the cache pool, library mounted read-only until the `WRITE_ENABLED` switch is flipped at cutover.
- **Side-by-side with Lidarr:** Cadence starts in observe-only mode (M1), then write mode for a single test artist, then everything.
- **Tests:** golden tests for normalization and matching built from the JSON dumps we already have (Will, Nujabes, Grimes), a fake gate and fake sources, zero live Tidal calls in tests. Real-library checks run read-only against `library_scan.jsonl`.
- Nix aside: the pinned Python toolchain (and a `nix flake` dev shell for the Mac) would make "works on my machine" a solved problem, and `dockerTools.buildImage` could build this very container reproducibly. The Unraid host itself won't run it, so it stays a dev-machine nicety for now.

## 12. UI / UX plan (draft 2026-10-25)

**Principles:** phone first, one-handed. The app's job is to answer three questions on its first screen: *what's new, what's missing, what needs me?* Every status has a "why" you can tap. Inbox-style triage instead of a wall of settings. Destination (Server / FiiO / Both) is a first-class control everywhere something can be requested.

### 12.1 Navigation
Bottom tab bar on phone, left rail on desktop: **Inbox · Library · Add · Activity · FiiO**, with Settings behind a gear. A small status strip under the header shows the gate ("Tidal OK" / "Tidal cooling 38 min") and card state.

### 12.2 Screens
1. **Inbox (home).** Cards you clear like email: *New releases* from followed artists, *Gaps found*, *Needs attention*, *Fetching now*. Each card has Grab (with destination chips) and Ignore, and Undo toast. Empty state says "nothing needs you".
2. **Add.** One search box that hits Tidal (cached, through the gate) and accepts a pasted Tidal/YouTube Music link. Results grouped Artists / Albums / Tracks. Tapping opens the request sheet: destination chips (remembers last choice), "all releases / albums only / new only", include variants toggle. Fallback availability is shown ("Tidal: lossless · not on Tidal, YT Music: AAC").
3. **Artist page.** Header with art, Follow toggle, default destination, policy. Release grid with state badges: *Owned*, *Partial 7/9*, *Missing*, *Fetching*, *Lossy*, *On card*. Filter chips: Albums, EPs, Singles, Appears on, Variants. "Get all missing" button. Release sheet shows the tracklist with per-track ownership, file facts (format, bit depth, source, confidence) and a history of what happened.
4. **Library.** Artist list with search, sort (recently added, missing count, size), and a "needs attention" filter (duplicates, lossy, missing cover, unmapped legacy).
5. **Activity.** Live queue with stage progress, gate status, Pause all / Retry / Cancel, then a filterable history log. Failures say what to do next.
6. **FiiO.** Card connected or not, last seen, capacity meter (used / budget / free), the **set-aside list** (drag to prioritize, pin), a *won't fit* list, *proposed removals* that need your confirm, and the last sync report. First-time card: register prompt.
7. **Settings.** Sources (Tidal auth state, YT cookie health), quality/format policy, schedules, notifications, hooks, paths, dry-run/write switch.

### 12.3 Shared components
Destination chips (Server / FiiO / Both), state badges, size meter, "why?" popover, undo toast, bottom-sheet request dialog, skeleton loaders. Tap targets ≥44 px, no hover-only controls, works in light and dark.

### 12.4 Visual direction (DECIDED 2026-10-27: koi pond)
Decided: a pixel-art **koi pond** look (16-bit sprites, teal/lily-green highlights, laid-back dark water tones, audiophile details such as a turntable, VU meter and cassette). No existing design system is reused. Tokens, sprites, screens and rules are in the `cadence-koi-ui` repo (`HANDOFF.md`, `tokens.css`, `design/`) and on the live canvas https://claude.ai/artifact/6YfnW56BrZAwfcMyhB8yPh. The earlier options (Ender "Lily Pond" tokens, or a neutral dark player look) were not chosen.

### 12.5 Frontend stack
Server-rendered Jinja + HTMX + a little Alpine, SSE for live parts, plain CSS with tokens, no build step, installable PWA. The one rich widget (drag-to-order on the FiiO list) uses SortableJS as an island. Escape hatch: if a screen outgrows this, that screen alone becomes a small SPA.

### 12.6 Empty / error states to design up front
No Tidal auth; Tidal paused; cookies expired; card read-only; card unknown; library mount missing; nothing to show; partial album owned; search returned only fuzzy YT matches.

### 12.7 Research-backed rules (2026-10-25)

Evidence quality: platform/standards docs (Material 3, WCAG, MDN, ntfy, htmx) are solid; music-app styling and client preferences come from forum threads and product docs (anecdotal); I found **no** usable source on Lidarr's UX or Seerr's request-modal layout, so our own §1 observations stand in for those.

**Navigation and touch**
- Bottom bar holds 3–5 top-level destinations (Material); we are at the cap with 5 (Inbox, Library, Add, Activity, FiiO), so Settings stays behind a gear and nothing else may be added without removing something.
- Primary tap targets 48 px (Material); never below 44 px (WCAG 2.5.5 AAA); WCAG 2.5.8 AA floor is 24 px. Visible icon may be smaller than its padded hit area.

**Dark theme and color**
- No pure black: darkest surface around 10% lightness (web.dev) or `#121212`-ish; layered surfaces by tone ("surface container" steps), not shadows. Set `color-scheme: dark light` so native controls and scrollbars match.
- Text ≥4.5:1 against every surface it sits on (WCAG AA). Keep vivid accent colors small (buttons, badges), not large fills.
- **Album-art color: tint, don't flood.** Apple Music's 2026 adaptive design drew complaints for blinding bright-art screens at night. Rules: extract a palette once at ingest and cache it (Namida does), clamp luminance in dark mode, apply it only as a subtle header/artwork-adjacent tint, keep text and control colors on fixed contrast-checked tokens (never raw extracted colors), and ship a "static theme" switch.

**Job, queue and status UX**
- A spinner is never enough. Each job shows its stage ("2 of 5: verifying"), queue position, and the **reason for any wait** ("Tidal cooling down, resumes 14:20"). Circular indicator for unknown duration, linear when measurable. Long jobs are cancellable and safe to leave.
- Errors are actionable: what failed, what Cadence will do next, and one button (Retry / Fix auth / Skip).
- Status changes announce via `aria-live="polite"`; progress uses `role="progressbar"`.
- Lifecycle vocabulary borrowed from the request-manager world (Seerr): **requested → queued → downloading → available**, plus ours: *lossy*, *on card*. The same words everywhere.
- Requesting is one tap with remembered defaults; advanced options (policy, variants, quality) are collapsed.

**Notifications and Inbox**
- Each message states what happened, why it matters, what you can do (Courier/MagicBell guidance). Group by key (one digest line per artist, not per track). Urgent items bypass the digest. Inbox = persistent/non-urgent, toast = confirmation, push = rare.
- Auto-archive handled items so the Inbox never becomes a 500-unread graveyard; log which suggestions you grab vs dismiss and use that to tune the gap-finder.
- ntfy specifics: action buttons support `view`, `http`, `broadcast` (Android only) and `copy`; `clear=true` dismisses after tap; JSON publishing goes to the server root URL, not the topic URL; priority ≥4 pops over on Android. Since our topic has no auth, **action buttons must carry single-use signed tokens, not the long-lived API key**, because anyone subscribed to the topic can read the payload (design inference, not a documented ntfy rule).

**Frontend plumbing**
- **htmx version:** 2.x is still npm `latest`; 4.0.0 shipped 2026-08-28 and is opt-in. Default: **pin 2.x** (most docs and examples, extension-based SSE). Revisit 4.x later; its SSE resumes with `Last-Event-ID` and jittered backoff, and it needs `:inherited` modifiers on attribute inheritance. Whichever we pick, pin the exact version.
- **SSE on a phone:** 2.x reconnects with exponential backoff (about 0.5 s up to 64 s) but starts a fresh EventSource, so no `Last-Event-ID`. Fix on our side: the server sends a **full state snapshot on every (re)connect**. Also close the stream on `pagehide` so back/forward-cached pages don't hold connections open.
- **PWA needs HTTPS** to be installable and to run a service worker (localhost excepted). Plain `http://10.0.0.48:port` will not install. Plan: serve through Tailscale Serve (HTTPS on the tailnet name), the same way the Ender Tracker already is.
- Manifest with 192 px and 512 px icons; service worker caches the app shell and static assets only, with an offline page. **Never cache API or fragment responses**, because stale queue or ownership state is worse than an error.
- Library-first browsing wins in the clients people praise (Feishin on desktop, Symfonium on mobile, both anecdotal): artist → releases, fast search, artist metadata. Cadence is not a player, so it skips player chrome and links out to Navidrome/MA to play.

## 13. Milestones (revised 2026-10-25)

| M | Deliverable | Exit test |
|---|-------------|-----------|
| 1 (in progress) | Ledger + Tidal catalog sync (192 artists, cached, gated) + legacy matching | Will Paquin report is exact; false "missing" rate on owned albums = 0 |
| 1b | **Gap sweep:** YT Music catalogs for the same artists, recording-level diff, static HTML report | Nujabes/Grimes results reproduce; variants labeled, not "missing" |
| 2 | Fetch pipeline (CLI) with gate, verify, tag, place, hooks, dry-run | Download a missing release; MA/Jellyfin/Navidrome see it |
| 2b | Card profile + Mac agent | Plug in the card: correct files, fits budget, nothing deleted |
| 3 | UI skeleton: Inbox, Add, Artist, Activity (read-mostly), PWA shell | Request an artist from the phone, watch it land |
| 4 | Auto-grab + upgrade + follow + digest notifications | A new release arrives with no manual step |
| 5 | Fallback worker (YT Music) + FiiO screen + Library screen | Parity with `ytdlp-fallback`'s done list |
| 6 | Cutover beside Lidarr/Aurral | Two weeks no discrepancies; Lidarr stopped, not deleted |

**Decisions for the owner:** (1) visual direction (§12.4), (2) whether gap suggestions ever auto-grab (§11.7 default: never), (3) web access model (§11.9 default: Tailscale/LAN only, no login), (4) Opus vs AAC for fallback after a FiiO playback test.
