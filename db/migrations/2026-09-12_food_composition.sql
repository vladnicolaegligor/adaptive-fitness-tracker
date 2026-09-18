-- Food composition: per-100g reference data, and meals built from weighed
-- components instead of hand-computed totals.
--
-- Why this exists: micros were only ever as good as whatever a photographed
-- label happened to declare. Anything without a label (every vegetable) got a
-- from-memory estimate or nothing at all — on 2026-09-12 a full day of eating
-- recorded 5 nutrients against 21-28 on the days before it.
--
-- The structural win is recomputation. A meal stored as "34g lettuce, 22g
-- spinach" can be re-derived whenever the underlying food data improves; a meal
-- stored as one flat estimate is frozen at the moment it was guessed.

-- One row per distinct food, per 100g, whatever the source.
CREATE TABLE food (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    -- 'usda'     — imported from FoodData Central, fdc_id set
    -- 'label'    — read off packaging, barcode set where known. Beats USDA for
    --              branded items: it is the actual formulation, not an average.
    -- 'estimate' — neither available. Must be visible as such.
    source      TEXT NOT NULL CHECK (source IN ('usda', 'label', 'estimate')),
    fdc_id      INTEGER,
    barcode     TEXT,
    -- Macros per 100g. Same columns and units as meal_log so a rollup is a sum.
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
);

-- Micros per 100g. EAV against the same nutrient slugs and canonical units as
-- health_core.NUTRIENTS — a row in a unit that table does not name cannot be
-- summed with the rest, so writes go through the same validation.
CREATE TABLE food_nutrient (
    food_slug   TEXT NOT NULL REFERENCES food(slug) ON DELETE CASCADE,
    nutrient    TEXT NOT NULL,
    amount      REAL NOT NULL,
    PRIMARY KEY (food_slug, nutrient)
);

-- What a meal was actually made of. Optional: meals logged before this existed,
-- and meals eaten out where nothing was weighed, keep their flat columns on
-- meal_log and simply have no components.
CREATE TABLE meal_component (
    id          INTEGER PRIMARY KEY,
    meal_id     INTEGER NOT NULL REFERENCES meal_log(id) ON DELETE CASCADE,
    food_slug   TEXT NOT NULL REFERENCES food(slug),
    grams       REAL NOT NULL CHECK (grams > 0),
    UNIQUE (meal_id, food_slug)
);

CREATE INDEX idx_meal_component_meal ON meal_component(meal_id);
CREATE INDEX idx_food_nutrient_slug ON food_nutrient(food_slug);
