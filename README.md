# Cadence

A private music ledger and fetcher for one person's library. Tidal's catalogue is the source of truth, a SQLite ledger records which recordings are owned, and a planner works out what is missing and downloads it. It replaces Lidarr and Aurral.

The library is **lossless-only**. When Tidal serves a track as lossy AAC, Cadence tries other Tidal editions of the same recording, then OrpheusDL and Soulseek (the latter only behind a Mullvad VPN), and only then holds the AAC in staging instead of placing it.

> Personal project, private repo. No licence; not intended for others to run as-is.

## Status

| Area | State |
|---|---|
| Ledger, legacy import, catalogue sync, identity (`link`) | built |
| Fetch planner and executor, lossless gate, edition fallback | built, deployed |
| OrpheusDL + Soulseek fallbacks, Mullvad VPN gate | built, tested against fakes only, **not deployed** |
| Web UI (koi-pond design) | designed only, no web code |
| FiiO card sync, scheduler, notifications, library hooks | not built |

## Quick start

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # needs Python >= 3.12 and ffmpeg
.venv/bin/python -m pytest -q                                   # 767 tests, ~35 s, no network
.venv/bin/python -m cadence --help
```

Typical flow (each step has `--help`):

```bash
cadence init                              # create / migrate the ledger
cadence import-legacy library_scan.jsonl  # import a scan made by m1/scan_library.py
cadence sync-catalog --artist "Name"      # pull Tidal catalogues (network, gated)
cadence link --rematch                    # match files to recordings
cadence report --artist "Name"            # what is owned / missing
cadence fetch --artist "Name"             # DRY RUN: prints the plan, touches nothing
cadence fetch --artist "Name" --execute   # really download and place files
```

`fetch` is a dry run unless you pass `--execute`. Add `--fallback orpheus --fallback soulseek` (or set `CADENCE_FALLBACKS`) to use the lossless fallbacks.

## Configuration

Everything comes from flags or environment variables; no paths are built in. Secrets are never stored in this repo.

`CADENCE_DB`, `CADENCE_LIBRARY`, `CADENCE_STAGING`, `CADENCE_TIDAL_TOKEN`, `CADENCE_FALLBACKS`, and the `CADENCE_ORPHEUS_*`, `CADENCE_SLSKD_*` and `CADENCE_GLUETUN_*` families. The full table is in [docs/PROJECT.md](docs/PROJECT.md#10-cli-and-configuration).

## Safety rules

1. Never delete or overwrite a user file (placement is no-clobber; replacements would go through a trash directory).
2. Dry run is the default for every stage.
3. No `fetch --execute` without an explicit go-ahead.
4. Never print secrets (the Tidal token file, API keys).
5. Runs beside Lidarr until cutover.

## Layout

```
cadence/        the package: cli, sources/ (the only tidalapi import), gate/, identity/, catalog/,
                fetch/ (plan, run, stream, fallback), ledger/ (db, naming, quality, migrations/)
tests/          767 tests plus fakes; no live network
deploy/slskd/   run-slskd.sh: slskd behind gluetun / Mullvad on Unraid
m1/, spikes/    library scan script and throwaway experiments
docs/           PROJECT.md (full reference), FALLBACK_SPEC.md, M1_SPEC.md, M2_SPEC.md, CADENCE.md (superseded)
DESIGN.md       original design doc (partly historical)
```

## Documentation

- **[docs/PROJECT.md](docs/PROJECT.md)**: the complete, current reference (architecture, data model, planner, executor, fallbacks, deploy runbook, known issues, decisions).
- [docs/FALLBACK_SPEC.md](docs/FALLBACK_SPEC.md): spec for the OrpheusDL / Soulseek / Mullvad work.
- [DESIGN.md](DESIGN.md): the original design, including the UI/UX plan.
- UI design (koi pond): the separate private repo `Endermoon21/cadence-koi-ui`.
