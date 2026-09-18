"""Garmin Connect network layer: auth and raw payload fetch. No DB, no mapping.

Everything that talks to Garmin lives here so garmin_core stays pure and
testable against recorded payloads — the API is unofficial and its field names
move, so the mapping must be exercisable without a login.

Credentials never live in this repo. The password sits in the macOS Keychain
(service `garmin-connect`) and is read once, at first login; after that the
OAuth token cached in db/garmin_tokens/ carries every later run, so the normal
path touches no password at all.
"""
import json
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TOKEN_DIR = PROJECT / "db" / "garmin_tokens"   # under db/ => already gitignored
KEYCHAIN_SERVICE = "garmin-connect"


class GarminError(Exception):
    pass


def keychain_password(account: str) -> str:
    """Read the Connect password from the login keychain.

    subprocess rather than a library so the secret is never written to a file
    this repo can see, and never passed as an argument (which would put it in
    the process table)."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", account, "-w"],
            capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        raise GarminError(
            f"no keychain item {KEYCHAIN_SERVICE!r} for {account!r}. Add it with:\n"
            f"  security add-generic-password -s {KEYCHAIN_SERVICE} -a {account} -w")
    return out.stdout.strip()


def connect(email: str, password: str | None = None, mfa_prompt=None):
    """Return a logged-in Garmin client, reusing the cached token when possible.

    `mfa_prompt` is called only when Garmin demands a code. It is a callback
    rather than an input() here because the daily sync must never be able to
    block on a prompt nobody is watching — pass None and a required MFA becomes
    an error instead of a hang.
    """
    from garminconnect import Garmin   # imported late: see garmin_core

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    def refuse_mfa() -> str:
        raise GarminError(
            "Garmin asked for an MFA code and nothing here can answer it — "
            "re-run `python3.13 src/garmin_sync.py auth`")

    # One call does the whole cascade: load the cached token if it is there and
    # still good, otherwise a credential login (prompting for MFA only if
    # Garmin demands it) and persist the new token. Passing the tokenstore is
    # what makes the write happen, so it must be given even on the cached path.
    api = Garmin(email=email, password=password or "",
                 prompt_mfa=mfa_prompt or refuse_mfa)
    try:
        api.login(tokenstore=str(TOKEN_DIR))
    except Exception as e:
        if not password:
            raise GarminError(
                "no usable Garmin token and no password given — run "
                f"`python3.13 src/garmin_sync.py auth` once to log in ({e})")
        raise
    return api


# --- raw fetch -----------------------------------------------------------
#
# Each entry is (key, method name, takes_date). Driven by a table rather than
# straight-line code so one endpoint 404ing or changing name degrades that key
# to None instead of losing the whole day.
DAILY_ENDPOINTS = [
    ("stats",        "get_stats_and_body", True),
    ("sleep",        "get_sleep_data",     True),
    ("rhr",          "get_rhr_day",        True),
    ("hrv",          "get_hrv_data",       True),
    ("body_battery", "get_body_battery",   True),
    ("respiration",  "get_respiration_data", True),
    ("spo2",         "get_spo2_data",      True),
    ("max_metrics",  "get_max_metrics",    True),
    ("intensity",    "get_intensity_minutes_data", True),
]


def fetch_day(api, date: str) -> dict:
    """Every daily endpoint for one date, as raw payloads keyed by name.

    A failing endpoint yields {"_error": ...} for its key rather than raising:
    a missing HRV reading must not cost us that night's sleep record.
    """
    out = {}
    for key, method, takes_date in DAILY_ENDPOINTS:
        fn = getattr(api, method, None)
        if fn is None:
            out[key] = {"_error": f"client has no {method}()"}
            continue
        try:
            out[key] = fn(date) if takes_date else fn()
        except Exception as e:
            out[key] = {"_error": f"{type(e).__name__}: {e}"}
    return out


def fetch_activities(api, start: str, end: str) -> list:
    try:
        return api.get_activities_by_date(start, end) or []
    except Exception as e:
        raise GarminError(f"activities {start}..{end}: {type(e).__name__}: {e}")


def fetch_exercise_sets(api, activity_id: int) -> list:
    """Per-set detail for one strength activity: reps, load, duration, rest.

    The activity list only carries per-category totals. This is the endpoint
    with the sets themselves, which is what makes the watch usable as the set
    log rather than a second opinion on one."""
    try:
        return (api.get_activity_exercise_sets(activity_id) or {}).get("exerciseSets") or []
    except Exception as e:
        raise GarminError(f"exercise sets {activity_id}: {type(e).__name__}: {e}")


def probe(api, date: str, out_path: Path) -> Path:
    """Dump one day of raw payloads to a file, for building the column mapping
    against what this watch actually returns rather than what the docs claim."""
    payload = {"date": date, "daily": fetch_day(api, date),
               "activities": fetch_activities(api, date, date)}
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path
