"""
Gmail OAuth2 authentication for the email monitor tool.
Read-only access only (gmail.readonly scope) — no send, modify, or delete
capability exists anywhere in this module.

One-time setup (run manually from a terminal — never called by the running
bot process, since it needs an interactive browser sign-in):

    cd ~/charlie && source venv/bin/activate
    python3 core/tools/email/auth.py
"""
import sys
import threading
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
ACCOUNT_EMAIL = "jonathan@ts.org"
CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"
TOKENS_DIR = Path(__file__).parent / "tokens"
TOKEN_PATH = TOKENS_DIR / "jonathan_at_ts_dot_org.json"

_lock = threading.Lock()


def get_credentials() -> Credentials:
    """
    Return valid credentials for the monitored account, refreshing the token
    if it has expired. Raises RuntimeError if no token file exists or the
    refresh token itself has been revoked/expired (needs re-running this
    file's __main__ consent flow, not something this function can fix).
    """
    with _lock:
        if not TOKEN_PATH.exists():
            raise RuntimeError(
                f"No Gmail token found at {TOKEN_PATH}. Run: "
                f"python3 {Path(__file__)} to authorise."
            )

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    raise RuntimeError(
                        f"Gmail token refresh was rejected ({e}) — the refresh token "
                        f"has likely been revoked or expired. Run: "
                        f"python3 {Path(__file__)} to re-authorise."
                    )
                TOKEN_PATH.write_text(creds.to_json())
            else:
                raise RuntimeError(
                    f"Gmail token at {TOKEN_PATH} is invalid and cannot be refreshed. "
                    f"Run: python3 {Path(__file__)} to re-authorise."
                )

        return creds


def run_initial_auth_flow() -> None:
    """
    One-time interactive OAuth consent flow. Opens a browser for Google
    sign-in, then saves the resulting token. Must be run from a terminal —
    never from the running bot process.
    """
    TOKENS_DIR.mkdir(exist_ok=True)

    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"credentials.json not found at {CREDENTIALS_PATH}")

    print(f"Opening browser for Google sign-in ({ACCOUNT_EMAIL})...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.write_text(creds.to_json())
    print(f"Done. Token saved for {ACCOUNT_EMAIL} at {TOKEN_PATH}.")


if __name__ == "__main__":
    run_initial_auth_flow()
