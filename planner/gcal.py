"""Google Calendar events for appointment-like tasks.

A task that has BOTH a date and a location smells like an appointment, so it
also gets a Calendar event. Unlike Google Tasks, Calendar events have a real
location field (tappable, opens Maps on the phone) and real times.

The event follows the task: edits update it, completing or deleting the task
removes it. Best-effort throughout — Calendar trouble never blocks the task.
"""

import datetime
import logging
import os
from pathlib import Path

from planner import gtasks

log = logging.getLogger("planner.gcal")

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")


def _machine_timezone() -> str:
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        return "UTC"


TIMEZONE = os.environ.get("CALENDAR_TIMEZONE") or _machine_timezone()
DEFAULT_DURATION = datetime.timedelta(hours=1)


def _service():
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=gtasks.get_credentials(),
                 cache_discovery=False)


def _event_body(task: dict, patching: bool = False) -> dict:
    description = "\n".join(
        p for p in (task.get("description"), task.get("maps_url"),
                    f"🗣 “{task['source']}”" if task.get("source") else None)
        if p
    )
    body = {
        "summary": task["title"],
        "location": task.get("location") or "",
        "description": description,
    }
    if task.get("deadline_time"):
        start = datetime.datetime.fromisoformat(f"{task['deadline']}T{task['deadline_time']}")
        end = start + DEFAULT_DURATION
        body["start"] = {"dateTime": start.isoformat(), "timeZone": TIMEZONE}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": TIMEZONE}
        other = "date"
    else:
        day = datetime.date.fromisoformat(task["deadline"])
        body["start"] = {"date": day.isoformat()}
        body["end"] = {"date": (day + datetime.timedelta(days=1)).isoformat()}
        other = "dateTime"
    if patching:
        # PATCH merges nested fields, so an event switching between all-day
        # and timed keeps the stale field unless it's explicitly cleared.
        body["start"][other] = None
        body["end"][other] = None
    return body


def upsert(task: dict) -> tuple[str | None, str | None]:
    """Create or update the task's event. Returns (event_id, error)."""
    if not gtasks.is_configured():
        return None, "Google not set up"
    try:
        service = _service()
        if task.get("calendar_event_id"):
            try:
                event = service.events().patch(
                    calendarId=CALENDAR_ID, eventId=task["calendar_event_id"],
                    body=_event_body(task, patching=True),
                ).execute()
                return event["id"], None
            except Exception as e:
                if "404" not in str(e) and "410" not in str(e):
                    raise
                # event was deleted from the calendar directly → recreate
        event = service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
        return event["id"], None
    except Exception as e:
        log.exception("calendar upsert failed for %r", task["title"])
        return task.get("calendar_event_id"), str(e)


def sync_task(task: dict) -> tuple[dict, str | None]:
    """Keep the Calendar event in step with the task: active + date + location
    → event exists and matches; anything else → event goes away."""
    from planner import storage

    try:
        if task["status"] == "active" and task["deadline"] and task["location"]:
            event_id, err = upsert(task)
            if event_id != task.get("calendar_event_id"):
                task = storage.update_task(task["id"], {"calendar_event_id": event_id})
        elif task.get("calendar_event_id"):
            err = delete(task["calendar_event_id"])
            if not err:
                task = storage.update_task(task["id"], {"calendar_event_id": None})
        else:
            err = None
    except Exception as e:
        log.exception("calendar sync failed for %r", task["title"])
        err = str(e)
    return task, (f"calendar: {err}" if err else None)


def delete(event_id: str) -> str | None:
    if not gtasks.is_configured():
        return "Google not set up"
    try:
        _service().events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return None
    except Exception as e:
        if "404" in str(e) or "410" in str(e):
            return None  # already gone
        log.exception("calendar delete failed for event %s", event_id)
        return str(e)
