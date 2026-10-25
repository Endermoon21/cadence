-- Schema v3: artist aliases. A file tagged with `alias_norm` (norm_artist of the album-artist / artist tag)
-- belongs to `artist_id` instead of getting an artist row of its own. Managed by `cadence alias`.
CREATE TABLE artist_alias (
  alias_norm TEXT PRIMARY KEY,
  artist_id  INTEGER NOT NULL REFERENCES artist(id),
  note       TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
