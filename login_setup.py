#!/usr/bin/env python3
"""
One-time (or occasional) interactive Garmin Connect login.

Run this YOURSELF in a normal Terminal window on your Mac:

    cd ~/Documents/garmin-coach
    source venv/bin/activate
    python3 login_setup.py

You'll be asked for your Garmin Connect email and password (hidden as you
type), and an MFA code if you have two-factor login enabled on Garmin.
Nothing you type here goes anywhere except Garmin's own login servers --
it is never seen by, sent to, or stored by Claude.

On success, your session is cached in garmin_tokens/ next to this script.
That cache (not your password) is what lets fetch_activities.py pull your
data later without you doing anything further. Garmin sessions are
typically valid for months, so you should rarely need to re-run this.
"""
import getpass
import sys
from pathlib import Path

from garminconnect import Garmin

TOKENSTORE = Path(__file__).parent / "garmin_tokens"


def main():
    print("=== Garmin Connect login ===")
    email = input("Garmin Connect email: ").strip()
    password = getpass.getpass("Garmin Connect password (hidden): ")

    garmin = Garmin(email=email, password=password)
    try:
        garmin.login()
    except Exception as e:
        print(f"\nLogin failed: {e}", file=sys.stderr)
        print("If you have two-factor auth enabled and were not prompted for a", file=sys.stderr)
        print("code, or the library needs updating, tell Claude the error above.", file=sys.stderr)
        sys.exit(1)

    TOKENSTORE.mkdir(parents=True, exist_ok=True)
    garmin.garth.dump(str(TOKENSTORE))

    print(f"\nLogin succeeded. Session cached in: {TOKENSTORE}")
    print("You can close this terminal now -- ask Claude to fetch/evaluate your runs any time.")


if __name__ == "__main__":
    main()
