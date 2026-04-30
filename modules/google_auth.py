"""
google_auth.py — FRIDAY's Google OAuth front door.

Owns the one-time browser auth dance, caches the refresh token to disk,
and hands every other module an already-authorized Calendar API service
object.

OAuth flow in three states:
    1. token.json exists and is still valid     -> load and reuse
    2. token.json exists but is expired         -> refresh silently
    3. no token, or expired without refresh tkn -> open browser, ask user

The first run opens a browser to accounts.google.com. After the user picks
their account and clicks through the unverified-app warning (FRIDAY lives
in Google Cloud "Testing" mode), the resulting refresh token is saved to
token.json and reused indefinitely.

Where to get credentials.json:
    https://console.cloud.google.com/apis/credentials
    Create OAuth 2.0 Client ID -> Desktop application -> download JSON
    -> rename to credentials.json -> drop in this project's root.

Test directly:
    python modules/google_auth.py
"""

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Silences "file_cache is unavailable when using oauth2client >= 4.0.0".
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CREDENTIALS_FILE = _PROJECT_ROOT / "credentials.json"
_TOKEN_FILE = _PROJECT_ROOT / "token.json"

# Read-only is the safe minimum — FRIDAY should never mutate the calendar.
_SCOPES: List[str] = ["https://www.googleapis.com/auth/calendar.readonly"]

# Per-process service cache. Keeps the [auth] log lines down to one per run
# even when many callers (today, next event, week, briefing) ask for it.
_service_cache: Optional[object] = None


def _load_cached_credentials() -> Credentials:
    """Load credentials from token.json. Caller must check .valid / .expired."""
    return Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)


def _save_credentials(creds: Credentials) -> None:
    """Persist credentials to token.json so the next run skips the browser."""
    with _TOKEN_FILE.open("w", encoding="utf-8") as f:
        f.write(creds.to_json())


def _run_oauth_flow() -> Credentials:
    """Open the browser, ask for consent, return fresh credentials."""
    if not _CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Missing OAuth client secrets at {_CREDENTIALS_FILE}.\n"
            "Get one at https://console.cloud.google.com/apis/credentials\n"
            "(Create OAuth 2.0 Client ID -> Desktop application -> download JSON -> "
            "rename to credentials.json -> drop in the FRIDAY project root.)"
        )

    print("[auth] Starting OAuth flow — your browser will open")
    flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_FILE), _SCOPES)
    return flow.run_local_server(
        port=0,
        prompt="select_account",
    )


def get_calendar_service():
    """
    Return an authorized Google Calendar v3 service object.

    Handles all three credential states (cached / refreshable / fresh-flow),
    caches the resulting token to disk so subsequent runs are silent, and
    caches the built service in-memory so subsequent calls in the same
    process don't repeat the auth log lines or rebuild the client.

    Raises:
        FileNotFoundError: if a fresh OAuth flow is needed but credentials.json
            is missing from the project root.
    """
    global _service_cache
    if _service_cache is not None:
        return _service_cache

    creds: Credentials = None  # type: ignore[assignment]

    if _TOKEN_FILE.exists():
        creds = _load_cached_credentials()

    if creds and creds.valid:
        print("[auth] Using cached token")
    elif creds and creds.expired and creds.refresh_token:
        print("[auth] Refreshing expired token")
        creds.refresh(Request())
        _save_credentials(creds)
    else:
        creds = _run_oauth_flow()
        _save_credentials(creds)

    _service_cache = build("calendar", "v3", credentials=creds)
    return _service_cache


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        service = get_calendar_service()
        print("[auth] Calendar service ready.")

        # Smoke test: list calendars without touching events.
        result = service.calendarList().list().execute()
        calendars = result.get("items", [])
        primary = next((c for c in calendars if c.get("primary")), None)

        print(f"[auth] Calendars accessible: {len(calendars)}")
        if primary:
            print(f"[auth] Primary calendar: {primary.get('summary', '(no summary)')}")
        else:
            print("[auth] No primary calendar found in the list.")

        print("[auth] All calendars:")
        for cal in calendars:
            print(f"  - {cal.get('summary', '(no summary)')}")

    except FileNotFoundError as e:
        print(f"[auth] Setup error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[auth] Auth or API failure: {type(e).__name__}: {e}")
        sys.exit(1)
