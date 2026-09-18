"""MCP server for the health and nutrition system.

Exposes the write path and the reads as typed tools, so a meal or a session is
logged as a sentence in chat rather than through a form. The signature is the
schema the model is held to and the docstring is the instruction it reads
before calling — the two are the same object, which is why they cannot drift
apart.

Extracted from a larger personal server; only the health, food and Garmin
tools are here. Register it with your MCP client pointing at this file, e.g.

    claude mcp add fitness -- python3.13 /path/to/src/mcp_server.py

Several tools exist purely to stop drift: health_known_exercises and
health_known_nutrients are meant to be called BEFORE writing a new name,
because a second spelling forks a trend line in two.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import health_core

mcp = FastMCP("fitness")


def _health(fn, *args, **kwargs):
    """Run a health_core function inside a connection. Convert HealthError → error dict."""
    try:
        with health_core.connect() as con:
            return fn(con, *args, **kwargs)
    except health_core.HealthError as e:
        return {"error": str(e)}


@mcp.tool()
def health_log_weight(date: str, weight_kg: float, notes: str | None = None,
                      body_fat_pct: float | None = None, water_pct: float | None = None,
                      muscle_pct: float | None = None, bone_mass_kg: float | None = None,
                      visceral_fat: float | None = None,
                      comp_source: str | None = None) -> dict:
    """Record a weigh-in (date YYYY-MM-DD). Re-logging a date CORRECTS it, no duplicate row.
    Weigh-ins are only comparable under matched conditions (morning, post-bathroom, pre-food).

    Body composition is optional and every field is kept on a later re-log.
    Pass `comp_source` naming the instrument (e.g. "xiaomi-bia", "dexa") —
    foot-to-foot bioimpedance is a model, so its absolute values must be
    reported as trend-only, never as measured."""
    return _health(health_core.log_weight, date, weight_kg, notes,
                   body_fat_pct=body_fat_pct, water_pct=water_pct,
                   muscle_pct=muscle_pct, bone_mass_kg=bone_mass_kg,
                   visceral_fat=visceral_fat, comp_source=comp_source)


@mcp.tool()
def health_log_meal(date: str, slot: str, raw: str, kcal: float | None = None,
                    protein_g: float | None = None, carbs_g: float | None = None,
                    fat_g: float | None = None, confidence: str | None = None,
                    fibre_g: float | None = None, sat_fat_g: float | None = None,
                    sugar_g: float | None = None, salt_g: float | None = None) -> dict:
    """Log one meal. slot ∈ {breakfast,lunch,dinner,snack}.

    `raw` MUST be verbatim what the user said — it is the source the macro estimate
    can be redone from. Macros are your estimate and are stored flagged as such;
    confidence ∈ {low,med,high} records how much of a guess it was.

    fibre/sat_fat/sugar/salt are on every EU label — fill them whenever a label
    is in hand, they are day-level figures. Vitamins and minerals go to
    health_log_meal_nutrients with the id this returns.

    Returns the day's running protein total, target and remaining gap — report
    the gap back to the user every time."""
    return _health(health_core.log_meal, date, slot, raw, kcal=kcal, protein_g=protein_g,
                   carbs_g=carbs_g, fat_g=fat_g, confidence=confidence,
                   fibre_g=fibre_g, sat_fat_g=sat_fat_g, sugar_g=sugar_g, salt_g=salt_g)


@mcp.tool()
def health_log_meal_nutrients(meal_id: int, nutrients: dict,
                              confidence: str | None = None) -> dict:
    """Attach vitamins/minerals to a meal already logged (meal_id from health_log_meal
    or health_day).

    `nutrients` maps nutrient -> amount in its canonical unit, e.g.
    {"vitamin-d": 50, "calcium": 510}. Call health_known_nutrients first: an
    unknown name or a wrong unit is rejected rather than silently stored.

    confidence is THIS BATCH's, not the meal's — figures off a label are 'high',
    figures recalled from memory are 'low'. Log them as separate calls rather
    than letting a guess inherit a label's confidence."""
    return _health(health_core.log_nutrients, meal_id, nutrients, confidence=confidence)


@mcp.tool()
def health_known_nutrients() -> list[dict]:
    """Accepted nutrient slugs with canonical unit and adult reference intake.
    Check before logging micros so units and spellings stay summable."""
    return health_core.known_nutrients()


@mcp.tool()
def health_log_workout(date: str, type: str, duration_min: int | None = None,
                       rpe: int | None = None, notes: str | None = None,
                       sets: list[dict] | None = None) -> dict:
    """Log a training session. type e.g. strength|run|bike|swim|walk. rpe 1-10.

    sets is a list of {exercise, reps, weight_kg, rpe} — one entry PER SET, so
    3x5 bench is three entries. Call health_known_exercises first and reuse an
    existing exercise name where it matches: a new spelling forks the trend."""
    return _health(health_core.log_workout, date, type, duration_min=duration_min,
                   rpe=rpe, notes=notes, sets=sets)


@mcp.tool()
def health_known_exercises() -> list[dict]:
    """Exercise slugs already logged, with their display names. Check before logging
    a lift so naming stays consistent."""
    return _health(health_core.known_exercises)


@mcp.tool()
def health_day(date: str) -> dict:
    """Everything logged on one day: weight, meals + macro totals, workouts + sets,
    protein target/consumed/remaining, plus per-meal nutrients and a `micros`
    rollup against reference intakes. Call this BEFORE writing to a day already
    touched — it turns a correction into an update instead of a duplicate.

    `garmin` carries what the watch measured that day (sleep, resting HR, HRV,
    Body Battery, steps, burn) — null there means the watch has no data for the
    date, never zero."""
    return _health(health_core.day, date)


@mcp.tool()
def health_energy(date: str, goal_kg: float | None = None,
                  rate_pct_per_week: float | None = None) -> dict | None:
    """Adaptive calorie target for a date, solved from observed results.

    Maintenance is inferred, not modelled: mean intake over complete days, plus
    what the weight trend says was burned on top of it. That absorbs the error
    in Garmin's BMR estimate AND in the macro guesses, because both are already
    inside the weight response being fitted.

    NEVER add workout calories to `target_kcal`. Training is already inside
    `tdee_kcal` by construction — adding it again double-counts it.

    Read `settled` and `stderr_kcal` before quoting the number. Under ~14 days
    the interval is wide enough that the trend is not distinguishable from no
    change, and reporting the target bare would present a guess as a
    measurement. `hold_deficit` true means the scale has not given permission to
    cut further — either too little time has passed to tell, or weight is still
    falling; it is the ordinary state early in a cut, not an alarm.
    `hold_applied` true is the stronger statement: a stall was about to drag the
    target down on what is almost certainly water, and the engine held it at
    recent mean intake instead.

    `stderr_kcal` is null at exactly two weigh-ins — a two-point fit is perfect
    and proves nothing. `slope_pct_per_week` is signed: negative is losing.

    Returns None when there are fewer than two weigh-ins: that is not a trend."""
    return _health(health_core.energy, date, goal_kg, rate_pct_per_week)


@mcp.tool()
def health_delete(kind: str, entry_id: int) -> dict:
    """Delete a logged entry. kind ∈ {weight,meal,workout,nutrient}. Deleting a
    workout also removes its sets; deleting a meal also removes its nutrients."""
    return _health(health_core.delete_entry, kind, entry_id)


@mcp.tool()
def health_amend_meal(meal_id: int, raw: str | None = None, slot: str | None = None,
                      date: str | None = None, kcal: float | None = None,
                      protein_g: float | None = None, carbs_g: float | None = None,
                      fat_g: float | None = None, fibre_g: float | None = None,
                      sat_fat_g: float | None = None, sugar_g: float | None = None,
                      salt_g: float | None = None,
                      confidence: str | None = None) -> dict:
    """Correct a meal already logged. Pass only what changes.

    Use this rather than re-logging: health_log_meal upserts on (date, slot,
    raw), so a CORRECTED sentence logged again becomes a second meal and the
    day counts the food twice. Deleting and re-logging loses any micronutrients
    attached to the id."""
    return _health(health_core.amend_meal, meal_id, raw=raw, slot=slot, date=date,
                   kcal=kcal, protein_g=protein_g, carbs_g=carbs_g, fat_g=fat_g,
                   fibre_g=fibre_g, sat_fat_g=sat_fat_g, sugar_g=sugar_g,
                   salt_g=salt_g, confidence=confidence)


@mcp.tool()
def health_unlinked_activities(date: str) -> list[dict] | dict:
    """Activities the watch recorded on `date` that no logged session claims yet.

    Call this before health_link_activity: it is the shortlist to match against,
    which matters on a day holding several walks."""
    return _health(health_core.unlinked_activities, date)


@mcp.tool()
def health_link_activity(workout_id: int, garmin_id: int) -> dict:
    """Bind a logged session to the Garmin activity recorded for it, copying the
    watch's heart rate and burn onto the session.

    Heart rate is the one thing about a session that cannot be self-reported, so
    a session without this link has loads and no idea what they cost. Both must
    be on the same date, and an activity can be claimed by only one session."""
    return _health(health_core.link_activity, workout_id, garmin_id)


@mcp.tool()
def health_watch_sets(workout_id: int) -> list[dict] | dict:
    """Per-set timing from the watch for a linked session: how long each set
    took and how long the rest after it ran, in performed order.

    The watch covers times and heart rate. Sets, reps and loads are logged in
    chat and chat is authoritative — the reps and weights returned here are the
    watch's own second typing of them and they drift, so never use them to
    correct a logged set."""
    return _health(health_core.watch_sets, workout_id)


@mcp.tool()
def health_merge_exercise(from_slug: str, into_slug: str) -> dict:
    """Move every set from one exercise slug onto another, and keep the target's
    display name. For repairing a fork that health_known_exercises did not catch
    in time — two spellings of one lift are two half-length trend lines."""
    return _health(health_core.merge_exercise, from_slug, into_slug)


@mcp.tool()
def health_range(start: str, end: str) -> list[dict] | dict:
    """Per-day intake, weight and workout counts between two dates, inclusive.

    health_day answers a single date; this is the one to reach for on any trend
    question. Days with nothing logged come back with nulls rather than being
    dropped — a gap is a fact about the record."""
    return _health(health_core.range_days, start, end)


# --- food composition -----------------------------------------------------
#
# The per-100g layer: foods stored once from a label or CIQUAL, meals built
# from weighed components. Without these tools the model can only write flat
# per-meal estimates into meal_log, which is what it did for every meal
# logged after the module landed.

def _food(fn, *args, **kwargs):
    """Run a food_core function inside a connection. FoodError → error dict."""
    import food_core
    try:
        with health_core.connect() as con:
            return fn(con, *args, **kwargs)
    except food_core.FoodError as e:
        return {"error": str(e)}


@mcp.tool()
def food_search(query: str, limit: int = 15) -> list[dict] | dict:
    """Search CIQUAL (the French composition table) for an unlabelled food.

    Accents and case are ignored, so `mache` finds `Mâche, crue`. Returns
    alim_code + French name; pass the code to food_import_ciqual."""
    import food_core
    return _food(food_core.ciqual_search, query, limit=limit)


@mcp.tool()
def food_list() -> list[dict] | dict:
    """Foods already stored, with their source. Check before importing or
    defining one: a second slug for a food already held forks it in two."""
    import food_core
    return _food(food_core.list_foods)


@mcp.tool()
def food_get(slug: str) -> dict | None:
    """One stored food: per-100g macros, micros, and where the numbers came from."""
    import food_core
    return _food(food_core.get_food, slug)


@mcp.tool()
def food_import_ciqual(alim_code: str, slug: str | None = None) -> dict:
    """Store a CIQUAL food per 100g under `slug` (from food_search).

    One CIQUAL code is one food: importing a code already stored under another
    slug is refused rather than forking it."""
    import food_core
    out = _food(food_core.import_ciqual, alim_code, slug=slug)
    return out if isinstance(out, dict) else {"slug": out}


@mcp.tool()
def food_define(slug: str, name: str, source: str = "label",
                kcal: float | None = None, protein_g: float | None = None,
                carbs_g: float | None = None, fat_g: float | None = None,
                sat_fat_g: float | None = None, sugar_g: float | None = None,
                fibre_g: float | None = None, salt_g: float | None = None,
                barcode: str | None = None, notes: str | None = None,
                nutrients: dict | None = None) -> dict:
    """Store a food from its label. ALL figures PER 100g, never per portion —
    the label's own per-100g column, not its per-serving one.

    source ∈ {label,ciqual,usda,off,estimate}; `label` for a photographed
    packet, `estimate` when nothing better exists — it must stay visible as a
    guess. Micros the label declares go in `nutrients` against the slugs from
    health_known_nutrients."""
    import food_core
    food = {"name": name, "source": source, "kcal": kcal, "protein_g": protein_g,
            "carbs_g": carbs_g, "fat_g": fat_g, "sat_fat_g": sat_fat_g,
            "sugar_g": sugar_g, "fibre_g": fibre_g, "salt_g": salt_g,
            "barcode": barcode, "notes": notes, "nutrients": nutrients or {}}
    out = _food(food_core.upsert_food, food, slug=slug)
    return out if isinstance(out, dict) else {"slug": out}


@mcp.tool()
def food_fill_micros(slug: str, alim_code: str) -> dict:
    """Fill a labelled food's MISSING micronutrients from a CIQUAL generic.

    EU labels declare almost no micros, so a day of dairy can report a calcium
    figure that is an absence rather than a measurement. A micro the label
    states always wins; CIQUAL only fills what the label left unsaid, and the
    row records which CIQUAL food it borrowed from so the blend is never
    silent."""
    import food_core
    return _food(food_core.attach_ciqual_micros, slug, alim_code)


@mcp.tool()
def meal_add_component(meal_id: int, food_slug: str, grams: float) -> dict:
    """Add a weighed component to a meal, then recompute it.

    This is what makes a meal recomputable: stored as `34g lettuce, 22g
    spinach` it can be re-derived when the food data improves, where a flat
    estimate is frozen at the moment it was guessed. Macros and micros on the
    meal are replaced from its components."""
    import food_core

    def _add_and_sync(con, meal_id, food_slug, grams):
        food_core.add_component(con, meal_id, food_slug, grams)
        return food_core.sync_meal(con, meal_id)

    return _food(_add_and_sync, meal_id, food_slug, grams)


@mcp.tool()
def meal_recompute(meal_id: int) -> dict | None:
    """Re-derive a meal's macros and micros from its weighed components.

    Worth running after a component's food is corrected or its micros filled —
    that is the whole point of storing components rather than a total."""
    import food_core
    return _food(food_core.sync_meal, meal_id)


# --- garmin ---------------------------------------------------------------
#
# garmin_core is pure stdlib and safe to import at call time; garmin_client
# pulls in `garminconnect`, so it stays inside the sync tool. A top-level
# import of it would take down every vault tool on a machine where that
# package is missing, not just the two below.

@mcp.tool()
def garmin_sync(days: int = 3, end: str | None = None) -> dict:
    """Pull the Venu 3S data for the last `days` days (default 3) into unified.db.

    The window is deliberately more than one day: Garmin revises sleep, HRV and
    Body Battery hours after the fact, so a rolling re-pull corrects yesterday's
    provisional figures. Every write is an upsert and never overwrites a stored
    value with a null, so running this repeatedly is safe.

    Reads afterwards go through health_day (compact) or garmin_day (everything).
    """
    from datetime import date as _date, timedelta
    try:
        import garmin_client, garmin_core
        email = (garmin_client.TOKEN_DIR / "account.txt").read_text().strip()
        api = garmin_client.connect(email)
        last = _date.fromisoformat(end) if end else _date.today()
        first = last - timedelta(days=max(1, days) - 1)
        with garmin_core.connect() as con:
            return garmin_core.sync_range(con, api, first.isoformat(), last.isoformat())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def garmin_day(date: str) -> dict:
    """Everything Garmin recorded for one date, raw payload included.

    health_day already carries the summary — reach for this only when you need a
    metric that was not modelled as a column (it is in `raw_json`)."""
    try:
        import garmin_core
        with garmin_core.connect() as con:
            return garmin_core.day(con, date)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    mcp.run()
