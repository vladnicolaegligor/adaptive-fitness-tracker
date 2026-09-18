-- Health & nutrition schema.
-- Structure only: no rows. The CIQUAL tables ship empty on purpose —
-- the composition data is ANSES's to distribute, not mine.

CREATE TABLE body_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  date       TEXT NOT NULL UNIQUE,          -- one weigh-in per day; re-logging updates
  weight_kg  REAL NOT NULL,
  notes      TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
, body_fat_pct  REAL, water_pct     REAL, muscle_pct    REAL, bone_mass_kg  REAL, visceral_fat  REAL, comp_source   TEXT);

CREATE TABLE meal_log (
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
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')), fibre_g   REAL, sat_fat_g REAL, sugar_g   REAL, salt_g    REAL,
  UNIQUE(date, slot, raw)                   -- guards literal double-insert
);

CREATE INDEX idx_meal_log_date ON meal_log(date);

CREATE TABLE meal_nutrient (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  meal_id    INTEGER NOT NULL REFERENCES meal_log(id) ON DELETE CASCADE,
  -- slugify()d canonical name. The slug is the join key, so "vitamin D",
  -- "Vitamin D3" and "vit-d" must collapse before they get here.
  nutrient   TEXT NOT NULL,
  amount     REAL NOT NULL,
  -- Validated against NUTRIENTS in health_core: mixed mg/ug for one nutrient
  -- is the failure that makes this table worthless to sum later.
  unit       TEXT NOT NULL,
  confidence TEXT CHECK(confidence IS NULL OR confidence IN ('low','med','high')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(meal_id, nutrient)
);

CREATE INDEX idx_meal_nutrient_meal ON meal_nutrient(meal_id);

CREATE TABLE meal_component (
    id          INTEGER PRIMARY KEY,
    meal_id     INTEGER NOT NULL REFERENCES meal_log(id) ON DELETE CASCADE,
    food_slug   TEXT NOT NULL REFERENCES food(slug),
    grams       REAL NOT NULL CHECK (grams > 0),
    UNIQUE (meal_id, food_slug)
);

CREATE INDEX idx_meal_component_meal ON meal_component(meal_id);

CREATE TABLE "food" (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('ciqual', 'usda', 'off', 'label', 'estimate')),
    fdc_id      INTEGER,
    ciqual_code TEXT,
    barcode     TEXT,
    kcal        REAL,
    protein_g   REAL,
    carbs_g     REAL,
    fat_g       REAL,
    sat_fat_g   REAL,
    sugar_g     REAL,
    fibre_g     REAL,
    salt_g      REAL,
    notes       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
, micro_ciqual_code TEXT);

CREATE TABLE food_nutrient (
    food_slug   TEXT NOT NULL REFERENCES food(slug) ON DELETE CASCADE,
    nutrient    TEXT NOT NULL,
    amount      REAL NOT NULL,
    PRIMARY KEY (food_slug, nutrient)
);

CREATE INDEX idx_food_nutrient_slug ON food_nutrient(food_slug);

CREATE TABLE workout_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  date         TEXT NOT NULL,
  type         TEXT NOT NULL,               -- strength|run|bike|swim|walk|other
  duration_min INTEGER,
  rpe          INTEGER CHECK(rpe IS NULL OR (rpe BETWEEN 1 AND 10)),
  notes        TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
, garmin_id INTEGER REFERENCES garmin_activity(garmin_id), avg_hr    INTEGER, max_hr    INTEGER, calories  INTEGER);

CREATE INDEX idx_workout_log_date ON workout_log(date);

CREATE INDEX idx_workout_log_garmin ON workout_log(garmin_id);

CREATE TABLE workout_set (
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

CREATE INDEX idx_workout_set_slug ON workout_set(exercise_slug);

CREATE TABLE garmin_daily (
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

CREATE TABLE garmin_activity (
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

CREATE INDEX idx_garmin_activity_date ON garmin_activity(date);

CREATE TABLE garmin_exercise_set (
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

CREATE INDEX idx_garmin_exercise_set_activity
  ON garmin_exercise_set(garmin_id);

CREATE TABLE ciqual_food (
    alim_code   TEXT PRIMARY KEY,
    name_fr     TEXT NOT NULL,
    group_fr    TEXT,
    subgroup_fr TEXT
);

CREATE INDEX idx_ciqual_food_name ON ciqual_food(name_fr);

CREATE TABLE ciqual_value (
    alim_code   TEXT NOT NULL REFERENCES ciqual_food(alim_code) ON DELETE CASCADE,
    column_name TEXT NOT NULL,
    raw         TEXT,
    PRIMARY KEY (alim_code, column_name)
);
