"""Mirror Google Tasks state into the local store.

Direction of truth:
- Local → Google happens immediately when something changes here (insert on
  new note, patch/delete from the page). Never during sync.
- Google → local happens here: deletes, completions, renames, and due-date
  changes made in the Google Tasks app land in the local DB. Tasks created
  directly in Google get imported too.

Runs at most once per THROTTLE_SECONDS, piggybacked on page loads, so there is
no background daemon to babysit.
"""

import logging
import threading
import time

from planner import gcal, gtasks, storage

log = logging.getLogger("planner.sync")

THROTTLE_SECONDS = 30
_lock = threading.Lock()
_last_sync = 0.0


def _gdate(gtask: dict) -> str | None:
    due = gtask.get("due")
    return due[:10] if due else None


def reconcile(force: bool = False) -> None:
    """Best-effort; callers wrap in try/except. Quietly does nothing when
    Google isn't configured or was synced moments ago."""
    global _last_sync
    if not gtasks.is_configured():
        return
    with _lock:
        if not force and time.monotonic() - _last_sync < THROTTLE_SECONDS:
            return
        _last_sync = time.monotonic()

        google = {g["id"]: g for g in gtasks.list_all()}
        local = storage.list_tasks(include_deleted=True)
        seen_gids = set()

        for task in local:
            gid = task["google_id"]
            if not gid:
                continue
            seen_gids.add(gid)
            g = google.get(gid)
            if task["status"] == "deleted":
                continue  # archive rows: nothing to mirror either way
            if g is None or g.get("deleted"):
                mirrored = storage.update_task(task["id"], {"status": "deleted"})
                gcal.sync_task(mirrored)
                log.info("google delete mirrored locally: %s", task["title"])
                continue
            changes = {}
            g_status = "completed" if g.get("status") == "completed" else "active"
            if g_status != task["status"]:
                changes["status"] = g_status
            if g.get("title") and g["title"] != task["title"]:
                changes["title"] = g["title"]
            if _gdate(g) != task["deadline"]:
                changes["deadline"] = _gdate(g)
            if changes:
                mirrored = storage.update_task(task["id"], changes)
                gcal.sync_task(mirrored)
                log.info("google edit mirrored locally: %s %s", task["title"], list(changes))

        # Tasks living only in Google: link to an unlinked local twin if one
        # exists (pre-sync era rows), otherwise import them.
        for gid, g in google.items():
            if gid in seen_gids or g.get("deleted") or not g.get("title"):
                continue
            status = "completed" if g.get("status") == "completed" else "active"
            twin = storage.find_unlinked(g["title"])
            if twin:
                storage.update_task(twin["id"], {"google_id": gid, "status": status,
                                                 "deadline": _gdate(g)})
                log.info("linked google task to local twin: %s", g["title"])
            else:
                rows = storage.add_tasks([{"title": g["title"], "deadline": _gdate(g)}])
                storage.update_task(rows[0]["id"], {"google_id": gid, "status": status})
                log.info("imported task from google: %s", g["title"])
