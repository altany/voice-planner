"""Google Tasks integration: insert, update, delete, and list for sync.

Uses the token created by scripts/google_auth.py (one-time browser login).
If Google isn't set up yet, everything degrades gracefully — tasks still live
locally and callers get an explanatory error string.
"""

import datetime
import logging
import os
from pathlib import Path

log = logging.getLogger("planner.gtasks")

_PROJECT_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = Path(os.environ.get("GOOGLE_TOKEN_FILE", _PROJECT_DIR / "google" / "token.json"))
TASKLIST = os.environ.get("GOOGLE_TASKLIST", "@default")

SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar.events",
]


def is_configured() -> bool:
    return TOKEN_FILE.exists()


def get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def _get_service():
    from googleapiclient.discovery import build

    return build("tasks", "v1", credentials=get_credentials(), cache_discovery=False)


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


def compose_notes(task: dict) -> str | None:
    """Google Tasks has no location/description fields, so fold the extras
    into the notes. (One-way: notes are never parsed back during sync.)"""
    # Google Tasks/Calendar auto-link URLs in notes → one tap opens Maps.
    # Prefer the canonical business link (opens the venue's card in Maps);
    # otherwise a short coordinates pin.
    maps_link = task.get("maps_url")
    if not maps_link and task.get("lat") is not None:
        maps_link = f"https://maps.google.com/?q={task['lat']:.5f},{task['lon']:.5f}"
    parts = [
        task.get("description"),
        # Google Tasks' due field can't hold a time, so it rides in the notes.
        f"🕐 {task['deadline_time']}" if task.get("deadline_time") else None,
        f"📍 {task['location']}" if task.get("location") else None,
        maps_link,
        f"🗣 “{task['source']}”" if task.get("source") else None,
    ]
    text = "\n".join(p for p in parts if p)
    return text or None


def _body(task: dict) -> dict:
    body = {"title": task["title"]}
    notes = compose_notes(task)
    if notes:
        body["notes"] = notes
    due = _due_rfc3339(task.get("deadline"))
    body["due"] = due  # None clears a removed deadline on update
    return {k: v for k, v in body.items() if v is not None}


def add_tasks(tasks: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Insert each task. Returns (errors, {local_id: google_id})."""
    if not tasks:
        return [], {}
    if not is_configured():
        msg = f"Google Tasks not set up yet ({TOKEN_FILE} missing) — run scripts/google_auth.py"
        log.warning(msg)
        return [msg], {}
    try:
        service = _get_service()
    except Exception as e:
        log.exception("could not connect to Google Tasks")
        return [f"Google auth failed: {e}"], {}

    errors, ids = [], {}
    for task in tasks:
        try:
            created = service.tasks().insert(tasklist=TASKLIST, body=_body(task)).execute()
            ids[task["id"]] = created["id"]
        except Exception as e:
            log.exception("failed to insert task %r", task["title"])
            errors.append(f"{task['title']}: {e}")
    return errors, ids


def patch_task(google_id: str, task: dict) -> str | None:
    """Push local edits to Google. Returns an error string or None."""
    if not is_configured():
        return "Google Tasks not set up"
    try:
        service = _get_service()
        body = _body(task)
        body["status"] = "completed" if task.get("status") == "completed" else "needsAction"
        service.tasks().update(
            tasklist=TASKLIST, task=google_id, body={"id": google_id, **body}
        ).execute()
        return None
    except Exception as e:
        log.exception("failed to update google task %s", google_id)
        return str(e)


def delete_task(google_id: str) -> str | None:
    if not is_configured():
        return "Google Tasks not set up"
    try:
        service = _get_service()
        service.tasks().delete(tasklist=TASKLIST, task=google_id).execute()
        return None
    except Exception as e:
        # A 404 means it's already gone from Google — that's fine.
        if "404" in str(e):
            return None
        log.exception("failed to delete google task %s", google_id)
        return str(e)


def list_all() -> list[dict]:
    """Every task Google will show us, including completed/hidden/deleted —
    the sync needs all of them to mirror state changes."""
    if not is_configured():
        return []
    service = _get_service()
    items, page_token = [], None
    while True:
        resp = (
            service.tasks()
            .list(
                tasklist=TASKLIST,
                showCompleted=True,
                showHidden=True,
                showDeleted=True,
                maxResults=100,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items
