-- Garmin Connect (Venu 3S) -> unified.db
--
-- Deliberately parallel to the hand-logged health tables, not merged into
-- them. body_log/meal_log/workout_log record what was chosen to report; these
-- record what the watch measured. Keeping them apart means a Garmin
-- "Strength" activity can never masquerade as, or overwrite, the per-set
-- workout_log entry for the same session — the two are joined by date at read
-- time instead, where a mismatch is visible rather than silently reconciled.

CREATE TABLE IF NOT EXISTS garmin_daily (
  date                   TEXT PRIMARY KEY,   -- Garmin's calendarDate, never derived
                                             -- from a timestamp: sleep spans midnight
  -- heart
  resting_hr             INTEGER,
  min_hr                 INTEGER,
  max_hr                 INTEGER,
  hrv_avg_ms             INTEGER,            -- overnight average, ms
  hrv_status             TEXT,               -- balanced|unbalanced|low|poor
  -- recovery
  body_battery_high      INTEGER,
  body_battery_low       INTEGER,
  body_battery_charged   INTEGER,
  body_battery_drained   INTEGER,
  stress_avg             INTEGER,
  stress_max             INTEGER,
  -- sleep
  sleep_score            INTEGER,
  sleep_quality          TEXT,
  sleep_total_min        INTEGER,
  sleep_deep_min         INTEGER,
  sleep_light_min        INTEGER,
  sleep_rem_min          INTEGER,
  sleep_awake_min        INTEGER,
  sleep_start            TEXT,               -- local ISO
  sleep_end              TEXT,
  respiration_avg        REAL,
  spo2_avg               INTEGER,
  spo2_min               INTEGER,
  -- movement
  steps                  INTEGER,
  floors                 INTEGER,
  distance_m             REAL,
  intensity_min_moderate INTEGER,
  intensity_min_vigorous INTEGER,
  -- burn: the number that makes a deficit real, next to meal_log's intake
  calories_total         INTEGER,
  calories_active        INTEGER,
  calories_bmr           INTEGER,
  vo2max                 REAL,
  -- The full payloads, minus per-minute time series (stripped before insert —
  -- they are megabytes a day and this DB is backed up nightly). Kept so a
  -- field I did not model is a re-read away rather than a re-sync.
  raw_json               TEXT,
  synced_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS garmin_activity (
  -- Garmin's own activityId is the PK, which is what makes a re-pull of an
  -- overlapping window a no-op instead of a duplicate.
  garmin_id    INTEGER PRIMARY KEY,
  date         TEXT NOT NULL,                -- from startTimeLocal, so a 23:00
                                             -- session lands on the day it felt like
  start_local  TEXT,
  type         TEXT,
  name         TEXT,
  duration_min REAL,
  distance_m   REAL,
  avg_hr       INTEGER,
  max_hr       INTEGER,
  calories     INTEGER,
  raw_json     TEXT,
  synced_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_garmin_activity_date ON garmin_activity(date);
