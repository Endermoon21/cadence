-- Schema v5: Tidal's album volume (disc) count. NULL until a catalog sync has stored it; the fetch plan then
-- falls back to counting the distinct discs among the release's available tracks.
ALTER TABLE release ADD COLUMN num_volumes INTEGER;
