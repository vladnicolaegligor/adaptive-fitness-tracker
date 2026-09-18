#!/usr/bin/env python3.13
"""Garmin Connect -> unified.db sync.

    python3.13 src/garmin_sync.py auth --email you@example.com   # once, interactive
    python3.13 src/garmin_sync.py probe [--date YYYY-MM-DD]      # dump raw payloads
    python3.13 src/garmin_sync.py sync  [--days N]               # the daily job

`sync` re-pulls the last N days (default 3) rather than just yesterday:
Garmin revises sleep, Body Battery and HRV after the fact, so a rolling window
corrects yesterday's provisional numbers. Every write is an upsert, so running
it twice changes nothing.
"""
import argparse
import sys
import time
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_client
from garmin_client import GarminError

ACCOUNT_FILE = garmin_client.TOKEN_DIR / "account.txt"


def _account(email: str | None) -> str:
    """The Connect email: given explicitly, else remembered from `auth`."""
    if email:
        return email
    if ACCOUNT_FILE.exists():
        return ACCOUNT_FILE.read_text().strip()
    raise GarminError("no account on file — run `garmin_sync.py auth --email ...` first")


def _wait_for_mfa_code(path: Path, timeout_s: int = 300):
    """Read the MFA code out of a file the user drops it into.

    Garmin keeps the pending-MFA state on the client object, so the code must
    arrive inside this same process — but a stdin prompt is useless when the
    process was launched by an agent with no terminal. A watched file is the
    one channel that works either way.
    """
    def wait() -> str:
        print(f"MFA required. Write the code to {path}, e.g.\n"
              f"  echo 123456 > {path}", flush=True)
        for _ in range(timeout_s):
            if path.exists():
                code = path.read_text().strip()
                if code:
                    path.unlink(missing_ok=True)   # a used code is a spent code
                    return code
            time.sleep(1)
        raise GarminError(f"no MFA code appeared in {path} within {timeout_s}s")
    return wait


def cmd_auth(args) -> int:
    """One login. Everything after this runs off the cached token.

    Non-interactive by design: email from the flag, password from the keychain,
    MFA code from a watched file. Nothing here reads stdin."""
    email = args.email or (ACCOUNT_FILE.exists() and ACCOUNT_FILE.read_text().strip())
    if not email:
        raise GarminError("pass --email (the Garmin Connect account address)")
    password = garmin_client.keychain_password(email)
    print("password: read from keychain")

    api = garmin_client.connect(
        email, password, mfa_prompt=_wait_for_mfa_code(Path(args.mfa_file)))
    garmin_client.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNT_FILE.write_text(email + "\n")
    name = api.get_full_name()
    print(f"logged in as {name}; token cached in {garmin_client.TOKEN_DIR}")
    return 0


def cmd_probe(args) -> int:
    email = _account(args.email)
    api = garmin_client.connect(email)
    day = args.date or (_date.today() - timedelta(days=1)).isoformat()
    out = Path(args.out or f"/tmp/garmin_probe_{day}.json")
    garmin_client.probe(api, day, out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


def cmd_sync(args) -> int:
    import garmin_core
    email = _account(args.email)
    api = garmin_client.connect(email)
    end = _date.fromisoformat(args.end) if args.end else _date.today()
    start = end - timedelta(days=args.days - 1)

    with garmin_core.connect() as con:
        result = garmin_core.sync_range(con, api, start.isoformat(), end.isoformat())
    print(f"{start}..{end}: {result['days']} days, {result['activities']} activities")
    for d, err in result.get("partial", {}).items():
        print(f"  {d}: missing {', '.join(err)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="interactive login; caches a token")
    a.add_argument("--email")
    a.add_argument("--mfa-file", default="/tmp/garmin_mfa_code",
                   help="file this process watches for an MFA code")
    a.set_defaults(fn=cmd_auth)

    b = sub.add_parser("probe", help="dump one day of raw payloads to a file")
    b.add_argument("--date")
    b.add_argument("--email")
    b.add_argument("--out")
    b.set_defaults(fn=cmd_probe)

    c = sub.add_parser("sync", help="pull days + activities into unified.db")
    c.add_argument("--days", type=int, default=3)
    c.add_argument("--end", help="last date of the window (default today)")
    c.add_argument("--email")
    c.set_defaults(fn=cmd_sync)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except GarminError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
