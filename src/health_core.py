"""Health tracking write path: weight, meals, workouts.

The single validating entry point for health data. Chat is the interface, so
ad-hoc INSERTs from whatever session happens to be running would drift on
units, slot names and exercise naming — everything goes through here instead.

Schema is owned by the files in db/migrations/, applied once, in order. Nothing here issues CREATE TABLE.
"""
import datetime
import re
import sqlite3

import profile as _profile

DB_PATH = _profile.DB_PATH

# Read from profile.json — see profile.example.json and AI_SETUP.md.
PROTEIN_G_PER_KG = _profile.get("protein_g_per_kg", 1.8)
GOAL_WEIGHT_KG = _profile.get("goal_weight_kg")
HEIGHT_CM = _profile.get("height_cm")
BIRTH_DATE = _profile.get("birth_date")
SEX = _profile.get("sex", "male")

SLOTS = ("breakfast", "lunch", "dinner", "snack")

# Every nutrient meal_nutrient will accept: canonical unit + adult reference
# intake (EU NRV where one exists, else the US RDA/AI). Kept as code rather
# than a table for the same reason PROTEIN_G_PER_KG is — it is a constant of
# nutrition, not user data, and it wants to diff in git.
#
# The unit is the point. An EAV table whose rows mix mg and ug for one
# nutrient cannot be summed, so writes are rejected unless they match.
# `ref` is None for things with no meaningful daily target (caffeine has a
# ceiling, not a target; creatine is a dose).
NUTRIENTS = {
    # fat-soluble vitamins
    "vitamin-a":   ("ug", 800),      # ug RAE
    "vitamin-d":   ("ug", 15),       # 15 ug = 600 IU; UL 100 ug/4000 IU
    "vitamin-e":   ("mg", 12),
    "vitamin-k1":  ("ug", 120),
    "vitamin-k2":  ("ug", None),     # no established NRV
    # water-soluble vitamins
    "vitamin-c":   ("mg", 90),
    "vitamin-b1":  ("mg", 1.1),      # thiamine
    "vitamin-b2":  ("mg", 1.4),      # riboflavin
    "vitamin-b3":  ("mg", 16),       # niacin
    "vitamin-b5":  ("mg", 6),        # pantothenic acid
    "vitamin-b6":  ("mg", 1.4),      # UL 12 mg/day — the one that stacks
    "vitamin-b12": ("ug", 2.5),
    "biotin":      ("ug", 50),
    "folate":      ("ug", 400),
    "choline":     ("mg", 550),
    # minerals
    "calcium":     ("mg", 1000),
    "iron":        ("mg", 14),
    "magnesium":   ("mg", 375),
    "phosphorus":  ("mg", 700),
    "potassium":   ("mg", 3500),
    "sodium":      ("mg", 2000),
    "zinc":        ("mg", 10),
    "selenium":    ("ug", 55),
    "iodine":      ("ug", 150),
    "copper":      ("mg", 1),
    "manganese":   ("mg", 2),
    # not nutrients, but they arrive on the same labels and want the same sum
    "caffeine":    ("mg", None),
    "taurine":     ("mg", None),
    "creatine":    ("g",  None),
    "cholesterol": ("mg", None),
}


class HealthError(Exception):
    pass


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def slugify(name: str) -> str:
    """Stable join key for a lift. Mirrors slugify() in nutrition.js."""
    return re.sub(r"\s+", "-", re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip())


def protein_target(weight_kg):
    if weight_kg is None:
        return None
    return round(weight_kg * PROTEIN_G_PER_KG)


# --- weight --------------------------------------------------------------

# Composition fields, in the order they are stored and returned. Nullable: a
# weigh-in without a smart scale is still a valid weigh-in.
COMP_FIELDS = ("body_fat_pct", "water_pct", "muscle_pct", "bone_mass_kg",
               "visceral_fat", "comp_source")


def log_weight(con, date: str, weight_kg: float, notes: str | None = None,
               body_fat_pct=None, water_pct=None, muscle_pct=None,
               bone_mass_kg=None, visceral_fat=None, comp_source=None) -> dict:
    """Record a weigh-in. Re-logging a date corrects it rather than duplicating.

    Every optional field is COALESCEd on conflict, so a later weight-only
    re-log corrects the weight without silently wiping that day's composition.
    To clear a field, delete the row and re-log it."""
    comp = {"body_fat_pct": _pct(body_fat_pct, "body_fat_pct"),
            "water_pct": _pct(water_pct, "water_pct"),
            "muscle_pct": _pct(muscle_pct, "muscle_pct"),
            "bone_mass_kg": None if bone_mass_kg is None else float(bone_mass_kg),
            "visceral_fat": None if visceral_fat is None else float(visceral_fat),
            "comp_source": comp_source}
    cols = ", ".join(COMP_FIELDS)
    holes = ", ".join("?" for _ in COMP_FIELDS)
    sets = ", ".join(f"{c} = COALESCE(excluded.{c}, body_log.{c})" for c in COMP_FIELDS)
    con.execute(
        f"""INSERT INTO body_log (date, weight_kg, notes, {cols})
            VALUES (?, ?, ?, {holes})
            ON CONFLICT(date) DO UPDATE SET weight_kg = excluded.weight_kg,
                                            notes = COALESCE(excluded.notes, body_log.notes),
                                            {sets}""",
        (date, float(weight_kg), notes, *(comp[c] for c in COMP_FIELDS)))
    con.commit()
    return {"date": date, "weight_kg": float(weight_kg),
            **{k: v for k, v in comp.items() if v is not None}}


def _pct(value, field: str):
    """A percentage that is not a percentage is a typo, not data — 0.263 for
    26.3% would poison the trend silently rather than fail. The floor is 1, not
    0: no body-composition percentage worth logging sits below it, so anything
    that does is a fraction that was meant to be a percent."""
    if value is None:
        return None
    value = float(value)
    if not 1 <= value <= 100:
        raise HealthError(f"{field}={value} is not a percentage between 0 and 100")
    return value


def latest_weight(con, on_or_before: str):
    """Most recent weigh-in at or before a date — weight carries forward between
    weigh-ins, but never backwards from a future one."""
    row = con.execute(
        "SELECT weight_kg FROM body_log WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (on_or_before,)).fetchone()
    return row["weight_kg"] if row else None


# --- meals ---------------------------------------------------------------

def log_meal(con, date: str, slot: str, raw: str, kcal=None, protein_g=None,
             carbs_g=None, fat_g=None, estimated: int = 1, confidence=None,
             fibre_g=None, sat_fat_g=None, sugar_g=None, salt_g=None) -> dict:
    """Record a meal: `raw` is verbatim what was said, macros are the estimate.

    fibre/sat fat/sugar/salt are columns rather than nutrients because they are
    mandatory on every EU declaration and they roll into the day's totals; the
    sparse rest goes to log_nutrients() against the id returned here.

    Returns the day's running protein total and remaining gap so the caller is
    handed the shortfall without having to think to ask for it.
    """
    if slot not in SLOTS:
        raise HealthError(f"slot must be one of {', '.join(SLOTS)}; got {slot!r}")
    cur = con.execute(
        """INSERT INTO meal_log (date, slot, raw, kcal, protein_g, carbs_g, fat_g,
                                 estimated, confidence, fibre_g, sat_fat_g,
                                 sugar_g, salt_g)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, slot, raw) DO UPDATE SET
             kcal=excluded.kcal, protein_g=excluded.protein_g,
             carbs_g=excluded.carbs_g, fat_g=excluded.fat_g,
             confidence=excluded.confidence,
             fibre_g=COALESCE(excluded.fibre_g, fibre_g),
             sat_fat_g=COALESCE(excluded.sat_fat_g, sat_fat_g),
             sugar_g=COALESCE(excluded.sugar_g, sugar_g),
             salt_g=COALESCE(excluded.salt_g, salt_g)""",
        (date, slot, raw, kcal, protein_g, carbs_g, fat_g, estimated, confidence,
         fibre_g, sat_fat_g, sugar_g, salt_g))
    con.commit()

    # lastrowid is 0 when ON CONFLICT took the UPDATE branch — resolve the id
    # so the caller can always hang nutrients off it.
    meal_id = cur.lastrowid or con.execute(
        "SELECT id FROM meal_log WHERE date=? AND slot=? AND raw=?",
        (date, slot, raw)).fetchone()["id"]
    p = _protein_state(con, date)
    return {"id": meal_id, "date": date, "slot": slot, **p}


def log_nutrients(con, meal_id: int, nutrients: dict, confidence=None) -> dict:
    """Attach micronutrients to an already-logged meal.

    `nutrients` maps name -> amount in the nutrient's canonical unit, or
    name -> [amount, unit] to state the unit explicitly and have it checked.
    Names are slugified, so "Vitamin D3" and "vitamin d" land on one row.

    confidence is this batch's, not the meal's: figures read off a label and
    figures recalled from memory are different facts and must not be flattened
    into one number that reads as measured.
    """
    if not con.execute("SELECT 1 FROM meal_log WHERE id = ?", (meal_id,)).fetchone():
        raise HealthError(f"no meal with id {meal_id}")
    if confidence is not None and confidence not in ("low", "med", "high"):
        raise HealthError(f"confidence must be low/med/high; got {confidence!r}")

    written = {}
    for name, value in nutrients.items():
        slug = slugify(name)
        if slug not in NUTRIENTS:
            raise HealthError(
                f"unknown nutrient {name!r} (slug {slug!r}); "
                f"add it to NUTRIENTS in health_core first")
        canonical, _ = NUTRIENTS[slug]
        amount, unit = (value if isinstance(value, (list, tuple))
                        else (value, canonical))
        if unit != canonical:
            raise HealthError(
                f"{slug} is stored in {canonical}, got {unit!r} — convert first, "
                f"a column that mixes units cannot be summed")
        con.execute(
            """INSERT INTO meal_nutrient (meal_id, nutrient, amount, unit, confidence)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(meal_id, nutrient) DO UPDATE SET
                 amount=excluded.amount, unit=excluded.unit,
                 confidence=excluded.confidence""",
            (meal_id, slug, float(amount), canonical, confidence))
        written[slug] = [float(amount), canonical]
    con.commit()
    return {"meal_id": meal_id, "written": written}


def known_nutrients() -> list[dict]:
    """The accepted nutrient slugs with their canonical unit and reference
    intake. Call before logging: it is what stops "vit D" in ug and "Vitamin D"
    in IU becoming two untotalable rows."""
    return [{"slug": s, "unit": u, "reference": r}
            for s, (u, r) in sorted(NUTRIENTS.items())]


def _protein_state(con, date: str) -> dict:
    consumed = con.execute(
        "SELECT COALESCE(SUM(protein_g), 0) t FROM meal_log WHERE date = ?",
        (date,)).fetchone()["t"]
    target = protein_target(latest_weight(con, date))
    return {
        "protein_today": round(consumed, 1),
        "protein_target": target,
        "protein_remaining": None if target is None else max(0, round(target - consumed)),
    }


# --- workouts ------------------------------------------------------------

def log_workout(con, date: str, type: str, duration_min=None, rpe=None,
                notes=None, sets: list[dict] | None = None) -> dict:
    cur = con.execute(
        "INSERT INTO workout_log (date, type, duration_min, rpe, notes) VALUES (?, ?, ?, ?, ?)",
        (date, type, duration_min, rpe, notes))
    workout_id = cur.lastrowid

    n = 0
    for s in (sets or []):
        name = s.get("exercise")
        if not name:
            raise HealthError("each set needs an exercise name")
        slug = slugify(name)
        # Set numbers run per exercise, so "set 3 of bench" means that even when
        # the session interleaved other lifts between them.
        set_no = con.execute(
            "SELECT COUNT(*) c FROM workout_set WHERE workout_id = ? AND exercise_slug = ?",
            (workout_id, slug)).fetchone()["c"] + 1
        con.execute(
            """INSERT INTO workout_set (workout_id, exercise, exercise_slug, set_no,
                                        reps, weight_kg, rpe)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (workout_id, name, slug, set_no, s.get("reps"), s.get("weight_kg"), s.get("rpe")))
        n += 1

    con.commit()
    return {"id": workout_id, "date": date, "type": type, "sets": n}


def known_exercises(con) -> list[dict]:
    """Existing lift slugs with their most recent display name, so a new log
    matches an established slug instead of forking the trend line."""
    rows = con.execute(
        """SELECT exercise_slug slug, exercise name FROM workout_set
           WHERE id IN (SELECT MAX(id) FROM workout_set GROUP BY exercise_slug)
           ORDER BY exercise_slug""").fetchall()
    return [{"slug": r["slug"], "name": r["name"]} for r in rows]


# --- read / correct ------------------------------------------------------

_TABLES = {"weight": "body_log", "meal": "meal_log", "workout": "workout_log",
           "nutrient": "meal_nutrient"}


def delete_entry(con, kind: str, entry_id: int) -> dict:
    if kind not in _TABLES:
        raise HealthError(f"kind must be one of {', '.join(_TABLES)}; got {kind!r}")
    con.execute(f"DELETE FROM {_TABLES[kind]} WHERE id = ?", (entry_id,))
    con.commit()
    return {"deleted": kind, "id": entry_id}


_MEAL_FIELDS = ("date", "slot", "raw", "kcal", "protein_g", "carbs_g", "fat_g",
                "fibre_g", "sat_fat_g", "sugar_g", "salt_g", "confidence",
                "estimated")


def amend_meal(con, meal_id: int, **fields) -> dict:
    """Correct a meal in place.

    log_meal upserts on (date, slot, raw), so a corrected SENTENCE re-logged
    through it does not fix anything — it lands as a second meal and the day
    counts the food twice. Deleting and re-logging loses the id, and with it
    any micronutrients already hung off it. Amending by id is the only
    correction that is actually a correction.

    Only named fields are touched; anything omitted keeps its value.
    """
    unknown = set(fields) - set(_MEAL_FIELDS)
    if unknown:
        raise HealthError(f"unknown meal field(s): {', '.join(sorted(unknown))}")
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        raise HealthError("amend_meal needs at least one field to change")
    if "slot" in fields and fields["slot"] not in SLOTS:
        raise HealthError(f"slot must be one of {', '.join(SLOTS)}; "
                          f"got {fields['slot']!r}")

    row = con.execute("SELECT date FROM meal_log WHERE id = ?",
                      (meal_id,)).fetchone()
    if row is None:
        raise HealthError(f"no meal with id {meal_id}")

    con.execute(f"UPDATE meal_log SET {', '.join(f'{k} = ?' for k in fields)} "
                f"WHERE id = ?", (*fields.values(), meal_id))
    con.commit()
    date = fields.get("date", row["date"])
    return {"id": meal_id, "amended": sorted(fields), "date": date,
            **_protein_state(con, date)}


def merge_exercise(con, from_slug: str, into_slug: str) -> dict:
    """Move every set logged under `from_slug` onto `into_slug`.

    known_exercises() stops a new spelling forking the trend at write time, but
    nothing repaired a fork that already happened — and two spellings of one
    lift are two half-length trend lines, which is the same defect as a
    duplicated food row.

    The target must already have sets: merging into a slug that was never
    logged would silently invent an exercise rather than repair one.
    """
    if from_slug == into_slug:
        return {"moved": 0, "slug": into_slug}

    target = con.execute(
        """SELECT exercise FROM workout_set WHERE exercise_slug = ?
           ORDER BY id DESC LIMIT 1""", (into_slug,)).fetchone()
    if target is None:
        raise HealthError(f"no sets logged under {into_slug!r} — merge into an "
                          f"exercise that exists, or rename by logging afresh")

    moving = con.execute(
        "SELECT id, workout_id FROM workout_set WHERE exercise_slug = ? ORDER BY id",
        (from_slug,)).fetchall()
    if not moving:
        raise HealthError(f"no sets logged under {from_slug!r}")

    # (workout_id, exercise_slug, set_no) is UNIQUE, so a workout holding BOTH
    # spellings would collide on merge. Continue the numbering after whatever
    # the target already has in that workout rather than dropping the set.
    for r in moving:
        nxt = con.execute(
            """SELECT COALESCE(MAX(set_no), 0) + 1 n FROM workout_set
               WHERE workout_id = ? AND exercise_slug = ?""",
            (r["workout_id"], into_slug)).fetchone()["n"]
        con.execute(
            "UPDATE workout_set SET exercise_slug = ?, exercise = ?, set_no = ? "
            "WHERE id = ?", (into_slug, target["exercise"], nxt, r["id"]))
    con.commit()
    return {"moved": len(moving), "slug": into_slug, "name": target["exercise"]}


def link_activity(con, workout_id: int, garmin_id: int) -> dict:
    """Bind a logged session to the activity the watch recorded for it.

    The two tables stay parallel on purpose — one is what I reported, the other
    what the watch measured — but the join between them was the date alone, and
    a date stops identifying anything the moment a day holds more than one
    session. The convention that filled the gap was a Garmin activity id typed
    into the notes field: a foreign key living in prose.

    Heart rate and burn are COPIED onto the session rather than joined at read
    time. They measure that session, they do not change afterwards, and loads
    sitting next to the heart rate that produced them is the point of
    correlating the two at all.
    """
    act = con.execute(
        """SELECT garmin_id, date, avg_hr, max_hr, calories FROM garmin_activity
           WHERE garmin_id = ?""", (garmin_id,)).fetchone()
    if act is None:
        raise HealthError(f"no Garmin activity {garmin_id} — sync first")

    w = con.execute("SELECT date FROM workout_log WHERE id = ?",
                    (workout_id,)).fetchone()
    if w is None:
        raise HealthError(f"no workout with id {workout_id}")
    if w["date"] != act["date"]:
        raise HealthError(
            f"activity {garmin_id} is on {act['date']} but workout {workout_id} "
            f"is on {w['date']} — the ambiguity this link removes is within a "
            f"day, so crossing days is never the right answer")

    taken = con.execute(
        "SELECT id FROM workout_log WHERE garmin_id = ? AND id != ?",
        (garmin_id, workout_id)).fetchone()
    if taken is not None:
        raise HealthError(f"activity {garmin_id} is already linked to workout "
                          f"{taken['id']} — one session, one activity")

    con.execute(
        """UPDATE workout_log SET garmin_id = ?, avg_hr = ?, max_hr = ?,
                                  calories = ? WHERE id = ?""",
        (garmin_id, act["avg_hr"], act["max_hr"], act["calories"], workout_id))
    con.commit()
    return {"workout_id": workout_id, "garmin_id": garmin_id, "date": act["date"],
            "avg_hr": act["avg_hr"], "max_hr": act["max_hr"],
            "calories": act["calories"]}


def watch_sets(con, workout_id: int) -> list[dict]:
    """Per-set TIMING from the watch for a linked session, in performed order.

    Set duration and the rest that followed it, which nothing else records —
    33 minutes of rest in a single session, invisible until now.

    Reps and load come back too, but they are NOT the record: the sets are
    logged in chat, where they can be reasoned about as they are entered, and
    chat is authoritative. The watch's figures are a second typing of the same
    thing and they drift — 15kg and 40kg against chat's 20kg and 50kg on
    2026-09-17. Treat them as provenance, never as a correction.
    """
    w = con.execute("SELECT garmin_id FROM workout_log WHERE id = ?",
                    (workout_id,)).fetchone()
    if w is None:
        raise HealthError(f"no workout with id {workout_id}")
    if w["garmin_id"] is None:
        raise HealthError(f"workout {workout_id} is not linked to an activity — "
                          f"link_activity first")
    return [dict(r) for r in con.execute(
        """SELECT set_no, reps, weight_kg, duration_s, rest_after_s, start_local,
                  category, probability
           FROM garmin_exercise_set WHERE garmin_id = ? ORDER BY set_no""",
        (w["garmin_id"],))]


def unlinked_activities(con, date: str) -> list[dict]:
    """Activities the watch recorded on `date` that no session has claimed.

    The shortlist for linking: on a day with six walks, this is what is left to
    match rather than all six every time."""
    rows = con.execute(
        """SELECT garmin_id, start_local, type, duration_min, distance_m,
                  avg_hr, max_hr, calories
           FROM garmin_activity
           WHERE date = ? AND garmin_id NOT IN
                 (SELECT garmin_id FROM workout_log WHERE garmin_id IS NOT NULL)
           ORDER BY start_local""", (date,))
    return [dict(r) for r in rows]


def range_days(con, start: str, end: str) -> list[dict]:
    """One row per calendar day between `start` and `end`, both inclusive.

    day() answers a single date, so every trend question — did intake track the
    target, is the weight actually falling — meant raw SQL, which defeats the
    point of having a tool surface. Days with nothing logged are still returned:
    a gap is a fact about the record, and collapsing it would hide exactly the
    days that explain a stalled fit.
    """
    if start > end:
        raise HealthError(f"start {start!r} is after end {end!r}")

    meals = {r["date"]: r for r in con.execute(
        """SELECT date, SUM(kcal) kcal, SUM(protein_g) protein_g,
                  COUNT(*) meals
           FROM meal_log WHERE date BETWEEN ? AND ? GROUP BY date""",
        (start, end))}
    weights = {r["date"]: r["weight_kg"] for r in con.execute(
        "SELECT date, weight_kg FROM body_log WHERE date BETWEEN ? AND ?",
        (start, end))}
    workouts = {r["date"]: r["n"] for r in con.execute(
        """SELECT date, COUNT(*) n FROM workout_log
           WHERE date BETWEEN ? AND ? GROUP BY date""", (start, end))}

    out, d = [], datetime.date.fromisoformat(start)
    last = datetime.date.fromisoformat(end)
    while d <= last:
        iso = d.isoformat()
        m = meals.get(iso)
        out.append({"date": iso,
                    "kcal": m["kcal"] if m else None,
                    "protein_g": m["protein_g"] if m else None,
                    "meals": m["meals"] if m else 0,
                    "weight_kg": weights.get(iso),
                    "workouts": workouts.get(iso, 0)})
        d += datetime.timedelta(days=1)
    return out


def day(con, date: str) -> dict:
    """Everything logged for one day. Call before writing: it is what makes a
    correction natural instead of a second duplicate row."""
    weight = con.execute(
        "SELECT * FROM body_log WHERE date = ?", (date,)).fetchone()
    meals = [dict(r) for r in con.execute(
        "SELECT * FROM meal_log WHERE date = ? ORDER BY id", (date,)).fetchall()]
    for m in meals:
        m["nutrients"] = {r["nutrient"]: {"amount": r["amount"], "unit": r["unit"],
                                          "confidence": r["confidence"]}
                          for r in con.execute(
            "SELECT * FROM meal_nutrient WHERE meal_id = ? ORDER BY nutrient",
            (m["id"],)).fetchall()}
    workouts = []
    for w in con.execute("SELECT * FROM workout_log WHERE date = ? ORDER BY id",
                         (date,)).fetchall():
        w = dict(w)
        w["sets"] = [dict(r) for r in con.execute(
            "SELECT * FROM workout_set WHERE workout_id = ? ORDER BY exercise_slug, set_no",
            (w["id"],)).fetchall()]
        workouts.append(w)

    p = _protein_state(con, date)
    totals = con.execute(
        """SELECT COALESCE(SUM(kcal),0) kcal, COALESCE(SUM(protein_g),0) protein_g,
                  COALESCE(SUM(carbs_g),0) carbs_g, COALESCE(SUM(fat_g),0) fat_g,
                  COALESCE(SUM(fibre_g),0) fibre_g, COALESCE(SUM(sat_fat_g),0) sat_fat_g,
                  COALESCE(SUM(sugar_g),0) sugar_g, COALESCE(SUM(salt_g),0) salt_g
           FROM meal_log WHERE date = ?""", (date,)).fetchone()

    # Micros summed across the day, each against its reference intake. Only
    # nutrients actually logged appear — a missing row means "not recorded",
    # never "zero", and a 0% that is really an unknown would read as a deficit.
    micros = {}
    for r in con.execute(
            """SELECT n.nutrient, SUM(n.amount) amount, n.unit,
                      MIN(CASE n.confidence WHEN 'low' THEN 0 WHEN 'med' THEN 1
                                            ELSE 2 END) conf
               FROM meal_nutrient n JOIN meal_log m ON m.id = n.meal_id
               WHERE m.date = ? GROUP BY n.nutrient, n.unit
               ORDER BY n.nutrient""", (date,)).fetchall():
        ref = NUTRIENTS.get(r["nutrient"], (None, None))[1]
        micros[r["nutrient"]] = {
            "amount": round(r["amount"], 2), "unit": r["unit"],
            "reference": ref,
            "pct_reference": None if not ref else round(100 * r["amount"] / ref),
            "confidence": ("low", "med", "high")[r["conf"]],
        }

    return {
        "date": date,
        "weight_kg": weight["weight_kg"] if weight else None,
        # What the watch measured, next to what was self-reported. None means
        # the watch has nothing for this date — it is not a zero.
        "garmin": _garmin(con, date),
        "composition": ({c: weight[c] for c in COMP_FIELDS}
                        if weight and weight["body_fat_pct"] is not None else None),
        "meals": meals,
        "workouts": workouts,
        "totals": dict(totals),
        "micros": micros,
        "protein": {"target": p["protein_target"], "consumed": p["protein_today"],
                    "remaining": p["protein_remaining"]},
        # Adaptive calorie target. None until there are two weigh-ins to fit.
        # Contains no exercise bonus and must never grow one — training is
        # already inside the inferred maintenance. See energy_core.
        "energy": _energy_safe(con, date),
        "estimated_macros": True,
    }


def energy(con, date: str, goal_kg: float | None = None,
           rate_pct_per_week: float | None = None) -> dict | None:
    """Adaptive calorie target for `date`, or None if the history is too thin.

    Two selection rules are the whole job here — energy_core owns the maths:

    1. Nothing after `date` is visible, so asking about a past day gives the
       answer that was available then rather than one informed by the future.
    2. Intake counts COMPLETE days only, so `date` itself is excluded. A day
       still being logged reads as a huge deficit and would drag maintenance
       down by hundreds of calories.

    Returns None rather than a guess when there are fewer than two weigh-ins:
    a first weigh-in is a data point, not a trend.
    """
    import energy_core

    series = [(r["date"], r["weight_kg"]) for r in con.execute(
        "SELECT date, weight_kg FROM body_log WHERE date <= ? ORDER BY date",
        (date,))]
    if len(series) < 2:
        return None

    intake = [(r["date"], r["kcal"]) for r in con.execute(
        """SELECT date, SUM(kcal) kcal FROM meal_log
           WHERE date < ? AND kcal IS NOT NULL
           GROUP BY date ORDER BY date""", (date,))]
    if not intake:
        return None

    # The formula prior is built on the CURRENT trend weight, so it tracks him
    # down the cut rather than anchoring to the weight he started at.
    prior = energy_core.prior_tdee(
        series[-1][1], HEIGHT_CM, _age_on(date), sex=SEX)

    return energy_core.report(
        series, intake, goal_kg=(goal_kg or GOAL_WEIGHT_KG),
        rate_pct_per_week=(rate_pct_per_week or energy_core.DEFAULT_RATE_PCT_PER_WEEK),
        prior=prior)


def _age_on(date: str) -> float:
    """Whole years old on a date. Recomputed per call so a birthday is never a
    stale constant someone has to remember to bump."""
    import datetime
    d = datetime.date.fromisoformat(date)
    b = datetime.date.fromisoformat(BIRTH_DATE)
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


def _energy_safe(con, date: str):
    """Never let the energy model break the food log — logging a meal matters
    more than knowing today's target."""
    # Deliberately broad: a bad fit, a missing module or a schema drift must
    # degrade to "no target today", never to a failed meal log.
    try:
        return energy(con, date)
    except Exception:
        return None


def _garmin(con, date: str):
    """Garmin's compact view of the day, if the sync has ever run.

    Imported lazily and failure-tolerant on purpose: health logging must keep
    working on a machine where the Garmin tables were never migrated in, and a
    watch outage must not break the food log."""
    try:
        import garmin_core
        return garmin_core.summary(con, date)
    except (ImportError, sqlite3.Error):
        return None
