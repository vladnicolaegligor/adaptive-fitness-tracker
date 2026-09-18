-- Body composition alongside the weigh-in.
--
-- These land as columns on body_log rather than a separate body_comp table: a
-- BIA scale emits weight and composition as ONE reading, body_log.date is
-- already UNIQUE, and log_weight's ON CONFLICT(date) DO UPDATE already gives
-- the correction semantics. A second table would duplicate the key, duplicate
-- the upsert, and add a join to every read for nothing.
--
-- NOT idempotent: SQLite ALTER TABLE ADD COLUMN has no IF NOT EXISTS, so a
-- second run fails with "duplicate column name". Applied once, by hand.
--
-- Every field is nullable — weigh-ins from a dumb scale, or from before this
-- migration, carry weight only. Readers MUST filter IS NOT NULL rather than
-- averaging over the gaps.

ALTER TABLE body_log ADD COLUMN body_fat_pct  REAL;
ALTER TABLE body_log ADD COLUMN water_pct     REAL;
ALTER TABLE body_log ADD COLUMN muscle_pct    REAL;
ALTER TABLE body_log ADD COLUMN bone_mass_kg  REAL;
ALTER TABLE body_log ADD COLUMN visceral_fat  REAL;

-- Which instrument produced the composition figures. Single-frequency
-- foot-to-foot BIA is a model, not a measurement: absolute values are unfit to
-- report, only the trend under matched conditions is. Named so the UI can say
-- so, and so a later DEXA reading is distinguishable from scale output.
ALTER TABLE body_log ADD COLUMN comp_source   TEXT;
