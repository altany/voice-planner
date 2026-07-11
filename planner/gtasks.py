"""Add tasks to Google Tasks.

Uses the token created by scripts/google_auth.py (one-time browser login).
If Google isn't set up yet, tasks are still saved locally — this module just
reports why the Google side was skipped.
"""

import datetime
import logging
import os
from pathlib import Path

log = logging.getLogger("planner.gtasks")

_PROJECT_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = Path(os.environ.get("GOOGLE_TOKEN_FILE", _PROJECT_DIR / "google" / "token.json"))
TASKLIST = os.environ.get("GOOGLE_TASKLIST", "@default")

SCOPES = ["https://www.googleapis.com/auth/tasks"]


def _get_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _due_rfc3339(deadline: str | None) -> str | None:
    if not deadline:
        return None
    try:
        day = datetime.date.fromisoformat(deadline)
    except ValueError:
        log.warning("ignoring unparseable deadline %r", deadline)
        return None
    # Google Tasks only stores the date part of `due`.
    return f"{day.isoformat()}T00:00:00.000Z"


def add_tasks(tasks: list[dict]) -> list[str]:
    """Insert each task; returns a list of error strings (empty = all good)."""
    if not tasks:
        return []
    if not TOKEN_FILE.exists():
        msg = f"Google Tasks not set up yet ({TOKEN_FILE} missing) — run scripts/google_auth.py"
        log.warning(msg)
        return [msg]

    try:
        service = _get_service()
    except Exception as e:
        log.exception("could not connect to Google Tasks")
        return [f"Google auth failed: {e}"]

    errors = []
    for task in tasks:
        body = {"title": task["title"]}
        # Google Tasks has no location/description fields, so fold everything
        # extra into the notes.
        notes = [
            task.get("description"),
            f"📍 {task['location']}" if task.get("location") else None,
            f"🗣 “{task['source']}”" if task.get("source") else None,
        ]
        if any(notes):
            body["notes"] = "\n".join(n for n in notes if n)
        due = _due_rfc3339(task.get("deadline"))
        if due:
            body["due"] = due
        try:
            service.tasks().insert(tasklist=TASKLIST, body=body).execute()
        except Exception as e:
            log.exception("failed to insert task %r", task["title"])
            errors.append(f"{task['title']}: {e}")
    return errors
