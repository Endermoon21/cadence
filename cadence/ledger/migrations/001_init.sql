-- Cadence ledger, schema v1 (M1). See DESIGN.md §4 and §11.8.
-- Identity: a *recording* (ISRC when known) is what you own; releases and tracks are views onto recordings.
-- Never-delete rule: nothing in the app deletes library files; rows describe files, they don't own them.

CREATE TABLE artist (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  norm_name         TEXT NOT NULL UNIQUE,
  tidal_id          INTEGER UNIQUE,
  tidal_match       TEXT NOT NULL DEFAULT 'pending'
                    CHECK (tidal_match IN ('pending', 'auto', 'ambiguous', 'none', 'manual')),
  tidal_candidates  TEXT,                       -- JSON [{id, name}] when ambiguous, for review
  ytm_channel_id    TEXT,
  mbid              TEXT,
  followed          INTEGER NOT NULL DEFAULT 0,
  default_dest      TEXT NOT NULL DEFAULT 'server' CHECK (default_dest IN ('server', 'fiio', 'both')),
  catalog_synced_at TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE release (
  id            INTEGER PRIMARY KEY,
  artist_id     INTEGER NOT NULL REFERENCES artist(id),
  title         TEXT NOT NULL,
  norm_title    TEXT NOT NULL,
  version       TEXT,
  type          TEXT NOT NULL CHECK (type IN ('album', 'ep', 'single', 'compilation', 'other')),
  role          TEXT NOT NULL DEFAULT 'own' CHECK (role IN ('own', 'appears_on')),
  release_date  TEXT,
  explicit      INTEGER,
  num_tracks    INTEGER,
  upc           TEXT,
  available     INTEGER,
  group_key     TEXT,                         -- duplicate grouping (M1: unused, reserved)
  hidden_reason TEXT,                         -- set => excluded from "missing" (user or rule)
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX release_artist ON release(artist_id);

-- Which provider has seen a release, under which id. A release can be seen by several.
CREATE TABLE release_source (
  release_id   INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
  provider     TEXT NOT NULL CHECK (provider IN ('tidal', 'ytm', 'mb', 'discogs')),
  provider_ref TEXT NOT NULL,
  seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (provider, provider_ref)
);
CREATE INDEX release_source_release ON release_source(release_id);

CREATE TABLE recording (
  id         INTEGER PRIMARY KEY,
  isrc       TEXT UNIQUE,                     -- NULL allowed (several NULLs are fine in SQLite)
  artist_id  INTEGER NOT NULL REFERENCES artist(id),
  title      TEXT NOT NULL,
  norm_title TEXT NOT NULL,                   -- normalize.norm_title(title, version): includes variant label
  version    TEXT,
  variant    TEXT,                            -- e.g. 'instrumental', 'bloodpop remix', NULL for the main version
  duration_s INTEGER
);
CREATE INDEX recording_artist_title ON recording(artist_id, norm_title);

-- A slot on a release. Several tracks (single, album, EP) may point at one recording.
CREATE TABLE track (
  id           INTEGER PRIMARY KEY,
  release_id   INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
  recording_id INTEGER NOT NULL REFERENCES recording(id),
  tidal_id     INTEGER UNIQUE,
  disc         INTEGER NOT NULL DEFAULT 1,
  number       INTEGER NOT NULL,
  title        TEXT NOT NULL,
  version      TEXT,
  duration_s   INTEGER,
  explicit     INTEGER,
  available    INTEGER,
  UNIQUE (release_id, disc, number)
);
CREATE INDEX track_recording ON track(recording_id);

CREATE TABLE file (
  id             INTEGER PRIMARY KEY,
  path           TEXT NOT NULL UNIQUE,        -- relative to the library root
  artist_id      INTEGER REFERENCES artist(id),
  recording_id   INTEGER REFERENCES recording(id),
  match          TEXT NOT NULL DEFAULT 'unmatched'
                 CHECK (match IN ('isrc', 'fuzzy', 'manual', 'unmatched')),
  match_score    REAL,
  source         TEXT NOT NULL DEFAULT 'legacy' CHECK (source IN ('legacy', 'tidal', 'ytm')),
  codec          TEXT,
  sample_rate    INTEGER,
  bit_depth      INTEGER,
  bitrate        INTEGER,
  duration_s     REAL,
  size           INTEGER,
  mtime          INTEGER,
  has_art        INTEGER,
  tag_title      TEXT,
  tag_artist     TEXT,
  tag_albumartist TEXT,
  tag_album      TEXT,
  tag_track      TEXT,
  tag_disc       TEXT,
  tag_date       TEXT,
  tag_isrc       TEXT,
  tag_mb_track   TEXT,
  upgradable     INTEGER NOT NULL DEFAULT 0,
  seen_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX file_recording ON file(recording_id);
CREATE INDEX file_artist ON file(artist_id);

CREATE TABLE want (
  id         INTEGER PRIMARY KEY,
  artist_id  INTEGER REFERENCES artist(id),
  release_id INTEGER REFERENCES release(id),
  rule       TEXT NOT NULL DEFAULT 'all' CHECK (rule IN ('all', 'new_only', 'albums_only', 'ignore')),
  dest       TEXT NOT NULL DEFAULT 'server' CHECK (dest IN ('server', 'fiio', 'both')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK ((artist_id IS NULL) != (release_id IS NULL))
);

CREATE TABLE suggestion (
  id            INTEGER PRIMARY KEY,
  artist_id     INTEGER NOT NULL REFERENCES artist(id),
  kind          TEXT NOT NULL CHECK (kind IN ('missing_release', 'variant', 'possible_duplicate', 'only_on_ytm')),
  provider      TEXT NOT NULL,
  provider_ref  TEXT NOT NULL,
  title         TEXT NOT NULL,
  evidence_json TEXT,
  state         TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new', 'grabbed', 'dismissed')),
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (provider, provider_ref, kind)
);

CREATE TABLE job (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('fetch', 'upgrade', 'fallback', 'gapscan', 'refresh', 'rescan', 'card')),
  target     TEXT NOT NULL,
  stage      TEXT NOT NULL DEFAULT 'queued',
  state      TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  attempts   INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  not_before TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE event_log (
  id        INTEGER PRIMARY KEY,
  at        TEXT NOT NULL DEFAULT (datetime('now')),
  kind      TEXT NOT NULL,                    -- e.g. 'gate.pause', 'catalog.sync', 'match'
  subject   TEXT,
  message   TEXT NOT NULL,                    -- human-readable "why", shown verbatim in the UI
  data_json TEXT
);

-- Cached provider responses so re-runs don't spend Tidal calls. key e.g. 'tidal:artist_albums:12345:ALBUMS'
CREATE TABLE catalog_cache (
  key        TEXT PRIMARY KEY,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
  body       TEXT NOT NULL                    -- JSON of plain DTOs, never pickled library objects
);

CREATE TABLE setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE card (
  uuid      TEXT PRIMARY KEY,
  label     TEXT,
  capacity  INTEGER,
  budget    INTEGER,
  last_seen TEXT
);

CREATE TABLE card_state (
  card_uuid  TEXT NOT NULL REFERENCES card(uuid),
  release_id INTEGER NOT NULL REFERENCES release(id),
  state      TEXT NOT NULL CHECK (state IN ('queued', 'copied', 'removed_pending')),
  copied_at  TEXT,
  PRIMARY KEY (card_uuid, release_id)
);
