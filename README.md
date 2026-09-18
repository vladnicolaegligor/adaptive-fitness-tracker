# Adaptive fitness tracker

The parts of a personal food-and-training tracker that are worth reading:
a calorie target **inferred from observed results** rather than predicted from
a formula, and a food layer where a meal is weighed components against
reference composition data rather than one flat estimate.

Companion code to a build post on [halzine.xyz](https://halzine.xyz).

## The idea

Every tracker sets a target by predicting maintenance from body dimensions and
then never checks the prediction. This solves it backwards instead:

```
TDEE = mean_intake − (weight_slope_kg_per_day × 7700)
```

Both inputs are wrong — macro estimates by an unknown amount, the wearable's
burn figure by another — and neither has to be right, because both are already
inside the weight response being fitted. A systematic bias in the intake
estimate shifts inferred maintenance by the same amount and cancels at the
target. The estimate has to be *consistently* inaccurate, not accurate.

## Layout

```
src/energy_core.py    the estimator: OLS fit over weigh-ins, slope standard
                      error with a scale-noise floor, inverse-variance blend
                      with a Mifflin-St Jeor prior. Pure: no DB, no clock, no
                      I/O — it takes numbers and returns numbers.
src/food_core.py      per-100g food composition: USDA and CIQUAL import, label
                      entry, CIQUAL micro gap-fill with provenance recorded,
                      meals as weighed components.
src/health_core.py    the validating write path: weight, meals, workouts.
src/mcp_server.py     25 tools over all of the above, so a meal or a session
                      is logged as a sentence in chat rather than through a
                      form. Register it with an MCP client:
                      claude mcp add fitness -- python3.13 src/mcp_server.py
src/garmin_*.py       Garmin Connect sync. Credentials are never in here — the
                      password lives in the macOS Keychain and the OAuth token
                      is cached under db/, which is gitignored.
db/schema-health.sql  the whole schema, structure only, no rows.
db/migrations/        the same schema as it was actually built, in order.
AI_SETUP.md           questions an AI assistant should ask to configure this
                      for a person, and what to warn them about.
profile.example.json  the only file describing a body.
```

## Setup

Python 3.10+ (the type syntax needs it; developed on 3.13).

```
pip install -r requirements.txt           # optional — see the file
cp profile.example.json profile.json      # or let an assistant fill it in
sqlite3 db/fitness.db < db/schema-health.sql
```

Computing a target needs no third-party packages at all: `energy_core` is
stdlib-only and `health_core` needs `sqlite3`. The requirements buy the MCP
tool surface, the Garmin sync and the CIQUAL spreadsheet import.

`FITNESS_PROFILE` and `FITNESS_DB` override those paths.

## What is deliberately not here

- **Any of my data.** The schema ships empty.
- **The CIQUAL composition tables.** The structure is here; the data is
  [ANSES's](https://ciqual.anses.fr/) to distribute, not mine.
- **Tests.** They exist, they are not published.
- **The wider system.** These modules came out of a larger personal setup
  (notes, todos, a graph over them). Only the health, food and Garmin parts are
  here.

## What it does not do

It is arithmetic about energy balance. It is not medical advice, it does not
know anything about your health, and it will hold you to a number without any
opinion about whether that number is a good idea. `AI_SETUP.md` says where an
assistant should stop and push back instead.
