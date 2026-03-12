"""Garmin Connect authentication helper."""

import sys
from getpass import getpass
from pathlib import Path

from garminconnect import Garmin, GarminConnectAuthenticationError

TOKENSTORE = str(Path("~/.garminconnect").expanduser())


def is_authenticated() -> bool:
    """Check if valid stored tokens exist."""
    try:
        garmin = Garmin()
        garmin.login(TOKENSTORE)
        return True
    except Exception:
        return False


def get_client() -> Garmin:
    """Return an authenticated Garmin client, or exit with a helpful message."""
    try:
        garmin = Garmin()
        garmin.login(TOKENSTORE)
        return garmin
    except Exception:
        print(
            "Not authenticated. Run: python -m garmin_sailing setup",
            file=sys.stderr,
        )
        sys.exit(1)


def setup():
    """Interactive setup: authenticate with Garmin Connect and store tokens."""
    tokenstore = Path(TOKENSTORE)

    # Check existing tokens
    try:
        garmin = Garmin()
        garmin.login(str(tokenstore))
        name = garmin.get_full_name()
        print(f"Already authenticated as {name}!")
        print(f"Tokens stored in {tokenstore}")
        return
    except Exception:
        print("Let's connect your Garmin account.\n")

    email = input("Garmin email: ")
    password = getpass("Garmin password: ")

    try:
        garmin = Garmin(
            email=email, password=password, is_cn=False, return_on_mfa=True
        )
        result1, result2 = garmin.login()

        if result1 == "needs_mfa":
            mfa_code = input("Enter MFA code: ")
            garmin.resume_login(result2, mfa_code)

        garmin.garth.dump(str(tokenstore))
        name = garmin.get_full_name()
        print(f"\nAuthenticated as {name}!")
        print(f"Tokens saved to {tokenstore}")
        print("\nYou're all set! Add this server to Claude Desktop and start sailing.")

    except GarminConnectAuthenticationError as e:
        print(f"\nAuthentication failed: {e}", file=sys.stderr)
        sys.exit(1)
