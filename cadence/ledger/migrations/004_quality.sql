-- Schema v4: what quality a file actually is (and what we asked for). All nullable: legacy rows have no request.
-- lossy: 1 = lossy codec, 0 = lossless, NULL = unknown. quality_received e.g. 'FLAC 16/44.1', 'AAC 320'.
ALTER TABLE file ADD COLUMN quality_requested TEXT;
ALTER TABLE file ADD COLUMN lossy INTEGER;
ALTER TABLE file ADD COLUMN quality_received TEXT;
CREATE INDEX file_lossy ON file(lossy);
