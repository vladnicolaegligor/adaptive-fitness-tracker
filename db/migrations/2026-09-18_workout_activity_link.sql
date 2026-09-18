-- Correlate a logged session with the activity the watch recorded for it.
--
-- workout_log and garmin_activity were deliberately parallel: one holds what I
-- reported, the other what the watch measured, and merging them would let a
-- Garmin "Strength" row overwrite the per-set account of the same session.
-- That separation was right and the join was wrong — by date alone, which is
-- ambiguous the moment a day holds more than one session. 2026-09-16 has six
-- walks in each table; joining those by date is a six-by-six cross product.
--
-- The convention that grew instead was a Garmin activity id typed into the
-- free-text notes field ("Morning commute walk. Garmin activity 24382544432").
-- That is a foreign key living in prose: correct, and unqueryable.
--
-- So the link becomes a column, and the three numbers only the watch can know
-- are copied onto the session when it is linked. Copied, not joined at read
-- time: they are a measurement of THAT session, they never change afterwards,
-- and a set of loads next to the heart rate that produced them is the whole
-- point of correlating the two at all.
ALTER TABLE workout_log ADD COLUMN garmin_id INTEGER REFERENCES garmin_activity(garmin_id);
ALTER TABLE workout_log ADD COLUMN avg_hr    INTEGER;
ALTER TABLE workout_log ADD COLUMN max_hr    INTEGER;
ALTER TABLE workout_log ADD COLUMN calories  INTEGER;

CREATE INDEX IF NOT EXISTS idx_workout_log_garmin ON workout_log(garmin_id);
