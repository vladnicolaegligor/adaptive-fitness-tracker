-- Health tracking: weight, nutrition, training.
-- Applied once, in order, against the database. Nothing creates these at
-- runtime: several processes issuing CREATE TABLE IF NOT EXISTS is how schemas
-- drift apart.
--
-- Deliberately NOT reusing lifestyle_logs: it is AG-owned and carries
-- CHECK(type IN ('gym','sleep','feeling','sick')), and its value/notes shape
-- cannot hold a set-by-set workout.

CREATE TABLE IF NOT EXISTS body_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  date       TEXT NOT NULL UNIQUE,          -- one weigh-in per day; re-logging updates
  weight_kg  REAL NOT NULL,
  notes      TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- raw = verbatim what was said. Macros are model estimates (estimated=1) and
-- must never be rendered as if measured. Keeping both means the estimate can be
-- redone later without losing the source.
CREATE TABLE IF NOT EXISTS meal_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  date       TEXT NOT NULL,
  slot       TEXT NOT NULL CHECK(slot IN ('breakfast','lunch','dinner','snack')),
  raw        TEXT NOT NULL,
  kcal       REAL,
  protein_g  REAL,
  carbs_g    REAL,
  fat_g      REAL,
  estimated  INTEGER NOT NULL DEFAULT 1,
  confidence TEXT CHECK(confidence IS NULL OR confidence IN ('low','med','high')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(date, slot, raw)                   -- guards literal double-insert
);
CREATE INDEX IF NOT EXISTS idx_meal_log_date ON meal_log(date);

CREATE TABLE IF NOT EXISTS workout_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  date         TEXT NOT NULL,
  type         TEXT NOT NULL,               -- strength|run|bike|swim|walk|other
  duration_min INTEGER,
  rpe          INTEGER CHECK(rpe IS NULL OR (rpe BETWEEN 1 AND 10)),
  notes        TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_workout_log_date ON workout_log(date);

-- exercise_slug is the normalized join key. Without it "bench press" / "BB
-- bench" / "Bench" fragment one volume trend into three dead lines.
CREATE TABLE IF NOT EXISTS workout_set (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  workout_id    INTEGER NOT NULL REFERENCES workout_log(id) ON DELETE CASCADE,
  exercise      TEXT NOT NULL,
  exercise_slug TEXT NOT NULL,
  set_no        INTEGER NOT NULL,
  reps          INTEGER,
  weight_kg     REAL,
  rpe           INTEGER CHECK(rpe IS NULL OR (rpe BETWEEN 1 AND 10)),
  UNIQUE(workout_id, exercise_slug, set_no)
);
CREATE INDEX IF NOT EXISTS idx_workout_set_slug ON workout_set(exercise_slug);
