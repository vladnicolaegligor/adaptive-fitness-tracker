-- Micronutrients and the rest of the EU nutrition declaration.
--
-- Split into two shapes, on one discriminator: does it participate in a daily
-- total that gets reported back?
--
-- COLUMNS on meal_log — fibre, saturated fat, sugars, salt. All four are on
-- every EU declaration (they are mandatory), so they are dense, not sparse;
-- and all four are day-level "am I over" figures that day() sums alongside
-- kcal and the macros. Same argument as 2026-09-08_body_comp.sql: one reading,
-- one row, no join.
--
-- CHILD TABLE meal_nutrient — vitamins, minerals, and the non-nutrients worth
-- tracking anyway (caffeine, taurine, creatine). Open-ended and sparse: a
-- column each would be ~25 mostly-NULL columns today and a migration every
-- time a label lists something new.
--
-- Keyed on meal_id, deliberately. meal_log's upsert key is (date, slot, raw),
-- so correcting a row through log_meal means reproducing a long verbatim `raw`
-- string byte-for-byte. Nutrients keyed on the id log_meal already returns
-- sidestep that: they are a natural second call, and re-logging one corrects
-- it via ON CONFLICT rather than duplicating.
--
-- confidence is PER NUTRIENT, not inherited from the meal. A meal can be
-- high-confidence off a label while a single figure in it is a guess — an
-- energy drink's B-complex read from memory is not the same fact as calcium
-- off a cheese wrapper, and a table that flattens the two lies.
--
-- NOT idempotent: SQLite ALTER TABLE ADD COLUMN has no IF NOT EXISTS, so a
-- second run fails with "duplicate column name". Applied once, by hand.

ALTER TABLE meal_log ADD COLUMN fibre_g   REAL;
ALTER TABLE meal_log ADD COLUMN sat_fat_g REAL;
ALTER TABLE meal_log ADD COLUMN sugar_g   REAL;
ALTER TABLE meal_log ADD COLUMN salt_g    REAL;

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
