"""Food composition: per-100g reference data, and meals built from weights.

Before this, a meal's micronutrients were whatever a photographed label happened
to declare. Everything without a label — which is every vegetable — got a
from-memory estimate or nothing: a full day of eating on 2026-09-12 recorded 5
nutrients against 21-28 on the days before it.

Two things change. Composition comes from a cited source (USDA FoodData Central,
or a read label) instead of recall. And a meal is stored as what it was made of
— "34g lettuce, 22g spinach" — rather than as one collapsed total, so it can be
re-derived whenever the underlying food data improves. A meal stored as a total
is frozen at the moment it was guessed.

Network access lives in one function (`usda_fetch`). Everything else is pure or
touches only the DB, so the arithmetic is testable against a saved payload.

Schema: db/migrations/2026-09-12_food_composition.sql
"""
import json
import os
import re
import sqlite3
import unicodedata
import urllib.parse
import urllib.request

import health_core

USDA_API = "https://api.nal.usda.gov/fdc/v1"

# DEMO_KEY works but allows only ~30 requests/hour per IP, which a single
# afternoon of importing exhausts. A free personal key from
# fdc.nal.usda.gov/api-key-signup lifts it to 1000/hour; put it in the
# environment rather than in this file, which is in git.
USDA_API_KEY = os.environ.get("USDA_API_KEY", "DEMO_KEY")

# Which USDA dataset to believe when several describe the same food.
#
# This ordering is load-bearing, not cosmetic. Foundation and SR Legacy are
# laboratory analyses of the food itself; Survey (FNDDS) is modelled "as
# consumed" and differs sharply — raw spinach is 194ug folate in SR Legacy and
# 116ug in Survey, a 40% gap on the same words. Branded is last: it is
# manufacturer-submitted label data for a specific product, which is excellent
# when you want THAT product and misleading when you wanted the generic food.
DATASET_PRIORITY = ["Foundation", "SR Legacy", "Survey (FNDDS)", "Branded"]

# USDA nutrient NUMBER -> our slug. Numbers, not names: names drift between
# dataset releases ("Vitamin K (phylloquinone)" vs "Vitamin K1"), and a rename
# upstream would silently drop nutrients from every subsequent import rather
# than failing loudly.
USDA_NUTRIENTS = {
    "320": "vitamin-a",     "328": "vitamin-d",     "323": "vitamin-e",
    "430": "vitamin-k1",    "401": "vitamin-c",     "404": "vitamin-b1",
    "405": "vitamin-b2",    "406": "vitamin-b3",    "410": "vitamin-b5",
    "415": "vitamin-b6",    "418": "vitamin-b12",   "416": "biotin",
    "417": "folate",        "421": "choline",       "301": "calcium",
    "303": "iron",          "304": "magnesium",     "305": "phosphorus",
    "306": "potassium",     "307": "sodium",        "309": "zinc",
    "317": "selenium",      "314": "iodine",        "312": "copper",
    "315": "manganese",     "262": "caffeine",      "601": "cholesterol",
}

# USDA macro numbers -> our meal_log/food columns, all per 100g.
USDA_MACROS = {
    "208": "kcal", "203": "protein_g", "205": "carbs_g", "204": "fat_g",
    "606": "sat_fat_g", "269": "sugar_g", "291": "fibre_g",
}

# EU labels declare salt; USDA reports sodium. Without the conversion a USDA
# food contributes 0g salt to a day that in fact contained plenty.
SALT_PER_SODIUM = 2.5

MACRO_COLS = ("kcal", "protein_g", "carbs_g", "fat_g",
              "sat_fat_g", "sugar_g", "fibre_g", "salt_g")

# --- CIQUAL (ANSES, France) ----------------------------------------------
#
# The reference source for generic foods, in preference to USDA: it measures a
# European food supply, carries iodine, selenium and vitamin K2 (USDA has no K2
# at all), declares SALT directly the way EU labels do, and distinguishes foods
# USDA does not — "Mache, crue" and "Tomate cerise, crue" both exist here, while
# a USDA search for lamb's lettuce confidently returned "Corn, sweet".
#
# It is also a local file rather than a rate-limited API, so lookups need no
# key, no quota and no network.

CIQUAL_MACROS = {
    "Energie, Règlement UE N° 1169/2011 (kcal/100 g)": "kcal",
    "Protéines, N x 6.25 (g/100 g)": "protein_g",
    "Glucides (g/100 g)": "carbs_g",
    "Lipides (g/100 g)": "fat_g",
    "AG saturés (g/100 g)": "sat_fat_g",
    "Sucres (g/100 g)": "sugar_g",
    "Fibres alimentaires (g/100 g)": "fibre_g",
    "Sel chlorure de sodium (g/100 g)": "salt_g",
}

# CIQUAL column -> our nutrient slug. Units already match health_core.NUTRIENTS
# canonical units (mg/ug as the column names state), so no conversion is needed.
CIQUAL_NUTRIENTS = {
    "Calcium (mg/100 g)": "calcium",
    "Cuivre (mg/100 g)": "copper",
    "Fer (mg/100 g)": "iron",
    "Iode (µg/100 g)": "iodine",
    "Magnésium (mg/100 g)": "magnesium",
    "Nganèse (mg/100 g)": "manganese",
    "Manganèse (mg/100 g)": "manganese",
    "Phosphore (mg/100 g)": "phosphorus",
    "Potassium (mg/100 g)": "potassium",
    "Sélénium (µg/100 g)": "selenium",
    "Sodium (mg/100 g)": "sodium",
    "Zinc (mg/100 g)": "zinc",
    "Vitamine D (µg/100 g)": "vitamin-d",
    "Vitamine E (mg/100 g)": "vitamin-e",
    "Vitamine K1 (µg/100 g)": "vitamin-k1",
    "Vitamine K2 (µg/100 g)": "vitamin-k2",
    "Vitamine C (mg/100 g)": "vitamin-c",
    "Vitamine B1 ou Thiamine (mg/100 g)": "vitamin-b1",
    "Vitamine B2 ou Riboflavine (mg/100 g)": "vitamin-b2",
    "Vitamine B3 ou PP ou Niacine (mg/100 g)": "vitamin-b3",
    "Vitamine B5 ou Acide pantothénique (mg/100 g)": "vitamin-b5",
    "Vitamine B6 (mg/100 g)": "vitamin-b6",
    "Vitamine B9 ou Folates totaux (µg/100 g)": "folate",
    "Vitamine B12 (µg/100 g)": "vitamin-b12",
    "Rétinol (µg/100 g)": "vitamin-a",
    "Cholestérol (mg/100 g)": "cholesterol",
}


class FoodError(Exception):
    pass


def slugify(name: str) -> str:
    """Same rule as health_core.slugify — one spelling per food, or the history
    forks into two half-populated trends."""
    return re.sub(r"\s+", "-", re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip())


# --- USDA ----------------------------------------------------------------

def usda_search(query: str, api_key: str = USDA_API_KEY, page_size: int = 10) -> list:
    """Candidate foods for a name. The ONLY other place that touches the network
    besides usda_fetch — keep it that way so everything else stays testable."""
    url = (f"{USDA_API}/foods/search?"
           + urllib.parse.urlencode({"query": query, "pageSize": page_size,
                                     "api_key": api_key}))
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read()).get("foods", [])


def usda_fetch(fdc_id: int, api_key: str = USDA_API_KEY) -> dict:
    url = f"{USDA_API}/food/{fdc_id}?" + urllib.parse.urlencode({"api_key": api_key})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def best_match(hits: list) -> dict | None:
    """Pick by dataset quality first, search rank only as a tiebreak.

    Search rank answers "which description looks most like the words typed",
    which is not the same question as "which of these is the better measurement
    of the food". Reference data outranks a closer string match."""
    if not hits:
        return None

    def rank(h):
        dt = h.get("dataType") or ""
        return (DATASET_PRIORITY.index(dt) if dt in DATASET_PRIORITY
                else len(DATASET_PRIORITY))

    return min(hits, key=rank)


def parse_usda(payload: dict) -> dict:
    """USDA payload -> a food row plus nutrients, per 100g.

    Nutrients that health_core cannot name are dropped rather than stored: a row
    in an unrecognised unit cannot be summed against the rest of the day, and a
    total that silently mixes mg and ug is worse than a missing one.
    """
    food = {"name": payload.get("description"),
            "fdc_id": payload.get("fdcId"),
            "source": "usda",
            "nutrients": {}}

    for n in payload.get("foodNutrients", []):
        num, value, unit = _nutrient_fields(n)
        if value is None or not num:
            continue

        if num in USDA_MACROS:
            # USDA lists energy twice, as KCAL and KJ. Take the kcal row.
            if num == "208" and unit.upper() != "KCAL":
                continue
            food[USDA_MACROS[num]] = value
        elif num in USDA_NUTRIENTS:
            slug = USDA_NUTRIENTS[num]
            want = health_core.NUTRIENTS.get(slug)
            if not want:
                continue
            converted = _convert(value, unit, want[0])
            if converted is not None:
                food["nutrients"][slug] = converted

    # Fail loud on a payload nothing was understood from. An empty parse means
    # the shape changed or the mapping is wrong; storing it quietly is how four
    # foods once landed in the table with no kcal and no nutrients, and how a
    # day would later show a mysterious nutritional hole nobody could source.
    if not food["nutrients"] and not any(food.get(c) is not None for c in MACRO_COLS):
        raise FoodError(
            f"understood nothing from USDA payload {payload.get('fdcId')} "
            f"({payload.get('description')!r}) — payload shape or nutrient map is wrong")

    sodium = food["nutrients"].get("sodium")
    if sodium is not None and "salt_g" not in food:
        food["salt_g"] = sodium * SALT_PER_SODIUM / 1000.0
    return food


def _nutrient_fields(n: dict):
    """USDA ships two shapes for the same data and both are in active use.

    /foods/search returns them FLAT:   {"nutrientNumber": "303", "value": 2.71,
                                        "unitName": "MG"}
    /food/{id} NESTS them:             {"nutrient": {"number": "303",
                                        "unitName": "mg"}, "amount": 2.71}

    Handling only the first meant every detail-endpoint import produced an empty
    food."""
    if "nutrient" in n:
        inner = n.get("nutrient") or {}
        return str(inner.get("number") or ""), n.get("amount"), inner.get("unitName") or ""
    return str(n.get("nutrientNumber") or ""), n.get("value"), n.get("unitName") or ""


def _convert(value: float, unit: str, want: str):
    """USDA units to our canonical ones. Returns None for anything we cannot
    convert exactly — notably IU, which needs a per-vitamin factor that differs
    by the form present, so guessing it would fabricate precision."""
    # Detail returns 'µg' (real micro sign) and lowercase; search returns 'UG'.
    unit = unit.replace("\u00b5", "U").replace("\u03bc", "U").upper()
    factors = {("MG", "mg"): 1, ("UG", "ug"): 1, ("G", "g"): 1,
               ("G", "mg"): 1000, ("MG", "ug"): 1000, ("MG", "g"): 0.001,
               ("UG", "mg"): 0.001, ("G", "ug"): 1e6, ("UG", "g"): 1e-6}
    f = factors.get((unit, want))
    return None if f is None else value * f


# --- storing -------------------------------------------------------------

def upsert_food(con, food: dict, slug: str | None = None) -> str:
    """Store or correct one food. Re-importing corrects in place.

    Nutrients are replaced wholesale rather than merged: a re-import that found
    fewer nutrients means the better source lists fewer, and leaving orphans
    from the previous source behind would silently blend two datasets.
    """
    slug = slug or slugify(food.get("name") or "")
    if not slug:
        raise FoodError("a food needs a name or an explicit slug")
    source = food.get("source", "estimate")
    if source not in ("ciqual", "usda", "off", "label", "estimate"):
        raise FoodError(f"unknown source {source!r}")

    cols = ", ".join(MACRO_COLS)
    holes = ", ".join("?" for _ in MACRO_COLS)
    sets = ", ".join(f"{c} = excluded.{c}" for c in MACRO_COLS)
    con.execute(
        f"""INSERT INTO food (slug, name, source, fdc_id, ciqual_code, barcode,
                              notes, {cols})
            VALUES (?, ?, ?, ?, ?, ?, ?, {holes})
            ON CONFLICT(slug) DO UPDATE SET
              name = excluded.name, source = excluded.source,
              fdc_id = excluded.fdc_id, ciqual_code = excluded.ciqual_code,
              barcode = excluded.barcode,
              notes = COALESCE(excluded.notes, food.notes),
              updated_at = datetime('now'), {sets}""",
        (slug, food.get("name"), source, food.get("fdc_id"), food.get("ciqual_code"),
         food.get("barcode"), food.get("notes"), *(food.get(c) for c in MACRO_COLS)))

    con.execute("DELETE FROM food_nutrient WHERE food_slug = ?", (slug,))
    for nutrient, amount in (food.get("nutrients") or {}).items():
        if nutrient not in health_core.NUTRIENTS:
            raise FoodError(f"unknown nutrient {nutrient!r} — see health_core.NUTRIENTS")
        con.execute(
            "INSERT INTO food_nutrient (food_slug, nutrient, amount) VALUES (?, ?, ?)",
            (slug, nutrient, float(amount)))
    con.commit()
    return slug


def get_food(con, slug: str) -> dict | None:
    row = con.execute("SELECT * FROM food WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    food = dict(row)
    food["nutrients"] = {
        r["nutrient"]: r["amount"] for r in con.execute(
            "SELECT nutrient, amount FROM food_nutrient WHERE food_slug = ?", (slug,))}
    return food


def list_foods(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT slug, name, source, fdc_id, kcal, protein_g FROM food ORDER BY name")]


# --- components and rollup -----------------------------------------------

# How much to trust a food, by where its numbers came from. A meal is only as
# good as its worst component — the same "weakest input wins" rule health_core
# already applies when rolling nutrients up into a day.
SOURCE_CONFIDENCE = {"ciqual": "high", "label": "high", "usda": "high",
                     "off": "med", "estimate": "low"}

CONFIDENCE_ORDER = ["low", "med", "high"]


def _confidence(parts) -> str:
    """Weakest component wins. A food whose micros were borrowed from a generic
    (micro_ciqual_code set) is capped at 'med' however good its label is: the
    macros are still label-grade but the micros are somebody else's average."""
    got = []
    for src, borrowed in parts:
        c = SOURCE_CONFIDENCE.get(src, "low")
        if borrowed and c == "high":
            c = "med"
        got.append(c)
    return min(got, key=CONFIDENCE_ORDER.index) if got else "low"


def scale(food: dict, grams: float) -> dict:
    """A food's per-100g figures at an actual portion weight."""
    f = grams / 100.0
    out = {c: (None if food.get(c) is None else food[c] * f) for c in MACRO_COLS}
    out["nutrients"] = {k: v * f for k, v in (food.get("nutrients") or {}).items()}
    out["grams"] = grams
    return out


def add_component(con, meal_id: int, food_slug: str, grams: float) -> dict:
    """Attach a weighed food to a meal. Re-adding the same food corrects its
    weight rather than double-counting it."""
    if grams is None or grams <= 0:
        raise FoodError("a component needs a positive weight in grams")
    if get_food(con, food_slug) is None:
        raise FoodError(f"unknown food {food_slug!r} — import or define it first")
    con.execute(
        """INSERT INTO meal_component (meal_id, food_slug, grams) VALUES (?, ?, ?)
           ON CONFLICT(meal_id, food_slug) DO UPDATE SET grams = excluded.grams""",
        (meal_id, food_slug, float(grams)))
    con.commit()
    return {"meal_id": meal_id, "food_slug": food_slug, "grams": float(grams)}


def meal_totals(con, meal_id: int) -> dict | None:
    """Everything a meal contained, summed from its components.

    None — never a zeroed dict — when the meal has no components. Meals logged
    before this table existed, and meals eaten out where nothing was weighed,
    are unknown rather than empty, and a 0 would read as a real measurement.
    """
    rows = con.execute(
        "SELECT food_slug, grams FROM meal_component WHERE meal_id = ? ORDER BY id",
        (meal_id,)).fetchall()
    if not rows:
        return None

    totals = {c: 0.0 for c in MACRO_COLS}
    nutrients: dict[str, float] = {}
    parts = []
    for r in rows:
        food = get_food(con, r["food_slug"])
        part = scale(food, r["grams"])
        for c in MACRO_COLS:
            if part[c] is not None:
                totals[c] += part[c]
        for k, v in part["nutrients"].items():
            nutrients[k] = nutrients.get(k, 0.0) + v
        parts.append({"food_slug": r["food_slug"], "grams": r["grams"],
                      "source": food["source"],
                      "micro_ciqual_code": food.get("micro_ciqual_code"),
                      **{c: part[c] for c in MACRO_COLS}})

    return {**totals, "nutrients": nutrients, "components": parts,
            "confidence": _confidence((p["source"], p["micro_ciqual_code"])
                                      for p in parts)}


def _deaccent(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(s).lower())
                   if unicodedata.category(c) != "Mn")


def parse_ciqual_value(raw):
    """One CIQUAL cell -> a number, or None for "not a measurement".

    The distinctions here decide whether the whole table is trustworthy:

      '16,8'     -> 16.8    European decimal comma.
      '-'        -> None    Not measured. THE most common cell in the table
                            (63,688 of them). Zero here would invent a measured
                            absence out of a gap in the data, and those zeros
                            would then be summed into daily totals as fact.
      'traces'   -> 0.0     Unlike '-', this IS a measurement: present, and
                            below anything that matters to a daily total.
      '< 20'     -> None    Below the limit of quantification. Using 20
                            overstates, using 0 understates; both claim a
                            precision the lab explicitly declined to give.
    """
    if raw is None:
        return None
    t = str(raw).strip()
    if not t or t == "-":
        return None
    if _deaccent(t) == "traces":
        return 0.0
    if t.startswith("<"):
        return None
    try:
        return float(t.replace("\u202f", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def ciqual_search(con, query: str, limit: int = 15) -> list[dict]:
    """Find staged CIQUAL foods by name, accent- and case-insensitively.

    Raw foods sort first: a search for 'tomate' should surface 'Tomate, crue'
    ahead of 'Tomate farcie, prete a cuire', because a component of a weighed
    salad is almost always the raw ingredient.
    """
    rows = [dict(r) for r in con.execute(
        "SELECT alim_code, name_fr, group_fr, subgroup_fr FROM ciqual_food")]
    q = _deaccent(query)
    hits = [r for r in rows if q in _deaccent(r["name_fr"])]
    hits.sort(key=lambda r: (0 if _deaccent(r["name_fr"]).endswith(", crue")
                             or _deaccent(r["name_fr"]).endswith(", cru") else 1,
                             len(r["name_fr"])))
    return hits[:limit]


def parse_ciqual(con, alim_code: str) -> dict:
    """A staged CIQUAL row -> a food dict, same shape parse_usda returns."""
    head = con.execute("SELECT * FROM ciqual_food WHERE alim_code = ?",
                       (alim_code,)).fetchone()
    if head is None:
        raise FoodError(f"CIQUAL code {alim_code!r} is not staged")

    food = {"name": head["name_fr"], "ciqual_code": alim_code,
            "source": "ciqual", "nutrients": {}}
    for r in con.execute("SELECT column_name, raw FROM ciqual_value WHERE alim_code = ?",
                         (alim_code,)):
        value = parse_ciqual_value(r["raw"])
        if value is None:
            continue
        col = r["column_name"]
        if col in CIQUAL_MACROS:
            food[CIQUAL_MACROS[col]] = value
        elif col in CIQUAL_NUTRIENTS:
            slug = CIQUAL_NUTRIENTS[col]
            if slug in health_core.NUTRIENTS:
                food["nutrients"][slug] = value

    # CIQUAL leaves BOTH energy columns '-' for a number of foods — lettuce,
    # onion and chilli among them. A real gap in the source, not a parse bug, so
    # derive energy from the EU Reg. 1169/2011 conversion factors rather than
    # letting a salad's components contribute no calories at all.
    if food.get("kcal") is None:
        derived = _atwater(food)
        if derived is not None:
            food["kcal"] = derived
            food["kcal_derived"] = True
            food["notes"] = ("Energy derived from EU Reg. 1169/2011 factors "
                             "(protein 4, carbs 4, fat 9, fibre 2 kcal/g) — "
                             "CIQUAL states no energy value for this food.")

    if not food["nutrients"] and not any(food.get(c) is not None for c in MACRO_COLS):
        raise FoodError(f"understood nothing from CIQUAL row {alim_code!r}")
    return food


# EU Regulation 1169/2011 Annex XIV energy conversion factors, kcal per gram.
ATWATER = {"protein_g": 4.0, "carbs_g": 4.0, "fat_g": 9.0, "fibre_g": 2.0}


def _atwater(food: dict):
    """Energy from macros. None unless protein, carbs and fat are all present —
    guessing from a partial macro set would be worse than leaving it unknown."""
    if any(food.get(k) is None for k in ("protein_g", "carbs_g", "fat_g")):
        return None
    return sum((food.get(k) or 0.0) * f for k, f in ATWATER.items())


def import_ciqual(con, alim_code: str, slug: str | None = None) -> str:
    """Import one CIQUAL food, at most once.

    `upsert_food` conflicts on `slug` alone, so nothing there stops the same
    alim_code from landing twice under two names — which is how `olive-oil`
    and `olive-oil-evoo` both became "Huile d'olive vierge extra", identical
    in every column. A forked food is the crowdsourced-database failure this
    table exists to avoid, so the code is treated as the identity:

      - already stored, no slug given -> reuse the stored slug (CIQUAL's
        French names slugify differently across accents and commas, and a
        spelling difference must not fork the food)
      - already stored under another slug -> refuse, naming the one to correct
      - stored under this slug, or not stored -> import as before
    """
    row = con.execute("SELECT slug FROM food WHERE ciqual_code = ?",
                      (str(alim_code),)).fetchone()
    if row is not None:
        stored = row["slug"] if isinstance(row, sqlite3.Row) else row[0]
        if slug is None:
            slug = stored
        elif slug != stored:
            raise FoodError(
                f"CIQUAL {alim_code} is already stored as {stored!r} — pass "
                f"slug={stored!r} to correct it, rather than forking it into "
                f"{slug!r}")
    return upsert_food(con, parse_ciqual(con, alim_code), slug=slug)


def stage_ciqual(con, xls_path: str) -> int:
    """Load the whole CIQUAL table into the staging tables, verbatim.

    Values are stored in their source spelling ('-', 'traces', '< 0,01', comma
    decimals) rather than parsed at load time, so the parsing decisions above
    stay visible and revisable without re-downloading anything.

    Needs xlrd (CIQUAL ships legacy .xls). Imported here rather than at module
    level so the rest of food_core works without it.
    """
    import xlrd

    sheet = xlrd.open_workbook(xls_path).sheet_by_index(0)
    headers = [str(sheet.cell_value(0, c)) for c in range(sheet.ncols)]
    wanted = {c: h for c, h in enumerate(headers)
              if h in CIQUAL_MACROS or h in CIQUAL_NUTRIENTS}

    n = 0
    for r in range(1, sheet.nrows):
        code = str(sheet.cell_value(r, 6)).strip()
        if code.endswith(".0"):
            code = code[:-2]
        if not code:
            continue
        con.execute(
            """INSERT INTO ciqual_food (alim_code, name_fr, group_fr, subgroup_fr)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(alim_code) DO UPDATE SET name_fr = excluded.name_fr""",
            (code, str(sheet.cell_value(r, 7)).strip(),
             str(sheet.cell_value(r, 3)).strip(), str(sheet.cell_value(r, 4)).strip()))
        for c, h in wanted.items():
            con.execute(
                """INSERT INTO ciqual_value (alim_code, column_name, raw)
                   VALUES (?, ?, ?)
                   ON CONFLICT(alim_code, column_name) DO UPDATE SET raw = excluded.raw""",
                (code, h, str(sheet.cell_value(r, c)).strip()))
        n += 1
    con.commit()
    return n


def sync_meal(con, meal_id: int) -> dict | None:
    """Refresh a meal's stored macros and micros from its components.

    meal_log's flat columns stay the one place day totals are read from — every
    existing caller depends on that — so rather than teach them all about
    components, the roll-up is written back. Components remain the derivation
    and the columns become a cache of it, which is what makes a later fix to a
    food propagate: re-import the food, re-sync, and the day corrects itself.

    Returns None and changes nothing for a meal with no components: a meal eaten
    out keeps whatever was estimated for it.
    """
    totals = meal_totals(con, meal_id)
    if totals is None:
        return None

    con.execute(
        f"""UPDATE meal_log SET {', '.join(f'{c} = ?' for c in MACRO_COLS)}
            WHERE id = ?""",
        (*(round(totals[c], 2) if totals[c] is not None else None for c in MACRO_COLS),
         meal_id))

    # Replace, never merge: a component change that removes a nutrient must
    # remove it here too, or an orphan from the previous composition survives.
    con.execute("DELETE FROM meal_nutrient WHERE meal_id = ?", (meal_id,))
    for nutrient, amount in totals["nutrients"].items():
        con.execute(
            """INSERT INTO meal_nutrient (meal_id, nutrient, amount, unit, confidence)
               VALUES (?, ?, ?, ?, ?)""",
            (meal_id, nutrient, round(amount, 4),
             health_core.NUTRIENTS[nutrient][0], totals["confidence"]))
    con.commit()
    return totals


def attach_ciqual_micros(con, slug: str, alim_code: str) -> dict:
    """Fill a label food's missing micronutrients from a CIQUAL generic.

    EU labels declare macros and, at most, a handful of fortified micros. Left
    alone, a day containing mozzarella, yogurt, whey and cas pane reports a
    calcium figure that is the absence of a measurement rather than a
    measurement — which then reads as a deficiency that is not real.

    Two rules keep this honest:

    1. **A micro the label states always wins.** The label is the actual
       formulation; CIQUAL's generic is an average of a different product.
       CIQUAL fills only what the label left unsaid.
    2. **The blend is recorded, never silent.** `micro_ciqual_code` names the
       source, and any meal built on this food is capped at 'med' confidence —
       the macros are still label-grade, the micros are borrowed.

    Macros are untouched, as is `source`: this stays a label food.
    """
    food = get_food(con, slug)
    if food is None:
        raise FoodError(f"unknown food {slug!r}")

    generic = parse_ciqual(con, alim_code)
    filled = {k: v for k, v in generic["nutrients"].items()
              if k not in food["nutrients"]}

    for nutrient, amount in filled.items():
        con.execute(
            "INSERT INTO food_nutrient (food_slug, nutrient, amount) VALUES (?, ?, ?)",
            (slug, nutrient, float(amount)))

    note = (f"Micros gap-filled from CIQUAL {alim_code} "
            f"({generic['name']}); label macros and label-declared micros kept.")
    con.execute(
        """UPDATE food SET micro_ciqual_code = ?,
                           notes = COALESCE(notes || ' ', '') || ?,
                           updated_at = datetime('now')
           WHERE slug = ?""", (alim_code, note, slug))
    con.commit()
    return {"slug": slug, "filled": sorted(filled), "from": generic["name"]}
