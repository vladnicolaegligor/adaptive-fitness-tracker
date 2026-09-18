"""The only file that describes a person. Everything else is arithmetic.

Values are read from `profile.json` beside this file — see profile.example.json,
and AI_SETUP.md for the questions that fill it in. Nothing here has a default
that is a guess about your body: a missing value raises rather than inventing
one, because a silently wrong height propagates into every target the engine
produces.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = Path(os.environ.get("FITNESS_PROFILE", ROOT / "profile.json"))
DB_PATH = os.environ.get("FITNESS_DB", str(ROOT / "db" / "fitness.db"))


class ProfileError(Exception):
    pass


def load() -> dict:
    if not PROFILE_PATH.exists():
        raise ProfileError(
            f"no profile at {PROFILE_PATH}. Copy profile.example.json to "
            f"profile.json and fill it in, or point FITNESS_PROFILE at one. "
            f"AI_SETUP.md has the questions.")
    data = json.loads(PROFILE_PATH.read_text())
    missing = [k for k in ("height_cm", "birth_date", "sex", "goal_weight_kg")
               if data.get(k) in (None, "")]
    if missing:
        raise ProfileError(f"profile is missing: {', '.join(missing)}")
    return data


def get(key: str, default=None):
    """One profile value. Falls back only for genuinely optional settings."""
    try:
        return load().get(key, default)
    except ProfileError:
        if default is not None:
            return default
        raise
