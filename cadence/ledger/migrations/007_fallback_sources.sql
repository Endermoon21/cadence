-- Schema v7: file.source also accepts 'soulseek' and 'orpheus' (fallback sources for tracks Tidal serves lossy).
-- SQLite cannot alter a CHECK, so `file` is rebuilt inside the runner's transaction. foreign_keys is ON and cannot be
-- switched off inside a transaction, and DROP TABLE file cascades into link_review (ON DELETE CASCADE): its rows are
-- copied to a temp table first and put back after. link_review is the only table that references file.
CREATE TEMP TABLE link_review_bak AS SELECT * FROM link_review;

CREATE TABLE file_new (
  id             INTEGER PRIMARY KEY,
  path           TEXT NOT NULL UNIQUE,        -- relative to the library root
  artist_id      INTEGER REFERENCES artist(id),
  recording_id   INTEGER REFERENCES recording(id),
  match          TEXT NOT NULL DEFAULT 'unmatched'
                 CHECK (match IN ('isrc', 'fuzzy', 'manual', 'unmatched')),
  match_score    REAL,
  source         TEXT NOT NULL DEFAULT 'legacy' CHECK (source IN ('legacy', 'tidal', 'ytm', 'soulseek', 'orpheus')),
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
  seen_at        TEXT NOT NULL DEFAULT (datetime('now')),
  quality_requested TEXT,
  lossy          INTEGER,
  quality_received TEXT
);

INSERT INTO file_new(id, path, artist_id, recording_id, match, match_score, source, codec, sample_rate, bit_depth,
                     bitrate, duration_s, size, mtime, has_art, tag_title, tag_artist, tag_albumartist, tag_album,
                     tag_track, tag_disc, tag_date, tag_isrc, tag_mb_track, upgradable, seen_at, quality_requested,
                     lossy, quality_received)
SELECT id, path, artist_id, recording_id, match, match_score, source, codec, sample_rate, bit_depth,
       bitrate, duration_s, size, mtime, has_art, tag_title, tag_artist, tag_albumartist, tag_album,
       tag_track, tag_disc, tag_date, tag_isrc, tag_mb_track, upgradable, seen_at, quality_requested,
       lossy, quality_received
FROM file;

DROP TABLE file;
ALTER TABLE file_new RENAME TO file;
CREATE INDEX file_recording ON file(recording_id);
CREATE INDEX file_artist ON file(artist_id);
CREATE INDEX file_lossy ON file(lossy);

INSERT INTO link_review(file_id, reason, candidates, created_at)
SELECT file_id, reason, candidates, created_at FROM link_review_bak;
DROP TABLE link_review_bak;
