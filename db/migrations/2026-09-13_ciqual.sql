-- CIQUAL (ANSES, France) as the reference source for generic foods, replacing
-- USDA in that role.
--
-- Why the switch: for a European food supply, CIQUAL is what measures it.
-- It also carries three nutrients USDA handles poorly or not at all — iodine,
-- selenium and vitamin K2 (USDA has no K2 whatsoever) — and it declares SALT
-- directly, the way EU labels do, instead of requiring a sodium conversion.
-- Decisively, it has "Mache, crue" (fetica / lamb's lettuce) and "Tomate
-- cerise, crue" as distinct foods; USDA lacked the first entirely and returned
-- "Corn, sweet, white, raw" for it with full confidence.
--
-- Operationally it is a 3.6 MB file rather than a rate-limited API. The whole
-- table is staged locally, so every later lookup is plain SQL: no key, no
-- quota, no network, and reproducible offline.

-- SQLite cannot alter a CHECK constraint, so `food` is rebuilt to widen
-- `source`. Two new values: 'ciqual' (reference table) and 'off' (Open Food
-- Facts, barcode lookups — MACROS ONLY, see food_core for why its micros are
-- not trusted).
CREATE TABLE food_new (
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
);

INSERT INTO food_new (slug, name, source, fdc_id, barcode, kcal, protein_g,
                      carbs_g, fat_g, sat_fat_g, sugar_g, fibre_g, salt_g,
                      notes, updated_at)
    SELECT slug, name, source, fdc_id, barcode, kcal, protein_g, carbs_g,
           fat_g, sat_fat_g, sugar_g, fibre_g, salt_g, notes, updated_at
    FROM food;

DROP TABLE food;
ALTER TABLE food_new RENAME TO food;

-- The full CIQUAL table, staged verbatim. Values keep their source spelling
-- ('-', 'traces', '< 0,01', comma decimals) so parsing decisions stay visible
-- and revisable rather than baked in at load time.
CREATE TABLE ciqual_food (
    alim_code   TEXT PRIMARY KEY,
    name_fr     TEXT NOT NULL,
    group_fr    TEXT,
    subgroup_fr TEXT
);

CREATE TABLE ciqual_value (
    alim_code   TEXT NOT NULL REFERENCES ciqual_food(alim_code) ON DELETE CASCADE,
    column_name TEXT NOT NULL,
    raw         TEXT,
    PRIMARY KEY (alim_code, column_name)
);

CREATE INDEX idx_ciqual_food_name ON ciqual_food(name_fr);
