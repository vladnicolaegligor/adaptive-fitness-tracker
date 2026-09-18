-- Per-set detail from the watch: the sets as they were typed in, at the time.
--
-- The activity list only carries summarizedExerciseSets, which is aggregated
-- per exercise CATEGORY — and the category is the one part of a strength
-- session the watch infers by itself rather than being told. It infers it
-- badly: the top guess on a 2026-09-17 bench set scored 45% against
-- SHOULDER_PRESS on 33%, and a 40kg bench set was filed under LAT_PULLDOWN.
--
-- The per-set endpoint has what the summary loses: reps, load and duration for
-- each set in the order performed, plus the REST between them. Rest is
-- recorded nowhere else at all.
--
-- Parallel to workout_set, for the same reason garmin_activity is parallel to
-- workout_log: this is the watch's account, and it must never overwrite mine.
-- Aligning the two is an explicit import, not a merge.
CREATE TABLE IF NOT EXISTS garmin_exercise_set (
  garmin_id     INTEGER NOT NULL REFERENCES garmin_activity(garmin_id),
  set_no        INTEGER NOT NULL,          -- ACTIVE sets only, in performed order
  reps          INTEGER,
  weight_kg     REAL,                      -- payload is grams; stored in kg
  duration_s    REAL,
  rest_after_s  REAL,                      -- the REST entry following this set
  start_local   TEXT,
  -- The watch's own guess and how sure it was. Kept because it is genuinely
  -- useful as a hint and genuinely unreliable as a fact; storing the
  -- probability beside it is what stops it being read as the latter.
  category      TEXT,
  probability   REAL,
  synced_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (garmin_id, set_no)
);

CREATE INDEX IF NOT EXISTS idx_garmin_exercise_set_activity
  ON garmin_exercise_set(garmin_id);
