"""Adaptive energy engine: infer maintenance from observed results, not models.

A flat calorie cap cannot know that daily burn swings 2154 -> 2533 kcal on step
count alone, and it cannot self-correct when metabolism adapts to a cut. So the
target is not set from a formula about the body — it is solved backwards from
what the body actually did:

    TDEE = mean_intake - (weight_slope_kg_per_day * FAT_KCAL_PER_KG)

Every error this replaces is absorbed rather than propagated. Garmin's BMR model
is wrong by some unknown amount; the macro estimates on a bowl of oats are wrong
by some other amount. Neither matters, because both are already baked into the
weight response being fitted. Garmin is evidence here, never truth.

THE INVARIANT THIS MODULE EXISTS TO PROTECT: never add workout calories on top
of an adaptive target. The session's burn is already inside the inferred
maintenance, because it is already inside the weight response. Adding it again
double-counts. If a caller ever wants an "exercise bonus" field, the answer is
no — see test_report_assembles_without_double_counting_the_workout.

Pure by design: no DB handle, no clock, no I/O. The arithmetic is the asset and
it wants to be checkable against a hand-computed fit.

The arithmetic is deliberately checkable by hand against a short series of
weigh-ins and intake days.
"""
import datetime
import math

# Standard fat-equivalent constant. Real tissue is not pure fat, so this is a
# convention shared with every cutting calculator rather than a measurement —
# but it is the same convention the input assumptions were built on.
FAT_KCAL_PER_KG = 7700.0

DEFAULT_RATE_PCT_PER_WEEK = 0.75

# Above ~1%/week of bodyweight the loss stops being mostly fat. Training near
# failure in a deficit is exactly the context where an over-aggressive rate is
# paid for in muscle.
MAX_RATE_PCT_PER_WEEK = 1.0

# Two floors, both derived from quantities this module already trusts.
#
# Deliberately NOT floored on Garmin's BMR figure. A wearable's "BMR" figure is typically high enough
# that using it as a hard constraint would clamp every target above the inferred
# maintenance and make a cut arithmetically impossible. More fundamentally, binding the target to an
# external model reintroduces precisely the model error this design exists to
# absorb. The relative floor scales with the individual without borrowing
# anyone else's estimate of them.
ABSOLUTE_FLOOR_KCAL = 1500.0
MIN_TDEE_FRACTION = 0.65

# Below this span a fit is dominated by water: glycogen, gut content and sodium
# move the scale by more in a day than fat does in a week.
MIN_SETTLED_DAYS = 14
STALL_DAYS = 10

PROTEIN_G_PER_KG = 1.8  # Mirrors health_core. The deficit never comes from here.

# Day-to-day scale noise: hydration, glycogen, gut content. A weigh-in is not a
# measurement of fat mass to better than roughly this, so a trend cannot be
# resolved finer than SCALE_NOISE_KG / sqrt(Sxx) no matter how tidily the points
# happen to line up. Without this floor a perfectly flat series reports zero
# error, which in a precision-weighted blend means infinite confidence — total
# control handed to the noisiest possible estimate.
SCALE_NOISE_KG = 0.3

# Mifflin-St Jeor predicts BMR to roughly +-10%, and the activity factor is a
# coarser guess still. 15% on the product is generous to the formula without
# pretending it is a measurement.
PRIOR_REL_STDERR = 0.15

# Corresponds to a desk job with ~5000 steps and a few gym sessions a week.
DEFAULT_ACTIVITY_FACTOR = 1.375


class EnergyError(Exception):
    pass


def _days_between(a: str, b: str) -> float:
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


def weight_trend(series: list[tuple[str, float]]) -> dict:
    """Least-squares fit over (date, kg) pairs, in real elapsed days.

    Elapsed days rather than sample index: weigh-ins are not guaranteed daily,
    and treating a 4-day gap as one step silently inflates the slope.

    Returns the fitted endpoint rather than the last reading. A single weigh-in
    carries a kilo of water noise; the fit is what the target should stand on.
    """
    if len(series) < 2:
        raise EnergyError("a trend needs at least two weigh-ins")

    pts = sorted(series, key=lambda p: p[0])
    origin = pts[0][0]
    xs = [_days_between(origin, d) for d, _ in pts]
    ys = [float(w) for _, w in pts]
    n = len(pts)

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        raise EnergyError("all weigh-ins share one date — no span to fit")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))

    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    # Standard error of the slope. With few points this is large, and that is
    # the honest headline: on five weigh-ins the 95% interval spans zero, so
    # the trend is not yet distinguishable from no change at all.
    # The floor is what the scale can physically support, and it relaxes on its
    # own as weigh-ins accumulate: ~0.095 kg/day over 5 daily points, ~0.006
    # over 30. No manual switch to forget.
    floor = SCALE_NOISE_KG / math.sqrt(sxx)
    if n > 2:
        sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        residual = math.sqrt((sse / (n - 2)) / sxx)
        stderr = max(residual, floor)
        floored = stderr > residual
    else:
        # Two points fit perfectly and prove nothing, so the floor is all there
        # is. Never NaN: NaN serializes to bare `NaN`, which is not valid JSON,
        # and this value travels out through an MCP tool.
        stderr = floor
        floored = True

    span = xs[-1]
    return {
        "slope_kg_per_day": slope,
        "intercept_kg": intercept,
        "trend_kg": intercept + slope * span,
        "stderr_kg_per_day": stderr,
        "stderr_floored": floored,
        "n": n,
        "days_span": span,
        "first_date": pts[0][0],
        "last_date": pts[-1][0],
        # "settled" gates presentation, not computation. A number is always
        # produced; this says whether it may be spoken of as established.
        "settled": span >= MIN_SETTLED_DAYS,
    }


def mifflin_bmr(weight_kg: float, height_cm: float, age_years: float,
                sex: str = "male") -> float:
    """Mifflin-St Jeor resting metabolic rate — the modern standard formula.

    Note what this is NOT: Garmin's `bmrKilocalories`. Solving this backwards for a
    typical Garmin `bmrKilocalories` gives an implausible height, because that
    figure already contains baseline daily movement and is closer to a sedentary
    TDEE. The two are not interchangeable."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age_years
    return base + (5 if sex == "male" else -161)


def prior_tdee(weight_kg: float, height_cm: float, age_years: float,
               activity_factor: float = DEFAULT_ACTIVITY_FACTOR,
               sex: str = "male") -> dict:
    """A population-formula estimate of maintenance, with an honest error bar.

    This is scaffolding, not truth. It predicts from body dimensions, so it is
    right about the average person of those dimensions and only incidentally
    right about this one. Its job is to stabilise the target during the weeks
    when the measured trend is still mostly water — and then to get out of the
    way. See adaptive_tdee(prior=...)."""
    tdee = mifflin_bmr(weight_kg, height_cm, age_years, sex) * activity_factor
    return {"tdee_kcal": tdee, "stderr_kcal": tdee * PRIOR_REL_STDERR,
            "bmr_kcal": mifflin_bmr(weight_kg, height_cm, age_years, sex),
            "activity_factor": activity_factor}


def adaptive_tdee(trend: dict, intake: list[tuple[str, float]],
                  prior: dict | None = None) -> dict:
    """Solve maintenance from intake and the observed weight response.

    `intake` must contain COMPLETE days only. A day still being logged drags
    the mean down and fabricates a deficit that was never eaten.

    With a `prior`, the two are combined by inverse-variance weighting — each
    estimate counts in proportion to its own precision. This matters because the
    raw fit is violently unstable early: on five weigh-ins, dropping a single
    water-driven point moved this figure by 776 kcal. Weighting fixes that
    without ever capping what the data can eventually say, because the adaptive
    standard error shrinks as weigh-ins accumulate and the prior's does not — so
    control transfers automatically. Around 19% adaptive weight at five
    weigh-ins, past 90% at a month.
    """
    if not intake:
        raise EnergyError("adaptive TDEE needs at least one complete intake day")

    kcals = [float(k) for _, k in intake]
    mean_intake = sum(kcals) / len(kcals)
    tdee = mean_intake - trend["slope_kg_per_day"] * FAT_KCAL_PER_KG

    # Slope uncertainty is the dominant term and converts straight to kcal.
    # Dropping it on the way out is how an estimate starts looking measured.
    se = trend["stderr_kg_per_day"]
    stderr_kcal = None if se is None else se * FAT_KCAL_PER_KG

    out = {
        "tdee_kcal": tdee,
        "mean_intake_kcal": mean_intake,
        "intake_days": len(kcals),
        "stderr_kcal": stderr_kcal,
        "settled": trend["settled"],
        "prior_kcal": None,
        "combined_kcal": tdee,
        "combined_stderr": stderr_kcal,
        "adaptive_weight": 1.0,
    }
    if prior is not None and stderr_kcal:
        wa, wp = 1 / stderr_kcal ** 2, 1 / prior["stderr_kcal"] ** 2
        out["prior_kcal"] = prior["tdee_kcal"]
        out["combined_kcal"] = (tdee * wa + prior["tdee_kcal"] * wp) / (wa + wp)
        out["combined_stderr"] = math.sqrt(1 / (wa + wp))
        out["adaptive_weight"] = wa / (wa + wp)
    return out


def daily_target(tdee_kcal: float, weight_kg: float,
                 rate_pct_per_week: float = DEFAULT_RATE_PCT_PER_WEEK,
                 hold_floor_kcal: float | None = None) -> dict:
    """Intake target = inferred maintenance - a rate-derived deficit.

    The setpoint is a percentage of bodyweight per week, not a fixed number of
    calories, so the cut eases automatically as he gets lighter instead of
    quietly becoming more aggressive every kilo down.

    When a floor binds, the DEFICIT gives way — never the floor.

    `hold_floor_kcal` is the stall guard, and it is a floor rather than a flag
    because a flag is something a caller can forget to read. See report().
    """
    rate_capped = rate_pct_per_week > MAX_RATE_PCT_PER_WEEK
    rate = min(rate_pct_per_week, MAX_RATE_PCT_PER_WEEK)

    kg_per_week = weight_kg * rate / 100.0
    deficit = kg_per_week * FAT_KCAL_PER_KG / 7.0

    floor = max(ABSOLUTE_FLOOR_KCAL, tdee_kcal * MIN_TDEE_FRACTION)

    raw_target = tdee_kcal - deficit
    target = max(raw_target, floor)
    floor_applied = target > raw_target

    hold_applied = False
    if hold_floor_kcal is not None and target < hold_floor_kcal:
        target = hold_floor_kcal
        hold_applied = True

    return {
        "target_kcal": target,
        "deficit_kcal": tdee_kcal - target,
        "requested_deficit_kcal": deficit,
        "rate_pct_per_week": rate,
        "rate_capped": rate_capped,
        "floor_kcal": floor,
        "floor_applied": floor_applied,
        "hold_applied": hold_applied,
        # Fixed by bodyweight, never traded against the deficit. Carbs and fat
        # absorb the cut.
        "protein_g": round(weight_kg * PROTEIN_G_PER_KG),
    }


def project_to_goal(current_kg: float, goal_kg: float,
                    rate_pct_per_week: float = DEFAULT_RATE_PCT_PER_WEEK,
                    start_date: str | None = None) -> dict:
    """Days to a goal weight at the PRESCRIBED rate, not the observed slope.

    Deliberately not projected from the measured slope: early in a cut that
    slope is mostly water and its error bar spans zero, so extrapolating it
    promises a date the body never agreed to.

    The path is geometric, not linear — a fixed percentage of a shrinking body
    is a shrinking absolute loss, so the back half of a cut takes longer than
    the front half. Pretending otherwise is how a plan slips its deadline.
    """
    if goal_kg >= current_kg:
        raise EnergyError("goal weight is not below current weight")
    if not 0 < rate_pct_per_week <= MAX_RATE_PCT_PER_WEEK:
        raise EnergyError(f"rate must be in (0, {MAX_RATE_PCT_PER_WEEK}] %/week")

    weekly_factor = 1.0 - rate_pct_per_week / 100.0
    weeks = math.log(goal_kg / current_kg) / math.log(weekly_factor)
    days = weeks * 7.0

    date = None
    if start_date:
        date = (datetime.date.fromisoformat(start_date)
                + datetime.timedelta(days=round(days))).isoformat()

    return {
        "days": days,
        "weeks": weeks,
        "date": date,
        "current_kg": current_kg,
        "goal_kg": goal_kg,
        "rate_pct_per_week": rate_pct_per_week,
    }


def should_cut_further(trend: dict) -> bool:
    """Whether a stall is real enough to act on.

    Guards the documented trap: a flat scale for under ~10 days is water, and
    cutting in response to it compounds an existing deficit for no reason. The
    engine must be able to say "hold" — a system that can only ratchet down is
    not adaptive, it is just impatient.
    """
    if trend["days_span"] < STALL_DAYS:
        return False
    return trend["slope_kg_per_day"] >= 0


def report(series: list[tuple[str, float]], intake: list[tuple[str, float]],
           goal_kg: float | None = None,
           rate_pct_per_week: float = DEFAULT_RATE_PCT_PER_WEEK,
           prior: dict | None = None) -> dict:
    """One call: trend -> maintenance -> target -> projection.

    Note what is absent. There is no exercise-bonus field and there must never
    be one: training is already inside `tdee_kcal` by construction.
    """
    trend = weight_trend(series)
    energy = adaptive_tdee(trend, intake, prior=prior)

    # THE STALL GUARD. A flat scale collapses inferred maintenance onto mean
    # intake, which would drive the target hundreds of calories down on what is
    # almost certainly water — glycogen and gut content, heaviest in exactly the
    # first fortnight when `settled` is still False. So while the trend is both
    # unsettled and not genuinely stalled, the target may not fall below what he
    # has actually been eating. Expressed as a floor passed into daily_target
    # rather than a flag on the way out, because a flag is something a caller
    # can forget to read — and the first version of this shipped as a flag that
    # nothing honoured.
    hold = not should_cut_further(trend)
    hold_floor = energy["mean_intake_kcal"] if (hold and not trend["settled"]) else None

    # The target is built on the BLENDED figure, which equals the raw adaptive
    # one when no prior was supplied.
    target = daily_target(energy["combined_kcal"], trend["trend_kg"],
                          rate_pct_per_week=rate_pct_per_week,
                          hold_floor_kcal=hold_floor)

    out = {
        **target,
        "tdee_kcal": energy["combined_kcal"],
        "adaptive_only_kcal": energy["tdee_kcal"],
        "prior_kcal": energy["prior_kcal"],
        "adaptive_weight": energy["adaptive_weight"],
        "mean_intake_kcal": energy["mean_intake_kcal"],
        "intake_days": energy["intake_days"],
        "stderr_kcal": energy["combined_stderr"],
        "adaptive_only_stderr": energy["stderr_kcal"],
        "trend_kg": trend["trend_kg"],
        "slope_kg_per_day": trend["slope_kg_per_day"],
        # Signed: negative is losing, positive is gaining. An abs() here once
        # reported a gaining trend as 0.88%/week of loss.
        "slope_pct_per_week": (trend["slope_kg_per_day"] * 7
                               / trend["trend_kg"] * 100) if trend["trend_kg"] else None,
        "weigh_ins": trend["n"],
        "days_span": trend["days_span"],
        "settled": trend["settled"],
        # hold_deficit: the scale has not given permission to cut further —
        # either too little time has passed to tell, or it is still falling.
        # hold_applied (from daily_target) is the stronger statement: the guard
        # actually intervened and raised the target.
        "hold_deficit": hold,
    }
    if goal_kg is not None:
        out["projection"] = project_to_goal(
            trend["trend_kg"], goal_kg,
            rate_pct_per_week=target["rate_pct_per_week"],
            start_date=trend["last_date"])
    return out
