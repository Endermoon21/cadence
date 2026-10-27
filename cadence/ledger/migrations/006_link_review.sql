-- Schema v6: files `link` would not decide for. 'ambiguous': equal-rank candidates (an unlinked file stays
-- match='unmatched', a linked one keeps its link). 'no_longer_matches': a linked file matched nothing on rematch and
-- keeps its link. The row says why and who the candidates were. Cleared by `link` when the file resolves.
CREATE TABLE link_review (
  file_id     INTEGER PRIMARY KEY REFERENCES file(id) ON DELETE CASCADE,
  reason      TEXT NOT NULL CHECK (reason IN ('ambiguous', 'no_longer_matches')),
  candidates  TEXT NOT NULL,           -- JSON list of {recording_id, norm_title, duration_s, score}
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
