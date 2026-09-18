# Setting this up for a person — instructions for an AI assistant

You are setting up an adaptive calorie-target system for the person you are
talking to. Your job in this file is to **interview them, write
`profile.json`, and create the database**. Do not guess any of these values.
A wrong height is not a small error: it feeds the Mifflin-St Jeor prior, which
is what holds the target steady for the first fortnight before there is enough
weight data to fit.

Ask in this order. Ask one thing at a time; do not present the whole list at
once.

## 1. The body

- **Height**, in centimetres.
- **Date of birth.** Age is what the formula takes; a birth date means the
  figure stays right next year without anyone remembering to update it.
- **Sex**, male or female. This selects a constant in the formula (+5 / −161).
  If they would rather not answer, say that the formula only offers these two
  and let them pick the one closer to their situation, or skip the prior
  entirely and accept a wobblier first two weeks.
- **Current weight**, in kilograms, and whether they have a scale they will
  use regularly. The engine needs repeat weigh-ins, not a one-off number.
  Roughly daily is ideal; three or four a week still fits.
- **Body composition**, if their scale reports it — body fat %, water %,
  muscle %, bone mass, visceral fat. Optional. Stored when present, never
  required.

## 2. The goal

- **Goal weight**, or "maintain", or "gain". The engine is written for a cut;
  a gain is the same arithmetic with the sign flipped, but say plainly that
  the guards (floors, rate cap) are tuned for loss.
- **How fast.** Express it as a percentage of bodyweight per week, and explain
  what that means for them in kilograms — 0.75%/week is the default and a
  reasonable one. Anything above 1%/week is refused by the code, because
  beyond that the loss stops being mostly fat.
- **Deadline, if any.** If they name a date, work out the rate it implies and
  tell them whether it is inside the cap. Do not quietly exceed it.

## 3. Training and diet

- **Do they lift?** If so, protein matters more and the default of 1.8 g per
  kg of bodyweight applies. Confirm it rather than assuming.
- **Dietary pattern** — vegetarian, vegan, omnivore, allergies, anything they
  avoid. This does not change the arithmetic. It changes every food you
  suggest afterwards, and getting it wrong once costs their trust in the whole
  system.
- **Activity outside training**: sedentary desk job, on their feet all day,
  long commute on foot. This sets the activity factor on the prior, 1.2 to
  1.55. It stops mattering as the fit takes over.

## 4. Medical

Ask whether there is anything that should shape the targets — a condition, a
medication, a history of disordered eating. Two rules:

- If they mention a condition, **do not treat it**. Record it, and say the
  targets here are arithmetic about energy and not medical advice.
- If anything they say suggests disordered eating, or if the goal weight they
  name is below a BMI of about 18.5, **stop and say so plainly** rather than
  computing a deficit. This system is very good at holding someone to a number
  and has no judgement about whether the number is a good idea.

## 5. Write the profile

Write `profile.json` in the repository root:

```json
{
  "height_cm": 175,
  "birth_date": "1990-01-01",
  "sex": "male",
  "goal_weight_kg": 70,
  "rate_pct_per_week": 0.75,
  "protein_g_per_kg": 1.8,
  "activity_factor": 1.375
}
```

Then create the database:

```
sqlite3 db/fitness.db < db/schema-health.sql
```

Log the current weight straight away — `health_core.log_weight` — because the
engine cannot do anything at all until there are two weigh-ins.

## 6. Tell them what to expect

Be honest about the first fortnight, because it is the part that feels broken:

- With fewer than two weigh-ins there is **no adaptive target at all**, only
  the formula's estimate.
- For the first two weeks the fit is dominated by water. The engine reports
  `settled: false` and holds the deficit where it is rather than chasing the
  trend.
- The error bar starts enormous — at five weigh-ins the 95% interval on the
  slope can easily span zero, meaning "we cannot yet tell this apart from no
  change". That is the engine being honest, not broken.
- Control transfers from the formula to their own data automatically as
  weigh-ins accumulate. Nothing needs switching over.

## Logging, afterwards

Meals are logged as free text; you resolve them into weighed components
against `food`, which holds per-100g figures from labels or from CIQUAL.
Rules that matter:

- Keep `raw` verbatim. It is what a correction is re-derived from.
- Call `health_known_exercises` / `health_known_nutrients` before writing a new
  name. A second spelling forks a trend line in two.
- Macro estimates are stored flagged as estimates, with a confidence. Never
  present an estimate as a measurement.
- Never add a workout's calories on top of the target. The burn is already
  inside the inferred maintenance because it is already inside the weight
  response being fitted.
