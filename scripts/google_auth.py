"""One-time Google Tasks authorization.

Run this ON THE PI, but with an SSH tunnel so the login page can open in the
browser on your laptop:

    ssh -L 8899:localhost:8899 <user>@<pi>
    cd ~/voice-planner
    .venv/bin/python scripts/google_auth.py

Then open the printed URL in your laptop's browser, log in to Google, and
approve. The token is saved to google/token.json and refreshes itself from
then on — you should never need to run this again.
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

PROJECT_DIR = Path(__file__).resolve().parent.parent
GOOGLE_DIR = PROJECT_DIR / "google"
CLIENT_SECRET = GOOGLE_DIR / "client_secret.json"
TOKEN_FILE = GOOGLE_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar.events",
]


def main():
    if not CLIENT_SECRET.exists():
        sys.exit(
            f"Missing {CLIENT_SECRET}\n"
            "Download the OAuth client JSON from Google Cloud Console "
            "(see README, 'Google Tasks one-time setup') and save it there first."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=8899,
        open_browser=False,
        authorization_prompt_message=(
            "\nOpen this URL in the browser on your laptop "
            "(make sure you connected with: ssh -L 8899:localhost:8899 ...):\n\n{url}\n"
        ),
    )

    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    print(f"\nDone! Token saved to {TOKEN_FILE}")
    print("Google Tasks is now connected — restart the service if it's running:")
    print("  sudo systemctl restart voice-planner")


if __name__ == "__main__":
    main()
