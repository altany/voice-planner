"""Append results to a local JSON-lines file on the Pi."""

import datetime
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("planner.storage")

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
TASKS_FILE = DATA_DIR / "tasks.jsonl"


def append_record(transcript: str, tasks: list[dict] | None, note: str | None = None) -> None:
    """One line per voice note. When extraction failed, tasks is null and the
    transcript is still kept, so nothing is ever lost."""
    record = {
        "received_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "transcript": transcript,
        "tasks": tasks,
    }
    if note:
        record["note"] = note

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TASKS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("saved %s task(s) to %s", len(tasks) if tasks else 0, TASKS_FILE)


def read_tasks(limit: int = 100) -> list[dict]:
    """Most recent tasks first, flattened across voice notes. Notes whose
    extraction failed (tasks: null) are skipped; malformed lines are ignored."""
    if not TASKS_FILE.exists():
        return []
    items = []
    with open(TASKS_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            for task in record.get("tasks") or []:
                items.append({**task, "received_at": record.get("received_at")})
    return items[-limit:][::-1]
