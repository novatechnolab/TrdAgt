"""
auth.py — Kite Auth Integration
================================
This module bridges your existing Kite login flow with the APEX backend.

OPTION A (recommended): If you already have a login script that generates
an access_token, just paste the token into .env as KITE_ACCESS_TOKEN=...
and this module is not needed at all.

OPTION B: If you want programmatic token generation (e.g. automated login),
implement the functions below.

OPTION C: Use the /kite/auth POST endpoint at runtime to push the token
in after your manual Kite login — no changes needed here.

Kite access_token lifecycle:
  - Valid for one trading day (resets at 6 AM IST)
  - Must be regenerated each day via login → request_token → generate_session
  - Full flow: https://kite.trade/docs/connect/v3/user/
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Path where the APEX backend persists tokens (written by /kite/auth endpoint)
_CONFIG_FILE = Path(__file__).parent / "kite_config.json"


def get_api_key() -> str:
    """Return Kite API key from env or config file."""
    return os.getenv("KITE_API_KEY", "") or _read_config().get("api_key", "")


def get_api_secret() -> str:
    """Return Kite API secret from env."""
    return os.getenv("KITE_API_SECRET", "")


def get_access_token() -> str:
    """
    Return a valid access_token.
    Priority: env var → kite_config.json (written by /kite/auth) → auto-generate
    """
    # 1. Environment variable (set manually or by your existing login script)
    token = os.getenv("KITE_ACCESS_TOKEN", "")
    if token:
        return token

    # 2. Config file written by /kite/auth endpoint
    token = _read_config().get("access_token", "")
    if token:
        return token

    # 3. Auto-generate (implement below if you want fully automated login)
    # token = _generate_token()
    # return token

    return ""


def _read_config() -> dict:
    """Read persisted token config."""
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# OPTIONAL: Automated token generation
# Uncomment and implement if you want fully automated daily login.
# ─────────────────────────────────────────────────────────────────────────────

# def _generate_token() -> str:
#     """
#     Generate access_token from request_token.
#     This requires a request_token obtained via the Kite login URL.
#
#     Steps:
#       1. Open https://kite.trade/connect/login?api_key=<API_KEY>&v=3
#       2. After login, Kite redirects to your redirect_url with ?request_token=XXX
#       3. Paste that token below (or automate via selenium/playwright)
#
#     request_token is valid for only a few minutes.
#     """
#     from kiteconnect import KiteConnect
#     request_token = os.getenv("KITE_REQUEST_TOKEN", "")
#     if not request_token:
#         raise RuntimeError("KITE_REQUEST_TOKEN not set")
#
#     kite = KiteConnect(api_key=get_api_key())
#     session = kite.generate_session(request_token, api_secret=get_api_secret())
#     access_token = session["access_token"]
#
#     # Persist for reuse within the day
#     config = _read_config()
#     config["access_token"] = access_token
#     with open(_CONFIG_FILE, "w") as f:
#         json.dump(config, f)
#
#     return access_token
