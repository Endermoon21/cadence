# Cadence M2: fetch pipeline (CLI)

`cadence fetch` downloads the tracks the ledger says are missing, through the Tidal gate, into staging, then verifies, tags, places and records them. Context: `DESIGN.md` §5 (Fetch, Folder naming, Editions), §6 (gate), §7 (M2).

```
cadence fetch [--artist NAME | --release TIDAL_RELEASE_ID] [--quality HI_RES_LOSSLESS|LOSSLESS|HIGH]
              [--staging DIR] [--library DIR] [--execute] [--limit N] [--include-variants]
```

**Dry run is the default.** Without `--execute` it prints the plan (target path, `would fetch` / `exists, skip`, Tidal track id, duration, destination) and makes no network call, no login and no write. `--library` (else `$CADENCE_LIBRARY`) is needed so it can say whether each target exists; `--staging` (else `$CADENCE_STAGING`) only for `--execute`. There are no built-in paths.

**Plan** (`fetch/plan.py`): one item per recording cluster with no linked file (library-wide), using the canonical recording's Tidal track (latest remaster, `cadence regroup`), preferring album over EP over single over compilation. Variants (instrumental, live, remix...) are left out unless `--include-variants`. Only artists with `tidal_match` auto/manual. Target is `Artist/Album/NN - Title.ext` from `ledger/naming.py`; the title carries Tidal's version (`Song (2009 Remaster)`). Run `cadence sync-catalog`, `link` and `regroup` first, or "missing" is stale.

**Stream** (`fetch/stream.py`): `resolve_stream(session, track_id, quality)` reads `tracks/<id>/playbackinfopostpaywall` with the requested `audioquality` and returns `StreamInfo`. It refuses encrypted streams, previews, non-stereo audio and manifest layouts it does not understand; it never decrypts. Direct-URL manifests are downloaded as is; DASH (hi-res) manifests are fetched as init + numbered segments by plain GET and remuxed (stream copy) by ffmpeg into `.flac`/`.m4a`.

**Run** (`fetch/run.py`): per track: skip if the target exists on disk or in the ledger (no Tidal call), `gate.call` the playback request, `gate.call` the download to `staging/<target>.part`, atomic rename in staging, ffmpeg decode check plus duration within 3 s of Tidal's, mutagen tags (FLAC: ALBUMARTIST = primary artist, TITLE, TRACKNUMBER, DISCNUMBER, ALBUM, ISRC; M4A: the equivalent atoms), rename into the library (staging and library must be on one filesystem), insert the `file` row (`source=tidal`, `match=manual`, `match_score=1`, `quality_requested`, `quality_received`), `fetch.placed` event, commit. It writes only under staging and library and never overwrites or deletes.

**Stops and exit codes.** A 429 (or a paused gate) stops the run with exit 3 and lists the remaining items. A 401 on one track's playback request logs `fetch.blocked` for that track and goes on, unless the 401 body names the token or session: that is an auth failure and stops the run (exit 1). Other per-track errors log `fetch.failed` and go on (exit 1 at the end); three dead connections in a row stop the run. Items whose destination is `fiio` only are skipped until M2b (the holding folder is not built).
