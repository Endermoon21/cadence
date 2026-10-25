-- Schema v2: derived duplicate grouping. Nothing is deleted; both columns are recomputed by `cadence regroup`.
-- recording.canon_id: the canonical recording of the cluster this recording belongs to (the same song across
--   editions / remasters). NULL or self = this recording is its own canonical.
-- release.group_key (already in v1): editions of one release share a key, see identity/grouping.py.

ALTER TABLE recording ADD COLUMN canon_id INTEGER REFERENCES recording(id);
CREATE INDEX recording_canon ON recording(canon_id);
CREATE INDEX release_artist_group ON release(artist_id, group_key);
