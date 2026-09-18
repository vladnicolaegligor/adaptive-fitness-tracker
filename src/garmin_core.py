"""Garmin data: payload -> row mapping, upserts, reads. No network.

Split from garmin_client on purpose. Everything here is a pure function of a
payload dict or a DB connection, so the mapping — the part that actually breaks
when Garmin renames a field — is testable against recorded payloads without a
login.

Schema is owned by db/migrations/2026-09-09_garmin.sql. Nothing here issues
CREATE TABLE, matching health_core.
"""
import datetime
import json
import sqlite3

import profile as _profile

DB_PATH = _profile.DB_PATH


class GarminDataError(Exception):
    pass


def connect(path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# --- payload helpers -----------------------------------------------------

def _get(payload, key, *path):
    """Dig through a payload, tolerating a missing or errored endpoint.

    Every endpoint is optional by design: an HRV reading that failed must cost
    us the HRV column, not the whole day."""
    node = payload.get(key)
    # Some endpoints (body_battery, max_metrics) answer with a single-element
    # list rather than an object, so unwrap before every lookup, not just
    # inside the loop — a list at the top level used to fall straight through
    # to None.
    for p in (key, *path):
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict) or "_error" in node:
            return None
        if p is key:
            continue
        node = node.get(p)
    return node


def _min(seconds):
    """Garmin reports sleep in seconds; the DB stores minutes because nobody
    ever wanted to know their REM sleep to the second."""
    return None if seconds is None else round(seconds / 60)


def _round(v, digits=1):
    return None if v is None else round(float(v), digits)


def _local_iso(value):
    """Garmin's *TimestampLocal is the GMT epoch already shifted by the
    device's UTC offset, so it reads correctly only when rendered as UTC.
    Formatting it in the machine's own zone applies the offset a second time —
    a 22:21 bedtime surfaced as 01:21. Some endpoints answer with an ISO
    string instead of an epoch; those are already local, pass them through."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return datetime.datetime.fromtimestamp(
        value / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# Per-minute arrays: dropped before raw_json is stored. They are the bulk of
# the payload (megabytes a day) and this DB is backed up nightly.
TIME_SERIES_KEYS = {
    "sleepMovement", "sleepLevels", "sleepRestlessMoments", "wellnessEpochRespirationDataDTOList",
    "wellnessEpochSPO2DataDTOList", "sleepHeartRate", "sleepStress", "sleepBodyBattery",
    "hrvReadings", "bodyBatteryValuesArray", "bodyBatteryValueDescriptorDTOList",
    "stressValuesArray", "heartRateValues", "respirationValuesArray", "spo2HourlyAverages",
    "wellnessEpochSPO2AveragesList", "remSleepData", "breathingDisruptionData",
}


def strip_series(node):
    """Recursively drop per-minute arrays so raw_json stays a few KB."""
    if isinstance(node, dict):
        return {k: strip_series(v) for k, v in node.items() if k not in TIME_SERIES_KEYS}
    if isinstance(node, list):
        return [strip_series(v) for v in node]
    return node


# --- mapping -------------------------------------------------------------

def map_daily(date: str, payload: dict) -> dict:
    """One day of raw endpoint payloads -> a garmin_daily row.

    `date` is passed in rather than read out of the payload: it is the
    calendarDate we requested, and sleep records span midnight, so deriving a
    date from any timestamp in here would misfile roughly half of them.
    """
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    stats = {} if "_error" in stats else stats
    sleep_dto = _get(payload, "sleep", "dailySleepDTO") or {}
    scores = sleep_dto.get("sleepScores") or {}

    row = {
        "date": date,
        # heart
        "resting_hr": stats.get("restingHeartRate") or _rhr_metric(payload),
        "min_hr": stats.get("minHeartRate"),
        "max_hr": stats.get("maxHeartRate"),
        "hrv_avg_ms": _get(payload, "hrv", "hrvSummary", "lastNightAvg"),
        "hrv_status": _get(payload, "hrv", "hrvSummary", "status"),
        # recovery
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "body_battery_charged": stats.get("bodyBatteryChargedValue"),
        "body_battery_drained": stats.get("bodyBatteryDrainedValue"),
        "stress_avg": stats.get("averageStressLevel"),
        "stress_max": stats.get("maxStressLevel"),
        # sleep
        "sleep_score": (scores.get("overall") or {}).get("value") if isinstance(
            scores.get("overall"), dict) else scores.get("overall"),
        "sleep_quality": (scores.get("overall") or {}).get("qualifierKey") if isinstance(
            scores.get("overall"), dict) else None,
        "sleep_total_min": _min(sleep_dto.get("sleepTimeSeconds")),
        "sleep_deep_min": _min(sleep_dto.get("deepSleepSeconds")),
        "sleep_light_min": _min(sleep_dto.get("lightSleepSeconds")),
        "sleep_rem_min": _min(sleep_dto.get("remSleepSeconds")),
        "sleep_awake_min": _min(sleep_dto.get("awakeSleepSeconds")),
        "sleep_start": _local_iso(sleep_dto.get("sleepStartTimestampLocal")),
        "sleep_end": _local_iso(sleep_dto.get("sleepEndTimestampLocal")),
        # Sleeping respiration is the meaningful one; the waking average is a
        # fallback so a day without a recorded night is not simply blank.
        "respiration_avg": _round(sleep_dto.get("averageRespirationValue")
                                  or _get(payload, "respiration", "avgSleepRespirationValue")
                                  or stats.get("avgWakingRespirationValue")),
        "spo2_avg": _get(payload, "spo2", "averageSpO2") or sleep_dto.get("averageSpO2Value"),
        "spo2_min": _get(payload, "spo2", "lowestSpO2") or sleep_dto.get("lowestSpO2Value"),
        # movement
        "steps": stats.get("totalSteps"),
        "floors": stats.get("floorsAscended"),
        "distance_m": _round(stats.get("totalDistanceMeters"), 0),
        "intensity_min_moderate": stats.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": stats.get("vigorousIntensityMinutes"),
        # burn
        "calories_total": stats.get("totalKilocalories"),
        "calories_active": stats.get("activeKilocalories"),
        "calories_bmr": stats.get("bmrKilocalories"),
        "vo2max": _round(_vo2max(payload)),
    }
    row["raw_json"] = json.dumps(strip_series(payload), default=str)
    return row


def _rhr_metric(payload):
    """Resting HR from get_rhr_day, which nests it two levels down under a
    metric name rather than exposing it directly:
    allMetrics.metricsMap.WELLNESS_RESTING_HEART_RATE[0].value"""
    metrics = _get(payload, "rhr", "allMetrics", "metricsMap")
    if not isinstance(metrics, dict):
        return None
    series = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
    for point in series:
        if isinstance(point, dict) and point.get("value") is not None:
            return round(point["value"])
    return None


def _vo2max(payload):
    """get_max_metrics returns a list; the generic VO2max lives under
    generic.vo2MaxPreciseValue, with cycling/running variants beside it."""
    node = payload.get("max_metrics")
    if isinstance(node, list) and node:
        node = node[0]
    if not isinstance(node, dict) or "_error" in node:
        return None
    generic = node.get("generic") or {}
    return generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")


def map_activity(a: dict) -> dict:
    """One Garmin activity -> a garmin_activity row.

    The date comes from startTimeLocal: a 22:30 session belongs to the day it
    was performed in local time, which is also the day workout_log used."""
    start = a.get("startTimeLocal") or ""
    return {
        "garmin_id": a.get("activityId"),
        "date": start[:10] or None,
        "start_local": start or None,
        "type": ((a.get("activityType") or {}).get("typeKey")),
        "name": a.get("activityName"),
        "duration_min": _round((a.get("duration") or 0) / 60),
        "distance_m": _round(a.get("distance"), 0),
        "avg_hr": a.get("averageHR") and round(a["averageHR"]),
        "max_hr": a.get("maxHR") and round(a["maxHR"]),
        "calories": a.get("calories") and round(a["calories"]),
        "raw_json": json.dumps(strip_series(a), default=str),
    }


# --- writes --------------------------------------------------------------

DAILY_COLS = [
    "resting_hr", "min_hr", "max_hr", "hrv_avg_ms", "hrv_status",
    "body_battery_high", "body_battery_low", "body_battery_charged",
    "body_battery_drained", "stress_avg", "stress_max", "sleep_score",
    "sleep_quality", "sleep_total_min", "sleep_deep_min", "sleep_light_min",
    "sleep_rem_min", "sleep_awake_min", "sleep_start", "sleep_end",
    "respiration_avg", "spo2_avg", "spo2_min", "steps", "floors", "distance_m",
    "intensity_min_moderate", "intensity_min_vigorous", "calories_total",
    "calories_active", "calories_bmr", "vo2max", "raw_json",
]


def upsert_daily(con, row: dict) -> dict:
    """Write one garmin_daily row, keeping any value the new payload lacks.

    COALESCE rather than overwrite for the same reason log_weight does it: a
    re-pull where the HRV endpoint 429s would otherwise erase a good reading
    with NULL. Re-syncing can improve a day; it must never degrade one."""
    if not row.get("date"):
        raise GarminDataError("a garmin_daily row needs a date")
    cols = ", ".join(DAILY_COLS)
    holes = ", ".join("?" for _ in DAILY_COLS)
    sets = ", ".join(f"{c} = COALESCE(excluded.{c}, garmin_daily.{c})" for c in DAILY_COLS)
    con.execute(
        f"""INSERT INTO garmin_daily (date, {cols}, synced_at)
            VALUES (?, {holes}, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            ON CONFLICT(date) DO UPDATE SET {sets},
                synced_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
        (row["date"], *(row.get(c) for c in DAILY_COLS)))
    con.commit()
    return {"date": row["date"], "written": sum(1 for c in DAILY_COLS
                                                if row.get(c) is not None and c != "raw_json")}


ACTIVITY_COLS = ["date", "start_local", "type", "name", "duration_min",
                 "distance_m", "avg_hr", "max_hr", "calories", "raw_json"]


def upsert_activity(con, row: dict) -> int:
    if not row.get("garmin_id"):
        raise GarminDataError("a garmin_activity row needs Garmin's activityId")
    cols = ", ".join(ACTIVITY_COLS)
    holes = ", ".join("?" for _ in ACTIVITY_COLS)
    sets = ", ".join(f"{c} = COALESCE(excluded.{c}, garmin_activity.{c})" for c in ACTIVITY_COLS)
    con.execute(
        f"""INSERT INTO garmin_activity (garmin_id, {cols}, synced_at)
            VALUES (?, {holes}, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            ON CONFLICT(garmin_id) DO UPDATE SET {sets},
                synced_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
        (row["garmin_id"], *(row.get(c) for c in ACTIVITY_COLS)))
    con.commit()
    return row["garmin_id"]


def map_exercise_sets(payload: list) -> list[dict]:
    """ACTIVE sets in performed order, each carrying the REST that followed it.

    The payload alternates ACTIVE and REST entries. REST is dropped as a row and
    folded into the set before it, because rest is a property of the set just
    finished, not an event in its own right — and it is recorded nowhere else at
    all. Load arrives in grams.
    """
    out, pending = [], None
    for entry in payload:
        kind = entry.get("setType")
        if kind == "ACTIVE":
            if pending is not None:
                out.append(pending)
            ex = (entry.get("exercises") or [{}])[0]
            pending = {
                "set_no": len(out) + 1,
                "reps": entry.get("repetitionCount"),
                "weight_kg": (entry.get("weight") / 1000.0
                              if entry.get("weight") is not None else None),
                "duration_s": _round(entry.get("duration"), 2),
                "rest_after_s": None,
                "start_local": _local_iso(entry.get("startTime")),
                # subCategory is the specific movement (LAT_PULLDOWN) where the
                # category is its family (PULL_UP); prefer the specific one.
                "category": ex.get("name") or ex.get("category"),
                "probability": ex.get("probability"),
            }
        elif kind == "REST" and pending is not None:
            pending["rest_after_s"] = _round(entry.get("duration"), 2)
    if pending is not None:
        out.append(pending)
    return out


def upsert_exercise_sets(con, garmin_id: int, sets: list[dict]) -> int:
    """Replace the stored per-set record for one activity. Replace rather than
    merge: a re-pull is the corrected version, and a set removed on the watch
    must not survive here."""
    con.execute("DELETE FROM garmin_exercise_set WHERE garmin_id = ?", (garmin_id,))
    for s in sets:
        con.execute(
            """INSERT INTO garmin_exercise_set (garmin_id, set_no, reps, weight_kg,
                                                duration_s, rest_after_s,
                                                start_local, category, probability)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (garmin_id, s["set_no"], s["reps"], s["weight_kg"], s["duration_s"],
             s["rest_after_s"], s["start_local"], s["category"], s["probability"]))
    con.commit()
    return len(sets)


# Activity types worth asking the per-set endpoint about. Anything else has no
# sets and the call would be a wasted round trip against a rate-limited API.
STRENGTH_TYPES = ("strength_training", "indoor_cardio", "pilates", "yoga")


def sync_range(con, api, start: str, end: str) -> dict:
    """Pull every day in [start, end] plus the activities that fall in it."""
    import garmin_client
    from datetime import date as _date, timedelta

    d, last = _date.fromisoformat(start), _date.fromisoformat(end)
    days, partial = 0, {}
    while d <= last:
        iso = d.isoformat()
        raw = garmin_client.fetch_day(api, iso)
        missing = [k for k, v in raw.items() if isinstance(v, dict) and "_error" in v]
        if missing:
            partial[iso] = missing
        upsert_daily(con, map_daily(iso, raw))
        days += 1
        d += timedelta(days=1)

    activities, sets_pulled = 0, 0
    for a in garmin_client.fetch_activities(api, start, end):
        row = map_activity(a)
        upsert_activity(con, row)
        activities += 1
        if row.get("type") in STRENGTH_TYPES:
            try:
                sets_pulled += upsert_exercise_sets(
                    con, row["garmin_id"],
                    map_exercise_sets(
                        garmin_client.fetch_exercise_sets(api, row["garmin_id"])))
            except Exception as e:
                # A missing per-set record must not fail the whole sync: the
                # activity, the daily row and every other session are still good.
                partial.setdefault("exercise_sets", []).append(
                    f"{row['garmin_id']}: {type(e).__name__}")
    return {"days": days, "activities": activities, "sets": sets_pulled,
            "partial": partial}


# --- reads ---------------------------------------------------------------

# What health_day() folds in: the handful of numbers that change a training or
# eating decision. raw_json and the long tail stay out — that output is already
# long enough to skim badly.
SUMMARY_COLS = ["sleep_score", "sleep_total_min", "sleep_deep_min", "sleep_rem_min",
                "resting_hr", "hrv_avg_ms", "hrv_status", "body_battery_high",
                "body_battery_low", "stress_avg", "steps", "calories_active",
                "calories_total", "vo2max"]


def summary(con, date: str) -> dict | None:
    """Compact Garmin view of a day, or None if the watch has nothing for it."""
    row = con.execute("SELECT * FROM garmin_daily WHERE date = ?", (date,)).fetchone()
    acts = [dict(r) for r in con.execute(
        """SELECT garmin_id, start_local, type, name, duration_min, distance_m,
                  avg_hr, max_hr, calories
           FROM garmin_activity WHERE date = ? ORDER BY start_local""", (date,)).fetchall()]
    if row is None and not acts:
        return None
    out = {c: (row[c] if row else None) for c in SUMMARY_COLS}
    out["activities"] = acts
    out["synced_at"] = row["synced_at"] if row else None
    return out


def day(con, date: str) -> dict:
    """Everything Garmin has for one date, raw_json included — the escape hatch
    for a field that was not modelled as a column."""
    row = con.execute("SELECT * FROM garmin_daily WHERE date = ?", (date,)).fetchone()
    acts = [dict(r) for r in con.execute(
        "SELECT * FROM garmin_activity WHERE date = ? ORDER BY start_local", (date,)).fetchall()]
    return {"date": date, "daily": dict(row) if row else None, "activities": acts}
